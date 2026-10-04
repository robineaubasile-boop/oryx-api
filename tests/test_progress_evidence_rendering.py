"""Tests de l'Étape 6.4B3 (frontière déterministe) : contrats, préflight,
chemin déterministe et validateur du rendu fidèle des exemples
(core/progress_evidence_rendering.py).

1. Contrats : versions, dataclasses immuables, API publique, hiérarchie
   d'erreurs, exclusions publiques (aucune métadonnée, aucun stade, aucun
   UUID, aucun score, aucun résumé).
2. Versions amont : 6-4A (module) et 6-4B2 (module et objet reçu).
3. Statuts non available : aucun exemple, provenance vide exigée.
4. available : 1 à MAX_SELECTED_EVIDENCE exemples, provenance, jetons,
   candidates structurellement exploitables.
5. Carte 6-4A : structure et cohérence avec le statut.
6. Chemin déterministe : non available seulement, jamais de repli.
7. Validateur : mapping 1:1 strict, ordre exact, texte mono-ligne ; aucune
   limite par exemple ; evidence_token préservé.
8. Planning forgé, déterminisme, entrées non modifiées.
9. Contrat statique : imports, aucune base, aucune persistance, aucune
   trace de support, aucune borne de texte, aucun branchement runtime.
"""
import ast
import dataclasses
import inspect
import re
import typing

import pytest

