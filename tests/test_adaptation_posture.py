"""Tests de l'étape 6.2B : posture de base de l'interaction, segment par
segment (core/adaptation_posture.py).

Aucune base, aucun réseau, aucun modèle : build_posture_baseline est pur.
Les profils viennent soit du validateur réel de 6-2A
(validate_task_profile_proposal), soit de la chaîne réelle
InteractionTaskInput -> classify_interaction_task (backend factice) ->
build_posture_baseline ; les dataclasses forgées à la main ne servent qu'à
prouver le fail-closed sur des profils incohérents.
"""
import ast
import enum
import inspect
import json
import re
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace

import pytest

from core import adaptation_posture as ap
from core import adaptation_task as at
from core.adaptation_posture import (
    CHALLENGE,
    CHALLENGE_TASK_OPERATIONS,
    CO_REASON,
    DIRECT_FALLBACK_BASIS,
    EXPLAIN,
    EXPLICIT_INTENT_BASIS,
    GUIDE,
    POSTURE_BASELINE_SCHEMA_VERSION,
    POSTURE_POLICY_VERSION,
    POSTURES,
    SELECTION_BASES,
    TASK_OPERATION_BASIS,
    InteractionPostureBaseline,
    InvalidPostureArgument,
    InvalidTaskProfileContent,
    PostureBaselineError,
    SegmentPostureDecision,
    UnsupportedTaskProfileVersion,
    build_posture_baseline,
)
from core.adaptation_task import (
    CHALLENGE_REQUESTED,
    COGNITIVE_OPERATIONS,
    DIRECT_ANSWER_REQUESTED,
    EXPLICIT_INTENTS,
    JOINT_REASONING_REQUESTED,
    MAX_TASK_SEGMENTS,
    NO_TASK,
    REASONING_RESERVED_FOR_USER,
    TASK_CHARACTERISTICS,
    TASK_POLICY_VERSION,
    TASK_REQUESTED,
    TASK_SCHEMA_VERSION,
    UNSPECIFIED,
    InteractionTaskInput,
    InteractionTaskProfile,
    TaskError,
    TaskProfileProposal,
    TaskSegment,
    TaskSegmentProposal,
    validate_task_profile_proposal,
)
from core.adaptation_task_classifier import classify_interaction_task
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_posture.py"


# --------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------

def _seg(excerpt, intent=UNSPECIFIED, operations=("retrieve_or_define",), characteristics=()):
    return TaskSegmentProposal(source_excerpt=excerpt, explicit_intent=intent,
                               cognitive_operations=tuple(operations), task_characteristics=tuple(characteristics))


def _validated(message, *segments, status=TASK_REQUESTED):
    """Profil produit par le VRAI validateur de 6-2A."""
    proposal = TaskProfileProposal(schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION,
                                   request_status=status, segments=tuple(segments))
    return validate_task_profile_proposal(proposal, InteractionTaskInput(current_message=message, context_turns=()))


def _one(intent, operations=("retrieve_or_define",), characteristics=(), message="Tâche."):
    return _validated(message, _seg(message, intent, operations, characteristics))


def _decision(baseline):
    (decision,) = baseline.segments
    return decision.posture, decision.selection_basis


class FakeBackend:
    """Backend factice du classificateur 6-2A : rend la sortie imposée."""

    def __init__(self, output):
        self.output = output
        self.calls = 0

    def complete(self, *, system_prompt, user_message):
        self.calls += 1
        return self.output


def _classified(message, *segments, status=TASK_REQUESTED):
    """Chaîne réelle : InteractionTaskInput -> classify_interaction_task."""
    output = json.dumps({
        "schema_version": TASK_SCHEMA_VERSION, "policy_version": TASK_POLICY_VERSION, "request_status": status,
        "segments": [{"source_excerpt": e, "explicit_intent": i, "cognitive_operations": list(o),
                      "task_characteristics": list(c)} for e, i, o, c in segments]}, ensure_ascii=False)
    backend = FakeBackend(output)
    profile = classify_interaction_task(InteractionTaskInput(current_message=message, context_turns=()),
                                        backend=backend)
    assert backend.calls == 1
    return profile


def _forged(*segments, status=TASK_REQUESTED, **overrides):
    """Profil forgé à la main (sans passer par le validateur)."""
    values = {"schema_version": TASK_SCHEMA_VERSION, "policy_version": TASK_POLICY_VERSION,
              "request_status": status, "segments": tuple(segments)}
    values.update(overrides)
    return InteractionTaskProfile(**values)


def _task_segment(excerpt="Tâche.", intent=UNSPECIFIED, operations=("retrieve_or_define",), characteristics=()):
    return TaskSegment(source_excerpt=excerpt, explicit_intent=intent, cognitive_operations=operations,
                       task_characteristics=characteristics)


