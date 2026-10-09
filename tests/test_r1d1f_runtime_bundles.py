"""Tests de R1-D1F : bundle courant V3 et recovery V1 / V2 / V3
(core/evaluation_runtime.py).

1. Sans base : CURRENT_V3_BUNDLE (versions exactes : D1A, normalisation V1
   et stade local V2 inchangés ; mapping, schéma, évaluateur, prompt V3),
   contrats D1B V2 / D1D V3, LEGACY_V2_BUNDLE et LEGACY_V1_BUNDLE figés
   (input_fingerprint V1 et V2 identiques à ceux calculés AVANT D1F, V3
   distinct), sélection du bundle par les versions persistées EXACTES
   (toute combinaison mixte V1 / V2 / V3 ou inconnue =>
   UnsupportedEvaluationRunVersion).

2. Contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon SKIPPÉS) :
   nouveau run initial => V3 (prompts D1B V2 + D1D V3, prémisses jamais
   persistées, aucun T5 / T6) ; un run V3 crashé est repris en V3 ; un run
   V2 laissé running par le worker R1-D1E est repris APRÈS le déploiement
   D1F avec EXACTEMENT le prompt, le contrat et l'input_fingerprint V2
   (jamais d'upgrade silencieux) ; idem V1 ; une combinaison mixte V2 / V3
   ne mute rien ; un event déjà traité en V2 ne reçoit jamais de run
   initial V3 ; aucun ancien run n'est réécrit.
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
from core.pedagogy.taxonomy_v1 import EXPECTED_V1_FINGERPRINT, load_taxonomy_v1
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
from tests.test_r1d1_runtime import (
    D1B_ONE,
    D1D_TWO,
    D1D_TWO_V2,
    MODEL,
    Crash,
    FakeProvider,
    _d1d_v3,
    _expire,
    _mappings,
    _observations,
    _process,
    _recover,
    _runs,
)
from tests.test_r1d1e_runtime_bundles import (
    D1B_ONE_V1,
    D1D_TWO_V1,
    V1_REFERENCE_INPUT_FINGERPRINT,
    V1_VERSIONS,
    V2_VERSIONS,
    _pre_d1e_worker,
    _pre_d1f_worker,
)

V3_VERSIONS = {
    "normalization_version": "decryptage-normalization-v1",
    "local_stage_version": "decryptage-local-stage-v2",
    "capability_mapping_version": "decryptage-capability-mapping-v3",
    "evaluation_schema_version": "decryptage-evaluation-schema-v3",
    "evaluator_version": "decryptage-local-evaluation-pipeline-v3",
    "prompt_spec_version": "decryptage-t3-prompt-bundle-v3",
}
# input_fingerprint du manifest de référence calculé sur origin/main 06e03bd
# (R1-D1E) AVANT R1-D1F avec CURRENT_V2_BUNDLE : le bundle legacy V2 doit le
# reproduire exactement.
V2_REFERENCE_INPUT_FINGERPRINT = "a2f1281c6815c7a1c360625d6e74eeac157598bd7bd8df0f6fca671ac0ed8c7b"
ALL_VERSIONS = {"v1": V1_VERSIONS, "v2": V2_VERSIONS, "v3": V3_VERSIONS}


def _reference_manifest(pipeline, **overrides):
    values = {"source_fingerprint": "s" * 64, "taxonomy_version_key": "oryx-v1",
              "taxonomy_spec_fingerprint": EXPECTED_V1_FINGERPRINT, "model_id": MODEL}
    values.update(overrides)
    return rt.build_input_manifest(pipeline=pipeline, **values)


def _fingerprint(pipeline, **overrides):
    return rt.compute_input_fingerprint(_reference_manifest(pipeline, **overrides))


# --------------------------------------------------------------------------
# 1. Sans base — bundles
# --------------------------------------------------------------------------

def test_the_current_bundle_is_v3_with_unchanged_d1a_normalization_and_stage():
    current = rt.CURRENT_V3_BUNDLE
    assert current.name == "current-v3"
    assert dict(current.versions) == V3_VERSIONS == dict(rt.RUN_VERSIONS)
    assert current.evaluation_input_schema_version == "decryptage-evaluation-input-v1"
    assert (rt.EVALUATION_INPUT_SCHEMA_VERSION, rt.NORMALIZATION_VERSION, rt.LOCAL_STAGE_VERSION,
            rt.CAPABILITY_MAPPING_VERSION, rt.EVALUATION_SCHEMA_VERSION, rt.EVALUATOR_VERSION,
            rt.PROMPT_SPEC_VERSION) == (
        "decryptage-evaluation-input-v1", "decryptage-normalization-v1", "decryptage-local-stage-v2",
        "decryptage-capability-mapping-v3", "decryptage-evaluation-schema-v3",
        "decryptage-local-evaluation-pipeline-v3", "decryptage-t3-prompt-bundle-v3")
    # Stade V2 inchangé : MÊME D1B que le bundle V2 ; seul D1D change.
    legacy = rt.LEGACY_V2_BUNDLE
    assert current.versions["local_stage_version"] == legacy.versions["local_stage_version"]
    assert (current.build_d1b_request, current.validate_d1b) == (legacy.build_d1b_request, legacy.validate_d1b) == (
        le.build_evaluator_request_v2, le.validate_local_evaluation_result_v2)
    assert (current.build_d1d_request, current.validate_d1d) == (cm.build_mapping_request_v3,
                                                                 cm.validate_capability_mapping_result_v3)


def test_legacy_bundles_keep_their_exact_versions_and_contracts():
    v2, v1 = rt.LEGACY_V2_BUNDLE, rt.LEGACY_V1_BUNDLE
    assert (v2.name, dict(v2.versions)) == ("legacy-v2", V2_VERSIONS)
    assert (v1.name, dict(v1.versions)) == ("legacy-v1", V1_VERSIONS)
    assert (v2.build_d1d_request, v2.validate_d1d) == (cm.build_mapping_request_v2,
                                                       cm.validate_capability_mapping_result_v2)
    assert (v1.build_d1b_request, v1.validate_d1b, v1.build_d1d_request, v1.validate_d1d) == (
        le.build_evaluator_request, le.validate_local_evaluation_result, cm.build_mapping_request,
        cm.validate_capability_mapping_result)
    assert rt.SUPPORTED_BUNDLES == (rt.CURRENT_V3_BUNDLE, v2, v1)
    for pipeline in rt.SUPPORTED_BUNDLES:
        assert set(pipeline.versions) == set(rt.RUN_VERSIONS)
    assert not hasattr(rt, "CURRENT_V2_BUNDLE")


def test_bundles_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        rt.CURRENT_V3_BUNDLE.name = "x"
    with pytest.raises(TypeError):
        rt.LEGACY_V2_BUNDLE.versions["capability_mapping_version"] = "decryptage-capability-mapping-v3"
    with pytest.raises(TypeError):
        rt.RUN_VERSIONS["capability_mapping_version"] = "decryptage-capability-mapping-v2"


def test_historical_input_fingerprints_are_unchanged_and_v3_has_its_own():
    v1, v2, v3 = (_fingerprint(p) for p in (rt.LEGACY_V1_BUNDLE, rt.LEGACY_V2_BUNDLE, rt.CURRENT_V3_BUNDLE))
    assert v1 == V1_REFERENCE_INPUT_FINGERPRINT
    assert v2 == V2_REFERENCE_INPUT_FINGERPRINT
    assert len({v1, v2, v3}) == 3
    assert set(_reference_manifest(rt.CURRENT_V3_BUNDLE)) == set(_reference_manifest(rt.LEGACY_V2_BUNDLE))
    event_id = uuid.uuid4()
    assert len({rt.compute_evaluation_dedup_key(event_id=event_id, input_fingerprint=f) for f in (v1, v2, v3)}) == 3


def test_bundle_selection_is_by_exact_persisted_versions():
    assert rt.bundle_for_versions(dict(V3_VERSIONS)) is rt.CURRENT_V3_BUNDLE
    assert rt.bundle_for_versions(dict(V2_VERSIONS)) is rt.LEGACY_V2_BUNDLE
    assert rt.bundle_for_versions(dict(V1_VERSIONS)) is rt.LEGACY_V1_BUNDLE
    for unknown in ({}, {**V3_VERSIONS, "capability_mapping_version": "decryptage-capability-mapping-v4"},
                    {**V3_VERSIONS, "local_stage_version": "decryptage-local-stage-v3"}):
        with pytest.raises(rt.UnsupportedEvaluationRunVersion):
            rt.bundle_for_versions(unknown)


@pytest.mark.parametrize("base, other", [(a, b) for a in ALL_VERSIONS for b in ALL_VERSIONS if a != b])
def test_any_mixed_combination_of_bundle_versions_is_unsupported(base, other):
    for name in V3_VERSIONS:
        if ALL_VERSIONS[base][name] == ALL_VERSIONS[other][name]:
            continue
        with pytest.raises(rt.UnsupportedEvaluationRunVersion):
            rt.bundle_for_versions({**ALL_VERSIONS[base], name: ALL_VERSIONS[other][name]})


def _fake_run(versions, **overrides):
    values = dict(id="run", trigger="initial", re_evaluates_run_id=None, model_id=MODEL,
                  pedagogical_taxonomy_release_id="release", **versions)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_check_run_supported_returns_the_bundle_of_the_run_never_the_current_one():
    assert rt.check_run_supported(_fake_run(V3_VERSIONS), MODEL) is rt.CURRENT_V3_BUNDLE
    assert rt.check_run_supported(_fake_run(V2_VERSIONS), MODEL) is rt.LEGACY_V2_BUNDLE
    assert rt.check_run_supported(_fake_run(V1_VERSIONS), MODEL) is rt.LEGACY_V1_BUNDLE
    for overrides in ({"trigger": "engine_upgrade"}, {"re_evaluates_run_id": "other"}, {"model_id": "other"},
                      {"pedagogical_taxonomy_release_id": None},
                      {"capability_mapping_version": "decryptage-capability-mapping-v2"}):
        with pytest.raises(rt.UnsupportedEvaluationRunVersion):
            rt.check_run_supported(_fake_run(V3_VERSIONS, **overrides), MODEL)


# --------------------------------------------------------------------------
# 2. PostgreSQL — nouveau run initial : V3
# --------------------------------------------------------------------------

def test_pg_a_new_initial_run_is_v3_with_v3_prompts_and_derived_results(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    # C7_A et C7_B clear ; C7_C « proche » (include touché, définition non
    # satisfaite) ; C7_D écartée par un exclude.
    d1d = {"mappings": [{"observation_token": "observation_1", "capability_assessments": [
        {"capability_token": "capability_1", "definition_satisfied": True, "matched_include_indices": [0, 1],
         "matched_exclude_indices": [], "boundary_status": "clear"},
        {"capability_token": "capability_2", "definition_satisfied": True, "matched_include_indices": [1],
         "matched_exclude_indices": [], "boundary_status": "clear"},
        {"capability_token": "capability_3", "definition_satisfied": False, "matched_include_indices": [0],
         "matched_exclude_indices": [], "boundary_status": "insufficient_specificity"},
        {"capability_token": "capability_4", "definition_satisfied": False, "matched_include_indices": [],
         "matched_exclude_indices": [0], "boundary_status": "excluded_by_boundary"}]}]}
    provider = FakeProvider(D1B_ONE, d1d)
    assert _process(Sessions, event_id, provider) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    assert {k: run[k] for k in rt.RUN_VERSIONS} == V3_VERSIONS
    source = bundle(Sessions, event_id).source_fingerprint
    expected = _fingerprint(rt.CURRENT_V3_BUNDLE, source_fingerprint=source)
    assert run["input_fingerprint"] == expected
    assert expected not in (_fingerprint(rt.LEGACY_V2_BUNDLE, source_fingerprint=source),
                            _fingerprint(rt.LEGACY_V1_BUNDLE, source_fingerprint=source))
    # Deux appels au plus : D1B V2 (stade V2 inchangé) puis D1D V3.
    assert [r.system for r in provider.requests] == [le.SYSTEM_PROMPT_V2, cm.SYSTEM_PROMPT_V3]
    [observation] = _observations(engine, run["id"])
    assert (observation["local_stage"], observation["capability_localization"]) == ("comprehension", "localized")
    assert [row[1] for row in _mappings(engine, run["id"])] == ["C7_A", "C7_B"]
    # Prémisses jamais persistées ; output_fingerprint sur le seul résultat
    # sémantique final.
    serialized = json.dumps(observation, default=str)
    for transient in ("definition_satisfied", "matched_include_indices", "matched_exclude_indices",
                      "boundary_status", "capability_assessments", "stage_basis", "include_satisfied"):
        assert transient not in serialized, transient
    candidates = le.validate_local_evaluation_result_v2(D1B_ONE, bundle(Sessions, event_id).evaluator_payload)
    mappings = cm.validate_capability_mapping_result_v3(
        d1d, candidates, cm.build_capability_reference_context(load_taxonomy_v1()[0], {"C7"}))
    assert run["output_fingerprint"] == rt.compute_output_fingerprint(rt.build_final_result(candidates, mappings))
    assert set(counts(engine, T5_T6_TABLES).values()) == {0}


def test_pg_v3_zero_supported_capability_is_competency_only(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    assert _process(Sessions, event_id, FakeProvider(D1B_ONE, {"mappings": [_d1d_v3()]})) == rt.COMPLETED
    [run] = _runs(engine, event_id)
    [observation] = _observations(engine, run["id"])
    assert observation["capability_localization"] == "competency_only"
    assert _mappings(engine, run["id"]) == []


def test_pg_an_incoherent_v3_output_fails_the_run_without_any_partial_result(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    d1d = _d1d_v3("capability_3")
    d1d["capability_assessments"][1]["definition_satisfied"] = False  # capability_3 : clear sans définition
    assert _process(Sessions, event_id, FakeProvider(D1B_ONE, {"mappings": [d1d]})) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["failure_code"]) == ("failed", "invalid_d1d_output")
    assert _observations(engine, run["id"]) == [] and _mappings(engine, run["id"]) == []


# --------------------------------------------------------------------------
# 2. PostgreSQL — recovery V3 / V2 / V1
# --------------------------------------------------------------------------

def _crashed(Sessions, engine, event_id, worker=None):  # noqa: F811
    """Le worker (courant, ou simulé d'avant un déploiement) meurt pendant
    D1B ; sa lease expire."""
    with worker or contextlib.nullcontext(), pytest.raises(Crash):
        _process(Sessions, event_id, FakeProvider(Crash()))
    [run] = _runs(engine, event_id)
    assert (run["execution_status"], run["interpretation_status"]) == ("running", "candidate")
    _expire(engine, run["id"])
    return run


def _assert_recovered_as(engine, event_id, crashed, versions):
    [run] = _runs(engine, event_id)
    assert run["id"] == crashed["id"]
    assert {k: run[k] for k in rt.RUN_VERSIONS} == versions
    assert (run["input_fingerprint"], run["evaluation_dedup_key"]) == (crashed["input_fingerprint"],
                                                                      crashed["evaluation_dedup_key"])
    assert (run["execution_status"], run["interpretation_status"], run["lease_token"]) == ("completed", "active",
                                                                                          None)
    return run


def test_pg_a_crashed_v3_run_is_recovered_with_the_v3_bundle(engine, Sessions, ext, r1d1, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed(Sessions, engine, event_id)
    assert {k: crashed[k] for k in rt.RUN_VERSIONS} == V3_VERSIONS
    provider = FakeProvider(D1B_ONE, D1D_TWO)
    with caplog.at_level("INFO", logger="core.evaluation_runtime"):
        assert _recover(Sessions, crashed["id"], provider) == rt.COMPLETED
    assert "bundle=current-v3" in caplog.text
    run = _assert_recovered_as(engine, event_id, crashed, V3_VERSIONS)
    assert [r.system for r in provider.requests] == [le.SYSTEM_PROMPT_V2, cm.SYSTEM_PROMPT_V3]
    assert [row[1] for row in _mappings(engine, run["id"])] == ["C7_A", "C7_B"]


def test_pg_a_crashed_v2_run_is_recovered_with_the_exact_v2_bundle(engine, Sessions, ext, r1d1, monkeypatch, caplog):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed(Sessions, engine, event_id, _pre_d1f_worker(monkeypatch))
    source = bundle(Sessions, event_id).source_fingerprint
    assert crashed["input_fingerprint"] == _fingerprint(rt.LEGACY_V2_BUNDLE, source_fingerprint=source)
    with Sessions() as session:
        assert rt.discover_recovery_runs(session, limit=10) == [crashed["id"]]
    provider = FakeProvider(D1B_ONE, D1D_TWO_V2)
    with caplog.at_level("INFO", logger="core.evaluation_runtime"):
        assert _recover(Sessions, crashed["id"], provider) == rt.COMPLETED
    assert "bundle=legacy-v2" in caplog.text
    run = _assert_recovered_as(engine, event_id, crashed, V2_VERSIONS)
    # Prompts, données et contrat V2 exacts : jamais le prompt ni
    # l'indexation V3.
    d1b, d1d = provider.requests
    assert (d1b.system, d1d.system) == (le.SYSTEM_PROMPT_V2, cm.SYSTEM_PROMPT_V2)
    assert all(type(i) is str for c in json.loads(d1d.user)["capability_reference"]
               for i in c["mapping_guidance"]["include"])
    assert [row[1] for row in _mappings(engine, run["id"])] == ["C7_A", "C7_B"]


@pytest.mark.parametrize("d1d", [D1D_TWO, {"mappings": [_d1d_v3()]}])
def test_pg_v2_recovery_never_accepts_the_v3_contract(engine, Sessions, ext, r1d1, monkeypatch, d1d):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed(Sessions, engine, event_id, _pre_d1f_worker(monkeypatch))
    provider = FakeProvider(D1B_ONE, d1d)
    assert _recover(Sessions, crashed["id"], provider) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["failure_code"], {k: run[k] for k in rt.RUN_VERSIONS}) == ("invalid_d1d_output", V2_VERSIONS)
    assert provider.requests[1].system == cm.SYSTEM_PROMPT_V2


def test_pg_v3_never_accepts_the_v2_contract(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed(Sessions, engine, event_id)
    assert _recover(Sessions, crashed["id"], FakeProvider(D1B_ONE, D1D_TWO_V2)) == rt.FAILED
    [run] = _runs(engine, event_id)
    assert (run["failure_code"], {k: run[k] for k in rt.RUN_VERSIONS}) == ("invalid_d1d_output", V3_VERSIONS)


def test_pg_a_crashed_v1_run_is_still_recovered_with_the_exact_v1_bundle(engine, Sessions, ext, r1d1, monkeypatch,
                                                                         caplog):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed(Sessions, engine, event_id, _pre_d1e_worker(monkeypatch))
    source = bundle(Sessions, event_id).source_fingerprint
    assert crashed["input_fingerprint"] == _fingerprint(rt.LEGACY_V1_BUNDLE, source_fingerprint=source)
    provider = FakeProvider(D1B_ONE_V1, D1D_TWO_V1)
    with caplog.at_level("INFO", logger="core.evaluation_runtime"):
        assert _recover(Sessions, crashed["id"], provider) == rt.COMPLETED
    assert "bundle=legacy-v1" in caplog.text
    _assert_recovered_as(engine, event_id, crashed, V1_VERSIONS)
    assert [r.system for r in provider.requests] == [le.SYSTEM_PROMPT, cm.SYSTEM_PROMPT]


@pytest.mark.parametrize("column, value", [
    ("capability_mapping_version", "decryptage-capability-mapping-v3"),
    ("evaluation_schema_version", "decryptage-evaluation-schema-v3"),
    ("evaluator_version", "decryptage-local-evaluation-pipeline-v3"),
    ("prompt_spec_version", "decryptage-t3-prompt-bundle-v3"),
])
def test_pg_a_mixed_v2_v3_run_mutates_nothing(engine, Sessions, ext, r1d1, monkeypatch, column, value):
    event_id = finalized_event(Sessions, ext, engine)
    crashed = _crashed(Sessions, engine, event_id, _pre_d1f_worker(monkeypatch))
    execute(engine, f"UPDATE observation_evaluation_runs SET {column} = :v WHERE id = :id", v=value, id=crashed["id"])
    [before] = _runs(engine, event_id)
    provider = FakeProvider()
    with pytest.raises(rt.UnsupportedEvaluationRunVersion):
        _recover(Sessions, crashed["id"], provider)
    assert _runs(engine, event_id) == [before] and provider.requests == []


@pytest.mark.parametrize("v2_responses, status", [((D1B_ONE, D1D_TWO_V2), "completed"),
                                                  ((rt.ProviderError("x"),), "failed")])
def test_pg_an_event_with_a_v2_initial_run_never_gets_a_v3_initial_run(engine, Sessions, ext, r1d1, monkeypatch,
                                                                       v2_responses, status):
    event_id = finalized_event(Sessions, ext, engine)
    with _pre_d1f_worker(monkeypatch):
        _process(Sessions, event_id, FakeProvider(*v2_responses))
    [v2_run] = _runs(engine, event_id)
    assert (v2_run["execution_status"], v2_run["capability_mapping_version"]) == (
        status, "decryptage-capability-mapping-v2")
    mappings_before = _mappings(engine, v2_run["id"])
    with Sessions() as session:
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert rt.discover_new_events(session, cutoff=cutoff, limit=10) == []
        assert rt.discover_recovery_runs(session, limit=10) == []
    provider = FakeProvider()
    assert _process(Sessions, event_id, provider) == rt.ALREADY_HANDLED
    assert provider.requests == []
    # Jamais réécrit, ni retry, ni supersede.
    assert _runs(engine, event_id) == [v2_run]
    assert _mappings(engine, v2_run["id"]) == mappings_before
    assert set(counts(engine, T5_T6_TABLES).values()) == {0}
