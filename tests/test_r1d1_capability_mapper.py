"""Tests de R1-D1D : localisation sémantique des observations sur les
capacités de LEUR compétence (core/capability_mapper.py).

Tests PURS (aucune base, aucun réseau) : contexte de référence construit
depuis la SPEC canonique oryx-v1 (jamais cross-competency), requête batch,
validation stricte de la sortie (une entrée par observation, localized /
competency_only, tokens connus de la même compétence, doublons), et
immutabilité des champs D1B.

Depuis R1-D1E, ces tests protègent le contrat V1 LEGACY (localization +
capability_tokens choisis par le modèle), toujours exécuté pour la recovery
des runs V1 ; le contrat V2 (éligibilité explicite par capacité,
localisation dérivée serveur) est couvert par
tests/test_r1d1e_capability_mapper_v2.py.
"""
import ast
import hashlib
import copy
import json

import pytest

from core import capability_mapper as cm
from core.capability_mapper import build_capability_reference_context, validate_capability_mapping_result
from core.local_evaluator import EvaluationOutputInvalid
from core.pedagogy.taxonomy_v1 import load_taxonomy_v1
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_r1d1_local_evaluator import PAYLOAD, _contradictory, _obs, _validate

SPEC, _ = load_taxonomy_v1()
MODULE_PATH = REPO_ROOT / "core" / "capability_mapper.py"


def _c6(index):
    return _obs(index, competency_code="C6", observation_role="secondary",
                primary_user_action={"action": "interpret", "contribution_token": "contribution_1"},
                source_contribution_tokens=["contribution_1"], support_level="none",
                residual_cognitive_work={"operations_left_to_user": [], "materially_used_support_refs": [],
                                         "summary": "s"},
                observation_text="Reformule la progression du bénéfice.")


@pytest.fixture
def observations():
    return _validate(_obs(), _c6(2), _contradictory(3))


@pytest.fixture
def context(observations):
    return build_capability_reference_context(SPEC, {o["competency_code"] for o in observations})


def _token(context, code):
    [token] = [t for t, (c, _, _) in context.by_token.items() if c == code]
    return token


def _mapping(token, localization, *capabilities):
    return {"observation_token": token, "localization": localization, "capability_tokens": list(capabilities)}


def _result(context, *entries):
    return {"mappings": list(entries)}


def _default(context):
    return [_mapping("observation_1", "localized", _token(context, "C7_A")),
            _mapping("observation_2", "competency_only"),
            _mapping("observation_3", "competency_only")]


# --------------------------------------------------------------------------
# Contexte de référence
# --------------------------------------------------------------------------

def test_context_only_contains_the_capabilities_of_the_observed_competencies(context):
    codes = [entry["capability_code"] for entry in context.reference]
    assert codes == ["C6_A", "C6_B", "C6_C", "C6_D", "C7_A", "C7_B", "C7_C", "C7_D"]
    assert context.candidates["C7"] == tuple(_token(context, c) for c in ("C7_A", "C7_B", "C7_C", "C7_D"))
    assert set(context.candidates) == {"C6", "C7"}
    by_code = {c["capability_code"]: c for c in SPEC["capabilities"]}
    for entry in context.reference:
        spec = by_code[entry["capability_code"]]
        assert entry["semantic_revision"] == spec["semantic_revision"] == 1
        assert (entry["label"], entry["definition"], entry["mapping_guidance"]) == (
            spec["label"], spec["definition"], spec["mapping_guidance"])
        assert set(entry["mapping_guidance"]) == {"include", "exclude", "boundary_notes"}
    assert [r["rule_code"] for r in context.global_rules] == [f"R{i}" for i in range(1, 9)]


def test_mapping_request_is_one_batch_with_only_same_competency_candidates(observations, context):
    request = cm.build_mapping_request(observations, context, PAYLOAD)
    data = json.loads(request.user)
    assert [r["rule_code"] for r in data["global_mapping_rules"]] == [f"R{i}" for i in range(1, 9)]
    for item in data["observations"]:
        codes = {context.by_token[t][0] for t in item["candidate_capability_tokens"]}
        assert {code.split("_")[0] for code in codes} == {item["competency_code"]}
    assert [c["contribution_token"] for c in data["source_contributions"]] == ["contribution_1", "contribution_2"]
    for rule in ("DONNÉES NON FIABLES", "candidate_capability_tokens", "competency_only", "ne les modifies"):
        assert rule in request.system, rule


