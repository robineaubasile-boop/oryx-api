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
- requirements.txt (production) ne gagne aucune dépendance. ACT-L1 / F04-B
  V0.3 : les dépendances DIRECTES de production sont celles de
  requirements.in (MAIN_REQUIREMENTS, liste exacte) et requirements.txt en
  est le verrou complet avec empreintes (tests/test_f04b_dependencies.py).

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
    # R1 PR 7 : routes /api/v2/me/portfolio (mêmes primitives que routes.py).
    "core/authz_f2_http/portfolio.py": frozenset({
        "core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies", "core.auth_f2_http.provenance",
        "core.auth_f2_http.routes"}),
    # R1 PR 8 : lectures des analyses et des thèses (mêmes primitives que le
    # portefeuille ; aucun CSRF, aucun canonical_uuid propre).
    "core/authz_f2_http/consultation.py": frozenset({
        "core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies", "core.auth_f2_http.provenance",
        "core.auth_f2_http.routes"}),
    # R1 PR 9-C3 : orchestration HTTP Décrypter isolée (mêmes primitives que
    # le portefeuille ; vérification CSRF par la fonction partagée de routes.py).
    "core/authz_f2_http/decryptage.py": frozenset({
        "core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies", "core.auth_f2_http.provenance",
        "core.auth_f2_http.routes"}),
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
    # R1 PR 7 : NO_STORE_HEADERS (en-têtes des corps canoniques du portefeuille).
    "core.auth_f2_http.routes": {"error_response", "json_response", "NO_STORE_HEADERS"},
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


# ACT-L1 / F04-B V0.3 (freeze fondateur) : dépendances DIRECTES de production,
# exactes et épinglées (requirements.in) ; httpx, httpx2 et starlette y sont
# désormais explicites. Toute autre modification exige un nouveau freeze.
MAIN_REQUIREMENTS = ["fastapi==0.110.0", "starlette==0.36.3", "uvicorn==0.27.0", "pydantic==2.10.0",
                     "requests==2.34.2", "python-dotenv==1.2.4", "yfinance==1.7.0", "anthropic==1.11.0",
                     "httpx==0.28.1", "httpx2==2.13.1", "sqlalchemy==2.1.3", "psycopg2-binary==2.9.13",
                     "alembic==1.20.0"]


