"""Tests sans base des outils de la CI GitHub V1 (ci/, docs/ci.md).

Ils prouvent que les garde-fous de la CI échouent quand il le faut :

1. ci/check_test_report.py refuse un rapport en échec, en erreur, sauté,
   xfail, désélectionné, vide ou incohérent ;
2. ci/verify_required.py (contrôle « ci / required ») refuse tout job
   échoué, annulé, sauté, manquant, inattendu ou au résultat inconnu, ainsi
   que tout résumé de branche manquant, rouge, d'un autre commit ou d'une
   autre version de PostgreSQL ;
3. ci/verify_test_database.py refuse toute base non locale, non jetable ou
   d'une autre version, et toute variable RAILWAY_* / DATABASE_* / PG* ;
4. ci/check_migrations.py détecte une migration historique modifiée et une
   différence de schéma ;
5. le workflow garde ses six contrôles, ses déclencheurs, ses permissions
   minimales et ses actions épinglées par SHA.
"""
import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path

import pytest
import yaml

from ci import check_migrations, check_test_report, verify_required, verify_test_database

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"


# --------------------------------------------------------------------------
# 1. ci/check_test_report.py
# --------------------------------------------------------------------------

def _pytest_junit(tmp_path, cases, declared=None):
    body = []
    for name, outcome in cases:
        child = {"passed": "", "failure": "<failure message='x'/>", "error": "<error message='x'/>",
                 "skipped": "<skipped message='x'/>", "xfail": "<skipped type='pytest.xfail' message='x'/>"}[outcome]
        body.append(f"<testcase classname='t' name='{name}'>{child}</testcase>")
    counts = declared or {
        "tests": len(cases),
        "failures": sum(o == "failure" for _, o in cases),
        "errors": sum(o == "error" for _, o in cases),
        "skipped": sum(o in ("skipped", "xfail") for _, o in cases),
    }
    attrs = " ".join(f"{key}='{value}'" for key, value in counts.items())
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite name='pytest' {attrs}>{''.join(body)}</testsuite></testsuites>")
    return str(path)


def _log(tmp_path, summary):
    path = tmp_path / "pytest.log"
    path.write_text(f"....\n{summary}\n")
    return str(path)


def test_report_accepts_a_fully_passing_pytest_run(tmp_path):
    junit = _pytest_junit(tmp_path, [("a", "passed"), ("b", "passed")])
    counts, problems = check_test_report.evaluate(
        "pytest", junit, _log(tmp_path, "=== 2 passed, 3 warnings in 1.20s ==="), 0)
    assert problems == []
    assert counts["tests"] == 2 and counts["summary"] == {"passed": 2, "warnings": 3}


def test_report_accepts_the_quiet_summary_line(tmp_path):
    junit = _pytest_junit(tmp_path, [("a", "passed")])
    assert check_test_report.evaluate("pytest", junit, _log(tmp_path, "1 passed in 0.50s (0:00:00)"), 0)[1] == []


@pytest.mark.parametrize("outcome, summary", [
    ("failure", "1 failed, 1 passed in 1.0s"),
    ("error", "1 passed, 1 error in 1.0s"),
    ("skipped", "1 passed, 1 skipped in 1.0s"),
    ("xfail", "1 passed, 1 xfailed in 1.0s"),
])
def test_report_refuses_failed_errored_skipped_and_xfailed_tests(tmp_path, outcome, summary):
    junit = _pytest_junit(tmp_path, [("a", "passed"), ("b", outcome)])
    _, problems = check_test_report.evaluate("pytest", junit, _log(tmp_path, f"=== {summary} ==="), 1)
    assert problems


