"""R1 PR 9-B2 — contrôles statiques et de dormance (sans base de données).

Complètent tests/test_authz_f2_boundary.py (inventaire BF1, instruction
_PREPARATION_CONTEXT nominative), tests/test_authz_f2_dormancy.py (matrice
d'imports) et tests/test_authz_f2_pr9b1_boundary.py (dispatch des trousseaux) :

- registre fermé à DIX-SEPT commandes, une seule nouvelle commande
  (ReadDecryptagePreparationContext, plan READ inchangé, aucun plan nouveau) ;
- l'orchestrateur ne parle à la base QUE par AuthorizedTransactionRunner.run,
  avec trois commandes exactes (ReadProfile, ReadDecryptagePreparationContext,
  RecordDecryptageAttemptFailure) ;
- graphe d'imports réel : ni yfinance, ni core.data_fetcher, ni
  core.ticker_resolver, ni core.market_lookup, ni api ne sont atteignables
  depuis B2 (modules de la frontière et transports) ;
- transports : aucune configuration lue, aucune journalisation ;
- dormance : aucun module applicatif n'importe B2, api.py ne le mentionne
  pas, le vrai api:app ne le charge pas et répond 404 sur les routes F2
  Décrypter futures, importer B2 n'ouvre ni connexion ni réseau (même avec
  DATABASE_URL), aucune migration nouvelle.
"""
import ast
import json
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_reservation as dr
from tests.test_f2_dormancy import MAIN_ROUTES
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

AUTHZ = REPO_ROOT / "core" / "authz_f2"
B2_BOUNDARY = ("decryptage_context", "decryptage_instrument", "decryptage_market_data", "decryptage_candidate",
               "decryptage_generation")
B2_FILES = [AUTHZ / f"{name}.py" for name in B2_BOUNDARY]
PROVIDERS = REPO_ROOT / "core" / "decryptage_providers.py"
FORBIDDEN_REACH = ("yfinance", "core.data_fetcher", "core.ticker_resolver", "core.market_lookup", "api", "fastapi",
                   "core.db_session", "core.evaluation_provider")


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(path) -> set:
    found = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                if (REPO_ROOT / (candidate.replace(".", "/") + ".py")).is_file():
                    found.add(candidate)
    return found


def _module_path(name):
    path = REPO_ROOT / (name.replace(".", "/") + ".py")
    if path.is_file():
        return path
    package = REPO_ROOT / name.replace(".", "/") / "__init__.py"
    return package if package.is_file() else None


def _reachable(start_paths) -> set:
    """Fermeture des imports du dépôt (et noms externes atteints)."""
    seen, stack = set(), list(start_paths)
    names = set()
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imports(path):
            names.add(name)
            target = _module_path(name)
            if target is not None:
                stack.append(target)
    return names


def test_the_production_registry_has_seventeen_commands_and_one_new_read():
    assert len(_adapters.REGISTRY) == 17
    spec = _adapters.REGISTRY[c.ReadDecryptagePreparationContext]
    assert spec.plan is c.READ and spec.lock is _adapters._no_business_lock
    assert spec.execute is _adapters._read_preparation_context and spec.decryptage_keyed and not spec.keyed
    assert c.PLANS == (c.READ, c.PROFILE_MUTATION, c.PORTFOLIO_MUTATION, c.DECRYPTAGE_MUTATION)
    assert c.READ == c.LockPlan("read", "FOR KEY SHARE", ())
    reads = {cmd for cmd, s in _adapters.REGISTRY.items() if s.plan is c.READ}
    assert len(reads) == 11
    assert dr.FAILURE_CLASSES == ("market_data_unavailable", "generation_unavailable", "generation_unusable")
    assert dr.LEASE_DURATION.total_seconds() == 360 and dr.MAX_ATTEMPTS == 3


