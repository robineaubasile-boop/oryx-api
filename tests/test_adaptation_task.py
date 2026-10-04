"""Tests de l'étape 6.2A1 : contrats et validateur déterministe du profil de
la demande et de la tâche courantes (core/adaptation_task.py).

Aucune base, aucun réseau, aucun modèle : le validateur est pur. Ces tests
valident la STRUCTURE (types exacts, versions, vocabulaires fermés, doublons,
ancrage verbatim et ordre des extraits, cohérence entre champs, absence de
repli), jamais la vérité sémantique d'une interprétation.
"""
import ast
import enum
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace

import pytest

from core import adaptation_task as at
from core.adaptation_task import (
    CHALLENGE_REQUESTED,
    COGNITIVE_OPERATIONS,
    DIRECT_ANSWER_REQUESTED,
    EXPLICIT_INTENTS,
    JOINT_REASONING_REQUESTED,
    MAX_TASK_CONTEXT_CHARS,
    MAX_TASK_CONTEXT_TURNS,
    MAX_TASK_MESSAGE_CHARS,
    MAX_TASK_SEGMENTS,
    NO_TASK,
    REASONING_RESERVED_FOR_USER,
    REQUEST_STATUSES,
    TASK_CHARACTERISTICS,
    TASK_POLICY_VERSION,
    TASK_REQUESTED,
    TASK_SCHEMA_VERSION,
    UNSPECIFIED,
    InteractionTaskInput,
    InteractionTaskProfile,
    InvalidTaskArgument,
    InvalidTaskInput,
    InvalidTaskProfileProposal,
    TaskContextTurn,
    TaskError,
    TaskProfileProposal,
    TaskSegment,
    TaskSegmentProposal,
    validate_task_profile_proposal,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_task.py"


# --------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------

def _turn(role, content):
    return TaskContextTurn(role=role, content=content)


def _input(message="Qu'est-ce que le free cash-flow ?", turns=()):
    return InteractionTaskInput(current_message=message, context_turns=tuple(turns))


def _seg(excerpt, intent=UNSPECIFIED, operations=("retrieve_or_define",), characteristics=()):
    return TaskSegmentProposal(source_excerpt=excerpt, explicit_intent=intent,
                               cognitive_operations=tuple(operations),
                               task_characteristics=tuple(characteristics))


def _proposal(*segments, status=TASK_REQUESTED, **overrides):
    values = {"schema_version": TASK_SCHEMA_VERSION, "policy_version": TASK_POLICY_VERSION,
              "request_status": status, "segments": tuple(segments)}
    values.update(overrides)
    return TaskProfileProposal(**values)


def _validate(message, *segments, status=TASK_REQUESTED, turns=(), **overrides):
    return validate_task_profile_proposal(_proposal(*segments, status=status, **overrides), _input(message, turns))


def _expected(*segments, status=TASK_REQUESTED):
    return InteractionTaskProfile(
        schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION, request_status=status,
        segments=tuple(TaskSegment(source_excerpt=e, explicit_intent=i, cognitive_operations=tuple(o),
                                   task_characteristics=tuple(c)) for e, i, o, c in segments))


# --------------------------------------------------------------------------
# 0. Contrats publics, versions, vocabulaires
# --------------------------------------------------------------------------

def test_frozen_versions_and_limits():
    assert TASK_SCHEMA_VERSION == "interaction-task-profile-v1"
    assert TASK_POLICY_VERSION == "task-policy-1"
    assert (MAX_TASK_CONTEXT_TURNS, MAX_TASK_MESSAGE_CHARS, MAX_TASK_CONTEXT_CHARS, MAX_TASK_SEGMENTS) == (
        8, 6000, 12000, 6)


def test_closed_vocabularies_are_frozen():
    assert REQUEST_STATUSES == ("task_requested", "no_task")
    assert EXPLICIT_INTENTS == ("direct_answer_requested", "reasoning_reserved_for_user",
                                "joint_reasoning_requested", "challenge_requested", "unspecified")
    assert COGNITIVE_OPERATIONS == (
        "retrieve_or_define", "explain_mechanism", "apply_procedure", "calculate", "select_relevant_information",
        "generate_hypotheses", "interpret_evidence", "compare", "construct_reasoning", "form_conclusion",
        "evaluate_existing_reasoning", "stress_test", "falsify", "synthesize")
    assert TASK_CHARACTERISTICS == ("multi_step", "domain_specialized", "open_ended",
                                    "user_specific_application", "existing_reasoning_to_evaluate")
    for vocabulary in (REQUEST_STATUSES, EXPLICIT_INTENTS, COGNITIVE_OPERATIONS, TASK_CHARACTERISTICS):
        assert type(vocabulary) is tuple and len(set(vocabulary)) == len(vocabulary)
        assert all(type(value) is str for value in vocabulary)


def test_public_api():
    for name in ("InteractionTaskInput", "TaskContextTurn", "TaskProfileProposal", "TaskSegmentProposal",
                 "InteractionTaskProfile", "TaskSegment", "validate_task_profile_proposal", "check_interaction"):
        assert hasattr(at, name), name


def test_errors_are_a_small_explicit_hierarchy_under_task_error():
    for error in (InvalidTaskArgument, InvalidTaskInput, InvalidTaskProfileProposal):
        assert issubclass(error, TaskError)
    assert issubclass(TaskError, Exception)


def test_dataclasses_are_frozen_kw_only_with_exact_fields():
    expected = {
        TaskContextTurn: ["role", "content"],
        InteractionTaskInput: ["current_message", "context_turns"],
        TaskSegmentProposal: ["source_excerpt", "explicit_intent", "cognitive_operations", "task_characteristics"],
        TaskProfileProposal: ["schema_version", "policy_version", "request_status", "segments"],
        TaskSegment: ["source_excerpt", "explicit_intent", "cognitive_operations", "task_characteristics"],
        InteractionTaskProfile: ["schema_version", "policy_version", "request_status", "segments"],
    }
    for cls, names in expected.items():
        assert is_dataclass(cls) and cls.__dataclass_params__.frozen, cls
        assert [f.name for f in fields(cls)] == names, cls
        assert all(f.kw_only for f in fields(cls)), cls
    profile = _validate("Qu'est-ce que le free cash-flow ?", _seg("Qu'est-ce que le free cash-flow ?"))
    with pytest.raises(FrozenInstanceError):
        profile.segments = ()
    with pytest.raises(FrozenInstanceError):
        profile.segments[0].explicit_intent = CHALLENGE_REQUESTED


# --------------------------------------------------------------------------
# 1. Entrée : message courant, contexte borné, jamais tronqué
# --------------------------------------------------------------------------

def test_valid_input_is_kept_exactly():
    turns = (_turn("user", "Challenge ma thèse."), _turn("assistant", "Sur quoi repose-t-elle ?"))
    interaction = _input("  Et le BFR ?  ", turns)
    assert interaction.current_message == "  Et le BFR ?  "
    assert interaction.context_turns == turns


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_empty_current_message(message):
    with pytest.raises(InvalidTaskInput):
        _input(message)


class _Str(str):
    pass


@pytest.mark.parametrize("message", [None, 3, b"bytes", _Str("x")])
def test_current_message_of_the_wrong_type(message):
    with pytest.raises(InvalidTaskInput):
        _input(message)


def test_message_over_the_limit_is_refused_never_truncated():
    _input("x" * MAX_TASK_MESSAGE_CHARS)
    with pytest.raises(InvalidTaskInput, match="jamais tronqué"):
        _input("x" * (MAX_TASK_MESSAGE_CHARS + 1))


def test_context_turns_must_be_an_exact_tuple():
    with pytest.raises(InvalidTaskInput):
        InteractionTaskInput(current_message="ok", context_turns=[_turn("user", "a")])


def test_more_than_8_turns_is_refused():
    _input("ok", [_turn("user", "a")] * MAX_TASK_CONTEXT_TURNS)
    with pytest.raises(InvalidTaskInput, match="jamais tronqué"):
        _input("ok", [_turn("user", "a")] * (MAX_TASK_CONTEXT_TURNS + 1))


@pytest.mark.parametrize("role", ["system", "tool", "User", "", None])
def test_other_roles_are_refused(role):
    with pytest.raises(InvalidTaskInput):
        _turn(role, "a")


@pytest.mark.parametrize("content", ["", "  ", None, 1])
def test_empty_or_non_text_content_is_refused(content):
    with pytest.raises(InvalidTaskInput):
        _turn("user", content)


def test_context_over_12000_chars_is_refused_never_truncated():
    half = MAX_TASK_CONTEXT_CHARS // 2
    _input("ok", [_turn("user", "x" * half), _turn("assistant", "y" * half)])
    with pytest.raises(InvalidTaskInput, match="jamais tronqué"):
        _input("ok", [_turn("user", "x" * half), _turn("assistant", "y" * (half + 1))])


def test_text_must_be_utf8_encodable():
    with pytest.raises(InvalidTaskInput):
        _input("\ud800")


def test_foreign_turn_object_is_refused():
    class Turn:
        role, content = "user", "a"
    with pytest.raises(InvalidTaskInput):
        InteractionTaskInput(current_message="ok", context_turns=(Turn(),))


def test_replace_revalidates():
    interaction = _input()
    with pytest.raises(InvalidTaskInput):
        replace(interaction, current_message="")


# --------------------------------------------------------------------------
# A. Intention explicite : conservée telle quelle, jamais inventée
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message, intent", [
    ("Donne-moi juste l'explication du PER.", DIRECT_ANSWER_REQUESTED),
    ("Aide-moi à calculer la marge, mais ne me donne pas la réponse.", REASONING_RESERVED_FOR_USER),
    ("Analyse avec moi cette entreprise.", JOINT_REASONING_REQUESTED),
    ("Challenge ma thèse sur LVMH.", CHALLENGE_REQUESTED),
    ("Qu'est-ce que le free cash-flow ?", UNSPECIFIED),
])
def test_A_each_explicit_intent_is_kept(message, intent):
    profile = _validate(message, _seg(message, intent))
    assert profile.segments[0].explicit_intent == intent