@pytest.mark.parametrize("summary", [
    "=== 2 passed, 5 deselected in 1.0s ===",
    "=== 2 passed, 1 xpassed in 1.0s ===",
    "=== 3 passed in 1.0s ===",
])
def test_report_refuses_deselection_xpass_and_count_mismatch(tmp_path, summary):
    junit = _pytest_junit(tmp_path, [("a", "passed"), ("b", "passed")])
    assert check_test_report.evaluate("pytest", junit, _log(tmp_path, summary), 0)[1]


def test_report_refuses_a_nonzero_exit_code_even_if_the_report_is_green(tmp_path):
    junit = _pytest_junit(tmp_path, [("a", "passed")])
    assert check_test_report.evaluate("pytest", junit, _log(tmp_path, "=== 1 passed in 1.0s ==="), 2)[1] == [
        "code de sortie 2"]


def test_report_refuses_empty_unreadable_or_inconsistent_reports(tmp_path):
    empty = _pytest_junit(tmp_path, [])
    assert "aucun test exécuté" in check_test_report.evaluate(
        "pytest", empty, _log(tmp_path, "=== no tests ran in 0.01s ==="), 0)[1]
    missing = str(tmp_path / "absent.xml")
    assert check_test_report.evaluate("pytest", missing, _log(tmp_path, "=== 1 passed in 1.0s ==="), 0)[1]
    lying = _pytest_junit(tmp_path, [("a", "passed")],
                          declared={"tests": 2, "failures": 0, "errors": 0, "skipped": 0})
    assert check_test_report.evaluate("pytest", lying, _log(tmp_path, "=== 1 passed in 1.0s ==="), 0)[1]
    no_summary = _pytest_junit(tmp_path, [("a", "passed")])
    assert check_test_report.evaluate("pytest", no_summary, _log(tmp_path, "interrompu"), 0)[1]


def test_report_checks_node_junit_cases(tmp_path):
    path = tmp_path / "node.xml"
    path.write_text("<testsuites><testcase name='a'/><testcase name='b'><skipped/></testcase></testsuites>")
    assert check_test_report.evaluate("node", str(path), "", 0)[1] == ["1 test(s) skipped"]
    path.write_text("<testsuites><testcase name='a'/><testcase name='b'/></testsuites>")
    assert check_test_report.evaluate("node", str(path), "", 0) == (
        {"tests": 2, "failures": 0, "errors": 0, "skipped": 0}, [])


def test_report_cli_writes_a_failure_result_for_the_aggregator(tmp_path):
    junit = _pytest_junit(tmp_path, [("a", "failure")])
    out = tmp_path / "result.json"
    code = check_test_report.main(["--kind", "pytest", "--leg", "postgres-16", "--junit", junit,
                                   "--log", _log(tmp_path, "=== 1 failed in 1.0s ==="), "--exit-code", "1",
                                   "--commit", SHA, "--out", str(out)])
    assert code == 1
    assert json.loads(out.read_text())["status"] == "failure"


# --------------------------------------------------------------------------
# 2. ci/verify_required.py
# --------------------------------------------------------------------------

def _green_needs():
    return {job: {"result": "success", "outputs": {}} for job in verify_required.EXPECTED_JOBS}


def _green_results():
    counts = {"tests": 10, "failures": 0, "errors": 0, "skipped": 0}
    results = {}
    for major in (16, 18):
        results[f"postgres-{major}"] = {
            "leg": f"postgres-{major}", "kind": "pytest", "commit": SHA, "status": "success", "counts": counts,
            "extra": {"postgres.json": {"server_version": f"{major}.1", "server_version_num": f"{major}0001"}},
        }
        results[f"migrations-{major}"] = {
            "leg": f"migrations-{major}", "kind": "migrations", "commit": SHA, "status": "success",
            "head": "0014_evaluation_run_leases", "server_version_num": major * 10000 + 1,
            "checks": [{"check": "x", "status": "passed"}],
        }
    results["web"] = {"leg": "web", "kind": "node", "commit": SHA, "status": "success", "counts": counts}
    return results


