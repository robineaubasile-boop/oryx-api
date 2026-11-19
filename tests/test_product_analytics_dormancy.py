"""Dormance de V0-T08 Lot 2A : les services core/product_analytics/ existent
mais ne sont raccordés à rien.

- aucun module applicatif (api.py, core/, scripts/, alembic/, ci/,
  diagnostics/, test.py) n'importe le paquet ; le vrai api:app, importé
  dans un sous-processus réseau coupé, ne le charge pas et n'expose aucune
  route nouvelle (même avec des variables évoquant une activation) ;
- le paquet n'importe que la bibliothèque standard utile, SQLAlchemy,
  core.models et lui-même : ni api, ni FastAPI / Starlette, ni F2
  (auth_f2*, authz_f2*, f2_runtime), ni route_policy (F-02A), ni moteurs
  pédagogiques, ni réseau, ni threads, ni planificateur, ni lecture
  d'environnement ;
- aucune migration ajoutée (tête 0022, 22 fichiers) et 0022 inchangée ;
- frontend : aucune référence au paquet ni aux services.
"""
import ast
import hashlib
import json
import subprocess
import sys

from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _script_directory

PACKAGE = REPO_ROOT / "core" / "product_analytics"
PACKAGE_MODULE = "core.product_analytics"
MODULES = ("__init__.py", "campaigns.py", "common.py", "events.py", "participation.py", "retention.py")
MIGRATION_0022 = REPO_ROOT / "alembic" / "versions" / "0022_product_analytics_v1.py"
# sha256 de 0022 telle que fusionnée sur main (PR #247, 47bb843) : le lot 2A
# ne la modifie pas.
MIGRATION_0022_SHA256 = "860305fd4b13a312347444fc31add80587ad58fa03c7f07818f7ec10013be053"
ALLOWED_IMPORTS = {"hashlib", "json", "re", "uuid", "dataclasses", "datetime", "sqlalchemy", "sqlalchemy.exc",
                   "core.models",
                   "core.product_analytics", "core.product_analytics.common", "core.product_analytics.campaigns",
                   "core.product_analytics.retention"}


def _imports(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            if node.module in ("core", "core.product_analytics"):
                found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_package_is_exactly_the_expected_modules():
    assert sorted(p.name for p in PACKAGE.iterdir() if p.suffix == ".py") == sorted(MODULES)
    assert not [p for p in PACKAGE.iterdir() if p.is_dir() and p.name != "__pycache__"]
    # Importer le paquet n'importe aucun service.
    assert _imports(PACKAGE / "__init__.py") == set()


def test_package_imports_only_the_allowed_modules():
    for name in MODULES:
        imported = _imports(PACKAGE / name)
        assert imported <= ALLOWED_IMPORTS, (name, imported - ALLOWED_IMPORTS)
        source = (PACKAGE / name).read_text(encoding="utf-8")
        for marker in ("os.environ", "getenv", "__main__", "Thread", "asyncio", "schedule", "socket", "requests",
                       "httpx", "anthropic", "APIRouter", "FastAPI", "route_policy", "authz_f2", "auth_f2",
                       "f2_runtime"):
            assert marker not in source, (name, marker)


def test_no_application_module_imports_the_package():
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        if parts[0] in ("tests", ".git") or "node_modules" in parts or path.parent == PACKAGE:
            continue
        imported = _imports(path)
        text = path.read_text(encoding="utf-8")
        if any(i == PACKAGE_MODULE or i.startswith(PACKAGE_MODULE + ".") for i in imported) or \
                "product_analytics." in text or "import_module" in text and "product_analytics" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
    for directory in ("web", "web-v2"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in (".html", ".js", ".mjs", ".css", ".json"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert "product_analytics" not in text and "record_event" not in text, path


def test_no_migration_added_and_0022_unchanged():
    script = _script_directory()
    assert script.get_heads() == ["0022_product_analytics_v1"]
    assert len(list((REPO_ROOT / "alembic" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))) == 22
    assert hashlib.sha256(MIGRATION_0022.read_bytes()).hexdigest() == MIGRATION_0022_SHA256


PROBE = r'''
import json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit pendant le test de dormance")
socket.socket.connect = guard
socket.create_connection = lambda *a, **k: guard(None, a[0] if a else None)

import api
routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
loaded = sorted(name for name in sys.modules if name == "core.product_analytics"
                or name.startswith("core.product_analytics."))
print(json.dumps({"routes": routes, "loaded": loaded, "attempts": attempts}))
'''


def test_real_api_never_loads_the_services_whatever_the_environment():
    from tests.test_f2_dormancy import MAIN_ROUTES

    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1",
                             "ORYX_ANALYTICS_CAMPAIGN": "wave-0", "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["loaded"] == []
        assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
            set(MAIN_ROUTES))
        assert result["attempts"] == []
