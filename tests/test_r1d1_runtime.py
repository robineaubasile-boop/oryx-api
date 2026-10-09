"""Tests de R1-D1C : orchestration du runtime d'évaluation
(core/evaluation_runtime.py), fournisseur (core/evaluation_provider.py) et
worker interne (core/evaluation_worker.py).

1. Sans base : versions immuables, manifest / input_fingerprint /
   evaluation_dedup_key / output_fingerprint déterministes, vocabulaire
   fermé des failure_code, configuration stricte du worker (cutoff UTC
   explicite obligatoire, durées, marge lease / timeout), fournisseur sans
   retry ni paramètre implicite, frontières (aucun T5 / T6, aucune route,
   aucune écriture ORM directe).

2. Contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL vers une base DÉDIÉE
   dont le nom contient "test" ; sinon SKIPPÉS). Events produits par le VRAI
   parcours Décrypter, fournisseur LLM simulé (FakeProvider) :
   pipeline complet (D1B -> D1D -> persistance atomique), zéro observation,
   échecs (aucun résultat partiel), lease perdue pendant D1B / D1D,
   aucune transaction ni verrou pendant un appel fournisseur, un seul run
   initial logique (dont concurrence réelle), recovery du même run,
   taxonomie canonique (release active, release retired, changement de
   release en cours de run), cutoff d'admission, boucle du worker.

Depuis R1-D1F, les nouveaux runs sont V3 : les réponses simulées suivent le
contrat D1B V2 (stage_basis) et D1D V3 (prémisses analytiques par
capacité). Bundles et recovery des runs V1 / V2 legacy :
tests/test_r1d1e_runtime_bundles.py et tests/test_r1d1f_runtime_bundles.py.
"""
import ast
import dataclasses
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest
import sqlalchemy as sa

from core import evaluation_provider as ep
from core import evaluation_runtime as rt
from core import evaluation_worker as worker
from core import taxonomy_service as tax
from core.evaluation_input import sha256_hex
from core.pedagogy.taxonomy_v1 import EXPECTED_V1_FINGERPRINT
from tests.test_assistant_delivery import Sessions, engine, ext  # noqa: F401 — fixtures
from tests.test_decryptage_cognitive_runtime import _events
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_r1d1_evaluation_input import (  # noqa: F401 — fixture r1d1
    ANSWER_1,
    ANSWER_2,
    T3_TABLES,
    T5_T6_TABLES,
    UUID_PATTERN,
    bundle,
    counts,
    execute,
    finalized_event,
    r1d1,
)
from tests.test_r1d1_local_evaluator import _contradictory, _obs
from tests.test_r1d1e_local_evaluator_v2 import contradictory_v2, obs_v2

MODEL = "claude-test-evaluator"
LEASE = 300
# Contrat courant (V3) : stage_basis (stade dérivé V2 : comprehension, comme
# _obs() en V1) et prémisses D1D V3 des 4 capacités C7 (capability_1..4 =
# C7_A..C7_D quand seule C7 est observée).
D1B_ONE = {"observations": [obs_v2()]}
D1B_ZERO = {"observations": []}


def _d1d_v2(*supported, observation_token="observation_1", candidates=("capability_1", "capability_2",
                                                                         "capability_3", "capability_4")):
    return {"observation_token": observation_token, "capability_assessments": [
        {"capability_token": t, "supported": t in supported,
         "reason": "include_satisfied" if t in supported else "insufficient_specificity"}
        for t in reversed(candidates)]}


def _d1d_v3(*supported, observation_token="observation_1", candidates=("capability_1", "capability_2",
                                                                         "capability_3", "capability_4")):
    """Prémisses V3 : capacité de `supported` => définition satisfaite, include
    0 démontré, clear ; sinon rien de démontré, insufficient_specificity."""
    return {"observation_token": observation_token, "capability_assessments": [
        {"capability_token": t, "definition_satisfied": t in supported,
         "matched_include_indices": [0] if t in supported else [], "matched_exclude_indices": [],
         "boundary_status": "clear" if t in supported else "insufficient_specificity"}
        for t in reversed(candidates)]}


D1D_TWO = {"mappings": [_d1d_v3("capability_2", "capability_1")]}
D1D_COMPETENCY_ONLY = {"mappings": [_d1d_v3()]}
# Contrat D1D V2 (R1-D1E) : recovery des runs V2 legacy uniquement.
D1D_TWO_V2 = {"mappings": [_d1d_v2("capability_2", "capability_1")]}
RUNTIME_PATH = REPO_ROOT / "core" / "evaluation_runtime.py"
WORKER_PATH = REPO_ROOT / "core" / "evaluation_worker.py"
PROVIDER_PATH = REPO_ROOT / "core" / "evaluation_provider.py"
ENV = {
    "ORYX_T3_INITIAL_CUTOFF": "2026-10-07T20:00:00Z",
    "ORYX_EVALUATION_POLL_SECONDS": "30",
    "ORYX_EVALUATION_LEASE_SECONDS": "600",
    "ORYX_EVALUATION_LLM_TIMEOUT_SECONDS": "300",
    "ORYX_EVALUATION_MODEL": MODEL,
    "ANTHROPIC_API_KEY": "sk-test",
    "DATABASE_URL": "postgresql://test",
}


class FakeProvider:
    """Fournisseur simulé : chaque appel consomme la réponse suivante (str,
    dict sérialisé, exception levée, ou callable(request) -> l'un d'eux)."""

    def __init__(self, *responses, model_id=MODEL):
        self.model_id = model_id
        self.responses = list(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if callable(response):
            response = response(request)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, str) else json.dumps(response)


class Crash(BaseException):
    """Arrêt brutal du processus (jamais rattrapé par le runtime)."""


# --------------------------------------------------------------------------
# 1. Sans base — versions et fingerprints
# --------------------------------------------------------------------------

