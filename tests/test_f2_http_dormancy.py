"""Dormance de production de R1 PR 3 (exigence prioritaire).

Le véritable point d'entrée api.py reste sans route /api/auth/f2, sans
cookie F2, sans appel fournisseur, sans initialisation anti-abus et sans
création de session au démarrage, même lorsque :

- des variables F2 sont présentes (valides ou invalides) ;
- une variable d'activation est définie ;
- le module HTTP F2 (core/auth_f2_http) existe dans le dépôt.

Les vérifications s'exécutent dans un sous-processus à environnement
contrôlé (aucune variable DATABASE_* / PG* / RAILWAY_*), qui importe le
vrai api.py et lui envoie des requêtes ASGI, avec une garde réseau qui
échoue à toute tentative de connexion sortante.

R1 PR 5 (C.5.2-B) : la sonde surveille aussi les modules chargés
core.authz_f2 et core.authz_f2_http, et interroge GET /api/v2/me (avec un
sélecteur et un cookie actif bien formés) : 404 exact, aucun cookie, aucun
module F2 / PR 5 chargé, aucun accès réseau. Les modules de
core/authz_f2_http qui importent core.auth_f2_http sont acceptés par la
matrice fermée de tests/test_f2_dormancy.py (F2_REFERENCE_MATRIX).
"""
import ast
import json
import os
import subprocess
import sys

import pytest

from tests.test_f2_dormancy import F2_REFERENCE_MATRIX, MAIN_ROUTES
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

HTTP_PACKAGE = REPO_ROOT / "core" / "auth_f2_http"

F2_LIKE_ENVIRONMENTS = {
    "none": {},
    "valid_f2_variables": {
        "ORYX_F2_ENABLED": "1", "ORYX_AUTH_F2": "true", "F2_ENABLED": "1", "ORYX_F2_HTTP": "on",
        "SUPABASE_URL": "https://oryx-auth-fake.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "fake-service",
        "SUPABASE_ANON_KEY": "fake-anon", "RESEND_API_KEY": "re_fake", "ORYX_F2_CSRF_KEY": "A" * 43,
        "ORYX_F2_KEYS": json.dumps({"candidate": {"t1": "A" * 43}}), "ORYX_F2_ALLOWED_ORIGINS": "https://oryx.test",
        "ORYX_F2_TRUSTED_PROXIES": "10.0.0.0/8",
    },
    # R1 PR 5 : drapeaux plausibles d'activation de la frontière personnelle.
    "pr5_like_variables": {
        "ORYX_AUTHZ_F2": "1", "ORYX_PR5_ENABLED": "true", "ORYX_V2_ME": "on", "ORYX_F2_PERSONAL_ROUTES": "1",
        "ORYX_API_V2": "enabled", "ORYX_F2_ENABLED": "1", "ORYX_F2_KEYS": json.dumps({"active": {"t1": "A" * 43}}),
        "ORYX_F2_ALLOWED_ORIGINS": "https://oryx.test",
    },
    "invalid_f2_variables": {
        "ORYX_F2_ENABLED": "yes-please", "ORYX_F2_KEYS": "{not json", "ORYX_F2_CSRF_KEY": "short",
        "SUPABASE_URL": "http://insecure", "RESEND_API_KEY": "", "ORYX_F2_ALLOWED_ORIGINS": "*",
    },
}

PROBE = r'''
import asyncio, json, socket, sys
attempts = []
_connect = socket.socket.connect
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit pendant le test de dormance")
socket.socket.connect = guard
socket.create_connection = lambda *a, **k: guard(None, a[0] if a else None)

import api
import httpx

routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
middleware = [m.cls.__name__ for m in api.app.user_middleware]

async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1))
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path in (("GET", "/api/auth/f2/csrf/bootstrap"), ("POST", "/api/auth/f2/sign-in/start"),
                             ("POST", "/api/auth/f2/sign-in/verify"), ("POST", "/api/auth/f2/session/resolve"),
                             ("POST", "/api/auth/f2/logout"), ("POST", "/api/auth/f2/invitation/begin"),
                             ("POST", "/api/auth/f2/invitation/complete"), ("POST", "/api/auth/f2/invitation/resume"),
                             ("POST", "/api/auth/f2/email-link/start"), ("POST", "/api/auth/f2/email-link/resend"),
                             ("POST", "/api/auth/f2/email-link/verify"), ("POST", "/api/auth/f2/email-link/result"),
                             ("GET", "/api/v2/me"), ("GET", "/health")):
            headers = {"origin": "https://oryx.test", "content-type": "application/json"}
            if path == "/api/v2/me":
                generation = "11111111-2222-4333-8444-555555555555"
                headers = {"sec-fetch-site": "same-origin", "x-oryx-generation-id": generation,
                           "cookie": "__Host-oryx-active-" + generation + "=" + "A" * 43}
            r = await client.request(method, path, headers=headers,
                                     content=b'{"email":"alice@example.com"}' if method == "POST" else None)
            out.append([method, path, r.status_code, r.headers.get_list("set-cookie"), r.text[:200]])
    return out

responses = asyncio.run(probe())
MONITORED = ("core.auth_f2", "core.auth_f2_http", "core.authz_f2", "core.authz_f2_http")
f2_modules = sorted(m for m in sys.modules if any(m == p or m.startswith(p + ".") for p in MONITORED))
print(json.dumps({"routes": routes, "middleware": middleware, "responses": responses, "f2_modules": f2_modules,
                  "attempts": attempts}))
'''


