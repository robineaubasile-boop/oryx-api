"""Tests de l'Étape 6.4C3 : rendu visible déterministe des jalons historiques
(core/progress_history_projection.py).

1. Contrats : versions, versions amont supportées, dataclasses minimales
   (aucun code de stade, transition, cause, plus haut stade, date, UUID,
   compteur), API publique, erreurs.
2. Rendu : libellés de stade EXACTEMENT ceux de 6-4A ; les sept ensembles
   de causes ; langage non punitif ; ordre et nombre de jalons conservés.
3. Validation stricte de l'entrée et des versions.
4. Bout en bout pur : 6-4C1 (contrat) -> 6-4C2 -> 6-4C3 sur les exemples.
5. Contrat statique : pur, aucune base, aucun LLM, aucune trace de
   support, aucun branchement runtime.
"""
import ast
import dataclasses
import inspect
import itertools
import re

import pytest

from core import progress_history_projection as hp
from core import progress_projection
from core.inference_service import TRANSITION_CAUSES
from core.progress_history_projection import (
    PROGRESS_HISTORY_PROJECTION_POLICY_VERSION,
    PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION,
    REVISION_TEMPLATES,
    InvalidProgressHistoryProjectionInput,
    ProgressHistoryProjection,
    ProgressHistoryProjectionError,
    UnsupportedProgressHistoryProjectionVersion,
    VisibleProgressMilestone,
    project_progress_history,
)
from core.progress_milestones import (
    PROGRESS_MILESTONES_POLICY_VERSION,
    PROGRESS_MILESTONES_SCHEMA_VERSION,
    REVISION_CAUSE_ORDER,
    ProgressMilestone,
    ProgressMilestones,
    project_progress_milestones,
)
from core.progress_projection import VISIBLE_STAGE_LABELS
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_progress_milestones import EXAMPLES, history

MODULE_PATH = REPO_ROOT / "core" / "progress_history_projection.py"
NEW, INTEGRITY, REINTERPRETATION = "new_user_evidence", "evidence_integrity_change", "pedagogical_reinterpretation"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STAGE_LABELS = {
    "discovery": "Notion reconnue",
    "comprehension": "Mécanisme compris",
    "application": "Utilisé en situation",
    "mastery": "Raisonnement solide dans des contextes variés",
}
EXPECTED_TEMPLATES = {
    (NEW,): ("Nouvelle évaluation", "De nouveaux éléments observés ont conduit Oryx à réviser son estimation."),
    (INTEGRITY,): ("Ajustement Oryx",
                   "Une correction des éléments disponibles a conduit Oryx à ajuster son estimation."),
    (REINTERPRETATION,): ("Réévaluation Oryx", "Une réinterprétation pédagogique des éléments disponibles a"
                                               " conduit Oryx à ajuster son estimation."),
    (NEW, INTEGRITY): ("Ajustement de l’estimation", "Des corrections des éléments disponibles et de nouveaux"
                                                     " éléments observés ont conduit Oryx à ajuster son"
                                                     " estimation."),
    (NEW, REINTERPRETATION): ("Réévaluation de l’estimation", "De nouveaux éléments observés et une"
                                                              " réinterprétation pédagogique ont conduit Oryx à"
                                                              " ajuster son estimation."),
    (INTEGRITY, REINTERPRETATION): ("Réévaluation de l’estimation", "Des corrections des éléments disponibles et"
                                                                    " une réinterprétation pédagogique ont conduit"
                                                                    " Oryx à ajuster son estimation."),
    (NEW, INTEGRITY, REINTERPRETATION): ("Ajustement de l’estimation", "Plusieurs évolutions des éléments"
                                                                       " disponibles et de leur interprétation ont"
                                                                       " conduit Oryx à ajuster son estimation."),
}
PUNITIVE = ("perte", "perdu", "régression", "regression", "baisse", "retour en arrière", "niveau", "downgrade",
            "échec", "faible", "recul", "dégrad", "moins bon", "erreur de l'utilisateur", "insuffisant")


def stage(code):
    return ProgressMilestone(milestone_kind="stage_established", stage_code=code, revision_causes=())


def revision(*causes):
    return ProgressMilestone(milestone_kind="revision", stage_code=None, revision_causes=tuple(causes))


def milestones(*items, code="C7"):
    return ProgressMilestones(schema_version=PROGRESS_MILESTONES_SCHEMA_VERSION,
                              policy_version=PROGRESS_MILESTONES_POLICY_VERSION, competency_code=code,
                              milestones=tuple(items))


