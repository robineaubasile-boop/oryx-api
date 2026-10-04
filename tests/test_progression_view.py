"""Tests de l'Étape 6.4D : vue générale Progression (core/progression_view.py).

1. Contrats : versions, dataclasses immuables et exactes, API publique,
   hiérarchie d'erreurs.
2. Port Academy : bornes, types exacts, total jamais figé, aucun ratio,
   aucune compétence, aucun module courant / suivant.
3. Port Rallye : bornes, types exacts, maximum jamais figé, libellé
   mono-ligne, aucun ratio, aucune compétence, aucun diagnostic.
4. Douze cartes 6-4A exactes, C1 -> C12, réutilisées telles quelles.
5. Indépendance : aucune correction croisée Academy / Rallye / C1-C12.
6. Exclusions : aucune synthèse, aucun score global.
7. Pureté et contrat statique (aucune base, aucun modèle, aucune écriture,
   aucune trace de support, aucun branchement runtime, aucun propriétaire
   Academy / Rallye).
"""
import ast
import dataclasses
import inspect
import re

import pytest

from core import progression_view as view
from core.progress_projection import CompetencyCurrentProgress, CurrentProgressProjection, project_current_progress
from core.progression_view import (
    ACADEMY_PROGRESS_SUMMARY_POLICY_VERSION,
    ACADEMY_PROGRESS_SUMMARY_SCHEMA_VERSION,
    PROGRESSION_VIEW_POLICY_VERSION,
    PROGRESSION_VIEW_SCHEMA_VERSION,
    RALLYE_SESSION_SUMMARY_POLICY_VERSION,
    RALLYE_SESSION_SUMMARY_SCHEMA_VERSION,
    AcademyProgressSummary,
    InvalidProgressionViewInput,
    ProgressionView,
    ProgressionViewError,
    RallyeSessionSummary,
    UnsupportedProgressionViewVersion,
    compose_progression_view,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_progress_projection import card, d, snap, state

MODULE_PATH = REPO_ROOT / "core" / "progression_view.py"
CODES = tuple(f"C{i}" for i in range(1, 13))


def academy(completed=7, total=12, **changes):
    return AcademyProgressSummary(**{"schema_version": ACADEMY_PROGRESS_SUMMARY_SCHEMA_VERSION,
                                     "policy_version": ACADEMY_PROGRESS_SUMMARY_POLICY_VERSION,
                                     "completed_modules": completed, "total_modules": total, **changes})


def rallye(value=5, maximum=6, label="Rallye du 3 octobre", **changes):
    return RallyeSessionSummary(**{"schema_version": RALLYE_SESSION_SUMMARY_SCHEMA_VERSION,
                                   "policy_version": RALLYE_SESSION_SUMMARY_POLICY_VERSION,
                                   "session_label": label, "score_value": value, "score_max": maximum, **changes})


def ladder(code, *stages):
    ids = (d(f"{code}_A"), d(f"{code}_B"))
    return {stage: (ids, False) for stage in stages}


def c5(stage):
    if stage == "non_etabli":
        return snap("C5", "non_etabli", {})
    reached = ("discovery", "comprehension", "application", "mastery")
    return snap("C5", stage, ladder("C5", *reached[:reached.index(stage) + 1]))


def current(*snapshots):
    return project_current_progress(state=state(*snapshots))


def compose(current_=None, **kwargs):
    return compose_progression_view(current=current_ if current_ is not None else current(), **kwargs)


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_exact():
    assert PROGRESSION_VIEW_SCHEMA_VERSION == "progression-view-v1"
    assert PROGRESSION_VIEW_POLICY_VERSION == "progression-view-policy-1"
    assert ACADEMY_PROGRESS_SUMMARY_SCHEMA_VERSION == "academy-progress-summary-v1"
    assert ACADEMY_PROGRESS_SUMMARY_POLICY_VERSION == "academy-progress-summary-policy-1"
    assert RALLYE_SESSION_SUMMARY_SCHEMA_VERSION == "rallye-session-summary-v1"
    assert RALLYE_SESSION_SUMMARY_POLICY_VERSION == "rallye-session-summary-policy-1"
    assert view.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION == "current-progress-projection-v1"
    assert view.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION == "current-progress-policy-1"


def _fields(cls):
    return [(f.name, f.type) for f in dataclasses.fields(cls)]


def test_contracts_are_exact():
    assert _fields(AcademyProgressSummary) == [("schema_version", str), ("policy_version", str),
                                               ("completed_modules", int), ("total_modules", int)]
    assert _fields(RallyeSessionSummary) == [("schema_version", str), ("policy_version", str),
                                             ("session_label", str), ("score_value", int), ("score_max", int)]
    assert _fields(ProgressionView) == [
        ("schema_version", str), ("policy_version", str), ("academy", AcademyProgressSummary | None),
        ("rallye", RallyeSessionSummary | None), ("competencies", tuple[CompetencyCurrentProgress, ...])]


def test_dataclasses_are_frozen_and_keyword_only():
    result = compose(academy=academy(), rallye=rallye())
    for obj, field in ((result, "academy"), (result.academy, "total_modules"), (result.rallye, "score_max")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, None)
    for cls in (AcademyProgressSummary, RallyeSessionSummary, ProgressionView):
        with pytest.raises(TypeError):
            cls("positional")


def test_public_api_and_signature():
    public = {name for name, obj in inspect.getmembers(view, inspect.isfunction)
              if obj.__module__ == view.__name__ and not name.startswith("_")}
    assert public == {"compose_progression_view"}
    parameters = inspect.signature(compose_progression_view).parameters.values()
    assert [(p.name, p.kind, p.default) for p in parameters] == [
        ("current", inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.empty),
        ("academy", inspect.Parameter.KEYWORD_ONLY, None),
        ("rallye", inspect.Parameter.KEYWORD_ONLY, None)]


def test_errors_are_a_dedicated_hierarchy():
    for error in (InvalidProgressionViewInput, UnsupportedProgressionViewVersion):
        assert issubclass(error, ProgressionViewError)
    assert not issubclass(ProgressionViewError, (ValueError, TypeError))


# --------------------------------------------------------------------------
# 2. Academy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("completed,total", [(7, 12), (12, 12), (0, 12), (0, 1), (1, 1), (3, 40)])
def test_valid_academy_summaries_are_kept_as_is(completed, total):
    summary = academy(completed, total)
    assert compose(academy=summary).academy is summary


@pytest.mark.parametrize("completed,total", [(-1, 12), (13, 12), (0, 0), (1, 0), (0, -1), (True, 12), (7, True),
                                             (7.0, 12), (7, 12.0), ("7", 12), (None, 12)])
def test_invalid_academy_summaries_fail_closed(completed, total):
    with pytest.raises(InvalidProgressionViewInput):
        compose(academy=academy(completed, total))


def test_academy_total_is_never_hardcoded():
    assert compose(academy=academy(5, 9)).academy.total_modules == 9
    assert _int_constants() <= {0, 1}


@pytest.mark.parametrize("changes", [{"schema_version": "academy-progress-summary-v2"},
                                     {"policy_version": "academy-progress-summary-policy-2"}])
def test_academy_unknown_version_fails_closed(changes):
    with pytest.raises(UnsupportedProgressionViewVersion):
        compose(academy=academy(**changes))


def test_academy_wrong_type_fails_closed():
    with pytest.raises(InvalidProgressionViewInput):
        compose(academy={"completed_modules": 7, "total_modules": 12})
    with pytest.raises(InvalidProgressionViewInput):
        compose(academy=rallye())


def test_academy_has_no_percent_competency_or_next_module():
    names = {f.name for f in dataclasses.fields(AcademyProgressSummary)}
    for word in ("percent", "ratio", "progress", "completion", "competency", "stage", "skill", "capability",
                 "current_module", "next", "recommended", "level"):
        assert not any(word in name for name in names), word


# --------------------------------------------------------------------------
# 3. Rallye
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,maximum", [(5, 6), (6, 7), (7, 7), (0, 6), (0, 1), (12, 20)])
def test_valid_rallye_summaries_are_kept_as_is(value, maximum):
    summary = rallye(value, maximum)
    assert compose(rallye=summary).rallye is summary


@pytest.mark.parametrize("value,maximum", [(-1, 6), (7, 6), (0, 0), (0, -1), (True, 6), (5, True), (5.0, 6),
                                           (5, 6.0), ("5", 6), (None, 6)])
def test_invalid_rallye_scores_fail_closed(value, maximum):
    with pytest.raises(InvalidProgressionViewInput):
        compose(rallye=rallye(value, maximum))


@pytest.mark.parametrize("label", ["", "   ", "Rallye\x00", "Rallye\nsuite", "Rallye\rsuite", "a b", None, 7,
                                   b"Rallye"])
def test_invalid_rallye_labels_fail_closed(label):
    with pytest.raises(InvalidProgressionViewInput):
        compose(rallye=rallye(label=label))


def test_rallye_max_is_never_hardcoded():
    assert compose(rallye=rallye(3, 10)).rallye.score_max == 10
    assert _int_constants() <= {0, 1}
    tokens = _code_tokens(_source())
    assert "score_out_of_six" not in tokens and "MAX_RALLYE_SCORE" not in tokens and "MAX_" not in tokens


@pytest.mark.parametrize("changes", [{"schema_version": "rallye-session-summary-v2"},
                                     {"policy_version": "rallye-session-summary-policy-2"}])
def test_rallye_unknown_version_fails_closed(changes):
    with pytest.raises(UnsupportedProgressionViewVersion):
        compose(rallye=rallye(**changes))


def test_rallye_wrong_type_fails_closed():
    with pytest.raises(InvalidProgressionViewInput):
        compose(rallye=academy())


def test_rallye_has_no_percent_competency_or_diagnostic():
    names = {f.name for f in dataclasses.fields(RallyeSessionSummary)}
    for word in ("percent", "ratio", "normalized", "competency", "stage", "comfortable", "consolidate",
                 "concept", "weak", "recommend", "next", "level"):
        assert not any(word in name for name in names), word


# --------------------------------------------------------------------------
# 4. Douze cartes 6-4A
# --------------------------------------------------------------------------

def test_exactly_twelve_cards_c1_to_c12_reused_as_is():
    projection = current(c5("application"))
    result = compose(projection)
    assert tuple(c.competency_code for c in result.competencies) == CODES
    assert result.competencies is projection.competencies
    assert all(a is b for a, b in zip(result.competencies, projection.competencies))


def _with(projection, cards):
    return dataclasses.replace(projection, competencies=tuple(cards))


@pytest.mark.parametrize("change", ["swap", "duplicate", "missing_c12", "extra", "empty", "lexical", "reversed"])
def test_any_other_card_tuple_fails_closed(change):
    projection = current()
    cards = list(projection.competencies)
    if change == "swap":
        cards[0], cards[1] = cards[1], cards[0]
    elif change == "duplicate":
        cards[11] = cards[10]
    elif change == "missing_c12":
        cards = cards[:11]
    elif change == "extra":
        cards = cards + [cards[0]]
    elif change == "empty":
        cards = []
    elif change == "lexical":
        cards = sorted(cards, key=lambda c: c.competency_code)
    else:
        cards = cards[::-1]
    with pytest.raises(InvalidProgressionViewInput):
        compose(_with(projection, cards))


def test_cards_must_be_6_4a_cards_in_a_tuple():
    projection = current()
    with pytest.raises(InvalidProgressionViewInput):
        compose(dataclasses.replace(projection, competencies=list(projection.competencies)))
    forged = list(projection.competencies)
    forged[0] = dataclasses.asdict(forged[0])
    with pytest.raises(InvalidProgressionViewInput):
        compose(_with(projection, forged))
    with pytest.raises(InvalidProgressionViewInput):
        compose_progression_view(current=projection.competencies)


@pytest.mark.parametrize("changes", [{"schema_version": "current-progress-projection-v2"},
                                     {"policy_version": "current-progress-policy-2"}])
def test_unknown_6_4a_version_fails_closed(changes):
    with pytest.raises(UnsupportedProgressionViewVersion):
        compose(dataclasses.replace(current(), **changes))


def test_unknown_6_4a_module_version_fails_closed(monkeypatch):
    monkeypatch.setattr(view, "CURRENT_PROGRESS_SCHEMA_VERSION", "current-progress-projection-v2")
    with pytest.raises(UnsupportedProgressionViewVersion):
        compose()


def test_never_sorted_by_stage():
    projection = current(snap("C2", "mastery", ladder("C2", "discovery", "comprehension", "application", "mastery")),
                         c5("discovery"), snap("C9", "non_etabli", {}))
    assert tuple(c.competency_code for c in compose(projection).competencies) == CODES


# --------------------------------------------------------------------------
# 5. Indépendance : aucune correction croisée
# --------------------------------------------------------------------------

@pytest.mark.parametrize("academy_,rallye_,stage", [
    (academy(12, 12), rallye(6, 6), "non_etabli"),
    (None, None, "mastery"),
    (academy(5, 12), rallye(2, 6), "application"),
    (academy(0, 12), rallye(6, 6), "mastery"),
])
def test_cross_layer_combinations_are_valid_and_never_corrected(academy_, rallye_, stage):
    projection = current(c5(stage))
    result = compose(projection, academy=academy_, rallye=rallye_)
    assert result.academy is academy_ and result.rallye is rallye_
    assert card(result, "C5") is card(projection, "C5")
    assert card(result, "C5").stage_code == stage
    assert result.competencies == compose(projection).competencies


def test_academy_and_rallye_are_optional_and_none_means_not_provided():
    result = compose()
    assert (result.academy, result.rallye) == (None, None)
    assert compose(academy=None, rallye=None) == result


def test_academy_and_rallye_never_change_each_other():
    a, r = academy(3, 12), rallye(1, 6)
    assert compose(academy=a, rallye=r).academy is a
    assert compose(academy=a, rallye=r).rallye is r
    assert compose(academy=a).academy == compose(academy=a, rallye=r).academy


# --------------------------------------------------------------------------
# 6. Exclusions
# --------------------------------------------------------------------------

def test_view_has_no_summary_or_global_score():
    names = {f.name for cls in (ProgressionView, AcademyProgressSummary, RallyeSessionSummary)
             for f in dataclasses.fields(cls)}
    assert {f.name for f in dataclasses.fields(ProgressionView)} == {
        "schema_version", "policy_version", "academy", "rallye", "competencies"}
    for word in ("summary", "overall", "global", "level", "percent", "rank", "xp", "streak", "average", "badge",
                 "weakest", "strongest", "profile_label", "investor", "learning"):
        assert not any(word in name for name in names), word


# --------------------------------------------------------------------------
# 7. Pureté et contrat statique
# --------------------------------------------------------------------------

def test_same_input_same_output():
    projection = current(c5("application"))
    a, r = academy(), rallye()
    assert compose(projection, academy=a, rallye=r) == compose(projection, academy=a, rallye=r)


def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _int_constants():
    """Entiers littéraux du module (hors bool) : aucune borne figée."""
    return {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is int}


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


def test_imports_are_public_contracts_only():
    assert _imports() == {
        "dataclasses": {"dataclass"},
        "core.adaptation_state": {"COMPETENCY_ORDER"},
        "core.progress_projection": {"CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
                                     "CompetencyCurrentProgress", "CurrentProgressProjection"},
    }
    assert CurrentProgressProjection  # contrat 6-4A importé, jamais recopié


def test_no_database_no_session_no_write():
    names = set(_code_tokens(_source()).split()) | {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("db", "Session", "session", "execute", "select", "query", "add", "flush", "commit",
                      "rollback", "insert", "update", "delete", "project_current_progress", "load_adaptation_state"):
        assert forbidden not in names, forbidden
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "alembic", "psycopg2"} & set(_imports())


def test_no_llm_clock_randomness_or_support_trace():
    assert not {"anthropic", "openai", "requests", "httpx", "random", "secrets", "datetime", "time", "os",
                "uuid", "json"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "backend", "renderer", "proposal",
                 "support_trace", "supporttrace", "environ", "complete("):
        assert word not in tokens, word
    assert not {"llm", "model", "now", "today"} & set(tokens.split())


def test_no_score_computation_or_cross_conversion():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("percent", "ratio", "average", "overall", "rank", "xp", "streak", "weight", "badge", "next",
                 "recommend", "weakest", "strongest", "sorted", "sort"):
        assert word not in parts, word
    divisions = [n for n in ast.walk(_tree()) if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.Mult,
                                                                                             ast.FloorDiv, ast.Add))]
    assert divisions == []


def test_not_wired_to_the_runtime():
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("compose_progression_view", "ProgressionView", "AcademyProgressSummary", "RallyeSessionSummary"):
        assert name not in api, name
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel != "core/progression_view.py" and ("progression_view" in text or "AcademyProgressSummary" in text
                                                  or "RallyeSessionSummary" in text):
            users.append(rel)
    assert users == []


def test_no_academy_or_rallye_owner_model_or_migration():
    for name in ("academy_service.py", "rallye_service.py"):
        assert not (REPO_ROOT / "core" / name).exists(), name
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0010_r1b_event_idempotence.py" and len(versions) == 10
    models = _code_tokens((REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8"))
    assert not re.search(r"academy|rallye", models, re.IGNORECASE)
    tokens = _code_tokens(_source()).lower()
    for word in ("__tablename__", "column", "fastapi", "route", "endpoint"):
        assert word not in tokens, word
