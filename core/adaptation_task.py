"""Étape 6.2A1 : contrats et validateur DÉTERMINISTE du profil de la
DEMANDE et de la TÂCHE courantes (InteractionTaskProfile).

Question de 6-2A, et seulement elle : « qu'est-ce que l'utilisateur demande
de faire dans cette interaction, sous quelle intention pédagogique
EXPLICITE, et quelles opérations cette tâche exige-t-elle ? ». Ce module en
porte les contrats et la porte de validation ; la proposition sémantique
vient du classificateur 6-2A2 (core/adaptation_task_classifier.py) :

    InteractionTaskInput (message courant + contexte minimal borné)
    TaskProfileProposal (NON FIABLE, sortie du classificateur)
        -> validate_task_profile_proposal -> InteractionTaskProfile (validé)

Frontière constitutionnelle. 6-2A décrit la tâche INDÉPENDAMMENT de ce
qu'Oryx sait de l'utilisateur : aucun identifiant de personne, aucun état
Step 5, aucune donnée de 6.1 (focus, présupposés sûrs, contraintes,
contexte ou projection pédagogique), aucune base. Même interaction + même
proposition => même profil, quelle que soit la personne. 6-2A ne choisit
AUCUNE posture (Expliquer / Guider / Co-raisonner / Challenger), ne calcule
aucun support ni aucun écart tâche / présupposés, ne crée ni question, ni
validation, ni observation, ni trace de support : ces décisions
appartiennent aux sous-étapes suivantes de 6.2. Le focus de compétence
(6-1B : « de quoi parle la demande ? ») est une autre question, sur le même
message : il n'est ni lu, ni importé, ni modifié.

Invariants :

- Versions explicites : TASK_SCHEMA_VERSION décrit le contrat entrée /
  sortie ; TASK_POLICY_VERSION décrit les vocabulaires fermés (intentions,
  opérations, caractéristiques) et leurs règles de cohérence. Une
  modification de vocabulaire exige une nouvelle policy.

- Entrée bornée, jamais tronquée : message courant non vide d'au plus
  MAX_TASK_MESSAGE_CHARS caractères ; au plus MAX_TASK_CONTEXT_TURNS tours
  précédents (du plus ancien au plus récent, rôles user / assistant), dont
  les contenus cumulés tiennent dans MAX_TASK_CONTEXT_CHARS. Le message
  courant est l'autorité ; le contexte ne sert qu'à désambiguïser.
  Structure propre à 6-2A (aucune dépendance au contrat d'entrée de 6-1B).

- Intention EXPLICITE seulement : explicit_intent dit ce que le segment
  demande explicitement quant à la forme de l'aide ; « unspecified » est
  l'ABSENCE d'intention explicite, jamais une demande de réponse directe
  (le repli « répondre directement » appartient au plan de posture).
  Aucune intention n'est déduite d'un niveau, d'un besoin de validation ni
  d'une préférence passée : elle vaut pour CE tour seulement.

- Exigences de la tâche QUALITATIVES : cognitive_operations (vocabulaire
  fermé, au moins une) et task_characteristics (vocabulaire fermé, 0..N).
  Aucun score, aucun niveau easy / medium / hard, aucune mesure de la
  personne.

- Segmentation locale : 1..MAX_TASK_SEGMENTS segments pour une tâche
  demandée, dans l'ordre de la demande ; aucun segment quand la demande ne
  comporte aucune tâche (salutation, remerciement) — décision explicite
  « no_task » du classificateur, jamais un repli.

- Ancrage vérifiable : chaque segment cite un extrait VERBATIM et contigu
  du message courant (source_excerpt). Les extraits apparaissent dans le
  message dans l'ordre des segments, sans chevauchement : l'ordre de la
  demande est ainsi vérifié, jamais supposé. Aucune paraphrase.

- Validateur PUR : validate_task_profile_proposal ne touche ni base, ni
  horloge, ni environnement, ni hasard, ni réseau. Il vérifie la structure
  (types exacts, versions, vocabulaires, doublons, ancrage, cohérence entre
  champs) et rien d'autre : il ne prétend jamais prouver que
  l'interprétation sémantique est vraie. Toute violation =>
  InvalidTaskProfileProposal ; rien n'est corrigé, aucune valeur n'est
  remplacée par « unspecified », aucun segment générique n'est synthétisé.

- Éphémère et sans valeur probante : le profil est recalculé à chaque tour,
  jamais persisté, jamais transmis à Step 5 comme preuve. Demander « challenge
  ma thèse » ne prouve aucune maîtrise ; demander une réponse directe ne
  prouve aucune faiblesse.

Ordres canoniques : opérations et caractéristiques d'un segment dans
l'ordre de leur vocabulaire (un ensemble, jamais un séquencement imposé) ;
segments dans l'ordre de la demande.
"""
from dataclasses import dataclass

