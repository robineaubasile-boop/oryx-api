"""Tests de R1-D1E : bundles d'exécution versionnés et recovery V1 / V2
(core/evaluation_runtime.py).

1. Sans base : LEGACY_V1_BUNDLE et CURRENT_V2_BUNDLE (versions exactes,
   contrats D1B / D1D associés), input_fingerprint V1 identique à R1-D1 et
   V2 différent, sélection du bundle par les versions persistées EXACTES
   (combinaison mixte ou inconnue => UnsupportedEvaluationRunVersion).

2. Contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon SKIPPÉS) :
   nouveaux runs initiaux en V2 ; un run V1 laissé running par un worker
   R1-D1 (simulé : process_new_event avec le bundle legacy, crash pendant
   D1B) est repris APRÈS le déploiement D1E avec EXACTEMENT le prompt, le
   contrat et l'input_fingerprint V1 ; un run V2 crashé est repris en V2 ;
   une combinaison inconnue ne mute rien ; un event ayant déjà un run
   initial V1 (completed ou failed) ne reçoit jamais de run initial V2 ;
   release du run figée ; aucune transaction pendant un appel fournisseur.
"""
import contextlib
import dataclasses
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import capability_mapper as cm
from core import evaluation_runtime as rt
from core import local_evaluator as le
from core.pedagogy.taxonomy_v1 import EXPECTED_V1_FINGERPRINT
from tests.test_assistant_delivery import Sessions, engine, ext  # noqa: F401 — fixtures
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_r1d1_evaluation_input import (  # noqa: F401 — fixture r1d1
    T5_T6_TABLES,
    bundle,
    counts,
    execute,
    finalized_event,
    r1d1,
)
from tests.test_r1d1_local_evaluator import _obs
from tests.test_r1d1_runtime import (
    D1B_ONE,
    D1D_TWO,
    MODEL,
    Crash,
    FakeProvider,
    _activate_other_release,
    _assert_nothing_held,
    _expire,
    _mappings,
    _observations,
    _process,
    _recover,
    _release_id,
    _runs,
)
from tests.test_r1d1e_local_evaluator_v2 import FULL_APPLICATION, obs_v2

V1_VERSIONS = {
    "normalization_version": "decryptage-normalization-v1",
    "local_stage_version": "decryptage-local-stage-v1",
    "capability_mapping_version": "decryptage-capability-mapping-v1",
    "evaluation_schema_version": "decryptage-evaluation-schema-v1",
    "evaluator_version": "decryptage-local-evaluation-pipeline-v1",
    "prompt_spec_version": "decryptage-t3-prompt-bundle-v1",
}
V2_VERSIONS = {
    "normalization_version": "decryptage-normalization-v1",
    "local_stage_version": "decryptage-local-stage-v2",
    "capability_mapping_version": "decryptage-capability-mapping-v2",
    "evaluation_schema_version": "decryptage-evaluation-schema-v2",
    "evaluator_version": "decryptage-local-evaluation-pipeline-v2",
    "prompt_spec_version": "decryptage-t3-prompt-bundle-v2",
}
# input_fingerprint du manifest de référence calculé sur origin/main 033b8d1
# (R1-D1) AVANT R1-D1E : le bundle legacy doit le reproduire exactement.
V1_REFERENCE_INPUT_FINGERPRINT = "4de58a6032917d6af898faffdd52f2bf7b9b74f76996468208e871ca539d1cb5"

# Réponses au format V1 (stade et localisation choisis par le modèle).
D1B_ONE_V1 = {"observations": [_obs()]}
D1D_TWO_V1 = {"mappings": [{"observation_token": "observation_1", "localization": "localized",
                            "capability_tokens": ["capability_2", "capability_1"]}]}


def _reference_manifest(pipeline, **overrides):
    values = {"source_fingerprint": "s" * 64, "taxonomy_version_key": "oryx-v1",
              "taxonomy_spec_fingerprint": EXPECTED_V1_FINGERPRINT, "model_id": MODEL}
    values.update(overrides)
    return rt.build_input_manifest(pipeline=pipeline, **values)