# --------------------------------------------------------------------------
# 0. Contrats publics, versions, vocabulaires
# --------------------------------------------------------------------------

def test_frozen_versions_distinct_from_6_2a():
    assert POSTURE_BASELINE_SCHEMA_VERSION == "interaction-posture-baseline-v1"
    assert POSTURE_POLICY_VERSION == "posture-policy-1"
    assert {POSTURE_BASELINE_SCHEMA_VERSION, POSTURE_POLICY_VERSION}.isdisjoint(
        {TASK_SCHEMA_VERSION, TASK_POLICY_VERSION})


def test_closed_vocabularies_are_frozen():
    assert POSTURES == ("explain", "guide", "co_reason", "challenge")
    assert (EXPLAIN, GUIDE, CO_REASON, CHALLENGE) == POSTURES
    assert SELECTION_BASES == ("explicit_intent", "task_operation", "direct_fallback")
    assert (EXPLICIT_INTENT_BASIS, TASK_OPERATION_BASIS, DIRECT_FALLBACK_BASIS) == SELECTION_BASES
    assert CHALLENGE_TASK_OPERATIONS == ("evaluate_existing_reasoning", "stress_test", "falsify")
    for vocabulary in (POSTURES, SELECTION_BASES, CHALLENGE_TASK_OPERATIONS):
        assert type(vocabulary) is tuple and len(set(vocabulary)) == len(vocabulary)
        assert all(type(value) is str for value in vocabulary)


def test_challenge_operations_are_exactly_the_6_2a_evaluation_operations():
    # Même ensemble que la règle de cohérence de 6-2A (aucune dérive).
    assert CHALLENGE_TASK_OPERATIONS == at._EVALUATION_OPERATIONS
    assert set(CHALLENGE_TASK_OPERATIONS) <= set(COGNITIVE_OPERATIONS)


def test_vocabularies_are_reused_not_duplicated():
    # Les vocabulaires de 6-2A sont importés, jamais redéfinis.
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name)}
    for name in ("EXPLICIT_INTENTS", "COGNITIVE_OPERATIONS", "TASK_CHARACTERISTICS", "REQUEST_STATUSES",
                 "TASK_SCHEMA_VERSION", "TASK_POLICY_VERSION", "UNSPECIFIED", "NO_TASK", "MAX_TASK_SEGMENTS"):
        assert name not in assigned, name
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is str]
    for value in set(COGNITIVE_OPERATIONS) | set(TASK_CHARACTERISTICS) | set(EXPLICIT_INTENTS):
        assert value not in constants, value


def test_every_explicit_intent_has_exactly_one_rule():
    assert set(ap._POSTURE_BY_EXPLICIT_INTENT) | {UNSPECIFIED} == set(EXPLICIT_INTENTS)
    assert UNSPECIFIED not in ap._POSTURE_BY_EXPLICIT_INTENT
    assert set(ap._POSTURE_BY_EXPLICIT_INTENT.values()) == set(POSTURES)


def test_public_api_signature_takes_only_the_task_profile():
    parameters = inspect.signature(build_posture_baseline).parameters
    assert list(parameters) == ["task_profile"]


def test_errors_are_a_small_explicit_hierarchy():
    for error in (InvalidPostureArgument, UnsupportedTaskProfileVersion, InvalidTaskProfileContent):
        assert issubclass(error, PostureBaselineError)
    assert issubclass(PostureBaselineError, Exception)
    assert not issubclass(PostureBaselineError, TaskError)


def test_dataclasses_are_frozen_kw_only_with_exact_minimal_fields():
    expected = {
        SegmentPostureDecision: ["segment_index", "source_excerpt", "posture", "selection_basis"],
        InteractionPostureBaseline: ["schema_version", "policy_version", "source_task_schema_version",
                                     "source_task_policy_version", "request_status", "segments"],
    }
    for cls, names in expected.items():
        assert is_dataclass(cls) and cls.__dataclass_params__.frozen, cls
        assert [f.name for f in fields(cls)] == names, cls
        assert all(f.kw_only for f in fields(cls)), cls


# --------------------------------------------------------------------------
# 1-4. L'intention explicite détermine la posture
# --------------------------------------------------------------------------

@pytest.mark.parametrize("intent, posture", [
    (DIRECT_ANSWER_REQUESTED, EXPLAIN),
    (REASONING_RESERVED_FOR_USER, GUIDE),
    (JOINT_REASONING_REQUESTED, CO_REASON),
    (CHALLENGE_REQUESTED, CHALLENGE),
])
def test_1_4_explicit_intent_maps_to_its_posture(intent, posture):
    assert _decision(build_posture_baseline(_one(intent))) == (posture, EXPLICIT_INTENT_BASIS)


