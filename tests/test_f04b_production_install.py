"""F04-B / B02, réserve d'audit 2 — installation de PRODUCTION SEULE, journalisée.

Preuve expérimentale indépendante du verrou CI : un environnement Python
VIERGE (python -m venv --clear, sans paquets du système) est créé, puis
UNIQUEMENT `pip install -r requirements.txt` (point d'entrée Railway) y est
exécuté — jamais ci/requirements-ci.txt. Suivent `pip check`, la liste des
versions installées (comparée à la fermeture exacte du verrou), puis la
sonde tests/f04b_production_probe.py exécutée par l'interpréteur de CET
environnement en mode isolé : imports de production, absence d'outil de
développement, construction des composants dormants sans réseau ni secret.

Journal : chaque commande, son code de retour, ses sorties et les versions
sont écrits dans <racine>/reports/f04b-production-install/ lorsque ce
répertoire de rapports existe (jobs ci / postgres-NN : publié comme artefact
« reports-postgres-NN ») ; sinon dans le répertoire temporaire du test.

Prérequis : Python de runtime.txt et accès à l'index de paquets. Si pip ne
peut pas joindre l'index (réseau, DNS, TLS : diagnostic lu dans SA sortie),
le test est SAUTÉ avec sa raison et le journal conservé ; toute autre erreur
d'installation le fait échouer. La CI refusant tout test sauté
(ci/check_test_report.py), il est nécessairement exécuté en CI.
"""
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys

import pytest

from tests.test_f04b_dependencies import PRODUCTION_LOCK
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

# Variables de transport réseau transmises à pip si présentes (proxy local
# d'un poste ou d'un bac à sable) ; la CI n'en définit aucune.
_TRANSPORT = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "PIP_CERT",
              "PIP_CONFIG_FILE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def _runtime_version() -> str:
    return (REPO_ROOT / "runtime.txt").read_text(encoding="utf-8").strip().removeprefix("python-")


# Signatures d'un index de paquets INJOIGNABLE (réseau, DNS, TLS) dans la
# sortie de pip ; toute autre erreur d'installation fait échouer le test.
_UNREACHABLE = ("(from versions: none)", "SSLError", "SSLCertVerificationError", "NewConnectionError",
                "Max retries exceeded", "Temporary failure in name resolution", "Network is unreachable",
                "ProxyError", "ConnectTimeoutError")


def _index_unreachable(result) -> bool:
    output = result.stdout + result.stderr
    return result.returncode != 0 and any(marker in output for marker in _UNREACHABLE)


def _environment() -> dict:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH") + _TRANSPORT
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _output_dir(tmp_path):
    reports = REPO_ROOT / "reports"
    target = (reports if reports.is_dir() else tmp_path) / "f04b-production-install"
    target.mkdir(parents=True, exist_ok=True)
    return target


class Journal:
    def __init__(self, directory):
        self.directory = directory
        self.steps = []
        self.log = (directory / "install.log").open("w", encoding="utf-8")

    def run(self, name, command, *, env, cwd=REPO_ROOT, timeout=900):
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        self.steps.append({"step": name, "command": command, "returncode": result.returncode, "started": started})
        self.log.write(f"===== {name}\n$ {' '.join(command)}\nreturncode: {result.returncode}\n"
                       f"----- stdout\n{result.stdout}\n----- stderr\n{result.stderr}\n")
        self.log.flush()
        return result

    def close(self, summary):
        self.log.close()
        (self.directory / "summary.json").write_text(json.dumps({**summary, "steps": self.steps}, indent=2,
                                                                sort_keys=True), encoding="utf-8")


def _freeze(text) -> dict:
    versions = {}
    for line in text.splitlines():
        name, _, version = line.partition("==")
        if version:
            versions[name.lower().replace("_", "-")] = version
    return versions


def test_a_clean_environment_installs_and_runs_from_requirements_txt_alone(tmp_path):
    if platform.python_version() != _runtime_version():
        pytest.skip(f"Python {_runtime_version()} requis (runtime.txt)")
    env = _environment()
    venv = tmp_path / "venv-production"
    python = str(venv / "bin" / "python")
    journal = Journal(_output_dir(tmp_path))
    summary = {"requirements_sha256": hashlib.sha256(
        (REPO_ROOT / "requirements.txt").read_bytes()).hexdigest(), "host_python": sys.version}
    try:
        commit = journal.run("commit", ["git", "rev-parse", "HEAD"], env=env)
        summary["commit"] = commit.stdout.strip()
        assert journal.run("venv", [sys.executable, "-m", "venv", "--clear", str(venv)], env=env).returncode == 0
        empty = journal.run("pip freeze (vierge)", [python, "-m", "pip", "freeze", "--all"], env=env)
        assert empty.returncode == 0
        summary["clean_before_install"] = sorted(_freeze(empty.stdout))
        assert set(summary["clean_before_install"]) <= {"pip", "setuptools", "wheel"}
        journal.run("pip --version", [python, "-m", "pip", "--version"], env=env)
        journal.run("pip config list", [python, "-m", "pip", "config", "list"], env=env)
        install = journal.run("pip install -r requirements.txt",
                              [python, "-m", "pip", "install", "--no-cache-dir", "-r", "requirements.txt"], env=env)
        if _index_unreachable(install):
            summary["status"] = "skipped: index de paquets injoignable"
            pytest.skip("index de paquets injoignable : preuve d'installation impossible ici (journal conservé)")
        assert install.returncode == 0, install.stderr[-3000:]
        check = journal.run("pip check", [python, "-m", "pip", "check"], env=env)
        assert check.returncode == 0 and "No broken requirements found" in check.stdout, check.stdout
        freeze = journal.run("pip freeze --all", [python, "-m", "pip", "freeze", "--all"], env=env)
        assert freeze.returncode == 0
        installed = {k: v for k, v in _freeze(freeze.stdout).items() if k not in ("pip", "setuptools", "wheel")}
        summary["installed"] = installed
        assert installed == PRODUCTION_LOCK  # exactement le verrou de production : ni plus, ni moins
        probe_env = {k: v for k, v in env.items() if k not in _TRANSPORT}  # sonde : aucun accès réseau
        probe = journal.run("sonde de production (python -I)", [python, "-I", "tests/f04b_production_probe.py"],
                            env=probe_env, timeout=300)
        assert probe.returncode == 0, probe.stderr[-3000:]
        report = json.loads(probe.stdout.strip().splitlines()[-1])
        summary["probe"] = report
        assert report["python"] == _runtime_version() and report["isolated_flag"] == 1
        assert report["prefix"] == str(venv) and report["prefix"] != report["base_prefix"]
        assert report["dev_tools_importable"] == []
        assert report["network_attempts"] == []
        components = report["components"]
        assert components["engine_driver"] == "psycopg2" and components["engine_checked_out"] == 0
        assert components["supabase_client"] == "httpx.Client" and components["httpx_version"] == "0.28.1"
        assert components["claude_max_retries"] == 0
        assert components["isolated_session_trust_env"] is False and components["isolated_session_type"] is True
        assert (components["auth_pairs"], components["personal_pairs"]) == (14, 13)
        assert components["gate"] == "RoutePolicyGate"
        assert components["alembic_head"] == "0021_decryptage_reservations"
        assert report["close"]["closed"] == ["claude", "fmp", "eodhd", "resend", "supabase", "engine"]
        summary["status"] = "success"
    except pytest.skip.Exception:
        raise
    except BaseException:
        summary["status"] = "failure"
        raise
    finally:
        journal.close(summary)
