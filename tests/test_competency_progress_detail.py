"""Tests de l'Étape 6.4D : détail d'UNE compétence
(core/competency_progress_detail.py).

1. Contrats : versions, dataclass immuable et exacte, API publique,
   hiérarchie d'erreurs.
2. Composition : quatre parties réelles (6-4A, limitations, 6-4B3, 6-4C3)
   réutilisées telles quelles, pour la même compétence.
3. Identité : toute compétence divergente => Incompatible.
4. Versions : toute version amont inconnue (objet ou module) => Unsupported.
5. Types et invariants des limitations revérifiés.
6. Exclusions : aucun résumé, récit, CTA, compteur, score ni trace.
7. Pureté et contrat statique (aucune base, aucun modèle, aucune écriture,
   aucun branchement runtime).
"""
import ast
import dataclasses
import inspect
import re

import pytest

from core import competency_progress_detail as detail_module
from core.competency_progress_detail import (
    COMPETENCY_PROGRESS_DETAIL_POLICY_VERSION,
    COMPETENCY_PROGRESS_DETAIL_SCHEMA_VERSION,
    CompetencyProgressDetail,
    CompetencyProgressDetailError,
    IncompatibleCompetencyProgressDetailInputs,
    InvalidCompetencyProgressDetailInput,
    UnsupportedCompetencyProgressDetailVersion,
    compose_competency_progress_detail,
)
from core.progress_evidence_rendering import ProgressWhyRendering
from core.progress_history_projection import ProgressHistoryProjection, project_progress_history
from core.progress_limitations import (
    CurrentProgressLimitations,
    LessObservedCapability,
    project_current_progress_limitations,
)
from core.progress_projection import CompetencyCurrentProgress, project_current_progress
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_progress_evidence_rendering import plan, selection, tokens_proposal, validate
from tests.test_progress_history_projection import milestones, revision, stage
from tests.test_progress_limitations import D, c7, tension, v2
from tests.test_progress_projection import card, state

MODULE_PATH = REPO_ROOT / "core" / "competency_progress_detail.py"


# --------------------------------------------------------------------------
# Fabriques : objets RÉELS produits par les APIs publiques amont
# --------------------------------------------------------------------------

def parts(code="C7"):
    """(carte 6-4A, limitations, rendu 6-4B3, historique 6-4C3) de C7."""
    st = state(c7(profile=v2(unobserved=(D,)), tensions=(tension("application", "localized", D),)))
    progress = project_current_progress(state=st)
    current = card(progress, code)
    limitations = project_current_progress_limitations(state=st, progress=progress, competency_code=code)
    why = validate(plan(s=selection()), tokens_proposal("evidence_2", "evidence_5"))
    history = project_progress_history(milestones=milestones(stage("discovery"), stage("application"),
                                                             revision("new_user_evidence"), code=code))
    return current, limitations, why, history


def compose(current=None, limitations=None, why=None, history=None):
    c, lim, w, h = parts()
    return compose_competency_progress_detail(current=current or c, limitations=limitations or lim,
                                              why=why or w, history=history or h)


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_exact():
    assert COMPETENCY_PROGRESS_DETAIL_SCHEMA_VERSION == "competency-progress-detail-v1"
    assert COMPETENCY_PROGRESS_DETAIL_POLICY_VERSION == "competency-progress-detail-policy-1"
    assert (detail_module.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION,
            detail_module.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION) == (
        "current-progress-projection-v1", "current-progress-policy-1")
    assert (detail_module.SUPPORTED_CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION,
            detail_module.SUPPORTED_CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION) == (
        "current-progress-limitations-v1", "current-progress-limitations-policy-1")
    assert (detail_module.SUPPORTED_PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
            detail_module.SUPPORTED_PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION) == (
        "progress-evidence-rendering-v1", "progress-evidence-rendering-policy-1")
    assert (detail_module.SUPPORTED_PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION,
            detail_module.SUPPORTED_PROGRESS_HISTORY_PROJECTION_POLICY_VERSION) == (
        "progress-history-projection-v1", "progress-history-projection-policy-1")


def test_contract_is_exact_and_frozen():
    assert [(f.name, f.type) for f in dataclasses.fields(CompetencyProgressDetail)] == [
        ("schema_version", str), ("policy_version", str), ("current", CompetencyCurrentProgress),
        ("limitations", CurrentProgressLimitations), ("why", ProgressWhyRendering),
        ("history", ProgressHistoryProjection)]
    result = compose()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.why = None
    with pytest.raises(TypeError):
        CompetencyProgressDetail("positional")


def test_public_api_and_signature():
    public = {name for name, obj in inspect.getmembers(detail_module, inspect.isfunction)
              if obj.__module__ == detail_module.__name__ and not name.startswith("_")}
    assert public == {"compose_competency_progress_detail"}
    assert [(p.name, p.kind) for p in inspect.signature(compose_competency_progress_detail).parameters.values()] == [
        (name, inspect.Parameter.KEYWORD_ONLY) for name in ("current", "limitations", "why", "history")]


