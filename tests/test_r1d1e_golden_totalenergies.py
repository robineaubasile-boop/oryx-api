"""Golden regression R1-D1E : le cas production TotalEnergies qui a révélé
les deux défauts sémantiques de R1-D1 (run 7dec8f64-a56b-43ae-bb3a-
3b9bfa6609ae, event 10686e9e-9544-418b-9283-2c67896de876) :

1. deux raisonnements contextualisés et substantiels classés
   local_stage = comprehension (le modèle V1 choisissait le stade) ;
2. une observation C4 sur le poids des segments dans le RÉSULTAT
   opérationnel, les marges et l'exposition au baril, sur-localisée sur
   C4_B (moteur de CHIFFRE D'AFFAIRES) au lieu de competency_only.

Tests PURS (aucune base, aucun réseau, aucun LLM) : ils figent le contrat
V2 et la calibration attendue À PARTIR DU CONTENU (stimulus, contributions
exactes, aide disponible avant la contribution 2) : la sortie conforme
d'un évaluateur correct (stage_basis, évaluations de capacités) produit,
par dérivation SERVEUR, application + competency_only pour #1 et
application + C5_D@1 pour #2. Aucun code de production ne contient de
règle « TotalEnergies => C4 / C5 ».
"""
import copy
import json

import pytest

from core import capability_mapper as cm
from core import evaluation_runtime as rt
from core import local_evaluator as le
from core.capability_mapper import build_capability_reference_context
from core.local_evaluator import EvaluationOutputInvalid
from core.pedagogy.taxonomy_v1 import load_taxonomy_v1
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

SPEC, _ = load_taxonomy_v1()

CONTRIBUTION_1 = (
    "Je dirais que l'essentiel du résultat opérationnel vient encore de l'amont, l'exploration-production et le "
    "GNL : ce sont eux qui profitent directement des prix du pétrole et du gaz. La partie électricité/renouvelables, "
    "c'est là que les volumes croissent le plus vite, mais elle part d'une base petite avec des marges plus faibles, "
    "donc elle pèse encore peu dans le résultat. Du coup je vois la croissance en pourcentage côté Integrated Power, "
    "mais le cash reste porté par les hydrocarbures, ce qui rend le résultat global très dépendant du prix du baril."
)
CONTRIBUTION_2 = (
    "Plutôt une diversification défensive à mon avis. Les marges de l'électricité sont structurellement basses et "
    "l'activité demande beaucoup de capital, donc même si elle grossit, je vois mal qu'elle dépasse l'amont en "
    "résultat d'ici 5-10 ans, sauf si les prix des hydrocarbures baissent durablement. Elle sert surtout à préparer "
    "la transition et à rendre le groupe moins exposé si la demande de pétrole décline."
)
SUPPORT_1 = (
    "L'amont (Exploration-Production) et Integrated LNG génèrent la majorité du résultat opérationnel : ce sont des "
    "activités à forte marge mais cycliques, très dépendantes des prix des hydrocarbures. Integrated Power croît "
    "vite, mais avec des marges structurellement plus faibles. Selon toi, Integrated Power est-il le vrai futur "
    "moteur de résultat à 5-10 ans, ou plutôt une diversification défensive ?"
)

GOLDEN_PAYLOAD = {
    "event_token": "event_1",
    "stimulus": {"visible_content": (
        "TotalEnergies — segments : Exploration-Production, Integrated LNG, Refining & Chemicals, Integrated Power. "
        "Question : quel segment génère la majorité du résultat opérationnel et où se situe la croissance ?")},
    "contributions": [
        {"contribution_token": "contribution_1", "phase": 1, "support_before": [], "text": CONTRIBUTION_1},
        {"contribution_token": "contribution_2", "phase": 2, "support_before": ["support_1"], "text": CONTRIBUTION_2},
    ],
    "support_catalog": [
        {"support_token": "support_1", "sequence_no": 2, "support_kind": "assistant_response",
         "visible_content": SUPPORT_1},
    ],
}

FULL_BASIS = {"contextualized_use": True, "substantive_selection_adaptation_interpretation": True,
              "semantic_mechanism_explained": True, "cognitive_discrimination": True}