from core import observation_service as t3
from core import progress_evidence as pe
from core import progress_evidence_rendering as ren
from core import progress_evidence_selection as sel
from core import progress_projection as pp
from core.progress_evidence import ProgressEvidenceCandidate
from core.progress_evidence_rendering import (
    PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
    PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
    InvalidProgressEvidenceRenderingInput,
    InvalidProgressEvidenceRenderingProposal,
    ProgressEvidenceRenderingCapability,
    ProgressEvidenceRenderingError,
    ProgressEvidenceRenderingPlanning,
    ProgressEvidenceRenderingProposal,
    ProgressEvidenceRenderingProposalExample,
    ProgressWhyRendering,
    RenderedProgressEvidenceExample,
    UnsupportedProgressEvidenceRenderingVersion,
    prepare_progress_evidence_rendering,
    resolve_deterministic_progress_evidence_rendering,
    validate_progress_evidence_rendering_proposal,
)
from core.progress_evidence_selection import (
    MAX_SELECTED_EVIDENCE,
    PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
    PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
    SelectedProgressEvidence,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_progress_evidence_selection import (
    NON_AVAILABLE,
    UUID_PATTERN,
    absent_card,
    card,
    cand,
)

MODULE_PATH = REPO_ROOT / "core" / "progress_evidence_rendering.py"

BFR = "L'utilisateur relie la hausse du BFR à une consommation de trésorerie."


# --------------------------------------------------------------------------
# Fabriques
# --------------------------------------------------------------------------

def selection(candidates=None, status="available", origin="direct", source="application", code="C7",
              schema=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION, policy=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION):
    if status != "available":
        candidates, origin, source = (), None, None
    return SelectedProgressEvidence(
        schema_version=schema,
        policy_version=policy,
        competency_code=code,
        evidence_status=status,
        basis_origin=origin,
        source_claim_stage=source,
        selected_candidates=(cand(2), cand(5)) if candidates is None else candidates,
    )


def picks(*indices, **kwargs):
    return tuple(cand(i, **kwargs) for i in indices)


def example(token, text=None):
    return ProgressEvidenceRenderingProposalExample(
        evidence_token=token, text=text if text is not None else f"Tu as relié l'idée {token}.")


def proposal(*examples, schema=PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
             policy=PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION):
    return ProgressEvidenceRenderingProposal(schema_version=schema, policy_version=policy, examples=tuple(examples))


def tokens_proposal(*tokens):
    return proposal(*(example(t) for t in tokens))


def plan(c=None, s=None):
    return prepare_progress_evidence_rendering(card=c if c is not None else card(),
                                               selection=s if s is not None else selection())


def validate(planning, prop):
    return validate_progress_evidence_rendering_proposal(planning=planning, proposal=prop)


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_frozen():
    assert PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION == "progress-evidence-rendering-v1"
    assert PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION == "progress-evidence-rendering-policy-1"
    assert ren.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION == "current-progress-projection-v1"
    assert ren.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION == "current-progress-policy-1"
    assert ren.SUPPORTED_PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION == "progress-evidence-selection-v1"
    assert ren.SUPPORTED_PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION == "progress-evidence-selection-policy-1"


def test_supported_upstream_versions_are_exactly_the_current_6_4a_and_6_4b2_ones():
    """Garde de version : si 6-4A ou 6-4B2 change de version, ce test (et le
    préflight) échouent jusqu'à une décision explicite pour 6-4B3."""
    assert (pp.CURRENT_PROGRESS_SCHEMA_VERSION, pp.CURRENT_PROGRESS_POLICY_VERSION) == (
        ren.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, ren.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION)
    assert (sel.PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION, sel.PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION) == (
        ren.SUPPORTED_PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
        ren.SUPPORTED_PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION)


def test_vocabularies_and_display_bound_are_read_from_their_owners_never_copied():
    assert ren.MAX_SELECTED_EVIDENCE is sel.MAX_SELECTED_EVIDENCE
    assert ren.CLAIM_STAGE_ORDER is pp.CLAIM_STAGE_ORDER and ren.SCOPE_MODES is pe.SCOPE_MODES
    assert ren.BASIS_ORIGINS is pe.BASIS_ORIGINS and ren.EVIDENCE_STATUSES is pe.EVIDENCE_STATUSES
    # support_level / elicitation_mode : vocabulaires T3 tels que 6-4B1 les
    # applique (jamais recopiés ici).
    assert ren.SUPPORT_LEVELS is pe.SUPPORT_LEVELS is t3.SUPPORT_LEVELS
    assert ren.ELICITATION_MODES is pe.ELICITATION_MODES is t3.ELICITATION_MODES
    assert ren.SUPPORT_LEVELS == frozenset({"none", "hinted", "guided", "answer_given"})
    assert ren.ELICITATION_MODES == frozenset({"prompted", "spontaneous"})


def test_structures_are_frozen_keyword_only_dataclasses_with_exactly_these_fields():
    expected = {
        RenderedProgressEvidenceExample: ["evidence_token", "text"],
        ProgressWhyRendering: ["schema_version", "policy_version", "competency_code", "evidence_status", "examples"],
        ProgressEvidenceRenderingProposalExample: ["evidence_token", "text"],
        ProgressEvidenceRenderingProposal: ["schema_version", "policy_version", "examples"],
        ProgressEvidenceRenderingCapability: ["capability_token", "label"],
        ProgressEvidenceRenderingPlanning: ["schema_version", "policy_version", "card", "selection",
                                            "represented_capabilities", "model_rendering_required"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names, cls
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls)), cls
    assert typing.get_type_hints(ProgressWhyRendering)["examples"] == tuple[RenderedProgressEvidenceExample, ...]
    assert typing.get_type_hints(ProgressEvidenceRenderingProposal)["examples"] == tuple[
        ProgressEvidenceRenderingProposalExample, ...]
    assert typing.get_type_hints(RenderedProgressEvidenceExample) == {"evidence_token": str, "text": str}
    result = validate(plan(), tokens_proposal("evidence_2", "evidence_5"))
    assert type(result.examples) is tuple
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.examples = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.examples[0].text = "autre"
    with pytest.raises(TypeError):
        RenderedProgressEvidenceExample("evidence_1", "texte")  # noqa
    with pytest.raises(TypeError):
        ProgressEvidenceRenderingProposal("s", "p", ())  # noqa


def test_visible_contract_carries_no_stage_no_metadata_no_identifier_no_score():
    """Le stade reste la propriété de 6-4A ; les gardes de fidélité ne sont
    jamais du contenu visible."""
    fields = {f.name for cls in (RenderedProgressEvidenceExample, ProgressWhyRendering,
                                 ProgressEvidenceRenderingProposalExample, ProgressEvidenceRenderingProposal)
              for f in dataclasses.fields(cls)}
    for forbidden in ("support_level", "elicitation_mode", "basis_origin", "source_claim_stage", "stage_code",
                      "stage_label", "observation_text", "scope_mode", "capability_tokens", "evidence_strength",
                      "score", "weight", "rank", "rating", "confidence", "percent", "summary", "intro",
                      "conclusion", "overall_explanation", "why_summary", "headline", "less_observed",
                      "weak_capabilities", "tension", "revision", "timeline", "milestone", "history",
                      "recommendation", "next_step", "user_id", "uuid", "id", "observation_id", "created_at"):
        assert forbidden not in fields, forbidden
    parts = {part for name in fields for part in name.split("_")}
    assert not {"score", "weight", "rank", "rating", "confidence", "percent", "strength", "summary", "intro",
                "conclusion", "headline", "stage", "support", "elicitation", "basis", "less", "tension",
                "revision", "timeline", "milestone", "history", "id", "at"} & parts


def test_rendered_output_never_contains_a_uuid():
    result = validate(plan(), tokens_proposal("evidence_2", "evidence_5"))
    assert not UUID_PATTERN.search(repr(result))
    assert not any(isinstance(getattr(result, f.name), __import__("uuid").UUID)
                   for f in dataclasses.fields(ProgressWhyRendering))


def test_evidence_token_is_documented_as_internal_provenance_never_displayed():
    doc = " ".join(RenderedProgressEvidenceExample.__doc__.split())
    assert "provenance INTERNE" in doc and "JAMAIS affichée" in doc
    assert "SEUL contenu destiné à l'affichage" in doc


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidProgressEvidenceRenderingInput, UnsupportedProgressEvidenceRenderingVersion,
                InvalidProgressEvidenceRenderingProposal):
        assert issubclass(cls, ProgressEvidenceRenderingError)
    assert not issubclass(ProgressEvidenceRenderingError, (sel.ProgressEvidenceSelectionError,
                                                           pe.ProgressEvidenceError, pp.ProgressProjectionError))
    assert not issubclass(InvalidProgressEvidenceRenderingProposal, InvalidProgressEvidenceRenderingInput)


