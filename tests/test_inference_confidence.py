"""Tests de T6-C2 : Confidence Profile Engine (core/inference_confidence.py).

Tests purs uniquement : chaque InferenceContext est construit par le builder
de T6-C1 (tests/test_inference_positive_basis.py), qui fait dériver le
LongitudinalDossier par les fonctions PURES réelles de T5-C, puis évalué par
evaluate_positive_basis réel (T6-C1) et evaluate_inference_state réel
(T6-C2) : T5-C réel -> T6-C1 réel -> T6-C2 réel. Aucune base, aucun modèle.
"""
import ast
import dataclasses
import re
from datetime import timedelta

from core import inference_confidence as confidence_module
from core.inference_confidence import (
    ClaimEvidenceContext,
    ConfidenceDimensionAssessment,
    ConfidenceFact,
    ConfidenceProfileAssessment,
    ContradictionClaimReading,
    ContradictionEvidence,
    project_claim_contexts,
)
from core.inference_positive_basis import StructuralRef, evaluate_positive_basis
from core.inference_service import CONFIDENCE_DIMENSIONS as T6B_CONFIDENCE_DIMENSIONS
from core.inference_state import evaluate_inference_state
from core.inference_state_policies import CONFIDENCE_DIMENSIONS, DIMENSION_FACT_CODES
from tests.test_inference_positive_basis import EPOCH, STAGES, Dossier, _mastery_dossier, definition_id
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

CONFIDENCE_PATH = REPO_ROOT / "core" / "inference_confidence.py"


def evaluate(d, **overrides):
    context = d.context(**overrides)
    return evaluate_inference_state(context, evaluate_positive_basis(context))


def profile(result, stage):
    return result.confidence_profiles[STAGES.index(stage)]


def facts(result, stage, dimension):
    return getattr(profile(result, stage), dimension).fact_codes


def _expert(competency="C2", caps=("C2_A", "C2_B"), **kwargs):
    d = Dossier(competency)
    o = d.observe("e1", *caps, name="expert", **kwargs)
    return d, o


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------

STRUCTURES = (ClaimEvidenceContext, ContradictionEvidence, ContradictionClaimReading, ConfidenceFact,
              ConfidenceDimensionAssessment, ConfidenceProfileAssessment)
FORBIDDEN_FIELDS = ("score", "level", "rating", "ratio", "percentage", "count", "weight", "average",
                    "confidence_score", "overall_confidence", "high", "medium", "low", "freshness", "points")


def test_structures_are_frozen_keyword_only_dataclasses_without_numbers():
    for cls in STRUCTURES:
        assert cls.__dataclass_params__.frozen, cls
        assert all(f.kw_only for f in dataclasses.fields(cls)), cls
        for f in dataclasses.fields(cls):
            assert f.name not in FORBIDDEN_FIELDS, (cls, f.name)
            assert f.type not in (int, float, "int", "float"), (cls, f.name)
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(ConfidenceProfileAssessment) == ["schema_version", "stage", *CONFIDENCE_DIMENSIONS]
    assert names(ConfidenceDimensionAssessment) == ["dimension", "fact_codes", "observation_ids", "structural_refs",
                                                    "capability_definition_ids", "limitations", "facts"]
    assert names(ClaimEvidenceContext)[:3] == ["stage", "basis_mode", "implied_from_stage"]
    assert set(CONFIDENCE_DIMENSIONS) == T6B_CONFIDENCE_DIMENSIONS
    assert tuple(DIMENSION_FACT_CODES) == CONFIDENCE_DIMENSIONS


def test_fact_vocabulary_is_closed_descriptive_and_never_a_level():
    for dimension, codes in DIMENSION_FACT_CODES.items():
        assert len(set(codes)) == len(codes), dimension
        for code in codes:
            words = set(code.split("_"))
            assert words.isdisjoint({"high", "medium", "low", "score", "level", "fresh", "stale", "old", "recent",
                                     "decay", "ratio", "percent", "count", "weak", "strong"}), code