def _write_results(tmp_path, results):
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    for leg, result in results.items():
        (root / f"result-{leg}").mkdir(parents=True)
        payload = result if isinstance(result, str) else json.dumps(result)
        (root / f"result-{leg}" / "result.json").write_text(payload)
    return root


def test_required_accepts_only_a_fully_green_run(tmp_path):
    assert verify_required.check_needs(_green_needs()) == []
    assert verify_required.check_results(_write_results(tmp_path, _green_results()), SHA) == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", None, "neutral", "SUCCESS"])
@pytest.mark.parametrize("job", verify_required.EXPECTED_JOBS)
def test_required_refuses_every_non_success_job_result(job, result):
    needs = _green_needs()
    needs[job]["result"] = result
    assert verify_required.check_needs(needs) != []


@pytest.mark.parametrize("job", verify_required.EXPECTED_JOBS)
def test_required_refuses_a_missing_job(job):
    needs = _green_needs()
    del needs[job]
    assert verify_required.check_needs(needs) == [f"job {job} : manquant"]


def test_required_refuses_unexpected_or_unreadable_needs():
    needs = _green_needs()
    needs["lint"] = {"result": "success"}
    assert verify_required.check_needs(needs) == ["job lint : inattendu"]
    assert verify_required.check_needs(None) and verify_required.check_needs([])
    broken = _green_needs()
    broken["web"] = "success"
    assert verify_required.check_needs(broken)


@pytest.mark.parametrize("leg", sorted(verify_required.EXPECTED_LEGS))
def test_required_refuses_a_missing_leg_result(tmp_path, leg):
    results = _green_results()
    del results[leg]
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) == [f"{leg} : résumé manquant"]


@pytest.mark.parametrize("leg", sorted(verify_required.EXPECTED_LEGS))
@pytest.mark.parametrize("mutation", ["status", "commit", "leg", "kind", "unreadable"])
def test_required_refuses_a_red_foreign_or_unreadable_leg_result(tmp_path, leg, mutation):
    results = _green_results()
    if mutation == "status":
        results[leg]["status"] = "failure"
    elif mutation == "commit":
        results[leg]["commit"] = "f" * 40
    elif mutation == "leg":
        results[leg]["leg"] = "postgres-17"
    elif mutation == "kind":
        results[leg]["kind"] = "other"
    else:
        results[leg] = "{tronqué"
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) != []


@pytest.mark.parametrize("leg", ["postgres-16", "postgres-18", "web"])
@pytest.mark.parametrize("field, value", [("failures", 1), ("errors", 1), ("skipped", 1), ("tests", 0)])
def test_required_refuses_red_test_counts(tmp_path, leg, field, value):
    results = _green_results()
    results[leg]["counts"] = dict(results[leg]["counts"], **{field: value})
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) != []


def test_required_refuses_the_wrong_postgres_version(tmp_path):
    results = _green_results()
    results["postgres-18"]["extra"]["postgres.json"]["server_version_num"] = "160015"
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) != []
    results = _green_results()
    results["migrations-16"]["server_version_num"] = 180006
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) != []
    results = _green_results()
    results["migrations-18"]["checks"][0]["status"] = "failed"
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) != []


def test_required_refuses_an_unexpected_leg(tmp_path):
    results = _green_results()
    results["postgres-17"] = copy.deepcopy(results["postgres-16"])
    assert verify_required.check_results(_write_results(tmp_path, results), SHA) == [
        "postgres-17 : résumé inattendu"]


def test_required_cli_fails_closed_without_needs_or_results(tmp_path, monkeypatch):
    monkeypatch.delenv("NEEDS_JSON", raising=False)
    assert verify_required.main(["--results", str(tmp_path / "absent"), "--commit", SHA]) == 1
    monkeypatch.setenv("NEEDS_JSON", json.dumps(_green_needs()))
    assert verify_required.main(["--results", str(_write_results(tmp_path, _green_results())),
                                 "--commit", SHA]) == 0