def test_public_api_is_keyword_only():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"prepare_progress_evidence_rendering", "resolve_deterministic_progress_evidence_rendering",
                      "validate_progress_evidence_rendering_proposal"}
    expected = {"prepare_progress_evidence_rendering": ["card", "selection"],
                "resolve_deterministic_progress_evidence_rendering": ["planning"],
                "validate_progress_evidence_rendering_proposal": ["planning", "proposal"]}
    for name, params in expected.items():
        signature = inspect.signature(getattr(ren, name)).parameters.values()
        assert [p.name for p in signature] == params
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
                   for p in signature)


# --------------------------------------------------------------------------
# 2. Versions amont
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("schema", "progress-evidence-selection-v2"),
    ("policy", "progress-evidence-selection-policy-2"),
    ("schema", None),
    ("policy", ""),
])
def test_unknown_received_6_4b2_versions_are_refused(field, value):
    with pytest.raises(UnsupportedProgressEvidenceRenderingVersion):
        plan(s=selection(**{field: value}))
    with pytest.raises(UnsupportedProgressEvidenceRenderingVersion):
        plan(absent_card(), selection(status="no_state", **{field: value}))


@pytest.mark.parametrize("name, value", [
    ("CURRENT_PROGRESS_SCHEMA_VERSION", "current-progress-projection-v2"),
    ("CURRENT_PROGRESS_POLICY_VERSION", "current-progress-policy-2"),
    ("PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION", "progress-evidence-selection-v2"),
    ("PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION", "progress-evidence-selection-policy-2"),
])
def test_a_future_upstream_module_policy_is_never_accepted_silently(monkeypatch, name, value):
    monkeypatch.setattr(ren, name, value)
    for c, s in ((card(), selection()), (absent_card(), selection(status="no_state"))):
        with pytest.raises(UnsupportedProgressEvidenceRenderingVersion):
            plan(c, s)


# --------------------------------------------------------------------------
# 3. Statuts non available
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status, make_card", NON_AVAILABLE)
def test_non_available_renders_no_example(status, make_card):
    planning = plan(make_card(), selection(status=status))
    assert planning.model_rendering_required is False
    result = resolve_deterministic_progress_evidence_rendering(planning=planning)
    assert result == ProgressWhyRendering(
        schema_version=PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
        competency_code="C7",
        evidence_status=status,
        examples=(),
    )
    assert validate(planning, proposal()) == result
    with pytest.raises(InvalidProgressEvidenceRenderingProposal):
        validate(planning, tokens_proposal("evidence_1"))


@pytest.mark.parametrize("status, make_card", NON_AVAILABLE)
@pytest.mark.parametrize("changes", [
    {"basis_origin": "direct"},
    {"source_claim_stage": "application"},
    {"selected_candidates": (cand(1),)},
    {"selected_candidates": []},
])
def test_non_available_with_provenance_or_examples_is_refused(status, make_card, changes):
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(make_card(), dataclasses.replace(selection(status=status), **changes))


# --------------------------------------------------------------------------
# 4. available
# --------------------------------------------------------------------------

@pytest.mark.parametrize("n", range(1, MAX_SELECTED_EVIDENCE + 1))
def test_available_with_one_to_max_examples_requires_a_rendering(n):
    planning = plan(s=selection(picks(*range(1, n + 1))))
    assert planning.model_rendering_required is True
    with pytest.raises(InvalidProgressEvidenceRenderingInput, match="aucun repli"):
        resolve_deterministic_progress_evidence_rendering(planning=planning)


@pytest.mark.parametrize("candidates", [(), picks(1, 2, 3, 4), picks(1, 2, 3, 4, 5, 6)])
def test_available_outside_one_to_max_examples_is_refused(candidates):
    with pytest.raises(InvalidProgressEvidenceRenderingInput, match="1 à 3"):
        plan(s=selection(candidates))


@pytest.mark.parametrize("changes", [
    {"basis_origin": None},
    {"basis_origin": "implied_by_higher_claim"},
    {"source_claim_stage": None},
    {"source_claim_stage": "non_etabli"},
    {"selected_candidates": [cand(1)]},
    {"evidence_status": "unknown"},
    {"evidence_status": None},
])
def test_available_contract_is_enforced(changes):
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(s=dataclasses.replace(selection(), **changes))