def rendered(*items):
    return [(m.milestone_kind, m.title, m.detail) for m in project_progress_history(milestones=milestones(*items))
            .milestones]


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_explicit_and_bound_to_the_supported_upstream():
    assert PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION == "progress-history-projection-v1"
    assert PROGRESS_HISTORY_PROJECTION_POLICY_VERSION == "progress-history-projection-policy-1"
    assert (hp.SUPPORTED_PROGRESS_MILESTONES_SCHEMA_VERSION, hp.SUPPORTED_PROGRESS_MILESTONES_POLICY_VERSION) == (
        PROGRESS_MILESTONES_SCHEMA_VERSION, PROGRESS_MILESTONES_POLICY_VERSION)
    assert hp.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION == progress_projection.CURRENT_PROGRESS_POLICY_VERSION


def test_visible_contract_is_minimal():
    """#82 : ni code de stade, ni transition, ni cause, ni plus haut stade,
    ni date, ni UUID, ni compteur, ni jeton technique."""
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(VisibleProgressMilestone) == ["milestone_kind", "title", "detail"]
    assert names(ProgressHistoryProjection) == ["schema_version", "policy_version", "competency_code", "milestones"]
    for cls in (VisibleProgressMilestone, ProgressHistoryProjection):
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls)), cls
        for field in ("raw_stage", "stage_code", "current_stage", "previous_stage", "transition",
                      "transition_cause", "revision_causes", "highest_stage", "peak_stage", "best_stage",
                      "record_stage", "from_stage", "to_stage", "delta", "previous_stage_label",
                      "resulting_stage_label", "date", "created_at", "completed_at", "token", "history_token",
                      "count", "revision_count", "number_of_milestones", "score", "percent", "rank", "level"):
            assert field not in names(cls), (cls, field)


def test_errors_are_a_dedicated_business_hierarchy():
    errors = {o for o in vars(hp).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == hp.__name__}
    assert errors == {ProgressHistoryProjectionError, InvalidProgressHistoryProjectionInput,
                      UnsupportedProgressHistoryProjectionVersion}
    assert ProgressHistoryProjectionError.__bases__ == (Exception,)
    for error in errors - {ProgressHistoryProjectionError}:
        assert error.__bases__ == (ProgressHistoryProjectionError,)


