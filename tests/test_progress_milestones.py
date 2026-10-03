"""Tests de l'Étape 6.4C2 : compression déterministe de l'historique en
jalons significatifs (core/progress_milestones.py).

1. Contrats : versions, vocabulaires fermés, ordre de sérialisation des
   causes, dataclasses immuables, exclusions (aucun score, jeton, plus haut
   stade, date, compteur), API publique, erreurs.
2. Exemples obligatoires (A à G) et règles : run courant jamais dupliqué,
   première montée seulement, non_etabli jamais positif, maintained
   invisible, révision seulement si un écart historique reste visible,
   causes agrégées depuis la dernière occurrence du stade le plus haut.
3. Propriétés exhaustives sur toutes les lignées courtes.
4. Validation fail-closed de l'entrée.
5. Contrat statique : pur, aucune base, aucun LLM, aucune borne arbitraire,
   aucune lecture de contexte de révision ni de tension.
"""
import ast
import dataclasses
import inspect
import itertools
import re

import pytest

from core import progress_milestones as pm
from core.inference_service import STAGE_SEQUENCE, TRANSITION_CAUSES
from core.progress_history import (
    PROGRESS_HISTORY_POLICY_VERSION,
    PROGRESS_HISTORY_SCHEMA_VERSION,
    HistoricalInferenceState,
    ProgressHistory,
)
from core.progress_milestones import (
    MILESTONE_KINDS,
    MILESTONE_REVISION,
    MILESTONE_STAGE_ESTABLISHED,
    PROGRESS_MILESTONES_POLICY_VERSION,
    PROGRESS_MILESTONES_SCHEMA_VERSION,
    REVISION_CAUSE_ORDER,
    InvalidProgressMilestoneHistory,
    InvalidProgressMilestoneInput,
    ProgressMilestone,
    ProgressMilestoneError,
    ProgressMilestones,
    project_progress_milestones,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "progress_milestones.py"
NEW, INTEGRITY, REINTERPRETATION = "new_user_evidence", "evidence_integrity_change", "pedagogical_reinterpretation"
D, C, A, M, N = "discovery", "comprehension", "application", "mastery", "non_etabli"


def transition(previous, current):
    if current == previous:
        return "maintained"
    return "upgraded" if STAGE_SEQUENCE.index(current) > STAGE_SEQUENCE.index(previous) else "revised_down"


def history(*steps, code="C7"):
    """steps : stade, ou (stade, cause) ; la première inférence n'a ni
    transition ni cause ; cause par défaut new_user_evidence."""
    entries, previous = [], None
    for position, step in enumerate(steps, start=1):
        stage, cause = (step, NEW) if isinstance(step, str) else step
        entries.append(HistoricalInferenceState(
            history_token=f"history_{position}", previous_stage=previous, current_stage=stage,
            transition=None if previous is None else transition(previous, stage),
            transition_cause=None if previous is None else cause))
        previous = stage
    return ProgressHistory(schema_version=PROGRESS_HISTORY_SCHEMA_VERSION,
                           policy_version=PROGRESS_HISTORY_POLICY_VERSION, competency_code=code,
                           entries=tuple(entries))


def milestones(*steps):
    return [(m.milestone_kind, m.stage_code, m.revision_causes)
            for m in project_progress_milestones(history=history(*steps)).milestones]


def stage(code):
    return (MILESTONE_STAGE_ESTABLISHED, code, ())


def revision(*causes):
    return (MILESTONE_REVISION, None, tuple(causes))


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_and_closed_vocabularies():
    assert PROGRESS_MILESTONES_SCHEMA_VERSION == "progress-milestones-v1"
    assert PROGRESS_MILESTONES_POLICY_VERSION == "progress-milestones-policy-1"
    assert (pm.SUPPORTED_PROGRESS_HISTORY_SCHEMA_VERSION, pm.SUPPORTED_PROGRESS_HISTORY_POLICY_VERSION) == (
        PROGRESS_HISTORY_SCHEMA_VERSION, PROGRESS_HISTORY_POLICY_VERSION)
    assert MILESTONE_KINDS == ("stage_established", "revision")
    for word in ("achievement", "regression", "loss", "downgrade", "record"):
        assert not any(word in kind for kind in MILESTONE_KINDS), word


def test_revision_cause_order_is_an_explicit_serialization_of_the_t6_vocabulary():
    """TRANSITION_CAUSES est un frozenset : l'ordre est déclaré ici, tuple
    fermé, ni priorité ni force."""
    assert type(TRANSITION_CAUSES) is frozenset
    assert REVISION_CAUSE_ORDER == (NEW, INTEGRITY, REINTERPRETATION)
    assert set(REVISION_CAUSE_ORDER) == TRANSITION_CAUSES and len(REVISION_CAUSE_ORDER) == 3


def test_structures_are_frozen_keyword_only_with_exactly_these_fields():
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(ProgressMilestone) == ["milestone_kind", "stage_code", "revision_causes"]
    assert names(ProgressMilestones) == ["schema_version", "policy_version", "competency_code", "milestones"]
    for cls in (ProgressMilestone, ProgressMilestones):
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls)), cls
        for f in dataclasses.fields(cls):
            assert not any(word in f.name for word in (
                "score", "rank", "weight", "percent", "level", "points", "highest", "peak", "record", "best",
                "token", "date", "_at", "duration", "days", "speed", "count", "number", "from_", "to_", "delta",
                "previous", "transition", "uuid", "id")), (cls, f.name)