def test_pipeline_versions_are_explicit_and_frozen():
    """Nouveaux runs initiaux : versions V3 (R1-D1F) ; D1A et normalisation
    restent V1, le stade local reste V2 (sémantique inchangée)."""
    assert (rt.EVALUATION_INPUT_SCHEMA_VERSION, rt.NORMALIZATION_VERSION, rt.LOCAL_STAGE_VERSION,
            rt.CAPABILITY_MAPPING_VERSION, rt.EVALUATION_SCHEMA_VERSION, rt.EVALUATOR_VERSION,
            rt.PROMPT_SPEC_VERSION) == (
        "decryptage-evaluation-input-v1", "decryptage-normalization-v1", "decryptage-local-stage-v2",
        "decryptage-capability-mapping-v3", "decryptage-evaluation-schema-v3",
        "decryptage-local-evaluation-pipeline-v3", "decryptage-t3-prompt-bundle-v3")
    assert dict(rt.RUN_VERSIONS) == dict(rt.CURRENT_V3_BUNDLE.versions) == {
        "normalization_version": rt.NORMALIZATION_VERSION, "local_stage_version": rt.LOCAL_STAGE_VERSION,
        "capability_mapping_version": rt.CAPABILITY_MAPPING_VERSION,
        "evaluation_schema_version": rt.EVALUATION_SCHEMA_VERSION, "evaluator_version": rt.EVALUATOR_VERSION,
        "prompt_spec_version": rt.PROMPT_SPEC_VERSION}
    with pytest.raises(TypeError):
        rt.RUN_VERSIONS["evaluator_version"] = "x"
    assert rt.INITIAL_TRIGGER == "initial"


def _manifest(pipeline=rt.CURRENT_V3_BUNDLE, **overrides):
    values = {"source_fingerprint": "s" * 64, "taxonomy_version_key": "oryx-v1",
              "taxonomy_spec_fingerprint": EXPECTED_V1_FINGERPRINT, "model_id": MODEL}
    values.update(overrides)
    return rt.build_input_manifest(pipeline=pipeline, **values)


def test_input_manifest_is_exactly_the_contract_and_its_fingerprint_is_stable():
    manifest = _manifest()
    assert set(manifest) == {
        "evaluation_input_schema_version", "source_fingerprint", "taxonomy_version_key",
        "taxonomy_spec_fingerprint", "normalization_version", "local_stage_version", "capability_mapping_version",
        "evaluation_schema_version", "evaluator_version", "model_id", "prompt_spec_version"}
    fingerprint = rt.compute_input_fingerprint(manifest)
    assert fingerprint == rt.compute_input_fingerprint(dict(reversed(list(manifest.items()))))
    assert fingerprint == sha256_hex(manifest) and len(fingerprint) == 64
    # Aucun fingerprint de contexte supplémentaire : le spec_fingerprint T4-C suffit.
    assert not [k for k in manifest if "context" in k or "reference" in k]


@pytest.mark.parametrize("change", [
    {"source_fingerprint": "t" * 64}, {"taxonomy_spec_fingerprint": "0" * 64}, {"taxonomy_version_key": "oryx-v2"},
    {"model_id": "other-model"},
])
def test_input_fingerprint_changes_with_any_input(change):
    assert rt.compute_input_fingerprint(_manifest(**change)) != rt.compute_input_fingerprint(_manifest())


@pytest.mark.parametrize("name", ["evaluation_input_schema_version", *rt.RUN_VERSIONS])
def test_input_fingerprint_changes_with_any_pipeline_version(name):
    before = rt.compute_input_fingerprint(_manifest())
    current = rt.CURRENT_V3_BUNDLE
    if name == "evaluation_input_schema_version":
        changed = dataclasses.replace(current, evaluation_input_schema_version="next")
    else:
        changed = dataclasses.replace(current, versions=MappingProxyType({**current.versions, name: "next"}))
    assert rt.compute_input_fingerprint(_manifest(changed)) != before


def test_dedup_key_is_deterministic_per_trigger_event_and_input():
    event_id = uuid.uuid4()
    key = rt.compute_evaluation_dedup_key(event_id=event_id, input_fingerprint="f" * 64)
    assert key == sha256_hex({"trigger": "initial", "event_id": str(event_id), "input_fingerprint": "f" * 64})
    assert key == rt.compute_evaluation_dedup_key(event_id=event_id, input_fingerprint="f" * 64)
    assert key != rt.compute_evaluation_dedup_key(event_id=uuid.uuid4(), input_fingerprint="f" * 64)
    assert key != rt.compute_evaluation_dedup_key(event_id=event_id, input_fingerprint="e" * 64)


def _mapping(localization="localized", *codes):
    return {"observation_token": "observation_1", "capability_localization": localization,
            "capabilities": [{"capability_token": f"capability_{i}", "capability_code": code, "semantic_revision": 1}
                             for i, code in enumerate(codes, 1)]}


def test_output_fingerprint_is_semantic_canonical_and_stable():
    final = rt.build_final_result([_obs()], [_mapping("localized", "C7_A", "C7_B")])
    [observation] = final["observations"]
    assert observation["capabilities"] == ["C7_A@1", "C7_B@1"]
    assert observation["capability_localization"] == "localized"
    assert "observation_token" not in observation
    assert not UUID_PATTERN.search(json.dumps(final))
    assert rt.compute_output_fingerprint(final) == rt.compute_output_fingerprint(json.loads(json.dumps(final)))
    reordered = {"observations": [dict(sorted(observation.items(), reverse=True))]}
    assert rt.compute_output_fingerprint(reordered) == rt.compute_output_fingerprint(final)
    other = rt.build_final_result([_obs()], [_mapping("localized", "C7_A")])
    assert rt.compute_output_fingerprint(other) != rt.compute_output_fingerprint(final)


def test_zero_observation_output_fingerprint():
    assert rt.build_final_result([], []) == {"observations": []}
    assert rt.compute_output_fingerprint(rt.build_final_result([], [])) == sha256_hex({"observations": []})


def test_semantic_order_of_observations_is_part_of_the_fingerprint():
    first, second = _obs(), _contradictory(2)
    mappings = [_mapping("competency_only"), dict(_mapping("competency_only"), observation_token="observation_2")]
    a = rt.build_final_result([first, second], mappings)
    b = rt.build_final_result([dict(second, observation_token="observation_1"),
                               dict(first, observation_token="observation_2")], mappings)
    assert rt.compute_output_fingerprint(a) != rt.compute_output_fingerprint(b)


def test_failure_codes_are_a_closed_vocabulary_without_lost_lease():
    assert rt.FAILURE_CODES == ("invalid_evaluation_input", "provider_error", "provider_timeout",
                                "invalid_d1b_output", "invalid_d1d_output", "input_fingerprint_mismatch",
                                "internal_validation_error")
    assert "lost_lease" not in rt.FAILURE_CODES


# --------------------------------------------------------------------------
# 1. Sans base — frontières
# --------------------------------------------------------------------------

def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return imported | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}