def production_requirements() -> list:
    """Dépendances directes déclarées par requirements.in (sans commentaire)."""
    lines = (REPO_ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


# ACT-L1 / F04-B V0.3 : fichiers de core/f2_runtime (ressources d'exécution F2,
# dormantes) autorisés à importer F2, avec l'ensemble EXACT de leurs modules F2
# et les noms importables. Un fichier F04-B hors de cette matrice ne peut
# mentionner core.auth_f2 ; aucun n'importe F2AuthService, une composition,
# HttpAdmission, ni n'initialise, ne fait tourner ou ne purge l'état anti-abus.
F04B_REFERENCE_MATRIX = {
    "core/f2_runtime/config.py": frozenset({
        "core.auth_f2.crypto", "core.auth_f2.keys", "core.auth_f2.providers", "core.auth_f2_http.csrf",
        "core.auth_f2_http.provenance"}),
    "core/f2_runtime/preflight.py": frozenset({"core.auth_f2.abuse", "core.auth_f2.keys", "core.auth_f2.policy"}),
    "core/f2_runtime/resources.py": frozenset({"core.auth_f2.http_providers"}),
}
F04B_REFERENCE_NAMES = {
    "core.auth_f2.crypto": {"CryptoContractError", "Redacted", "b64url_decode_secret"},
    "core.auth_f2.keys": {"PURPOSES", "Keyring", "KeyringError"},
    "core.auth_f2.providers": {"is_valid_issuer"},
    "core.auth_f2_http.csrf": {"CsrfConfigError", "CsrfKeyring"},
    "core.auth_f2_http.provenance": {"validate_allowed_origins"},
    "core.auth_f2.abuse": {"AbuseUnavailable", "load_key_state"},
    "core.auth_f2.policy": {"LOCK_TIMEOUT", "STATEMENT_TIMEOUT"},
    "core.auth_f2.http_providers": {"HttpResendMailer", "HttpSupabaseOtpProvider"},
}
# Marqueurs EXACTS (dans l'ordre des listes de marqueurs de chaque balayage)
# tolérés dans les fichiers F04-B par les autres tests de dormance.
F04B_BOUNDARY_MARKERS = {"core/f2_runtime/config.py": ["authz_f2", "PortfolioKeyring"]}
F04B_B1_MARKERS = {"core/f2_runtime/config.py": ["DecryptageKeyring"],
                   "core/f2_runtime/preflight.py": ["decryptage_turn_reservations"]}
F04B_B2_MARKERS = {"core/f2_runtime/resources.py": ["decryptage_providers"]}

# ACT-L1 / F04-C V0.3 — arbitrage E-1 (fondateur, 13/11/2026 ; voir
# docs/act_l1_f04c_traceability_v03.md § 6) : le point d'entrée DORMANT F04-C est
# le SEUL fichier applicatif autorisé à composer F2 (Auth, Personnel, C3, porte
# F-02A) et à lire os.environ (copie au démarrage). Exceptions NOMINATIVES et
# EXACTES (imports, noms, marqueurs dans l'ordre de chaque balayage), utilisées par
# les autres tests de dormance ; tout autre fichier reste soumis aux règles
# historiques (tests négatifs ci-dessous et dans chaque balayage).
F04C_ENTRYPOINT = "core/f2_runtime/entrypoint.py"
F04C_REFERENCE_MATRIX = {F04C_ENTRYPOINT: frozenset({"core.auth_f2_http.client_ip", "core.auth_f2_http.isolated"})}
F04C_REFERENCE_NAMES = {
    "core.auth_f2_http.client_ip": {"PeerAddressResolver"},
    "core.auth_f2_http.isolated": {"build_isolated_f2_app"},
}
# Imports COMPLETS et exacts du point d'entrée : ni api, ni core.db, ni
# core.models, ni module legacy, ni service / runner / adaptateur F2 directs.
F04C_ENTRYPOINT_IMPORTS = frozenset({
    "asyncio", "os", "threading", "starlette.routing", "core.auth_f2_http.client_ip", "core.auth_f2_http.isolated",
    "core.authz_f2_http.decryptage", "core.authz_f2_http.isolated", "core.f2_runtime.config",
    "core.f2_runtime.preflight", "core.f2_runtime.resources", "core.f2_runtime.work_registry", "core.route_policy",
    "core.route_policy.gate"})
F04C_BOUNDARY_MARKERS = {F04C_ENTRYPOINT: ["authz_f2", "build_isolated_personal_app"]}
F04C_ROUTE_POLICY_MARKERS = {F04C_ENTRYPOINT: ["route_policy", "RoutePolicyGate", "F-02A"]}
F04C_C3_MARKERS = {F04C_ENTRYPOINT: ["DecryptageHttpConfig", "authz_f2_http.decryptage"]}


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


def unauthorized_f2_reference(relative: str, text: str) -> bool:
    """Fichier applicatif (hors paquets F2) qui mentionne F2 sans figurer dans
    une matrice fermée (R1 PR 5, F04-B, F04-C nominatif)."""
    return ("auth_f2" in text or "F2AuthService" in text) and relative not in F2_REFERENCE_MATRIX \
        and relative not in F04B_REFERENCE_MATRIX and relative not in F04C_REFERENCE_MATRIX


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
            relative = str(path.relative_to(REPO_ROOT))
            if unauthorized_f2_reference(relative, text):
                offenders.append(relative)
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


def test_f04b_reference_matrix_is_closed_and_exact():
    """ACT-L1 / F04-B : chaque fichier de core/f2_runtime qui mentionne F2 est
    dans la matrice, importe EXACTEMENT ses modules F2 et seulement des noms
    autorisés ; aucun service, aucune composition, aucune admission HTTP,
    aucun acte sur l'état anti-abus (initialisation, rotation, retrait, purge,
    création de sujets), aucun cookie."""
    mentioning = {str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "core" / "f2_runtime").glob("*.py")
                  if "auth_f2" in p.read_text(encoding="utf-8")}
    # F04-C (arbitrage E-1) : le seul point d'entrée s'ajoute, vérifié exactement
    # par test_f04c_reference_matrix_is_closed_and_exact.
    assert mentioning == set(F04B_REFERENCE_MATRIX) | set(F04C_REFERENCE_MATRIX)
    for relative, expected in F04B_REFERENCE_MATRIX.items():
        path = REPO_ROOT / relative
        imports = f2_imports(path)
        assert set(imports) == expected, relative
        for module, names in imports.items():
            assert names <= F04B_REFERENCE_NAMES[module], (relative, module, names)
        text = path.read_text(encoding="utf-8")
        for forbidden in ("F2AuthService", "build_isolated_f2_app", "HttpAdmission", "set_cookie", "clear_cookie"):
            assert forbidden not in text, (relative, forbidden)
        tree = ast.parse(text)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"initialize_abuse_key_state", "rotate_abuse_key", "retire_previous_abuse_key",
                            "purge_abuse_history", "ensure_subjects", "lock_key_state", "F2AuthService"}, relative


