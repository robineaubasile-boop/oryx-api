"""Contrôle strict d'un rapport JUnit produit par la CI, puis résumé JSON.

    python ci/check_test_report.py --kind pytest --leg postgres-16 \\
        --junit reports/junit.xml --log reports/pytest.log --exit-code 0 \\
        --commit "$GITHUB_SHA" --out results/postgres-16.json

Un rapport n'est accepté que si :

- le code de sortie de l'exécuteur de tests vaut 0 ;
- le JUnit est lisible et contient au moins un test ;
- aucun cas n'est en échec, en erreur ou sauté (un xfail apparaît comme
  sauté dans le JUnit de pytest : il est donc refusé aussi) ;
- pour pytest, la ligne de résumé finale ne mentionne que « passed » (et
  éventuellement « warnings ») : ni « deselected » (filtrage -k / -m), ni
  « xfailed » / « xpassed », ni « skipped », ni « error » ;
- les totaux déclarés par les éléments <testsuite> concordent avec les
  éléments <testcase> réellement présents.

Le résumé JSON (--out) est écrit dans tous les cas, avec "status" à
"success" ou "failure" : l'agrégateur ci / required le relit.
"""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

# « ===== 12 passed, 1 warning in 3.21s ===== » (ou sans les « = » en -q).
PYTEST_SUMMARY = re.compile(r"^(?:=+ )?(?P<body>[0-9]+ [a-z]+(?:, [0-9]+ [a-z]+)*) in [0-9.]+s(?: \([0-9:]+\))?(?: =+)?$")
PYTEST_ALLOWED_OUTCOMES = {"passed", "warning", "warnings"}


def count_cases(root) -> dict:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for case in root.iter("testcase"):
        counts["tests"] += 1
        if case.find("failure") is not None:
            counts["failures"] += 1
        if case.find("error") is not None:
            counts["errors"] += 1
        if case.find("skipped") is not None:
            counts["skipped"] += 1
    return counts


def declared_totals(root) -> dict:
    """Totaux déclarés par les <testsuite> de premier niveau (pytest)."""
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
    return totals


def pytest_summary_outcomes(log_text: str) -> dict:
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    for line in reversed(lines):
        match = PYTEST_SUMMARY.match(line)
        if match:
            outcomes = {}
            for part in match.group("body").split(","):
                number, _, label = part.strip().partition(" ")
                if not number.isdigit():
                    raise ValueError(f"ligne de résumé pytest illisible : {line!r}")
                outcomes[label] = int(number)
            return outcomes
    raise ValueError("ligne de résumé pytest introuvable")


def evaluate(kind: str, junit_path: str, log_path: str, exit_code: int) -> tuple:
    problems = []
    counts = {}
    if exit_code != 0:
        problems.append(f"code de sortie {exit_code}")
    try:
        root = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError) as exc:
        return {}, problems + [f"JUnit illisible : {exc.__class__.__name__}"]
    counts = count_cases(root)
    if counts["tests"] == 0:
        problems.append("aucun test exécuté")
    for key in ("failures", "errors", "skipped"):
        if counts[key]:
            problems.append(f"{counts[key]} test(s) {key}")
    if kind == "pytest":
        declared = declared_totals(root)
        if declared != counts:
            problems.append(f"totaux JUnit incohérents : déclarés {declared}, constatés {counts}")
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                outcomes = pytest_summary_outcomes(handle.read())
        except (OSError, ValueError) as exc:
            problems.append(str(exc))
        else:
            counts["summary"] = outcomes
            unexpected = sorted(set(outcomes) - PYTEST_ALLOWED_OUTCOMES)
            if unexpected:
                problems.append("résumé pytest non autorisé : " + ", ".join(
                    f"{outcomes[label]} {label}" for label in unexpected))
            if outcomes.get("passed") != counts["tests"]:
                problems.append(f"résumé pytest ({outcomes.get('passed')} passed) != JUnit ({counts['tests']})")
    return counts, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("pytest", "node"), required=True)
    parser.add_argument("--leg", required=True)
    parser.add_argument("--junit", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--extra", action="append", default=[],
                        help="fichier JSON à joindre au résumé (versions, base...)")
    args = parser.parse_args(argv)

    counts, problems = evaluate(args.kind, args.junit, args.log, args.exit_code)
    result = {
        "leg": args.leg,
        "kind": args.kind,
        "commit": args.commit,
        "status": "failure" if problems else "success",
        "counts": counts,
        "problems": problems,
        "extra": {},
    }
    for path in args.extra:
        try:
            with open(path, encoding="utf-8") as handle:
                result["extra"][os.path.basename(path)] = json.load(handle)
        except (OSError, ValueError):
            result["status"] = "failure"
            problems.append(f"fichier joint illisible : {os.path.basename(path)}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
    print(json.dumps({key: result[key] for key in ("leg", "status", "counts", "problems")},
                     indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