def test_errors_are_a_dedicated_hierarchy():
    for error in (InvalidCompetencyProgressDetailInput, IncompatibleCompetencyProgressDetailInputs,
                  UnsupportedCompetencyProgressDetailVersion):
        assert issubclass(error, CompetencyProgressDetailError)
    assert not issubclass(CompetencyProgressDetailError, (ValueError, TypeError))


# --------------------------------------------------------------------------
# 2. Composition
# --------------------------------------------------------------------------

def test_same_competency_parts_are_composed_as_is():
    current, limitations, why, history = parts()
    result = compose_competency_progress_detail(current=current, limitations=limitations, why=why, history=history)
    assert (result.schema_version, result.policy_version) == (COMPETENCY_PROGRESS_DETAIL_SCHEMA_VERSION,
                                                              COMPETENCY_PROGRESS_DETAIL_POLICY_VERSION)
    assert result.current is current and result.limitations is limitations
    assert result.why is why and result.history is history
    assert {current.competency_code, limitations.competency_code, why.competency_code,
            history.competency_code} == {"C7"}


def test_empty_parts_are_valid_and_never_filled():
    current, limitations, _, _ = parts()
    why = dataclasses.replace(validate(plan(s=selection()), tokens_proposal("evidence_2", "evidence_5")),
                              examples=())
    history = project_progress_history(milestones=milestones())
    result = compose_competency_progress_detail(current=current, limitations=limitations, why=why, history=history)
    assert (result.why.examples, result.history.milestones) == ((), ())


def test_same_input_same_output():
    current, limitations, why, history = parts()
    args = dict(current=current, limitations=limitations, why=why, history=history)
    assert compose_competency_progress_detail(**args) == compose_competency_progress_detail(**args)


# --------------------------------------------------------------------------
# 3. Identité
# --------------------------------------------------------------------------

@pytest.mark.parametrize("part", ["limitations", "why", "history"])
@pytest.mark.parametrize("other", ["C8", "C1", "c7", None])
def test_any_competency_mismatch_fails_closed(part, other):
    current, limitations, why, history = parts()
    values = dict(current=current, limitations=limitations, why=why, history=history)
    values[part] = dataclasses.replace(values[part], competency_code=other)
    with pytest.raises(IncompatibleCompetencyProgressDetailInputs):
        compose_competency_progress_detail(**values)


@pytest.mark.parametrize("code", ["C13", "c7", None, 7])
def test_current_card_must_name_a_known_competency(code):
    current, limitations, why, history = parts()
    with pytest.raises(InvalidCompetencyProgressDetailInput):
        compose_competency_progress_detail(current=dataclasses.replace(current, competency_code=code),
                                           limitations=limitations, why=why, history=history)


# --------------------------------------------------------------------------
# 4. Versions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("part,field,value", [
    ("limitations", "schema_version", "current-progress-limitations-v2"),
    ("limitations", "policy_version", "current-progress-limitations-policy-2"),
    ("why", "schema_version", "progress-evidence-rendering-v2"),
    ("why", "policy_version", "progress-evidence-rendering-policy-2"),
    ("history", "schema_version", "progress-history-projection-v2"),
    ("history", "policy_version", "progress-history-projection-policy-2"),
])
def test_unknown_upstream_object_version_fails_closed(part, field, value):
    current, limitations, why, history = parts()
    values = dict(current=current, limitations=limitations, why=why, history=history)
    values[part] = dataclasses.replace(values[part], **{field: value})
    with pytest.raises(UnsupportedCompetencyProgressDetailVersion):
        compose_competency_progress_detail(**values)


@pytest.mark.parametrize("constant", [
    "CURRENT_PROGRESS_SCHEMA_VERSION", "CURRENT_PROGRESS_POLICY_VERSION",
    "CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION", "CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION",
    "PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION", "PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION",
    "PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION", "PROGRESS_HISTORY_PROJECTION_POLICY_VERSION"])
def test_unknown_upstream_module_version_fails_closed(monkeypatch, constant):
    current, limitations, why, history = parts()
    monkeypatch.setattr(detail_module, constant, "future-version")
    with pytest.raises(UnsupportedCompetencyProgressDetailVersion):
        compose_competency_progress_detail(current=current, limitations=limitations, why=why, history=history)


# --------------------------------------------------------------------------
# 5. Types et invariants des limitations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("part", ["current", "limitations", "why", "history"])
def test_wrong_types_fail_closed(part):
    current, limitations, why, history = parts()
    values = dict(current=current, limitations=limitations, why=why, history=history)
    values[part] = dataclasses.asdict(values[part])
    with pytest.raises(InvalidCompetencyProgressDetailInput):
        compose_competency_progress_detail(**values)


