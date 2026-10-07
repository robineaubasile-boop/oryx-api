"""Tests de l'Étape 6.4C1 : acquisition de l'historique authentique de l'état
pédagogique courant (core/progress_history.py).

1. Contrats : versions, dataclasses immuables, champs exacts, exclusions
   (aucun UUID, horodatage, tension, contexte de révision), API publique,
   hiérarchie d'erreurs.
2. Carte sans état : histoire vide, aucune lecture.
3. Compatibilité 6-4A : projection recalculée, falsification => incompatible.
4. Lignée : jetons history_k séquentiels, copie telle quelle, oldest ->
   current, dernier = run capturé ; revérification minimale du contrat du
   service propriétaire.
5. Revalidation Step 5 avant / après : stale vs invalide, aucun retry.
6. Contrat statique : imports, lecture seule, aucun LLM, aucune preuve
   relue, aucune trace de support, aucun branchement runtime.
7. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
   SKIPPÉS) : T3 -> T5 -> T6 réels (plusieurs runs) -> 6-1A -> 6-4A ->
   6-4C1 -> 6-4C2 -> 6-4C3.

Couches 2 à 5 : le snapshot est produit par la projection 6-1A RÉELLE
depuis des enregistrements construits à la main (tests/test_adaptation_state
.World), puis la carte par 6-4A réelle ; seules les deux lectures du service
propriétaire relues par 6-4C1 sont simulées.
"""
import ast
import contextlib
import dataclasses
import inspect
import re
import uuid

import pytest
import sqlalchemy as sa

from core import progress_history as ph
from core.adaptation_state import AdaptationStateSnapshot, load_adaptation_state
from core.inference_service import (
    HistoricalInferenceRun,
    InferenceRunNotFound,
    InvalidInferenceState,
    StaleInferenceChain,
    ValidatedCompetencyState,
)
from core.progress_history import (
    PROGRESS_HISTORY_POLICY_VERSION,
    PROGRESS_HISTORY_SCHEMA_VERSION,
    HistoricalInferenceState,
    IncompatibleProgressHistoryInputs,
    InvalidProgressHistoryArgument,
    InvalidProgressHistoryState,
    ProgressHistory,
    ProgressHistoryError,
    StaleProgressHistory,
    acquire_progress_history,
)
from core.progress_projection import project_current_progress
from tests.test_adaptation_state import World
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "progress_history.py"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FORBIDDEN_FIELDS = ("inference_run_id", "predecessor_inference_run_id", "run_id", "user_id", "id", "uuid",
                    "created_at", "started_at", "completed_at", "updated_at", "date", "timestamp", "duration",
                    "tension_state", "unresolved_revision_context", "validation_needs", "confidence", "claims",
                    "evidence", "score", "rank", "level", "highest_stage", "count", "state_generation")


# --------------------------------------------------------------------------
# Services propriétaires simulés
# --------------------------------------------------------------------------

class FakeDB:
    """Session simulée : seul db.no_autoflush est permis."""

    def __init__(self):
        self.entered = 0

    @property
    def no_autoflush(self):
        @contextlib.contextmanager
        def block():
            self.entered += 1
            yield
        return block()

    def __getattr__(self, name):
        raise AssertionError(f"db.{name} : 6-4C1 ne touche la session que par no_autoflush")


def application_world(current="application"):
    """Application directe localized (C8_A / C8_C), inférieures implied."""
    w = World()
    w.direct("application", w.obs("a", "C", "A"))
    w.implied("comprehension")
    w.implied("discovery")
    w.current_stage = current
    return w


def run_id(name):
    return uuid.uuid5(uuid.UUID("6a4c1000-0000-4000-8000-000000000000"), name)