def test_cognitive_tables_are_only_read_by_the_preparation_statement():
    statements = {k: v for k, v in vars(_adapters).items() if k.startswith("_") and k.isupper() and isinstance(v, str)}
    readers = {k for k, v in statements.items() if any(t in v for t in ("cognitive_events", "decryptage_cognitive_links",
                                                                      "user_statements", "analysis_facts"))}
    assert readers == {"_PREPARATION_CONTEXT"}
    assert not any(word in statements["_PREPARATION_CONTEXT"].upper().split() for word in
                   ("UPDATE", "INSERT", "DELETE", "MERGE"))


def test_the_orchestrator_reaches_postgresql_only_through_the_runner():
    tree = _tree(AUTHZ / "decryptage_generation.py")
    runner_calls = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Attribute) and n.value.attr == "_runner"}
    assert runner_calls == {"self._o._runner.run"}
    commands = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "c"}
    assert commands == {"ReadProfile", "ReadDecryptagePreparationContext", "RecordDecryptageAttemptFailure",
                        "ReserveDecryptageTurn", "ProfileView"}  # ReserveDecryptageTurn : contrôle de type seulement
    source = (AUTHZ / "decryptage_generation.py").read_text(encoding="utf-8")
    assert "c.ReserveDecryptageTurn(" not in source and "c.ProfileView(" not in source
    for path in B2_FILES:
        names = {n.id for n in ast.walk(_tree(path)) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(_tree(path)) if isinstance(n, ast.Attribute)}
        assert not names & {"Session", "SessionLocal", "engine", "connect", "begin", "commit", "rollback",
                            "claim_delivery", "acknowledge_delivery", "apply_construction_these_progress",
                            "capture_user_turn", "record_turn_progress", "bind_analysis_session"}, path.name


def test_neither_yfinance_nor_the_historical_fetchers_are_reachable_from_b2():
    reached = _reachable(B2_FILES + [PROVIDERS])
    for forbidden in FORBIDDEN_REACH:
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in reached), forbidden
    for path in B2_FILES:
        direct = _imports(path)
        assert not direct & {"anthropic", "requests", "httpx", "httpx2", "socket", "urllib.request", "os"}, path.name
    assert {n for n in _imports(PROVIDERS) if not n.startswith("core")} == {
        "json", "time", "urllib.parse", "anthropic", "httpx2", "requests", "requests.adapters"}
    for path in B2_FILES + [PROVIDERS]:  # ni import, ni nom, ni chargement dynamique
        tree = _tree(path)
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # (dfp.normalize_ticker est la normalisation LOCALE de la trame B1, jamais le résolveur)
        assert not identifiers & {"yfinance", "yf", "import_module", "__import__", "fetch_financial_data",
                                  "ticker_resolver", "data_fetcher"}, path.name


def test_the_transports_read_no_configuration_and_log_nothing():
    tree = _tree(PROVIDERS)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"environ", "getenv", "logging", "logger", "print", "SessionLocal", "fetch_financial_data"}
    assert "max_retries=0" in PROVIDERS.read_text(encoding="utf-8")
    text = PROVIDERS.read_text(encoding="utf-8")
    for marker in ("authz_f2", "LeaseGranted", "DecryptageKeyring", "ReserveDecryptageTurn",
                   "decryptage_turn_reservations"):
        assert marker not in text, marker


B2_MARKERS = ("decryptage_generation", "decryptage_candidate", "decryptage_context", "decryptage_instrument",
              "decryptage_market_data", "decryptage_providers", "ReadDecryptagePreparationContext",
              "GeneratedDecryptageCandidate", "DecryptageGenerationOrchestrator", "/api/v2/me/decryptage")


def test_no_application_module_imports_b2_and_api_never_mentions_it():
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for marker in B2_MARKERS:
        assert marker not in source, marker
    roots = [REPO_ROOT / "core", REPO_ROOT / "scripts", REPO_ROOT / "ci", REPO_ROOT / "web", REPO_ROOT / "web-v2",
             REPO_ROOT / "alembic"]
    offenders = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or "node_modules" in path.parts \
                    or path.suffix not in (".py", ".js", ".html", ".sh"):
                continue
            if path == PROVIDERS or AUTHZ in path.parents:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            offenders += [(str(path.relative_to(REPO_ROOT)), m) for m in B2_MARKERS if m in text]
    assert offenders == []


