"""Tests de l'Étape 6.4B2 (frontière déterministe) : contrats, préflight,
chemins déterministes et validateur de la sélection d'exemples
représentatifs (core/progress_evidence_selection.py).

1. Contrats : versions, borne d'affichage, dataclasses immuables, API
   publique, hiérarchie d'erreurs, exclusions publiques.
2. Versions amont : 6-4A (module) et 6-4B1 (module et objet reçu).
3. Statuts non available : aucune sélection, provenance vide exigée.
4. available : provenance, jetons séquentiels, candidates.
5. Carte 6-4A : structure et cohérence avec le statut 6-4B1.
6. Chemins déterministes : non available, une candidate, refus au-delà.
7. Validateur : 1 à 3 jetons, connus, distincts, ordre canonique ; aucune
   couverture ni quota ; objets 6-4B1 originaux ; provenance propagée.
8. Planning forgé, déterminisme, entrées non modifiées.
9. Contrat statique : imports, aucune base, aucune persistance, aucune
   SupportTrace, aucune borne de candidates, aucun score, aucun texte,
   aucun branchement runtime.
"""
import ast
import dataclasses
import inspect
import re
import typing

import pytest

from core import progress_evidence as pe
from core import progress_evidence_selection as sel
from core import progress_projection as pp
from core.progress_evidence import ProgressEvidenceCandidate, ProgressEvidenceSet
from core.progress_evidence_selection import (
    MAX_SELECTED_EVIDENCE,
    PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
    PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
    InvalidProgressEvidenceSelectionInput,
    InvalidProgressEvidenceSelectionProposal,
    ProgressEvidenceSelectionCapability,
    ProgressEvidenceSelectionError,
    ProgressEvidenceSelectionPlanning,
    ProgressEvidenceSelectionProposal,
    SelectedProgressEvidence,
    UnsupportedProgressEvidenceSelectionVersion,
    prepare_progress_evidence_selection,
    resolve_deterministic_progress_evidence_selection,
    validate_progress_evidence_selection_proposal,
)
from core.progress_projection import (
    NO_STATE_LABEL,
    VISIBLE_STAGE_LABELS,
    CompetencyCurrentProgress,
    VisibleCapabilityProjection,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "progress_evidence_selection.py"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

CAPS = (("C7_A", 1, "Résultat comptable et trésorerie"), ("C7_B", 1, "Besoin en fonds de roulement"),
        ("C7_C", 2, "Qualité du free cash flow"))


# --------------------------------------------------------------------------
# Fabriques (objets publics 6-4A / 6-4B1 construits à la main)
# --------------------------------------------------------------------------

def card(stage="application", coverage="localized", caps=CAPS, code="C7", present=True, label="Flux et cash"):
    return CompetencyCurrentProgress(
        competency_code=code,
        competency_label=label,
        state_present=present,
        stage_code=stage if present else None,
        stage_label=VISIBLE_STAGE_LABELS[stage] if present else NO_STATE_LABEL,
        coverage_mode=coverage,
        represented_capabilities=tuple(VisibleCapabilityProjection(capability_code=c, semantic_revision=r, label=lab)
                                       for c, r, lab in caps),
    )


def absent_card(code="C7"):
    return card(present=False, coverage="none", caps=(), code=code)


def non_etabli_card():
    return card(stage="non_etabli", coverage="none", caps=())


def not_established_card():
    return card(stage="application", coverage="none", caps=())


def cand(i, text=None, scope="localized", tokens=("C7_A@r1",), elicitation="prompted", support="none"):
    return ProgressEvidenceCandidate(
        evidence_token=f"evidence_{i}",
        observation_text=text if text is not None else f"Observation interne {i}",
        scope_mode=scope,
        capability_tokens=tokens if scope == "localized" else (),
        elicitation_mode=elicitation,
        support_level=support,
    )


def cands(n, **kwargs):
    return tuple(cand(i, **kwargs) for i in range(1, n + 1))


def evidence(candidates=None, status="available", origin="direct", source="application", code="C7"):
    if status != "available":
        candidates, origin, source = (), None, None
    return ProgressEvidenceSet(
        schema_version="progress-evidence-set-v1",
        policy_version="progress-evidence-policy-1",
        competency_code=code,
        evidence_status=status,
        basis_origin=origin,
        source_claim_stage=source,
        candidates=cands(2) if candidates is None else candidates,
    )


NON_AVAILABLE = (
    ("no_state", absent_card),
    ("no_positive_basis", non_etabli_card),
    ("current_stage_not_established", not_established_card),
)


def proposal(*tokens, schema=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
             policy=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION):
    return ProgressEvidenceSelectionProposal(schema_version=schema, policy_version=policy,
                                             selected_evidence_tokens=tuple(tokens))


def plan(c=None, e=None):
    return prepare_progress_evidence_selection(card=c if c is not None else card(),
                                               evidence=e if e is not None else evidence())


def validate(planning, prop):
    return validate_progress_evidence_selection_proposal(planning=planning, proposal=prop)


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_and_display_bound_are_frozen():
    assert PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION == "progress-evidence-selection-v1"
    assert PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION == "progress-evidence-selection-policy-1"
    assert MAX_SELECTED_EVIDENCE == 3 and type(MAX_SELECTED_EVIDENCE) is int
    assert sel.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION == "current-progress-projection-v1"
    assert sel.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION == "current-progress-policy-1"
    assert sel.SUPPORTED_PROGRESS_EVIDENCE_SCHEMA_VERSION == "progress-evidence-set-v1"
    assert sel.SUPPORTED_PROGRESS_EVIDENCE_POLICY_VERSION == "progress-evidence-policy-1"


def test_supported_upstream_versions_are_exactly_the_current_6_4a_and_6_4b1_ones():
    """Garde de version : si 6-4A ou 6-4B1 change de version, ce test (et le
    préflight) échouent jusqu'à une décision explicite pour 6-4B2."""
    assert (pp.CURRENT_PROGRESS_SCHEMA_VERSION, pp.CURRENT_PROGRESS_POLICY_VERSION) == (
        sel.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, sel.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION)
    assert (pe.PROGRESS_EVIDENCE_SCHEMA_VERSION, pe.PROGRESS_EVIDENCE_POLICY_VERSION) == (
        sel.SUPPORTED_PROGRESS_EVIDENCE_SCHEMA_VERSION, sel.SUPPORTED_PROGRESS_EVIDENCE_POLICY_VERSION)
    # Les vocabulaires lus sont ceux des modules propriétaires (jamais recopiés).
    assert sel.CLAIM_STAGE_ORDER is pp.CLAIM_STAGE_ORDER and sel.SCOPE_MODES is pe.SCOPE_MODES
    assert sel.BASIS_ORIGINS is pe.BASIS_ORIGINS and sel.EVIDENCE_STATUSES is pe.EVIDENCE_STATUSES


def test_structures_are_frozen_keyword_only_dataclasses_with_exactly_these_fields():
    expected = {
        ProgressEvidenceSelectionCapability: ["capability_token", "label"],
        ProgressEvidenceSelectionPlanning: ["schema_version", "policy_version", "card", "evidence",
                                            "represented_capabilities", "model_selection_required"],
        ProgressEvidenceSelectionProposal: ["schema_version", "policy_version", "selected_evidence_tokens"],
        SelectedProgressEvidence: ["schema_version", "policy_version", "competency_code", "evidence_status",
                                   "basis_origin", "source_claim_stage", "selected_candidates"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names, cls
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls)), cls
    hints = typing.get_type_hints(SelectedProgressEvidence)
    assert hints["selected_candidates"] == tuple[ProgressEvidenceCandidate, ...]
    assert typing.get_type_hints(ProgressEvidenceSelectionProposal)["selected_evidence_tokens"] == tuple[str, ...]
    result = resolve_deterministic_progress_evidence_selection(planning=plan(e=evidence(cands(1))))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.selected_candidates = ()
    with pytest.raises(TypeError):
        ProgressEvidenceSelectionProposal("s", "p", ())  # noqa
    assert type(result.selected_candidates) is tuple


def test_public_contracts_exclude_identifiers_scores_modes_and_user_facing_text():
    fields = {f.name for cls in (ProgressEvidenceSelectionCapability, ProgressEvidenceSelectionPlanning,
                                 ProgressEvidenceSelectionProposal, SelectedProgressEvidence)
              for f in dataclasses.fields(cls)}
    for forbidden in ("selection_mode", "selection_source", "model_selected", "deterministic_selected",
                      "reason", "rationale", "confidence", "score", "ranking", "rank", "text", "summary",
                      "explanation", "why_text", "visible_text", "message", "bullet_text", "weight", "quality",
                      "priority", "percent", "rating", "evidence_strength", "support_level", "elicitation_mode",
                      "user_id", "uuid", "id", "observation_id", "timestamp", "created_at", "proof_set",
                      "decision_basis", "complete_reason_set", "stage_causes"):
        assert forbidden not in fields, forbidden
    parts = {part for name in fields for part in name.split("_")}
    assert not {"score", "weight", "confidence", "rank", "ranking", "quality", "priority", "percent", "rating",
                "strength", "summary", "explanation", "why", "message", "bullet", "mode", "id", "at"} & parts


def test_selected_output_never_contains_a_uuid():
    result = validate(plan(), proposal("evidence_1", "evidence_2"))
    assert not UUID_PATTERN.search(repr(result))
    assert not any(isinstance(getattr(result, f.name), __import__("uuid").UUID)
                   for f in dataclasses.fields(SelectedProgressEvidence))


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidProgressEvidenceSelectionInput, UnsupportedProgressEvidenceSelectionVersion,
                InvalidProgressEvidenceSelectionProposal):
        assert issubclass(cls, ProgressEvidenceSelectionError)
    assert not issubclass(ProgressEvidenceSelectionError, (pe.ProgressEvidenceError, pp.ProgressProjectionError))
    assert not issubclass(InvalidProgressEvidenceSelectionProposal, InvalidProgressEvidenceSelectionInput)