@pytest.mark.parametrize("stage, origin, source, ok", [
    ("application", "direct", "application", True),
    ("application", "direct", "mastery", False),
    ("application", "inherited_from_higher_claim", "mastery", True),
    ("application", "inherited_from_higher_claim", "application", False),
    ("application", "inherited_from_higher_claim", "comprehension", False),
    ("comprehension", "inherited_from_higher_claim", "application", True),
    ("mastery", "inherited_from_higher_claim", "mastery", False),
])
def test_basis_origin_is_coherent_with_the_displayed_stage(stage, origin, source, ok):
    s = selection(origin=origin, source=source)
    if ok:
        assert plan(card(stage=stage), s).selection is s
    else:
        with pytest.raises(InvalidProgressEvidenceRenderingInput):
            plan(card(stage=stage), s)


@pytest.mark.parametrize("tokens", [
    ("evidence_5", "evidence_2"), ("evidence_2", "evidence_2"), ("evidence_0",), ("evidence_02",),
    ("evidence_x",), ("Evidence_1",), ("evidence_",), ("evidence_1 ",), ("evidence_²",), ("",),
])
def test_selected_tokens_must_be_distinct_canonical_and_in_6_4b1_order(tokens):
    candidates = tuple(dataclasses.replace(cand(1), evidence_token=t) for t in tokens)
    with pytest.raises(InvalidProgressEvidenceRenderingInput, match="evidence_token"):
        plan(s=selection(candidates))


@pytest.mark.parametrize("bad", [
    {"evidence_token": 2},
    {"observation_text": ""},
    {"observation_text": "  \n\t"},
    {"observation_text": None},
    {"observation_text": "a\x00b"},
    {"observation_text": "\udcff"},
    {"scope_mode": "partial"},
    {"scope_mode": None},
    {"capability_tokens": ["C7_A@r1"]},
    {"capability_tokens": (1,)},
    {"capability_tokens": ()},
    {"capability_tokens": ("C7_A@r1", "C7_A@r1")},
    {"capability_tokens": ("C7_Z@r1",)},
    {"capability_tokens": ("C8_A@r1",)},
    {"support_level": "unknown"},
    {"support_level": None},
    {"support_level": "NONE"},
    {"elicitation_mode": "forced"},
    {"elicitation_mode": None},
])
def test_each_selected_candidate_is_structurally_exploitable(bad):
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(s=selection((dataclasses.replace(cand(1), **bad),)))


def test_selected_candidate_must_be_exactly_a_6_4b1_candidate():
    class Sub(ProgressEvidenceCandidate):
        pass

    fake = Sub(**dataclasses.asdict(cand(1)))
    for bad in (fake, dataclasses.asdict(cand(1)), "evidence_1", None):
        with pytest.raises(InvalidProgressEvidenceRenderingInput):
            plan(s=selection((bad,)))


def test_competency_only_examples_need_a_competency_only_or_mixed_card():
    s = selection((cand(1, scope="competency_only"),))
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(card(coverage="localized"), s)
    with_tokens = dataclasses.replace(cand(1, scope="competency_only"), capability_tokens=("C7_A@r1",))
    with pytest.raises(InvalidProgressEvidenceRenderingInput, match="competency_only avec des capacités"):
        plan(card(coverage="mixed"), selection((with_tokens,)))
    assert plan(card(coverage="mixed"), s).model_rendering_required
    assert plan(card(coverage="competency_only", caps=()), s).model_rendering_required


@pytest.mark.parametrize("support", sorted(t3.SUPPORT_LEVELS))
@pytest.mark.parametrize("elicitation", sorted(t3.ELICITATION_MODES))
def test_every_t3_support_and_elicitation_value_is_accepted_unchanged(support, elicitation):
    s = selection((cand(1, support=support, elicitation=elicitation),))
    planning = plan(s=s)
    assert planning.selection.selected_candidates[0] is s.selected_candidates[0]
    assert validate(planning, tokens_proposal("evidence_1")).examples[0].evidence_token == "evidence_1"


def test_observation_text_is_never_bounded_nor_altered_by_the_preflight():
    long_text = "Le cash suit le BFR, pas le résultat — « citation »\n" * 5000
    s = selection((cand(1, text=long_text),))
    assert plan(s=s).selection.selected_candidates[0].observation_text == long_text


# --------------------------------------------------------------------------
# 5. Carte 6-4A
# --------------------------------------------------------------------------