PROBE = r'''
import asyncio, json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit")
socket.socket.connect = guard
import api, httpx
GEN = "11111111-2222-4333-8444-555555555555"
COOKIE = "__Host-oryx-active-" + GEN + "=" + "A" * 43
CASES = [("POST", "/api/v2/me/decryptage"), ("GET", "/api/v2/me/decryptage"),
         ("POST", "/api/v2/me/assistant-deliveries/123e4567-e89b-42d3-a456-426614174000/ack"),
         ("POST", "/api/v2/me/decryptage/prepare"), ("POST", "/api/v2/decryptage")]
async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1))
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path in CASES:
            r = await client.request(method, path, headers={"origin": "https://oryx.test", "cookie": COOKIE,
                                     "x-oryx-generation-id": GEN, "content-type": "application/json"},
                                     content=b'{"ticker":"MC.PA","question":"","conversation_key":"c"}')
            out.append([method, path, r.status_code])
    return out
responses = asyncio.run(probe())
loaded = sorted(m for m in sys.modules if "decryptage_" in m and m.startswith("core.")
                and m not in ("core.decryptage_engine", "core.decryptage_progress", "core.decryptage_cognitive_runtime"))
routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
print(json.dumps({"responses": responses, "loaded": loaded, "attempts": attempts, "routes": routes}))
'''


@pytest.mark.parametrize("flags", [{}, {"ORYX_DECRYPTAGE_F2": "1", "ORYX_PR9B2_ENABLED": "1",
                                        "ORYX_DECRYPTAGE_GENERATION": "true", "ANTHROPIC_API_KEY": "sk-x",
                                        "EOD_API_KEY": "e", "FMP_API_KEY": "f"}])
def test_the_real_entrypoint_never_loads_b2_and_answers_404(flags):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(flags), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert [r[2] for r in result["responses"]] == [404] * 5
    assert result["loaded"] == [] and result["attempts"] == []
    assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
        set(MAIN_ROUTES))


IMPORT_PROBE = r'''
import json, socket, sys, importlib
attempts = []
socket.socket.connect = lambda self, address: attempts.append(repr(address))
socket.create_connection = lambda *a, **k: attempts.append("create_connection")
import psycopg2
psycopg2.connect = lambda *a, **k: attempts.append("psycopg2")
for m in ("core.authz_f2.decryptage_context", "core.authz_f2.decryptage_instrument",
          "core.authz_f2.decryptage_market_data", "core.authz_f2.decryptage_candidate",
          "core.authz_f2.decryptage_generation"):
    importlib.import_module(m)
boundary = sorted(m for m in ("anthropic", "requests", "httpx", "httpx2", "yfinance", "api", "fastapi",
                              "core.data_fetcher", "core.ticker_resolver", "core.market_lookup") if m in sys.modules)
importlib.import_module("core.decryptage_providers")
after = sorted(m for m in ("yfinance", "api", "fastapi", "core.data_fetcher", "core.ticker_resolver") if m in sys.modules)
print(json.dumps({"attempts": attempts, "boundary": boundary, "after": after}))
'''


@pytest.mark.parametrize("database_url", [None, "postgresql://u:p@127.0.0.1:1/oryx_never_test"])
def test_importing_b2_opens_nothing_and_loads_no_network_client(database_url):
    extra = {} if database_url is None else {"DATABASE_URL": database_url}
    out = subprocess.run([sys.executable, "-c", IMPORT_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env={**_clean_env({}), **extra}, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"attempts": [], "boundary": [], "after": []}


def test_no_new_migration():
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0021_decryptage_reservations"]
    assert len(list((REPO_ROOT / "alembic" / "versions").glob("*.py"))) == 21