def _all_imports(tree) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_f04c_reference_matrix_is_closed_and_exact():
    """ACT-L1 / F04-C (arbitrage E-1) : UNE seule entrée, imports F2 exacts et
    noms autorisés ; imports complets fermés (ni api, ni core.db, ni
    core.models) ; jamais de service, d'admission HTTP, d'acte anti-abus, de
    cookie, de moteur ou de session legacy."""
    assert set(F04C_REFERENCE_MATRIX) == {F04C_ENTRYPOINT}
    path = REPO_ROOT / F04C_ENTRYPOINT
    imports = f2_imports(path)
    assert set(imports) == F04C_REFERENCE_MATRIX[F04C_ENTRYPOINT]
    for module, names in imports.items():
        assert names <= F04C_REFERENCE_NAMES[module], (module, names)
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    assert _all_imports(tree) == F04C_ENTRYPOINT_IMPORTS
    for forbidden in ("F2AuthService", "HttpAdmission", "initialize_abuse_key_state", "http_providers", "set_cookie",
                      "clear_cookie", "SessionLocal", "get_db"):
        assert forbidden not in text, forbidden
    # api / core.db / core.models : exclus par l'égalité EXACTE des imports ci-dessus
    # (le module ne fait que DOCUMENTER le chargement indirect de core.db, Q2 / O1).
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"initialize_abuse_key_state", "rotate_abuse_key", "retire_previous_abuse_key",
                        "purge_abuse_history", "ensure_subjects", "lock_key_state", "F2AuthService", "SessionLocal",
                        "get_db", "AuthorizedTransactionRunner", "create_engine", "create_f2_engine"}


def test_the_f04c_exception_is_nominative_and_refuses_any_other_module():
    """Négatif : le texte EXACT du point d'entrée, placé dans tout autre fichier
    applicatif (autre module F2, api.py, worker, script, paquet core), reste
    refusé ; seul le point d'entrée nominatif est accepté."""
    text = (REPO_ROOT / F04C_ENTRYPOINT).read_text(encoding="utf-8")
    assert not unauthorized_f2_reference(F04C_ENTRYPOINT, text)
    for other in ("core/f2_runtime/entrypoint_copy.py", "core/f2_runtime/work_registry.py", "core/entrypoint.py",
                  "api.py", "core/evaluation_worker.py", "scripts/serve_f2.py", "alembic/env.py"):
        assert unauthorized_f2_reference(other, text), other


def test_no_application_entrypoint_imports_or_serves_the_f04c_entrypoint():
    """Dormance F04-C : ni api.py, ni le worker, ni un script, la CI, Alembic,
    les frontends ou un fichier de configuration n'importent ou ne servent le
    point d'entrée (aucune cible « entrypoint:app » hors de son propre module)."""
    offenders = []
    roots = [REPO_ROOT / "api.py", REPO_ROOT / "test.py", REPO_ROOT / "alembic", REPO_ROOT / "scripts",
             REPO_ROOT / "core", REPO_ROOT / "ci", REPO_ROOT / "web", REPO_ROOT / "web-v2", REPO_ROOT / ".github"]
    roots += [p for p in REPO_ROOT.iterdir() if p.is_file() and p.suffix in ("", ".txt", ".toml", ".json", ".yml",
                                                                             ".yaml", ".cfg", ".ini", ".in")]
    for root in roots:
        for path in ([root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]):
            relative = str(path.relative_to(REPO_ROOT))
            if "__pycache__" in path.parts or "node_modules" in path.parts or relative == F04C_ENTRYPOINT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "entrypoint:app" in text or "F2Envelope" in text:
                offenders.append(relative)
            if path.suffix == ".py" and any(m == "core.f2_runtime.entrypoint" or m.endswith(".entrypoint")
                                            for m in _all_imports(ast.parse(text))):
                offenders.append(relative)
            if path.suffix == ".py" and any(isinstance(n, ast.ImportFrom) and n.module == "core.f2_runtime"
                                            and any(a.name == "entrypoint" for a in n.names)
                                            for n in ast.walk(ast.parse(text))):
                offenders.append(relative)
    assert offenders == []


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
    assert production_requirements() == MAIN_REQUIREMENTS
