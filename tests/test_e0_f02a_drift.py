"""E0 — garde de dérive statique du registre de routage F2 (spécification V0.2-R3 § 3.9, § 6.2).

Le chemin du registre figé est désigné ICI (côté tests) et fourni au module
du banc, qui ne nomme lui-même aucun module applicatif. L'instantané attendu
est réécrit indépendamment ci-dessous (14 + 13 couples).
"""
import ast
import hashlib
import json
import subprocess
import sys

import pytest

from tests import e0_support as s

import e0_f02a_drift as drift  # noqa: E402

REGISTRY = s.REPO_ROOT / "core" / "route_policy" / "registry.py"
LOCK_PATH = s.CONFIG_DIR / "f2_registry.lock.v1.json"
LOCK = drift.load_lock(LOCK_PATH)

AUTH = ["/csrf/bootstrap", "/sign-in/start", "/sign-in/resend", "/sign-in/verify", "/sign-in/resume",
        "/session/resolve", "/logout", "/invitation/begin", "/invitation/complete", "/invitation/resume",
        "/email-link/start", "/email-link/resend", "/email-link/verify", "/email-link/result"]
INDEPENDENT_AUTH = sorted([["GET" if p == "/csrf/bootstrap" else "POST", "/api/auth/f2" + p] for p in AUTH])
INDEPENDENT_PERSONAL = sorted([
    ["GET", "/api/v2/me"], ["PUT", "/api/v2/me/level"], ["GET", "/api/v2/me/portfolio"],
    ["GET", "/api/v2/me/portfolio/{position_id}"], ["POST", "/api/v2/me/portfolio"],
    ["PUT", "/api/v2/me/portfolio/{position_id}"], ["DELETE", "/api/v2/me/portfolio/{position_id}"],
    ["GET", "/api/v2/me/analysis-overview"], ["GET", "/api/v2/me/theses"], ["GET", "/api/v2/me/theses/{thesis_id}"],
    ["POST", "/api/v2/me/decryptage/turns"], ["POST", "/api/v2/me/decryptage/replay"],
    ["POST", "/api/v2/me/decryptage/ack"]])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


def test_lock_matches_an_independently_written_snapshot():
    assert sorted(LOCK["snapshot"]["F2_AUTH"]) == INDEPENDENT_AUTH and len(INDEPENDENT_AUTH) == 14
    assert sorted(LOCK["snapshot"]["F2_PERSONAL"]) == INDEPENDENT_PERSONAL and len(INDEPENDENT_PERSONAL) == 13
    assert LOCK["expected_counts"] == {"F2_AUTH": 14, "F2_PERSONAL": 13}
    assert LOCK["registry_sha256"] == "d8accc92a55170f5fa4092eae18c7e8c29cf921f97b9853402516decefdb4b29"


def test_frozen_registry_passes():
    result = drift.check(REGISTRY, LOCK)
    assert result["verdict"] == "PASS" and result["alerts"] == [] and result["reasons"] == []
    assert result["sha256"] == hashlib.sha256(REGISTRY.read_bytes()).hexdigest()
    assert result["extraction"]["pairs"]["F2_AUTH"] == INDEPENDENT_AUTH
    assert result["extraction"]["pairs"]["F2_PERSONAL"] == INDEPENDENT_PERSONAL


def test_divergent_sha256_is_inconclusive_with_alert_never_pass(tmp_path):
    copy = tmp_path / "registry.py"
    copy.write_bytes(REGISTRY.read_bytes() + b"\n# commentaire ajout\xc3\xa9\n")
    result = drift.check(copy, LOCK)
    assert result["verdict"] == "INCONCLUSIVE" and "registry_sha256_mismatch" in result["alerts"]
    assert result["extraction"] is not None  # structure encore lisible, mais jamais PASS


def test_lock_with_another_sha256_is_inconclusive():
    result = drift.check(REGISTRY, dict(LOCK, registry_sha256="0" * 64))
    assert result["verdict"] == "INCONCLUSIVE" and result["alerts"] == ["registry_sha256_mismatch"]