def test_public_api_is_keyword_only():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"prepare_progress_evidence_selection", "resolve_deterministic_progress_evidence_selection",
                      "validate_progress_evidence_selection_proposal"}
    expected = {"prepare_progress_evidence_selection": ["card", "evidence"],
                "resolve_deterministic_progress_evidence_selection": ["planning"],
                "validate_progress_evidence_selection_proposal": ["planning", "proposal"]}
    for name, params in expected.items():
        signature = inspect.signature(getattr(sel, name)).parameters.values()
        assert [p.name for p in signature] == params
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
                   for p in signature)


# --------------------------------------------------------------------------
# 2. Versions amont
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("schema_version", "progress-evidence-set-v2"),
    ("policy_version", "progress-evidence-policy-2"),
    ("schema_version", None),
    ("policy_version", ""),
])
def test_unknown_received_6_4b1_versions_are_refused(field, value):
    with pytest.raises(UnsupportedProgressEvidenceSelectionVersion):
        plan(e=dataclasses.replace(evidence(), **{field: value}))


@pytest.mark.parametrize("name, value", [
    ("CURRENT_PROGRESS_SCHEMA_VERSION", "current-progress-projection-v2"),
    ("CURRENT_PROGRESS_POLICY_VERSION", "current-progress-policy-2"),
    ("PROGRESS_EVIDENCE_SCHEMA_VERSION", "progress-evidence-set-v2"),
    ("PROGRESS_EVIDENCE_POLICY_VERSION", "progress-evidence-policy-2"),
])
def test_a_future_upstream_module_policy_is_never_accepted_silently(monkeypatch, name, value):
    monkeypatch.setattr(sel, name, value)
    for c, e in ((card(), evidence()), (absent_card(), evidence(status="no_state"))):
        with pytest.raises(UnsupportedProgressEvidenceSelectionVersion):
            plan(c, e)