class Owner:
    """get_validated_user_competency_state / get_inference_lineage simulés,
    cohérents avec le World sauf corruption explicite."""

    def __init__(self, w: World, stages=(("discovery", None), ("comprehension", "new_user_evidence"))):
        self.w = w
        self.calls = []
        self.revalidations = []  # effet du n-ième appel : ValidatedCompetencyState, None ou exception
        runs, previous, predecessor = [], None, None
        for index, (stage, cause) in enumerate((*stages, (w.current_stage, "new_user_evidence"))):
            identifier = w.run_id if index == len(stages) else run_id(f"r{index}")
            if previous is None:
                transition, cause = None, None
            elif stage == previous:
                transition = "maintained"
            else:
                order = ("non_etabli", "discovery", "comprehension", "application", "mastery")
                transition = "upgraded" if order.index(stage) > order.index(previous) else "revised_down"
            runs.append(HistoricalInferenceRun(
                inference_run_id=identifier, user_id="u", competency_code=w.code,
                predecessor_inference_run_id=predecessor, previous_stage=previous, current_stage=stage,
                transition=transition, transition_cause=cause))
            previous, predecessor = stage, identifier
        self.lineage = tuple(runs)
        self.lineage_effect = None

    def state(self) -> AdaptationStateSnapshot:
        return AdaptationStateSnapshot(user_id="u", competencies=(self.w.project(),))

    def install(self, monkeypatch):
        monkeypatch.setattr(ph, "get_validated_user_competency_state", self.validated)
        monkeypatch.setattr(ph, "get_inference_lineage", self.read_lineage)
        return self

    def validated(self, db, *, user_id, competency_code):
        self.calls.append(("validated", user_id, competency_code))
        effect = self.revalidations.pop(0) if self.revalidations else self.w.validated()
        if isinstance(effect, Exception):
            raise effect
        return effect

    def read_lineage(self, db, *, run_id):
        self.calls.append(("lineage", run_id))
        if isinstance(self.lineage_effect, Exception):
            raise self.lineage_effect
        return self.lineage

    def acquire(self, state=None, progress=None, code=None, db=None):
        state = state or self.state()
        progress = progress or project_current_progress(state=state)
        return acquire_progress_history(db or FakeDB(), state=state, progress=progress,
                                        competency_code=code or self.w.code)


@pytest.fixture
def owner(monkeypatch):
    def build(w=None, **kwargs):
        return Owner(w or application_world(), **kwargs).install(monkeypatch)
    return build


def entries(result):
    return [(e.history_token, e.previous_stage, e.current_stage, e.transition, e.transition_cause)
            for e in result.entries]


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_explicit():
    assert PROGRESS_HISTORY_SCHEMA_VERSION == "progress-history-v1"
    assert PROGRESS_HISTORY_POLICY_VERSION == "progress-history-policy-1"
    assert ph.HISTORY_TOKEN_PREFIX == "history_"


def test_structures_are_frozen_keyword_only_dataclasses_with_exactly_these_fields():
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(HistoricalInferenceState) == ["history_token", "previous_stage", "current_stage", "transition",
                                               "transition_cause"]
    assert names(ProgressHistory) == ["schema_version", "policy_version", "competency_code", "entries"]
    for cls in (HistoricalInferenceState, ProgressHistory):
        assert cls.__dataclass_params__.frozen, cls
        assert all(f.kw_only for f in dataclasses.fields(cls)), cls
        for field in FORBIDDEN_FIELDS:
            assert field not in names(cls), (cls, field)
        for f in dataclasses.fields(cls):
            assert "UUID" not in str(f.type) and "datetime" not in str(f.type), (cls, f.name)
    with pytest.raises(dataclasses.FrozenInstanceError):
        HistoricalInferenceState(history_token="history_1", previous_stage=None, current_stage="discovery",
                                 transition=None, transition_cause=None).current_stage = "mastery"


def test_errors_are_a_dedicated_business_hierarchy():
    errors = {o for o in vars(ph).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == ph.__name__}
    assert errors == {ProgressHistoryError, InvalidProgressHistoryArgument, IncompatibleProgressHistoryInputs,
                      InvalidProgressHistoryState, StaleProgressHistory}
    assert ProgressHistoryError.__bases__ == (Exception,)
    for error in errors - {ProgressHistoryError}:
        assert error.__bases__ == (ProgressHistoryError,), error


def test_public_api_is_exactly_acquire_progress_history():
    functions = {n.name for n in _tree().body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == {"acquire_progress_history"}
    params = inspect.signature(acquire_progress_history).parameters
    assert list(params) == ["db", "state", "progress", "competency_code"]
    assert params["db"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(params[n].kind is inspect.Parameter.KEYWORD_ONLY for n in ("state", "progress", "competency_code"))
    assert all(p.default is inspect.Parameter.empty for p in params.values())


# --------------------------------------------------------------------------
# 2. Arguments et carte sans état
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("state", None), ("state", "u"), ("progress", None), ("progress", ()), ("competency_code", "C13"),
    ("competency_code", None), ("competency_code", "c8"),
])
def test_bad_arguments_are_rejected(owner, field, value):
    o = owner()
    state = o.state()
    kwargs = {"state": state, "progress": project_current_progress(state=state), "competency_code": "C8"}
    kwargs[field] = value
    with pytest.raises(InvalidProgressHistoryArgument):
        acquire_progress_history(FakeDB(), **kwargs)
    assert o.calls == []