# --------------------------------------------------------------------------
# Sorties valides
# --------------------------------------------------------------------------

def test_localized_one_capability_and_competency_only(observations, context):
    result = validate_capability_mapping_result(_result(context, *_default(context)), observations, context)
    assert [(m["observation_token"], m["capability_localization"]) for m in result] == [
        ("observation_1", "localized"), ("observation_2", "competency_only"), ("observation_3", "competency_only")]
    assert result[0]["capabilities"] == [{"capability_token": _token(context, "C7_A"), "capability_code": "C7_A",
                                          "semantic_revision": 1}]
    assert result[1]["capabilities"] == [] == result[2]["capabilities"]


def test_one_observation_may_map_several_capabilities_of_its_competency(observations, context):
    entries = _default(context)
    entries[0] = _mapping("observation_1", "localized", _token(context, "C7_B"), _token(context, "C7_A"))
    result = validate_capability_mapping_result(_result(context, *entries), observations, context)
    # Toujours UNE observation ; capacités en ordre naturel.
    assert len(result) == 3
    assert [c["capability_code"] for c in result[0]["capabilities"]] == ["C7_A", "C7_B"]


def test_a_contradiction_can_be_localized(observations, context):
    entries = _default(context)
    entries[2] = _mapping("observation_3", "localized", _token(context, "C7_B"))
    result = validate_capability_mapping_result(_result(context, *entries), observations, context)
    assert result[2]["capability_localization"] == "localized"
    assert observations[2]["polarity"] == "contradictory"


def test_entries_may_come_in_any_order_but_the_result_follows_the_observations(observations, context):
    entries = list(reversed(_default(context)))
    result = validate_capability_mapping_result(_result(context, *entries), observations, context)
    assert [m["observation_token"] for m in result] == ["observation_1", "observation_2", "observation_3"]


def test_mapping_guidance_fixture_cash_conversion_is_c7_not_c8(observations, context):
    """« Relie la progression des créances à une consommation de cash » :
    une capacité C7 est acceptée, une capacité C8 (ROIC) est refusée même si
    le modèle la propose (R5)."""
    c8 = build_capability_reference_context(SPEC, {"C7", "C8"})
    entries = _default(c8)
    entries[0] = _mapping("observation_1", "localized", _token(c8, "C8_A"))
    with pytest.raises(EvaluationOutputInvalid, match="cross-competency"):
        validate_capability_mapping_result(_result(c8, *entries), observations, c8)


def test_d1d_never_changes_any_d1b_field(observations, context):
    before = copy.deepcopy(observations)
    result = validate_capability_mapping_result(_result(context, *_default(context)), observations, context)
    assert observations == before
    for mapping in result:
        assert set(mapping) == {"observation_token", "capability_localization", "capabilities"}


def test_d1d_output_cannot_carry_d1b_fields(observations, context):
    entries = _default(context)
    entries[0]["competency_code"] = "C8"
    with pytest.raises(EvaluationOutputInvalid, match="clés"):
        validate_capability_mapping_result(_result(context, *entries), observations, context)