# --------------------------------------------------------------------------
# 3. Statuts non available
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status, make_card", NON_AVAILABLE)
def test_non_available_statuses_select_nothing_and_keep_their_provenance(status, make_card):
    e = evidence(status=status)
    planning = plan(make_card(), e)
    assert planning.model_selection_required is False
    result = resolve_deterministic_progress_evidence_selection(planning=planning)
    assert result == SelectedProgressEvidence(
        schema_version=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
        competency_code="C7",
        evidence_status=status,
        basis_origin=None,
        source_claim_stage=None,
        selected_candidates=(),
    )
    assert validate(planning, proposal()) == result
    with pytest.raises(InvalidProgressEvidenceSelectionProposal):
        validate(planning, proposal("evidence_1"))


@pytest.mark.parametrize("status, make_card", NON_AVAILABLE)
@pytest.mark.parametrize("changes", [
    {"basis_origin": "direct"},
    {"source_claim_stage": "application"},
    {"candidates": (cand(1),)},
    {"candidates": []},
])
def test_non_available_with_provenance_or_candidates_is_refused(status, make_card, changes):
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(make_card(), dataclasses.replace(evidence(status=status), **changes))


# --------------------------------------------------------------------------
# 4. available : provenance, jetons, candidates
# --------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"basis_origin": None},
    {"basis_origin": "indirect"},
    {"basis_origin": "DIRECT"},
    {"source_claim_stage": None},
    {"source_claim_stage": "non_etabli"},
    {"source_claim_stage": "expert"},
    {"candidates": ()},
    {"candidates": [cand(1), cand(2)]},
    {"evidence_status": "unknown"},
    {"evidence_status": None},
])
def test_available_contract_is_enforced(changes):
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(e=dataclasses.replace(evidence(), **changes))