# --------------------------------------------------------------------------
# 5-11. unspecified : réponse directe, jamais un challenge artificiel
# --------------------------------------------------------------------------

@pytest.mark.parametrize("operations, characteristics", [
    (("retrieve_or_define",), ()),                                                            # 5 / 6
    (("explain_mechanism",), ("domain_specialized",)),                                          # 7
    (("apply_procedure", "calculate"), ("user_specific_application",)),                         # 8
    (("compare",), ("domain_specialized",)),                                                    # 9
    (("construct_reasoning", "form_conclusion"), ("multi_step", "open_ended")),                 # 10
    (("synthesize",), ()),                                                                      # 11
    (("interpret_evidence",), ("open_ended",)),
    (("generate_hypotheses", "construct_reasoning"), ("multi_step", "open_ended", "domain_specialized")),
    (("select_relevant_information", "interpret_evidence", "compare", "construct_reasoning", "form_conclusion",
      "synthesize"), ("multi_step", "domain_specialized", "open_ended", "user_specific_application")),
])
def test_5_11_unspecified_without_challenge_operation_is_a_direct_explanation(operations, characteristics):
    profile = _one(UNSPECIFIED, operations, characteristics)
    assert _decision(build_posture_baseline(profile)) == (EXPLAIN, DIRECT_FALLBACK_BASIS)


def test_5_11_every_non_challenge_operation_alone_falls_back_to_explain():
    for operation in COGNITIVE_OPERATIONS:
        if operation in CHALLENGE_TASK_OPERATIONS:
            continue
        assert _decision(build_posture_baseline(_one(UNSPECIFIED, (operation,)))) == (EXPLAIN, DIRECT_FALLBACK_BASIS)


# --------------------------------------------------------------------------
# 12-14. unspecified + tâche réellement évaluative -> challenge
# --------------------------------------------------------------------------

@pytest.mark.parametrize("operations, characteristics", [
    (("evaluate_existing_reasoning",), ("existing_reasoning_to_evaluate",)),                     # 12
    (("stress_test",), ()),                                                                     # 13
    (("stress_test",), ("existing_reasoning_to_evaluate",)),
    (("falsify",), ()),                                                                         # 14
    (("generate_hypotheses", "falsify"),
     ("domain_specialized", "open_ended", "user_specific_application", "existing_reasoning_to_evaluate")),
    (("explain_mechanism", "stress_test"), ("domain_specialized",)),
])
def test_12_14_unspecified_with_a_challenge_operation_is_a_task_challenge(operations, characteristics):
    profile = _one(UNSPECIFIED, operations, characteristics)
    assert _decision(build_posture_baseline(profile)) == (CHALLENGE, TASK_OPERATION_BASIS)


def test_12_evaluate_existing_reasoning_without_its_characteristic_is_refused_not_challenged():
    forged = _forged(_task_segment(operations=("evaluate_existing_reasoning",)))
    with pytest.raises(InvalidTaskProfileContent, match="sans la caractéristique"):
        build_posture_baseline(forged)


def test_12_existing_reasoning_characteristic_without_evaluation_operation_is_refused():
    forged = _forged(_task_segment(operations=("compare",), characteristics=("existing_reasoning_to_evaluate",)))
    with pytest.raises(InvalidTaskProfileContent, match="sans opération"):
        build_posture_baseline(forged)


# --------------------------------------------------------------------------
# 15-18. L'intention explicite domine la nature de la tâche
# --------------------------------------------------------------------------

EVALUATIVE = (("evaluate_existing_reasoning", "stress_test", "falsify"), ("existing_reasoning_to_evaluate",))


def test_15_explicit_direct_answer_dominates_an_evaluative_operation():
    # « Répondre est prioritaire sur tester. »
    assert _decision(build_posture_baseline(_one(DIRECT_ANSWER_REQUESTED, *EVALUATIVE))) == (
        EXPLAIN, EXPLICIT_INTENT_BASIS)
    complex_task = (("select_relevant_information", "interpret_evidence", "construct_reasoning", "form_conclusion"),
                    ("multi_step", "domain_specialized", "open_ended", "user_specific_application"))
    assert _decision(build_posture_baseline(_one(DIRECT_ANSWER_REQUESTED, *complex_task))) == (
        EXPLAIN, EXPLICIT_INTENT_BASIS)


@pytest.mark.parametrize("intent, posture", [
    (REASONING_RESERVED_FOR_USER, GUIDE),     # 16
    (JOINT_REASONING_REQUESTED, CO_REASON),   # 17
    (CHALLENGE_REQUESTED, CHALLENGE),         # 18
])
def test_16_18_explicit_intent_dominates_the_task_nature(intent, posture):
    for operations, characteristics in (EVALUATIVE, (("retrieve_or_define",), ()),
                                        (("compare", "synthesize"), ("open_ended",))):
        profile = _one(intent, operations, characteristics)
        assert _decision(build_posture_baseline(profile)) == (posture, EXPLICIT_INTENT_BASIS)