TASK_SCHEMA_VERSION = "interaction-task-profile-v1"
TASK_POLICY_VERSION = "task-policy-1"

MAX_TASK_CONTEXT_TURNS = 8
MAX_TASK_MESSAGE_CHARS = 6000
MAX_TASK_CONTEXT_CHARS = 12000
MAX_TASK_SEGMENTS = 6

CONTEXT_ROLES = ("user", "assistant")

# TaskProfileProposal.request_status / InteractionTaskProfile.request_status
TASK_REQUESTED = "task_requested"
NO_TASK = "no_task"
REQUEST_STATUSES = (TASK_REQUESTED, NO_TASK)

# TaskSegment.explicit_intent : intention pédagogique EXPLICITE du segment.
DIRECT_ANSWER_REQUESTED = "direct_answer_requested"
REASONING_RESERVED_FOR_USER = "reasoning_reserved_for_user"
JOINT_REASONING_REQUESTED = "joint_reasoning_requested"
CHALLENGE_REQUESTED = "challenge_requested"
UNSPECIFIED = "unspecified"
EXPLICIT_INTENTS = (
    DIRECT_ANSWER_REQUESTED,
    REASONING_RESERVED_FOR_USER,
    JOINT_REASONING_REQUESTED,
    CHALLENGE_REQUESTED,
    UNSPECIFIED,
)

# TaskSegment.cognitive_operations : ce que la tâche exige (ordre canonique).
RETRIEVE_OR_DEFINE = "retrieve_or_define"
EXPLAIN_MECHANISM = "explain_mechanism"
APPLY_PROCEDURE = "apply_procedure"
CALCULATE = "calculate"
SELECT_RELEVANT_INFORMATION = "select_relevant_information"
GENERATE_HYPOTHESES = "generate_hypotheses"
INTERPRET_EVIDENCE = "interpret_evidence"
COMPARE = "compare"
CONSTRUCT_REASONING = "construct_reasoning"
FORM_CONCLUSION = "form_conclusion"
EVALUATE_EXISTING_REASONING = "evaluate_existing_reasoning"
STRESS_TEST = "stress_test"
FALSIFY = "falsify"
SYNTHESIZE = "synthesize"
COGNITIVE_OPERATIONS = (
    RETRIEVE_OR_DEFINE,
    EXPLAIN_MECHANISM,
    APPLY_PROCEDURE,
    CALCULATE,
    SELECT_RELEVANT_INFORMATION,
    GENERATE_HYPOTHESES,
    INTERPRET_EVIDENCE,
    COMPARE,
    CONSTRUCT_REASONING,
    FORM_CONCLUSION,
    EVALUATE_EXISTING_REASONING,
    STRESS_TEST,
    FALSIFY,
    SYNTHESIZE,
)

# TaskSegment.task_characteristics : caractéristiques qualitatives de la
# demande (ordre canonique ; aucune n'est un score).
MULTI_STEP = "multi_step"
DOMAIN_SPECIALIZED = "domain_specialized"
OPEN_ENDED = "open_ended"
USER_SPECIFIC_APPLICATION = "user_specific_application"
EXISTING_REASONING_TO_EVALUATE = "existing_reasoning_to_evaluate"
TASK_CHARACTERISTICS = (
    MULTI_STEP,
    DOMAIN_SPECIALIZED,
    OPEN_ENDED,
    USER_SPECIFIC_APPLICATION,
    EXISTING_REASONING_TO_EVALUATE,
)

# Opérations qui portent sur un raisonnement déjà formulé : la
# caractéristique existing_reasoning_to_evaluate en exige au moins une, et
# evaluate_existing_reasoning exige la caractéristique.
_EVALUATION_OPERATIONS = (EVALUATE_EXISTING_REASONING, STRESS_TEST, FALSIFY)


class TaskError(Exception):
    """Erreur métier de 6-2A : jamais un profil partiel, jamais un repli
    « unspecified » ou un segment générique implicite."""


class InvalidTaskArgument(TaskError):
    """Argument d'un type inattendu (proposition, interaction, backend)."""


class InvalidTaskInput(TaskError):
    """Interaction ou contexte hors contrat (jamais tronqué)."""


class InvalidTaskProfileProposal(TaskError):
    """Proposition structurellement invalide (jamais corrigée)."""


# --------------------------------------------------------------------------
# Entrée
# --------------------------------------------------------------------------