@pytest.mark.parametrize("stage, origin, source, ok", [
    ("application", "direct", "application", True),
    ("application", "direct", "mastery", False),
    ("comprehension", "inherited_from_higher_claim", "application", True),
    ("comprehension", "inherited_from_higher_claim", "mastery", True),
    ("comprehension", "inherited_from_higher_claim", "comprehension", False),
    ("comprehension", "inherited_from_higher_claim", "discovery", False),
    ("mastery", "inherited_from_higher_claim", "mastery", False),
])
def test_basis_origin_is_coherent_with_the_displayed_stage(stage, origin, source, ok):
    c, e = card(stage=stage), evidence(origin=origin, source=source)
    if ok:
        assert plan(c, e).evidence is e
    else:
        with pytest.raises(InvalidProgressEvidenceSelectionInput):
            plan(c, e)


@pytest.mark.parametrize("tokens", [
    ("evidence_1", "evidence_3"),
    ("evidence_2", "evidence_3"),
    ("evidence_1", "evidence_1"),
    ("evidence_0", "evidence_1"),
    ("evidence_2", "evidence_1"),
    ("evidence_1", "6a4b2000-0000-4000-8000-000000000000"),
    ("evidence_01", "evidence_2"),
    ("Evidence_1", "evidence_2"),
])
def test_evidence_tokens_must_be_exactly_sequential(tokens):
    candidates = tuple(dataclasses.replace(cand(1), evidence_token=t) for t in tokens)
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(e=evidence(candidates))


@pytest.mark.parametrize("bad", [
    {"observation_text": ""},
    {"observation_text": "   \n\t"},
    {"observation_text": None},
    {"observation_text": b"bytes"},
    {"observation_text": "avec \x00 NUL"},
    {"observation_text": "surrogate \ud800 isolé"},
    {"scope_mode": "mixed"},
    {"scope_mode": "LOCALIZED"},
    {"scope_mode": None},
    {"capability_tokens": ["C7_A@r1"]},
    {"capability_tokens": ()},
    {"capability_tokens": ("C7_A@r1", "C7_A@r1")},
    {"capability_tokens": ("C7_D@r1",)},
    {"capability_tokens": ("C7_C@r1",)},
    {"capability_tokens": ("C7_A",)},
    {"capability_tokens": ("C8_A@r1",)},
    {"capability_tokens": (1,)},
    {"scope_mode": "competency_only"},
])
def test_each_candidate_is_structurally_exploitable(bad):
    candidates = (cand(1), dataclasses.replace(cand(2), **bad))
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(e=evidence(candidates))


def test_candidate_must_be_exactly_a_6_4b1_candidate():
    class Forged(ProgressEvidenceCandidate):
        pass

    forged = Forged(**{f.name: getattr(cand(2), f.name) for f in dataclasses.fields(ProgressEvidenceCandidate)})
    for bad in (forged, {"evidence_token": "evidence_2"}, "evidence_2", None):
        with pytest.raises(InvalidProgressEvidenceSelectionInput):
            plan(e=evidence((cand(1), bad)))


def test_competency_only_candidates_need_a_competency_only_or_mixed_card():
    co = cand(1, scope="competency_only")
    assert plan(card(coverage="competency_only", caps=()), evidence((co,))).model_selection_required is False
    assert plan(card(coverage="mixed"), evidence((co, cand(2)))).model_selection_required is True
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(card(coverage="localized"), evidence((co, cand(2))))
    # Localized sur une carte competency_only : aucune capacité représentée.
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(card(coverage="competency_only", caps=()), evidence((cand(1),)))


def test_observation_text_is_never_bounded_nor_altered_by_the_preflight():
    long_text = "Mécanisme décrit longuement, sans troncature. " * 5000
    e = evidence((cand(1, text=long_text), cand(2, text="  espaces conservés  ")))
    planning = plan(e=e)
    assert planning.evidence.candidates[0].observation_text == long_text
    assert planning.evidence.candidates[1].observation_text == "  espaces conservés  "


def test_many_candidates_are_all_kept_no_candidate_cap():
    e = evidence(cands(150))
    planning = plan(e=e)
    assert planning.evidence.candidates == e.candidates and len(planning.evidence.candidates) == 150
    assert planning.model_selection_required is True


# --------------------------------------------------------------------------
# 5. Carte 6-4A
# --------------------------------------------------------------------------

def test_card_and_evidence_must_be_exact_types():
    for c, e in ((None, evidence()), (card(), None), (evidence(), card()), (dataclasses.asdict(card()), evidence()),
                 (card(), dataclasses.asdict(evidence()))):
        with pytest.raises(InvalidProgressEvidenceSelectionInput):
            prepare_progress_evidence_selection(card=c, evidence=e)

    class Forged(CompetencyCurrentProgress):
        pass

    forged = Forged(**{f.name: getattr(card(), f.name) for f in dataclasses.fields(CompetencyCurrentProgress)})
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(forged, evidence())