def test_18_explicit_challenge_on_a_simple_task_is_kept_as_is():
    assert _decision(build_posture_baseline(_one(CHALLENGE_REQUESTED, ("retrieve_or_define",)))) == (
        CHALLENGE, EXPLICIT_INTENT_BASIS)


# --------------------------------------------------------------------------
# 19-22. Plusieurs segments : ordre, extraits, une décision par segment
# --------------------------------------------------------------------------

MULTI_MESSAGE = ("Explique-moi le ROIC, puis analyse avec moi cette entreprise, puis challenge ma conclusion, "
                 "et enfin aide-moi à calculer la marge sans me donner la réponse.")
MULTI_SEGMENTS = (
    ("Explique-moi le ROIC", UNSPECIFIED, ("explain_mechanism",), ("domain_specialized",)),
    ("analyse avec moi cette entreprise", JOINT_REASONING_REQUESTED,
     ("select_relevant_information", "interpret_evidence", "construct_reasoning", "form_conclusion"),
     ("multi_step", "open_ended", "user_specific_application")),
    ("challenge ma conclusion", CHALLENGE_REQUESTED, ("evaluate_existing_reasoning", "stress_test"),
     ("existing_reasoning_to_evaluate",)),
    ("aide-moi à calculer la marge sans me donner la réponse", REASONING_RESERVED_FOR_USER,
     ("apply_procedure", "calculate"), ("user_specific_application",)),
)


def test_19_22_multiple_segments_keep_order_excerpts_and_one_decision_each():
    profile = _validated(MULTI_MESSAGE, *(_seg(*segment) for segment in MULTI_SEGMENTS))
    baseline = build_posture_baseline(profile)
    assert baseline == InteractionPostureBaseline(
        schema_version=POSTURE_BASELINE_SCHEMA_VERSION, policy_version=POSTURE_POLICY_VERSION,
        source_task_schema_version=TASK_SCHEMA_VERSION, source_task_policy_version=TASK_POLICY_VERSION,
        request_status=TASK_REQUESTED, segments=(
            SegmentPostureDecision(segment_index=0, source_excerpt="Explique-moi le ROIC", posture=EXPLAIN,
                                   selection_basis=DIRECT_FALLBACK_BASIS),
            SegmentPostureDecision(segment_index=1, source_excerpt="analyse avec moi cette entreprise",
                                   posture=CO_REASON, selection_basis=EXPLICIT_INTENT_BASIS),
            SegmentPostureDecision(segment_index=2, source_excerpt="challenge ma conclusion", posture=CHALLENGE,
                                   selection_basis=EXPLICIT_INTENT_BASIS),
            SegmentPostureDecision(segment_index=3,
                                   source_excerpt="aide-moi à calculer la marge sans me donner la réponse",
                                   posture=GUIDE, selection_basis=EXPLICIT_INTENT_BASIS)))
    assert len(baseline.segments) == len(profile.segments)
    assert [d.segment_index for d in baseline.segments] == list(range(len(profile.segments)))
    for decision, segment in zip(baseline.segments, profile.segments):
        assert decision.source_excerpt is segment.source_excerpt


def test_21_excerpt_is_kept_verbatim_including_whitespace_and_case():
    message = "  explique-moi  le ROIC ?  "
    profile = _validated(message, _seg(message, UNSPECIFIED, ("explain_mechanism",)))
    (decision,) = build_posture_baseline(profile).segments
    assert decision.source_excerpt == message


def test_20_identical_segments_are_never_merged():
    message = "Explique-moi le ROIC. Puis : Explique-moi le ROIC."
    profile = _validated(message, _seg("Explique-moi le ROIC"), _seg("Explique-moi le ROIC"))
    baseline = build_posture_baseline(profile)
    assert [(d.segment_index, d.source_excerpt) for d in baseline.segments] == [
        (0, "Explique-moi le ROIC"), (1, "Explique-moi le ROIC")]


def test_22_max_segments_one_decision_each():
    message = " ; ".join(f"partie {i}" for i in range(MAX_TASK_SEGMENTS))
    profile = _validated(message, *(_seg(f"partie {i}") for i in range(MAX_TASK_SEGMENTS)))
    assert len(build_posture_baseline(profile).segments) == MAX_TASK_SEGMENTS


