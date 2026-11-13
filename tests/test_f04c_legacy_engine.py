"""ACT-L1 / F04-C V0.3 — Q2 / O1 : moteur SQLAlchemy legacy inerte (freeze § 7, § 8.D).

Q2-01 à Q2-06 : un sous-processus Python NEUF par scénario
(tests/f04c_q2_probe.py), instrumentation posée AVANT les imports,
environnement explicite (aucune variable d'environnement modifiée par F04-C).
Plus un test STATIQUE : aucun module F2 ni module chargé par la composition
n'accède à core.db.engine, SessionLocal ou get_db.
"""
import ast
import json
import subprocess
import sys

import pytest
import sqlalchemy as sa

from tests import f04b_support as synthetic
from tests.f2_support import prepare_schema
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture

PROBE = REPO_ROOT / "tests" / "f04c_q2_probe.py"
LEGACY = "core.db (legacy, import)"
F04B = "core.f2_runtime.database (F04-B)"


def probe(tmp_path, scenario, **env):
    out = tmp_path / f"{scenario}.json"
    result = subprocess.run([sys.executable, str(PROBE), scenario, str(out)], cwd=REPO_ROOT, capture_output=True,
                            text=True, env=_clean_env(env), timeout=300)
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["errors"] == [], report["errors"]
    from tests.f04c_support import evidence_dir
    (evidence_dir() / f"{scenario}.log").write_text(json.dumps(
        {k: v for k, v in report.items() if k != "events"}, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def origins(report):
    return [e["origin"] for e in report["engines"] if e["label"] != "OBSERVER"]


def label(report, origin):
    [found] = [e["label"] for e in report["engines"] if e["origin"] == origin]
    return found


def no_legacy_use(report):
    legacy = [e["label"] for e in report["engines"] if e["origin"] == LEGACY]
    for engine in legacy:
        assert engine not in report["physical_connects_by_engine"], report["physical_connects_by_engine"]
        assert engine not in report["checkouts_by_engine"], report["checkouts_by_engine"]
        assert engine not in report["engine_connect_by_engine"]
        assert all(s["engine"] != engine for s in report["orm_sessions"])
    assert report["legacy_calls"] == []
    assert report["final_modules"]["api"] is False


@pytest.mark.parametrize("database_url", [None, synthetic.FAKE_DATABASE_URL])
def test_q2_01_02_importing_the_entrypoint_loads_no_legacy_module_and_builds_no_engine(tmp_path, database_url):
    env = synthetic.environ() if database_url else {}
    report = probe(tmp_path, "Q2-01" if database_url is None else "Q2-02", **env)
    assert {k: report["after_import"][k] for k in ("api", "core.db", "core.models")} == {
        "api": False, "core.db": False, "core.models": False}
    assert report["engines"] == [] and report["create_engine_calls"] == []
    assert report["psycopg2_connects"] == [] and report["socket_connects"] == []
    assert report["threads_alive"] == ["MainThread"]


@pytest.mark.parametrize("invalid", ["postgresql://u:SECRET-Q2@127.0.0.1:notaport/x",
                                     "mysql://u:SECRET-Q2@127.0.0.1:1/x", "garbage SECRET-Q2"])
def test_q2_03_an_invalid_url_is_refused_before_any_import_that_could_build_the_legacy_engine(tmp_path, invalid):
    report = probe(tmp_path, "Q2-03", DATABASE_URL=synthetic.FAKE_DATABASE_URL, Q2_INVALID_URL=invalid)
    assert report["startup"] == {"type": "lifespan.startup.failed",
                                 "message": "f2_startup_failed stage=config cause=F2ConfigError"}
    assert report["after_startup"]["core.db"] is False and report["final_modules"]["core.db"] is False
    assert report["engines"] == [] and report["psycopg2_connects"] == []
    assert "SECRET-Q2" not in json.dumps(report)


def test_q2_04_an_aborted_startup_closes_the_f04b_engine_without_loading_the_legacy(tmp_path):
    report = probe(tmp_path, "Q2-04", DATABASE_URL=synthetic.FAKE_DATABASE_URL)
    assert report["failure"] == ["preflight", "F2PreflightError:connection_failed"]
    assert origins(report) == [F04B] and report["final_modules"]["core.db"] is False
    assert [p["phase"] for p in report["psycopg2_connects"]] == ["startup"]  # tentative du moteur F04-B seul
    assert report["alive_engines_after_cleanup"] == []


@pytest.fixture(scope="module")
def migrated(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()
    return pg_url


def test_q2_05_f2_operations_use_only_the_f04b_engine_and_never_the_legacy_one(tmp_path, migrated):
    report = probe(tmp_path, "Q2-05", DATABASE_URL=migrated)
    assert report["startup"] == {"type": "lifespan.startup.complete"}
    assert report["shutdown"] == {"type": "lifespan.shutdown.complete"} and report["outcome"] == "graceful"
    assert set(report["statuses"].values()) <= {200, 202} and len(report["statuses"]) == 11
    assert report["db_effects"] == {"level": "avance", "delivered": 1}
    # core.db n'est chargé qu'à la composition, APRÈS config, ressources et précontrôle.
    assert report["before_startup"]["core.db"] is False and report["after_startup"]["core.db"] is True
    assert report["chain_core_db"][-2:] == ["core.models", "core.db"]
    assert sorted(origins(report)) == sorted([F04B, LEGACY])  # O1 : un objet legacy INERTE
    f2 = label(report, F04B)
    assert report["f2_engine_label"] == f2
    assert report["engine_refs"] == {"AuthorizedTransactionRunner": [f2], "F2AuthService": [f2],
                                     "HttpAdmission": [f2]}
    window = report["checkouts_in_f2_window"]
    assert set(window) <= {f2, "OBSERVER"} and window[f2] >= 30  # toutes les connexions F2 : moteur F04-B
    assert {(s["engine"], s["bind_kind"]) for s in report["orm_sessions"]} == {(f2, "Connection")}  # pont ORM
    no_legacy_use(report)
    assert report["backends_after"] == report["backends_before"]  # aucune connexion du processus ne survit
    assert [e["engine"] for e in report["alive_engines_after_cleanup"]] == [label(report, LEGACY)]


def test_q2_06_failures_after_and_before_the_legacy_import_clean_up_and_never_use_it(tmp_path, migrated):
    report = probe(tmp_path, "Q2-06", DATABASE_URL=migrated)
    a, b = report["resources_failure"], report["composition_failure"]
    assert a["failure"] == ["resources", "F2ResourceBuildError"] and a["core.db"] is False
    assert "engine" in a["cleanup"] and "failed=()" in a["cleanup"]
    assert b["failure"] == ["composition", "F2CompositionError"] and b["core.db"] is True
    assert "engine" in b["cleanup"] and "failed=()" in b["cleanup"]
    no_legacy_use(report)
    assert report["backends_after"] == report["backends_before"]
    assert [e["engine"] for e in report["alive_engines_after_cleanup"]] == [label(report, LEGACY)]


# --------------------------------------------------------------------------
# Test statique : aucun accès F2 à core.db.engine / SessionLocal / get_db
# --------------------------------------------------------------------------

F2_PACKAGES = ("core/auth_f2", "core/auth_f2_http", "core/authz_f2", "core/authz_f2_http", "core/f2_runtime")
# Modules non F2 chargés par la composition (Q2-05 / chaîne d'import) :
COMPOSITION_LEGACY_MODULES = ("core/models.py", "core/assistant_delivery.py", "core/interaction_identity.py",
                              "core/cognitive_capture.py", "core/decryptage_cognitive_runtime.py",
                              "core/decryptage_progress.py", "core/decryptage_engine.py",
                              "core/pedagogie_library.py", "core/decryptage_providers.py")


def _paths():
    for package in F2_PACKAGES:
        yield from sorted((REPO_ROOT / package).glob("*.py"))
    for relative in COMPOSITION_LEGACY_MODULES:
        yield REPO_ROOT / relative


def test_no_f2_or_composition_module_reaches_the_legacy_engine_session_factory_or_get_db():
    for path in _paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.db":
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module == "core" and any(a.name == "db" for a in node.names):
                imported.add("*module*")
            elif isinstance(node, ast.Import) and any(a.name == "core.db" for a in node.names):
                imported.add("*module*")
        relative = str(path.relative_to(REPO_ROOT))
        assert not names & {"SessionLocal", "get_db"}, relative
        allowed = {"Base"} if relative == "core/models.py" else set()
        assert imported <= allowed, (relative, imported)