# --------------------------------------------------------------------------
# 15-17 : présence et forme des profils
# --------------------------------------------------------------------------

def test_not_established_claim_has_no_confidence_profile_never_low():
    """15."""
    d, _ = _expert()
    result = evaluate(d)
    assert profile(result, "mastery") is None
    empty = evaluate(Dossier("C2"))
    assert empty.confidence_profiles == (None, None, None, None)


def test_every_established_claim_has_exactly_five_separate_dimensions():
    """16."""
    for builder in (lambda: _expert()[0], lambda: _mastery_dossier()[0], _mixed):
        result = evaluate(builder())
        positive = evaluate_positive_basis(builder().context())
        for claim, prof in zip(positive.claims, result.confidence_profiles):
            assert (prof is not None) == (claim.status == "established"), claim.stage
            if prof is None:
                continue
            assert prof.schema_version == "confidence-profile-v1" and prof.stage == claim.stage
            for name in CONFIDENCE_DIMENSIONS:
                dimension = getattr(prof, name)
                assert dimension.dimension == name
                assert dimension.fact_codes, (claim.stage, name)
                assert set(dimension.fact_codes) <= set(DIMENSION_FACT_CODES[name])
                assert dimension.fact_codes == tuple(f.code for f in dimension.facts)
                assert list(dimension.fact_codes) == sorted(dimension.fact_codes,
                                                            key=DIMENSION_FACT_CODES[name].index)


