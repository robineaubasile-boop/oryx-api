"""Étape 6.2B : POSTURE DE BASE de l'interaction, segment par segment
(InteractionPostureBaseline).

Question de 6-2B, et seulement elle : « pour chaque segment de la demande,
quelle posture pédagogique est appropriée compte tenu de l'intention
explicite de l'utilisateur et de la nature de la tâche ? ». 6-2B consomme
UNIQUEMENT le contrat validé de 6-2A :

    6-2A InteractionTaskProfile (validé)
        -> build_posture_baseline -> InteractionPostureBaseline

Frontière constitutionnelle. 6-2A décrit ce que l'utilisateur demande de
faire ; 6-2B décrit comment Oryx se positionne pour CHAQUE segment. La
posture est locale au segment courant : jamais un niveau, un stade ni un
trait attribué à la personne. 6-2B ne lit ni Step 5, ni aucune donnée de
6.1 (focus, présupposés sûrs, contraintes de validation, contexte ou
projection pédagogique), ni base ; il n'appelle ni modèle ni classificateur
(l'orchestration future enchaîne 6-2A -> 6-2B) ; il ne calcule aucun écart
tâche / présupposés, aucune quantité de support, aucun score ; il n'active
aucune validation, ne choisit aucun mouvement 6.3, ne génère ni réponse, ni
question, ni contenu de challenge, et ne produit aucune trace de support
réel. Le croisement tâche <-> présupposés sûrs et la calibration du support
appartiennent à la sous-étape suivante de 6.2. « Décrire, jamais
prescrire. »

Invariants :

- Versions explicites : POSTURE_BASELINE_SCHEMA_VERSION décrit le contrat de
  sortie ; POSTURE_POLICY_VERSION décrit les vocabulaires fermés (postures,
  bases de sélection) et les règles de sélection. Elles sont distinctes des
  versions de 6-2A, que la sortie conserve comme versions SOURCE
  (source_task_schema_version, source_task_policy_version).

- Quatre postures, aucune hiérarchie : explain, guide, co_reason,
  challenge. Aucune n'est supérieure à une autre, aucune n'est un stade
  pédagogique ; elles ne sont jamais comparées, ordonnées ni pondérées.

- UNE posture dominante par segment : la combinaison se fait à l'échelle de
  la réponse, par plusieurs segments ; jamais de mélange pondéré sur un
  même segment.

- Règles de sélection (POSTURE_POLICY_VERSION), dans cet ordre :
    1. intention EXPLICITE (prioritaire sur toute autre règle) :
         direct_answer_requested     -> explain
         reasoning_reserved_for_user -> guide
         joint_reasoning_requested   -> co_reason
         challenge_requested         -> challenge
       base : explicit_intent. « Répondre est prioritaire sur tester » :
       une opération évaluative ne transforme jamais une demande explicite
       de réponse directe en challenge ou en guidage.
    2. intention « unspecified » ET le segment exige réellement d'éprouver
       un raisonnement (au moins une opération parmi
       CHALLENGE_TASK_OPERATIONS : evaluate_existing_reasoning,
       stress_test, falsify) -> challenge ; base : task_operation.
    3. sinon, intention « unspecified » -> explain ; base : direct_fallback.
       « unspecified » est l'ABSENCE d'intention explicite : ni débutant, ni
       passif, ni besoin de guidage, ni permission de tester. Sans
       intention particulière, Oryx répond directement, clairement et
       utilement. compare, synthesize, construct_reasoning,
       interpret_evidence, open_ended ou multi_step ne déclenchent jamais un
       challenge.

- Challenge : il porte sur des hypothèses, des mécanismes, des données, des
  conditions de validité ou un raisonnement existant, jamais sur la
  personne. 6-2B choisit la posture seulement : il n'invente ni
  contradiction, ni hypothèse alternative, ni scénario, ni correction, ni
  falsification, et ne prétend jamais qu'un raisonnement est faux.

- no_task : aucune posture (segments ()), jamais un explain artificiel.

- Ordre et ancrage conservés : une décision par segment source, dans
  l'ordre exact de 6-2A (segment_index = position du segment), même
  source_excerpt ; aucun segment réordonné, fusionné, supprimé ni fabriqué.
  La sortie reste minimale : ni opérations, ni caractéristiques recopiées.

- Fail-closed : le profil reçu est revérifié (type exact, versions
  supportées, statut, nombre de segments, types, vocabulaires fermés de
  6-2A, ordre canonique, cohérence existing_reasoning_to_evaluate <->
  opérations d'évaluation). Toute violation => PostureBaselineError ; rien
  n'est corrigé, aucune valeur n'est remplacée par « unspecified », aucun
  plan partiel, jamais un repli explain. Le repli direct est une décision
  pédagogique pour un « unspecified » VALIDE, jamais une récupération
  d'erreur. Compatibilité != authenticité : sans le message courant,
  l'ancrage verbatim des extraits ne peut pas être revérifié ici ; il
  relève de validate_task_profile_proposal et de l'orchestration.

- PUR et sans valeur probante : ni base, ni horloge, ni environnement, ni
  hasard, ni réseau, ni modèle, ni persistance ; même profil => même plan.
  La posture prévue n'est pas le support réellement fourni : explain ne
  prouve aucune faiblesse, guide aucune autonomie, challenge aucune
  maîtrise. selection_basis est une provenance de décision 6-2B, jamais une
  preuve pédagogique, jamais transmise à Step 5.
"""
from dataclasses import dataclass