# --------------------------------------------------------------------------
# 23. no_task : aucune posture artificielle
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["Bonjour !", "Merci beaucoup pour ton aide.", "Ok, bien reçu."])
def test_23_no_task_yields_no_segment_never_an_explain(message):
    baseline = build_posture_baseline(_validated(message, status=NO_TASK))
    assert baseline == InteractionPostureBaseline(
        schema_version=POSTURE_BASELINE_SCHEMA_VERSION, policy_version=POSTURE_POLICY_VERSION,
        source_task_schema_version=TASK_SCHEMA_VERSION, source_task_policy_version=TASK_POLICY_VERSION,
        request_status=NO_TASK, segments=())


# --------------------------------------------------------------------------
# Chaîne réelle : InteractionTaskInput -> classify_interaction_task ->
# InteractionTaskProfile -> build_posture_baseline
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message, segments, expected", [
    ("Qu'est-ce que le free cash-flow ?",
     (("Qu'est-ce que le free cash-flow ?", UNSPECIFIED, ("retrieve_or_define",), ("domain_specialized",)),),
     ((EXPLAIN, DIRECT_FALLBACK_BASIS),)),
    ("Donne-moi juste l'explication : pourquoi le BFR consomme du cash ?",
     (("Donne-moi juste l'explication : pourquoi le BFR consomme du cash ?", DIRECT_ANSWER_REQUESTED,
       ("explain_mechanism",), ("domain_specialized",)),),
     ((EXPLAIN, EXPLICIT_INTENT_BASIS),)),
    ("Aide-moi à calculer le ROIC, mais ne me donne pas la réponse.",
     (("Aide-moi à calculer le ROIC, mais ne me donne pas la réponse.", REASONING_RESERVED_FOR_USER,
       ("apply_procedure", "calculate"), ("multi_step", "domain_specialized")),),
     ((GUIDE, EXPLICIT_INTENT_BASIS),)),
    ("Qu'est-ce qui pourrait prouver que ma thèse sur Nvidia est fausse ?",
     (("Qu'est-ce qui pourrait prouver que ma thèse sur Nvidia est fausse ?", UNSPECIFIED,
       ("generate_hypotheses", "falsify"),
       ("domain_specialized", "open_ended", "user_specific_application", "existing_reasoning_to_evaluate")),),
     ((CHALLENGE, TASK_OPERATION_BASIS),)),
    ("Explique-moi le ROIC puis challenge mon raisonnement sur cette entreprise.",
     (("Explique-moi le ROIC", UNSPECIFIED, ("explain_mechanism",), ("domain_specialized",)),
      ("challenge mon raisonnement sur cette entreprise", CHALLENGE_REQUESTED,
       ("evaluate_existing_reasoning", "stress_test"),
       ("domain_specialized", "user_specific_application", "existing_reasoning_to_evaluate"))),
     ((EXPLAIN, DIRECT_FALLBACK_BASIS), (CHALLENGE, EXPLICIT_INTENT_BASIS))),
    ("Analyse avec moi cette entreprise puis compare-la à son concurrent.",
     (("Analyse avec moi cette entreprise", JOINT_REASONING_REQUESTED,
       ("interpret_evidence", "construct_reasoning"), ("multi_step", "user_specific_application")),
      ("compare-la à son concurrent", UNSPECIFIED, ("compare",), ("user_specific_application",))),
     ((CO_REASON, EXPLICIT_INTENT_BASIS), (EXPLAIN, DIRECT_FALLBACK_BASIS))),
])
def test_chain_classifier_then_posture(message, segments, expected):
    profile = _classified(message, *segments)
    baseline = build_posture_baseline(profile)
    assert [(d.posture, d.selection_basis) for d in baseline.segments] == list(expected)
    assert [d.source_excerpt for d in baseline.segments] == [segment[0] for segment in segments]
    assert (baseline.source_task_schema_version, baseline.source_task_policy_version) == (
        profile.schema_version, profile.policy_version)


def test_chain_no_task():
    baseline = build_posture_baseline(_classified("Bonjour, merci pour ton aide !", status=NO_TASK))
    assert baseline.request_status == NO_TASK and baseline.segments == ()


# --------------------------------------------------------------------------
# 24-34. Fail-closed : aucune correction, aucun plan partiel, aucun repli
# --------------------------------------------------------------------------

class _Str(str):
    pass


class _Enum(str, enum.Enum):
    UNSPECIFIED = "unspecified"


class _SubProfile(InteractionTaskProfile):
    pass


VALID_SEGMENT = _task_segment()


@pytest.mark.parametrize("argument", [
    None, {}, "profile", (), TaskProfileProposal(
        schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION, request_status=NO_TASK, segments=()),
    _SubProfile(schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION, request_status=NO_TASK,
                segments=()),
    InteractionTaskInput(current_message="Bonjour", context_turns=()),
])
def test_24_wrong_profile_type_is_refused(argument):
    with pytest.raises(InvalidPostureArgument):
        build_posture_baseline(argument)