def test_card_and_selection_must_be_exact_types():
    class SubCard(pp.CompetencyCurrentProgress):
        pass

    class SubSelection(SelectedProgressEvidence):
        pass

    for c, s in ((None, selection()), (card(), None), (dataclasses.asdict(card()), selection()),
                 (SubCard(**{f.name: getattr(card(), f.name) for f in dataclasses.fields(card())}), selection()),
                 (card(), SubSelection(**{f.name: getattr(selection(), f.name)
                                          for f in dataclasses.fields(selection())})),
                 (card(), pe.ProgressEvidenceSet(
                     schema_version="progress-evidence-set-v1", policy_version="progress-evidence-policy-1",
                     competency_code="C7", evidence_status="available", basis_origin="direct",
                     source_claim_stage="application", candidates=(cand(1),)))):
        with pytest.raises(InvalidProgressEvidenceRenderingInput):
            prepare_progress_evidence_rendering(card=c, selection=s)


def test_card_and_selection_must_describe_the_same_competency():
    with pytest.raises(InvalidProgressEvidenceRenderingInput, match="C8"):
        plan(card(), selection(code="C8"))
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(absent_card("C8"), selection(status="no_state"))


@pytest.mark.parametrize("changes", [
    {"competency_code": "C13"},
    {"competency_label": " "},
    {"state_present": 1},
    {"stage_code": "expert"},
    {"stage_label": "Avancé"},
    {"coverage_mode": "partial"},
    {"coverage_mode": "none"},
    {"represented_capabilities": [pp.VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1,
                                                                 label="x")]},
    {"represented_capabilities": ()},
    {"represented_capabilities": (pp.VisibleCapabilityProjection(capability_code="C8_A", semantic_revision=1,
                                                                 label="x"),)},
    {"represented_capabilities": (pp.VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=0,
                                                                 label="x"),)},
    {"represented_capabilities": (pp.VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1,
                                                                 label=""),)},
    {"represented_capabilities": (pp.VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1,
                                                                 label="x"),) * 2},
])
def test_available_card_is_well_formed(changes):
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(dataclasses.replace(card(), **changes), selection((cand(1),)))


@pytest.mark.parametrize("c, status", [
    (card(), "no_state"),
    (card(), "no_positive_basis"),
    (card(), "current_stage_not_established"),
    (absent_card(), "no_positive_basis"),
    (absent_card(), "current_stage_not_established"),
    (card(stage="non_etabli", coverage="none", caps=()), "no_state"),
    (card(stage="non_etabli", coverage="none", caps=()), "current_stage_not_established"),
    (card(stage="application", coverage="none", caps=()), "no_positive_basis"),
    (card(stage="application", coverage="none", caps=()), "no_state"),
])
def test_evidence_status_must_match_the_card_never_repaired(c, status):
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(c, selection(status=status))


@pytest.mark.parametrize("status, make_card", NON_AVAILABLE)
def test_available_is_refused_on_cards_without_positive_coverage(status, make_card):
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        plan(make_card(), selection((cand(1),)))


def test_planning_exposes_only_tokens_and_labels_of_the_card_capabilities():
    planning = plan()
    assert planning.represented_capabilities == (
        ProgressEvidenceRenderingCapability(capability_token="C7_A@r1", label="Résultat comptable et trésorerie"),
        ProgressEvidenceRenderingCapability(capability_token="C7_B@r1", label="Besoin en fonds de roulement"),
        ProgressEvidenceRenderingCapability(capability_token="C7_C@r2", label="Qualité du free cash flow"),
    )


# --------------------------------------------------------------------------
# 7. Validateur : mapping 1:1, ordre exact, texte mono-ligne
# --------------------------------------------------------------------------

def test_one_example_renders_exactly_one_text():
    s = selection((cand(4, text=BFR),))
    result = validate(plan(s=s), proposal(example(
        "evidence_4", "Tu as relié une hausse du BFR à une consommation de trésorerie.")))
    assert result == ProgressWhyRendering(
        schema_version=PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
        competency_code="C7",
        evidence_status="available",
        examples=(RenderedProgressEvidenceExample(
            evidence_token="evidence_4", text="Tu as relié une hausse du BFR à une consommation de trésorerie."),),
    )


def test_three_examples_render_three_texts_in_selection_order():
    s = selection(picks(2, 5, 8))
    result = validate(plan(s=s), tokens_proposal("evidence_2", "evidence_5", "evidence_8"))
    assert [e.evidence_token for e in result.examples] == ["evidence_2", "evidence_5", "evidence_8"]
    assert [e.text for e in result.examples] == [f"Tu as relié l'idée evidence_{i}." for i in (2, 5, 8)]


def test_evidence_token_is_preserved_exactly_from_the_selected_candidate():
    s = selection(picks(2, 5))
    result = validate(plan(s=s), tokens_proposal("evidence_2", "evidence_5"))
    assert [e.evidence_token for e in result.examples] == [c.evidence_token for c in s.selected_candidates]