def _text(value, path: str, limit: int | None = None) -> None:
    """str exact, non vide après strip, encodable en UTF-8, borné ; jamais
    modifié."""
    if type(value) is not str:
        raise InvalidTaskInput(f"{path} : str attendu, reçu {type(value).__name__}")
    if not value.strip():
        raise InvalidTaskInput(f"{path} : texte vide")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidTaskInput(f"{path} : texte non encodable en UTF-8") from exc
    if limit is not None and len(value) > limit:
        raise InvalidTaskInput(f"{path} : {len(value)} caractères pour {limit} au plus (jamais tronqué)")


@dataclass(frozen=True, kw_only=True)
class TaskContextTurn:
    """Un tour précédent de la conversation (user ou assistant)."""
    role: str
    content: str

    def __post_init__(self):
        _check_turn(self, "turn")


def _check_turn(turn, path: str) -> None:
    if type(turn) is not TaskContextTurn:
        raise InvalidTaskInput(f"{path} : TaskContextTurn attendu, reçu {type(turn).__name__}")
    if type(turn.role) is not str or turn.role not in CONTEXT_ROLES:
        raise InvalidTaskInput(f"{path}.role : {turn.role!r} hors de {list(CONTEXT_ROLES)}")
    _text(turn.content, f"{path}.content")


@dataclass(frozen=True, kw_only=True)
class InteractionTaskInput:
    """Demande à profiler : message courant (autorité principale) et
    contexte minimal, ordonné du plus ancien au plus récent."""
    current_message: str
    context_turns: tuple[TaskContextTurn, ...]

    def __post_init__(self):
        check_interaction(self)


def check_interaction(interaction) -> None:
    """Contrôle complet d'une InteractionTaskInput (type exact, bornes) ;
    utilisé à la construction et à chaque frontière publique."""
    if type(interaction) is not InteractionTaskInput:
        raise InvalidTaskArgument(f"InteractionTaskInput attendue, reçu {type(interaction).__name__}")
    _text(interaction.current_message, "current_message", MAX_TASK_MESSAGE_CHARS)
    turns = interaction.context_turns
    if type(turns) is not tuple:
        raise InvalidTaskInput(f"context_turns : tuple attendu, reçu {type(turns).__name__}")
    if len(turns) > MAX_TASK_CONTEXT_TURNS:
        raise InvalidTaskInput(
            f"context_turns : {len(turns)} tours pour {MAX_TASK_CONTEXT_TURNS} au plus (jamais tronqué)")
    for index, turn in enumerate(turns):
        _check_turn(turn, f"context_turns[{index}]")
    total = sum(len(turn.content) for turn in turns)
    if total > MAX_TASK_CONTEXT_CHARS:
        raise InvalidTaskInput(
            f"context_turns : {total} caractères cumulés pour {MAX_TASK_CONTEXT_CHARS} au plus (jamais tronqué)")


# --------------------------------------------------------------------------
# Proposition (NON FIABLE) et profil validé
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class TaskSegmentProposal:
    """Segment proposé (NON FIABLE)."""
    source_excerpt: str
    explicit_intent: str
    cognitive_operations: tuple[str, ...]
    task_characteristics: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class TaskProfileProposal:
    """Proposition NON FIABLE (sortie du classificateur 6-2A2) :
    inutilisable avant validate_task_profile_proposal."""
    schema_version: str
    policy_version: str

    request_status: str

    segments: tuple[TaskSegmentProposal, ...]


@dataclass(frozen=True, kw_only=True)
class TaskSegment:
    """UNE opération pédagogique cohérente de la demande : extrait verbatim
    du message courant, intention explicite, exigences qualitatives (ordre
    canonique)."""
    source_excerpt: str
    explicit_intent: str
    cognitive_operations: tuple[str, ...]
    task_characteristics: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class InteractionTaskProfile:
    """Profil validé de la demande et de la tâche du TOUR COURANT :
    segments dans l'ordre de la demande. Éphémère, sans valeur probante."""
    schema_version: str
    policy_version: str

    request_status: str

    segments: tuple[TaskSegment, ...]


# --------------------------------------------------------------------------
# Validation de la proposition (pure)
# --------------------------------------------------------------------------