def test_A_unspecified_is_never_converted_into_a_direct_answer():
    message = "Qu'est-ce que le free cash-flow ?"
    profile = _validate(message, _seg(message, UNSPECIFIED, ("retrieve_or_define",), ("domain_specialized",)))
    assert profile == _expected((message, UNSPECIFIED, ("retrieve_or_define",), ("domain_specialized",)))
    assert profile.segments[0].explicit_intent != DIRECT_ANSWER_REQUESTED


def test_A_unspecified_and_direct_answer_are_distinct_values():
    assert UNSPECIFIED != DIRECT_ANSWER_REQUESTED
    assert at.UNSPECIFIED in EXPLICIT_INTENTS and at.DIRECT_ANSWER_REQUESTED in EXPLICIT_INTENTS


# --------------------------------------------------------------------------
# B. Priorité du message courant (ancrage dans current_message seulement)
# --------------------------------------------------------------------------

def test_B_excerpt_must_come_from_the_current_message_not_the_context():
    turns = (_turn("user", "Challenge ma thèse sur LVMH."),)
    message = "Non, finalement explique-moi juste le concept."
    profile = _validate(message, _seg(message, DIRECT_ANSWER_REQUESTED, ("explain_mechanism",)), turns=turns)
    assert profile.segments[0].explicit_intent == DIRECT_ANSWER_REQUESTED
    with pytest.raises(InvalidTaskProfileProposal, match="absent du message courant"):
        _validate(message, _seg("Challenge ma thèse sur LVMH.", CHALLENGE_REQUESTED,
                                ("evaluate_existing_reasoning",), ("existing_reasoning_to_evaluate",)), turns=turns)