def test_exact_token_order_two_five_is_valid_five_two_is_not():
    planning = plan(s=selection(picks(2, 5)))
    assert len(validate(planning, tokens_proposal("evidence_2", "evidence_5")).examples) == 2
    with pytest.raises(InvalidProgressEvidenceRenderingProposal, match="ordre"):
        validate(planning, tokens_proposal("evidence_5", "evidence_2"))


@pytest.mark.parametrize("tokens, reason", [
    (("evidence_2",), "manquants \\['evidence_5'\\]"),
    (("evidence_5",), "manquants \\['evidence_2'\\]"),
    ((), "manquants"),
    (("evidence_2", "evidence_5", "evidence_7"), "en trop \\['evidence_7'\\]"),
    (("evidence_2", "evidence_7"), "en trop"),
    (("evidence_2", "evidence_2"), "double"),
    (("evidence_2", "evidence_5", "evidence_5"), "double"),
    (("evidence_2", "evidence_2", "evidence_5"), "double"),
])
def test_missing_extra_or_duplicate_tokens_are_refused_never_corrected(tokens, reason):
    with pytest.raises(InvalidProgressEvidenceRenderingProposal, match=reason):
        validate(plan(s=selection(picks(2, 5))), tokens_proposal(*tokens))


def test_two_observations_merged_into_one_text_are_refused():
    merged = proposal(example("evidence_2", "Tu as relié le BFR à la trésorerie et distingué résultat et cash."))
    with pytest.raises(InvalidProgressEvidenceRenderingProposal):
        validate(plan(s=selection(picks(2, 5))), merged)


@pytest.mark.parametrize("token", [2, None, ["evidence_2"], b"evidence_2"])
def test_non_string_tokens_are_refused(token):
    with pytest.raises(InvalidProgressEvidenceRenderingProposal, match="evidence_token"):
        validate(plan(s=selection(picks(2))), proposal(example(token)))


@pytest.mark.parametrize("text, reason", [
    ("", "vide"),
    ("   ", "vide"),
    ("\t", "vide"),
    ("Tu as\x00 relié.", "NUL"),
    ("Phrase 1\nPhrase 2", "ligne"),
    ("Phrase 1\rPhrase 2", "ligne"),
    ("Phrase 1\r\nPhrase 2", "ligne"),
    ("Tu as relié le BFR.\n", "ligne"),
    ("\nTu as relié le BFR.", "ligne"),
    ("Phrase 1 Phrase 2", "ligne"),
    ("Phrase 1 Phrase 2", "ligne"),
    ("Phrase 1\x85Phrase 2", "ligne"),
    ("Phrase 1\x0bPhrase 2", "ligne"),
    ("Tu as relié \udcff.", "UTF-8"),
    (None, "str"),
    (42, "str"),
    (["Tu as relié."], "str"),
])
def test_rendered_text_must_be_one_non_empty_utf8_line(text, reason):
    with pytest.raises(InvalidProgressEvidenceRenderingProposal, match=reason):
        validate(plan(s=selection(picks(2))), proposal(
            ProgressEvidenceRenderingProposalExample(evidence_token="evidence_2", text=text)))


def test_one_bad_text_among_several_rejects_the_whole_rendering():
    with pytest.raises(InvalidProgressEvidenceRenderingProposal, match=r"examples\[1\]"):
        validate(plan(s=selection(picks(2, 5, 8))), proposal(
            example("evidence_2"), example("evidence_5", "Ligne 1\nLigne 2"), example("evidence_8")))


def test_no_individual_length_limit_on_rendered_text():
    long_text = "Tu as relié une hausse du BFR à une consommation de trésorerie, " * 400
    assert len(long_text) > 20_000
    result = validate(plan(s=selection(picks(2))), proposal(example("evidence_2", long_text)))
    assert result.examples[0].text == long_text


def test_rendered_text_is_kept_as_is_never_cleaned():
    text = "  Tu as relié une hausse du BFR à une consommation de trésorerie.\t"
    assert validate(plan(s=selection(picks(2))), proposal(example("evidence_2", text))).examples[0].text == text


@pytest.mark.parametrize("bad", [
    {"schema_version": "progress-evidence-rendering-v2"},
    {"policy_version": "progress-evidence-rendering-policy-2"},
    {"schema_version": None},
    {"policy_version": PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION},
    {"examples": [example("evidence_2"), example("evidence_5")]},
    {"examples": None},
    {"examples": ({"evidence_token": "evidence_2", "text": "x"}, example("evidence_5"))},
    {"examples": (RenderedProgressEvidenceExample(evidence_token="evidence_2", text="x"), example("evidence_5"))},
])
def test_proposal_shape_and_versions_are_exact(bad):
    with pytest.raises(InvalidProgressEvidenceRenderingProposal):
        validate(plan(), dataclasses.replace(tokens_proposal("evidence_2", "evidence_5"), **bad))