@pytest.mark.parametrize("user_id", ["", "   ", "u\x00", 7])
def test_blank_user_identity_is_rejected(owner, user_id):
    o = owner()
    state = dataclasses.replace(o.state(), user_id=user_id)
    with pytest.raises(InvalidProgressHistoryArgument):
        acquire_progress_history(FakeDB(), state=state, progress=project_current_progress(state=state),
                                 competency_code="C8")


def test_card_without_state_has_no_history_and_reads_nothing(owner):
    """Compétence absente du snapshot : aucune histoire inventée (jamais un
    non_etabli), aucune lecture de base."""
    o = owner()
    db = FakeDB()
    result = o.acquire(code="C3", db=db)
    assert result == ProgressHistory(schema_version="progress-history-v1", policy_version="progress-history-policy-1",
                                     competency_code="C3", entries=())
    assert o.calls == [] and db.entered == 0


# --------------------------------------------------------------------------
# 3. Compatibilité 6-4A
# --------------------------------------------------------------------------

def _replace_card(progress, code, **changes):
    return dataclasses.replace(progress, competencies=tuple(
        dataclasses.replace(card, **changes) if card.competency_code == code else card
        for card in progress.competencies))


@pytest.mark.parametrize("changes", [
    {"stage_code": "mastery"}, {"stage_label": "Expert"}, {"coverage_mode": "none"},
    {"state_present": False}, {"represented_capabilities": ()},
])
def test_falsified_projection_is_incompatible(owner, changes):
    o = owner()
    state = o.state()
    progress = _replace_card(project_current_progress(state=state), "C8", **changes)
    with pytest.raises(IncompatibleProgressHistoryInputs):
        o.acquire(state=state, progress=progress)
    assert o.calls == []


def test_projection_of_another_state_or_a_missing_card_is_incompatible(owner):
    o = owner()
    state = o.state()
    other = AdaptationStateSnapshot(user_id="u", competencies=())
    with pytest.raises(IncompatibleProgressHistoryInputs):
        o.acquire(state=state, progress=project_current_progress(state=other))
    progress = project_current_progress(state=state)
    truncated = dataclasses.replace(progress, competencies=progress.competencies[:5])
    with pytest.raises(IncompatibleProgressHistoryInputs, match="0 carte"):
        o.acquire(state=state, progress=truncated)
    with pytest.raises(IncompatibleProgressHistoryInputs):
        o.acquire(state=state, progress=dataclasses.replace(progress, competencies=list(progress.competencies)))


def test_snapshot_rejected_by_6_4a_is_an_invalid_state(owner):
    o = owner()
    state = o.state()
    progress = project_current_progress(state=state)
    (snapshot,) = state.competencies
    broken = AdaptationStateSnapshot(user_id="u", competencies=(dataclasses.replace(snapshot, claims=()),))
    with pytest.raises(InvalidProgressHistoryState, match="non projetable"):
        o.acquire(state=broken, progress=progress)


# --------------------------------------------------------------------------
# 4. Lignée
# --------------------------------------------------------------------------

def test_lineage_is_copied_oldest_to_current_with_sequential_ephemeral_tokens(owner):
    o = owner()
    db = FakeDB()
    result = o.acquire(db=db)
    assert result == ProgressHistory(
        schema_version="progress-history-v1", policy_version="progress-history-policy-1", competency_code="C8",
        entries=(
            HistoricalInferenceState(history_token="history_1", previous_stage=None, current_stage="discovery",
                                     transition=None, transition_cause=None),
            HistoricalInferenceState(history_token="history_2", previous_stage="discovery",
                                     current_stage="comprehension", transition="upgraded",
                                     transition_cause="new_user_evidence"),
            HistoricalInferenceState(history_token="history_3", previous_stage="comprehension",
                                     current_stage="application", transition="upgraded",
                                     transition_cause="new_user_evidence")))
    assert not UUID_PATTERN.search(repr(result))
    assert "u" not in {getattr(e, f.name) for e in result.entries for f in dataclasses.fields(e)}
    assert db.entered == 1