# --------------------------------------------------------------------------
# 3. ci/verify_test_database.py
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "postgresql://postgres:x@127.0.0.1:55432/oryx_ci_postgres_16_test",
    "postgresql+psycopg2://postgres:x@localhost:55432/oryx_ci_migrations_18_test",
    "postgresql://postgres:x@[::1]:55432/oryx_ci_offline_16_test",
])
def test_database_url_accepts_local_disposable_databases(url):
    assert verify_test_database.check_url(url).database.endswith("_test")


@pytest.mark.parametrize("url", [
    "",
    "sqlite:///oryx_ci_test",
    "postgresql://postgres:secret@monorail.proxy.rlwy.net:41234/oryx_ci_postgres_16_test",
    "postgresql://postgres:secret@postgres.railway.internal/oryx_ci_postgres_16_test",
    "postgresql://postgres:secret@10.0.0.5/oryx_ci_postgres_16_test",
    "postgresql://postgres:secret@/oryx_ci_postgres_16_test?host=/var/run/postgresql",
    "postgresql://postgres:secret@127.0.0.1/railway",
    "postgresql://postgres:secret@127.0.0.1/oryx",
    "postgresql://postgres:secret@127.0.0.1/production_test",
])
def test_database_url_refuses_remote_or_non_disposable_targets_without_leaking(url):
    with pytest.raises(verify_test_database.Refused) as error:
        verify_test_database.check_url(url)
    assert "secret" not in str(error.value)


def _server(**overrides):
    row = {"database": "oryx_ci_postgres_18_test", "server_addr": "127.0.0.1", "comment": "oryx-disposable-test-db",
           "in_recovery": False, "listen_addresses": "127.0.0.1", "server_version": "18.6"}
    row.update(overrides)
    return row


def test_database_server_accepts_the_pinned_local_disposable_server():
    verify_test_database.check_server(_server(), "18.6")


@pytest.mark.parametrize("overrides", [
    {"database": "railway"},
    {"server_addr": None},
    {"server_addr": "172.17.0.2"},
    {"server_addr": "10.1.2.3"},
    {"comment": None},
    {"comment": "production"},
    {"in_recovery": True},
    {"listen_addresses": "*"},
    {"server_version": "18.5"},
])
def test_database_server_refuses_anything_but_the_pinned_local_disposable_server(overrides):
    with pytest.raises(verify_test_database.Refused):
        verify_test_database.check_server(_server(**overrides), "18.6")


def test_database_environment_check_names_railway_application_and_libpq_variables():
    environ = {"HOME": "/x", "RAILWAY_ENVIRONMENT": "production", "DATABASE_URL": "postgres://...",
               "DATABASE_PUBLIC_URL": "postgres://...", "PGHOST": "db", "PGPASSWORD": "p",
               "ORYX_TEST_DATABASE_URL": "postgresql://..."}
    assert verify_test_database.forbidden_environment(environ) == [
        "DATABASE_PUBLIC_URL", "DATABASE_URL", "PGHOST", "PGPASSWORD", "RAILWAY_ENVIRONMENT"]


# --------------------------------------------------------------------------
# 4. ci/check_migrations.py (parties sans base)
# --------------------------------------------------------------------------

def test_migration_chain_and_frozen_history_pass_on_this_checkout():
    chain = check_migrations.revision_chain()
    check_migrations.check_revision_files(chain)
    frozen = check_migrations.check_frozen_history(chain)
    assert frozen[0] == "0001_current_oryx_baseline"
    assert frozen[-1] == "0014_evaluation_run_leases"


def test_frozen_history_manifest_matches_the_reference_main_files():
    lines = (REPO_ROOT / "ci" / "migration_history.sha256").read_text().splitlines()
    assert len(lines) == 14
    for number, line in enumerate(lines, start=1):
        digest, path = line.split("  ")
        assert Path(path).name.startswith(f"{number:04d}_")
        assert hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() == digest


