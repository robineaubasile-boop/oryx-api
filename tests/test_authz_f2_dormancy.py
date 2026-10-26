"""Dormance de production de R1 PR 5 (C.5.2 §3, C.5.2-B).

Le véritable point d'entrée api:app n'importe ni ne monte la frontière
d'autorisation (core/authz_f2) ni la route personnelle (core/authz_f2_http) :

- GET /api/v2/me répond 404 sur le vrai api:app, sans cookie, sans module
  F2 / PR 5 chargé, sans accès réseau, avec ou sans variables évoquant une
  activation (sous-processus à environnement contrôlé) ;
- aucun module applicatif ne référence ces paquets (seuls tests et docs) ;
- ils ne lisent aucune configuration, n'ouvrent aucune connexion et ne
  chargent aucun client à l'import ;
- matrice FERMÉE de leurs imports internes (aucune dépendance nouvelle,
  ni core.db, ni core.models, ni fournisseur) ;
- aucune migration nouvelle (tête Alembic 0019_f2_http_admission), aucune
  dépendance de production nouvelle.

Les garanties historiques (routes publiques exactes, middlewares, imports
interdits, chargement dynamique, réseau au démarrage, variables F2 valides
ou invalides) restent vérifiées par tests/test_f2_dormancy.py et
tests/test_f2_http_dormancy.py, dont la sonde couvre aussi /api/v2/me.
"""
import ast
import json
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.test_f2_dormancy import MAIN_REQUIREMENTS, MAIN_ROUTES
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

AUTHZ = REPO_ROOT / "core" / "authz_f2"
AUTHZ_HTTP = REPO_ROOT / "core" / "authz_f2_http"
HEAD = "0019_f2_http_admission"

# Matrice fermée : module -> imports core.* exacts.
IMPORT_MATRIX = {
    "core/authz_f2/__init__.py": set(),
    "core/authz_f2/principal.py": set(),
    "core/authz_f2/commands.py": set(),
    "core/authz_f2/_adapters.py": {"core.authz_f2.commands"},
    "core/authz_f2/runner.py": {"core.auth_f2.sessions", "core.auth_f2.keys", "core.auth_f2.txn",
                                "core.authz_f2._adapters", "core.authz_f2.commands", "core.authz_f2.principal"},
    "core/authz_f2_http/__init__.py": set(),
    "core/authz_f2_http/provenance.py": {"core.auth_f2_http.provenance"},
    "core/authz_f2_http/routes.py": {"core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies",
                                     "core.auth_f2_http.provenance", "core.auth_f2_http.routes",
                                     "core.authz_f2.commands", "core.authz_f2.principal", "core.authz_f2.runner",
                                     "core.authz_f2_http.provenance"},
    "core/authz_f2_http/isolated.py": {"core.auth_f2_http.isolated", "core.auth_f2_http.provenance",
                                       "core.authz_f2.runner", "core.authz_f2_http.routes"},
}
PACKAGES = ("core.auth_f2", "core.auth_f2_http", "core.authz_f2", "core.authz_f2_http")

PROBE = r'''
import asyncio, json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit pendant le test de dormance")
socket.socket.connect = guard
socket.create_connection = lambda *a, **k: guard(None, a[0] if a else None)

import api
import httpx

GEN = "11111111-2222-4333-8444-555555555555"
COOKIE = "__Host-oryx-active-" + GEN + "=" + "A" * 43
CASES = [
    ("GET", "/api/v2/me", {}),
    ("GET", "/api/v2/me", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me?user_id=user-a", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN}),
    ("POST", "/api/v2/me", {"origin": "https://oryx.test", "cookie": COOKIE}),
    ("GET", "/api/v2", {}),
]

async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1))
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path, headers in CASES:
            r = await client.request(method, path, headers=headers)
            out.append([method, path, r.status_code, r.headers.get_list("set-cookie"), r.text[:200]])
    return out

responses = asyncio.run(probe())
routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
MONITORED = ("core.auth_f2", "core.auth_f2_http", "core.authz_f2", "core.authz_f2_http")
loaded = sorted(m for m in sys.modules if any(m == p or m.startswith(p + ".") for p in MONITORED))
print(json.dumps({"responses": responses, "routes": routes, "loaded": loaded, "attempts": attempts,
                  "middleware": [m.cls.__name__ for m in api.app.user_middleware]}))
'''

