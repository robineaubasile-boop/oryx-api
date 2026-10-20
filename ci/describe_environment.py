"""Consigne l'environnement réellement exécuté par un job CI (JSON + journal).

    python ci/describe_environment.py --out reports/environment.json

Commit testé (HEAD du checkout et GITHUB_SHA), interpréteur, distributions
installées et système. Aucune valeur de variable d'environnement n'est
copiée, hormis les métadonnées GitHub non secrètes listées ci-dessous.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
from importlib import metadata

GITHUB_METADATA = (
    "GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
    "GITHUB_JOB", "GITHUB_WORKFLOW", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion",
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def describe() -> dict:
    return {
        "git_head": _git("rev-parse", "HEAD"),
        "git_is_shallow": _git("rev-parse", "--is-shallow-repository"),
        "github": {name: os.environ.get(name) for name in GITHUB_METADATA},
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": platform.platform(),
        "distributions": sorted(
            f"{dist.metadata['Name'].lower()}=={dist.version}" for dist in metadata.distributions()
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = describe()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