# Sortie D1B V2 conforme d'un évaluateur calibré (aucune clé local_stage).
GOLDEN_D1B_V2 = {"observations": [
    {
        "observation_token": "observation_1",
        "competency_code": "C4",
        "observation_role": "primary",
        "task_kind": "analysis",
        "primary_user_action": {"action": "connect", "contribution_token": "contribution_1"},
        "contributive_user_actions": [],
        "elicitation_mode": "prompted",
        "support_level": "none",
        "source_contribution_tokens": ["contribution_1"],
        "residual_cognitive_work": {
            "operations_left_to_user": [
                "attribuer le résultat opérationnel aux segments amont et GNL",
                "distinguer croissance des volumes et poids dans le résultat",
                "relier la dépendance du résultat global au prix du baril"],
            "materially_used_support_refs": [],
            "summary": "Aucune aide avant la contribution : toute la mise en relation est produite par l'utilisateur.",
        },
        "polarity": "supportive",
        "evidence_strength": "medium",
        "stage_basis": dict(FULL_BASIS),
        "contradiction_scope": None,
        "error_type": None,
        "observation_text": ("Relie le poids des segments amont dans le résultat opérationnel à leur exposition aux "
                             "prix des hydrocarbures et distingue croissance en volume et contribution au résultat."),
    },
    {
        "observation_token": "observation_2",
        "competency_code": "C5",
        "observation_role": "primary",
        "task_kind": "analysis",
        "primary_user_action": {"action": "hypothesize", "contribution_token": "contribution_2"},
        "contributive_user_actions": [],
        "elicitation_mode": "prompted",
        "support_level": "hinted",
        "source_contribution_tokens": ["contribution_2"],
        "residual_cognitive_work": {
            "operations_left_to_user": [
                "juger si la croissance d'Integrated Power peut devenir un moteur de résultat",
                "relier marges basses et intensité en capital à la qualité de cette croissance",
                "formuler la condition (prix durablement bas) qui changerait la conclusion"],
            "materially_used_support_refs": [{"support_token": "support_1", "contribution_token": "contribution_2"}],
            "summary": "L'aide posait l'alternative et les marges ; le jugement conditionnel reste construit par "
                       "l'utilisateur.",
        },
        "polarity": "supportive",
        "evidence_strength": "medium",
        "stage_basis": dict(FULL_BASIS),
        "contradiction_scope": None,
        "error_type": None,
        "observation_text": ("Qualifie la croissance d'Integrated Power de diversification défensive au regard de ses "
                             "marges basses et du capital qu'elle exige."),
    },
]}


def _tokens(context, competency):
    return {context.by_token[t][0]: t for t in context.candidates[competency]}


def _golden_d1d(context):
    """Sortie D1D V2 conforme : #1 aucune capacité C4 exacte ; #2 C5_D
    seule."""
    c4, c5 = _tokens(context, "C4"), _tokens(context, "C5")
    reasons_c4 = {"C4_A": "semantic_mismatch", "C4_B": "insufficient_specificity",
                  "C4_C": "insufficient_specificity", "C4_D": "semantic_mismatch"}
    reasons_c5 = {"C5_A": "insufficient_specificity", "C5_B": "insufficient_specificity",
                  "C5_C": "semantic_mismatch"}
    return {"mappings": [
        {"observation_token": "observation_1", "capability_assessments": [
            {"capability_token": c4[code], "supported": False, "reason": reasons_c4[code]} for code in sorted(c4)]},
        {"observation_token": "observation_2", "capability_assessments": [
            {"capability_token": c5[code], "supported": code == "C5_D",
             "reason": "include_satisfied" if code == "C5_D" else reasons_c5[code]} for code in sorted(c5)]},
    ]}


@pytest.fixture
def candidates():
    return le.validate_local_evaluation_result_v2(copy.deepcopy(GOLDEN_D1B_V2), GOLDEN_PAYLOAD)


@pytest.fixture
def context(candidates):
    return build_capability_reference_context(SPEC, {c["competency_code"] for c in candidates})


# --------------------------------------------------------------------------
# D1B V2 : stade dérivé serveur
# --------------------------------------------------------------------------

def test_observation_1_is_c4_connect_unaided_medium_application(candidates):
    first = candidates[0]
    assert (first["competency_code"], first["primary_user_action"]["action"], first["support_level"],
            first["polarity"], first["evidence_strength"]) == ("C4", "connect", "none", "supportive", "medium")
    assert first["local_stage"] == "application"
    assert first["residual_cognitive_work"]["materially_used_support_refs"] == []


def test_observation_2_is_c5_hypothesize_hinted_medium_application(candidates):
    second = candidates[1]
    assert (second["competency_code"], second["primary_user_action"]["action"], second["support_level"],
            second["polarity"], second["evidence_strength"]) == ("C5", "hypothesize", "hinted", "supportive",
                                                                  "medium")
    # Une aide au raisonnement réduit l'autonomie observable sans plafonner
    # mécaniquement le stade.
    assert second["local_stage"] == "application"
    assert second["residual_cognitive_work"]["materially_used_support_refs"] == [
        {"support_token": "support_1", "contribution_token": "contribution_2"}]


def test_the_v2_model_can_no_longer_under_calibrate_by_choosing_comprehension():
    """Le défaut production : le modèle choisissait local_stage =
    comprehension. En V2 cette clé est refusée ; seul stage_basis compte."""
    for index in (0, 1):
        raw = copy.deepcopy(GOLDEN_D1B_V2)
        raw["observations"][index]["local_stage"] = "comprehension"
        with pytest.raises(EvaluationOutputInvalid, match="en trop"):
            le.validate_local_evaluation_result_v2(raw, GOLDEN_PAYLOAD)


def test_the_support_was_only_causally_available_before_contribution_2():
    raw = copy.deepcopy(GOLDEN_D1B_V2)
    raw["observations"][0]["support_level"] = "hinted"
    raw["observations"][0]["residual_cognitive_work"]["materially_used_support_refs"] = [
        {"support_token": "support_1", "contribution_token": "contribution_1"}]
    with pytest.raises(EvaluationOutputInvalid, match="causalité"):
        le.validate_local_evaluation_result_v2(raw, GOLDEN_PAYLOAD)