from core.adaptation_task import (
    CHALLENGE_REQUESTED,
    COGNITIVE_OPERATIONS,
    DIRECT_ANSWER_REQUESTED,
    EVALUATE_EXISTING_REASONING,
    EXISTING_REASONING_TO_EVALUATE,
    EXPLICIT_INTENTS,
    FALSIFY,
    JOINT_REASONING_REQUESTED,
    MAX_TASK_SEGMENTS,
    NO_TASK,
    REASONING_RESERVED_FOR_USER,
    REQUEST_STATUSES,
    STRESS_TEST,
    TASK_CHARACTERISTICS,
    TASK_POLICY_VERSION,
    TASK_SCHEMA_VERSION,
    UNSPECIFIED,
    InteractionTaskProfile,
    TaskSegment,
)

POSTURE_BASELINE_SCHEMA_VERSION = "interaction-posture-baseline-v1"
POSTURE_POLICY_VERSION = "posture-policy-1"

# SegmentPostureDecision.posture : vocabulaire fermé, SANS hiérarchie.
EXPLAIN = "explain"
GUIDE = "guide"
CO_REASON = "co_reason"
CHALLENGE = "challenge"
POSTURES = (EXPLAIN, GUIDE, CO_REASON, CHALLENGE)

# SegmentPostureDecision.selection_basis : provenance de la décision 6-2B.
EXPLICIT_INTENT_BASIS = "explicit_intent"
TASK_OPERATION_BASIS = "task_operation"
DIRECT_FALLBACK_BASIS = "direct_fallback"
SELECTION_BASES = (EXPLICIT_INTENT_BASIS, TASK_OPERATION_BASIS, DIRECT_FALLBACK_BASIS)

# Opérations qui consistent réellement à éprouver un raisonnement : seules
# elles justifient un challenge en l'absence d'intention explicite.
CHALLENGE_TASK_OPERATIONS = (EVALUATE_EXISTING_REASONING, STRESS_TEST, FALSIFY)

# Règle 1 : intention explicite -> posture (UNSPECIFIED n'y figure pas).
_POSTURE_BY_EXPLICIT_INTENT = {
    DIRECT_ANSWER_REQUESTED: EXPLAIN,
    REASONING_RESERVED_FOR_USER: GUIDE,
    JOINT_REASONING_REQUESTED: CO_REASON,
    CHALLENGE_REQUESTED: CHALLENGE,
}


class PostureBaselineError(Exception):
    """Erreur métier de 6-2B : jamais un plan partiel, jamais un repli
    explain, jamais une valeur corrigée."""


class InvalidPostureArgument(PostureBaselineError):
    """Argument qui n'est pas exactement un InteractionTaskProfile."""


class UnsupportedTaskProfileVersion(PostureBaselineError):
    """Versions de schéma ou de policy 6-2A non supportées par cette
    policy de posture."""


class InvalidTaskProfileContent(PostureBaselineError):
    """Profil 6-2A structurellement incohérent (jamais corrigé)."""


@dataclass(frozen=True, kw_only=True)
class SegmentPostureDecision:
    """Posture dominante d'UN segment de la demande : position et extrait
    du segment source, posture, base de sélection (provenance 6-2B)."""
    segment_index: int
    source_excerpt: str
    posture: str
    selection_basis: str


@dataclass(frozen=True, kw_only=True)
class InteractionPostureBaseline:
    """Posture de base du TOUR COURANT : une décision par segment de 6-2A,
    dans l'ordre de la demande. Éphémère, sans valeur probante."""
    schema_version: str
    policy_version: str

    source_task_schema_version: str
    source_task_policy_version: str

    request_status: str

    segments: tuple[SegmentPostureDecision, ...]


# --------------------------------------------------------------------------
# Revérification du profil 6-2A (pure, fail-closed)
# --------------------------------------------------------------------------

def _content_fail(path: str, message: str) -> InvalidTaskProfileContent:
    return InvalidTaskProfileContent(f"{path} : {message}")