def test_reads_are_bound_to_the_captured_run_and_revalidated_before_and_after(owner):
    o = owner()
    o.acquire()
    assert o.calls == [("validated", "u", "C8"), ("lineage", o.w.run_id), ("validated", "u", "C8")]


def test_single_first_inference_is_a_one_entry_history(owner):
    o = owner(stages=())
    assert entries(o.acquire()) == [("history_1", None, "application", None, None)]


def test_tokens_are_strictly_sequential_for_a_long_history(owner):
    stages = (("application", None), ("application", "pedagogical_reinterpretation"),
              ("comprehension", "new_user_evidence"), ("comprehension", "new_user_evidence"),
              ("discovery", "evidence_integrity_change"), ("non_etabli", "evidence_integrity_change"),
              ("mastery", "new_user_evidence"))
    o = owner(stages=stages)
    result = o.acquire()
    assert [e.history_token for e in result.entries] == [f"history_{k}" for k in range(1, 9)]
    assert [e.transition for e in result.entries] == [None, "maintained", "revised_down", "maintained",
                                                      "revised_down", "revised_down", "upgraded", "revised_down"]


def test_non_etabli_current_card_still_has_its_history(owner):
    """non_etabli est un état Step 5 présent : son histoire est acquise
    (Application -> non_etabli par intégrité)."""
    w = World()
    o = owner(w, stages=(("application", None),))
    o.lineage = (o.lineage[0], dataclasses.replace(o.lineage[1], transition_cause="evidence_integrity_change"))
    assert entries(o.acquire()) == [("history_1", None, "application", None, None),
                                    ("history_2", "application", "non_etabli", "revised_down",
                                     "evidence_integrity_change")]


def _swap(o, index, **changes):
    runs = list(o.lineage)
    runs[index] = dataclasses.replace(runs[index], **changes)
    o.lineage = tuple(runs)


@pytest.mark.parametrize("corrupt,match", [
    (lambda o: setattr(o, "lineage", ()), "non vide"),
    (lambda o: setattr(o, "lineage", list(o.lineage)), "tuple"),
    (lambda o: setattr(o, "lineage", (*o.lineage[:-1], dataclasses.asdict(o.lineage[-1]))), "HistoricalInferenceRun"),
    (lambda o: setattr(o, "lineage", o.lineage[:-1]), "différent du run capturé"),
    (lambda o: setattr(o, "lineage", o.lineage[1:]), "non relié"),
    (lambda o: _swap(o, -1, current_stage="mastery"), "différent du run capturé"),
    (lambda o: _swap(o, -1, inference_run_id=uuid.uuid4()), "différent du run capturé"),
    (lambda o: _swap(o, 1, predecessor_inference_run_id=uuid.uuid4()), "non relié"),
    (lambda o: _swap(o, 0, user_id="u2"), "autre couple"),
    (lambda o: _swap(o, 1, competency_code="C9"), "autre couple"),
])
def test_lineage_contract_is_rechecked_minimally(owner, corrupt, match):
    o = owner()
    corrupt(o)
    with pytest.raises(InvalidProgressHistoryState, match=match):
        o.acquire()


@pytest.mark.parametrize("error", [InvalidInferenceState("cycle"), InferenceRunNotFound("x")])
def test_owner_lineage_errors_are_invalid_states_never_partial(owner, error):
    o = owner()
    o.lineage_effect = error
    with pytest.raises(InvalidProgressHistoryState, match="illisible") as info:
        o.acquire()
    assert info.value.__cause__ is error
    assert [c[0] for c in o.calls] == ["validated", "lineage"]


def test_same_inputs_same_history(owner):
    o = owner()
    assert o.acquire() == o.acquire()


# --------------------------------------------------------------------------
# 5. Revalidation Step 5 avant / après
# --------------------------------------------------------------------------