def test_card_and_evidence_must_describe_the_same_competency():
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(card(), evidence(code="C8"))
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(absent_card("C8"), evidence(status="no_state", code="C7"))


@pytest.mark.parametrize("changes", [
    {"competency_code": "C13"},
    {"competency_code": None},
    {"competency_label": ""},
    {"competency_label": None},
    {"state_present": 1},
    {"stage_code": "expert"},
    {"stage_code": None},
    {"stage_label": "Avancé"},
    {"stage_label": VISIBLE_STAGE_LABELS["mastery"]},
    {"coverage_mode": "partial"},
    {"coverage_mode": "none"},
    {"represented_capabilities": ()},
    {"represented_capabilities": [VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1,
                                                              label="A")]},
    {"represented_capabilities": ({"capability_code": "C7_A"},)},
    {"represented_capabilities": (VisibleCapabilityProjection(capability_code="C8_A", semantic_revision=1,
                                                              label="A"),)},
    {"represented_capabilities": (VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=0,
                                                              label="A"),)},
    {"represented_capabilities": (VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=True,
                                                              label="A"),)},
    {"represented_capabilities": (VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1,
                                                              label=" "),)},
    {"represented_capabilities": (VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1,
                                                              label="A"),) * 2},
])
def test_available_card_is_well_formed(changes):
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(dataclasses.replace(card(), **changes), evidence())


@pytest.mark.parametrize("c, status", [
    (absent_card(), "no_positive_basis"),
    (absent_card(), "available"),
    (non_etabli_card(), "no_state"),
    (non_etabli_card(), "current_stage_not_established"),
    (not_established_card(), "available"),
    (not_established_card(), "no_positive_basis"),
    (card(), "current_stage_not_established"),
    (card(), "no_state"),
])
def test_evidence_status_must_match_the_card(c, status):
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(c, evidence(status=status))


@pytest.mark.parametrize("make, changes", [
    (absent_card, {"stage_code": "application"}),
    (absent_card, {"stage_label": VISIBLE_STAGE_LABELS["non_etabli"]}),
    (absent_card, {"coverage_mode": "localized"}),
    (non_etabli_card, {"coverage_mode": "competency_only"}),
    (non_etabli_card, {"stage_label": NO_STATE_LABEL}),
    (not_established_card, {"represented_capabilities": (
        VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1, label="A"),)}),
])
def test_cards_without_examples_are_well_formed_too(make, changes):
    statuses = {absent_card: "no_state", non_etabli_card: "no_positive_basis",
                not_established_card: "current_stage_not_established"}
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        plan(dataclasses.replace(make(), **changes), evidence(status=statuses[make]))


def test_planning_exposes_only_tokens_and_labels_of_the_card_capabilities():
    planning = plan()
    assert planning.represented_capabilities == (
        ProgressEvidenceSelectionCapability(capability_token="C7_A@r1", label="Résultat comptable et trésorerie"),
        ProgressEvidenceSelectionCapability(capability_token="C7_B@r1", label="Besoin en fonds de roulement"),
        ProgressEvidenceSelectionCapability(capability_token="C7_C@r2", label="Qualité du free cash flow"),
    )
    assert plan(card(coverage="competency_only", caps=()),
                evidence((cand(1, scope="competency_only"),))).represented_capabilities == ()


# --------------------------------------------------------------------------
# 6. Chemins déterministes
# --------------------------------------------------------------------------

def test_one_candidate_is_selected_directly_as_the_original_object():
    only = cand(1, scope="localized", tokens=("C7_B@r1",), support="answer_given", elicitation="spontaneous")
    e = evidence((only,))
    planning = plan(e=e)
    assert planning.model_selection_required is False
    result = resolve_deterministic_progress_evidence_selection(planning=planning)
    assert result.selected_candidates == (only,) and result.selected_candidates[0] is only
    assert (result.competency_code, result.evidence_status, result.basis_origin, result.source_claim_stage) == (
        "C7", "available", "direct", "application")
    assert validate(planning, proposal("evidence_1")) == result
    for bad in (proposal(), proposal("evidence_2"), proposal("evidence_1", "evidence_1")):
        with pytest.raises(InvalidProgressEvidenceSelectionProposal):
            validate(planning, bad)


@pytest.mark.parametrize("n", [2, 3, 4, 20])
def test_deterministic_path_refuses_when_a_semantic_choice_exists(n):
    planning = plan(e=evidence(cands(n)))
    assert planning.model_selection_required is True
    with pytest.raises(InvalidProgressEvidenceSelectionInput, match="aucun repli"):
        resolve_deterministic_progress_evidence_selection(planning=planning)