def test_proposal_must_be_exactly_a_rendering_proposal():
    class Sub(ProgressEvidenceRenderingProposal):
        pass

    good = tokens_proposal("evidence_2", "evidence_5")
    for bad in (None, dataclasses.asdict(good), Sub(**{f.name: getattr(good, f.name)
                                                       for f in dataclasses.fields(good)})):
        with pytest.raises(InvalidProgressEvidenceRenderingProposal):
            validate(plan(), bad)


def test_text_is_never_compared_to_the_source_nor_judged_in_python():
    """Aucune sémantique en Python : la fidélité appartient au prompt ; le
    validateur ne contrôle que la structure (le renderer reste fail-closed
    sur tout le reste)."""
    planning = plan(s=selection((cand(2, text=BFR),)))
    for text in ("Tu es expert.", BFR, "Continue à travailler le BFR."):
        assert validate(planning, proposal(example("evidence_2", text))).examples[0].text == text


# --------------------------------------------------------------------------
# 8. Planning forgé, déterminisme, entrées non modifiées
# --------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"model_rendering_required": False},
    {"represented_capabilities": ()},
    {"schema_version": "progress-evidence-rendering-v2"},
    {"selection": selection(picks(2, 5, 7, 9))},
    {"selection": selection(picks(5, 2))},
    {"card": card(stage="mastery", coverage="mixed")},
])
def test_forged_planning_is_never_trusted(changes):
    forged = dataclasses.replace(plan(), **changes)
    with pytest.raises((InvalidProgressEvidenceRenderingInput, UnsupportedProgressEvidenceRenderingVersion)):
        validate(forged, tokens_proposal("evidence_2", "evidence_5"))


def test_forged_available_planning_cannot_reach_the_deterministic_path():
    forged = dataclasses.replace(plan(s=selection(picks(1))), model_rendering_required=False)
    with pytest.raises(InvalidProgressEvidenceRenderingInput):
        resolve_deterministic_progress_evidence_rendering(planning=forged)
    for bad in (None, card(), selection()):
        with pytest.raises(InvalidProgressEvidenceRenderingInput):
            resolve_deterministic_progress_evidence_rendering(planning=bad)


def test_pure_and_deterministic_inputs_untouched():
    c = card(coverage="mixed")
    s = selection((cand(1, support="answer_given"), cand(2, scope="competency_only", elicitation="spontaneous"),
                   cand(3, support="guided")), origin="inherited_from_higher_claim", source="mastery")
    before = (dataclasses.asdict(c), dataclasses.asdict(s))
    prop = tokens_proposal("evidence_1", "evidence_2", "evidence_3")
    first, second = validate(plan(c, s), prop), validate(plan(c, s), prop)
    assert first == second
    assert (dataclasses.asdict(c), dataclasses.asdict(s)) == before
    assert plan(c, s) == plan(c, s)
    assert plan(c, s).selection is s and plan(c, s).card is c


def test_support_elicitation_and_provenance_never_change_the_validation():
    results = set()
    for support in sorted(t3.SUPPORT_LEVELS):
        for elicitation in sorted(t3.ELICITATION_MODES):
            for origin, source in (("direct", "application"), ("inherited_from_higher_claim", "mastery")):
                s = selection(picks(2, 5, support=support, elicitation=elicitation), origin=origin, source=source)
                results.add(validate(plan(s=s), tokens_proposal("evidence_2", "evidence_5")))
    assert len(results) == 1


# --------------------------------------------------------------------------
# 9. Contrat statique
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


def test_imports_are_exactly_stdlib_and_the_6_4a_6_4b1_6_4b2_public_contracts():
    assert _imports() == {
        "dataclasses": {"dataclass"},
        "core.adaptation_state": {"COMPETENCY_ORDER"},
        "core.progress_evidence": {
            "BASIS_DIRECT", "BASIS_INHERITED_FROM_HIGHER_CLAIM", "BASIS_ORIGINS", "ELICITATION_MODES",
            "EVIDENCE_AVAILABLE", "EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED", "EVIDENCE_NO_POSITIVE_BASIS",
            "EVIDENCE_NO_STATE", "EVIDENCE_STATUSES", "SCOPE_COMPETENCY_ONLY", "SCOPE_LOCALIZED", "SCOPE_MODES",
            "SUPPORT_LEVELS", "ProgressEvidenceCandidate"},
        "core.progress_evidence_selection": {
            "MAX_SELECTED_EVIDENCE", "PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION",
            "PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION", "SelectedProgressEvidence"},
        "core.progress_projection": {
            "CLAIM_STAGE_ORDER", "COVERAGE_COMPETENCY_ONLY", "COVERAGE_LOCALIZED", "COVERAGE_MIXED",
            "COVERAGE_MODES", "COVERAGE_NONE", "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
            "NO_STATE_LABEL", "NON_ETABLI", "VISIBLE_STAGE_LABELS", "CompetencyCurrentProgress",
            "VisibleCapabilityProjection"},
    }
    imported = set().union(*_imports().values())
    assert not any(name.startswith("_") for name in imported)
    # Aucun snapshot Step 5, aucun ensemble 6-4B1 complet, aucune sélection
    # rejouée, aucun renderer.
    for name in ("AdaptationStateSnapshot", "load_adaptation_state", "acquire_progress_evidence",
                 "project_current_progress", "CurrentProgressProjection", "ProgressEvidenceSet",
                 "prepare_progress_evidence_selection", "validate_progress_evidence_selection_proposal",
                 "select_representative_progress_evidence", "ProgressEvidenceSelectionProposal"):
        assert name not in imported and name not in _code_tokens(_source()).split(), name
    assert "core.progress_evidence_renderer" not in _imports()
    assert "progress_evidence_renderer" not in _code_tokens(_source())