def test_runtime_has_no_t5_t6_step6_route_or_llm_sdk():
    for path in (RUNTIME_PATH, WORKER_PATH):
        for module in _imports(path):
            assert not module.startswith(("core.longitudinal", "core.inference", "core.adaptation", "core.progress",
                                          "core.competency", "api", "fastapi", "anthropic")), (path, module)
        tokens = _code_tokens(path.read_text(encoding="utf-8"))
        for word in ("CompetencyInferenceRun", "UserCompetencyState", "LongitudinalAssessmentRun", "stage_claim",
                     "Step6", "step_6", "UserStatement", "AnalysisSession", "HTTPException"):
            assert word not in tokens, (path, word)
        lowered = set(tokens.lower().split("\n"))
        for word in ("xp", "score", "mastery", "confidence"):
            assert not [t for t in lowered if word in t.split("_")], (path, word)


def test_runtime_never_mutates_orm_columns_directly():
    """Toute mutation T3 passe par observation_service, tout mapping par
    taxonomy_service : aucune affectation d'attribut ni db.add / delete."""
    tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not any(isinstance(t, ast.Attribute) for t in targets), ast.unparse(node)
    tokens = set(_code_tokens(RUNTIME_PATH.read_text(encoding="utf-8")).split("\n"))
    for forbidden in ("add", "add_all", "delete", "merge", "flush", "with_for_update", "update", "insert"):
        assert forbidden not in tokens, forbidden
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in ("svc", "tax")}
    assert not [name for name in used if name.startswith("_")]


def test_api_and_web_never_reach_the_evaluation_runtime():
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("evaluation_runtime", "evaluation_worker", "evaluation_input", "local_evaluator",
                 "capability_mapper", "evaluation_provider"):
        assert name not in api, name
    for path in list((REPO_ROOT / "core").glob("*.py")):
        if path.name.startswith(("evaluation_", "local_evaluator", "capability_mapper")):
            continue
        for module in _imports(path):
            assert module not in ("core.evaluation_runtime", "core.evaluation_worker", "core.evaluation_provider",
                                  "core.evaluation_input", "core.local_evaluator", "core.capability_mapper"), path


def test_provider_is_explicit_without_retries_or_sampling_parameters():
    tokens = _code_tokens(PROVIDER_PATH.read_text(encoding="utf-8"))
    for word in ("temperature", "top_p", "top_k", "budget_tokens", "print"):
        assert word not in tokens, word
    assert ep.MAX_TOKENS == 16000


class _Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class _Response:
    def __init__(self, stop_reason="end_turn", content=None):
        self.stop_reason = stop_reason
        self.content = content if content is not None else [_Block("thinking"), _Block("text", '{"observ'),
                                                            _Block("text", 'ations": []}')]


def _fake_anthropic(monkeypatch, outcome):
    created = {}

    class Client:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.messages = self

        def create(self, **kwargs):
            created["request"] = kwargs
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(ep.anthropic, "Anthropic", Client)
    return created


def test_provider_calls_one_model_without_retry_and_returns_only_text(monkeypatch):
    created = _fake_anthropic(monkeypatch, _Response())
    provider = ep.AnthropicEvaluationProvider(model_id=MODEL, timeout_seconds=120, api_key="sk-test")
    from core.local_evaluator import EvaluatorRequest
    assert provider.complete(EvaluatorRequest(system="s", user="u")) == '{"observations": []}'
    assert (created["max_retries"], created["timeout"], created["api_key"]) == (0, 120.0, "sk-test")
    assert created["request"] == {"model": MODEL, "max_tokens": ep.MAX_TOKENS, "system": "s",
                                  "messages": [{"role": "user", "content": "u"}]}


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens", "pause_turn"])
def test_provider_refuses_truncated_or_refused_answers(monkeypatch, stop_reason):
    _fake_anthropic(monkeypatch, _Response(stop_reason=stop_reason))
    provider = ep.AnthropicEvaluationProvider(model_id=MODEL, timeout_seconds=120, api_key="sk-test")
    from core.local_evaluator import EvaluatorRequest
    with pytest.raises(rt.ProviderError) as failure:
        provider.complete(EvaluatorRequest(system="s", user="u"))
    assert not isinstance(failure.value, rt.ProviderTimeout)


def test_provider_maps_timeouts_and_api_errors(monkeypatch):
    import httpx2
    from core.local_evaluator import EvaluatorRequest
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    for outcome, expected in ((ep.anthropic.APITimeoutError(request=request), rt.ProviderTimeout),
                              (ep.anthropic.APIConnectionError(request=request), rt.ProviderError)):
        _fake_anthropic(monkeypatch, outcome)
        provider = ep.AnthropicEvaluationProvider(model_id=MODEL, timeout_seconds=120, api_key="sk-test")
        with pytest.raises(expected):
            provider.complete(EvaluatorRequest(system="secret-prompt", user="secret-user"))


# --------------------------------------------------------------------------
# 1. Sans base — configuration du worker
# --------------------------------------------------------------------------

def test_cutoff_must_be_an_explicit_utc_iso_datetime():
    assert worker.parse_cutoff("2026-10-07T20:00:00Z") == datetime(2026, 10, 7, 20, tzinfo=timezone.utc)
    assert worker.parse_cutoff("2026-10-07T20:00:00+00:00") == datetime(2026, 10, 7, 20, tzinfo=timezone.utc)
    for raw in (None, "", "   ", "2026-10-07", "2026-10-07T20:00:00", "2026-10-07T22:00:00+02:00", "now", "0",
                "1970-01-01", "demain", 1760000000):
        with pytest.raises(worker.WorkerConfigError):
            worker.parse_cutoff(raw)


def test_config_requires_every_setting_explicitly():
    config = worker.load_config(ENV)
    assert (config.poll_seconds, config.lease_seconds, config.llm_timeout_seconds, config.model_id) == (
        30, 600, 300, MODEL)
    for name in ENV:
        with pytest.raises(worker.WorkerConfigError, match=name):
            worker.load_config({k: v for k, v in ENV.items() if k != name})


@pytest.mark.parametrize("name, value", [
    ("ORYX_EVALUATION_POLL_SECONDS", "0"), ("ORYX_EVALUATION_POLL_SECONDS", "1.5"),
    ("ORYX_EVALUATION_POLL_SECONDS", "-3"), ("ORYX_EVALUATION_POLL_SECONDS", "abc"),
    ("ORYX_EVALUATION_LEASE_SECONDS", "59"), ("ORYX_EVALUATION_LEASE_SECONDS", "359"),
    ("ORYX_EVALUATION_LLM_TIMEOUT_SECONDS", "5"), ("ORYX_EVALUATION_LLM_TIMEOUT_SECONDS", "541"),
    ("ORYX_T3_INITIAL_CUTOFF", "2026-10-07T20:00:00"), ("ORYX_EVALUATION_MODEL", "  "),
])
def test_invalid_settings_refuse_to_start(name, value):
    with pytest.raises(worker.WorkerConfigError):
        worker.load_config({**ENV, name: value})