def test_errors_are_a_dedicated_business_hierarchy():
    errors = {o for o in vars(pm).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == pm.__name__}
    assert errors == {ProgressMilestoneError, InvalidProgressMilestoneInput, InvalidProgressMilestoneHistory}
    assert ProgressMilestoneError.__bases__ == (Exception,)
    assert InvalidProgressMilestoneInput.__bases__ == InvalidProgressMilestoneHistory.__bases__ == (
        ProgressMilestoneError,)


def test_public_api_is_exactly_project_progress_milestones():
    functions = {n.name for n in _tree().body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == {"project_progress_milestones"}
    params = inspect.signature(project_progress_milestones).parameters
    assert list(params) == ["history"] and params["history"].kind is inspect.Parameter.KEYWORD_ONLY


def test_output_contract():
    result = project_progress_milestones(history=history(D, C, A))
    assert result == ProgressMilestones(
        schema_version="progress-milestones-v1", policy_version="progress-milestones-policy-1",
        competency_code="C7", milestones=(
            ProgressMilestone(milestone_kind="stage_established", stage_code="discovery", revision_causes=()),
            ProgressMilestone(milestone_kind="stage_established", stage_code="comprehension", revision_causes=())))
    assert "history_" not in repr(result)


# --------------------------------------------------------------------------
# 2. Exemples obligatoires et règles
# --------------------------------------------------------------------------

EXAMPLES = {
    # A : la carte affiche Application ; l'historique ne la répète pas.
    "A": ((D, C, A), [stage(D), stage(C)]),
    # B : Comprehension atteinte par révision n'est pas « établie » ;
    # l'ancienne Application n'est qu'une copie de la carte : retirée.
    "B": ((A, C, A), []),
    # C : Application précède Mastery (chemin) : conservée ; écart visible.
    "C": ((D, C, A, M, (A, REINTERPRETATION)), [stage(D), stage(C), stage(A), stage(M), revision(REINTERPRETATION)]),
    # D : arc entier, causes agrégées dans l'ordre de sérialisation.
    "D": ((M, (A, INTEGRITY), (C, NEW)), [stage(M), revision(NEW, INTEGRITY)]),
    # E : révision résolue, le courant a rattrapé l'histoire.
    "E": ((M, (A, INTEGRITY), M), []),
    # F : non_etabli courant sous Application historique.
    "F": ((A, (N, INTEGRITY)), [stage(A), revision(INTEGRITY)]),
    # G : non_etabli jamais positif ; Discovery courant non dupliqué.
    "G": ((N, D), []),
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_mandatory_examples(name):
    steps, expected = EXAMPLES[name]
    assert milestones(*steps) == expected


@pytest.mark.parametrize("current", STAGE_SEQUENCE)
def test_a_single_current_inference_has_no_milestone(current):
    """La carte 6-4A affiche déjà l'état courant (#66)."""
    assert milestones(current) == []


def test_first_stages_before_the_current_one():
    assert milestones(D, C, A) == [stage(D), stage(C)]
    assert milestones(D, A) == [stage(D)]  # saut : aucun passage intermédiaire inventé


def test_duplicate_stage_is_compressed_and_current_is_not_rendered():
    """#68 : Comprehension une seule fois ; Application est le courant ;
    aucune révision (le courant a retrouvé le plus haut historique)."""
    assert milestones(D, C, A, C, A) == [stage(D), stage(C)]


def test_maintained_is_never_a_milestone_whatever_its_cause():
    """#69 / #24 : jamais trois Application ; un ancien jalon identique à la
    carte, sans établissement supérieur après lui, est retiré."""
    assert milestones(A, (A, REINTERPRETATION), (A, INTEGRITY)) == []
    assert milestones(D, (D, REINTERPRETATION), (D, NEW), C) == [stage(D)]
    assert milestones(D, D, C, C, A) == [stage(D), stage(C)]


def test_current_stage_simplification_rule():
    """#70 : retiré s'il ne précède aucun établissement supérieur ; conservé
    s'il a précédé un stade supérieur historiquement établi."""
    assert milestones(D, C, A) == [stage(D), stage(C)]
    assert milestones(A, A) == []
    assert milestones(A, M, (A, NEW)) == [stage(A), stage(M), revision(NEW)]
    # Mastery établi AVANT Application : Application n'est pas sur le chemin.
    assert milestones(M, (A, NEW), (C, NEW), A) == [stage(M), revision(NEW)]


def test_a_stage_reached_by_revision_is_never_established_but_a_later_climb_is():
    assert milestones(M, (C, NEW), (D, INTEGRITY)) == [stage(M), revision(NEW, INTEGRITY)]
    assert milestones(M, (C, NEW), A, (D, REINTERPRETATION)) == [stage(M), stage(A), revision(NEW, REINTERPRETATION)]


def test_non_etabli_is_never_a_positive_milestone():
    assert milestones(N, N, D, C) == [stage(D)]
    assert milestones(A, (N, INTEGRITY)) == [stage(A), revision(INTEGRITY)]
    assert milestones(D, (N, REINTERPRETATION), D) == []
    assert all(m[1] != N for m in milestones(N, D, (N, INTEGRITY), C, M, (N, INTEGRITY)))


def test_unresolved_revision_stays_visible():
    """#71."""
    assert milestones(D, C, A, M, (A, NEW)) == [stage(D), stage(C), stage(A), stage(M), revision(NEW)]


def test_resolved_revision_is_not_shown():
    """#72 : Mastery -> Application -> Mastery."""
    assert milestones(M, (A, NEW), M) == []
    assert milestones(D, C, A, M, (A, NEW), M) == [stage(D), stage(C), stage(A)]


def test_partial_rebound_keeps_the_revision_visible():
    """#73 : Mastery -> Comprehension -> Application."""
    assert milestones(M, (C, INTEGRITY), A) == [stage(M), revision(INTEGRITY)]


def test_multiple_causes_are_aggregated_over_the_whole_arc_in_serialization_order():
    """#74 / #28 : jamais la seule dernière révision."""
    assert milestones(M, (A, INTEGRITY), (C, NEW)) == [stage(M), revision(NEW, INTEGRITY)]
    assert milestones(M, (A, REINTERPRETATION), (C, INTEGRITY), (D, NEW)) == [
        stage(M), revision(NEW, INTEGRITY, REINTERPRETATION)]


def test_same_cause_twice_is_deduplicated_without_any_counter():
    """#75."""
    assert milestones(M, (A, INTEGRITY), (C, INTEGRITY)) == [stage(M), revision(INTEGRITY)]


def test_only_revisions_after_the_last_occurrence_of_the_highest_stage_count():
    assert milestones(M, (A, INTEGRITY), M, (C, REINTERPRETATION)) == [stage(M), revision(REINTERPRETATION)]
    # Les causes des montées et des maintiens ne sont jamais des causes de révision.
    assert milestones(A, M, (C, INTEGRITY), (A, REINTERPRETATION), (A, NEW)) == [
        stage(A), stage(M), revision(INTEGRITY)]


def test_incoherent_gap_without_revision_fails_closed():
    """#77 : un état courant sous le plus haut historique sans aucune
    transition revised_down est une histoire incohérente."""
    entries = history(M, (A, NEW)).entries
    tampered = dataclasses.replace(history(M, (A, NEW)), entries=(
        entries[0], dataclasses.replace(entries[1], transition="maintained")))
    with pytest.raises(InvalidProgressMilestoneHistory):
        project_progress_milestones(history=tampered)
    # Garde explicite de l'arc, indépendante de la validation des transitions.
    arc = (entries[0], dataclasses.replace(entries[1], transition="upgraded"))
    with pytest.raises(InvalidProgressMilestoneHistory, match="sans aucune transition revised_down"):
        pm._revision_causes(arc)


def test_pure_and_deterministic_and_input_untouched():
    h = history(D, C, A, M, (A, INTEGRITY), (C, NEW))
    snapshot = repr(h)
    assert project_progress_milestones(history=h) == project_progress_milestones(history=h)
    assert repr(h) == snapshot


def test_empty_history_has_no_milestone():
    result = project_progress_milestones(history=history())
    assert (result.competency_code, result.milestones) == ("C7", ())


# --------------------------------------------------------------------------
# 3. Propriétés exhaustives (toutes les lignées de 1 à 5 runs)
# --------------------------------------------------------------------------

def _positions(steps):
    return [STAGE_SEQUENCE.index(s) for s in steps]


@pytest.mark.parametrize("length", [1, 2, 3, 4, 5])
def test_properties_over_every_short_lineage(length):
    for stages in itertools.product(STAGE_SEQUENCE, repeat=length):
        causes = itertools.cycle(REVISION_CAUSE_ORDER)
        steps = [stages[0], *((s, next(causes)) for s in stages[1:])]
        h = history(*steps)
        result = project_progress_milestones(history=h).milestones
        kinds = [m.milestone_kind for m in result]
        established = [m.stage_code for m in result if m.milestone_kind == MILESTONE_STAGE_ESTABLISHED]
        current = stages[-1]
        highest = max((s for s in stages if s != N), key=STAGE_SEQUENCE.index, default=None)
        label = stages
        assert N not in established, label
        assert len(set(established)) == len(established) <= 4, label
        assert kinds.count(MILESTONE_REVISION) <= 1, label
        assert MILESTONE_REVISION not in kinds[:-1], label  # toujours en dernier
        visible_gap = highest is not None and STAGE_SEQUENCE.index(current) < STAGE_SEQUENCE.index(highest)
        assert (MILESTONE_REVISION in kinds) == visible_gap, label
        # Chaque stade établi l'a été par une montée ou une première inférence, avant le courant.
        for code in established:
            assert any(e.current_stage == code and e.transition in (None, "upgraded") for e in h.entries[:-1]), label
        # Jamais une copie de la carte : le courant n'apparaît que s'il précède un stade supérieur établi.
        if current in established:
            later = established[established.index(current) + 1:]
            assert any(STAGE_SEQUENCE.index(s) > STAGE_SEQUENCE.index(current) for s in later), label
        for m in result:
            if m.milestone_kind == MILESTONE_REVISION:
                assert m.stage_code is None and m.revision_causes, label
                assert m.revision_causes == tuple(c for c in REVISION_CAUSE_ORDER if c in m.revision_causes)
            else:
                assert m.revision_causes == (), label
        assert project_progress_milestones(history=h).milestones == result


# --------------------------------------------------------------------------
# 4. Validation fail-closed
# --------------------------------------------------------------------------

def _entries(h, index, **changes):
    entries = list(h.entries)
    entries[index] = dataclasses.replace(entries[index], **changes)
    return dataclasses.replace(h, entries=tuple(entries))


@pytest.mark.parametrize("build", [
    lambda h: None,
    lambda h: dataclasses.asdict(h),
    lambda h: dataclasses.replace(h, schema_version="progress-history-v2"),
    lambda h: dataclasses.replace(h, policy_version="progress-history-policy-2"),
    lambda h: dataclasses.replace(h, competency_code="C13"),
    lambda h: dataclasses.replace(h, competency_code=None),
    lambda h: dataclasses.replace(h, entries=list(h.entries)),
    lambda h: dataclasses.replace(h, entries=(*h.entries[:-1], dataclasses.asdict(h.entries[-1]))),
    lambda h: _entries(h, 1, history_token="history_3"),
    lambda h: _entries(h, 0, history_token="evidence_1"),
    lambda h: dataclasses.replace(h, entries=h.entries[1:]),
    lambda h: _entries(h, 2, current_stage="expert"),
    lambda h: _entries(h, 2, current_stage=None),
    lambda h: _entries(h, 1, transition_cause=None),
    lambda h: _entries(h, 1, transition_cause="user_regression"),
    lambda h: _entries(h, 1, transition="downgraded"),
    lambda h: _entries(h, 1, previous_stage="expert"),
])
def test_malformed_input_is_rejected(build):
    with pytest.raises(InvalidProgressMilestoneInput):
        project_progress_milestones(history=build(history(D, C, A)))


@pytest.mark.parametrize("build", [
    lambda h: _entries(h, 0, transition_cause=NEW),
    lambda h: _entries(h, 0, previous_stage=D),
    lambda h: _entries(h, 0, transition="upgraded"),
    lambda h: _entries(h, 1, previous_stage=A),
    lambda h: _entries(h, 2, transition="maintained"),
    lambda h: _entries(h, 2, transition="revised_down"),
])
def test_incoherent_history_is_rejected(build):
    with pytest.raises(InvalidProgressMilestoneHistory):
        project_progress_milestones(history=build(history(D, C, A)))


def test_subclass_is_not_accepted():
    class Sub(ProgressHistory):
        pass
    h = history(D, C)
    with pytest.raises(InvalidProgressMilestoneInput):
        project_progress_milestones(history=Sub(**{f.name: getattr(h, f.name) for f in dataclasses.fields(h)}))


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


def test_imports_are_vocabularies_and_the_6_4c1_contract_only():
    assert _imports() == {
        "dataclasses": {"dataclass"},
        "core.inference_service": {
            "CLAIM_STAGES", "COMPETENCY_CODES", "CURRENT_STAGES", "EVIDENCE_INTEGRITY_CHANGE", "MAINTAINED",
            "NEW_USER_EVIDENCE", "NON_ETABLI", "PEDAGOGICAL_REINTERPRETATION", "REVISED_DOWN", "STAGE_SEQUENCE",
            "TRANSITION_CAUSES", "TRANSITIONS", "UPGRADED"},
        "core.progress_history": {"HISTORY_TOKEN_PREFIX", "HistoricalInferenceState", "ProgressHistory"},
    }
    # Aucune fonction ni lecture du service propriétaire : des constantes.
    imported = _imports()["core.inference_service"]
    assert all(name.isupper() for name in imported)


def test_no_database_no_session_no_orm():
    tokens = set(_code_tokens(_source()).split())
    names = tokens | {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("db", "Session", "session", "execute", "select", "query", "add", "flush", "commit",
                      "rollback", "no_autoflush", "acquire_progress_history", "get_inference_lineage",
                      "get_validated_user_competency_state"):
        assert forbidden not in names, forbidden
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "alembic", "psycopg2"} & set(_imports())


def test_no_llm_clock_randomness_or_support_trace():
    assert not {"anthropic", "openai", "requests", "httpx", "random", "secrets", "datetime", "time", "os",
                "uuid", "json"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "backend", "renderer", "proposal",
                 "support_trace", "supporttrace", "environ", "uuid4"):
        assert word not in tokens, word
    assert not {"llm", "model", "now", "today"} & set(tokens.split())


def test_no_revision_context_tension_or_anti_oscillation_reconstruction():
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    for forbidden in ("unresolved_revision_context", "revision_context", "motifs", "resolution_status",
                      "tension_state", "tensions", "validation_needs", "contradiction", "positive_basis",
                      "revalidation", "observation", "evidence", "claims", "confidence"):
        assert forbidden not in names, forbidden
    read = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "entry"}
    assert read == {"current_stage", "previous_stage", "transition", "transition_cause", "history_token"}


def test_no_arbitrary_bound_score_or_visible_highest():
    """#31 / #88 / #90 : aucune constante MAX_*, aucun score ; le plus haut
    stade est une variable locale, jamais un champ ni une constante."""
    assert "MAX_" not in _source() and "max(" not in _source()
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "percent", "rank", "points", "xp", "speed", "days", "duration", "elapsed",
                 "count", "best", "peak", "record", "achievement", "regression", "loss", "downgrade", "level"):
        assert word not in parts, word
    integers = {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is int}
    assert integers <= {1, 2}, integers  # positions de jetons seulement, aucune borne pédagogique
    module_names = {t.id for n in _tree().body if isinstance(n, ast.Assign) for t in n.targets
                    if isinstance(t, ast.Name)}
    assert not any("HIGHEST" in name or "PEAK" in name or "MAX" in name for name in module_names)


def test_not_wired_to_the_runtime():
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("project_progress_milestones", "ProgressMilestones", "ProgressMilestone"):
        assert name not in api, name
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_milestones.py" and "progress_milestones" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    assert users == ["core/progress_history_projection.py"]
