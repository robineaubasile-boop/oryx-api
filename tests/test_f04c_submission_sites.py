"""ACT-L1 / F04-C V0.3 — les SEPT points de soumission (freeze § 5), contrôlés
nominativement et statiquement : chacun passe par le soumetteur injecté, le
chemin historique run_in_threadpool n'est atteint que sans injection, aucun
huitième site n'existe dans les modules HTTP F2. La preuve dynamique (chaque
site observé à travers le vrai point d'entrée) est
tests/test_f04c_lifecycle.py::test_q1_01_normal_journey_uses_the_registered_submitter_at_the_seven_sites.
"""
import ast

from tests.test_migration_0002_analysis_sessions import REPO_ROOT

SITES = {
    # fichier : [(fonction englobante, forme exacte de l'appelé)]
    "core/auth_f2_http/routes.py": [("call", "run_in_threadpool if deps.submit is None else deps.submit")],
    "core/authz_f2_http/routes.py": [("me", "run_in_threadpool if deps.submit is None else deps.submit"),
                                     ("me_level", "run_in_threadpool if deps.submit is None else deps.submit")],
    "core/authz_f2_http/portfolio.py": [("run", "run_in_threadpool if deps.submit is None else deps.submit")],
    "core/authz_f2_http/consultation.py": [("run", "run_in_threadpool if deps.submit is None else deps.submit")],
    "core/authz_f2_http/decryptage.py": [("_submit", "run_in_threadpool if submit is None else submit"),
                                         ("single", "run_in_threadpool if deps.submit is None else deps.submit")],
}
HTTP_MODULES = sorted((REPO_ROOT / "core" / "auth_f2_http").glob("*.py")) + sorted(
    (REPO_ROOT / "core" / "authz_f2_http").glob("*.py"))


def _enclosing(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _function_of(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def test_exactly_seven_submission_sites_all_route_through_the_injected_submitter():
    found = {}
    for path in HTTP_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _enclosing(tree)
        relative = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "run_in_threadpool" and isinstance(node.ctx, ast.Load):
                expression = parents[node]
                assert isinstance(expression, ast.IfExp), (relative, ast.unparse(expression))  # jamais appelé seul
                call = parents[expression]
                assert isinstance(call, ast.Call) and call.func is expression, relative
                assert isinstance(parents[call], ast.Await), relative
                found.setdefault(relative, []).append((_function_of(node, parents), ast.unparse(expression)))
    assert found == SITES
    assert sum(len(v) for v in found.values()) == 7


def test_the_c3_turn_handler_still_submits_through_the_replaceable_submit_point():
    tree = ast.parse((REPO_ROOT / "core" / "authz_f2_http" / "decryptage.py").read_text(encoding="utf-8"))
    turns = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "turns")
    source = ast.unparse(turns)
    assert "reply = await _submit(work) if deps.submit is None else await _submit(work, deps.submit)" in source
    work = next(n for n in ast.walk(turns) if isinstance(n, ast.FunctionDef) and n.name == "work")
    assert ast.unparse(work.body[0]).startswith("if not slot.start():")  # garde C3 AVANT B1, inchangée


def test_dependencies_default_to_the_historical_path():
    from core.auth_f2_http.routes import Dependencies
    from core.authz_f2_http.routes import PersonalDependencies

    assert Dependencies.__dataclass_fields__["submit"].default is None
    assert PersonalDependencies.__dataclass_fields__["submit"].default is None
    assert list(Dependencies.__dataclass_fields__)[-1] == "submit"  # ajout en fin : positions historiques intactes
    assert list(PersonalDependencies.__dataclass_fields__)[-1] == "submit"