@pytest.mark.parametrize("value", ["interaction-task-profile-v2", "interaction-posture-baseline-v1", "", None,
                                   _Str(TASK_SCHEMA_VERSION)])
def test_25_unsupported_schema_version(value):
    with pytest.raises(UnsupportedTaskProfileVersion, match="schema_version"):
        build_posture_baseline(_forged(VALID_SEGMENT, schema_version=value))


@pytest.mark.parametrize("value", ["task-policy-2", "posture-policy-1", "focus-policy-1", None,
                                   _Str(TASK_POLICY_VERSION)])
def test_26_unsupported_policy_version(value):
    with pytest.raises(UnsupportedTaskProfileVersion, match="policy_version"):
        build_posture_baseline(_forged(VALID_SEGMENT, policy_version=value))


@pytest.mark.parametrize("status", ["ambiguous", "TASK_REQUESTED", "", None, _Str(TASK_REQUESTED)])
def test_27_unknown_request_status(status):
    with pytest.raises(InvalidTaskProfileContent, match="request_status"):
        build_posture_baseline(_forged(VALID_SEGMENT, status=status))


@pytest.mark.parametrize("segment", [
    None, "Tâche.", {"source_excerpt": "Tâche."},
    _seg("Tâche."),  # TaskSegmentProposal : jamais accepté à la place d'un TaskSegment validé
    SegmentPostureDecision(segment_index=0, source_excerpt="Tâche.", posture=EXPLAIN,
                           selection_basis=DIRECT_FALLBACK_BASIS),
])
def test_28_segment_of_the_wrong_type(segment):
    with pytest.raises(InvalidTaskProfileContent, match="TaskSegment attendu"):
        build_posture_baseline(_forged(segment))


@pytest.mark.parametrize("intent", ["guided", "explain", "challenge", "posture", "", None, "Unspecified",
                                    _Enum.UNSPECIFIED, _Str(UNSPECIFIED)])
def test_29_unknown_intent(intent):
    with pytest.raises(InvalidTaskProfileContent, match="explicit_intent"):
        build_posture_baseline(_forged(_task_segment(intent=intent)))


@pytest.mark.parametrize("operations, match", [
    (("define",), "hors vocabulaire"),
    (("challenge",), "hors vocabulaire"),
    ((None,), "hors vocabulaire"),
    ((_Str("stress_test"),), "hors vocabulaire"),
    ((), "au moins une"),
    (("compare", "compare"), "en double"),
    (["retrieve_or_define"], "tuple attendu"),
])
def test_30_unknown_empty_duplicate_or_untyped_operations(operations, match):
    with pytest.raises(InvalidTaskProfileContent, match=match):
        build_posture_baseline(_forged(_task_segment(operations=operations)))


@pytest.mark.parametrize("characteristics, match", [
    (("hard",), "hors vocabulaire"),
    (("easy",), "hors vocabulaire"),
    ((3,), "hors vocabulaire"),
    (("multi_step", "multi_step"), "en double"),
    (["multi_step"], "tuple attendu"),
])
def test_31_unknown_duplicate_or_untyped_characteristics(characteristics, match):
    with pytest.raises(InvalidTaskProfileContent, match=match):
        build_posture_baseline(_forged(_task_segment(characteristics=characteristics)))


@pytest.mark.parametrize("profile, match", [
    (_forged(), "1 à"),                                                       # task_requested sans segment
    (_forged(*([VALID_SEGMENT] * (MAX_TASK_SEGMENTS + 1))), "1 à"),           # trop de segments
    (_forged(VALID_SEGMENT, status=NO_TASK), "aucun segment"),                # no_task avec segment
    (_forged(segments=[VALID_SEGMENT]), "tuple attendu"),                     # segments non tuple
    (_forged(_task_segment(excerpt="")), "extrait non vide"),
    (_forged(_task_segment(excerpt="   ")), "extrait non vide"),
    (_forged(_task_segment(excerpt=None)), "extrait non vide"),
    (_forged(_task_segment(excerpt=_Str("Tâche."))), "extrait non vide"),
    (_forged(_task_segment(operations=("evaluate_existing_reasoning",))), "sans la caractéristique"),
    (_forged(_task_segment(characteristics=("existing_reasoning_to_evaluate",))), "sans opération"),
])
def test_32_incoherent_structure_is_refused(profile, match):
    with pytest.raises(InvalidTaskProfileContent, match=match):
        build_posture_baseline(profile)


@pytest.mark.parametrize("segment", [
    _task_segment(operations=("calculate", "retrieve_or_define")),
    _task_segment(characteristics=("open_ended", "multi_step")),
])
def test_33_non_canonical_order_is_refused_never_reordered(segment):
    with pytest.raises(InvalidTaskProfileContent, match="ordre canonique"):
        build_posture_baseline(_forged(segment))