# --------------------------------------------------------------------------
# C. Segmentation : un ou plusieurs segments ordonnés, ancrés verbatim
# --------------------------------------------------------------------------

def test_C_single_request_single_segment():
    message = "Calcule le ROE de cette entreprise et dis-moi s'il est bon."
    profile = _validate(message, _seg(message, UNSPECIFIED, ("form_conclusion", "calculate", "interpret_evidence"),
                                      ("user_specific_application", "multi_step")))
    assert len(profile.segments) == 1
    assert profile.segments[0].cognitive_operations == ("calculate", "interpret_evidence", "form_conclusion")
    assert profile.segments[0].task_characteristics == ("multi_step", "user_specific_application")


@pytest.mark.parametrize("message, segments", [
    ("Explique-moi le ROIC et ensuite aide-moi à l'appliquer à cette entreprise.", (
        ("Explique-moi le ROIC", UNSPECIFIED, ("explain_mechanism",), ("domain_specialized",)),
        ("aide-moi à l'appliquer à cette entreprise", UNSPECIFIED, ("apply_procedure", "calculate"),
         ("domain_specialized", "user_specific_application")))),
    ("Analyse avec moi cette entreprise puis challenge ma conclusion.", (
        ("Analyse avec moi cette entreprise", JOINT_REASONING_REQUESTED,
         ("select_relevant_information", "interpret_evidence", "construct_reasoning", "form_conclusion"),
         ("multi_step", "open_ended", "user_specific_application")),
        ("challenge ma conclusion", CHALLENGE_REQUESTED, ("evaluate_existing_reasoning", "stress_test"),
         ("existing_reasoning_to_evaluate",)))),
    ("Donne-moi la définition du moat, mais pour la deuxième partie ne me donne pas la réponse.", (
        ("Donne-moi la définition du moat", DIRECT_ANSWER_REQUESTED, ("retrieve_or_define",), ()),
        ("pour la deuxième partie ne me donne pas la réponse", REASONING_RESERVED_FOR_USER,
         ("construct_reasoning", "form_conclusion"), ("open_ended",)))),
])
def test_C_multi_requests_keep_plural_ordered_segments_with_their_own_intents(message, segments):
    profile = _validate(message, *(_seg(*segment) for segment in segments))
    assert profile == _expected(*segments)
    assert [s.source_excerpt for s in profile.segments] == [segment[0] for segment in segments]