def test_worker_refuses_to_start_without_cutoff_before_touching_the_database(monkeypatch, caplog):
    monkeypatch.setattr(worker, "run_forever", lambda *a: pytest.fail("ne doit pas démarrer"))
    environ = {k: v for k, v in ENV.items() if k != "ORYX_T3_INITIAL_CUTOFF"}
    with caplog.at_level(logging.ERROR):
        assert worker.main(environ) == 2
    assert "worker_refused_to_start" in caplog.text and "sk-test" not in caplog.text


def test_worker_entrypoints_exist():
    assert "python -m core.evaluation_worker" in WORKER_PATH.read_text(encoding="utf-8")
    script = (REPO_ROOT / "scripts" / "run_evaluation_worker.py").read_text(encoding="utf-8")
    assert "from core.evaluation_worker import main" in script
    doc = (REPO_ROOT / "docs" / "evaluation_worker.md").read_text(encoding="utf-8")
    for name in (*ENV, "oryx-evaluation-worker", "python -m core.evaluation_worker"):
        assert name in doc, name


# --------------------------------------------------------------------------
# 2. PostgreSQL — helpers
# --------------------------------------------------------------------------

def _process(Sessions, event_id, provider):  # noqa: F811
    return rt.process_new_event(Sessions, event_id=event_id, provider=provider, lease_seconds=LEASE)


def _recover(Sessions, run_id, provider):  # noqa: F811
    return rt.process_recovery_run(Sessions, run_id=run_id, provider=provider, lease_seconds=LEASE)


def _runs(engine, event_id=None):  # noqa: F811
    query = "SELECT * FROM observation_evaluation_runs"
    params = {}
    if event_id is not None:
        query, params = query + " WHERE event_id = :e", {"e": event_id}
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(query + " ORDER BY created_at"), params).mappings()]


def _observations(engine, run_id):  # noqa: F811
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(
            "SELECT * FROM pedagogical_observations WHERE evaluation_run_id = :r ORDER BY ordinal"),
            {"r": run_id}).mappings()]


def _mappings(engine, run_id):  # noqa: F811
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT o.ordinal, d.capability_code, d.semantic_revision, d.competency_code, m.taxonomy_release_id "
            "FROM observation_capabilities oc JOIN pedagogical_observations o ON o.id = oc.observation_id "
            "JOIN capability_taxonomy_memberships m ON m.id = oc.capability_membership_id "
            "JOIN core_capability_definitions d ON d.id = m.capability_definition_id "
            "WHERE o.evaluation_run_id = :r ORDER BY o.ordinal, d.capability_code"), {"r": run_id})]


def _release_id(engine, version_key="oryx-v1"):  # noqa: F811
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT id FROM pedagogical_taxonomy_releases WHERE version_key = :k"),
                            {"k": version_key}).scalar_one()


def _expire(engine, run_id):  # noqa: F811
    execute(engine, "UPDATE observation_evaluation_runs SET lease_expires_at = clock_timestamp() "
            "- interval '1 second' WHERE id = :id", id=run_id)


def _activate_other_release(Sessions):  # noqa: F811
    """Une autre release devient active : oryx-v1 passe retired."""
    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key="oryx-test-v2", spec_fingerprint="f" * 64)
        tax.activate_release(session, release_id=release.id)
        session.commit()


def _crashed_run(Sessions, engine, event_id):  # noqa: F811
    """Le worker meurt pendant D1B (aucun fail) ; sa lease expire."""
    with pytest.raises(Crash):
        _process(Sessions, event_id, FakeProvider(Crash()))
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["interpretation_status"]) == ("running", "candidate")
    assert run["lease_token"] is not None
    _expire(engine, run["id"])
    return run


