"""F04-B / B02 — dépendances de production qualifiées et verrouillées.

requirements.in : dépendances DIRECTES, toutes épinglées (MAIN_REQUIREMENTS).
requirements.txt (point d'entrée Railway) : verrou COMPLET avec empreintes,
généré par la commande notée dans son en-tête ; pip passe automatiquement en
mode « hash-checking » (toute distribution non conforme est refusée).
Cohérence exacte avec ci/requirements-ci.txt (mêmes versions, mêmes
empreintes) ; runtime.txt inchangé ; aucun alias global httpx / httpx2.
"""
import ast
import importlib.metadata
import re

from tests.test_f2_dormancy import MAIN_REQUIREMENTS, production_requirements
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

COMPILE = ("uv pip compile requirements.in --python-version 3.11 --python-platform x86_64-manylinux_2_28 "
           "--exclude-newer 2026-10-05T00:00:00Z --generate-hashes -o requirements.txt")
# Fermeture exacte du verrou de production (47 distributions), versions
# qualifiées par la CI.
PRODUCTION_LOCK = {
    "alembic": "1.20.0", "annotated-types": "0.8.0", "anthropic": "1.11.0", "anyio": "4.15.1",
    "beautifulsoup4": "4.15.0", "certifi": "2026.7.22", "cffi": "2.1.1", "charset-normalizer": "3.5.2",
    "click": "8.5.0", "curl-cffi": "0.16.3", "docstring-parser": "0.18.0", "fastapi": "0.110.0", "h11": "0.16.0",
    "httpcore": "1.0.9", "httpcore2": "2.13.1", "httpx": "0.28.1", "httpx2": "2.13.1", "idna": "3.20",
    "jiter": "0.17.0", "lxml": "6.1.3", "mako": "1.4.3", "markupsafe": "3.0.4", "multitasking": "0.0.13",
    "numpy": "2.4.6", "pandas": "3.0.6", "peewee": "4.5.2", "platformdirs": "4.12.3", "protobuf": "7.36.2",
    "psycopg2-binary": "2.9.13", "pycparser": "3.0", "pydantic": "2.10.0", "pydantic-core": "2.27.0",
    "python-dateutil": "2.9.0.post0", "python-dotenv": "1.2.4", "pytz": "2026.5", "requests": "2.34.2",
    "six": "1.17.0", "sniffio": "1.3.1", "soupsieve": "2.10", "sqlalchemy": "2.1.3", "starlette": "0.36.3",
    "truststore": "0.10.4", "typing-extensions": "4.16.0", "urllib3": "2.8.0", "uvicorn": "0.27.0",
    "websockets": "17.2", "yfinance": "1.7.0",
}
CI_ONLY = {"iniconfig", "packaging", "pluggy", "pygments", "pytest", "pyyaml"}
_PIN = re.compile(r"^([a-z0-9][a-z0-9._-]*)==([^\s\\]+) \\$")
_HASH = re.compile(r"^\s+--hash=(sha256:[0-9a-f]{64})(?: \\)?$")


def _lock(path) -> dict:
    """{distribution: (version, {empreintes})} ; refuse toute ligne inattendue."""
    entries, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pin, digest = _PIN.match(line), _HASH.match(line)
        if pin:
            current = pin.group(1)
            assert current not in entries, current
            entries[current] = (pin.group(2), set())
        elif digest and current is not None:
            entries[current][1].add(digest.group(1))
        else:
            raise AssertionError(f"ligne de verrou inattendue : {line!r}")
    return entries


def test_direct_production_dependencies_are_explicit_and_pinned():
    assert production_requirements() == MAIN_REQUIREMENTS
    for requirement in MAIN_REQUIREMENTS:
        assert re.fullmatch(r"[a-z0-9-]+==[0-9][0-9a-z.]*", requirement), requirement
    names = [r.split("==")[0] for r in MAIN_REQUIREMENTS]
    for name in ("fastapi", "uvicorn", "starlette", "anthropic", "httpx", "httpx2", "requests", "sqlalchemy",
                 "psycopg2-binary", "alembic"):
        assert name in names, name


def test_requirements_txt_is_the_complete_hashed_lock_of_requirements_in():
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert COMPILE in text.splitlines()[1]
    assert COMPILE in (REPO_ROOT / "requirements.in").read_text(encoding="utf-8")
    lock = _lock(REPO_ROOT / "requirements.txt")
    assert {name: version for name, (version, _) in lock.items()} == PRODUCTION_LOCK
    for name, (_, hashes) in lock.items():
        assert hashes, name  # au moins une empreinte : pip impose alors le mode hash-checking
    for requirement in MAIN_REQUIREMENTS:
        name, version = requirement.split("==")
        assert lock[name][0] == version, requirement
    for forbidden in ("--index-url", "--extra-index-url", "--trusted-host", "--find-links", "-e ", "git+", "http://",
                      "file:"):
        assert forbidden not in text, forbidden


def test_production_lock_is_exactly_the_ci_qualified_subset():
    production, ci = _lock(REPO_ROOT / "requirements.txt"), _lock(REPO_ROOT / "ci" / "requirements-ci.txt")
    assert set(ci) - set(production) == CI_ONLY  # outils de test seulement
    for name, (version, hashes) in production.items():
        assert ci[name] == (version, hashes), name  # même version, mêmes empreintes


def test_ci_inputs_still_install_the_production_requirements_plus_test_tools():
    inputs = (REPO_ROOT / "ci" / "requirements-ci.in").read_text(encoding="utf-8")
    assert "-r ../requirements.txt" in inputs and "\nhttpx\n" in inputs and "\npytest\n" in inputs


def test_runtime_is_unchanged():
    assert (REPO_ROOT / "runtime.txt").read_text(encoding="utf-8").strip() == "python-3.11.9"


def test_the_test_environment_runs_the_locked_versions():
    for requirement in MAIN_REQUIREMENTS:
        name, version = requirement.split("==")
        assert importlib.metadata.version(name) == version, requirement


def test_no_global_httpx_alias_anywhere():
    roots = [REPO_ROOT / "api.py", REPO_ROOT / "core", REPO_ROOT / "scripts", REPO_ROOT / "alembic", REPO_ROOT / "ci",
             REPO_ROOT / "tests"]
    for root in roots:
        for path in ([root] if root.is_file() else sorted(root.rglob("*.py"))):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in ("httpx", "httpx2") and alias.asname not in (None, alias.name):
                            assert alias.asname not in ("httpx", "httpx2"), (path, alias.name, alias.asname)
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "sys.modules":
                    assert ast.unparse(node.slice) not in ("'httpx'", "'httpx2'"), path
    providers = (REPO_ROOT / "core" / "decryptage_providers.py").read_text(encoding="utf-8")
    assert "import httpx2\n" in providers and "import httpx\n" not in providers
    adapters = (REPO_ROOT / "core" / "auth_f2" / "http_providers.py").read_text(encoding="utf-8")
    assert "    import httpx\n" in adapters and "httpx2" not in adapters
