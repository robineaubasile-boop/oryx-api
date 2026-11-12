"""F04-B — dormance : F2 reste inactif.

Aucun module applicatif n'importe core.f2_runtime ; le point d'entrée servi
(api:app) répond exactement de la même façon avec ou sans configuration F2
complète dans l'environnement ; api.py, core/db.py, alembic/env.py, la porte
F-02A et runtime.txt sont octet pour octet ceux du freeze (5ccc9e2).
"""
import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from core.f2_runtime.config import MISSING, VARIABLES, F2Config, F2ConfigError, load_f2_config
from tests import f04b_support as s
from tests.test_f2_dormancy import MAIN_ROUTES
from tests.test_f2_http_dormancy import PROBE, _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

FROZEN_SHA256 = {
    "api.py": "31767d6f52f58f216e5aaa033006a9316094fb2ae4a37f7f6311c87399690d04",
    "core/db.py": "bae1a4ccb340335e3eccbf1ecbb4776cc46132b2fad4eb569e5548778815d50f",
    "alembic/env.py": "0f46c48581d9f0ce405e9441627fff5eb930cc4881c8295527eeabbdbc901970",
    "runtime.txt": "771150983efa8bb06d8f1961c8c70b3578bb003e0c28509ca25925a7c6970709",
    "core/route_policy/__init__.py": "c42bcbb2fdc94831398272edf789560a4cffa5dd7f412a1dc2e6005dd160f1b8",
    "core/route_policy/canonical.py": "a86db10993a56988e5f4b75825ff9f73dc212a8b28a5d9c7a923ebb430d3f8b7",
    "core/route_policy/gate.py": "84b1eb7f8990d34f13db2f55802f5890ba910747e27c12037793ff0d5146e9f6",
    "core/route_policy/registry.py": "d8accc92a55170f5fa4092eae18c7e8c29cf921f97b9853402516decefdb4b29",
}
MONITORED_PROBE = PROBE.replace('MONITORED = ("core.auth_f2", "core.auth_f2_http", "core.authz_f2", "core.authz_f2_http")',
                                'MONITORED = ("core.auth_f2", "core.auth_f2_http", "core.authz_f2", "core.authz_f2_http", '
                                '"core.f2_runtime")')


def test_frozen_files_are_unchanged():
    for relative, digest in FROZEN_SHA256.items():
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == digest, relative