def _other_run(o):
    return dataclasses.replace(o.w.validated(), active_inference_run_id=uuid.uuid4())


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("effect,error", [
    ("other_run", StaleProgressHistory),
    (None, StaleProgressHistory),
    (StaleInferenceChain("T5 superseded"), StaleProgressHistory),
    (InvalidInferenceState("cache divergent"), InvalidProgressHistoryState),
    ("other_stage", InvalidProgressHistoryState),
])
def test_revalidation_outcomes(owner, position, effect, error):
    o = owner()
    if effect == "other_run":
        effect = _other_run(o)
    elif effect == "other_stage":
        effect = dataclasses.replace(o.w.validated(), current_stage="mastery")
    o.revalidations = [o.w.validated()] * position + [effect]
    with pytest.raises(error):
        o.acquire()
    expected = ["validated"] if position == 0 else ["validated", "lineage", "validated"]
    assert [c[0] for c in o.calls] == expected  # stale avant : aucune lignée lue ; aucun retry


def test_validated_state_type_is_the_owner_contract():
    assert {f.name for f in dataclasses.fields(ValidatedCompetencyState)} >= {"active_inference_run_id",
                                                                              "current_stage"}


# --------------------------------------------------------------------------
# 6. Contrat statique
# --------------------------------------------------------------------------

def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    by_module = {}
    for node in _tree().body:
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                by_module.setdefault(alias.name, set())
    return by_module


def test_imports_are_exactly_the_owner_service_and_public_contracts():
    assert _imports() == {
        "uuid": set(),
        "dataclasses": {"dataclass"},
        "core.adaptation_state": {"COMPETENCY_ORDER", "AdaptationStateSnapshot", "CompetencyAdaptationSnapshot"},
        "core.inference_service": {"HistoricalInferenceRun", "InferenceServiceError", "StaleInferenceChain",
                                   "get_inference_lineage", "get_validated_user_competency_state"},
        "core.progress_projection": {"CompetencyCurrentProgress", "CurrentProgressProjection",
                                     "ProgressProjectionError", "project_current_progress"},
    }
    assert not any(name.startswith("_") for name in set().union(*_imports().values()))


def test_read_only_no_sql_no_lock_no_orm_model():
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    for forbidden in ("add", "add_all", "flush", "commit", "rollback", "delete", "merge", "execute", "insert",
                      "update", "begin_nested", "with_for_update", "select", "text", "query", "refresh",
                      "expire", "pg_advisory_xact_lock", "FOR UPDATE"):
        assert forbidden not in names, forbidden
    used = {node.attr for node in ast.walk(_tree())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "db"}
    assert used == {"no_autoflush"}
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "alembic", "psycopg2"} & set(_imports())


def test_no_evidence_reread_no_reinference_no_active_api():
    """L'histoire n'est jamais reconstruite depuis les preuves : aucune
    claim, tension, ref, observation, dossier T5 ni moteur relu."""
    tokens = set(_code_tokens(_source()).split())
    for forbidden in ("get_competency_inference", "get_active_competency_inference", "get_user_competency_state",
                      "get_stage_claims", "get_inference_tensions", "get_inference_basis_refs",
                      "get_inference_context", "load_adaptation_state", "infer_competency",
                      "build_longitudinal_dossier", "get_observation", "get_longitudinal_inputs",
                      "get_longitudinal_assessment", "get_evaluation_run", "get_event"):
        assert forbidden not in tokens, forbidden


def test_no_timestamp_tension_or_revision_context_is_read():
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    for forbidden in ("created_at", "started_at", "completed_at", "closed_at", "updated_at", "tension_state",
                      "tensions", "unresolved_revision_context", "validation_needs", "claims",
                      "state_generation", "confidence_profile", "mastery_assessment"):
        assert forbidden not in names, forbidden


def test_no_llm_network_clock_randomness_or_support_trace():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "random", "secrets", "datetime",
                "time", "os", "json", "logging"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "backend", "environ", "getenv", "uuid4",
                 "support_trace", "supporttrace", "add_support_trace"):
        assert word not in tokens, word
    assert not {"llm", "model", "now", "today", "sorted", "order_by"} & set(tokens.split())


