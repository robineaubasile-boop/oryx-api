"""ACT-L1 / F04-C V0.3 — Q1-10 avec signaux RÉELS (freeze § 6 « délai dépassé »,
§ 8.C dernier point). Sous-processus : Uvicorn 0.27.0 dans le fil principal,
vrai SIGTERM / double SIGINT envoyés par ce test, fil bloqué dans une
transaction ouverte.

Ce que ces tests ÉTABLISSENT : sous B, un délai Uvicorn fini annule la
requête mais jamais le job ; aucune ressource n'est fermée tant que le fil
travaille ; un dépassement de l'échéance de drainage F04-C est DRAIN_TIMEOUT
(jamais une fermeture gracieuse) ; un double SIGINT saute le lifespan.
Ce qu'ils n'établissent PAS (P2, F04-E) : délais Railway, SIGKILL, issue d'un
COMMIT dont la réponse HTTP était 500.
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest
import sqlalchemy as sa

from tests import f04c_support as f
from tests.f2_support import prepare_schema
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture

CHILD = REPO_ROOT / "tests" / "f04c_signal_child.py"


@pytest.fixture(scope="module")
def migrated(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()
    return pg_url


def load(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_kind(path, kind, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(e["kind"] == kind for e in load(path)):
            return True
        time.sleep(0.02)
    return False


def run_child(tmp_path, migrated, name, graceful, drain, signals, hold):
    journal_path, release = tmp_path / f"{name}.jsonl", tmp_path / f"{name}.release"
    child = subprocess.Popen([sys.executable, str(CHILD), str(journal_path), str(release),
                              "none" if graceful is None else str(graceful), "none" if drain is None else str(drain)],
                             cwd=REPO_ROOT, env=_clean_env({"ORYX_TEST_DATABASE_URL": migrated}),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_kind(journal_path, "HOOK_BLOCKED"), "fil non bloqué"
        for sig in signals:
            child.send_signal(sig)
            time.sleep(0.3)
        time.sleep(hold)
        before = load(journal_path)
        alive_before_release = child.poll() is None
        release.write_text("go")
        output, _ = child.communicate(timeout=120)
    finally:
        if child.poll() is None:
            child.kill()
    events = load(journal_path)
    journal = f.Journal()
    journal.events = events
    result = f.check_invariants(journal)
    payload = {"scenario": name, "signals": [s.name for s in signals], "timeout_graceful_shutdown": graceful,
               "drain_timeout": drain, "alive_before_release": alive_before_release, "exit_code": child.returncode,
               "invariants": {k: ("OK" if not v else "VIOLATED") for k, v in result.items()}, "violations": result,
               "timeline": [f"{e['seq']:>4} +{(e['t'] - events[0]['t']) * 1000:9.1f}ms {e['kind']:<20} "
                            + " ".join(f"{k}={v}" for k, v in e.items()
                                       if k not in ("seq", "t", "kind", "ident", "thread")) for e in events]}
    (f.evidence_dir() / f"{name}.log").write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    kinds_before = [e["kind"] for e in before]
    return events, kinds_before, alive_before_release, child.returncode, result, output


def first(events, kind):
    found = [e for e in events if e["kind"] == kind]
    return found[0] if found else None


def test_q1_10_real_sigterm_with_finite_uvicorn_timeout_is_an_honest_drain_timeout(tmp_path, migrated):
    events, before, alive, code, result, output = run_child(
        tmp_path, migrated, "Q1-10-sigterm-graceful1-drain2", 1.0, 2.0, [signal.SIGTERM], hold=4.0)
    assert f.violated(result) == {}, (result, output[-2000:])
    cancelled = first(events, "HTTP_CANCELLED")
    assert cancelled and "graceful shutdown" in cancelled["msg"]  # Uvicorn a annulé la tâche de requête
    assert first(events, "OUTCOME")["outcome"] == "drain_timeout" and "RESOURCE_CLOSED" not in before
    assert first(events, "RESOURCE_CLOSED") is None  # jamais fermées : le fil pouvait encore les utiliser
    returned = first(events, "UVICORN_RUN_RETURNED")
    assert returned["state"] == "draining" and returned["level"] == "debutant"
    assert returned["nondaemon"] == ["AnyIO worker thread"] and alive  # le fil suivi garde le processus
    assert first(events, "JOB_FINISHED")["seq"] > first(events, "HOOK_RELEASED")["seq"]
    assert code == 0


def test_q1_10_real_sigterm_without_drain_deadline_closes_only_after_the_thread_ends(tmp_path, migrated):
    events, before, alive, code, result, output = run_child(
        tmp_path, migrated, "Q1-10-sigterm-graceful1-nodeadline", 1.0, None, [signal.SIGTERM], hold=3.0)
    assert f.violated(result) == {}, (result, output[-2000:])
    assert "RESOURCE_CLOSED" not in before and alive
    assert first(events, "RESOURCE_CLOSED")["seq"] > first(events, "JOB_FINISHED")["seq"]
    assert first(events, "OUTCOME")["outcome"] == "graceful"
    assert first(events, "UVICORN_RUN_RETURNED")["level"] == "avance"  # commit suivi AVANT la fermeture
    assert code == 0


def test_q1_10_double_sigint_skips_the_lifespan_and_claims_nothing(tmp_path, migrated):
    events, before, alive, code, result, output = run_child(
        tmp_path, migrated, "Q1-10-double-sigint", None, None, [signal.SIGINT, signal.SIGINT], hold=2.0)
    assert f.violated(result) == {}, (result, output[-2000:])
    returned = first(events, "UVICORN_RUN_RETURNED")
    assert returned["force_exit"] is True and returned["state"] == "open" and returned["outcome"] is None
    assert first(events, "OUTCOME") is None and first(events, "RESOURCE_CLOSED") is None  # aucune garantie revendiquée
    assert alive and first(events, "JOB_FINISHED")["seq"] > first(events, "HOOK_RELEASED")["seq"]
