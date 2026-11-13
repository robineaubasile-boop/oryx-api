"""Dormance intégrale de F-02A V0.2 sur le véritable point d'entrée api:app.

- api:app garde EXACTEMENT ses routes (déclarées et automatiques), son
  middleware (CORSMiddleware historique) et ses comportements : aucune
  route legacy ne répond 410, /web, /analyze, la documentation et le CORS
  hérité répondent comme avant, avec ou sans variables évoquant une
  activation de la politique (sous-processus à environnement contrôlé,
  garde réseau active) ;
- le paquet core/route_policy n'est chargé par api ni référencé par aucun
  module applicatif, script, migration, configuration CI ou frontend (seuls
  tests et docs) ;
- matrice FERMÉE des imports du paquet : aucune dépendance framework, base,
  fournisseur, configuration ou F2 ;
- aucune migration, aucune dépendance de production nouvelle.

Les garanties historiques de dormance (tests/test_f2_dormancy.py,
tests/test_f2_http_dormancy.py, tests/test_authz_f2_dormancy.py) restent
inchangées et continuent de s'appliquer.
"""
import ast
import json
import subprocess
import sys

import pytest

from tests.test_f2_dormancy import MAIN_REQUIREMENTS, MAIN_ROUTES, production_requirements
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

PACKAGE = REPO_ROOT / "core" / "route_policy"
IMPORT_MATRIX = {
    "core/route_policy/__init__.py": set(),
    "core/route_policy/registry.py": {"dataclasses"},
    "core/route_policy/canonical.py": set(),
    "core/route_policy/gate.py": {"dataclasses", "core.route_policy.registry", "core.route_policy.canonical"},
}
MARKERS = ("route_policy", "RoutePolicyGate", "legacy_personal_api_retired", "F-02A", "RouteGateConfigError")

PROBE = r'''
import asyncio, json, socket, sys
attempts = []
def guard(*a, **k):
    attempts.append(repr(a[1:2]))
    raise OSError("réseau interdit pendant le test de dormance")
socket.socket.connect = guard
socket.create_connection = guard

import api
import httpx

CASES = [
    ("GET", "/health", {}, None),
    ("HEAD", "/health", {}, None),
    ("GET", "/web", {}, None),
    ("GET", "/web-v2", {}, None),
    ("GET", "/docs", {}, None),
    ("GET", "/openapi.json", {}, None),
    ("POST", "/analyze", {"content-type": "application/json"}, b"["),
    ("POST", "/pedagogie/lookup", {"content-type": "application/json"}, b"["),
    ("POST", "/education", {"content-type": "application/json"}, b"["),
    ("POST", "/coach", {"content-type": "application/json"}, b"["),
    ("POST", "/checklist", {"content-type": "application/json"}, b"["),
    ("POST", "/web-chat", {"content-type": "application/json"}, b"["),
    ("POST", "/web-v2/portfolio-analyze", {"content-type": "application/json"}, b"["),
    ("GET", "/api/user/dormancy-probe", {}, None),
    ("OPTIONS", "/api/user/dormancy-probe", {"origin": "https://evil.test", "access-control-request-method": "GET"},
     None),
    ("DELETE", "/decryptage", {}, None),
    ("GET", "/api/user/1/", {}, None),
]

async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1), raise_app_exceptions=False)
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path, headers, body in CASES:
            r = await client.request(method, path, headers=headers, content=body)
            out.append([method, path, r.status_code, r.headers.get("access-control-allow-origin"),
                        r.headers.get("location"), r.text[:300]])
    return out

responses = asyncio.run(probe())
pairs = sorted({(m, r.path) for r in api.app.routes for m in r.methods})
loaded = sorted(m for m in sys.modules if m == "core.route_policy" or m.startswith("core.route_policy."))
print(json.dumps({"responses": responses, "pairs": pairs, "loaded": loaded, "attempts": attempts,
                  "middleware": [m.cls.__name__ for m in api.app.user_middleware]}))
'''