def _choice(value, path: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise _content_fail(path, f"valeur {value!r} hors vocabulaire {list(allowed)}")
    return value


def _canonical_set(values, path: str, allowed: tuple, *, required: bool) -> tuple[str, ...]:
    """Tuple exact, valeurs connues, sans doublon, dans l'ordre canonique du
    vocabulaire (celui que produit 6-2A) : rien n'est réordonné."""
    if type(values) is not tuple:
        raise _content_fail(path, f"tuple attendu, reçu {type(values).__name__}")
    if required and not values:
        raise _content_fail(path, "au moins une valeur attendue")
    chosen = tuple(_choice(value, f"{path}[{i}]", allowed) for i, value in enumerate(values))
    if len(set(chosen)) != len(chosen):
        raise _content_fail(path, "valeur en double")
    if chosen != tuple(value for value in allowed if value in chosen):
        raise _content_fail(path, "hors de l'ordre canonique du vocabulaire (jamais réordonné)")
    return chosen


def _check_segment(segment, path: str) -> None:
    if type(segment) is not TaskSegment:
        raise _content_fail(path, f"TaskSegment attendu, reçu {type(segment).__name__}")
    excerpt = segment.source_excerpt
    if type(excerpt) is not str or not excerpt.strip():
        raise _content_fail(f"{path}.source_excerpt", f"extrait non vide attendu, reçu {excerpt!r}")
    _choice(segment.explicit_intent, f"{path}.explicit_intent", EXPLICIT_INTENTS)
    operations = _canonical_set(segment.cognitive_operations, f"{path}.cognitive_operations",
                                COGNITIVE_OPERATIONS, required=True)
    characteristics = _canonical_set(segment.task_characteristics, f"{path}.task_characteristics",
                                     TASK_CHARACTERISTICS, required=False)
    if EXISTING_REASONING_TO_EVALUATE in characteristics and not set(operations) & set(CHALLENGE_TASK_OPERATIONS):
        raise _content_fail(path, f"{EXISTING_REASONING_TO_EVALUATE} sans opération parmi "
                                  f"{list(CHALLENGE_TASK_OPERATIONS)}")
    if EVALUATE_EXISTING_REASONING in operations and EXISTING_REASONING_TO_EVALUATE not in characteristics:
        raise _content_fail(path, f"{EVALUATE_EXISTING_REASONING} sans la caractéristique "
                                  f"{EXISTING_REASONING_TO_EVALUATE}")


def _check_profile(profile: InteractionTaskProfile) -> None:
    if type(profile) is not InteractionTaskProfile:
        raise InvalidPostureArgument(f"InteractionTaskProfile attendu, reçu {type(profile).__name__}")
    for name, value, supported in (("schema_version", profile.schema_version, TASK_SCHEMA_VERSION),
                                   ("policy_version", profile.policy_version, TASK_POLICY_VERSION)):
        if type(value) is not str or value != supported:
            raise UnsupportedTaskProfileVersion(f"{name} : {value!r} non supportée ({supported!r} attendue)")
    status = _choice(profile.request_status, "request_status", REQUEST_STATUSES)
    segments = profile.segments
    if type(segments) is not tuple:
        raise _content_fail("segments", f"tuple attendu, reçu {type(segments).__name__}")
    if status == NO_TASK:
        if segments:
            raise _content_fail("segments", f"{NO_TASK} : aucun segment attendu, {len(segments)} reçu(s)")
    elif not 1 <= len(segments) <= MAX_TASK_SEGMENTS:
        raise _content_fail("segments", f"{status} : {len(segments)} segment(s), 1 à {MAX_TASK_SEGMENTS} attendus")
    for index, segment in enumerate(segments):
        _check_segment(segment, f"segments[{index}]")


# --------------------------------------------------------------------------
# Sélection de la posture (posture-policy-1)
# --------------------------------------------------------------------------

def _decide(index: int, segment: TaskSegment) -> SegmentPostureDecision:
    intent = segment.explicit_intent
    if intent in _POSTURE_BY_EXPLICIT_INTENT:
        posture, basis = _POSTURE_BY_EXPLICIT_INTENT[intent], EXPLICIT_INTENT_BASIS
    elif intent == UNSPECIFIED and set(segment.cognitive_operations) & set(CHALLENGE_TASK_OPERATIONS):
        posture, basis = CHALLENGE, TASK_OPERATION_BASIS
    elif intent == UNSPECIFIED:
        posture, basis = EXPLAIN, DIRECT_FALLBACK_BASIS
    else:
        # Intention connue de 6-2A sans règle de posture : vocabulaire et
        # policy désalignés, jamais un repli.
        raise _content_fail(f"segments[{index}].explicit_intent", f"{intent!r} sans règle de posture")
    return SegmentPostureDecision(segment_index=index, source_excerpt=segment.source_excerpt,
                                  posture=posture, selection_basis=basis)


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def build_posture_baseline(task_profile: InteractionTaskProfile) -> InteractionPostureBaseline:
    """InteractionTaskProfile validé (6-2A) -> InteractionPostureBaseline.
    Fonction PURE : même profil => même plan.

    Le profil est entièrement revérifié avant toute décision ; argument
    d'un autre type => InvalidPostureArgument, versions 6-2A non supportées
    => UnsupportedTaskProfileVersion, contenu incohérent =>
    InvalidTaskProfileContent. Aucun plan partiel, aucune correction,
    aucun repli. no_task => aucun segment."""
    _check_profile(task_profile)
    return InteractionPostureBaseline(
        schema_version=POSTURE_BASELINE_SCHEMA_VERSION,
        policy_version=POSTURE_POLICY_VERSION,
        source_task_schema_version=task_profile.schema_version,
        source_task_policy_version=task_profile.policy_version,
        request_status=task_profile.request_status,
        segments=tuple(_decide(index, segment) for index, segment in enumerate(task_profile.segments)),
    )