def test_public_api_is_exactly_project_progress_history():
    functions = {n.name for n in _tree().body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == {"project_progress_history"}
    params = inspect.signature(project_progress_history).parameters
    assert list(params) == ["milestones"] and params["milestones"].kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------
# 2. Rendu
# --------------------------------------------------------------------------

def test_stage_labels_are_exactly_those_of_6_4a():
    """#79 : source canonique importée, jamais recopiée."""
    assert hp.VISIBLE_STAGE_LABELS is progress_projection.VISIBLE_STAGE_LABELS
    for code, label in STAGE_LABELS.items():
        assert VISIBLE_STAGE_LABELS[code] == label
        assert rendered(stage(code)) == [("stage_established", label, None)]
    assert "Notion reconnue" not in _source() and "Mécanisme compris" not in _source()


def test_templates_cover_exactly_the_seven_non_empty_cause_sets():
    combinations = [tuple(c for c in REVISION_CAUSE_ORDER if c in subset)
                    for size in (1, 2, 3) for subset in itertools.combinations(REVISION_CAUSE_ORDER, size)]
    assert len(combinations) == 7 and set(REVISION_TEMPLATES) == set(combinations)
    assert set().union(*map(set, REVISION_TEMPLATES)) == TRANSITION_CAUSES
    assert dict(REVISION_TEMPLATES) == EXPECTED_TEMPLATES
    with pytest.raises(TypeError):
        REVISION_TEMPLATES[(NEW,)] = ("x", "y")


@pytest.mark.parametrize("causes", sorted(EXPECTED_TEMPLATES, key=lambda c: (len(c), c)))
def test_each_cause_combination_renders_deterministically(causes):
    """#80."""
    title, detail = EXPECTED_TEMPLATES[causes]
    first = rendered(stage("mastery"), revision(*causes))
    assert first == [("stage_established", STAGE_LABELS["mastery"], None), ("revision", title, detail)]
    assert rendered(stage("mastery"), revision(*causes)) == first


def test_integrity_correction_is_an_oryx_adjustment():
    (title, detail), = [EXPECTED_TEMPLATES[(INTEGRITY,)]]
    assert "Oryx" in title and "Oryx" in detail and "ajuster son estimation" in detail


def test_no_punitive_language_in_any_template():
    """#81 / #35 : la révision parle de l'estimation d'Oryx, jamais de la
    personne."""
    texts = [text for pair in REVISION_TEMPLATES.values() for text in pair] + list(VISIBLE_STAGE_LABELS.values())
    for text in texts:
        lowered = text.lower()
        for word in PUNITIVE:
            assert word not in lowered, (word, text)
        assert "%" not in text and not re.search(r"\d", text), text
    for title, detail in REVISION_TEMPLATES.values():
        assert "estimation" in detail and "Oryx" in detail
        assert "\n" not in title + detail and title.strip() == title and detail.endswith(".")
        for word in ("vous", "tu ", "utilisateur", "compétence"):
            assert word not in (title + " " + detail).lower(), word


def test_order_and_number_of_milestones_are_preserved():
    items = (stage("discovery"), stage("comprehension"), stage("application"), stage("mastery"),
             revision(NEW, REINTERPRETATION))
    result = project_progress_history(milestones=milestones(*items))
    assert [m.milestone_kind for m in result.milestones] == ["stage_established"] * 4 + ["revision"]
    assert [m.title for m in result.milestones][:4] == [STAGE_LABELS[s] for s in
                                                        ("discovery", "comprehension", "application", "mastery")]
    assert result == ProgressHistoryProjection(
        schema_version="progress-history-projection-v1", policy_version="progress-history-projection-policy-1",
        competency_code="C7", milestones=result.milestones)


def test_empty_milestones_render_an_empty_projection():
    result = project_progress_history(milestones=milestones())
    assert (result.competency_code, result.milestones) == ("C7", ())


def test_no_raw_code_or_identifier_is_visible():
    for causes in EXPECTED_TEMPLATES:
        result = project_progress_history(milestones=milestones(stage("mastery"), stage("application"),
                                                                revision(*causes)))
        text = repr(result)
        for raw in ("discovery", "comprehension", "application", "mastery", "non_etabli", "revised_down",
                    "upgraded", "maintained", *TRANSITION_CAUSES, "history_"):
            assert raw not in text, raw
        assert not UUID_PATTERN.search(text)


# --------------------------------------------------------------------------
# 3. Validation stricte
# --------------------------------------------------------------------------

@pytest.mark.parametrize("build", [
    lambda: None,
    lambda: dataclasses.asdict(milestones(stage("discovery"))),
    lambda: milestones(code="C0"),
    lambda: dataclasses.replace(milestones(), milestones=[stage("discovery")]),
    lambda: milestones({"milestone_kind": "revision"}),
    lambda: milestones(ProgressMilestone(milestone_kind="achievement", stage_code="mastery", revision_causes=())),
    lambda: milestones(stage("non_etabli")),
    lambda: milestones(stage("expert")),
    lambda: milestones(stage(None)),
    lambda: milestones(stage("discovery"), stage("discovery")),
    lambda: milestones(ProgressMilestone(milestone_kind="stage_established", stage_code="mastery",
                                         revision_causes=(NEW,))),
    lambda: milestones(ProgressMilestone(milestone_kind="revision", stage_code="mastery", revision_causes=(NEW,))),
    lambda: milestones(revision()),
    lambda: milestones(revision(NEW, NEW)),
    lambda: milestones(revision(INTEGRITY, NEW)),
    lambda: milestones(revision("user_weakness")),
    lambda: milestones(revision(["new_user_evidence"])),
    lambda: milestones(ProgressMilestone(milestone_kind="revision", stage_code=None, revision_causes=[NEW])),
    lambda: milestones(revision(NEW), stage("mastery")),
    lambda: milestones(revision(NEW), revision(INTEGRITY)),
])
def test_malformed_milestones_are_rejected(build):
    with pytest.raises(InvalidProgressHistoryProjectionInput):
        project_progress_history(milestones=build())


@pytest.mark.parametrize("field", ["schema_version", "policy_version"])
def test_unsupported_input_version(field):
    with pytest.raises(UnsupportedProgressHistoryProjectionVersion):
        project_progress_history(milestones=dataclasses.replace(milestones(), **{field: "v999"}))


@pytest.mark.parametrize("name", ["PROGRESS_MILESTONES_SCHEMA_VERSION", "PROGRESS_MILESTONES_POLICY_VERSION",
                                  "CURRENT_PROGRESS_POLICY_VERSION"])
def test_unsupported_upstream_module_version(monkeypatch, name):
    monkeypatch.setattr(hp, name, "v999")
    with pytest.raises(UnsupportedProgressHistoryProjectionVersion):
        project_progress_history(milestones=milestones())


# --------------------------------------------------------------------------
# 4. Bout en bout pur : exemples obligatoires
# --------------------------------------------------------------------------

EXPECTED_VISIBLE = {
    "A": [("stage_established", "Notion reconnue", None), ("stage_established", "Mécanisme compris", None)],
    "B": [],
    "C": [("stage_established", "Notion reconnue", None), ("stage_established", "Mécanisme compris", None),
          ("stage_established", "Utilisé en situation", None),
          ("stage_established", "Raisonnement solide dans des contextes variés", None),
          ("revision", *EXPECTED_TEMPLATES[(REINTERPRETATION,)])],
    "D": [("stage_established", "Raisonnement solide dans des contextes variés", None),
          ("revision", *EXPECTED_TEMPLATES[(NEW, INTEGRITY)])],
    "E": [],
    "F": [("stage_established", "Utilisé en situation", None), ("revision", *EXPECTED_TEMPLATES[(INTEGRITY,)])],
    "G": [],
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_mandatory_examples_end_to_end(name):
    steps, _ = EXAMPLES[name]
    result = project_progress_history(milestones=project_progress_milestones(history=history(*steps)))
    assert [(m.milestone_kind, m.title, m.detail) for m in result.milestones] == EXPECTED_VISIBLE[name]
    assert result == project_progress_history(milestones=project_progress_milestones(history=history(*steps)))


# --------------------------------------------------------------------------
# 5. Contrat statique
# --------------------------------------------------------------------------

def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    by_module = {}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                by_module.setdefault(alias.name, set())
    return by_module


def test_imports_are_vocabularies_and_public_contracts_only():
    assert _imports() == {
        "dataclasses": {"dataclass"},
        "types": {"MappingProxyType"},
        "core.inference_service": {"CLAIM_STAGES", "COMPETENCY_CODES", "EVIDENCE_INTEGRITY_CHANGE",
                                   "NEW_USER_EVIDENCE", "PEDAGOGICAL_REINTERPRETATION"},
        "core.progress_milestones": {
            "MILESTONE_KINDS", "MILESTONE_REVISION", "MILESTONE_STAGE_ESTABLISHED",
            "PROGRESS_MILESTONES_POLICY_VERSION", "PROGRESS_MILESTONES_SCHEMA_VERSION", "REVISION_CAUSE_ORDER",
            "ProgressMilestone", "ProgressMilestones"},
        "core.progress_projection": {"CURRENT_PROGRESS_POLICY_VERSION", "VISIBLE_STAGE_LABELS"},
    }
    assert all(name.isupper() for name in _imports()["core.inference_service"])


def test_no_database_no_session_no_orm():
    names = set(_code_tokens(_source()).split()) | {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("db", "Session", "session", "execute", "select", "query", "add", "flush", "commit",
                      "no_autoflush", "acquire_progress_history", "project_progress_milestones",
                      "get_inference_lineage", "project_current_progress"):
        assert forbidden not in names, forbidden
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "alembic", "psycopg2",
                "core.progress_history"} & set(_imports())


def test_no_llm_clock_randomness_or_support_trace():
    """#83 / #86 : aucun modèle, classifier ni prompt ; « généré » n'est pas
    « montré » : aucune trace de support."""
    assert not {"anthropic", "openai", "requests", "httpx", "random", "secrets", "datetime", "time", "os",
                "uuid", "json"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "backend", "renderer", "proposal",
                 "support_trace", "supporttrace", "environ", "uuid4"):
        assert word not in tokens, word
    assert not {"llm", "model", "now", "today"} & set(tokens.split())


def test_no_score_count_date_or_bound():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "percent", "rank", "points", "xp", "speed", "days", "duration", "elapsed",
                 "date", "highest", "peak", "record", "best", "recommend", "next"):
        assert word not in parts, word
    assert "MAX_" not in _source()


def test_not_wired_to_the_runtime():
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("project_progress_history", "ProgressHistoryProjection", "VisibleProgressMilestone"):
        assert name not in api, name
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_history_projection.py" and "progress_history_projection" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    assert users == []