def test_current_must_be_one_card_never_the_whole_projection():
    current, limitations, why, history = parts()
    projection = project_current_progress(state=state(c7()))
    with pytest.raises(InvalidCompetencyProgressDetailInput):
        compose_competency_progress_detail(current=projection, limitations=limitations, why=why, history=history)


@pytest.mark.parametrize("change", ["unavailable_with_capabilities", "not_applicable_with_capabilities",
                                    "unknown_status", "capabilities_list", "foreign_capability", "tension_str"])
def test_limitations_invariants_are_rechecked(change):
    current, limitations, why, history = parts()
    one = LessObservedCapability(capability_code="C7_D", semantic_revision=1, label="label C7_D")
    forged = dataclasses.replace(limitations)
    attribute = {"unavailable_with_capabilities": ("less_observed_status", "unavailable"),
                 "not_applicable_with_capabilities": ("less_observed_status", "not_applicable"),
                 "unknown_status": ("less_observed_status", "weak"),
                 "capabilities_list": ("less_observed_capabilities", [one]),
                 "foreign_capability": ("less_observed_capabilities", ({"capability_code": "C7_D"},)),
                 "tension_str": ("current_tensions", ("Des éléments...",))}[change]
    object.__setattr__(forged, *attribute)
    with pytest.raises(InvalidCompetencyProgressDetailInput):
        compose_competency_progress_detail(current=current, limitations=forged, why=why, history=history)


# --------------------------------------------------------------------------
# 6. Exclusions
# --------------------------------------------------------------------------

def test_no_summary_narrative_cta_or_count_field():
    names = {f.name for f in dataclasses.fields(CompetencyProgressDetail)}
    assert names == {"schema_version", "policy_version", "current", "limitations", "why", "history"}
    for word in ("summary", "narrative", "journey", "causal", "explanation", "next", "recommend", "practice",
                 "revalidate", "continue", "action", "count", "score", "percent", "ratio", "trace"):
        assert not any(word in name for name in names), word


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


def test_no_text_is_produced_and_no_part_is_read_beyond_identity_and_versions():
    """Aucune chaîne utilisateur fabriquée : aucun texte de why, history ou
    limitations n'est lu (examples / milestones / text / title / detail) ;
    aucun compteur (len) ; aucune trace de support."""
    attributes = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("examples", "milestones", "text", "title", "detail", "label", "stage_label", "stage_code",
                      "represented_capabilities", "evidence_status"):
        assert forbidden not in attributes, forbidden
    calls = {n.func.id for n in ast.walk(_tree()) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {"len", "sum", "max", "min", "sorted", "round"} & calls
    tokens = _code_tokens(_source()).lower()
    parts_ = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("summary", "narrative", "journey", "because", "grâce", "malgré", "next", "recommend", "practice",
                 "percent", "ratio", "score", "average", "overall", "level"):
        assert word not in parts_, word


def test_imports_are_public_contracts_only():
    assert _imports() == {
        "dataclasses": {"dataclass"},
        "core.adaptation_state": {"COMPETENCY_ORDER"},
        "core.progress_evidence_rendering": {"PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION",
                                             "PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION", "ProgressWhyRendering"},
        "core.progress_history_projection": {"PROGRESS_HISTORY_PROJECTION_POLICY_VERSION",
                                             "PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION",
                                             "ProgressHistoryProjection"},
        "core.progress_limitations": {"CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION",
                                      "CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION", "LESS_OBSERVED_AVAILABLE",
                                      "LESS_OBSERVED_STATUSES", "CurrentProgressLimitations",
                                      "LessObservedCapability", "VisibleCurrentTension"},
        "core.progress_projection": {"CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
                                     "CompetencyCurrentProgress"},
    }


def test_no_database_no_session_no_write_no_recomputation():
    names = set(_code_tokens(_source()).split()) | {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("db", "Session", "session", "execute", "select", "query", "add", "flush", "commit",
                      "rollback", "insert", "update", "delete", "project_current_progress",
                      "project_current_progress_limitations", "project_progress_history",
                      "validate_progress_evidence_rendering_proposal", "render_progress_evidence",
                      "acquire_progress_evidence", "acquire_progress_history", "load_adaptation_state"):
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


def test_not_wired_to_the_runtime():
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("compose_competency_progress_detail", "CompetencyProgressDetail"):
        assert name not in api, name
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/competency_progress_detail.py" and "competency_progress_detail" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    assert users == []
    tokens = _code_tokens(_source()).lower()
    for word in ("web_chat", "fastapi", "route", "endpoint", "coach", "education", "decrypt", "portfolio",
                 "rallye", "academy", "__tablename__", "column"):
        assert word not in tokens, word
