"""Tests de T6-C1 : policies versionnées de la base positive et leur
résolution (core/inference_policies.py). Purs : aucune base, aucun modèle.

Le registry positive_basis-1 déclare les patterns C1-C12 par capability_code
(parité exacte avec core/pedagogy/taxonomy_v1.py) ; le resolver les traduit
en capability_definition_id de la taxonomie COURANTE (CurrentTaxonomyContext,
T6-C0) et échoue fermé sur toute version, tout format ou toute sémantique
taxonomique qu'il ne connaît pas.
"""
import dataclasses
import uuid
from types import MappingProxyType

import pytest

from core import inference_policies as pol
from core import inference_service as svc
from core.inference_policies import (
    POSITIVE_BASIS_POLICIES,
    POSITIVE_BASIS_V1,
    POSITIVE_BASIS_V1_POLICY,
    CompetencyPositiveBasisPolicy,
    InvalidPositiveBasisPolicy,
    MasteryPolicy,
    PositiveBasisError,
    PositiveBasisPolicy,
    PositiveBasisPolicyResolver,
    ResolvedPattern,
    ResolvedPositiveBasisPolicy,
    ResolvedStagePolicy,
    SemanticPattern,
    StageRepresentativityPolicy,
    UnsupportedInferenceContext,
    UnsupportedPositiveBasisPolicy,
    UnsupportedTaxonomySemantics,
    resolve_positive_basis_policy,
    validate_positive_basis_policy,
)
from core.pedagogy.taxonomy_v1 import CAPABILITY_CODES, COMPETENCY_CODES, V1_SEMANTIC_REVISION, taxonomy_v1_spec
from tests.test_inference_positive_basis import RELEASE, Dossier, competency_codes, definition_id, taxonomy
from tests.test_inference_service import _start_kwargs

STAGES = ("discovery", "comprehension", "application")


def test_version_is_the_t6b_convention_and_the_only_registered_policy():
    """positive_basis-1 : convention des versions T6 déjà utilisée par les
    tests T6-B ; aucune autre policy, aucun défaut."""
    assert POSITIVE_BASIS_V1 == "positive_basis-1"
    assert _start_kwargs()["positive_basis_version"] == POSITIVE_BASIS_V1
    assert isinstance(POSITIVE_BASIS_POLICIES, MappingProxyType)
    assert dict(POSITIVE_BASIS_POLICIES) == {POSITIVE_BASIS_V1: POSITIVE_BASIS_V1_POLICY}
    assert POSITIVE_BASIS_V1_POLICY.version == POSITIVE_BASIS_V1
    # Format d'entrée : exactement le V2 courant de T6-B (recopié, jamais importé).
    assert pol.SUPPORTED_INPUT_SCHEMA_VERSION == svc.INPUT_SCHEMA_VERSION == 2
    assert pol.SUPPORTED_SEMANTIC_REVISION == V1_SEMANTIC_REVISION == 1
    assert (pol.DISCOVERY, pol.COMPREHENSION, pol.APPLICATION, pol.MASTERY) == svc.CLAIM_STAGES