ENVIRONMENTS = {
    "none": {},
    "f02a_flags": {"ORYX_ROUTE_POLICY": "1", "ORYX_F02A_ENABLED": "true", "ORYX_LEGACY_RETIRED": "1",
                   "ORYX_RETIRE_LEGACY_API": "on", "ORYX_F04_ENABLED": "1", "ORYX_ROUTE_GATE": "enabled",
                   "ORYX_F2_ENABLED": "1", "ORYX_AUTHZ_F2": "1"},
    "invalid_flags": {"ORYX_ROUTE_POLICY": "maybe", "ORYX_F02A_ENABLED": "{", "ORYX_ROUTE_GATE": ""},
}

# Comportement historique attendu (inchangé par F-02A).
EXPECTED = {
    ("GET", "/health"): 200, ("HEAD", "/health"): 405, ("GET", "/web"): 200, ("GET", "/web-v2"): 200,
    ("GET", "/docs"): 200, ("GET", "/openapi.json"): 200, ("POST", "/analyze"): 422,
    ("POST", "/pedagogie/lookup"): 422, ("POST", "/education"): 422, ("POST", "/coach"): 422,
    ("POST", "/checklist"): 422, ("POST", "/web-chat"): 422, ("POST", "/web-v2/portfolio-analyze"): 422,
    ("GET", "/api/user/dormancy-probe"): 500,  # handler legacy atteint : base non configurée (aucune connexion)
    ("OPTIONS", "/api/user/dormancy-probe"): 200,  # pré-vol CORS hérité, toujours actif sur api:app
    ("DELETE", "/decryptage"): 405, ("GET", "/api/user/1/"): 307,  # redirect_slashes historique
}


@pytest.mark.parametrize("case", sorted(ENVIRONMENTS))
def test_the_real_entrypoint_is_unchanged_and_never_applies_the_policy(case):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(ENVIRONMENTS[case]), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["loaded"] == []
    assert result["attempts"] == []
    assert result["middleware"] == ["CORSMiddleware"]
    declared = sorted({(m.lower(), p) for m, p in result["pairs"] if not p.startswith(("/docs", "/openapi",
                                                                                         "/redoc"))})
    assert declared == sorted(set(MAIN_ROUTES))
    assert len(result["pairs"]) == 31
    statuses = {(method, path): status for method, path, status, *_ in result["responses"]}
    assert statuses == EXPECTED
    for method, path, status, cors, location, text in result["responses"]:
        assert status != 410 and "legacy_personal_api_retired" not in text, (method, path)
    preflight = next(r for r in result["responses"] if r[0] == "OPTIONS")
    assert preflight[3] == "https://evil.test"  # CORS historique intact : la politique n'est pas branchée
    assert {tuple(r[:2]) for r in result["responses"] if r[4]} == {("GET", "/api/user/1/")}


def test_api_source_never_mentions_the_route_policy():
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for marker in MARKERS + ("core.route_policy", "410"):
        assert marker not in source, marker


def test_only_tests_and_docs_reference_the_route_policy():
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if (not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or "node_modules" in path.parts
                or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh", ".yml", ".yaml", ".toml",
                                       ".ini", ".cfg", ".txt", ".md", ".css")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs") or PACKAGE in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in MARKERS):
            offenders.append(str(relative))
    assert offenders == []


def _imports(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, path
            if node.module == "core.route_policy":
                found |= {f"core.route_policy.{a.name}" for a in node.names}
            else:
                found.add(node.module)
    return found


def test_the_package_import_matrix_is_closed():
    files = {str(p.relative_to(REPO_ROOT)) for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert files == set(IMPORT_MATRIX)
    for relative, expected in IMPORT_MATRIX.items():
        assert _imports(REPO_ROOT / relative) == expected, relative


def test_the_package_reads_no_configuration_and_starts_nothing():
    for relative in IMPORT_MATRIX:
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"environ", "getenv", "open", "exec", "eval", "__import__", "import_module",
                            "SessionLocal", "get_db", "set_cookie", "on_event", "lifespan_context",
                            "add_middleware", "include_router", "mount", "RedirectResponse", "logging", "print"}, relative
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for forbidden in ("api.app", "import api", "auth_f2", "authz_f2", "core.db", "core.models", "CORSMiddleware(",
                          "set-cookie", "location"):
            assert forbidden not in source.replace("Set-Cookie", ""), (relative, forbidden)


def test_no_migration_and_no_production_dependency():
    assert production_requirements() == MAIN_REQUIREMENTS
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 21
    assert versions[-1] == "0021_decryptage_turn_reservations.py"