def test_33_errors_are_never_turned_into_an_explain_fallback():
    for bad in (_task_segment(intent="guided"), _task_segment(operations=("define",))):
        with pytest.raises(PostureBaselineError):
            build_posture_baseline(_forged(bad))


def test_34_no_partial_plan_when_a_later_segment_is_invalid():
    good = _task_segment(intent=DIRECT_ANSWER_REQUESTED)
    for bad in (_task_segment(intent="guided"), None, _task_segment(operations=())):
        with pytest.raises(InvalidTaskProfileContent, match=r"segments\[1\]"):
            build_posture_baseline(_forged(good, bad))


def test_32_corrupted_validated_profile_is_refused():
    profile = _one(UNSPECIFIED)
    object.__setattr__(profile.segments[0], "explicit_intent", "guided")
    with pytest.raises(InvalidTaskProfileContent):
        build_posture_baseline(profile)


def test_forged_but_coherent_profile_is_accepted():
    # Un profil forgé cohérent est structurellement indiscernable d'un
    # profil validé : la compatibilité n'est pas l'authenticité.
    baseline = build_posture_baseline(_forged(_task_segment(intent=JOINT_REASONING_REQUESTED)))
    assert _decision(baseline) == (CO_REASON, EXPLICIT_INTENT_BASIS)


# --------------------------------------------------------------------------
# 35-36. Déterminisme et immutabilité
# --------------------------------------------------------------------------

def test_35_deterministic_and_does_not_mutate_its_input():
    profile = _validated(MULTI_MESSAGE, *(_seg(*segment) for segment in MULTI_SEGMENTS))
    before = repr(profile)
    first = build_posture_baseline(profile)
    second = build_posture_baseline(profile)
    assert first == second and first is not second
    assert repr(profile) == before


def test_35_same_profile_same_plan_whoever_built_it():
    message = "Challenge ma thèse sur LVMH."
    segment = (message, CHALLENGE_REQUESTED, ("evaluate_existing_reasoning",), ("existing_reasoning_to_evaluate",))
    assert build_posture_baseline(_validated(message, _seg(*segment))) == build_posture_baseline(
        _classified(message, segment))


def test_36_output_is_immutable():
    baseline = build_posture_baseline(_one(UNSPECIFIED))
    with pytest.raises(FrozenInstanceError):
        baseline.segments = ()
    with pytest.raises(FrozenInstanceError):
        baseline.segments[0].posture = CHALLENGE
    assert type(baseline.segments) is tuple
    with pytest.raises(FrozenInstanceError):
        replace(baseline).policy_version = "x"


# --------------------------------------------------------------------------
# 37-44. Pureté, isolation, absence de support / score / trace
# --------------------------------------------------------------------------

def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_imports_are_only_dataclasses_and_the_6_2a_contract():
    assert _imports() == {"dataclasses", "core.adaptation_task"}


def test_only_validated_6_2a_contracts_are_imported_never_the_validator_nor_the_classifier():
    imported = {a.name for n in ast.walk(_tree()) if isinstance(n, ast.ImportFrom) for a in n.names
                if n.module == "core.adaptation_task"}
    for name in ("validate_task_profile_proposal", "TaskProfileProposal", "TaskSegmentProposal",
                 "InteractionTaskInput", "check_interaction", "TaskContextTurn"):
        assert name not in imported, name
    assert "adaptation_task_classifier" not in _source()
    tokens = _code_tokens(_source()).split()
    for word in ("classify_interaction_task", "backend", "complete", "TaskClassifierBackend"):
        assert word not in tokens, word


def test_37_42_no_db_clock_env_network_llm_nor_persistence():
    assert not {"os", "sys", "random", "time", "datetime", "uuid", "json", "sqlalchemy", "core.db", "core.models",
                "requests", "httpx", "socket", "anthropic", "openai", "pathlib", "io"} & _imports()
    tokens = _code_tokens(_source()).split()
    for word in ("db", "session", "commit", "flush", "execute", "query", "now", "random", "environ", "getenv",
                 "open", "__tablename__", "Column", "Base", "complete", "backend", "llm", "model", "save",
                 "persist", "cache", "lru_cache", "global"):
        assert word not in tokens, word


def test_37_db_is_never_touched_at_runtime(monkeypatch):
    import core.db as db

    def _forbidden(*args, **kwargs):
        raise AssertionError("accès base interdit en 6-2B")

    for name in dir(db):
        if callable(getattr(db, name)) and not name.startswith("__"):
            monkeypatch.setattr(db, name, _forbidden, raising=False)
    profile = _validated(MULTI_MESSAGE, *(_seg(*segment) for segment in MULTI_SEGMENTS))
    assert len(build_posture_baseline(profile).segments) == len(MULTI_SEGMENTS)