def test_no_score_count_speed_or_user_facing_text():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "percent", "rank", "best", "highest", "peak", "record", "speed", "days",
                 "duration", "elapsed", "count", "label", "title", "render", "recommend", "milestone"):
        assert word not in parts, word
    calls = {n.func.id for n in ast.walk(_tree()) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {"sorted", "max", "min", "sum"} & calls


def test_no_migration_no_db_model_and_not_wired_to_the_runtime():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0014_evaluation_run_leases.py" and len(versions) == 14
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "ProgressHistory" not in models and "progress_history" not in models
    # Seuls consommateurs, eux-mêmes non branchés : 6-4C2 lit les contrats
    # publics de 6-4C1 (jamais acquire_progress_history) ; 6-4C3 n'importe
    # rien de 6-4C1.
    step6_consumers = {
        "core/progress_milestones.py": {"HISTORY_TOKEN_PREFIX", "HistoricalInferenceState", "ProgressHistory"},
        "core/progress_history_projection.py": set(),
        # Étape 6.4D : le détail lit seulement le contrat de sortie de 6-4C3,
        # rien de 6-4C1 (tests/test_competency_progress_detail.py).
        "core/competency_progress_detail.py": set(),
    }
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if getattr(n, "module", None) == "core.progress_history"
                    or a.name == "core.progress_history"]
        assert sorted(imported) == sorted(("core.progress_history", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_history.py" and "acquire_progress_history" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
        if rel not in (*step6_consumers, "core/progress_history.py") and "progress_history" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("acquire_progress_history", "ProgressHistory", "HistoricalInferenceState",
                 "get_inference_lineage"):
        assert name not in api, name
    tokens = _code_tokens(_source()).lower()
    for word in ("web_chat", "fastapi", "route", "endpoint", "coach", "education", "decrypt", "portfolio",
                 "rallye", "academy", "__tablename__", "column"):
        assert word not in tokens, word


# --------------------------------------------------------------------------
# 7. Intégration PostgreSQL : T3 / T5 / T6 réels -> 6-1A -> 6-4A -> 6-4C
# --------------------------------------------------------------------------

UPSTREAM_TABLES = ("cognitive_events", "support_traces", "observation_evaluation_runs", "pedagogical_observations",
                   "observation_capabilities", "longitudinal_assessment_runs", "longitudinal_assessment_inputs",
                   "competency_inference_runs", "competency_stage_claims", "competency_inference_tensions",
                   "competency_inference_basis_refs", "user_competency_states")


def _dump(engine):  # noqa: F811
    with engine.connect() as conn:
        return {table: sorted(map(repr, conn.execute(sa.text(f"SELECT * FROM {table}")).all()))
                for table in UPSTREAM_TABLES}


def _views(Sessions):  # noqa: N803
    with Sessions() as session:
        state = load_adaptation_state(session, user_id="u")
    return state, project_current_progress(state=state)


def _acquire(Sessions, state, progress, code="C7"):  # noqa: N803
    with Sessions() as session:
        result = acquire_progress_history(session, state=state, progress=progress, competency_code=code)
        assert not session.new and not session.dirty and not session.deleted
        return result


def _recorded(engine, fn):  # noqa: F811
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(" ".join(sql.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        return fn(), statements
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def _visible(history):
    from core.progress_history_projection import project_progress_history
    from core.progress_milestones import project_progress_milestones
    return [(m.milestone_kind, m.title, m.detail) for m in project_progress_history(
        milestones=project_progress_milestones(history=history)).milestones]


def test_pg_revision_by_new_evidence_end_to_end(Sessions, engine):  # noqa: F811
    """Application -> Application sous tension (maintained) -> Comprehension
    (revised_down, new_user_evidence) : la carte affiche Comprehension ;
    l'historique montre Application et une note de révision, jamais une
    perte."""
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    p.infer()
    p.t3(p.contra("C7_A", "C7_B", contradiction_scope="application", evidence_strength="strong"))
    p.infer()
    state, progress = _views(Sessions)
    assert progress.competencies[6].stage_code == "comprehension"
    before = _dump(engine)
    history, statements = _recorded(engine, lambda: _acquire(Sessions, state, progress))
    assert _dump(engine) == before
    assert statements and all(s.startswith(("SELECT", "WITH RECURSIVE")) and " FOR " not in s for s in statements)
    assert entries(history) == [
        ("history_1", None, "application", None, None),
        ("history_2", "application", "application", "maintained", "new_user_evidence"),
        ("history_3", "application", "comprehension", "revised_down", "new_user_evidence")]
    assert not UUID_PATTERN.search(repr(history))
    assert _acquire(Sessions, state, progress) == history
    assert _visible(history) == [
        ("stage_established", "Utilisé en situation", None),
        ("revision", "Nouvelle évaluation",
         "De nouveaux éléments observés ont conduit Oryx à réviser son estimation.")]


def test_pg_integrity_reset_keeps_the_historical_milestone(Sessions, engine):  # noqa: F811
    """L'unique preuve de l'Application initiale est invalidée : la carte
    affiche non_etabli ; l'ancienne Application reste un fait historique
    (aucune preuve relue), la révision est un ajustement Oryx."""
    from tests.test_inference_engine import Pipeline
    from tests.test_longitudinal_service import _invalidate
    p = Pipeline(Sessions)
    a = p.t3(p.app("C7_A", "C7_B", evidence_strength="strong")).id
    p.t3(p.app("C7_D", local_stage="none"))
    p.infer()
    _invalidate(Sessions, a)
    p.infer()
    state, progress = _views(Sessions)
    assert progress.competencies[6].stage_code == "non_etabli"
    history = _acquire(Sessions, state, progress)
    assert entries(history) == [
        ("history_1", None, "application", None, None),
        ("history_2", "application", "non_etabli", "revised_down", "evidence_integrity_change")]
    assert _visible(history) == [
        ("stage_established", "Utilisé en situation", None),
        ("revision", "Ajustement Oryx",
         "Une correction des éléments disponibles a conduit Oryx à ajuster son estimation.")]


def test_pg_rebound_after_revision_shows_no_revision(Sessions):  # noqa: F811
    """Application -> Comprehension -> Comprehension -> Application courant :
    l'état actuel a rattrapé l'histoire ; aucun jalon ne duplique la carte."""
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", "C7_B", contradiction_scope="application", evidence_strength="strong"))
    p.infer()
    p.t3(p.app("C7_D", local_stage="discovery"))
    p.infer()
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong", support_level="none"))
    p.infer()
    state, progress = _views(Sessions)
    assert progress.competencies[6].stage_code == "application"
    history = _acquire(Sessions, state, progress)
    assert [e.transition for e in history.entries] == [None, "revised_down", "maintained", "upgraded"]
    assert _visible(history) == []


def test_pg_absent_competency_issues_no_statement(Sessions, engine):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    state, progress = _views(Sessions)
    result, statements = _recorded(engine, lambda: _acquire(Sessions, state, progress, code="C8"))
    assert (result.entries, statements) == ((), [])


def test_pg_stale_before_a_newer_inference(Sessions):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    state, progress = _views(Sessions)
    p.t3(p.app("C7_A", "C7_C", evidence_strength="strong"))
    p.infer()
    with pytest.raises(StaleProgressHistory):
        _acquire(Sessions, state, progress)
    fresh_state, fresh_progress = _views(Sessions)
    assert len(_acquire(Sessions, fresh_state, fresh_progress).entries) == 2


def test_pg_stale_when_a_newer_inference_is_activated_during_acquisition(Sessions, monkeypatch):  # noqa: F811
    """R1 capturé et revalidé ; R2 activé et COMMITÉ par une autre session
    pendant la lecture de la lignée : la revalidation APRÈS désigne R2.
    Jamais de résultat, jamais de retry."""
    from tests.test_inference_engine import Pipeline
    from tests.test_longitudinal_service import _start as start_t5
    from tests.test_progress_evidence import _activate_r2
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    first, _ = p.infer()
    state, progress = _views(Sessions)
    p.t3(p.app("C7_A", "C7_C", evidence_strength="strong"))
    parent = start_t5(Sessions, p.release_id)
    activated, validations = [], []
    real_validated, real_lineage = ph.get_validated_user_competency_state, ph.get_inference_lineage

    def validated(db, *, user_id, competency_code):
        result = real_validated(db, user_id=user_id, competency_code=competency_code)
        validations.append(result.active_inference_run_id)
        return result

    def lineage(db, *, run_id):
        rows = real_lineage(db, run_id=run_id)
        activated.append(_activate_r2(Sessions, parent))
        return rows

    monkeypatch.setattr(ph, "get_validated_user_competency_state", validated)
    monkeypatch.setattr(ph, "get_inference_lineage", lineage)
    with pytest.raises(StaleProgressHistory, match="run T6 courant"):
        _acquire(Sessions, state, progress)
    assert validations == [first.run_id, activated[0]]