def test_C_segments_out_of_the_order_of_the_request_are_refused():
    message = "Explique-moi le ROIC puis challenge mon raisonnement."
    first = _seg("Explique-moi le ROIC", UNSPECIFIED, ("explain_mechanism",))
    second = _seg("challenge mon raisonnement", CHALLENGE_REQUESTED, ("evaluate_existing_reasoning",),
                  ("existing_reasoning_to_evaluate",))
    _validate(message, first, second)
    with pytest.raises(InvalidTaskProfileProposal, match="hors de l'ordre"):
        _validate(message, second, first)


def test_C_overlapping_excerpts_are_refused():
    message = "Explique-moi le ROIC puis le ROE."
    with pytest.raises(InvalidTaskProfileProposal, match="chevauchant"):
        _validate(message, _seg("Explique-moi le ROIC"), _seg("le ROIC puis le ROE"))


def test_C_repeated_text_is_anchored_in_order():
    message = "Explique-moi le ROIC. Puis, plus tard : Explique-moi le ROIC."
    profile = _validate(message, _seg("Explique-moi le ROIC"), _seg("Explique-moi le ROIC"))
    assert len(profile.segments) == 2
    with pytest.raises(InvalidTaskProfileProposal):
        _validate(message, _seg("Explique-moi le ROIC"), _seg("Explique-moi le ROIC"), _seg("Explique-moi le ROIC"))


@pytest.mark.parametrize("excerpt", ["explique-moi le roic", "Explique moi le ROIC", "Explique-moi  le ROIC",
                                     "Explique-moi le ROIC, s'il te plaît"])
def test_C_paraphrased_excerpt_is_refused_never_corrected(excerpt):
    with pytest.raises(InvalidTaskProfileProposal, match="absent du message courant"):
        _validate("Explique-moi le ROIC.", _seg(excerpt))


def test_C_max_segments():
    message = " ; ".join(f"partie {i}" for i in range(MAX_TASK_SEGMENTS + 1))
    excerpts = [f"partie {i}" for i in range(MAX_TASK_SEGMENTS + 1)]
    _validate(message, *(_seg(e) for e in excerpts[:MAX_TASK_SEGMENTS]))
    with pytest.raises(InvalidTaskProfileProposal, match=f"1 à {MAX_TASK_SEGMENTS}"):
        _validate(message, *(_seg(e) for e in excerpts))