# --------------------------------------------------------------------------
# 1. Sans base — bundles
# --------------------------------------------------------------------------

def test_bundles_have_the_exact_versions_and_contracts():
    assert dict(rt.LEGACY_V1_BUNDLE.versions) == V1_VERSIONS
    assert dict(rt.CURRENT_V2_BUNDLE.versions) == V2_VERSIONS == dict(rt.RUN_VERSIONS)
    for pipeline in (rt.LEGACY_V1_BUNDLE, rt.CURRENT_V2_BUNDLE):
        assert pipeline.evaluation_input_schema_version == "decryptage-evaluation-input-v1"
        assert set(pipeline.versions) == set(rt.RUN_VERSIONS)
    legacy, current = rt.LEGACY_V1_BUNDLE, rt.CURRENT_V2_BUNDLE
    assert (legacy.build_d1b_request, legacy.validate_d1b, legacy.build_d1d_request, legacy.validate_d1d) == (
        le.build_evaluator_request, le.validate_local_evaluation_result, cm.build_mapping_request,
        cm.validate_capability_mapping_result)
    assert (current.build_d1b_request, current.validate_d1b, current.build_d1d_request, current.validate_d1d) == (
        le.build_evaluator_request_v2, le.validate_local_evaluation_result_v2, cm.build_mapping_request_v2,
        cm.validate_capability_mapping_result_v2)
    assert rt.SUPPORTED_BUNDLES == (current, legacy)


def test_bundles_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        rt.CURRENT_V2_BUNDLE.name = "x"
    with pytest.raises(TypeError):
        rt.LEGACY_V1_BUNDLE.versions["local_stage_version"] = "decryptage-local-stage-v2"


def test_v1_input_fingerprint_is_unchanged_and_v2_differs():
    v1 = rt.compute_input_fingerprint(_reference_manifest(rt.LEGACY_V1_BUNDLE))
    v2 = rt.compute_input_fingerprint(_reference_manifest(rt.CURRENT_V2_BUNDLE))
    assert v1 == V1_REFERENCE_INPUT_FINGERPRINT
    assert v2 != v1
    assert set(_reference_manifest(rt.LEGACY_V1_BUNDLE)) == set(_reference_manifest(rt.CURRENT_V2_BUNDLE))
    event_id = uuid.uuid4()
    assert (rt.compute_evaluation_dedup_key(event_id=event_id, input_fingerprint=v1)
            != rt.compute_evaluation_dedup_key(event_id=event_id, input_fingerprint=v2))


def test_the_manifest_requires_an_explicit_bundle():
    with pytest.raises(TypeError):
        rt.build_input_manifest(source_fingerprint="s" * 64, taxonomy_version_key="oryx-v1",
                                taxonomy_spec_fingerprint=EXPECTED_V1_FINGERPRINT, model_id=MODEL)


def test_bundle_selection_is_by_exact_persisted_versions():
    assert rt.bundle_for_versions(dict(V1_VERSIONS)) is rt.LEGACY_V1_BUNDLE
    assert rt.bundle_for_versions(dict(V2_VERSIONS)) is rt.CURRENT_V2_BUNDLE
    with pytest.raises(rt.UnsupportedEvaluationRunVersion):
        rt.bundle_for_versions({})


@pytest.mark.parametrize("name", [n for n in V1_VERSIONS if V1_VERSIONS[n] != V2_VERSIONS[n]])
def test_mixed_v1_v2_combinations_are_unsupported(name):
    with pytest.raises(rt.UnsupportedEvaluationRunVersion):
        rt.bundle_for_versions({**V1_VERSIONS, name: V2_VERSIONS[name]})
    with pytest.raises(rt.UnsupportedEvaluationRunVersion):
        rt.bundle_for_versions({**V2_VERSIONS, name: V1_VERSIONS[name]})