# --------------------------------------------------------------------------
# 7. Validateur
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tokens", [
    ("evidence_1",), ("evidence_4",), ("evidence_1", "evidence_3"), ("evidence_2", "evidence_3", "evidence_5"),
    ("evidence_1", "evidence_2", "evidence_3"), ("evidence_5",),
])
def test_one_to_three_known_unique_tokens_in_input_order_are_valid(tokens):
    e = evidence(cands(5))
    result = validate(plan(e=e), proposal(*tokens))
    assert tuple(c.evidence_token for c in result.selected_candidates) == tokens
    indices = [int(t.split("_")[1]) - 1 for t in tokens]
    assert all(got is e.candidates[i] for got, i in zip(result.selected_candidates, indices))


@pytest.mark.parametrize("tokens", [
    (),
    ("evidence_1", "evidence_2", "evidence_3", "evidence_4"),
    ("evidence_1", "evidence_2", "evidence_3", "evidence_4", "evidence_5"),
    ("evidence_99",),
    ("evidence_0",),
    ("evidence_1", "evidence_99"),
    ("evidence_2", "evidence_2"),
    ("evidence_3", "evidence_1"),
    ("evidence_1", "evidence_3", "evidence_2"),
    ("EVIDENCE_1",),
    (" evidence_1",),
    (1,),
    (None,),
    (["evidence_1"],),
])
def test_invalid_selections_are_refused_never_corrected(tokens):
    with pytest.raises(InvalidProgressEvidenceSelectionProposal):
        validate(plan(e=evidence(cands(5))), proposal(*tokens))


def test_reversed_order_is_refused_and_never_resorted():
    planning = plan(e=evidence(cands(3)))
    with pytest.raises(InvalidProgressEvidenceSelectionProposal, match="ordre"):
        validate(planning, proposal("evidence_3", "evidence_1"))
    assert validate(planning, proposal("evidence_1", "evidence_3")).selected_candidates == (
        planning.evidence.candidates[0], planning.evidence.candidates[2])


def test_subsequence_of_the_6_4b1_order_is_valid():
    e = evidence(cands(4))
    result = validate(plan(e=e), proposal("evidence_1", "evidence_3"))
    assert result.selected_candidates == (e.candidates[0], e.candidates[2])


@pytest.mark.parametrize("bad", [
    ProgressEvidenceSelectionProposal(schema_version=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
                                      policy_version=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
                                      selected_evidence_tokens=["evidence_1"]),
    proposal("evidence_1", schema="progress-evidence-selection-v2"),
    proposal("evidence_1", policy="progress-evidence-selection-policy-2"),
    proposal("evidence_1", schema=None),
    {"schema_version": PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
     "policy_version": PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION, "selected_evidence_tokens": ("evidence_1",)},
    None,
])
def test_proposal_shape_and_versions_are_exact(bad):
    with pytest.raises(InvalidProgressEvidenceSelectionProposal):
        validate(plan(), bad)


def test_no_exhaustive_coverage_is_required():
    """Carte à trois capacités ; un seul exemple qui n'en illustre qu'une est
    valide (aucune checklist)."""
    e = evidence((cand(1, tokens=("C7_A@r1",)), cand(2, tokens=("C7_B@r1",)), cand(3, tokens=("C7_C@r2",))))
    for token in ("evidence_1", "evidence_2", "evidence_3"):
        result = validate(plan(e=e), proposal(token))
        assert len(result.selected_candidates) == 1
    result = validate(plan(e=e), proposal("evidence_2", "evidence_3"))
    assert {t for c in result.selected_candidates for t in c.capability_tokens} == {"C7_B@r1", "C7_C@r2"}


@pytest.mark.parametrize("tokens", [
    ("evidence_1",), ("evidence_1", "evidence_3"),   # localized seulement
    ("evidence_2",), ("evidence_2", "evidence_4"),   # competency_only seulement
    ("evidence_1", "evidence_2"), ("evidence_2", "evidence_3", "evidence_4"),  # les deux
])
def test_mixed_card_has_no_family_quota(tokens):
    e = evidence((cand(1), cand(2, scope="competency_only"), cand(3, tokens=("C7_B@r1",)),
                  cand(4, scope="competency_only")))
    result = validate(plan(card(coverage="mixed"), e), proposal(*tokens))
    assert tuple(c.evidence_token for c in result.selected_candidates) == tokens