def test_C_task_requested_without_segment_is_refused():
    with pytest.raises(InvalidTaskProfileProposal, match="1 à"):
        _validate("Qu'est-ce que le PER ?")


def test_C_no_task_is_an_explicit_status_with_no_segment():
    profile = _validate("Bonjour, merci !", status=NO_TASK)
    assert profile == _expected(status=NO_TASK)
    with pytest.raises(InvalidTaskProfileProposal, match="aucun segment"):
        _validate("Bonjour, merci !", _seg("Bonjour"), status=NO_TASK)


# --------------------------------------------------------------------------
# D. Nature de la tâche : vocabulaire fermé, qualitatif, ordre canonique
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message, operations, characteristics", [
    ("C'est quoi le PER ?", ("retrieve_or_define",), ()),
    ("Pourquoi le BFR consomme du cash quand les ventes montent ?", ("explain_mechanism",), ("domain_specialized",)),
    ("Calcule la marge nette avec ces chiffres.", ("apply_procedure", "calculate"),
     ("user_specific_application",)),
    ("Analyse avec moi une banque à partir de ses états financiers.",
     ("select_relevant_information", "interpret_evidence", "construct_reasoning", "form_conclusion"),
     ("multi_step", "domain_specialized", "user_specific_application")),
    ("Aide-moi à construire une thèse sur Air Liquide.", ("generate_hypotheses", "construct_reasoning",
                                                         "form_conclusion"),
     ("multi_step", "domain_specialized", "open_ended", "user_specific_application")),
    ("Mon raisonnement sur Apple tient-il ?", ("evaluate_existing_reasoning",),
     ("user_specific_application", "existing_reasoning_to_evaluate")),
    ("Que deviendrait ma thèse si les taux doublaient ? Qu'est-ce qui la réfuterait ?",
     ("stress_test", "falsify"), ("open_ended", "existing_reasoning_to_evaluate")),
    ("Compare le ROE et le ROIC puis fais-moi une synthèse.", ("compare", "synthesize"), ("domain_specialized",)),
])
def test_D_task_natures_are_kept_in_canonical_vocabulary_order(message, operations, characteristics):
    profile = _validate(message, _seg(message, UNSPECIFIED, tuple(reversed(operations)),
                                      tuple(reversed(characteristics))))
    assert profile.segments[0].cognitive_operations == operations
    assert profile.segments[0].task_characteristics == characteristics


def test_D_every_operation_and_characteristic_is_accepted():
    message = "Tâche complète."
    for operation in COGNITIVE_OPERATIONS:
        characteristics = ("existing_reasoning_to_evaluate",) if operation == "evaluate_existing_reasoning" else ()
        _validate(message, _seg(message, UNSPECIFIED, (operation,), characteristics))
    _validate(message, _seg(message, UNSPECIFIED, COGNITIVE_OPERATIONS, TASK_CHARACTERISTICS))


def test_D_existing_reasoning_requires_an_evaluation_operation():
    message = "Ma thèse."
    with pytest.raises(InvalidTaskProfileProposal, match="existing_reasoning_to_evaluate sans opération"):
        _validate(message, _seg(message, CHALLENGE_REQUESTED, ("construct_reasoning",),
                                ("existing_reasoning_to_evaluate",)))
    for operation in ("stress_test", "falsify"):
        _validate(message, _seg(message, UNSPECIFIED, (operation,), ("existing_reasoning_to_evaluate",)))
        _validate(message, _seg(message, UNSPECIFIED, (operation,), ()))


def test_D_evaluate_existing_reasoning_requires_the_characteristic():
    with pytest.raises(InvalidTaskProfileProposal, match="sans la caractéristique"):
        _validate("Ma thèse.", _seg("Ma thèse.", CHALLENGE_REQUESTED, ("evaluate_existing_reasoning",)))


def test_D_intent_and_task_nature_are_independent_axes():
    message = "Challenge-moi."
    for intent in EXPLICIT_INTENTS:
        _validate(message, _seg(message, intent, ("retrieve_or_define",)))