def test_no_application_module_references_f04b():
    roots = [REPO_ROOT / "api.py", REPO_ROOT / "test.py", REPO_ROOT / "alembic", REPO_ROOT / "scripts",
             REPO_ROOT / "core", REPO_ROOT / "ci", REPO_ROOT / "web", REPO_ROOT / "web-v2", REPO_ROOT / ".github"]
    package = REPO_ROOT / "core" / "f2_runtime"
    offenders = []
    for root in roots:
        for path in ([root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]):
            if package in path.parents or "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(marker in text for marker in ("f2_runtime", "load_f2_config", "build_f2_resources",
                                                 "run_f2_preflight", "create_f2_engine", "ORYX_F2_")):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def _probe(env):
    out = subprocess.run([sys.executable, "-c", MONITORED_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(env), timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _complete_f2_environment() -> dict:
    """Les QUINZE variables contractuelles, valeurs synthétiques ; DATABASE_URL
    désigne le port 1 de la boucle locale (aucun serveur, aucune production)."""
    full = s.environ()
    contract = {name: full[name] for name in VARIABLES}
    assert len(contract) == 15 and "DATABASE_URL" in contract
    assert contract["DATABASE_URL"] == s.FAKE_DATABASE_URL and ":1/" in contract["DATABASE_URL"]
    return contract


def test_the_served_entrypoint_ignores_a_complete_f2_configuration():
    """ACT-L1 / F04-B, réserve d'audit 1 : avec les QUINZE variables
    contractuelles (DATABASE_URL comprise, synthétique), api:app n'expose
    aucune route F2, ne charge aucun module F2 ni F04-B, ne tente aucune
    connexion et répond exactement comme le legacy avec la seule
    DATABASE_URL, et comme sans aucune variable.

    Historique : la première version de ce test filtrait sur le préfixe
    ORYX_F2_ et omettait donc DATABASE_URL (14 variables) ; ce n'était pas une
    mesure d'isolement (l'URL est synthétique et le sous-processus reçoit un
    environnement nettoyé), mais une erreur de filtre, corrigée ici."""
    contract = _complete_f2_environment()
    without_any = _probe({})
    legacy_only = _probe({"DATABASE_URL": contract["DATABASE_URL"]})
    configured = _probe(contract)
    assert configured == legacy_only == without_any
    assert configured["f2_modules"] == [] and configured["attempts"] == []
    routes = [tuple(r) for r in configured["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))]
    assert routes == sorted(set(MAIN_ROUTES))


ENGINE_PROBE = r'''
import gc, json, socket, sys
attempts = []
def guard(self, address):
    attempts.append("socket")
    raise OSError("réseau interdit")
socket.socket.connect = guard
import psycopg2
psycopg2.connect = lambda *a, **k: attempts.append("psycopg2")
import api
import sqlalchemy as sa
engines = [o for o in gc.get_objects() if isinstance(o, sa.engine.Engine)]
import core.db
print(json.dumps({
    "engines": len(engines),
    "legacy_engine_only": all(e is core.db.engine for e in engines),
    "pool_checked_out": [e.pool.checkedout() for e in engines],
    "f2_runtime": sorted(m for m in sys.modules if m.startswith("core.f2_runtime")),
    "attempts": attempts,
}))
'''


def test_a_complete_f2_configuration_builds_no_f2_engine_or_composition():
    """Le seul moteur présent après import d'api est le moteur legacy de
    core.db (construit par le legacy dès que DATABASE_URL existe, inchangé),
    sans connexion ; aucun moteur F2, aucune composition, aucun module F04-B."""
    contract = _complete_f2_environment()
    for env, engines in (({}, 0), ({"DATABASE_URL": contract["DATABASE_URL"]}, 1), (contract, 1)):
        out = subprocess.run([sys.executable, "-c", ENGINE_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(env), timeout=180)
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result == {"engines": engines, "legacy_engine_only": True, "pool_checked_out": [0] * engines,
                          "f2_runtime": [], "attempts": []}, (sorted(env), result)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_and_query(env) -> dict:
    """Vrai serveur Uvicorn (point d'entrée historique api:app), boucle locale."""
    port = _free_port()
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(port)],
                              cwd=REPO_ROOT, env=_clean_env(env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read()
                break
            except OSError:
                if time.monotonic() > deadline or server.poll() is not None:
                    raise AssertionError("le serveur historique n'a pas démarré")
                time.sleep(0.3)
        answers = {}
        for method, path in (("GET", "/health"), ("GET", "/api/auth/f2/csrf/bootstrap"),
                             ("POST", "/api/auth/f2/sign-in/start"), ("GET", "/api/v2/me"),
                             ("POST", "/api/v2/me/decryptage/turns"), ("GET", "/web-v2"), ("GET", "/openapi.json")):
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                             data=b"{}" if method == "POST" else None,
                                             headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    status, body = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, body = error.code, error.read()
            answers[f"{method} {path}"] = [status, hashlib.sha256(body).hexdigest()]
        return answers
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log = server.stdout.read() if server.stdout else ""
        assert "f2_runtime" not in log and "Traceback" not in log, log[-2000:]


def test_the_real_legacy_server_answers_identically_with_a_complete_f2_configuration():
    contract = _complete_f2_environment()
    legacy_only = _serve_and_query({"DATABASE_URL": contract["DATABASE_URL"]})
    configured = _serve_and_query(contract)
    assert configured == legacy_only
    assert configured["GET /health"][0] == 200
    for key in ("GET /api/auth/f2/csrf/bootstrap", "POST /api/auth/f2/sign-in/start", "GET /api/v2/me",
                "POST /api/v2/me/decryptage/turns"):
        assert configured[key][0] == 404, key  # aucune route F2 sur le serveur historique


def test_the_f2_loader_still_refuses_an_incomplete_configuration_when_called():
    """Ne pas confondre dormance et validation : le serveur historique ignore
    l'environnement F2 (tests ci-dessus), alors que le chargeur F2, appelé
    EXPLICITEMENT, accepte la configuration complète et refuse chacune de ses
    quinze amputations, sans jamais citer une valeur."""
    contract = _complete_f2_environment()
    assert type(load_f2_config(contract)) is F2Config
    for name in VARIABLES:
        partial = {k: v for k, v in contract.items() if k != name}
        try:
            load_f2_config(partial)
        except F2ConfigError as error:
            assert error.problems == ((name, MISSING, None),), name
            assert "synthetic-password" not in str(error)
        else:
            raise AssertionError(f"configuration sans {name} acceptée")