def _fake_run(versions, **overrides):
    values = dict(id="run", trigger="initial", re_evaluates_run_id=None, model_id=MODEL,
                  pedagogical_taxonomy_release_id="release", **versions)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_check_run_supported_returns_the_bundle_of_the_run():
    assert rt.check_run_supported(_fake_run(V1_VERSIONS), MODEL) is rt.LEGACY_V1_BUNDLE
    assert rt.check_run_supported(_fake_run(V2_VERSIONS), MODEL) is rt.CURRENT_V2_BUNDLE
    for overrides in ({"trigger": "engine_upgrade"}, {"re_evaluates_run_id": "other"}, {"model_id": "other"},
                      {"pedagogical_taxonomy_release_id": None}, {"evaluator_version": "x"}):
        for versions in (V1_VERSIONS, V2_VERSIONS):
            with pytest.raises(rt.UnsupportedEvaluationRunVersion):
                rt.check_run_supported(_fake_run(versions, **overrides), MODEL)


# --------------------------------------------------------------------------
# 2. PostgreSQL — helpers
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _pre_d1e_worker(monkeypatch):
    """Simule le worker R1-D1 déployé AVANT D1E : ses nouveaux runs
    initiaux utilisent le bundle V1."""
    with monkeypatch.context() as patched:
        patched.setattr(rt, "CURRENT_V2_BUNDLE", rt.LEGACY_V1_BUNDLE)
        yield


def _crashed_v1_run(Sessions, engine, event_id, monkeypatch):  # noqa: F811
    with _pre_d1e_worker(monkeypatch), pytest.raises(Crash):
        _process(Sessions, event_id, FakeProvider(Crash()))
    [run] = _runs(engine, event_id)
    assert {k: run[k] for k in rt.RUN_VERSIONS} == V1_VERSIONS
    assert (run["execution_status"], run["interpretation_status"]) == ("running", "candidate")
    _expire(engine, run["id"])
    return run


def _v1_fingerprint(Sessions, event_id):  # noqa: F811
    source = bundle(Sessions, event_id).source_fingerprint
    return rt.compute_input_fingerprint(_reference_manifest(rt.LEGACY_V1_BUNDLE, source_fingerprint=source))


# --------------------------------------------------------------------------
# 2. PostgreSQL — nouveaux runs initiaux : V2
# --------------------------------------------------------------------------