# --------------------------------------------------------------------------
# Refus
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mutate, match", [
    (lambda e, c: e.__setitem__(0, _mapping("observation_1", "localized")), "au moins une"),
    (lambda e, c: e.__setitem__(1, _mapping("observation_2", "competency_only", _token(c, "C6_A"))),
     "competency_only"),
    (lambda e, c: e.__setitem__(0, _mapping("observation_1", "localized", _token(c, "C6_A"))), "cross-competency"),
    (lambda e, c: e.__setitem__(0, _mapping("observation_1", "localized", "capability_99")), "inconnue"),
    (lambda e, c: e.__setitem__(0, _mapping("observation_1", "localized", "C7_A")), "inconnue"),
    (lambda e, c: e.pop(), "sans entrée"),
    (lambda e, c: e.append(_mapping("observation_1", "competency_only")), "double"),
    (lambda e, c: e.append(_mapping("observation_9", "competency_only")), "inconnue"),
    (lambda e, c: e.__setitem__(0, _mapping("observation_1", "localized", _token(c, "C7_A"), _token(c, "C7_A"))),
     "doublon"),
    (lambda e, c: e.__setitem__(0, _mapping("observation_1", "partial", _token(c, "C7_A"))), "vocabulaire"),
])
def test_invalid_mappings_are_refused(observations, context, mutate, match):
    entries = _default(context)
    mutate(entries, context)
    with pytest.raises(EvaluationOutputInvalid, match=match):
        validate_capability_mapping_result(_result(context, *entries), observations, context)


@pytest.mark.parametrize("raw", [{"mappings": {}}, {"mappings": [], "model_id": "m"}, [], {"mapping": []}])
def test_invalid_roots_are_refused(observations, context, raw):
    with pytest.raises(EvaluationOutputInvalid):
        validate_capability_mapping_result(raw, observations, context)


def test_module_is_pure():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imported == {"json", "dataclasses", "core.local_evaluator"}


# --------------------------------------------------------------------------
# R1-D1E — le contrat V1 legacy reste byte-identique à R1-D1 (PR #217)
# --------------------------------------------------------------------------

# Empreintes calculées sur origin/main 033b8d1 AVANT R1-D1E.
V1_SYSTEM_PROMPT_SHA256 = "5682befaf62cd782c2706d9926e2a95c1f485e8dbc1e8c7ac5e25192663829df"
V1_REQUEST_USER_SHA256 = "e559a89cd9618398d360555f096314a42f45ff483fbc1daad3133ec36220f3d3"


def test_v1_prompt_and_request_are_byte_identical_to_r1d1(observations, context):
    request = cm.build_mapping_request(observations, context, PAYLOAD)
    assert request.system == cm.SYSTEM_PROMPT
    assert hashlib.sha256(request.system.encode("utf-8")).hexdigest() == V1_SYSTEM_PROMPT_SHA256
    assert hashlib.sha256(request.user.encode("utf-8")).hexdigest() == V1_REQUEST_USER_SHA256
    assert "residual_cognitive_work" not in json.loads(request.user)["observations"][0]


# --------------------------------------------------------------------------
# Blocker 2 — une contradiction localisée reste contradictory
# --------------------------------------------------------------------------

def test_d1d_prompt_never_suggests_that_a_contradiction_becomes_supportive():
    prompt = cm.SYSTEM_PROMPT
    assert "supportive" not in prompt
    assert "preuve supportive" not in prompt
    assert ('Une observation contradictory peut être "localized" sur une capacité précise ; elle reste '
            "contradictory et la localisation ne modifie jamais sa polarité.") in prompt
    assert "ne les modifies jamais (ni compétence, ni polarité" in prompt


def test_contradictory_c7_localized_keeps_its_polarity_end_to_end():
    from core import evaluation_runtime as rt
    [contradiction] = _validate(_contradictory())
    assert (contradiction["competency_code"], contradiction["polarity"]) == ("C7", "contradictory")
    context = build_capability_reference_context(SPEC, {"C7"})
    raw = {"mappings": [_mapping("observation_1", "localized", _token(context, "C7_B"))]}
    [mapping] = validate_capability_mapping_result(raw, [contradiction], context)
    assert mapping["capability_localization"] == "localized"
    assert "polarity" not in mapping and "local_stage" not in mapping
    [final] = rt.build_final_result([contradiction], [mapping])["observations"]
    assert final["capabilities"] == ["C7_B@1"]
    assert (final["polarity"], final["local_stage"], final["contradiction_scope"], final["error_type"]) == (
        "contradictory", None, "comprehension", "conceptual")
    assert {k: final[k] for k in contradiction if k in final} == {
        k: v for k, v in contradiction.items() if k in final}