def test_the_golden_request_carries_the_exact_production_content():
    request = le.build_evaluator_request_v2(GOLDEN_PAYLOAD, le.build_competency_reference_context(SPEC))
    data = json.loads(request.user)["evaluation_input"]
    assert [c["text"] for c in data["contributions"]] == [CONTRIBUTION_1, CONTRIBUTION_2]
    assert [c["support_before"] for c in data["contributions"]] == [[], ["support_1"]]
    assert request.system == le.SYSTEM_PROMPT_V2


# --------------------------------------------------------------------------
# D1D V2 : localisation dérivée serveur
# --------------------------------------------------------------------------

def test_d1d_request_assesses_c4_and_c5_candidates_with_the_residual_work(candidates, context):
    data = json.loads(cm.build_mapping_request_v2(candidates, context, GOLDEN_PAYLOAD).user)
    first, second = data["observations"]
    assert [context.by_token[t][0] for t in first["candidate_capability_tokens"]] == ["C4_A", "C4_B", "C4_C", "C4_D"]
    assert [context.by_token[t][0] for t in second["candidate_capability_tokens"]] == ["C5_A", "C5_B", "C5_C", "C5_D"]
    assert (first["local_stage"], second["local_stage"]) == ("application", "application")
    assert first["residual_cognitive_work"] == candidates[0]["residual_cognitive_work"]
    assert [c["text"] for c in data["source_contributions"]] == [CONTRIBUTION_1, CONTRIBUTION_2]


def test_observation_1_is_competency_only_and_observation_2_is_c5_d(candidates, context):
    first, second = cm.validate_capability_mapping_result_v2(_golden_d1d(context), candidates, context)
    assert (first["capability_localization"], first["capabilities"]) == ("competency_only", [])
    assert second["capability_localization"] == "localized"
    assert [(c["capability_code"], c["semantic_revision"]) for c in second["capabilities"]] == [("C5_D", 1)]


def test_c4_b_is_only_selected_by_an_explicit_include_satisfied_judgement(candidates, context):
    """Le défaut production (C4_B « la plus proche ») n'est plus une
    décision libre de localisation : il faudrait que le modèle affirme
    explicitement include_satisfied pour C4_B ; « proche mais non exacte »
    reste supported=false et ne peut pas porter include_satisfied."""
    raw = _golden_d1d(context)
    c4_b = _tokens(context, "C4")["C4_B"]
    [assessment] = [a for a in raw["mappings"][0]["capability_assessments"] if a["capability_token"] == c4_b]
    assessment["reason"] = "include_satisfied"
    with pytest.raises(EvaluationOutputInvalid, match="supported=false"):
        cm.validate_capability_mapping_result_v2(raw, candidates, context)
    # La sortie V1 de production (decision libre "localized" sur C4_B) est
    # hors contrat V2.
    production_v1 = {"mappings": [
        {"observation_token": "observation_1", "localization": "localized", "capability_tokens": [c4_b]},
        {"observation_token": "observation_2", "localization": "localized",
         "capability_tokens": [_tokens(context, "C5")["C5_D"]]}]}
    with pytest.raises(EvaluationOutputInvalid):
        cm.validate_capability_mapping_result_v2(production_v1, candidates, context)
    # C4_B porte bien sur le chiffre d'affaires, pas sur le résultat.
    [c4_b_spec] = [c for c in SPEC["capabilities"] if c["capability_code"] == "C4_B"]
    assert "chiffre d'affaires" in c4_b_spec["definition"]


# --------------------------------------------------------------------------
# Résultat final
# --------------------------------------------------------------------------

def test_final_semantic_result_and_fingerprint(candidates, context):
    mappings = cm.validate_capability_mapping_result_v2(_golden_d1d(context), candidates, context)
    final = rt.build_final_result(candidates, mappings)
    first, second = final["observations"]
    assert (first["competency_code"], first["local_stage"], first["capability_localization"],
            first["capabilities"]) == ("C4", "application", "competency_only", [])
    assert (second["competency_code"], second["local_stage"], second["capability_localization"],
            second["capabilities"]) == ("C5", "application", "localized", ["C5_D@1"])
    serialized = json.dumps(final)
    for transient in ("stage_basis", "capability_assessments", "include_satisfied", "insufficient_specificity"):
        assert transient not in serialized, transient
    assert rt.compute_output_fingerprint(final) == rt.compute_output_fingerprint(json.loads(serialized))


def test_no_evaluation_code_hardcodes_the_golden_case():
    paths = [REPO_ROOT / "core" / name for name in ("local_evaluator.py", "capability_mapper.py")]
    paths += list((REPO_ROOT / "core").glob("evaluation_*.py"))
    assert len(paths) == 6
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for word in ("TotalEnergies", "Integrated Power", "baril", "pétrole", "C4_B", "C5_D"):
            assert word not in source, (path.name, word)