def test_no_db_session_service_nor_persistence():
    imported = set(_imports())
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "core.inference_service",
                "core.observation_service", "core.taxonomy_service", "core.longitudinal_service",
                "core.cognitive_capture", "alembic", "psycopg2"} & imported
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    for forbidden in ("Session", "session", "db", "add", "add_all", "flush", "commit", "rollback", "execute",
                      "select", "insert", "update", "delete", "merge", "query", "no_autoflush", "refresh",
                      "add_support_trace", "SupportTrace", "support_trace", "save", "persist", "cache", "history"):
        assert forbidden not in names, forbidden


def test_no_llm_network_clock_randomness_nor_environment():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "random", "secrets", "datetime",
                "time", "os", "json", "logging", "uuid", "functools"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "backend", "complete(", "environ", "getenv",
                 "uuid4", "retry"):
        assert word not in tokens, word
    assert not {"llm", "now", "today", "global"} & set(tokens.split())


def test_no_text_bound_and_no_truncation():
    source = _source()
    for name in ("MAX_RENDERED_EXAMPLE_CHARS", "MAX_OBSERVATION_TEXT_CHARS", "MAX_SOURCE_TEXT_CHARS",
                 "MAX_OBSERVATION_TEXT", "MAX_TEXT"):
        assert name not in source, name
    assert [n.targets[0].id for n in _tree().body if isinstance(n, ast.Assign)
            and n.targets[0].id.startswith("MAX_")] == []
    assert [n for n in ast.walk(_tree()) if isinstance(n, ast.Slice)] == []
    calls = {n.func.id for n in ast.walk(_tree()) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {"sorted", "reversed", "min", "max", "sum", "filter"} & calls
    attributes = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    # strip ne sert qu'à détecter un texte vide ; jamais pour nettoyer.
    assert not {"sort", "replace", "rstrip", "lstrip", "split", "join", "truncate", "shorten"} & attributes
    integers = {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is int}
    assert integers <= {0, 1}, integers  # aucun « 3 » recopié, aucune longueur


def test_no_semantic_rule_score_nor_user_facing_text_in_the_code():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "confidence", "rank", "ranking", "quality", "priority", "percent", "rating",
                 "strength", "best", "summary", "intro", "headline", "keyword", "forbidden_words", "expert",
                 "autonomous", "spontaneously", "recommend", "next", "weak", "less", "tension", "revisions",
                 "timeline", "milestone", "similarity", "sentence", "sentences"):
        assert word not in parts, word
    assert "re" not in _imports()


def test_not_wired_to_api_runtime_or_any_module_but_its_renderer():
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_evidence_rendering.py" and "progress_evidence_rendering" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    # Étape 6.4D : le détail juxtapose le seul contrat de sortie
    # (ProgressWhyRendering et ses versions), jamais le préflight ni le
    # validateur ; non branché (tests/test_competency_progress_detail.py).
    assert sorted(users) == ["core/competency_progress_detail.py", "core/progress_evidence_renderer.py"]
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("ProgressWhyRendering", "RenderedProgressEvidenceExample", "prepare_progress_evidence_rendering",
                 "validate_progress_evidence_rendering_proposal", "render_progress_evidence"):
        assert name not in api, name
    tokens = _code_tokens(_source()).lower()
    for word in ("web_chat", "fastapi", "route", "endpoint", "coach", "education", "decrypt", "portfolio",
                 "rallye", "academy", "__tablename__", "column"):
        assert word not in tokens, word


def test_no_migration_nor_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0010_r1b_event_idempotence.py" and len(versions) == 10
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    for word in ("Rendering", "rendered", "progress_evidence", "ProgressWhy"):
        assert word not in models, word


def test_no_module_level_mutable_state():
    for name, value in vars(ren).items():
        if not name.startswith("__"):
            assert not isinstance(value, (list, dict, set)), name
