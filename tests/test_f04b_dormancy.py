"""F04-B — dormance : F2 reste inactif.

Aucun module applicatif n'importe core.f2_runtime ; le point d'entrée servi
(api:app) répond exactement de la même façon avec ou sans configuration F2
complète dans l'environnement ; api.py, core/db.py, alembic/env.py, la porte
F-02A et runtime.txt sont octet pour octet ceux du freeze (5ccc9e2).
"""
import hashlib
import json
import subprocess
import sys

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


def test_the_served_entrypoint_ignores_a_complete_f2_configuration():
    """Même avec les quinze variables contractuelles valides (synthétiques),
    api:app n'expose aucune route F2, ne charge aucun module F2 ni F04-B, ne
    tente aucune connexion et répond exactement comme sans elles."""
    contract = {k: v for k, v in s.environ().items() if k.startswith("ORYX_F2_")}
    baseline = _probe({})
    configured = _probe(contract)
    assert configured == baseline
    assert configured["f2_modules"] == [] and configured["attempts"] == []
    routes = [tuple(r) for r in configured["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))]
    assert routes == sorted(set(MAIN_ROUTES))