@pytest.mark.parametrize("stage, origin, source", [
    ("application", "direct", "application"),
    ("comprehension", "inherited_from_higher_claim", "application"),
    ("discovery", "inherited_from_higher_claim", "mastery"),
    ("mastery", "direct", "mastery"),
])
def test_6_4b1_provenance_is_propagated_unchanged(stage, origin, source):
    e = evidence(origin=origin, source=source)
    result = validate(plan(card(stage=stage), e), proposal("evidence_2"))
    assert (result.competency_code, result.evidence_status, result.basis_origin, result.source_claim_stage) == (
        e.competency_code, e.evidence_status, e.basis_origin, e.source_claim_stage)
    assert result.selected_candidates[0] is e.candidates[1]


def test_selected_candidates_keep_every_6_4b1_field_including_support_provenance():
    original = cand(2, text="Texte exact", support="guided", elicitation="spontaneous")
    e = evidence((cand(1), original))
    (selected,) = validate(plan(e=e), proposal("evidence_2")).selected_candidates
    assert selected is original
    assert dataclasses.asdict(selected) == dataclasses.asdict(original)


def test_support_and_elicitation_never_change_the_validation():
    """Le validateur n'a aucune hiérarchie aide / élicitation : toute
    candidate connue est également sélectionnable."""
    supports = ("none", "hinted", "guided", "answer_given")
    e = evidence(tuple(cand(i + 1, support=s, elicitation=("prompted", "spontaneous")[i % 2])
                       for i, s in enumerate(supports)))
    planning = plan(e=e)
    for index in range(4):
        assert validate(planning, proposal(f"evidence_{index + 1}")).selected_candidates == (e.candidates[index],)


# --------------------------------------------------------------------------
# 8. Planning forgé, déterminisme, entrées intactes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"model_selection_required": False},
    {"represented_capabilities": ()},
    {"schema_version": "x"},
    {"evidence": dataclasses.replace(evidence(), evidence_status="no_state")},
])
def test_forged_planning_is_never_trusted(changes):
    forged = dataclasses.replace(plan(), **changes)
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        validate(forged, proposal("evidence_1"))
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        resolve_deterministic_progress_evidence_selection(planning=forged)
    for bad in (None, dataclasses.asdict(plan())):
        with pytest.raises(InvalidProgressEvidenceSelectionInput):
            validate(bad, proposal("evidence_1"))


def test_forged_one_candidate_planning_cannot_reach_the_deterministic_path():
    forged = dataclasses.replace(plan(e=evidence(cands(3))), model_selection_required=False)
    with pytest.raises(InvalidProgressEvidenceSelectionInput):
        resolve_deterministic_progress_evidence_selection(planning=forged)


def test_pure_and_deterministic_inputs_untouched():
    c, e = card(coverage="mixed"), evidence((cand(1), cand(2, scope="competency_only"), cand(3)))
    before = (dataclasses.asdict(c), dataclasses.asdict(e))
    first = validate(plan(c, e), proposal("evidence_1", "evidence_2"))
    second = validate(plan(c, e), proposal("evidence_1", "evidence_2"))
    assert first == second
    assert (dataclasses.asdict(c), dataclasses.asdict(e)) == before
    assert plan(c, e) == plan(c, e)


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


def test_imports_are_exactly_stdlib_and_the_6_4a_6_4b1_public_contracts():
    assert _imports() == {
        "dataclasses": {"dataclass"},
        "core.adaptation_state": {"COMPETENCY_ORDER"},
        "core.progress_evidence": {
            "BASIS_DIRECT", "BASIS_INHERITED_FROM_HIGHER_CLAIM", "BASIS_ORIGINS", "EVIDENCE_AVAILABLE",
            "EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED", "EVIDENCE_NO_POSITIVE_BASIS", "EVIDENCE_NO_STATE",
            "EVIDENCE_STATUSES", "PROGRESS_EVIDENCE_POLICY_VERSION", "PROGRESS_EVIDENCE_SCHEMA_VERSION",
            "SCOPE_COMPETENCY_ONLY", "SCOPE_LOCALIZED", "SCOPE_MODES", "ProgressEvidenceCandidate",
            "ProgressEvidenceSet"},
        "core.progress_projection": {
            "CLAIM_STAGE_ORDER", "COVERAGE_COMPETENCY_ONLY", "COVERAGE_LOCALIZED", "COVERAGE_MIXED",
            "COVERAGE_MODES", "COVERAGE_NONE", "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
            "NO_STATE_LABEL", "NON_ETABLI", "VISIBLE_STAGE_LABELS", "CompetencyCurrentProgress",
            "VisibleCapabilityProjection"},
    }
    imported = set().union(*_imports().values())
    assert not any(name.startswith("_") for name in imported)
    # Aucun snapshot Step 5, aucune acquisition 6-4B1 rejouée, aucun sélecteur.
    for name in ("AdaptationStateSnapshot", "load_adaptation_state", "acquire_progress_evidence",
                 "project_current_progress", "CurrentProgressProjection"):
        assert name not in imported and name not in _code_tokens(_source()).split(), name
    assert "core.progress_evidence_selector" not in _imports()