# --------------------------------------------------------------------------
# G. Proposition NON FIABLE : toute violation est une erreur, jamais un repli
# --------------------------------------------------------------------------

class _Enum(str, enum.Enum):
    UNSPECIFIED = "unspecified"


MESSAGE = "Qu'est-ce que le PER ?"


@pytest.mark.parametrize("overrides, match", [
    ({"schema_version": "interaction-task-profile-v2"}, "schema_version"),
    ({"schema_version": None}, "schema_version"),
    ({"policy_version": "task-policy-2"}, "policy_version"),
    ({"policy_version": "focus-policy-1"}, "policy_version"),
    ({"request_status": "ambiguous"}, "request_status"),
    ({"request_status": "neutral"}, "request_status"),
    ({"request_status": "TASK_REQUESTED"}, "request_status"),
])
def test_G_versions_and_status_are_exact(overrides, match):
    with pytest.raises(InvalidTaskProfileProposal, match=match):
        _validate(MESSAGE, _seg(MESSAGE), **overrides)


def test_G_segments_must_be_a_tuple():
    with pytest.raises(InvalidTaskProfileProposal, match="segments : tuple"):
        validate_task_profile_proposal(
            TaskProfileProposal(schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION,
                                request_status=TASK_REQUESTED, segments=[_seg(MESSAGE)]), _input(MESSAGE))


@pytest.mark.parametrize("segment", [
    None, {"source_excerpt": MESSAGE}, TaskSegment(source_excerpt=MESSAGE, explicit_intent=UNSPECIFIED,
                                                   cognitive_operations=("retrieve_or_define",),
                                                   task_characteristics=())])
def test_G_segment_of_the_wrong_type(segment):
    with pytest.raises(InvalidTaskProfileProposal, match="TaskSegmentProposal attendu"):
        _validate(MESSAGE, segment)


@pytest.mark.parametrize("excerpt", ["", "   ", None, 1, _Str("Qu'est-ce")])
def test_G_empty_or_non_text_excerpt(excerpt):
    with pytest.raises(InvalidTaskProfileProposal, match="extrait non vide"):
        _validate(MESSAGE, _seg(excerpt))


@pytest.mark.parametrize("intent", ["guided", "expliquer", "challenger", "posture", "", None, "Unspecified",
                                    _Enum.UNSPECIFIED])
def test_G_unknown_intent(intent):
    with pytest.raises(InvalidTaskProfileProposal, match="explicit_intent"):
        _validate(MESSAGE, _seg(MESSAGE, intent))


@pytest.mark.parametrize("operations, match", [
    ((), "au moins une"),
    (("define",), "hors vocabulaire"),
    (("guider",), "hors vocabulaire"),
    (("retrieve_or_define", "retrieve_or_define"), "en double"),
    (("retrieve_or_define", None), "hors vocabulaire"),
])
def test_G_unknown_empty_or_duplicate_operation(operations, match):
    with pytest.raises(InvalidTaskProfileProposal, match=match):
        _validate(MESSAGE, _seg(MESSAGE, UNSPECIFIED, operations))


@pytest.mark.parametrize("characteristics, match", [
    (("hard",), "hors vocabulaire"),
    (("easy",), "hors vocabulaire"),
    (("multi_step", "multi_step"), "en double"),
    ((3,), "hors vocabulaire"),
])
def test_G_unknown_or_duplicate_characteristic(characteristics, match):
    with pytest.raises(InvalidTaskProfileProposal, match=match):
        _validate(MESSAGE, _seg(MESSAGE, UNSPECIFIED, ("retrieve_or_define",), characteristics))


@pytest.mark.parametrize("field", ["cognitive_operations", "task_characteristics"])
def test_G_lists_are_not_tuples(field):
    segment = replace(_seg(MESSAGE), **{field: ["retrieve_or_define"]})
    with pytest.raises(InvalidTaskProfileProposal, match="tuple attendu"):
        _validate(MESSAGE, segment)


@pytest.mark.parametrize("proposal", [None, {}, "proposal", InteractionTaskProfile(
    schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION, request_status=NO_TASK, segments=())])