def _choice(value, name: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise InvalidTaskProfileProposal(f"{name} : valeur {value!r} hors vocabulaire {list(allowed)}")
    return value


def _vocabulary_set(values, name: str, allowed: tuple, *, required: bool) -> tuple[str, ...]:
    """Tuple exact de valeurs du vocabulaire, sans doublon -> ordre
    canonique du vocabulaire."""
    if type(values) is not tuple:
        raise InvalidTaskProfileProposal(f"{name} : tuple attendu, reçu {type(values).__name__}")
    if required and not values:
        raise InvalidTaskProfileProposal(f"{name} : au moins une valeur attendue")
    chosen = [_choice(value, f"{name}[{i}]", allowed) for i, value in enumerate(values)]
    if len(set(chosen)) != len(chosen):
        raise InvalidTaskProfileProposal(f"{name} : valeur en double")
    return tuple(value for value in allowed if value in chosen)


def _segment(proposed, path: str) -> TaskSegment:
    if type(proposed) is not TaskSegmentProposal:
        raise InvalidTaskProfileProposal(f"{path} : TaskSegmentProposal attendu, reçu {type(proposed).__name__}")
    excerpt = proposed.source_excerpt
    if type(excerpt) is not str or not excerpt.strip():
        raise InvalidTaskProfileProposal(f"{path}.source_excerpt : extrait non vide attendu, reçu {excerpt!r}")
    intent = _choice(proposed.explicit_intent, f"{path}.explicit_intent", EXPLICIT_INTENTS)
    operations = _vocabulary_set(proposed.cognitive_operations, f"{path}.cognitive_operations",
                                 COGNITIVE_OPERATIONS, required=True)
    characteristics = _vocabulary_set(proposed.task_characteristics, f"{path}.task_characteristics",
                                      TASK_CHARACTERISTICS, required=False)
    evaluates = [operation for operation in operations if operation in _EVALUATION_OPERATIONS]
    if EXISTING_REASONING_TO_EVALUATE in characteristics and not evaluates:
        raise InvalidTaskProfileProposal(
            f"{path} : {EXISTING_REASONING_TO_EVALUATE} sans opération parmi {list(_EVALUATION_OPERATIONS)}")
    if EVALUATE_EXISTING_REASONING in operations and EXISTING_REASONING_TO_EVALUATE not in characteristics:
        raise InvalidTaskProfileProposal(
            f"{path} : {EVALUATE_EXISTING_REASONING} sans la caractéristique {EXISTING_REASONING_TO_EVALUATE}")
    return TaskSegment(source_excerpt=excerpt, explicit_intent=intent, cognitive_operations=operations,
                       task_characteristics=characteristics)


def validate_task_profile_proposal(
    proposal: TaskProfileProposal,
    interaction: InteractionTaskInput,
) -> InteractionTaskProfile:
    """TaskProfileProposal NON FIABLE -> InteractionTaskProfile validé.
    Fonction PURE (aucune base, horloge, environnement, réseau ni hasard) :
    même entrée => même sortie.

    Versions exactes ; request_status task_requested => 1..MAX_TASK_SEGMENTS
    segments, no_task => aucun segment. Chaque segment : extrait verbatim
    non vide du message courant (les extraits se suivent dans l'ordre des
    segments, sans chevauchement), intention explicite connue, opérations
    connues (au moins une, sans doublon), caractéristiques connues (sans
    doublon), cohérence existing_reasoning_to_evaluate <-> opérations
    d'évaluation. Toute violation => InvalidTaskProfileProposal (jamais
    corrigée, jamais un repli)."""
    if type(proposal) is not TaskProfileProposal:
        raise InvalidTaskArgument(f"TaskProfileProposal attendue, reçu {type(proposal).__name__}")
    check_interaction(interaction)

    _choice(proposal.schema_version, "schema_version", (TASK_SCHEMA_VERSION,))
    _choice(proposal.policy_version, "policy_version", (TASK_POLICY_VERSION,))
    status = _choice(proposal.request_status, "request_status", REQUEST_STATUSES)
    if type(proposal.segments) is not tuple:
        raise InvalidTaskProfileProposal(f"segments : tuple attendu, reçu {type(proposal.segments).__name__}")
    if status == NO_TASK:
        if proposal.segments:
            raise InvalidTaskProfileProposal(f"{NO_TASK} : aucun segment attendu, {len(proposal.segments)} reçu(s)")
    elif not 1 <= len(proposal.segments) <= MAX_TASK_SEGMENTS:
        raise InvalidTaskProfileProposal(
            f"{TASK_REQUESTED} : {len(proposal.segments)} segment(s), 1 à {MAX_TASK_SEGMENTS} attendus")

    segments = tuple(_segment(proposed, f"segments[{i}]") for i, proposed in enumerate(proposal.segments))

    # Ancrage : plus petite position compatible, extrait après extrait
    # (suffisant pour décider s'il existe un placement ordonné sans
    # chevauchement).
    message, cursor = interaction.current_message, 0
    for i, segment in enumerate(segments):
        start = message.find(segment.source_excerpt, cursor)
        if start < 0:
            where = "absent du message courant" if segment.source_excerpt not in message else (
                "hors de l'ordre de la demande ou chevauchant l'extrait précédent")
            raise InvalidTaskProfileProposal(f"segments[{i}].source_excerpt : {where} (aucune paraphrase)")
        cursor = start + len(segment.source_excerpt)

    return InteractionTaskProfile(
        schema_version=TASK_SCHEMA_VERSION,
        policy_version=TASK_POLICY_VERSION,
        request_status=status,
        segments=segments,
    )
