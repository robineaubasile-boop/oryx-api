"""Agrégateur du contrôle obligatoire « ci / required ».

    NEEDS_JSON='${{ toJSON(needs) }}' python ci/verify_required.py \\
        --results results --commit "$GITHUB_SHA"

Le job required s'exécute toujours (if: always()), même quand un job dont il
dépend a échoué, a été annulé ou sauté. Il n'accepte que :

1. exactement les jobs attendus dans `needs` (ni manquant, ni en trop), tous
   avec le résultat "success" — failure, cancelled, skipped, une valeur vide
   ou inconnue sont refusés ;
2. exactement un résumé par branche attendue (postgres-16, postgres-18,
   migrations-16, migrations-18, web), produit pour le commit testé, au
   statut "success", cohérent avec sa branche (version PostgreSQL réellement
   exécutée, aucun test en échec, en erreur ou sauté).

Un job de matrice ne remonte qu'un résultat global dans `needs` : le point 2
garantit en plus que chaque version PostgreSQL a bien été exécutée.
Aucun résultat rouge, absent ou inconnu ne peut produire un succès.
"""
import argparse
import json
import os
import sys
from pathlib import Path

EXPECTED_JOBS = ("postgres", "migrations", "web")
EXPECTED_LEGS = {
    "postgres-16": ("pytest", 16),
    "postgres-18": ("pytest", 18),
    "migrations-16": ("migrations", 16),
    "migrations-18": ("migrations", 18),
    "web": ("node", None),
}


def check_needs(needs) -> list:
    problems = []
    if not isinstance(needs, dict):
        return ["`needs` illisible"]
    for job in EXPECTED_JOBS:
        if job not in needs:
            problems.append(f"job {job} : manquant")
            continue
        entry = needs[job]
        result = entry.get("result") if isinstance(entry, dict) else None
        if result != "success":
            problems.append(f"job {job} : résultat {result!r} refusé (seul 'success' est accepté)")
    for job in sorted(set(needs) - set(EXPECTED_JOBS)):
        problems.append(f"job {job} : inattendu")
    return problems


def _counts_ok(counts) -> bool:
    return (isinstance(counts, dict) and isinstance(counts.get("tests"), int) and counts["tests"] > 0
            and counts.get("failures") == 0 and counts.get("errors") == 0 and counts.get("skipped") == 0)


def check_leg(leg: str, result, commit: str) -> list:
    kind, major = EXPECTED_LEGS[leg]
    if not isinstance(result, dict):
        return [f"{leg} : résumé illisible"]
    problems = []
    if result.get("leg") != leg:
        problems.append(f"{leg} : résumé d'une autre branche ({result.get('leg')!r})")
    if result.get("kind") != kind:
        problems.append(f"{leg} : type {result.get('kind')!r} au lieu de {kind!r}")
    if result.get("commit") != commit:
        problems.append(f"{leg} : commit {result.get('commit')!r} au lieu de {commit!r}")
    if result.get("status") != "success":
        problems.append(f"{leg} : statut {result.get('status')!r}")
    if kind in ("pytest", "node") and not _counts_ok(result.get("counts")):
        problems.append(f"{leg} : compteurs de tests refusés {result.get('counts')!r}")
    if kind == "pytest":
        server = (result.get("extra") or {}).get("postgres.json") or {}
        version_num = str(server.get("server_version_num", ""))
        if not version_num.isdigit() or int(version_num) // 10000 != major:
            problems.append(f"{leg} : PostgreSQL {server.get('server_version')!r} au lieu de {major}")
    if kind == "migrations":
        version_num = result.get("server_version_num")
        if not isinstance(version_num, int) or version_num // 10000 != major:
            problems.append(f"{leg} : PostgreSQL {version_num!r} au lieu de {major}")
        if not result.get("head") or not result.get("checks"):
            problems.append(f"{leg} : contrôles Alembic absents")
        elif any(check.get("status") != "passed" for check in result["checks"]):
            problems.append(f"{leg} : contrôle Alembic non réussi")
    return problems


def check_results(results_dir: Path, commit: str) -> list:
    problems = []
    found = {}
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*/result.json")):
            leg = path.parent.name.removeprefix("result-")
            try:
                found[leg] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                found[leg] = None
    for leg in EXPECTED_LEGS:
        if leg not in found:
            problems.append(f"{leg} : résumé manquant")
        else:
            problems.extend(check_leg(leg, found[leg], commit))
    for leg in sorted(set(found) - set(EXPECTED_LEGS)):
        problems.append(f"{leg} : résumé inattendu")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        needs = json.loads(os.environ.get("NEEDS_JSON", ""))
    except ValueError:
        needs = None
    problems = check_needs(needs) + check_results(Path(args.results), args.commit)
    if isinstance(needs, dict):
        for job, entry in sorted(needs.items()):
            print(f"needs.{job}.result = {entry.get('result') if isinstance(entry, dict) else entry!r}")
    if problems:
        print("ci / required : REFUSÉ", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"ci / required : les {len(EXPECTED_JOBS)} jobs et {len(EXPECTED_LEGS)} branches attendus "
          f"ont réussi pour {args.commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