def _assert_nothing_held(engine):  # noqa: F811
    """Pendant un appel fournisseur : aucune transaction ouverte, aucun
    verrou de ligne détenu (vérifié depuis une connexion indépendante)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        idle = conn.execute(sa.text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()")).scalar_one()
        assert idle == 0
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT id FROM observation_evaluation_runs FOR UPDATE NOWAIT")).all()
        conn.execute(sa.text("SELECT id FROM cognitive_events FOR UPDATE NOWAIT")).all()
        conn.rollback()


# --------------------------------------------------------------------------
# 2. PostgreSQL — pipeline
# --------------------------------------------------------------------------

def test_pg_full_pipeline_one_c7_observation_localized_on_two_capabilities(engine, Sessions, ext, r1d1, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    expected_bundle = bundle(Sessions, event_id)
    provider = FakeProvider(D1B_ONE, D1D_TWO)
    with caplog.at_level(logging.INFO, logger="core.evaluation_runtime"):
        assert _process(Sessions, event_id, provider) == rt.COMPLETED

    [run] = _runs(engine, event_id)
    release = _release_id(engine)
    assert (run["execution_status"], run["interpretation_status"], run["trigger"]) == ("completed", "active",
                                                                                       "initial")
    assert (run["lease_token"], run["lease_expires_at"], run["failure_code"]) == (None, None, None)
    assert run["pedagogical_taxonomy_release_id"] == release and run["model_id"] == MODEL
    assert {k: run[k] for k in rt.RUN_VERSIONS} == dict(rt.RUN_VERSIONS)
    manifest = rt.build_input_manifest(pipeline=rt.CURRENT_V3_BUNDLE,
                                       source_fingerprint=expected_bundle.source_fingerprint,
                                       taxonomy_version_key="oryx-v1",
                                       taxonomy_spec_fingerprint=EXPECTED_V1_FINGERPRINT, model_id=MODEL)
    assert run["input_fingerprint"] == rt.compute_input_fingerprint(manifest)
    assert run["evaluation_dedup_key"] == rt.compute_evaluation_dedup_key(event_id=event_id,
                                                                          input_fingerprint=run["input_fingerprint"])
    candidates = [_obs()]
    mapping = {"observation_token": "observation_1", "capability_localization": "localized",
               "capabilities": [{"capability_token": "capability_1", "capability_code": "C7_A",
                                 "semantic_revision": 1},
                                {"capability_token": "capability_2", "capability_code": "C7_B",
                                 "semantic_revision": 1}]}
    assert run["output_fingerprint"] == rt.compute_output_fingerprint(rt.build_final_result(candidates, [mapping]))

    [observation] = _observations(engine, run["id"])
    assert (observation["competency_code"], observation["polarity"], observation["local_stage"],
            observation["capability_localization"], observation["integrity_status"]) == (
        "C7", "supportive", "comprehension", "localized", "valid")
    assert observation["observation_text"] == _obs()["observation_text"]
    contribution_2 = expected_bundle.contribution_ids["contribution_2"]
    assert observation["source_contribution_refs"] == [
        {"contribution_token": "contribution_2", "contribution_id": contribution_2, "phase": 2}]
    assert observation["primary_user_action"] == {"action": "connect", "contribution_token": "contribution_2",
                                                  "contribution_id": contribution_2}
    assert observation["residual_cognitive_work"]["materially_used_support_refs"] == [
        {"support_token": "support_1", "support_trace_id": expected_bundle.support_ids["support_1"],
         "contribution_token": "contribution_2", "contribution_id": contribution_2}]
    # UNE observation, deux localisations, même release, même compétence.
    assert _mappings(engine, run["id"]) == [(1, "C7_A", 1, "C7", release), (1, "C7_B", 1, "C7", release)]

    # D1B puis D1D batch, même modèle ; le payload LLM n'a aucun UUID interne.
    assert len(provider.requests) == 2
    d1b, d1d = provider.requests
    assert json.loads(d1b.user)["evaluation_input"] == expected_bundle.evaluator_payload
    for request in provider.requests:
        assert not UUID_PATTERN.search(request.user)
    assert {e["capability_code"] for e in json.loads(d1d.user)["capability_reference"]} == {
        "C7_A", "C7_B", "C7_C", "C7_D"}
    # STOP après complete : aucun T5 / T6, aucune mutation T2.
    assert set(counts(engine, T5_T6_TABLES).values()) == {0}
    assert [e["status"] for e in _events(engine)] == ["finalized", "open"]
    for line in ("[R1-D1] run_started", "[R1-D1] lease_claimed", "[R1-D1] d1b_completed",
                 "observations=1", "[R1-D1] lease_renewed", "localized=1 competency_only=0",
                 "[R1-D1] run_completed"):
        assert line in caplog.text, line
    for secret in (ANSWER_1, ANSWER_2, "Réponse business", _obs()["observation_text"], "BFR"):
        assert secret not in caplog.text, secret


def test_pg_zero_observation_is_a_completed_run_without_d1d(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    provider = FakeProvider(D1B_ZERO)
    assert _process(Sessions, event_id, provider) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["interpretation_status"]) == ("completed", "active")
    assert run["output_fingerprint"] == sha256_hex({"observations": []})
    assert (run["lease_token"], run["lease_expires_at"]) == (None, None)
    assert _observations(engine, run["id"]) == [] and _mappings(engine, run["id"]) == []
    assert len(provider.requests) == 1


def test_pg_competency_only_and_contradictory_observations(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    contradiction = contradictory_v2(2)
    d1d = {"mappings": [_d1d_v3(), _d1d_v3("capability_3", observation_token="observation_2")]}
    assert _process(Sessions, event_id, FakeProvider({"observations": [obs_v2(), contradiction]}, d1d)) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    first, second = _observations(engine, run["id"])
    assert (first["capability_localization"], second["capability_localization"]) == ("competency_only", "localized")
    assert (second["polarity"], second["local_stage"], second["contradiction_scope"]) == (
        "contradictory", None, "comprehension")
    assert _mappings(engine, run["id"]) == [(2, "C7_C", 1, "C7", _release_id(engine))]


# --------------------------------------------------------------------------
# 2. PostgreSQL — échecs (aucun résultat partiel)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("responses, code, calls", [
    ([rt.ProviderError("http 500")], "provider_error", 1),
    ([rt.ProviderTimeout("timeout")], "provider_timeout", 1),
    (["not json"], "invalid_d1b_output", 1),
    (['```json\n{"observations": []}\n```'], "invalid_d1b_output", 1),
    ([{"observations": [dict(obs_v2(), local_stage="mastery")]}], "invalid_d1b_output", 1),
    # Contrat V1 (stade choisi par le modèle) refusé pour un run V3.
    ([{"observations": [_obs()]}], "invalid_d1b_output", 1),
    ([{"observations": [obs_v2(stage_basis={**obs_v2()["stage_basis"], "contextualized_use": False,
                                            "substantive_selection_adaptation_interpretation": True})]}],
     "invalid_d1b_output", 1),
    ([D1B_ONE, rt.ProviderError("http 529")], "provider_error", 2),
    ([D1B_ONE, {"mappings": []}], "invalid_d1d_output", 2),
    ([D1B_ONE, {"mappings": [_d1d_v3(candidates=("capability_1", "capability_2", "capability_3",
                                                 "capability_9"))]}], "invalid_d1d_output", 2),
    ([D1B_ONE, {"mappings": [_d1d_v3(candidates=("capability_1", "capability_2", "capability_3"))]}],
     "invalid_d1d_output", 2),
    # Contrat D1D V2 (supported / reason choisis par le modèle) refusé pour un run V3.
    ([D1B_ONE, D1D_TWO_V2], "invalid_d1d_output", 2),
    # Contrat V1 (localisation choisie par le modèle) refusé pour un run V3.
    ([D1B_ONE, {"mappings": [{"observation_token": "observation_1", "localization": "localized",
                              "capability_tokens": ["capability_2"]}]}], "invalid_d1d_output", 2),
])
def test_pg_failures_leave_a_failed_run_and_no_partial_result(engine, Sessions, ext, r1d1, responses, code, calls):
    event_id = finalized_event(Sessions, ext, engine)
    provider = FakeProvider(*responses)
    assert _process(Sessions, event_id, provider) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["interpretation_status"], run["failure_code"]) == ("failed", "obsolete",
                                                                                            code)
    assert (run["lease_token"], run["output_fingerprint"]) == (None, None)
    assert _observations(engine, run["id"]) == [] and counts(engine, ("observation_capabilities",)) == {
        "observation_capabilities": 0}
    assert len(provider.requests) == calls


def test_pg_unexpected_provider_or_validation_errors_fail_the_run_instead_of_looping(engine, Sessions, ext, r1d1,
                                                                                     monkeypatch):
    """Une exception inattendue du fournisseur ou du validateur échoue le
    run (lease-aware) : jamais un run laissé en boucle de recovery."""
    first = finalized_event(Sessions, ext, engine)
    assert _process(Sessions, first, FakeProvider(RuntimeError("sdk"))) == rt.FAILED
    assert _runs(engine, first)[0]["failure_code"] == "provider_error"
    second = finalized_event(Sessions, ext, engine, conv="conv-c", ticker="NVDA")

    def broken(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr(rt, "CURRENT_V3_BUNDLE", dataclasses.replace(rt.CURRENT_V3_BUNDLE, validate_d1b=broken))
    assert _process(Sessions, second, FakeProvider(D1B_ONE)) == rt.FAILED
    assert _runs(engine, second)[0]["failure_code"] == "internal_validation_error"


def test_pg_persistence_failure_rolls_back_everything_then_fails_the_run(engine, Sessions, ext, r1d1, monkeypatch):
    event_id = finalized_event(Sessions, ext, engine)
    original = tax.map_observation_capability
    calls = []

    def flaky(db, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise tax.InvalidCapabilityMapping("simulated")
        return original(db, **kwargs)

    monkeypatch.setattr(tax, "map_observation_capability", flaky)
    assert _process(Sessions, event_id, FakeProvider(D1B_ONE, D1D_TWO)) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["failure_code"]) == ("failed", "internal_validation_error")
    assert _observations(engine, run["id"]) == [] and _mappings(engine, run["id"]) == []


def test_pg_a_failed_initial_run_is_never_retried_automatically(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    assert _process(Sessions, event_id, FakeProvider(rt.ProviderError("x"))) == rt.FAILED
    with Sessions() as session:
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert rt.discover_new_events(session, cutoff=cutoff, limit=10) == []
        assert rt.discover_recovery_runs(session, limit=10) == []
    provider = FakeProvider()
    assert _process(Sessions, event_id, provider) == rt.ALREADY_HANDLED
    assert provider.requests == [] and len(_runs(engine, event_id)) == 1


# --------------------------------------------------------------------------
# 2. PostgreSQL — aucune transaction pendant un appel fournisseur, leases
# --------------------------------------------------------------------------

def test_pg_no_transaction_nor_lock_is_held_during_provider_calls(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)

    def checked(response):
        def call(request):
            _assert_nothing_held(engine)
            return response
        return call

    assert _process(Sessions, event_id, FakeProvider(checked(D1B_ONE), checked(D1D_TWO))) == rt.COMPLETED


def test_pg_lease_lost_during_d1b_the_old_worker_mutates_nothing(engine, Sessions, ext, r1d1, caplog):
    """A claim, D1B lent ; sa lease expire ; B reprend le MÊME run et le
    termine. A revient : ni renew, ni fail, ni persist."""
    event_id = finalized_event(Sessions, ext, engine)
    other = {"observations": [contradictory_v2()]}
    worker_b = FakeProvider(other, D1D_COMPETENCY_ONLY)

    def slow_d1b(request):
        [run] = _runs(engine, event_id)
        _expire(engine, run["id"])
        assert _recover(Sessions, run["id"], worker_b) == rt.COMPLETED
        return D1B_ONE

    worker_a = FakeProvider(slow_d1b, D1D_TWO)
    with caplog.at_level(logging.INFO, logger="core.evaluation_runtime"):
        assert _process(Sessions, event_id, worker_a) == rt.LEASE_LOST
    assert len(worker_a.requests) == 1  # aucun D1D pour A
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["interpretation_status"]) == ("completed", "active")
    [observation] = _observations(engine, run["id"])
    assert observation["polarity"] == "contradictory"  # le résultat de B, jamais celui de A
    assert "[R1-D1] lease_lost" in caplog.text and "[R1-D1] recovery" in caplog.text


def test_pg_lease_lost_during_d1b_then_provider_error_never_fails_the_taken_over_run(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    taken = {}

    def slow_failure(request):
        [run] = _runs(engine, event_id)
        _expire(engine, run["id"])
        with Sessions() as session:
            taken["token"] = rt.svc.claim_evaluation_lease(session, run_id=run["id"],
                                                           lease_seconds=LEASE).lease_token
            session.commit()
        return rt.ProviderError("late failure")

    assert _process(Sessions, event_id, FakeProvider(slow_failure)) == rt.LEASE_LOST
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["failure_code"], run["lease_token"]) == ("running", None, taken["token"])


def test_pg_lease_lost_during_d1d_the_old_worker_cannot_overwrite(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    worker_b = FakeProvider(D1B_ZERO)

    def slow_d1d(request):
        [run] = _runs(engine, event_id)
        _expire(engine, run["id"])
        assert _recover(Sessions, run["id"], worker_b) == rt.COMPLETED
        return D1D_TWO

    assert _process(Sessions, event_id, FakeProvider(D1B_ONE, slow_d1d)) == rt.LEASE_LOST
    [run] = _runs(engine, event_id)
    assert run["output_fingerprint"] == sha256_hex({"observations": []})
    assert _observations(engine, run["id"]) == []


# --------------------------------------------------------------------------
# 2. PostgreSQL — un seul run initial logique
# --------------------------------------------------------------------------

def test_pg_two_workers_on_the_same_event_make_one_run_and_one_evaluation(engine, Sessions, ext, r1d1, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    worker_b = FakeProvider()
    seen = {}

    def d1b(request):
        # B découvre le même event pendant que A évalue : même clé, doublon
        # traité proprement, aucun appel fournisseur ; la lease valide de A
        # exclut le run de la recovery.
        seen["b"] = _process(Sessions, event_id, worker_b)
        with Sessions() as session:
            seen["recovery"] = rt.discover_recovery_runs(session, limit=10)
            seen["new"] = rt.discover_new_events(session, cutoff=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=10)
        [run] = _runs(engine, event_id)
        seen["b_recover"] = _recover(Sessions, run["id"], worker_b)
        return D1B_ZERO

    with caplog.at_level(logging.INFO, logger="core.evaluation_runtime"):
        assert _process(Sessions, event_id, FakeProvider(d1b)) == rt.COMPLETED
    assert seen == {"b": rt.ALREADY_HANDLED, "recovery": [], "new": [], "b_recover": rt.ALREADY_HANDLED}
    assert worker_b.requests == [] and len(_runs(engine, event_id)) == 1
    assert "[R1-D1] initial_run_exists" in caplog.text


def test_pg_a_worker_with_another_configuration_never_creates_a_second_initial_run(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    assert _process(Sessions, event_id, FakeProvider(D1B_ZERO)) == rt.COMPLETED
    other = FakeProvider(model_id="another-model")
    assert _process(Sessions, event_id, other) == rt.ALREADY_HANDLED
    assert other.requests == [] and len(_runs(engine, event_id)) == 1


def test_pg_concurrent_workers_with_different_configurations_serialize_on_the_event(engine, Sessions, ext, r1d1):
    """Deux workers (clés de dédup différentes) démarrent réellement en même
    temps : le verrou de l'event les sérialise, le second voit le run initial
    du premier et n'évalue rien."""
    event_id = finalized_event(Sessions, ext, engine)
    providers = [FakeProvider(D1B_ZERO, model_id="model-a"), FakeProvider(D1B_ZERO, model_id="model-b")]
    outcomes = {}
    holder = Sessions()
    holder.execute(sa.text("SELECT id FROM cognitive_events WHERE id = :id FOR UPDATE"), {"id": event_id})

    def work(provider):
        try:
            outcomes[provider.model_id] = _process(Sessions, event_id, provider)
        except Exception as exc:  # noqa: BLE001 — restitué au test
            outcomes[provider.model_id] = exc

    threads = [threading.Thread(target=work, args=(p,)) for p in providers]
    try:
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        # AUTOCOMMIT : pg_stat_activity est figé pour la durée d'une transaction.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            while conn.execute(sa.text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND datname = current_database()")).scalar_one() < 2:
                assert time.monotonic() < deadline, "les deux workers n'attendent pas le verrou de l'event"
                time.sleep(0.005)
        holder.commit()
    finally:
        holder.close()
        for thread in threads:
            thread.join(timeout=20)
    assert sorted(outcomes.values()) == sorted([rt.COMPLETED, rt.ALREADY_HANDLED])
    assert sum(len(p.requests) for p in providers) == 1
    [run] = _runs(engine, event_id)
    assert run["execution_status"] == "completed"