def _clean_env(extra):
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH")}
    return {**keep, **extra}


@pytest.mark.parametrize("case", sorted(F2_LIKE_ENVIRONMENTS))
def test_production_entrypoint_exposes_no_f2_route_whatever_the_environment(case):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(F2_LIKE_ENVIRONMENTS[case]), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
        set(MAIN_ROUTES))
    assert not any("/api/auth" in path for _, path in result["routes"])
    assert result["middleware"] == ["CORSMiddleware"]
    assert result["f2_modules"] == []  # ni le moteur, ni la couche HTTP, ni la frontière PR 5 ne sont chargés
    assert result["attempts"] == []  # aucun appel fournisseur au démarrage ni aux requêtes
    # R1 PR 4 : les sept routes d'invitation et de liaison e-mail sont absentes.
    assert sum(1 for _, path, _, _, _ in result["responses"] if "/invitation/" in path or "/email-link/" in path) == 7
    for method, path, status, cookies, _ in result["responses"]:
        if path.startswith("/api/auth/f2"):
            assert status in (404, 405), (method, path, status)
        assert cookies == [], (method, path)
    # R1 PR 5 : GET /api/v2/me est absent de l'application publique.
    assert [status for _, path, status, _, _ in result["responses"] if path == "/api/v2/me"] == [404]
    assert not any(path.startswith("/api/v2") for _, path in result["routes"])


def test_api_source_never_mentions_f2_configuration():
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for forbidden in ("auth_f2", "/api/auth", "build_isolated_f2_app", "SUPABASE", "RESEND", "ORYX_F2", "csrf",
                      "include_router", "mount("):
        assert forbidden not in source, forbidden


def test_only_tests_compose_the_isolated_application():
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if (not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or "node_modules" in path.parts
                or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh", ".yml", ".yaml", ".toml",
                                       ".ini", ".cfg", ".txt")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs") or HTTP_PACKAGE in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "build_isolated_f2_app" in text or ("auth_f2_http" in text and str(relative) not in F2_REFERENCE_MATRIX):
            offenders.append(str(relative))
    assert offenders == []


def test_http_package_reads_no_configuration_and_loads_no_client():
    for path in sorted(HTTP_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        assert not roots & {"os", "dotenv", "api", "requests", "httpx", "uvicorn"}, path.name
        assert "core.db" not in imported and "core.models" not in imported, path.name
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"environ", "getenv", "SessionLocal", "get_db", "initialize_abuse_key_state",
                            "rotate_abuse_key", "on_event", "lifespan"}, path.name


def test_importing_the_http_package_starts_nothing():
    code = ("import sys, importlib\n"
            "for m in ('core.auth_f2_http', 'core.auth_f2_http.routes', 'core.auth_f2_http.isolated'):\n"
            "    importlib.import_module(m)\n"
            "print(' '.join(str(x in sys.modules) for x in ('api', 'httpx', 'core.db')))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), check=True)
    assert out.stdout.strip() == "False False False"


def test_production_requirements_gain_no_dependency():
    from tests.test_f2_dormancy import MAIN_REQUIREMENTS
    lines = [l.strip() for l in (REPO_ROOT / "requirements.txt").read_text().splitlines() if l.strip()]
    assert lines == MAIN_REQUIREMENTS
    ci_inputs = (REPO_ROOT / "ci" / "requirements-ci.in").read_text()
    assert "\nhttpx\n" in ci_inputs  # httpx : dépendance de test explicite, verrouillée par empreinte
