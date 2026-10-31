"""Dormance de production de R1 PR 5 (C.5.2 §3, C.5.2-B) et de R1 PR 6.

Le véritable point d'entrée api:app n'importe ni ne monte la frontière
d'autorisation (core/authz_f2) ni la route personnelle (core/authz_f2_http) :

- GET /api/v2/me et PUT /api/v2/me/level (R1 PR 6) répondent 404 sur le
  vrai api:app, sans cookie, sans module F2 / PR 5 / PR 6 chargé, sans
  accès réseau, avec ou sans variables évoquant une activation
  (sous-processus à environnement contrôlé) ; la route historique
  PUT /api/user/{user_id}/level garde son comportement ;
- aucun module applicatif ne référence ces paquets (seuls tests et docs) ;
- ils ne lisent aucune configuration, n'ouvrent aucune connexion et ne
  chargent aucun client à l'import ;
- matrice FERMÉE de leurs imports internes (aucune dépendance nouvelle,
  ni core.db, ni core.models, ni fournisseur) ;
- R1 PR 7 : les cinq routes /api/v2/me/portfolio répondent 404 sur le
  vrai api:app ; une seule migration nouvelle, additive
  (0020_position_revision, tête), aucune dépendance de production nouvelle ;
- R1 PR 8 : GET /api/v2/me/analysis-overview, GET /api/v2/me/theses et
  GET /api/v2/me/theses/{thesis_id} répondent 404 sur le vrai api:app (avec
  ou sans query, cookie, sélecteur ni variables d'activation) ; aucune
  migration (tête inchangée 0020_position_revision), aucune dépendance ;
- R1 PR 9-A : le pont ORM privé (core/authz_f2/_decryptage_orm_bridge.py)
  entre dans la matrice fermée avec ses imports exacts (services Décrypter
  historiques et core.models, donc core.db indirectement) ; aucun module de
  production ne l'importe, le vrai api:app ne le charge pas, aucune
  migration (tête 0020_position_revision) ; ses imports transitifs et leur
  absence de réseau sont vérifiés par tests/test_authz_f2_pr9a_boundary.py ;
- R1 PR 9-B1 : deux modules PURS (decryptage_fingerprint,
  decryptage_reservation : aucune base, aucun réseau, aucun core.db) entrent
  dans la matrice ; seize commandes, aucune route ; une seule migration
  nouvelle, additive (0021_decryptage_reservations, fichier
  0021_decryptage_turn_reservations.py, tête) ; aucune variable n'active
  la réservation Décrypter ; api.py ne la mentionne pas.

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
HEAD = "0021_decryptage_reservations"
HEAD_FILE = "0021_decryptage_turn_reservations.py"

# Matrice fermée : module -> imports core.* exacts.
IMPORT_MATRIX = {
    "core/authz_f2/__init__.py": set(),
    "core/authz_f2/principal.py": set(),
    "core/authz_f2/commands.py": {"core.authz_f2.portfolio_values", "core.authz_f2.decryptage_fingerprint",
                                  "core.authz_f2.decryptage_reservation"},
    "core/authz_f2/_adapters.py": {"core.authz_f2.commands", "core.authz_f2.portfolio_fingerprint",
                                   "core.authz_f2.portfolio_results", "core.authz_f2.portfolio_values",
                                   "core.authz_f2.decryptage_fingerprint", "core.authz_f2.decryptage_reservation"},
    "core/authz_f2/runner.py": {"core.auth_f2.sessions", "core.auth_f2.keys", "core.auth_f2.txn",
                                "core.authz_f2._adapters", "core.authz_f2.commands", "core.authz_f2.principal",
                                "core.authz_f2.portfolio_fingerprint", "core.authz_f2.decryptage_fingerprint"},
    # R1 PR 9-B1 : modules purs de la réservation Décrypter.
    "core/authz_f2/decryptage_fingerprint.py": set(),
    "core/authz_f2/decryptage_reservation.py": set(),
    # R1 PR 7 : modules purs du portefeuille (aucune base, aucun réseau).
    "core/authz_f2/portfolio_values.py": set(),
    "core/authz_f2/portfolio_fingerprint.py": set(),
    "core/authz_f2/portfolio_results.py": {"core.authz_f2.commands", "core.authz_f2.portfolio_fingerprint",
                                           "core.authz_f2.portfolio_values"},
    "core/authz_f2_http/__init__.py": set(),
    "core/authz_f2_http/provenance.py": {"core.auth_f2_http.provenance"},
    "core/authz_f2_http/routes.py": {"core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies",
                                     "core.auth_f2_http.csrf", "core.auth_f2_http.provenance", "core.auth_f2_http.routes",
                                     "core.authz_f2.commands", "core.authz_f2.principal", "core.authz_f2.runner",
                                     "core.authz_f2_http.provenance"},
    "core/authz_f2_http/isolated.py": {"core.auth_f2.keys", "core.auth_f2_http.csrf", "core.auth_f2_http.isolated",
                                       "core.auth_f2_http.provenance", "core.authz_f2.runner",
                                       "core.authz_f2_http.routes", "core.authz_f2.portfolio_fingerprint",
                                       "core.authz_f2_http.portfolio", "core.authz_f2_http.consultation"},
    "core/authz_f2_http/portfolio.py": {"core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies",
                                        "core.auth_f2_http.provenance", "core.auth_f2_http.routes",
                                        "core.authz_f2.portfolio_values", "core.authz_f2.commands",
                                        "core.authz_f2.portfolio_fingerprint", "core.authz_f2.portfolio_results",
                                        "core.authz_f2.principal", "core.authz_f2.runner",
                                        "core.authz_f2_http.provenance", "core.authz_f2_http.routes"},
    # R1 PR 9-A : pont ORM privé, seul module de la frontière qui importe
    # des services métier et core.models (exception nominative, V2.2-03).
    "core/authz_f2/_decryptage_orm_bridge.py": {"core.assistant_delivery", "core.decryptage_cognitive_runtime",
                                                "core.decryptage_progress", "core.authz_f2._adapters",
                                                "core.authz_f2.commands", "core.authz_f2.principal",
                                                "core.models"},
    # R1 PR 8 : codec pur (commandes et vues seulement) et routeur de lecture.
    "core/authz_f2_http/consultation_codec.py": {"core.authz_f2.commands"},
    "core/authz_f2_http/consultation.py": {"core.auth_f2.crypto", "core.auth_f2.service",
                                           "core.auth_f2_http.cookies", "core.auth_f2_http.provenance",
                                           "core.auth_f2_http.routes", "core.authz_f2.commands",
                                           "core.authz_f2.principal", "core.authz_f2.runner",
                                           "core.authz_f2_http.consultation_codec", "core.authz_f2_http.portfolio",
                                           "core.authz_f2_http.provenance", "core.authz_f2_http.routes"},
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
OP = "22222222-2222-4222-8222-222222222222"
CASES = [
    ("GET", "/api/v2/me", {}),
    ("GET", "/api/v2/me", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me?user_id=user-a", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN}),
    ("POST", "/api/v2/me", {"origin": "https://oryx.test", "cookie": COOKIE}),
    ("GET", "/api/v2", {}),
    ("PUT", "/api/v2/me/level", {}),
    ("PUT", "/api/v2/me/level", {"origin": "https://oryx.test", "sec-fetch-site": "same-origin",
                                 "x-oryx-generation-id": GEN, "x-oryx-csrf": "c1.1.AAAAAAAAAAAAAAAAAAAAAA.x",
                                 "content-type": "application/json",
                                 "cookie": COOKIE + "; __Host-oryx-csrf-binding=" + "B" * 43}),
    ("GET", "/api/v2/me/level", {}),
    # R1 PR 7 : cinq routes du portefeuille personnel, avec en-têtes plausibles.
    ("GET", "/api/v2/me/portfolio", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me/portfolio/1", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN}),
    ("POST", "/api/v2/me/portfolio", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN,
                                      "x-oryx-operation-id": OP, "content-type": "application/json",
                                      "cookie": COOKIE}),
    ("PUT", "/api/v2/me/portfolio/1", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN,
                                       "x-oryx-operation-id": OP, "x-oryx-expected-revision": "0",
                                       "content-type": "application/json", "cookie": COOKIE}),
    ("DELETE", "/api/v2/me/portfolio/1", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN,
                                          "x-oryx-operation-id": OP, "x-oryx-expected-revision": "0",
                                          "cookie": COOKIE}),
    # R1 PR 8 : trois lectures des analyses et des thèses, en-têtes plausibles.
    ("GET", "/api/v2/me/analysis-overview", {}),
    ("GET", "/api/v2/me/analysis-overview", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN,
                                             "cookie": COOKIE}),
    ("GET", "/api/v2/me/analysis-overview?limit=1", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN,
                                                     "cookie": COOKIE}),
    ("GET", "/api/v2/me/theses", {}),
    ("GET", "/api/v2/me/theses", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me/theses?ticker=AIR.PA&limit=50", {"sec-fetch-site": "same-origin",
                                                        "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me/theses/1", {}),
    ("GET", "/api/v2/me/theses/1", {"sec-fetch-site": "same-origin", "x-oryx-generation-id": GEN, "cookie": COOKIE}),
    ("GET", "/api/v2/me/theses/2147483647", {"origin": "https://oryx.test", "x-oryx-generation-id": GEN}),
]
BODY = b'{"level":"avance","expected_revision":"0"}'
PORTFOLIO_BODY = (b'{"ticker":"AIR.PA","quantity":"4","purchase_price":null,"target_percent":null,'
                  b'"envelope":"PEA"}')

async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1))
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path, headers in CASES:
            body = (PORTFOLIO_BODY if "portfolio" in path else BODY) if method in ("PUT", "POST") else None
            r = await client.request(method, path, headers=headers, content=body)
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
    # R1 PR 7 : aucune variable n'active le portefeuille F2.
    "pr7_flags": {"ORYX_PORTFOLIO_F2": "1", "ORYX_V2_PORTFOLIO": "true", "ORYX_F2_ENABLED": "1",
                  "ORYX_PORTFOLIO_KEYS": json.dumps({"k1": "A" * 43}), "ORYX_PORTFOLIO_KEY_VERSION": "k1"},
    # R1 PR 8 : aucune variable n'active les lectures des analyses et thèses.
    # R1 PR 9-B1 : aucune variable n'active la réservation Décrypter.
    "pr9b1_flags": {"ORYX_DECRYPTAGE_F2": "1", "ORYX_DECRYPTAGE_RESERVATION": "true", "ORYX_PR9B1_ENABLED": "1",
                    "ORYX_DECRYPTAGE_KEYS": json.dumps({"d1": "A" * 43}), "ORYX_DECRYPTAGE_KEY_VERSION": "d1",
                    "ORYX_F2_ENABLED": "1", "ORYX_AUTHZ_F2": "1"},
    "pr8_flags": {"ORYX_THESES_F2": "1", "ORYX_V2_THESES": "true", "ORYX_ANALYSIS_OVERVIEW_F2": "on",
                  "ORYX_PR8_ENABLED": "1", "ORYX_F2_ENABLED": "1", "ORYX_AUTHZ_F2": "1"},
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
                found.add(f"{node.module}.{alias.name}" if node.module in PACKAGES + ("core",) else node.module)
    return found


@pytest.mark.parametrize("case", sorted(ENVIRONMENTS))
def test_real_entrypoint_answers_404_on_api_v2_me(case):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(ENVIRONMENTS[case]), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(result["responses"]) == 23
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
                      "x-oryx-generation-id", "X-Oryx-Generation-Id", "include_router", "mount(", "lifespan",
                      "level_revision", "UpdateProfileLevel", "x-oryx-csrf", "X-Oryx-CSRF",
                      # R1 PR 7
                      "/api/v2/me/portfolio", "position_revision", "PortfolioKeyring", "CreatePortfolioPosition",
                      "ReplacePortfolioPosition", "DeletePortfolioPosition", "x-oryx-operation-id",
                      "X-Oryx-Operation-Id", "X-Oryx-Expected-Revision", "portfolio_write_idempotency",
                      # R1 PR 8
                      "/api/v2/me/theses", "/api/v2/me/analysis-overview", "analysis-overview",
                      "ListAnalysisOverviewPage", "ListThesesPage", "ReadThesis", "consultation",
                      "thesis_not_found", "next_cursor",
                      # R1 PR 9-B1
                      "ReserveDecryptageTurn", "RecordDecryptageAttemptFailure", "DecryptageKeyring",
                      "decryptage_turn_reservations", "LeaseGranted", "LeaseToken", "decryptage_reservation",
                      "decryptage_fingerprint", "lease_epoch"):
        assert forbidden not in source, forbidden


def test_only_tests_and_docs_reference_the_boundary():
    offenders = []
    markers = ("authz_f2", "AuthorizedTransactionRunner", "build_isolated_personal_app", "/api/v2/me",
               "PortfolioKeyring", "oryx-portfolio-request-v1", "portfolio-write-result-v1",
               # R1 PR 8
               "ListAnalysisOverviewPage", "ListThesesPage", "build_consultation_router")
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
            "          'core.authz_f2.portfolio_values', 'core.authz_f2.portfolio_fingerprint',\n"
            "          'core.authz_f2.portfolio_results', 'core.authz_f2.decryptage_fingerprint',\n"
            "          'core.authz_f2.decryptage_reservation', 'core.authz_f2_http', 'core.authz_f2_http.routes',\n"
            "          'core.authz_f2_http.portfolio', 'core.authz_f2_http.consultation_codec',\n"
            "          'core.authz_f2_http.consultation', 'core.authz_f2_http.isolated'):\n"
            "    importlib.import_module(m)\n"
            "print(' '.join(str(x in sys.modules) for x in ('api', 'httpx', 'core.db', 'core.models')), attempts)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), check=True)
    assert out.stdout.strip() == "False False False False []"


def test_only_the_additive_0021_migration_and_no_new_production_dependency():
    """R1 PR 7 a ajouté 0020_position_revision ; R1 PR 9-B1 n'ajoute que
    0021_decryptage_reservations (tête) ; aucune dépendance de production."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_revision("0020_position_revision").down_revision == "0019_f2_http_admission"
    assert script.get_revision(HEAD).down_revision == "0020_position_revision"
    assert script.get_heads() == [HEAD]
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 21 and versions[-1] == HEAD_FILE
    lines = [line.strip() for line in (REPO_ROOT / "requirements.txt").read_text().splitlines() if line.strip()]
    assert lines == MAIN_REQUIREMENTS