ENVIRONMENTS = {
    "none": {},
    "pr5_flags": {"ORYX_AUTHZ_F2": "1", "ORYX_PR5_ENABLED": "1", "ORYX_V2_ME": "true", "ORYX_API_V2": "on",
                  "ORYX_F2_ENABLED": "1", "ORYX_F2_ALLOWED_ORIGINS": "https://oryx.test",
                  "ORYX_F2_KEYS": json.dumps({"active": {"t1": "A" * 43}})},
    "invalid_pr5_flags": {"ORYX_AUTHZ_F2": "maybe", "ORYX_F2_KEYS": "{", "ORYX_F2_ALLOWED_ORIGINS": "*"},
}


def _boundary_files():
    return sorted(list(AUTHZ.glob("*.py")) + list(AUTHZ_HTTP.glob("*.py")))


def _core_imports(path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("core")}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("core"):
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}" if node.module in PACKAGES else node.module)
    return found


@pytest.mark.parametrize("case", sorted(ENVIRONMENTS))
def test_real_entrypoint_answers_404_on_api_v2_me(case):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(ENVIRONMENTS[case]), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(result["responses"]) == 6
    for method, path, status, cookies, _ in result["responses"]:
        assert status == 404, (method, path, status)
        assert cookies == [], (method, path)
    assert result["loaded"] == []
    assert result["attempts"] == []
    assert result["middleware"] == ["CORSMiddleware"]
    assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
        set(MAIN_ROUTES))


def test_api_source_never_mentions_the_authorization_boundary():
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for forbidden in ("authz", "/api/v2", "AuthorizedTransactionRunner", "build_isolated_personal_app",
                      "x-oryx-generation-id", "X-Oryx-Generation-Id", "include_router", "mount(", "lifespan"):
        assert forbidden not in source, forbidden


def test_only_tests_and_docs_reference_the_boundary():
    offenders = []
    markers = ("authz_f2", "AuthorizedTransactionRunner", "build_isolated_personal_app", "/api/v2/me")
    for path in REPO_ROOT.rglob("*"):
        if (not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or "node_modules" in path.parts
                or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh", ".yml", ".yaml", ".toml",
                                       ".ini", ".cfg", ".txt", ".md")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs") or AUTHZ in path.parents or AUTHZ_HTTP in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in markers):
            offenders.append(str(relative))
    assert offenders == []


def test_boundary_import_matrix_is_closed():
    files = {str(p.relative_to(REPO_ROOT)) for p in _boundary_files()}
    assert files == set(IMPORT_MATRIX)
    for relative, expected in IMPORT_MATRIX.items():
        assert _core_imports(REPO_ROOT / relative) == expected, relative


def test_boundary_reads_no_configuration_and_starts_nothing():
    for path in _boundary_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert not roots & {"os", "dotenv", "api", "requests", "httpx", "uvicorn", "anthropic", "socket"}, path.name
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"environ", "getenv", "SessionLocal", "get_db", "initialize_abuse_key_state",
                            "rotate_abuse_key", "on_event", "lifespan", "add_event_handler", "set_cookie",
                            "clear_cookie", "CORSMiddleware"}, path.name


def test_importing_the_boundary_opens_nothing():
    code = ("import socket, sys, importlib\n"
            "attempts = []\n"
            "socket.socket.connect = lambda self, address: attempts.append(address)\n"
            "import psycopg2\n"
            "psycopg2.connect = lambda *a, **k: attempts.append('psycopg2')\n"
            "for m in ('core.authz_f2', 'core.authz_f2.runner', 'core.authz_f2.commands', 'core.authz_f2._adapters',\n"
            "          'core.authz_f2_http', 'core.authz_f2_http.routes', 'core.authz_f2_http.isolated'):\n"
            "    importlib.import_module(m)\n"
            "print(' '.join(str(x in sys.modules) for x in ('api', 'httpx', 'core.db', 'core.models')), attempts)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), check=True)
    assert out.stdout.strip() == "False False False False []"


def test_no_new_migration_and_no_new_production_dependency():
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == [HEAD]
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 19 and versions[-1] == HEAD + ".py"
    lines = [line.strip() for line in (REPO_ROOT / "requirements.txt").read_text().splitlines() if line.strip()]
    assert lines == MAIN_REQUIREMENTS