def _walk(value):
    if dataclasses.is_dataclass(value):
        yield type(value).__name__
        for f in dataclasses.fields(value):
            yield f.name
            yield from _walk(getattr(value, f.name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def test_no_profile_contains_score_ratio_percentage_or_level():
    """17 : aucun nombre, aucun niveau, dans toute la sortie."""
    for builder in (lambda: _expert()[0], lambda: _mastery_dossier()[0], _mixed):
        result = evaluate(builder())
        for value in _walk(result):
            assert type(value) not in (int, float), value
            if isinstance(value, str):
                assert set(re.split(r"[^a-z]+", value.lower())).isdisjoint(
                    {"score", "ratio", "percentage", "percent", "high", "medium", "low", "level", "rating",
                     "average", "weight", "count"}), value


# --------------------------------------------------------------------------
# Diagnosticity
# --------------------------------------------------------------------------

def test_direct_representative_basis_projects_the_t6c1_basis_kind():
    d, o = _expert()
    result = evaluate(d)
    diagnosticity = profile(result, "application").diagnosticity
    assert diagnosticity.fact_codes == ("direct_representative_basis", "single_representative_demonstration")
    assert diagnosticity.observation_ids == (o,)
    assert diagnosticity.capability_definition_ids == (definition_id("C2_A"), definition_id("C2_B"))


def test_implied_claim_keeps_descriptive_provenance_without_a_second_positive_basis():
    """§5 : Application directe O42, Comprehension / Discovery implied. O42
    reste la base positive de la SEULE claim Application (T6-C1 inchangé),
    mais le profil de confiance des claims implied reste explicable par O42
    (provenance descriptive, rôle confidence), sur le périmètre hérité."""
    d = Dossier("C2")
    o42 = d.observe("e42", "C2_A", "C2_B", name="O42")
    positive = evaluate_positive_basis(d.context())
    contexts = project_claim_contexts(positive)
    comprehension = contexts["comprehension"]
    assert (comprehension.basis_mode, comprehension.implied_from_stage, comprehension.effective_basis_stage) == (
        "implied_by_higher_claim", "application", "application")
    assert comprehension.effective_basis_observation_ids == (o42,)
    assert comprehension.represented_capability_definition_ids == \
        contexts["application"].represented_capability_definition_ids
    assert "mastery" not in contexts
    result = evaluate(d)
    for stage in ("discovery", "comprehension"):
        implied = profile(result, stage)
        assert implied.diagnosticity.fact_codes == ("implied_from_higher_claim", "single_representative_demonstration")
        assert implied.diagnosticity.observation_ids == (o42,)
        assert all(f.observation_ids == (o42,) for f in implied.diagnosticity.facts)
        assert implied.diagnosticity.capability_definition_ids == (definition_id("C2_A"), definition_id("C2_B"))
        assert implied.independence.observation_ids == (o42,)
        assert implied.temporal_validation.observation_ids == (o42,)
    # T6-C1 inchangé : O42 n'est base positive que d'Application.
    assert [c.basis_observation_ids for c in positive.claims] == [(), (), (o42,), ()]
    assert [c.basis_mode for c in positive.claims] == ["implied_by_higher_claim", "implied_by_higher_claim",
                                                        "direct", "none"]


def test_claims_implied_by_mastery_keep_the_t5_structure_as_confidence_provenance():
    d, a, b, t = _mastery_dossier()
    result = evaluate(d)
    application = profile(result, "application")
    assert application.diagnosticity.fact_codes == ("implied_from_higher_claim", "longitudinal_mastery_basis")
    assert application.diagnosticity.structural_refs == (StructuralRef(relation_kind="transfer", relation_id=t),)
    assert set(application.diagnosticity.observation_ids) == {a, b}
    assert "durability_evidence_present" in application.temporal_validation.fact_codes
    assert application.temporal_validation.structural_refs == (StructuralRef(relation_kind="transfer", relation_id=t),)
    positive = evaluate_positive_basis(d.context())
    assert [c.basis_observation_ids for c in positive.claims[:3]] == [(), (), ()]


def test_evidence_strength_never_changes_diagnosticity():
    """Aucun « strong => diagnosticity élevée » : la force locale T3 reste
    une propriété descriptive des observations."""
    profiles = []
    for strength in ("weak", "medium", "strong"):
        d, _ = _expert(strength=strength)
        profiles.append(profile(evaluate(d), "application").diagnosticity)
    assert profiles[0] == profiles[1] == profiles[2]


def test_mastery_diagnosticity_is_longitudinal_and_cites_t5_structure():
    d, a, b, t = _mastery_dossier()
    result = evaluate(d)
    diagnosticity = profile(result, "mastery").diagnosticity
    assert diagnosticity.fact_codes == ("direct_representative_basis", "longitudinal_mastery_basis")
    assert diagnosticity.structural_refs == (StructuralRef(relation_kind="transfer", relation_id=t),)
    assert set(diagnosticity.observation_ids) == {a, b}
    application = profile(result, "application").diagnosticity
    assert application.fact_codes == ("implied_from_higher_claim", "longitudinal_mastery_basis")


def test_complementary_history_and_episode_are_distinguished():
    d = Dossier("C2")
    k = d.contra("k", "C2_A", name="k")
    a = d.observe("e1", "C2_A", name="a")
    b = d.observe("e2", "C2_B", name="b")
    d.revalidation(k, a)
    j = d.contra("j", "C2_B", name="j")
    d.revalidation(j, b)
    assert "complementary_representative_history" in facts(evaluate(d), "application", "diagnosticity")
    d = Dossier("C2")
    d.observe("e1", "C2_A", name="a")
    d.observe("e1", "C2_B", name="b")
    assert "single_representative_episode" in facts(evaluate(d), "application", "diagnosticity")


# --------------------------------------------------------------------------
# 18 : coverage relative à la claim, jamais une annulation
# --------------------------------------------------------------------------

def test_coverage_is_relative_to_the_claim_and_never_cancels_it():
    """18."""
    d = Dossier("C10")
    d.observe("e1", "C10_B", "C10_A", name="a")
    result = evaluate(d)
    coverage = profile(result, "application").coverage
    assert coverage.fact_codes == ("localized_representative_scope", "coverage_concentrated_on_claim_scope",
                                   "unobserved_capabilities_present")
    unobserved = next(f for f in coverage.facts if f.code == "unobserved_capabilities_present")
    assert unobserved.capability_definition_ids == (definition_id("C10_C"), definition_id("C10_D"))
    assert result.current_stage == "application"
    # Un périmètre positif supplémentaire est décrit, jamais compté.
    d.observe("e2", "C10_D", stage="discovery", name="x")
    result = evaluate(d)
    coverage = profile(result, "application").coverage
    assert "additional_positive_scope_present" in coverage.fact_codes
    assert next(f for f in coverage.facts if f.code == "additional_positive_scope_present"
                ).capability_definition_ids == (definition_id("C10_D"),)
    assert result.current_stage == "application"


def test_competency_only_scope_stays_competency_only_in_coverage():
    """33 (confiance) : aucune capacité inventée."""
    d = Dossier("C10")
    o = d.observe("e1", name="o")
    coverage = profile(evaluate(d), "application").coverage
    assert "competency_only_scope" in coverage.fact_codes
    assert "localized_representative_scope" not in coverage.fact_codes
    assert coverage.observation_ids == (o,)
    scoped = next(f for f in coverage.facts if f.code == "competency_only_scope")
    assert scoped.capability_definition_ids == ()


# --------------------------------------------------------------------------
# 19 : independence (absence de dépendance != indépendance)
# --------------------------------------------------------------------------

def test_independence_not_established_never_cancels_an_application():
    """19."""
    d, o = _expert()
    result = evaluate(d)
    independence = profile(result, "application").independence
    assert independence.fact_codes == ("single_episode_only", "independence_not_established")
    assert result.current_stage == "application"
    assert result.state_reason_code == "highest_compatible_positive_claim"


def test_dependency_and_independence_evidence_are_read_from_t5():
    d = Dossier("C2")
    s = d.observe("e0", "C2_A", "C2_B", stage="discovery", name="s")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    dep = d.dependency(a, s, kind="partially_dependent", caps=["C2_B"])
    independence = profile(evaluate(d), "application").independence
    assert independence.fact_codes == ("single_episode_only", "partial_dependency_present",
                                       "independence_not_established", "mixed_dependency_structure")
    assert StructuralRef(relation_kind="dependency", relation_id=dep) in independence.structural_refs
    m, a2, b2, t = _mastery_dossier()
    independence = profile(evaluate(m), "mastery").independence
    assert "independence_evidence_present" in independence.fact_codes
    assert "independent_remobilization_present" in independence.fact_codes
    assert "single_episode_only" not in independence.fact_codes


# --------------------------------------------------------------------------
# 10-11, 21 : consistency seule lit les contradictions
# --------------------------------------------------------------------------

def test_absence_of_contradiction_is_no_observed_current_tension_never_high():
    """11."""
    d, _ = _expert()
    consistency = profile(evaluate(d), "application").consistency
    assert consistency.fact_codes == ("no_observed_current_tension",)
    assert consistency.observation_ids == () and consistency.limitations == ()


def test_historically_revalidated_contradiction_stays_visible_without_open_tension():
    """10."""
    d = Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    k = d.contra("e2", "C2_A", "C2_B", name="k")
    r = d.observe("e3", "C2_A", "C2_B", name="r")
    rv = d.revalidation(k, r)
    result = evaluate(d)
    consistency = profile(result, "application").consistency
    assert consistency.fact_codes == ("no_observed_current_tension", "historically_revalidated_contradiction_present")
    assert consistency.observation_ids == (k,)
    assert consistency.structural_refs == (StructuralRef(relation_kind="revalidation", relation_id=rv),)
    assert result.tensions == ()
    assert a


def test_contradictions_never_alter_the_other_dimensions():
    """21."""
    d, _ = _expert()
    before = profile(evaluate(d), "application")
    d.contra("k1", "C2_A", "C2_B", name="k1")
    d.contra("k2", "C2_C", scope="recognition", name="k2")
    d.contra("k3", strength="weak", scope="undetermined", name="k3")
    after = profile(evaluate(d), "application")
    for name in ("diagnosticity", "coverage", "independence", "temporal_validation"):
        assert getattr(before, name) == getattr(after, name), name
    assert before.consistency != after.consistency


def test_contradiction_outside_claim_scope_is_described_not_counted():
    """7 (consistency) : C8_C ne touche pas une claim portée par C8_A."""
    d = Dossier("C8")
    d.observe("e1", "C8_A", "C8_B", stage="comprehension", name="c")
    k = d.contra("e2", "C8_D", scope="comprehension", name="k")
    consistency = profile(evaluate(d), "comprehension").consistency
    assert consistency.fact_codes == ("no_observed_current_tension", "contradiction_outside_claim_scope_present")
    assert consistency.observation_ids == (k,)


def test_recurrence_candidate_is_described_never_an_independent_recurrence():
    d, _ = _expert()
    for i in range(3):
        d.contra(f"k{i}", "C2_B", strength="medium", name=f"k{i}")
    consistency = profile(evaluate(d), "application").consistency
    assert "structural_recurrence_candidate_present" in consistency.fact_codes
    assert "open_comparable_contradiction_present" in consistency.fact_codes


# --------------------------------------------------------------------------
# 20 : temporal_validation
# --------------------------------------------------------------------------

def test_temporal_validation_reads_t5_facts_never_timestamps():
    """20 : décaler les horodatages techniques ne change rien."""
    def build(clock):
        d = Dossier("C2", clock=clock)
        a = d.observe("e1", "C2_A", "C2_B", name="a")
        b = d.observe("e2", "C2_A", "C2_C", name="b")
        d.transfer(a, b, caps=["C2_A"])
        d.contra("e3", "C2_B", name="k")
        return evaluate(d)

    baseline = build(EPOCH)
    assert baseline == build(EPOCH + timedelta(days=3650)) == build(EPOCH - timedelta(days=900))
    temporal = profile(baseline, "mastery" if profile(baseline, "mastery") else "application").temporal_validation
    assert "exact_demonstration_time_unavailable" in temporal.fact_codes
    assert not {"fresh", "stale", "recent", "old"} & set("_".join(temporal.fact_codes).split("_"))


def test_temporal_validation_facts_for_single_and_remobilized_bases():
    d, _ = _expert()
    assert facts(evaluate(d), "application", "temporal_validation") == ("single_episode_only",
                                                                         "exact_demonstration_time_unavailable")
    m, *_ = _mastery_dossier()
    assert facts(evaluate(m), "mastery", "temporal_validation") == (
        "distinct_positive_episodes_present", "independent_remobilization_present", "durability_evidence_present",
        "exact_demonstration_time_unavailable")


# --------------------------------------------------------------------------
# Pureté du module
# --------------------------------------------------------------------------

def _mixed():
    d = Dossier("C12")
    a = d.observe("e1", "C12_A", "C12_B", name="a")
    b = d.observe("e2", "C12_B", "C12_C", name="b", support="hinted")
    d.transfer(a, b, caps=["C12_B"])
    d.observe("e3", "C12_B", stage="comprehension", strength="medium", name="c")
    d.observe("e5", stage="application", strength="medium", name="e")
    k = d.contra("e6", "C12_C", name="k")
    r = d.observe("e7", "C12_C", "C12_D", name="r")
    d.revalidation(k, r)
    d.contra("e8", "C12_B", scope="comprehension", strength="medium", name="k2")
    d.contra("e9", scope="undetermined", name="k3")
    return d


def test_confidence_module_imports_and_tokens_are_pure():
    imported = set()
    for node in ast.walk(ast.parse(CONFIDENCE_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"uuid", "dataclasses", "core.inference_policies", "core.inference_positive_basis",
                        "core.inference_service", "core.inference_state_policies"}
    tokens = _code_tokens(CONFIDENCE_PATH.read_text(encoding="utf-8")).split("\n")
    for field in ("observation_text", "primary_user_action", "residual_cognitive_work", "event_started_at_technical",
                  "event_closed_at_technical", "observation_created_at_inference", "run_completed_at_technical",
                  "demonstration_time", "predecessor", "error_type_weight"):
        assert field not in tokens, field
    assert confidence_module.DOSSIER_LIMITATIONS