def test_no_db_session_service_nor_persistence():
    imported = set(_imports())
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "core.inference_service",
                "core.observation_service", "core.taxonomy_service", "core.longitudinal_service",
                "core.cognitive_capture", "alembic", "psycopg2"} & imported
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    for forbidden in ("Session", "session", "db", "add", "add_all", "flush", "commit", "rollback", "execute",
                      "select", "insert", "update", "delete", "merge", "query", "no_autoflush", "refresh",
                      "add_support_trace", "SupportTrace", "support_trace", "save", "persist", "cache"):
        assert forbidden not in names, forbidden


def test_no_llm_network_clock_randomness_nor_environment():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "random", "secrets", "datetime",
                "time", "os", "json", "logging", "uuid", "functools"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "backend", "complete(", "environ", "getenv",
                 "uuid4", "retry"):
        assert word not in tokens, word
    assert not {"llm", "now", "today", "global"} & set(tokens.split())


def test_no_candidate_or_text_bound_and_no_truncation():
    source = _source()
    for name in ("MAX_CANDIDATES", "MAX_OBSERVATION_TEXT", "MAX_TEXT_PER_CANDIDATE", "MAX_OBSERVATION_TEXT_CHARS"):
        assert name not in source, name
    assert [n.targets[0].id for n in _tree().body if isinstance(n, ast.Assign)
            and n.targets[0].id.startswith("MAX_")] == ["MAX_SELECTED_EVIDENCE"]
    slices = [n for n in ast.walk(_tree()) if isinstance(n, ast.Slice)]
    assert slices == []  # aucune troncature (ni texte, ni candidates, ni jetons)
    calls = {n.func.id for n in ast.walk(_tree()) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {"sorted", "reversed", "min", "max", "sum"} & calls
    assert "sort" not in {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}


def test_support_elicitation_and_strength_are_never_read():
    read = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    tokens = set(_code_tokens(_source()).split())
    for name in ("support_level", "elicitation_mode", "evidence_strength", "observation_role", "local_stage",
                 "created_at", "timestamp", "confidence", "tension", "validation_need", "state_generation"):
        assert name not in read and name not in tokens, name


def test_no_score_rank_quality_nor_user_facing_text_in_the_code():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "confidence", "rank", "ranking", "quality", "priority", "percent", "rating",
                 "strength", "strongest", "best", "convincing", "reliable", "recent", "recency", "independent",
                 "autonomous", "spontaneous", "assisted", "summary", "explanation", "why", "bullet", "render",
                 "message", "recommend", "weak", "coverage_quota", "quota"):
        assert word not in parts, word


def test_not_wired_to_api_runtime_or_any_module_but_its_selector():
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_evidence_selection.py" and "progress_evidence_selection" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    # Étape 6.4B3 : la frontière de rendu et son renderer lisent les seuls
    # contrats publics de sortie (SelectedProgressEvidence, versions,
    # MAX_SELECTED_EVIDENCE), jamais la préparation ni le validateur de
    # sélection ; aucun n'est branché au runtime.
    assert sorted(users) == ["core/progress_evidence_renderer.py", "core/progress_evidence_rendering.py",
                             "core/progress_evidence_selector.py"]
    step6_b3_imports = {
        "core/progress_evidence_rendering.py": {
            "MAX_SELECTED_EVIDENCE", "PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION",
            "PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION", "SelectedProgressEvidence"},
        "core/progress_evidence_renderer.py": {"MAX_SELECTED_EVIDENCE", "SelectedProgressEvidence"},
    }
    for rel, names in step6_b3_imports.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "progress_evidence_selection" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.progress_evidence_selection", name) for name in names), rel
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("SelectedProgressEvidence", "prepare_progress_evidence_selection",
                 "validate_progress_evidence_selection_proposal", "select_representative_progress_evidence"):
        assert name not in api, name
    tokens = _code_tokens(_source()).lower()
    for word in ("web_chat", "fastapi", "route", "endpoint", "coach", "education", "decrypt", "portfolio",
                 "rallye", "academy", "__tablename__", "column"):
        assert word not in tokens, word


def test_no_migration_nor_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0009_competency_inference_state.py" and len(versions) == 9
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "Selection" not in models and "selected_evidence" not in models and "progress_evidence" not in models


def test_no_module_level_mutable_state():
    for name, value in vars(sel).items():
        if not name.startswith("__"):
            assert not isinstance(value, (list, dict, set)), name