def test_G_non_proposal_argument(proposal):
    with pytest.raises(InvalidTaskArgument):
        validate_task_profile_proposal(proposal, _input())


@pytest.mark.parametrize("interaction", [None, MESSAGE, {"current_message": MESSAGE, "context_turns": ()}])
def test_G_non_interaction_argument(interaction):
    with pytest.raises(InvalidTaskArgument):
        validate_task_profile_proposal(_proposal(_seg(MESSAGE)), interaction)


def test_G_interaction_is_revalidated_at_the_boundary():
    interaction = _input()
    object.__setattr__(interaction, "current_message", "")
    with pytest.raises(InvalidTaskInput):
        validate_task_profile_proposal(_proposal(status=NO_TASK), interaction)


def test_G_errors_are_never_turned_into_unspecified_nor_a_generic_segment():
    bad = _seg(MESSAGE, "guided")
    with pytest.raises(InvalidTaskProfileProposal):
        _validate(MESSAGE, bad)
    with pytest.raises(InvalidTaskProfileProposal):
        _validate(MESSAGE, _seg(MESSAGE), bad)


# --------------------------------------------------------------------------
# E / F. Aucun profil utilisateur, aucune posture
# --------------------------------------------------------------------------

PUBLIC_DATACLASSES = (TaskContextTurn, InteractionTaskInput, TaskSegmentProposal, TaskProfileProposal,
                      TaskSegment, InteractionTaskProfile)
PERSON_WORDS = ("stage", "level", "mastery", "confidence", "tension", "autonomy", "personality", "score",
                "user_id", "ambition", "difficulty", "preference")
POSTURE_WORDS = ("posture", "expliquer", "guider", "co_raisonner", "challenger", "explain", "guide", "co_reason",
                 "challenge", "support", "movement")


def test_E_no_user_profile_field():
    for cls in PUBLIC_DATACLASSES:
        for field in fields(cls):
            for word in PERSON_WORDS:
                assert word not in field.name.lower(), (cls.__name__, field.name)


def test_F_no_posture_field_nor_posture_value():
    for cls in PUBLIC_DATACLASSES:
        for field in fields(cls):
            for word in POSTURE_WORDS:
                assert word not in field.name.lower(), (cls.__name__, field.name)
    postures = {"expliquer", "guider", "co_raisonner", "co-raisonner", "challenger", "explain", "guide",
                "co_reason", "challenge"}
    for vocabulary in (REQUEST_STATUSES, EXPLICIT_INTENTS, COGNITIVE_OPERATIONS, TASK_CHARACTERISTICS):
        assert not postures & set(vocabulary)


