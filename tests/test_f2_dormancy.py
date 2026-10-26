"""Dormance de R1 PR 2 : les services F2 existent mais ne sont reliés à
rien.

- Aucun module applicatif (api.py, core hors core/auth_f2 et
  core/auth_f2_http, scripts, workers, alembic/env.py, frontends) ne
  référence core.auth_f2. La couche HTTP de R1 PR 3 (core/auth_f2_http,
  composition isolée de test) est le seul autre paquet qui l'importe, et
  rien ne l'importe à son tour (tests/test_f2_http_dormancy.py).
- Les routes, middlewares et points de démarrage d'api.py sont exactement
  ceux de main (a8d5b60) : aucune route publique ajoutée ou modifiée.
- core/auth_f2 ne lit aucune variable d'environnement, ne pose aucun
  cookie, n'importe ni FastAPI ni api.py, et n'importe httpx qu'à la
  construction d'un adaptateur HTTP (jamais au chargement).
- requirements.txt (production) ne gagne aucune dépendance.

R1 PR 5 (C.5.2-B) : la frontière d'autorisation (core/authz_f2) et la
route personnelle isolée (core/authz_f2_http) importent des modules F2.
Ces dépendances sont acceptées par une matrice FERMÉE fichier -> imports
F2 exacts (F2_REFERENCE_MATRIX) : un fichier hors matrice ne peut toujours
pas mentionner core.auth_f2, un fichier de la matrice ne peut importer que
ses modules F2 listés, jamais F2AuthService ni la composition F2. Aucun
paquet n'est exempté en bloc.
"""
import ast
import importlib
import subprocess
import sys

from tests.test_migration_0002_analysis_sessions import REPO_ROOT

PACKAGE = REPO_ROOT / "core" / "auth_f2"
HTTP_PACKAGE = REPO_ROOT / "core" / "auth_f2_http"
MAIN_ROUTES = [
    ("get", "/health"), ("post", "/analyze"), ("post", "/analyze-etf"), ("post", "/pedagogie/lookup"),
    ("post", "/decryptage"), ("post", "/api/runtime/assistant-deliveries/{assistant_turn_id}/ack"),
    ("post", "/education"), ("post", "/coach"), ("post", "/checklist"), ("post", "/web-chat"), ("get", "/web"),
    ("get", "/web-v2"), ("get", "/web-v2/search"), ("get", "/web-v2/fx-rate"), ("get", "/api/user/{user_id}"),
    ("put", "/api/user/{user_id}/level"), ("get", "/api/user/{user_id}/portfolio"),
    ("post", "/api/user/{user_id}/portfolio"), ("delete", "/api/user/{user_id}/portfolio/{position_id}"),
    ("put", "/api/user/{user_id}/portfolio/{position_id}"), ("get", "/api/user/{user_id}/theses"),
    ("delete", "/api/user/{user_id}/theses/{ticker}"), ("post", "/web-v2/portfolio-analyze"),
]
# R1 PR 5 : fichiers hors core/auth_f2 et core/auth_f2_http autorisés à
# mentionner F2, avec l'ensemble EXACT des modules F2 qu'ils importent.
F2_REFERENCE_MATRIX = {
    "core/authz_f2/__init__.py": frozenset(),  # docstring seulement
    "core/authz_f2/runner.py": frozenset({"core.auth_f2.keys", "core.auth_f2.sessions", "core.auth_f2.txn"}),
    "core/authz_f2_http/provenance.py": frozenset({"core.auth_f2_http.provenance"}),
    "core/authz_f2_http/routes.py": frozenset({
        "core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies", "core.auth_f2_http.csrf",
        "core.auth_f2_http.provenance", "core.auth_f2_http.routes"}),
    "core/authz_f2_http/isolated.py": frozenset({"core.auth_f2.keys", "core.auth_f2_http.csrf",
                                                 "core.auth_f2_http.isolated", "core.auth_f2_http.provenance"}),
}
# Noms importables depuis F2 par la matrice (aucun F2AuthService, aucune
# composition F2, aucun fournisseur, aucune initialisation anti-abus).
F2_REFERENCE_NAMES = {
    "core.auth_f2.keys": {"Keyring", "PURPOSES"},
    "core.auth_f2.sessions": {"sessions"},
    "core.auth_f2.txn": {"pg_clock", "transaction"},
    "core.auth_f2_http.provenance": {"ProvenanceRejected", "validate_allowed_origins"},
    "core.auth_f2.crypto": {"Redacted"},
    "core.auth_f2.service": {"SessionCredential"},
    "core.auth_f2_http.cookies": {"CookieHeaderRejected", "canonical_uuid", "parse_cookie_headers"},
    "core.auth_f2_http.routes": {"error_response", "json_response"},
    "core.auth_f2_http.isolated": {"_NoStore"},
    # R1 PR 6 : protocole CSRF de PR 3 réutilisé (vérification seulement ; le
    # bootstrap reste dans l'application F2 isolée).
    "core.auth_f2_http.csrf": {"csrf", "CsrfKeyring"},
}
F2_PACKAGES = ("core.auth_f2", "core.auth_f2_http")