def test_pg_a_new_initial_run_is_v2_with_v2_prompts_and_derived_results(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    provider = FakeProvider({"observations": [obs_v2(stage_basis=FULL_APPLICATION)]}, D1D_TWO)
    assert _process(Sessions, event_id, provider) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert {k: run[k] for k in rt.RUN_VERSIONS} == V2_VERSIONS
    source = bundle(Sessions, event_id).source_fingerprint
    expected = rt.compute_input_fingerprint(_reference_manifest(rt.CURRENT_V2_BUNDLE, source_fingerprint=source))
    assert run["input_fingerprint"] == expected != _v1_fingerprint(Sessions, event_id)
    # Deux appels au plus, contrats V2.
    assert [r.system for r in provider.requests] == [le.SYSTEM_PROMPT_V2, cm.SYSTEM_PROMPT_V2]
    [observation] = _observations(engine, run["id"])
    assert (observation["local_stage"], observation["capability_localization"]) == ("application", "localized")
    assert [row[1] for row in _mappings(engine, run["id"])] == ["C7_A", "C7_B"]
    # stage_basis et évaluations de capacités ne sont jamais persistés.
    serialized = json.dumps(observation, default=str)
    for transient in ("stage_basis", "capability_assessments", "include_satisfied", "contextualized_use"):
        assert transient not in serialized, transient
    assert set(counts(engine, T5_T6_TABLES).values()) == {0}


def test_pg_v2_zero_supported_capability_is_competency_only(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    d1d = {"mappings": [{"observation_token": "observation_1", "capability_assessments": [
        {"capability_token": f"capability_{i}", "supported": False, "reason": "insufficient_specificity"}
        for i in range(1, 5)]}]}
    assert _process(Sessions, event_id, FakeProvider(D1B_ONE, d1d)) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    [observation] = _observations(engine, run["id"])
    assert observation["capability_localization"] == "competency_only"
    assert _mappings(engine, run["id"]) == []


# --------------------------------------------------------------------------
# 2. PostgreSQL — recovery V1 legacy après le déploiement D1E
# --------------------------------------------------------------------------

def test_pg_a_crashed_v1_run_is_recovered_with_the_exact_v1_bundle(engine, Sessions, ext, r1d1, monkeypatch, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_v1_run(Sessions, engine, event_id, monkeypatch)
    assert crashed["input_fingerprint"] == _v1_fingerprint(Sessions, event_id)
    with Sessions() as session:
        assert rt.discover_recovery_runs(session, limit=10) == [crashed["id"]]

    provider = FakeProvider(D1B_ONE_V1, D1D_TWO_V1)
    with caplog.at_level("INFO", logger="core.evaluation_runtime"):
        assert _recover(Sessions, crashed["id"], provider) == rt.COMPLETED
    assert "bundle=legacy-v1" in caplog.text
    [run] = _runs(engine, event_id)
    # Même run, mêmes versions V1, même input_fingerprint, même dédup.
    assert run["id"] == crashed["id"]
    assert {k: run[k] for k in rt.RUN_VERSIONS} == V1_VERSIONS
    assert (run["input_fingerprint"], run["evaluation_dedup_key"]) == (crashed["input_fingerprint"],
                                                                      crashed["evaluation_dedup_key"])
    assert (run["execution_status"], run["interpretation_status"], run["lease_token"]) == ("completed", "active",
                                                                                          None)
    # Prompts et contrats V1, jamais V2.
    d1b, d1d = provider.requests
    assert (d1b.system, d1d.system) == (le.SYSTEM_PROMPT, cm.SYSTEM_PROMPT)
    assert "residual_cognitive_work" not in json.loads(d1d.user)["observations"][0]
    [observation] = _observations(engine, run["id"])
    assert (observation["local_stage"], observation["capability_localization"]) == ("comprehension", "localized")
    assert [row[1] for row in _mappings(engine, run["id"])] == ["C7_A", "C7_B"]
    assert run["output_fingerprint"] == rt.compute_output_fingerprint(rt.build_final_result(
        [_obs()], [{"observation_token": "observation_1", "capability_localization": "localized", "capabilities": [
            {"capability_token": "capability_1", "capability_code": "C7_A", "semantic_revision": 1},
            {"capability_token": "capability_2", "capability_code": "C7_B", "semantic_revision": 1}]}]))


@pytest.mark.parametrize("responses, code", [
    ([D1B_ONE], "invalid_d1b_output"),            # sortie V2 (stage_basis) refusée par le contrat V1
    ([D1B_ONE_V1, D1D_TWO], "invalid_d1d_output"),  # évaluations V2 refusées par le contrat V1
])
def test_pg_a_v1_recovery_never_accepts_the_v2_contract(engine, Sessions, ext, r1d1, monkeypatch, responses, code):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_v1_run(Sessions, engine, event_id, monkeypatch)
    provider = FakeProvider(*responses)
    assert _recover(Sessions, crashed["id"], provider) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["failure_code"], {k: run[k] for k in rt.RUN_VERSIONS}) == (code, V1_VERSIONS)
    assert provider.requests[0].system == le.SYSTEM_PROMPT


def test_pg_v1_recovery_holds_no_transaction_during_provider_calls(engine, Sessions, ext, r1d1, monkeypatch):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_v1_run(Sessions, engine, event_id, monkeypatch)

    def checked(response):
        def call(request):
            _assert_nothing_held(engine)
            return response
        return call

    provider = FakeProvider(checked(D1B_ONE_V1), checked(D1D_TWO_V1))
    assert _recover(Sessions, crashed["id"], provider) == rt.COMPLETED
    assert len(provider.requests) == 2


def test_pg_v1_recovery_stays_on_the_run_release(engine, Sessions, ext, r1d1, monkeypatch):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_v1_run(Sessions, engine, event_id, monkeypatch)
    v1_release = _release_id(engine)
    _activate_other_release(Sessions)
    assert _recover(Sessions, crashed["id"], FakeProvider(D1B_ONE_V1, D1D_TWO_V1)) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert run["pedagogical_taxonomy_release_id"] == v1_release
    assert {row[4] for row in _mappings(engine, run["id"])} == {v1_release}


def test_pg_a_crashed_v2_run_is_recovered_with_the_v2_bundle(engine, Sessions, ext, r1d1, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    with pytest.raises(Crash):
        _process(Sessions, event_id, FakeProvider(Crash()))
    [crashed] = _runs(engine, event_id)
    _expire(engine, crashed["id"])
    provider = FakeProvider(D1B_ONE, D1D_TWO)
    with caplog.at_level("INFO", logger="core.evaluation_runtime"):
        assert _recover(Sessions, crashed["id"], provider) == rt.COMPLETED
    assert "bundle=current-v2" in caplog.text
    [run] = _runs(engine, event_id)
    assert {k: run[k] for k in rt.RUN_VERSIONS} == V2_VERSIONS
    assert run["input_fingerprint"] == crashed["input_fingerprint"]
    assert [r.system for r in provider.requests] == [le.SYSTEM_PROMPT_V2, cm.SYSTEM_PROMPT_V2]


@pytest.mark.parametrize("column, value", [
    ("local_stage_version", "decryptage-local-stage-v2"),
    ("capability_mapping_version", "decryptage-capability-mapping-v2"),
    ("prompt_spec_version", "decryptage-t3-prompt-bundle-v2"),
    ("evaluator_version", "decryptage-local-evaluation-pipeline-v3"),
])
def test_pg_an_unknown_version_combination_mutates_nothing(engine, Sessions, ext, r1d1, monkeypatch, column,
                                                          value):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed_v1_run(Sessions, engine, event_id, monkeypatch)
    execute(engine, f"UPDATE observation_evaluation_runs SET {column} = :v WHERE id = :id", v=value, id=crashed["id"])
    [before] = _runs(engine, event_id)
    provider = FakeProvider()
    with pytest.raises(rt.UnsupportedEvaluationRunVersion):
        _recover(Sessions, crashed["id"], provider)
    assert _runs(engine, event_id) == [before] and provider.requests == []


# --------------------------------------------------------------------------
# 2. PostgreSQL — un event déjà traité en V1 ne reçoit jamais d'initial V2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("v1_responses, status", [((D1B_ONE_V1, D1D_TWO_V1), "completed"),
                                                  ((rt.ProviderError("x"),), "failed")])
def test_pg_an_event_with_a_v1_initial_run_never_gets_a_v2_initial_run(engine, Sessions, ext, r1d1, monkeypatch,
                                                                       v1_responses, status):
    event_id = finalized_event(Sessions, ext, engine)
    with _pre_d1e_worker(monkeypatch):
        _process(Sessions, event_id, FakeProvider(*v1_responses))
    [v1_run] = _runs(engine, event_id)
    assert (v1_run["execution_status"], v1_run["evaluator_version"]) == (
        status, "decryptage-local-evaluation-pipeline-v1")

    with Sessions() as session:
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert rt.discover_new_events(session, cutoff=cutoff, limit=10) == []
        assert rt.discover_recovery_runs(session, limit=10) == []
    provider = FakeProvider()
    assert _process(Sessions, event_id, provider) == rt.ALREADY_HANDLED
    assert provider.requests == []
    assert _runs(engine, event_id) == [v1_run]  # intact, jamais retry ni supersede