UPSTREAM_6_1 = ("adaptation_state", "adaptation_assumptions", "adaptation_safety", "adaptation_context",
                "adaptation_focus", "adaptation_focus_classifier")
STEP5 = ("inference", "longitudinal", "observation", "cognitive_capture", "taxonomy")


def test_39_no_6_1_import_nor_mention():
    text = _source()
    for module in UPSTREAM_6_1:
        assert module not in text, module
    tokens = _code_tokens(text)
    for name in ("PedagogicalProjection", "PedagogicalResponseContext", "SafeAssumptionPlan", "safe_assumptions",
                 "safe_stages", "MinimalValidationConstraint", "validation_constraints", "validation_need",
                 "InteractionCompetencyFocus", "FocusProposal", "AdaptationStateSnapshot", "AssumptionBaseline",
                 "current_stage", "competency_code"):
        assert name not in tokens, name


def test_40_no_step5_dependency():
    for module in _imports():
        for word in STEP5:
            assert word not in module, module
    tokens = _code_tokens(_source()).split()
    for word in ("claims", "tensions", "inference_run", "state_generation", "user_id", "person_id"):
        assert word not in tokens, word


def test_41_not_wired_to_api_web_chat_nor_orchestration():
    # Étape 6.2C : calibration du support, consomme le baseline validé et la
    # frontière publique build_posture_baseline (préflight : vérifier, jamais
    # recalculer à la place de 6-2B) ; non branché : tests/test_adaptation_support.py.
    step6_consumers = {"core/adaptation_support.py": {
        "CHALLENGE", "CHALLENGE_TASK_OPERATIONS", "CO_REASON", "EXPLAIN", "GUIDE", "POSTURE_BASELINE_SCHEMA_VERSION",
        "POSTURE_POLICY_VERSION", "POSTURES", "SELECTION_BASES", "InteractionPostureBaseline", "PostureBaselineError",
        "SegmentPostureDecision", "UnsupportedTaskProfileVersion", "build_posture_baseline"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_posture" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_posture", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if (rel != "core/adaptation_posture.py" and rel not in step6_consumers
                and "adaptation_posture" in path.read_text(encoding="utf-8", errors="replace")):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("build_posture_baseline", "InteractionPostureBaseline", "SegmentPostureDecision",
                 "POSTURE_POLICY_VERSION"):
        assert name not in api, name
    tokens = _code_tokens(_source())
    for word in ("web_chat", "fastapi", "route", "request_handler"):
        assert word not in tokens, word


def test_42_no_migration_no_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0011_assistant_deliveries.py" and len(versions) == 11
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    for word in ("posture", "Posture", "selection_basis"):
        assert word not in models, word


FORBIDDEN_FIELD_WORDS = ("support", "score", "difficulty", "level", "stage", "mastery", "autonomy", "confidence",
                         "weight", "rank", "user", "person", "trace", "validation", "question", "movement",
                         "assumption", "gap", "projection")


def test_43_44_no_support_trace_score_level_nor_person_field():
    for cls in (SegmentPostureDecision, InteractionPostureBaseline):
        for field in fields(cls):
            for word in FORBIDDEN_FIELD_WORDS:
                assert word not in field.name.lower(), (cls.__name__, field.name)
    tokens = _code_tokens(_source())
    for name in ("actual_support_trace", "support_trace", "support_score", "difficulty_score", "low_support",
                 "medium_support", "high_support", "minimal", "scaffolded", "full_help", "easy", "medium", "hard"):
        assert name not in tokens.split(), name
    # « support » n'apparaît que dans « Unsupported… » / « non supportée » (versions).
    assert re.findall(r"(?<![a-z])support(?!ed|ée)", tokens.lower()) == []


def test_44_no_numeric_weighting_nor_hierarchy_between_postures():
    floats = [n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert floats == []
    ints = {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is int}
    assert ints <= {1}  # seule borne : au moins un segment (jamais une note)
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                                                 for op in node.ops):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not names & {"EXPLAIN", "GUIDE", "CO_REASON", "CHALLENGE", "POSTURES", "posture"}, names


def test_postures_and_bases_are_disjoint_from_6_2a_vocabularies():
    postures = set(POSTURES) | set(SELECTION_BASES)
    assert not postures & (set(EXPLICIT_INTENTS) | set(COGNITIVE_OPERATIONS) | set(TASK_CHARACTERISTICS))


def test_output_carries_no_task_requirement_copy():
    # Sortie minimale : ni opérations, ni caractéristiques, ni intention
    # recopiées depuis le profil.
    names = {f.name for cls in (SegmentPostureDecision, InteractionPostureBaseline) for f in fields(cls)}
    assert not names & {"explicit_intent", "cognitive_operations", "task_characteristics", "task_profile"}