def f2_imports(path) -> dict:
    """{module F2 importé : noms importés} d'un fichier (from core.auth_f2
    import sessions -> core.auth_f2.sessions)."""
    found = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[:2] in (["core", "auth_f2"], ["core", "auth_f2_http"]):
                    found.setdefault(alias.name, set()).add("*module*")
        elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module in F2_PACKAGES or node.module.startswith(tuple(p + "." for p in F2_PACKAGES))):
            for alias in node.names:
                module = f"{node.module}.{alias.name}" if node.module in F2_PACKAGES else node.module
                found.setdefault(module, set()).add(alias.name)
    return found


MAIN_REQUIREMENTS = ["fastapi==0.110.0", "uvicorn==0.27.0", "pydantic==2.10.0", "requests", "python-dotenv",
                     "yfinance", "anthropic>=0.25.0", "sqlalchemy", "psycopg2-binary", "alembic>=1.13"]


def _app_calls(tree):
    routes, others = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "app":
            if node.func.attr in ("get", "post", "put", "delete", "patch", "options", "head", "api_route",
                                  "websocket"):
                routes.append((node.func.attr, node.args[0].value))
            else:
                others.append(node.func.attr)
    return routes, others


def test_api_routes_and_hooks_are_exactly_those_of_main():
    tree = ast.parse((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    routes, others = _app_calls(tree)
    assert routes == MAIN_ROUTES
    assert others == ["add_middleware"]
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for forbidden in ("on_event", "lifespan", "auth_f2", "F2AuthService", "Set-Cookie", "set_cookie", "__Host-"):
        assert forbidden not in source, forbidden


def test_no_application_module_references_the_f2_package():
    offenders = []
    roots = [REPO_ROOT / "api.py", REPO_ROOT / "test.py", REPO_ROOT / "alembic", REPO_ROOT / "scripts",
             REPO_ROOT / "core", REPO_ROOT / "ci", REPO_ROOT / "web", REPO_ROOT / "web-v2"]
    for root in roots:
        paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in paths:
            if PACKAGE in path.parents or HTTP_PACKAGE in path.parents or "__pycache__" in path.parts or path.suffix not in (".py", ".js", ".html",
                                                                                            ".ts", ".json"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if ("auth_f2" in text or "F2AuthService" in text) and str(path.relative_to(REPO_ROOT)) not in F2_REFERENCE_MATRIX:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_f2_reference_matrix_is_closed_and_exact():
    """R1 PR 5 : chaque fichier de la matrice existe, importe EXACTEMENT ses
    modules F2 listés et seulement des noms autorisés ; F2AuthService et la
    composition F2 n'y apparaissent jamais."""
    for relative, expected in F2_REFERENCE_MATRIX.items():
        path = REPO_ROOT / relative
        assert path.is_file(), relative
        imports = f2_imports(path)
        assert set(imports) == expected, relative
        for module, names in imports.items():
            assert names <= F2_REFERENCE_NAMES[module], (relative, module, names)
        text = path.read_text(encoding="utf-8")
        for forbidden in ("F2AuthService", "build_isolated_f2_app", "HttpAdmission", "initialize_abuse_key_state",
                          "http_providers", "set_cookie", "clear_cookie"):
            assert forbidden not in text, (relative, forbidden)


def test_f2_package_reads_no_configuration_and_sets_no_cookie():
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"fastapi", "api", "requests", "os", "dotenv", "starlette", "uvicorn"}, path.name
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"environ", "getenv", "set_cookie", "SessionLocal", "get_db"}, path.name
        for forbidden in ("Set-Cookie", "core.db"):
            assert forbidden not in source, (path.name, forbidden)
        top_level = {n.names[0].name for n in tree.body if isinstance(n, ast.Import)}
        assert "httpx" not in top_level, path.name


def test_importing_the_package_loads_no_network_client():
    code = ("import sys, importlib\n"
            "for m in ('core.auth_f2', 'core.auth_f2.service', 'core.auth_f2.http_providers', 'core.auth_f2.abuse'):\n"
            "    importlib.import_module(m)\n"
            "print('httpx' in sys.modules, 'fastapi' in sys.modules, 'api' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False False False"
    assert importlib.import_module("core.auth_f2").__doc__.strip().startswith("Services internes F2")


def test_production_requirements_are_unchanged():
    lines = [l.strip() for l in (REPO_ROOT / "requirements.txt").read_text().splitlines() if l.strip()]
    assert lines == MAIN_REQUIREMENTS