def test_E_no_difficulty_score_nor_level_vocabulary():
    values = set(COGNITIVE_OPERATIONS) | set(TASK_CHARACTERISTICS)
    assert not {"easy", "medium", "hard", "simple", "complex", "difficulty"} & values
    floats = [n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert floats == []


# --------------------------------------------------------------------------
# I. Pureté, déterminisme, éphémérité
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


def test_I_imports_are_only_the_stdlib_dataclasses():
    assert _imports() == {"dataclasses"}


def test_I_validator_is_deterministic_and_does_not_mutate_its_inputs():
    message = "Explique-moi le ROIC puis challenge ma conclusion."
    proposal = _proposal(_seg("Explique-moi le ROIC", UNSPECIFIED, ("explain_mechanism", "retrieve_or_define")),
                         _seg("challenge ma conclusion", CHALLENGE_REQUESTED,
                              ("stress_test", "evaluate_existing_reasoning"), ("existing_reasoning_to_evaluate",)))
    interaction = _input(message, (_turn("user", "Parlons de Microsoft."),))
    before = (repr(proposal), repr(interaction))
    first = validate_task_profile_proposal(proposal, interaction)
    second = validate_task_profile_proposal(proposal, interaction)
    assert first == second and first is not second
    assert (repr(proposal), repr(interaction)) == before


def test_I_no_db_clock_random_network_nor_persistence():
    assert not {"os", "random", "time", "datetime", "uuid", "sqlalchemy", "core.db", "core.models",
                "requests", "httpx", "socket", "anthropic", "json"} & _imports()
    tokens = _code_tokens(_source()).split()
    for word in ("db", "session", "commit", "flush", "execute", "now", "random", "environ", "__tablename__",
                 "Column", "Base"):
        assert word not in tokens, word


# --------------------------------------------------------------------------
# 19. Isolation architecturale : rien de 6.1 ni de Step 5, aucun runtime
# --------------------------------------------------------------------------

FORBIDDEN_UPSTREAM = ("adaptation_state", "adaptation_assumptions", "adaptation_safety", "adaptation_context",
                      "adaptation_focus", "inference", "longitudinal", "observation", "cognitive_capture")


def test_19_no_6_1_nor_step5_import():
    for module in _imports():
        for word in FORBIDDEN_UPSTREAM:
            assert word not in module, module


def test_19_no_6_1_nor_step5_object_in_the_code():
    tokens = _code_tokens(_source())
    for name in ("PedagogicalProjection", "PedagogicalResponseContext", "SafeAssumptionPlan", "safe_stages",
                 "validation_constraints", "validation_need", "InteractionCompetencyFocus", "FocusProposal",
                 "AdaptationStateSnapshot", "current_stage", "actual_support_trace", "reasoning_opportunity"):
        assert name not in tokens, name


def test_19_not_wired_to_api_web_nor_orchestration():
    # Étape 6.2B : posture de base, consomme le profil validé seulement
    # (contrats de sortie et vocabulaires, jamais le validateur ni le
    # classificateur) ; non branché : tests/test_adaptation_posture.py.
    step6_consumers = {"core/adaptation_posture.py": {
        "CHALLENGE_REQUESTED", "COGNITIVE_OPERATIONS", "DIRECT_ANSWER_REQUESTED", "EVALUATE_EXISTING_REASONING",
        "EXISTING_REASONING_TO_EVALUATE", "EXPLICIT_INTENTS", "FALSIFY", "JOINT_REASONING_REQUESTED",
        "MAX_TASK_SEGMENTS", "NO_TASK", "REASONING_RESERVED_FOR_USER", "REQUEST_STATUSES", "STRESS_TEST",
        "TASK_CHARACTERISTICS", "TASK_POLICY_VERSION", "TASK_SCHEMA_VERSION", "UNSPECIFIED",
        "InteractionTaskProfile", "TaskSegment"},
        # Étape 6.2C : calibration du support, profil validé revérifié via
        # 6-2B (contrats de sortie et vocabulaires seulement) ; non branché :
        # tests/test_adaptation_support.py.
        "core/adaptation_support.py": {
        "COGNITIVE_OPERATIONS", "NO_TASK", "REQUEST_STATUSES", "TASK_POLICY_VERSION", "TASK_SCHEMA_VERSION",
        "InteractionTaskProfile"},
        # Étape 6.2C2 : classificateur du support, bornes publiques de 6-2A
        # réutilisées pour sa propre garde d'entrée (aucun autre contrat) ;
        # non branché : tests/test_adaptation_support_classifier.py.
        "core/adaptation_support_classifier.py": {"MAX_TASK_MESSAGE_CHARS", "MAX_TASK_SEGMENTS"},
        # Étape 6.3B : classificateur du mouvement, mêmes bornes publiques de
        # 6-2A pour sa propre garde d'entrée (aucun autre contrat) ; non
        # branché : tests/test_adaptation_movement_classifier.py.
        "core/adaptation_movement_classifier.py": {"MAX_TASK_MESSAGE_CHARS", "MAX_TASK_SEGMENTS"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_task" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_task", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel in ("core/adaptation_task.py", "core/adaptation_task_classifier.py") or rel in step6_consumers:
            continue
        if "adaptation_task" in path.read_text(encoding="utf-8", errors="replace"):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("validate_task_profile_proposal", "InteractionTaskProfile", "InteractionTaskInput",
                 "TaskProfileProposal", "TASK_SCHEMA_VERSION"):
        assert name not in api, name


def test_19_no_migration_no_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0010_r1b_event_idempotence.py" and len(versions) == 10
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    for word in ("TaskProfile", "task_profile", "explicit_intent", "cognitive_operations", "task_characteristics"):
        assert word not in models, word