def test_frozen_history_detects_a_modified_historical_migration(tmp_path, monkeypatch):
    lines = (REPO_ROOT / "ci" / "migration_history.sha256").read_text().splitlines()
    digest, path = lines[6].split("  ")
    lines[6] = f"{'0' * 64}  {path}"
    tampered = tmp_path / "manifest.sha256"
    tampered.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(check_migrations, "MANIFEST", tampered)
    with pytest.raises(check_migrations.CheckFailed, match="migration historique modifiée"):
        check_migrations.check_frozen_history(check_migrations.revision_chain())


def test_frozen_history_refuses_a_manifest_that_is_not_a_chain_prefix(tmp_path, monkeypatch):
    lines = (REPO_ROOT / "ci" / "migration_history.sha256").read_text().splitlines()
    tampered = tmp_path / "manifest.sha256"
    tampered.write_text("\n".join(lines[:3] + lines[4:]) + "\n")
    monkeypatch.setattr(check_migrations, "MANIFEST", tampered)
    with pytest.raises(check_migrations.CheckFailed, match="préfixe exact"):
        check_migrations.check_frozen_history(check_migrations.revision_chain())


def test_schema_difference_reports_missing_and_extra_catalog_rows():
    base = {key: [] for key in check_migrations.SNAPSHOT_QUERIES}
    base["constraints"] = [["t", "t_pkey", "p", "PRIMARY KEY (id)", True, False, False, " ", " "]]
    same = copy.deepcopy(base)
    assert check_migrations.schema_difference(base, same) == []
    extra = copy.deepcopy(base)
    extra["constraints"].append(["t", "t_id_not_null", "n", "NOT NULL id", True, False, False, " ", " "])
    assert check_migrations.schema_difference(base, extra)
    assert check_migrations.schema_difference(extra, base)


# --------------------------------------------------------------------------
# 5. Workflow
# --------------------------------------------------------------------------

def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_triggers_without_path_filters_or_pull_request_target():
    workflow = _workflow()
    triggers = workflow[True]  # YAML 1.1 : la clé « on » est lue comme True
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request_target" not in WORKFLOW.read_text(encoding="utf-8")


def test_workflow_has_the_six_stable_checks_and_a_strict_aggregator():
    workflow = _workflow()
    assert workflow["name"] == "ci"
    jobs = workflow["jobs"]
    names = []
    for job in jobs.values():
        matrix = job.get("strategy", {}).get("matrix", {}).get("postgres")
        names += [job["name"].replace("${{ matrix.postgres }}", major) for major in matrix] if matrix else [job["name"]]
        if matrix:
            assert job["strategy"]["fail-fast"] is False
        assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] <= 90
        assert job["runs-on"] == "ubuntu-24.04"
    assert sorted(names) == sorted(["postgres-16", "postgres-18", "migrations-16", "migrations-18", "web",
                                    "required"])
    required = jobs["required"]
    assert required["if"] == "always()"
    assert required["needs"] == list(verify_required.EXPECTED_JOBS)
    assert sorted(verify_required.EXPECTED_LEGS) == sorted(n for n in names if n != "required")


def test_workflow_is_read_only_pinned_and_never_bypasses_failures():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert _workflow()["permissions"] == {"contents": "read"}
    for job in _workflow()["jobs"].values():
        assert "permissions" not in job
        for step in job["steps"]:
            assert "continue-on-error" not in step
            if "uses" in step:
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
    for forbidden in ("|| true", "secrets.", "id-token", "self-hosted", " -k ", "--deselect", "xdist",
                      "pull_request_target", "RAILWAY_TOKEN", "DATABASE_URL"):
        assert forbidden not in text, forbidden
    assert not re.search(r"\brailway\s+(up|run|ssh|link|deploy|redeploy|variables|connect)\b", text, re.I)