def _mutated(tmp_path, old, new):
    text = REGISTRY.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "registry.py"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def _relocked(path):
    return dict(LOCK, registry_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_an_added_route_is_detected_even_with_a_matching_sha(tmp_path):
    path = _mutated(tmp_path, '    ("POST", "/logout"),\n', '    ("POST", "/logout"),\n    ("POST", "/extra"),\n')
    result = drift.check(path, _relocked(path))
    assert result["verdict"] == "INCONCLUSIVE"
    assert any("F2_AUTH" in r for r in result["reasons"])


def test_a_changed_namespace_is_detected(tmp_path):
    path = _mutated(tmp_path, 'PERSONAL_NAMESPACE = "/api/v2"', 'PERSONAL_NAMESPACE = "/api/v3"')
    result = drift.check(path, _relocked(path))
    assert result["verdict"] == "INCONCLUSIVE" and any("F2_PERSONAL" in r for r in result["reasons"])


def test_a_changed_expected_count_is_detected(tmp_path):
    path = _mutated(tmp_path, "F2_AUTH: 14", "F2_AUTH: 15")
    assert drift.check(path, _relocked(path))["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("old, new", [
    ('_F2_AUTH = _rules(F2_AUTH, SOURCE_F2_AUTH, tuple((method, AUTH_NAMESPACE + suffix) for method, suffix in (',
     '_F2_AUTH = _rules(F2_AUTH, SOURCE_F2_AUTH, tuple((method, AUTH_NAMESPACE + suffix) for method, suffix in '
     'sorted(('),
    ('AUTH_NAMESPACE = "/api/auth/f2"', 'AUTH_NAMESPACE = "/api/auth/" + "f2"'),
    ("EXPECTED_COUNTS = {", "EXPECTED_COUNTS = dict(**{"),
])
def test_statically_unresolvable_structures_are_inconclusive(tmp_path, old, new):
    text = REGISTRY.read_text(encoding="utf-8")
    assert old in text
    changed = text.replace(old, new, 1)
    if new.endswith("sorted(("):
        changed = changed.replace('    ("POST", "/email-link/result"),\n)))', '    ("POST", "/email-link/result"),\n))))', 1)
    if new.startswith("EXPECTED_COUNTS = dict"):
        changed = changed.replace("FRAMEWORK_DOCS: 8}", "FRAMEWORK_DOCS: 8})", 1)
    ast.parse(changed)
    path = tmp_path / "registry.py"
    path.write_text(changed, encoding="utf-8")
    result = drift.check(path, _relocked(path))
    assert result["verdict"] == "INCONCLUSIVE"
    assert any("non résoluble" in r for r in result["reasons"])


def test_unreadable_registry_is_inconclusive(tmp_path):
    result = drift.check(tmp_path / "absent.py", LOCK)
    assert result["verdict"] == "INCONCLUSIVE" and result["alerts"] == ["registry_unreadable"]


def test_extraction_never_executes_the_registry(tmp_path):
    marker = tmp_path / "executed"
    text = REGISTRY.read_text(encoding="utf-8")
    path = tmp_path / "registry.py"
    path.write_text(f"open({str(marker)!r}, 'w').write('x')\nimport os\n" + text, encoding="utf-8")
    drift.check(path, _relocked(path))
    assert not marker.exists()


def test_drift_module_never_imports_application_code():
    source = (s.BENCH_PY / "e0_f02a_drift.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"argparse", "ast", "hashlib", "json", "sys"}
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & {"exec", "eval", "compile", "__import__", "open_code"}
    for forbidden in ("importlib", "runpy", "import_module", "spec_from"):
        assert forbidden not in source, forbidden
    assert "core" not in imported


def test_cli_check(tmp_path):
    ok = subprocess.run([sys.executable, str(s.BENCH_PY / "e0_f02a_drift.py"), "--check", "--registry",
                         str(REGISTRY), "--lock", str(LOCK_PATH)], capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and json.loads(ok.stdout)["verdict"] == "PASS"
    missing = subprocess.run([sys.executable, str(s.BENCH_PY / "e0_f02a_drift.py"), "--check", "--lock",
                              str(LOCK_PATH)], capture_output=True, text=True, timeout=60)
    assert missing.returncode == 2  # chemin du registre obligatoire, jamais deviné
    copy = tmp_path / "registry.py"
    copy.write_bytes(REGISTRY.read_bytes() + b"\n")
    drifted = subprocess.run([sys.executable, str(s.BENCH_PY / "e0_f02a_drift.py"), "--check", "--registry",
                              str(copy), "--lock", str(LOCK_PATH)], capture_output=True, text=True, timeout=60)
    assert drifted.returncode == 1 and json.loads(drifted.stdout)["verdict"] == "INCONCLUSIVE"