# --------------------------------------------------------------------------
# 2. PostgreSQL — recovery
# --------------------------------------------------------------------------

def test_pg_crash_then_recovery_resumes_the_same_run(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_run(Sessions, engine, event_id)
    with Sessions() as session:
        assert rt.discover_recovery_runs(session, limit=10) == [crashed["id"]]
    assert _recover(Sessions, crashed["id"], FakeProvider(D1B_ONE, D1D_TWO)) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert run["id"] == crashed["id"] and run["input_fingerprint"] == crashed["input_fingerprint"]
    assert (run["execution_status"], run["lease_token"]) == ("completed", None)
    assert len(_mappings(engine, run["id"])) == 2


def test_pg_recovery_skips_a_run_whose_lease_is_still_valid(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    with pytest.raises(Crash):
        _process(Sessions, event_id, FakeProvider(Crash()))
    [run] = _runs(engine, event_id)
    with Sessions() as session:
        assert rt.discover_recovery_runs(session, limit=10) == []
    provider = FakeProvider()
    assert _recover(Sessions, run["id"], provider) == rt.ALREADY_HANDLED
    assert provider.requests == [] and _runs(engine, event_id) == [run]


@pytest.mark.parametrize("column, value", [("evaluator_version", "decryptage-local-evaluation-pipeline-v0"),
                                           ("prompt_spec_version", "other"), ("normalization_version", "other"),
                                           ("local_stage_version", "other"), ("capability_mapping_version", "other"),
                                           ("evaluation_schema_version", "other"), ("trigger", "manual"),
                                           ("model_id", "another-model")])
def test_pg_recovery_of_an_unknown_version_mutates_nothing(engine, Sessions, ext, r1d1, column, value):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_run(Sessions, engine, event_id)
    execute(engine, f"UPDATE observation_evaluation_runs SET {column} = :v WHERE id = :id", v=value, id=crashed["id"])
    [before] = _runs(engine, event_id)
    provider = FakeProvider()
    with pytest.raises(rt.UnsupportedEvaluationRunVersion):
        _recover(Sessions, crashed["id"], provider)
    assert _runs(engine, event_id) == [before] and provider.requests == []


def test_pg_recovery_under_a_retired_release_stays_on_the_run_release(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_run(Sessions, engine, event_id)
    v1 = _release_id(engine)
    _activate_other_release(Sessions)
    assert _recover(Sessions, crashed["id"], FakeProvider(D1B_ONE, D1D_TWO)) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert run["pedagogical_taxonomy_release_id"] == v1
    assert {row[4] for row in _mappings(engine, run["id"])} == {v1}


def test_pg_active_release_changing_mid_run_does_not_change_the_run(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    v1 = _release_id(engine)

    def d1b(request):
        _activate_other_release(Sessions)
        return D1B_ONE

    assert _process(Sessions, event_id, FakeProvider(d1b, D1D_TWO)) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert run["pedagogical_taxonomy_release_id"] == v1
    assert {row[4] for row in _mappings(engine, run["id"])} == {v1}


def test_pg_recovery_with_a_mismatching_input_fingerprint_fails_closed(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_run(Sessions, engine, event_id)
    execute(engine, "UPDATE observation_evaluation_runs SET input_fingerprint = :f WHERE id = :id", f="0" * 64,
            id=crashed["id"])
    provider = FakeProvider()
    assert _recover(Sessions, crashed["id"], provider) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["failure_code"], run["lease_token"]) == (
        "failed", "input_fingerprint_mismatch", None)
    assert provider.requests == []


# --------------------------------------------------------------------------
# 2. PostgreSQL — taxonomie canonique
# --------------------------------------------------------------------------

def test_pg_canonical_taxonomy_is_verified_before_any_run(engine, Sessions, ext, r1d1):
    with Sessions() as session:
        context = rt.load_active_taxonomy(session)
    assert (context.version_key, context.spec_fingerprint, context.release_id) == (
        "oryx-v1", EXPECTED_V1_FINGERPRINT, _release_id(engine))
    assert len(context.memberships) == 45 and len(context.competency_reference) == 12


def test_pg_no_run_starts_without_the_active_oryx_v1(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    _activate_other_release(Sessions)
    with pytest.raises(rt.TaxonomyMismatch):
        _process(Sessions, event_id, FakeProvider())
    execute(engine, "UPDATE pedagogical_taxonomy_releases SET status = 'retired' WHERE status = 'active'")
    with pytest.raises(rt.TaxonomyUnavailable):
        _process(Sessions, event_id, FakeProvider())
    assert _runs(engine, event_id) == []


def test_pg_a_drifted_persisted_taxonomy_refuses_to_start(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, "UPDATE core_capability_definitions SET label = label || ' ' WHERE capability_code = 'C7_A'")
    try:
        with pytest.raises(rt.TaxonomyMismatch):
            _process(Sessions, event_id, FakeProvider())
        assert _runs(engine, event_id) == []
    finally:
        execute(engine, "UPDATE core_capability_definitions SET label = rtrim(label) WHERE capability_code = 'C7_A'")


# --------------------------------------------------------------------------
# 2. PostgreSQL — cutoff d'admission et boucle du worker
# --------------------------------------------------------------------------

def _closed_at(engine, event_id):  # noqa: F811
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT closed_at FROM cognitive_events WHERE id = :id"),
                            {"id": event_id}).scalar_one()


def test_pg_cutoff_admits_only_events_closed_at_or_after_it(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    closed = _closed_at(engine, event_id)
    with Sessions() as session:
        assert rt.discover_new_events(session, cutoff=closed + timedelta(microseconds=1), limit=10) == []
        assert rt.discover_new_events(session, cutoff=closed, limit=10) == [event_id]
        assert rt.discover_new_events(session, cutoff=closed - timedelta(days=1), limit=10) == [event_id]
        assert rt.discover_new_events(session, cutoff=closed, limit=10, exclude={event_id}) == []


@pytest.mark.parametrize("statement", [
    "UPDATE cognitive_events SET status = 'abandoned' WHERE id = :id",
    "UPDATE cognitive_events SET event_origin = 'education' WHERE id = :id",
    "UPDATE cognitive_events SET event_builder_version = 'decryptage-event-builder-v0' WHERE id = :id",
    "UPDATE cognitive_events SET event_schema_version = NULL, event_dedup_key = NULL WHERE id = :id",
])
def test_pg_ineligible_events_are_never_discovered(engine, Sessions, ext, r1d1, statement):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, statement, id=event_id)
    with Sessions() as session:
        found = rt.discover_new_events(session, cutoff=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=10)
    assert found == []  # l'event open (moat) n'est jamais découvert non plus


def _config(cutoff):
    return worker.WorkerConfig(cutoff=cutoff, poll_seconds=1, lease_seconds=LEASE, llm_timeout_seconds=60,
                               model_id=MODEL)


def test_pg_worker_cycle_evaluates_eligible_events_once(engine, Sessions, ext, r1d1, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    state = worker.WorkerState(excluded_events=set(), excluded_runs=set())
    provider = FakeProvider(D1B_ZERO)
    stop = threading.Event()
    config = _config(_closed_at(engine, event_id))
    with caplog.at_level(logging.INFO, logger="core.evaluation_runtime"):
        assert worker.run_once(Sessions, config, provider, state, stop) == 1
        assert worker.run_once(Sessions, config, provider, state, stop) == 0
    assert f"[R1-D1] discovered event={event_id}" in caplog.text
    [run] = _runs(engine, event_id)
    assert run["execution_status"] == "completed" and len(provider.requests) == 1


def test_pg_worker_ignores_events_before_the_cutoff(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    state = worker.WorkerState(excluded_events=set(), excluded_runs=set())
    config = _config(_closed_at(engine, event_id) + timedelta(seconds=1))
    assert worker.run_once(Sessions, config, FakeProvider(), state, threading.Event()) == 0
    assert _runs(engine, event_id) == []


def test_pg_worker_excludes_invalid_inputs_and_unsupported_runs_without_any_write(engine, Sessions, ext, r1d1,
                                                                                    caplog):
    event_id = finalized_event(Sessions, ext, engine)
    other_id = finalized_event(Sessions, ext, engine, conv="conv-c", ticker="NVDA")
    execute(engine, "UPDATE cognitive_events SET stimulus_snapshot = '{}'::jsonb WHERE id = :id", id=event_id)
    crashed = _crashed_run(Sessions, engine, other_id)
    execute(engine, "UPDATE observation_evaluation_runs SET evaluator_version = 'old' WHERE id = :id",
            id=crashed["id"])
    state = worker.WorkerState(excluded_events=set(), excluded_runs=set())
    config = _config(datetime(2000, 1, 1, tzinfo=timezone.utc))
    with caplog.at_level(logging.INFO, logger="core.evaluation_runtime"):
        worker.run_once(Sessions, config, FakeProvider(), state, threading.Event())
    assert state.excluded_events == {event_id} and state.excluded_runs == {crashed["id"]}
    assert _runs(engine, event_id) == []
    [run] = _runs(engine, other_id)
    assert run["execution_status"] == "running" and run["evaluator_version"] == "old"
    assert "[R1-D1] not_evaluable" in caplog.text and "[R1-D1] unsupported_version" in caplog.text
    assert worker.run_once(Sessions, config, FakeProvider(), state, threading.Event()) == 0


def test_pg_worker_never_blocks_its_queue_on_an_unexpected_item_error(engine, Sessions, ext, r1d1, monkeypatch,
                                                                       caplog):
    event_id = finalized_event(Sessions, ext, engine)

    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(worker, "process_new_event", broken)
    state = worker.WorkerState(excluded_events=set(), excluded_runs=set())
    with caplog.at_level(logging.INFO, logger="core.evaluation_runtime"):
        worker.run_once(Sessions, _config(datetime(2000, 1, 1, tzinfo=timezone.utc)), FakeProvider(), state,
                        threading.Event())
    assert state.excluded_events == {event_id} and "[R1-D1] event_error" in caplog.text


def test_pg_worker_does_not_start_runs_without_taxonomy(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    _activate_other_release(Sessions)
    state = worker.WorkerState(excluded_events=set(), excluded_runs=set())
    worker.run_once(Sessions, _config(datetime(2000, 1, 1, tzinfo=timezone.utc)), FakeProvider(), state,
                    threading.Event())
    assert _runs(engine, event_id) == [] and state.excluded_events == set()


def test_pg_run_forever_stops_cleanly(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    stop = threading.Event()
    provider = FakeProvider(lambda request: (stop.set(), D1B_ZERO)[1])
    worker.run_forever(Sessions, _config(datetime(2000, 1, 1, tzinfo=timezone.utc)), provider, stop)
    [run] = _runs(engine, event_id)
    assert run["execution_status"] == "completed"


def test_pg_check_schema_requires_0014(engine):
    worker.check_schema(engine)
    with pytest.raises(worker.WorkerConfigError):
        class _Columns:
            def get_columns(self, table):
                return [{"name": "id"}]
        original = worker.inspect
        worker.inspect = lambda e: _Columns()
        try:
            worker.check_schema(engine)
        finally:
            worker.inspect = original


def test_pg_decryptage_flow_alone_still_writes_no_t3(engine, Sessions, ext, r1d1):
    """R1-C : le tour utilisateur Décrypter n'évalue jamais rien ; seul le
    worker crée des runs."""
    finalized_event(Sessions, ext, engine)
    assert set(counts(engine, T3_TABLES + T5_T6_TABLES).values()) == {0}