def test_structures_are_frozen_keyword_only_dataclasses():
    for cls in (SemanticPattern, StageRepresentativityPolicy, MasteryPolicy, CompetencyPositiveBasisPolicy,
                PositiveBasisPolicy, ResolvedPattern, ResolvedStagePolicy, ResolvedPositiveBasisPolicy,
                PositiveBasisPolicyResolver):
        assert cls.__dataclass_params__.frozen, cls
        assert all(f.kw_only for f in dataclasses.fields(cls)), cls
        for f in dataclasses.fields(cls):
            assert f.type not in (int, float, "int", "float"), (cls, f.name)
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(CompetencyPositiveBasisPolicy) == ["competency_code", "capability_codes", "discovery_policy",
                                                    "comprehension_policy", "application_policy", "mastery_policy"]
    assert names(StageRepresentativityPolicy) == ["stage", "representative_patterns", "narrow_scope_patterns",
                                                  "generalization_limits", "competency_only_allowed"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        POSITIVE_BASIS_V1_POLICY.competencies[0].discovery_policy.stage = "mastery"


def test_registry_is_valid_and_matches_taxonomy_v1_exactly():
    validate_positive_basis_policy(POSITIVE_BASIS_V1_POLICY)
    competencies = POSITIVE_BASIS_V1_POLICY.competencies
    assert tuple(c.competency_code for c in competencies) == COMPETENCY_CODES
    spec = taxonomy_v1_spec()
    for competency in competencies:
        expected = tuple(c["capability_code"] for c in spec["capabilities"]
                         if c["competency_code"] == competency.competency_code)
        assert competency.capability_codes == expected
    assert tuple(code for c in competencies for code in c.capability_codes) == CAPABILITY_CODES


def test_every_stage_policy_is_explicit_and_qualitative():
    """Discovery : règle commune (toute capacité noyau, jamais relationnelle)
    ; Comprehension / Application : patterns nommés ; competency_only
    légitime partout ; C11 seule désigne une capacité de révision (C11_D)."""
    for competency in POSITIVE_BASIS_V1_POLICY.competencies:
        discovery = competency.discovery_policy
        [pattern] = discovery.representative_patterns
        assert pattern.core_capability_codes == competency.capability_codes
        assert pattern.related_capability_codes == ()
        for stage in STAGES:
            stage_policy = competency.stage_policy(stage)
            assert stage_policy.stage == stage and stage_policy.competency_only_allowed is True
            if stage != "discovery":
                assert stage_policy.narrow_scope_patterns and stage_policy.generalization_limits
            for p in (*stage_policy.representative_patterns, *stage_policy.narrow_scope_patterns):
                assert p.name.startswith(f"{competency.competency_code.lower()}_")
        expected_revision = ("C11_D",) if competency.competency_code == "C11" else ()
        assert competency.mastery_policy.revision_capability_codes == expected_revision


def _policy(code):
    return next(c for c in POSITIVE_BASIS_V1_POLICY.competencies if c.competency_code == code)


def _shape(stage_policy):
    return {(p.core_capability_codes, p.related_capability_codes) for p in stage_policy.representative_patterns}


def test_specified_c1_to_c12_relations():
    """Les relations de la spécification T6-C1 V1, sous forme de relations
    sémantiques (core, related), jamais de nombres requis."""
    expected = {
        "C1": ({(("C1_A",), ()), (("C1_C",), ())}, {(("C1_A",), ()), (("C1_C",), ())}),
        "C2": ({(("C2_A",), ("C2_B", "C2_C"))}, {(("C2_A",), ("C2_B", "C2_C"))}),
        "C3": ({(("C3_B",), ())}, {(("C3_B",), ())}),
        "C4": ({(("C4_A",), ("C4_B",))}, {(("C4_B",), ("C4_A", "C4_C", "C4_D"))}),
        "C5": ({(("C5_A",), ())}, {(("C5_B",), ("C5_A", "C5_C", "C5_D"))}),
        "C6": ({(("C6_A",), ())}, {(("C6_B",), ())}),
        "C7": ({(("C7_A",), ("C7_C", "C7_B"))}, {(("C7_A",), ("C7_B", "C7_C", "C7_D"))}),
        "C8": ({(("C8_A",), ("C8_B",))}, {(("C8_B",), ("C8_A", "C8_C"))}),
        "C9": ({(("C9_A",), ("C9_B",))}, {(("C9_B",), ("C9_C",))}),
        "C10": ({(("C10_B",), ())}, {(("C10_B",), ("C10_A", "C10_C", "C10_D"))}),
        "C11": ({(("C11_A",), ("C11_B",))}, {(("C11_A",), ("C11_B",)), (("C11_D",), ())}),
        "C12": ({(("C12_B",), ())}, {(("C12_A",), ("C12_B",)), (("C12_B",), ("C12_C",)), (("C12_C",), ("C12_D",))}),
    }
    for code, (comprehension, application) in expected.items():
        assert _shape(_policy(code).comprehension_policy) == comprehension, code
        assert _shape(_policy(code).application_policy) == application, code
    # C10_B seule : Comprehension représentative, Application jamais.
    assert "c10_implied_assumptions_only" in [p.name for p in _policy("C10").application_policy.narrow_scope_patterns]


def _broken(transform):
    competencies = list(POSITIVE_BASIS_V1_POLICY.competencies)
    competencies[9] = transform(competencies[9])  # C10
    return dataclasses.replace(POSITIVE_BASIS_V1_POLICY, competencies=tuple(competencies))


def _with_application(c10, *patterns):
    return dataclasses.replace(c10, application_policy=dataclasses.replace(
        c10.application_policy, representative_patterns=tuple(patterns)))


@pytest.mark.parametrize("transform,message", [
    (lambda c: _with_application(c, SemanticPattern(name="x", core_capability_codes=("C11_A",))),
     "hors de la compétence"),
    (lambda c: _with_application(c, SemanticPattern(name="x", core_capability_codes=("C10_B",),
                                                    related_capability_codes=("C10_B", "C10_A"))), "disjoints"),
    (lambda c: _with_application(c, SemanticPattern(name="x", core_capability_codes=())), "core non vide"),
    (lambda c: _with_application(c), "aucun pattern représentatif"),
    (lambda c: _with_application(c, SemanticPattern(name="x", core_capability_codes=("C10_A",)),
                                 SemanticPattern(name="x", core_capability_codes=("C10_B",))), "en double"),
    (lambda c: dataclasses.replace(c, discovery_policy=dataclasses.replace(
        c.discovery_policy, representative_patterns=(SemanticPattern(
            name="x", core_capability_codes=("C10_A",), related_capability_codes=("C10_B",)),))),
     "jamais relationnelle"),
    (lambda c: dataclasses.replace(c, capability_codes=("C10_A", "C10_B", "C10_C")), "capacités"),
    (lambda c: dataclasses.replace(c, mastery_policy=MasteryPolicy(revision_capability_codes=("C11_D",))),
     "mastery_policy"),
])
def test_invalid_policies_are_refused(transform, message):
    with pytest.raises(InvalidPositiveBasisPolicy, match=message):
        validate_positive_basis_policy(_broken(transform))


def test_missing_competency_is_refused():
    with pytest.raises(InvalidPositiveBasisPolicy, match="C1-C12"):
        validate_positive_basis_policy(dataclasses.replace(
            POSITIVE_BASIS_V1_POLICY, competencies=POSITIVE_BASIS_V1_POLICY.competencies[:-1]))


def test_resolved_pattern_is_a_boolean_semantic_relation():
    a, b, c, d = (definition_id(code) for code in competency_codes("C10"))
    relation = ResolvedPattern(name="r", core_definition_ids=frozenset({b}),
                               related_definition_ids=frozenset({a, c, d}))
    assert relation.satisfied_by({b, a}) and relation.satisfied_by({b, d})
    assert not relation.satisfied_by({b}) and not relation.satisfied_by({a, c, d})
    single = ResolvedPattern(name="s", core_definition_ids=frozenset({b}), related_definition_ids=frozenset())
    assert single.satisfied_by({b}) and not single.satisfied_by({a, c, d})
    assert relation.touched_by({c}) and not single.touched_by({a})


def test_resolution_uses_definition_ids_in_natural_order():
    d = Dossier("C11")
    d.observe("e1", "C11_A", "C11_B")
    resolved = resolve_positive_basis_policy(d.context())
    assert resolved.policy_version == POSITIVE_BASIS_V1 and resolved.competency_code == "C11"
    assert resolved.release_id == RELEASE
    assert resolved.definition_ids == tuple(definition_id(c) for c in competency_codes("C11"))
    assert tuple(c.capability_code for c in resolved.capabilities) == competency_codes("C11")
    assert resolved.revision_definition_ids == frozenset({definition_id("C11_D")})
    assert set(resolved.stage_policies) == set(STAGES)
    assert isinstance(resolved.stage_policies, MappingProxyType)
    [revision] = [p for p in resolved.stage_policies["application"].representative_patterns
                  if p.name == "c11_rational_thesis_revision"]
    assert revision.core_definition_ids == frozenset({definition_id("C11_D")})
    assert resolved.ordered({definition_id("C11_D"), definition_id("C11_A")}) == (definition_id("C11_A"),
                                                                                    definition_id("C11_D"))


def test_future_release_reusing_v1_definitions_resolves_identically():
    d = Dossier("C7")
    d.observe("e1", "C7_A", "C7_B")
    future = uuid.uuid5(uuid.NAMESPACE_URL, "oryx-v2")
    d2 = Dossier("C7", release=future)
    d2.observe("e1", "C7_A", "C7_B")
    first, second = resolve_positive_basis_policy(d.context()), resolve_positive_basis_policy(d2.context())
    assert first.definition_ids == second.definition_ids
    assert first.stage_policies == second.stage_policies
    assert {c.membership_id for c in first.capabilities}.isdisjoint({c.membership_id for c in second.capabilities})


@pytest.mark.parametrize("code", ["C1_B", "C10_B", "C12_D"])
@pytest.mark.parametrize("revision", [2, 3, 0])
def test_unknown_semantic_revision_is_never_remapped_by_code(code, revision):
    competency = code.split("_")[0]
    d = Dossier(competency, revisions={code: revision})
    d.observe("e1", code)
    with pytest.raises(UnsupportedTaxonomySemantics, match=f"{code} semantic_revision {revision}"):
        resolve_positive_basis_policy(d.context())


def test_resolver_rejects_unknown_versions_and_non_v1_contexts():
    d = Dossier("C3")
    d.observe("e1", "C3_B")
    for version in ("positive_basis-2", "positive_basis-1 ", "", None, 1):
        with pytest.raises(UnsupportedPositiveBasisPolicy):
            resolve_positive_basis_policy(d.context(positive_basis_version=version))
    for overrides in ({"input_schema_version": 1}, {"input_schema_version": 3}, {"model_id": "m"},
                      {"prompt_spec_version": "p"}):
        with pytest.raises(UnsupportedInferenceContext):
            resolve_positive_basis_policy(d.context(**overrides))
    # Un registry fourni explicitement n'ouvre aucun défaut.
    with pytest.raises(UnsupportedPositiveBasisPolicy):
        PositiveBasisPolicyResolver(registry=MappingProxyType({})).resolve(d.context())


def test_resolver_validates_a_supplied_registry():
    d = Dossier("C3")
    d.observe("e1", "C3_B")
    broken = _broken(lambda c: _with_application(c))
    with pytest.raises(InvalidPositiveBasisPolicy):
        PositiveBasisPolicyResolver(registry=MappingProxyType({POSITIVE_BASIS_V1: broken})).resolve(d.context())


def test_errors_share_one_business_base():
    for exc in (InvalidPositiveBasisPolicy, UnsupportedPositiveBasisPolicy, UnsupportedInferenceContext,
                UnsupportedTaxonomySemantics, pol.InconsistentInferenceContext):
        assert exc.__bases__ == (PositiveBasisError,), exc
    assert PositiveBasisError.__bases__ == (Exception,)


def test_taxonomy_context_order_is_irrelevant():
    d = Dossier("C5")
    d.observe("e1", "C5_B", "C5_A")
    context = taxonomy("C5")
    reversed_context = dataclasses.replace(context, capabilities=tuple(reversed(context.capabilities)))
    assert resolve_positive_basis_policy(d.context(taxonomy_context=reversed_context)) == \
        resolve_positive_basis_policy(d.context())
