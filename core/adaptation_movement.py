"""Étape 6.3A : MOUVEMENT PÉDAGOGIQUE marginal de l'interaction
(InteractionPedagogicalMovement) — contrats, préflight, validateur
DÉTERMINISTE et projection de génération.

Question de 6.3, et seulement elle : « une fois la tâche, la posture et le
support pédagogique de cette interaction déjà planifiés, existe-t-il UN
incrément pédagogique supplémentaire qui rendrait sensiblement la réponse
plus utile pour l'objectif explicite courant ? Si oui, lequel ? Sinon, rester
sur le besoin présent. ». Ce module en porte les contrats et les portes
déterministes ; la proposition sémantique vient du classificateur 6-3B
(core/adaptation_movement_classifier.py) :

    6-2A InteractionTaskProfile + 6-2B InteractionPostureBaseline
    + 6-2C InteractionSupportPlan + 6-1E PedagogicalResponseContext
    + 6-1B CurrentFocusTaxonomy
        -> prepare_movement_planning (préflight) -> MovementPlanningInput
           (segments + support prévu + catalogue restreint, seule matière
           de 6-3B)
    MovementProposal (NON FIABLE, sortie de 6-3B) + les MÊMES entrées
        -> validate_movement_proposal -> InteractionPedagogicalMovement
    InteractionPedagogicalMovement + les MÊMES entrées
        -> project_pedagogical_movement -> MovementGenerationProjection

Frontière constitutionnelle. 6.3 est MARGINAL, jamais descriptif : 6-2A a
identifié la tâche, 6-2B choisi la posture, 6-2C calibré le support ; 6.3 ne
reclassifie aucun de ces objets et demande seulement ce qu'il faudrait
éventuellement AJOUTER. Ne rien ajouter (stay) est une décision pédagogique
positive, valide et souvent optimale. « Décrire, jamais prescrire. » La
demande explicite domine ; répondre est prioritaire sur tester ; une
progression n'est pas un parcours imposé ; une lacune n'est pas une dette
pédagogique. 6.3 ne choisit ni stade, ni compétence hors 6.1, ni surface
produit (l'exécution inline est la norme, aucune surface n'est décidée ici),
n'active aucune revalidation Step 5, ne génère aucun texte, ne crée ni
observation, ni preuve, ni trace de support réel.

Invariants :

- Versions explicites et distinctes : MOVEMENT_SCHEMA_VERSION (structure des
  contrats), MOVEMENT_POLICY_VERSION (vocabulaire des mouvements, règles
  déterministes) ; la politique du prompt 6-3B a sa propre version. Les
  versions des entrées sont vérifiées EXACTEMENT ; aucune compatibilité
  implicite.

- Six mouvements, vocabulaire fermé, SANS hiérarchie : stay, clarify,
  deepen, apply, generalize, integrate. Ils ne sont ni ordonnés, ni des
  niveaux, ni des étapes obligatoires ; aucun n'est « supérieur » ; aucun
  index, aucun rang, aucun « mouvement suivant ». apply / generalize /
  integrate sont des occasions créées par Oryx, jamais des preuves.

- UN mouvement dominant pour l'interaction ENTIÈRE, jamais un mouvement par
  segment (aucun mini-curriculum) : les segments servent uniquement à
  l'ancrer.

- Préflight : la frontière publique de 6-2C est RÉUTILISÉE, jamais
  dupliquée. prepare_support_planning revérifie tâche <-> posture (via 6-2B)
  et contexte <-> taxonomie (via 6-1E et 6-1B) et reconstruit le
  RestrictedSupportCatalogue canonique ; project_support_plan revérifie le
  plan de support reçu contre CE catalogue (versions, statut de résolution,
  présupposés, ponts, allocations, règles de posture). 6-3A ajoute ce que
  ces frontières ne voient pas : versions source du plan == versions des
  entrées reçues, request_status identique, même nombre de segments, même
  source_excerpt et même posture, segment par segment, opérations allouées
  appartenant aux opérations 6-2A du segment. Compatibilité != authenticité
  : la provenance réelle des objets relève de l'orchestration. Rien n'est
  corrigé, remappé ni recalculé : aucun plan de support différent n'est
  reconstruit à la place de 6-2C.

- MovementPlanningInput : immuable, éphémère, minimal. Par segment :
  extrait, intention explicite, opérations et caractéristiques 6-2A,
  posture 6-2B, support prévu 6-2C (sens sémantique de la projection 6-2C).
  Catalogue restreint 6.1 : codes, libellés, questions centrales, tokens,
  libellés et définitions des capacités, safe_stages. Jamais : UUID, release,
  fingerprint, provenance Step 5, identité de personne, mapping_guidance
  (6-3B ne refait pas le travail de 6-1B / 6-2C), contraintes de validation
  (un besoin de validation ne doit jamais CAUSER un mouvement : elles
  restent inertes, ni projetées, ni sélectionnées, ni copiées), historique,
  activité de marché.

- Stades sûrs : un PLANCHER de présupposés, jamais un déclencheur. Aucune
  table stade -> mouvement, aucun index de stade, aucun seuil, aucune
  distance, aucune comparaison « plus haut / plus bas ».

- Stay DÉTERMINISTE (politique V1) : request_status no_task, ou focus non
  résolu (neutral / ambiguous / composite) => seul stay est valide (anchor
  None, movement_intent None). Ce n'est pas une conclusion pédagogique sur
  ces contextes : 6.3 ne crée aucun incrément personnalisé sans périmètre
  pédagogique résolu ; la réponse 6.2 reste normale et utile.

- Proposition NON FIABLE (MovementProposal) : mouvement, indices de
  segments, codes de compétence, tokens sémantiques
  <capability_code>@r<semantic_revision> (ceux du catalogue restreint),
  movement_intent. Jamais d'UUID, de stade, de score, de rationale, de
  diagnostic, de surface ni de réponse rédigée.
    stay      : aucun segment, aucune compétence, aucun token, intention None ;
    autre     : au moins un segment existant, au moins une compétence du
                catalogue restreint (integrate : au moins deux distinctes),
                movement_intent non vide d'au plus MAX_MOVEMENT_INTENT_CHARS
                caractères (jamais tronqué, jamais reformulé) ;
    localized : au moins un token, tous dans le périmètre 6.1 exact de la
                compétence (jamais élargi à la compétence entière) ;
    competency_only : aucun token.
  Aucun token d'une compétence non déclarée, hors catalogue, ni en double.
  Python ne juge JAMAIS la valeur sémantique du mouvement (integrate exige
  deux compétences, pas une preuve d'intégration) : il vérifie la structure.

- Canonicalisation : indices dans l'ordre des segments source, compétences
  cible puis supports (ordre du catalogue), definition_id reconstruits par
  Python dans l'ordre du catalogue. L'ordre du JSON proposé n'est jamais une
  décision pédagogique ; doublon, valeur inconnue ou hors périmètre =>
  refus.

- Fail-closed : InvalidMovementArgument (types top-level),
  UnsupportedMovementVersion (version non supportée),
  IncompatibleMovementInputs (entrées non appariées),
  InvalidMovementContent (entrée ou mouvement structurellement incohérent),
  InvalidMovementProposal (proposition refusée). Les erreurs des frontières
  amont sont chaînées. Jamais un stay synthétique sur corruption : stay est
  une décision pédagogique, jamais un repli d'erreur.

- PUR, éphémère, sans valeur probante : ni base, ni horloge, ni
  environnement, ni hasard, ni réseau, ni modèle, ni persistance. Mêmes
  entrées + même proposition => même mouvement. Recalculé à chaque
  interaction : aucun mouvement, aucune compétence « suivante » mémorisés,
  aucun curriculum caché. Le mouvement ne prouve rien sur l'utilisateur et
  n'est jamais transmis à Step 5.

- Projection de génération (MovementGenerationProjection) : dérivée d'un
  mouvement revérifié contre les mêmes entrées ; mouvement, intention,
  indices de segments, compétences (code, libellé) et capacités (token,
  libellé, définition). Aucun UUID, aucune provenance, aucune contrainte de
  validation, aucun stade (le futur générateur reçoit le support 6.2 par
  ailleurs ; rien ici ne ressemble à un niveau).

Dépendances : la surface publique de 6-2C (contrats, préflight, projection,
erreurs et contrats amont qu'elle ré-expose) ; aucun autre module amont.
"""
import uuid
from dataclasses import dataclass

from core.adaptation_support import (
    COMPETENCY_ONLY,  # ré-exposé pour 6-3B (vocabulaire du catalogue), comme LOCALIZED
    LOCALIZED,
    NO_TASK,
    RESOLVED,
    CurrentFocusTaxonomy,
    IncompatibleSupportPlanInputs,
    InteractionPostureBaseline,
    InteractionSupportPlan,
    InteractionTaskProfile,
    InvalidSupportPlanArgument,
    PedagogicalResponseContext,
    PlannedOperationAllocation,
    ProjectedCapability,
    ProjectedConceptualBridge,
    ProjectedSafeAssumption,
    RestrictedSupportCatalogue,
    SupportPlanError,
    UnsupportedSupportPlanVersion,
    prepare_support_planning,
    project_support_plan,
)

MOVEMENT_SCHEMA_VERSION = "interaction-pedagogical-movement-v1"
MOVEMENT_POLICY_VERSION = "movement-policy-1"

# Vocabulaire fermé, SANS hiérarchie : l'ordre du tuple n'est qu'un ordre
# d'énumération, jamais un rang, un niveau ni une séquence.
STAY = "stay"
CLARIFY = "clarify"
DEEPEN = "deepen"
APPLY = "apply"
GENERALIZE = "generalize"
INTEGRATE = "integrate"
PEDAGOGICAL_MOVEMENTS = (STAY, CLARIFY, DEEPEN, APPLY, GENERALIZE, INTEGRATE)

# Borne technique de movement_intent (jamais tronqué : dépassement = refus).
MAX_MOVEMENT_INTENT_CHARS = 500


class MovementError(Exception):
    """Erreur métier de 6.3 : jamais un mouvement partiel, jamais un stay
    synthétique sur corruption."""


class InvalidMovementArgument(MovementError):
    """Argument top-level d'un type inattendu."""


class UnsupportedMovementVersion(MovementError):
    """Version d'une entrée (6-2A, 6-2B, 6-2C, 6-1E, 6-1B) ou d'un mouvement
    non supportée par cette policy."""


class IncompatibleMovementInputs(MovementError):
    """Entrées valides isolément mais non appariées (tâche / posture /
    support / contexte / taxonomie, mouvement / entrées) : jamais
    rapprochées."""


class InvalidMovementContent(MovementError):
    """Entrée ou mouvement structurellement incohérent (jamais corrigé)."""


class InvalidMovementProposal(MovementError):
    """Proposition NON FIABLE refusée (jamais corrigée)."""


# --------------------------------------------------------------------------
# Entrée de 6-3B (sortie du préflight) : jamais d'UUID
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class MovementCatalogueCapability:
    """Une capacité AUTORISÉE par 6.1 : token sémantique, sens et safe_stages
    exacts. Ni definition_id, ni mapping_guidance."""
    capability_token: str
    label: str
    definition: str
    safe_stages: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class MovementCatalogueCompetency:
    """Une compétence AUTORISÉE par 6.1 (cible ou support). localized :
    capacités du périmètre exact ; competency_only : aucune capacité,
    safe_stages de la couverture competency_only."""
    role: str
    competency_code: str
    competency_label: str
    central_question: str
    scope_mode: str
    capabilities: tuple[MovementCatalogueCapability, ...]
    competency_only_safe_stages: tuple[str, ...] | None


@dataclass(frozen=True, kw_only=True)
class PlannedSegmentSupport:
    """Support DÉJÀ prévu par 6-2C pour un segment (sens sémantique de la
    projection 6-2C) : ce que 6.3 ne refait jamais."""
    relevant_safe_assumptions: tuple[ProjectedSafeAssumption, ...]
    conceptual_bridges: tuple[ProjectedConceptualBridge, ...]
    operation_allocations: tuple[PlannedOperationAllocation, ...]


@dataclass(frozen=True, kw_only=True)
class MovementSegmentInput:
    """Un segment déjà planifié : ancrage et exigences 6-2A, posture 6-2B,
    support 6-2C."""
    segment_index: int
    source_excerpt: str
    explicit_intent: str
    cognitive_operations: tuple[str, ...]
    task_characteristics: tuple[str, ...]
    posture: str
    planned_support: PlannedSegmentSupport


@dataclass(frozen=True, kw_only=True)
class MovementPlanningInput:
    """Sortie du préflight : seule matière sémantique de 6-3B (aucun
    identifiant, aucune provenance, aucune contrainte de validation)."""
    request_status: str
    pedagogical_resolution_status: str
    segments: tuple[MovementSegmentInput, ...]
    catalogue: tuple[MovementCatalogueCompetency, ...]


# --------------------------------------------------------------------------
# Proposition NON FIABLE et mouvement validé
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class MovementProposal:
    """Proposition NON FIABLE (sortie de 6-3B) : inutilisable avant
    validate_movement_proposal. Rien d'autre : ni score, ni stade, ni
    rationale, ni surface."""
    schema_version: str
    policy_version: str

    movement: str

    segment_indices: tuple[int, ...]
    competency_codes: tuple[str, ...]
    capability_tokens: tuple[str, ...]

    movement_intent: str | None


@dataclass(frozen=True, kw_only=True)
class MovementAnchor:
    """Ancrage d'un mouvement autre que stay : segments, compétences et
    capacités du catalogue restreint (definition_id reconstruits par
    Python)."""
    segment_indices: tuple[int, ...]
    competency_codes: tuple[str, ...]
    capability_definition_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, kw_only=True)
class InteractionPedagogicalMovement:
    """Mouvement pédagogique marginal du TOUR COURANT, un seul pour toute
    l'interaction. Éphémère, sans valeur probante."""
    schema_version: str
    policy_version: str

    source_task_schema_version: str
    source_task_policy_version: str

    source_posture_schema_version: str
    source_posture_policy_version: str

    source_support_schema_version: str
    source_support_policy_version: str

    source_pedagogical_context_schema_version: str

    request_status: str
    pedagogical_resolution_status: str

    movement: str
    anchor: MovementAnchor | None
    movement_intent: str | None


# --------------------------------------------------------------------------
# Projection de génération
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ProjectedMovementCompetency:
    competency_code: str
    competency_label: str
    capabilities: tuple[ProjectedCapability, ...]


@dataclass(frozen=True, kw_only=True)
class MovementGenerationProjection:
    """Vue minimale du mouvement pour le futur générateur : sens sémantique
    seulement, aucun UUID, aucune provenance, aucun stade. Éphémère, sans
    valeur probante."""
    schema_version: str
    policy_version: str

    movement: str
    movement_intent: str | None
    segment_indices: tuple[int, ...]

    competencies: tuple[ProjectedMovementCompetency, ...]


# --------------------------------------------------------------------------
# Préflight (réutilise la frontière publique de 6-2C)
# --------------------------------------------------------------------------

_INPUT_TYPES = (("task_profile", InteractionTaskProfile), ("posture_baseline", InteractionPostureBaseline),
                ("support_plan", InteractionSupportPlan), ("context", PedagogicalResponseContext),
                ("taxonomy", CurrentFocusTaxonomy))


def _incompatible(path: str, message: str) -> IncompatibleMovementInputs:
    return IncompatibleMovementInputs(f"{path} : {message} (aucun rapprochement)")


def _upstream(call, what: str):
    """Frontière publique 6-2C ; ses erreurs sont chaînées, jamais
    absorbées."""
    try:
        return call()
    except InvalidSupportPlanArgument as exc:
        raise InvalidMovementArgument(f"{what} : {exc}") from exc
    except UnsupportedSupportPlanVersion as exc:
        raise UnsupportedMovementVersion(f"{what} : {exc}") from exc
    except IncompatibleSupportPlanInputs as exc:
        raise IncompatibleMovementInputs(f"{what} : {exc}") from exc
    except SupportPlanError as exc:
        raise InvalidMovementContent(f"{what} : {exc}") from exc


def _check_support_against_inputs(inputs: dict, planning) -> None:
    """Ce que project_support_plan ne peut pas voir : le plan décrit-il
    EXACTEMENT la tâche, la posture et le contexte reçus ?"""
    task, posture, plan, context = (inputs["task_profile"], inputs["posture_baseline"], inputs["support_plan"],
                                    inputs["context"])
    for name, planned, source in (
            ("source_task_schema_version", plan.source_task_schema_version, task.schema_version),
            ("source_task_policy_version", plan.source_task_policy_version, task.policy_version),
            ("source_posture_schema_version", plan.source_posture_schema_version, posture.schema_version),
            ("source_posture_policy_version", plan.source_posture_policy_version, posture.policy_version),
            ("source_pedagogical_context_schema_version", plan.source_pedagogical_context_schema_version,
             context.schema_version),
            ("request_status", plan.request_status, task.request_status),
            ("pedagogical_resolution_status", plan.pedagogical_resolution_status,
             planning.catalogue.resolution_status)):
        if planned != source:
            raise _incompatible(f"support.{name}", f"plan {planned!r}, entrée {source!r}")
    if len(plan.segments) != len(planning.segments):
        raise _incompatible("support.segments", f"{len(plan.segments)} segment(s) de support pour "
                                                f"{len(planning.segments)} segment(s) de tâche")
    for planned, segment in zip(plan.segments, planning.segments):
        path = f"support.segments[{segment.segment_index}]"
        if planned.source_excerpt != segment.source_excerpt:
            raise _incompatible(f"{path}.source_excerpt", "extrait différent du segment 6-2A")
        if planned.posture != segment.posture:
            raise _incompatible(f"{path}.posture", f"{planned.posture!r} au lieu de la posture 6-2B "
                                                   f"{segment.posture!r}")
        foreign = [a.operation for a in planned.operation_allocations
                   if a.operation not in segment.cognitive_operations]
        if foreign:
            raise _incompatible(f"{path}.operation_allocations", f"{foreign} absente(s) du segment 6-2A")


def _prepared(inputs: dict) -> tuple[MovementPlanningInput, RestrictedSupportCatalogue]:
    for name, expected in _INPUT_TYPES:
        if type(inputs[name]) is not expected:
            raise InvalidMovementArgument(f"{name} : {expected.__name__} attendu, reçu {type(inputs[name]).__name__}")
    planning = _upstream(lambda: prepare_support_planning(
        task_profile=inputs["task_profile"], posture_baseline=inputs["posture_baseline"], context=inputs["context"],
        taxonomy=inputs["taxonomy"]), "préflight 6-2C")
    projection = _upstream(lambda: project_support_plan(inputs["support_plan"], catalogue=planning.catalogue),
                           "plan de support 6-2C")
    _check_support_against_inputs(inputs, planning)
    catalogue = planning.catalogue
    return MovementPlanningInput(
        request_status=planning.request_status,
        pedagogical_resolution_status=catalogue.resolution_status,
        segments=tuple(MovementSegmentInput(
            segment_index=segment.segment_index,
            source_excerpt=segment.source_excerpt,
            explicit_intent=segment.explicit_intent,
            cognitive_operations=segment.cognitive_operations,
            task_characteristics=segment.task_characteristics,
            posture=segment.posture,
            planned_support=PlannedSegmentSupport(
                relevant_safe_assumptions=support.relevant_safe_assumptions,
                conceptual_bridges=support.conceptual_bridges,
                operation_allocations=support.operation_allocations,
            ),
        ) for segment, support in zip(planning.segments, projection.segments)),
        catalogue=tuple(MovementCatalogueCompetency(
            role=competency.role,
            competency_code=competency.competency_code,
            competency_label=competency.label,
            central_question=competency.central_question,
            scope_mode=competency.scope_mode,
            capabilities=tuple(MovementCatalogueCapability(
                capability_token=capability.capability_token,
                label=capability.label,
                definition=capability.definition,
                safe_stages=capability.safe_stages,
            ) for capability in competency.capabilities),
            competency_only_safe_stages=competency.competency_only_safe_stages,
        ) for competency in catalogue.competencies),
    ), catalogue


# --------------------------------------------------------------------------
# Validation de la proposition (pure)
# --------------------------------------------------------------------------

def _proposal_fail(path: str, message: str) -> InvalidMovementProposal:
    return InvalidMovementProposal(f"{path} : {message}")


def _tuple(value, path: str) -> tuple:
    if type(value) is not tuple:
        raise _proposal_fail(path, f"tuple attendu, reçu {type(value).__name__}")
    return value


def _segment_indices(values: tuple, planning: MovementPlanningInput) -> tuple[int, ...]:
    if not values:
        raise _proposal_fail("segment_indices", "au moins un segment d'ancrage attendu")
    known = tuple(segment.segment_index for segment in planning.segments)
    for index, value in enumerate(values):
        if type(value) is not int or value not in known:
            raise _proposal_fail(f"segment_indices[{index}]", f"{value!r} hors des segments {list(known)}")
    if len(set(values)) != len(values):
        raise _proposal_fail("segment_indices", "segment en double")
    return tuple(segment_index for segment_index in known if segment_index in values)


def _competencies(movement: str, codes: tuple, catalogue: RestrictedSupportCatalogue) -> tuple:
    allowed = [competency.competency_code for competency in catalogue.competencies]
    if not codes:
        raise _proposal_fail("competency_codes", "au moins une compétence du catalogue restreint attendue")
    for index, code in enumerate(codes):
        if type(code) is not str or code not in allowed:
            raise _proposal_fail(f"competency_codes[{index}]",
                                 f"{code!r} hors du catalogue restreint {allowed} (aucune compétence hors 6.1)")
    if len(set(codes)) != len(codes):
        raise _proposal_fail("competency_codes", "compétence en double")
    if movement == INTEGRATE and len(codes) == 1:
        raise _proposal_fail("competency_codes", f"{INTEGRATE} : au moins deux compétences distinctes attendues")
    return tuple(competency for competency in catalogue.competencies if competency.competency_code in codes)


def _capabilities(tokens: tuple, chosen: tuple, catalogue: RestrictedSupportCatalogue) -> tuple[uuid.UUID, ...]:
    owners = {capability.capability_token: competency.competency_code
              for competency in catalogue.competencies for capability in competency.capabilities}
    declared = [competency.competency_code for competency in chosen]
    for index, token in enumerate(tokens):
        if type(token) is not str or token not in owners:
            raise _proposal_fail(f"capability_tokens[{index}]", f"{token!r} absent du catalogue restreint (aucune "
                                                                "capacité hors du périmètre 6.1)")
        if owners[token] not in declared:
            raise _proposal_fail(f"capability_tokens[{index}]", f"{token} appartient à {owners[token]}, compétence "
                                                                f"non déclarée {declared}")
    if len(set(tokens)) != len(tokens):
        raise _proposal_fail("capability_tokens", "token en double")
    for competency in chosen:
        if competency.scope_mode == LOCALIZED and not any(owners[token] == competency.competency_code
                                                          for token in tokens):
            raise _proposal_fail("capability_tokens", f"{competency.competency_code} {LOCALIZED} sans capacité "
                                                      "(jamais élargi à la compétence entière)")
    return tuple(capability.definition_id for competency in chosen for capability in competency.capabilities
                 if capability.capability_token in tokens)


def _intent(value) -> str:
    if type(value) is not str or not value.strip():
        raise _proposal_fail("movement_intent", f"texte non vide attendu hors {STAY}, reçu {value!r}")
    if len(value) > MAX_MOVEMENT_INTENT_CHARS:
        raise _proposal_fail("movement_intent", f"{len(value)} caractères pour {MAX_MOVEMENT_INTENT_CHARS} au plus "
                                                "(jamais tronqué)")
    return value


def _validated(proposal, inputs: dict, planning: MovementPlanningInput,
               catalogue: RestrictedSupportCatalogue) -> InteractionPedagogicalMovement:
    if type(proposal) is not MovementProposal:
        raise InvalidMovementArgument(f"MovementProposal attendue, reçu {type(proposal).__name__}")
    for name, expected in (("schema_version", MOVEMENT_SCHEMA_VERSION), ("policy_version", MOVEMENT_POLICY_VERSION)):
        value = getattr(proposal, name)
        if type(value) is not str or value != expected:
            raise _proposal_fail(name, f"{value!r} au lieu de {expected!r}")
    movement = proposal.movement
    if type(movement) is not str or movement not in PEDAGOGICAL_MOVEMENTS:
        raise _proposal_fail("movement", f"{movement!r} hors vocabulaire {list(PEDAGOGICAL_MOVEMENTS)}")
    indices = _tuple(proposal.segment_indices, "segment_indices")
    codes = _tuple(proposal.competency_codes, "competency_codes")
    tokens = _tuple(proposal.capability_tokens, "capability_tokens")
    intent = proposal.movement_intent
    if movement == STAY:
        if indices or codes or tokens or intent is not None:
            raise _proposal_fail(STAY, "aucun segment, aucune compétence, aucun token ni aucune intention attendus")
        anchor = None
    else:
        if planning.request_status == NO_TASK:
            raise _proposal_fail("movement", f"{NO_TASK} : seul {STAY} est valide")
        if planning.pedagogical_resolution_status != RESOLVED:
            raise _proposal_fail("movement", f"focus {planning.pedagogical_resolution_status} : seul {STAY} est "
                                             f"valide en {MOVEMENT_POLICY_VERSION} (aucun incrément personnalisé "
                                             "sans périmètre pédagogique résolu)")
        chosen = _competencies(movement, codes, catalogue)
        anchor = MovementAnchor(
            segment_indices=_segment_indices(indices, planning),
            competency_codes=tuple(competency.competency_code for competency in chosen),
            capability_definition_ids=_capabilities(tokens, chosen, catalogue),
        )
        intent = _intent(intent)
    task, posture, plan = inputs["task_profile"], inputs["posture_baseline"], inputs["support_plan"]
    return InteractionPedagogicalMovement(
        schema_version=MOVEMENT_SCHEMA_VERSION,
        policy_version=MOVEMENT_POLICY_VERSION,
        source_task_schema_version=task.schema_version,
        source_task_policy_version=task.policy_version,
        source_posture_schema_version=posture.schema_version,
        source_posture_policy_version=posture.policy_version,
        source_support_schema_version=plan.schema_version,
        source_support_policy_version=plan.policy_version,
        source_pedagogical_context_schema_version=inputs["context"].schema_version,
        request_status=planning.request_status,
        pedagogical_resolution_status=planning.pedagogical_resolution_status,
        movement=movement,
        anchor=anchor,
        movement_intent=intent,
    )


# --------------------------------------------------------------------------
# Revérification d'un mouvement fourni (projection)
# --------------------------------------------------------------------------

_HEADER_FIELDS = ("schema_version", "policy_version", "source_task_schema_version", "source_task_policy_version",
                  "source_posture_schema_version", "source_posture_policy_version", "source_support_schema_version",
                  "source_support_policy_version", "source_pedagogical_context_schema_version", "request_status",
                  "pedagogical_resolution_status")


def _rechecked(movement, inputs: dict, planning: MovementPlanningInput,
               catalogue: RestrictedSupportCatalogue) -> InteractionPedagogicalMovement:
    """Le mouvement reçu doit être EXACTEMENT celui que la porte de
    validation produit, pour ces entrées, depuis la proposition équivalente
    (même doctrine que le préflight canonique de 6-2C : vérifier, jamais
    réparer)."""
    if type(movement) is not InteractionPedagogicalMovement:
        raise InvalidMovementArgument(f"InteractionPedagogicalMovement attendu, reçu {type(movement).__name__}")
    for name in _HEADER_FIELDS:
        if type(getattr(movement, name)) is not str:
            raise InvalidMovementContent(f"{name} : str exact attendu")
    for name, expected in (("schema_version", MOVEMENT_SCHEMA_VERSION), ("policy_version", MOVEMENT_POLICY_VERSION)):
        if getattr(movement, name) != expected:
            raise UnsupportedMovementVersion(f"{name} {getattr(movement, name)!r} non supportée "
                                             f"({expected!r} attendue)")
    anchor = movement.anchor
    if anchor is None:
        indices, codes, tokens = (), (), ()
    elif type(anchor) is not MovementAnchor:
        raise InvalidMovementContent(f"anchor : MovementAnchor ou None attendu, reçu {type(anchor).__name__}")
    else:
        ids = anchor.capability_definition_ids
        if type(ids) is not tuple or any(type(i) is not uuid.UUID for i in ids):
            raise InvalidMovementContent("anchor.capability_definition_ids : tuple de uuid.UUID attendu")
        tokens_by_id = {capability.definition_id: capability.capability_token
                        for competency in catalogue.competencies for capability in competency.capabilities}
        unknown = [str(i) for i in ids if i not in tokens_by_id]
        if unknown:
            raise _incompatible("anchor.capability_definition_ids", f"{unknown} hors du catalogue restreint")
        indices, codes, tokens = anchor.segment_indices, anchor.competency_codes, tuple(tokens_by_id[i] for i in ids)
    try:
        expected = _validated(MovementProposal(
            schema_version=MOVEMENT_SCHEMA_VERSION, policy_version=MOVEMENT_POLICY_VERSION,
            movement=movement.movement, segment_indices=indices, competency_codes=codes, capability_tokens=tokens,
            movement_intent=movement.movement_intent), inputs, planning, catalogue)
    except InvalidMovementProposal as exc:
        raise InvalidMovementContent(f"mouvement : {exc}") from exc
    if movement != expected:
        diverging = [name for name in (*_HEADER_FIELDS, "anchor")
                     if getattr(movement, name) != getattr(expected, name)]
        raise _incompatible(f"mouvement {diverging}", "versions source, statuts ou ordre canonique différents de ceux"
                                                      " de ces entrées (jamais corrigé)")
    return expected


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def prepare_movement_planning(
    *,
    task_profile: InteractionTaskProfile,
    posture_baseline: InteractionPostureBaseline,
    support_plan: InteractionSupportPlan,
    context: PedagogicalResponseContext,
    taxonomy: CurrentFocusTaxonomy,
) -> MovementPlanningInput:
    """Préflight DÉTERMINISTE de 6.3 : revérifie que tâche, posture, support,
    contexte et taxonomie décrivent la MÊME interaction (frontières publiques
    de 6-2C réutilisées, puis appariement support <-> tâche / posture /
    contexte) et construit l'entrée minimale de 6-3B. Fonction PURE.

    Erreurs : InvalidMovementArgument, UnsupportedMovementVersion,
    IncompatibleMovementInputs, InvalidMovementContent (erreurs amont
    chaînées). Aucun appel modèle ; rien n'est corrigé."""
    return _prepared(dict(task_profile=task_profile, posture_baseline=posture_baseline, support_plan=support_plan,
                          context=context, taxonomy=taxonomy))[0]


def validate_movement_proposal(
    proposal: MovementProposal,
    *,
    task_profile: InteractionTaskProfile,
    posture_baseline: InteractionPostureBaseline,
    support_plan: InteractionSupportPlan,
    context: PedagogicalResponseContext,
    taxonomy: CurrentFocusTaxonomy,
) -> InteractionPedagogicalMovement:
    """MovementProposal NON FIABLE -> InteractionPedagogicalMovement validé.
    Fonction PURE : mêmes entrées + même proposition => même mouvement.

    Le préflight complet est rejoué sur les entrées reçues. Puis : versions
    exactes ; mouvement du vocabulaire fermé ; stay sans ancrage ni
    intention ; no_task ou focus non résolu => seul stay ; sinon au moins un
    segment existant, au moins une compétence du catalogue restreint (deux
    pour integrate), tokens du périmètre 6.1 exact des compétences déclarées
    (au moins un par compétence localized, aucun pour competency_only),
    movement_intent non vide et borné. Ensembles canonicalisés ; doublon ou
    valeur hors périmètre => InvalidMovementProposal ; jamais un mouvement
    partiel ni corrigé, jamais un stay de repli."""
    inputs = dict(task_profile=task_profile, posture_baseline=posture_baseline, support_plan=support_plan,
                  context=context, taxonomy=taxonomy)
    planning, catalogue = _prepared(inputs)
    return _validated(proposal, inputs, planning, catalogue)


def project_pedagogical_movement(
    movement: InteractionPedagogicalMovement,
    *,
    task_profile: InteractionTaskProfile,
    posture_baseline: InteractionPostureBaseline,
    support_plan: InteractionSupportPlan,
    context: PedagogicalResponseContext,
    taxonomy: CurrentFocusTaxonomy,
) -> MovementGenerationProjection:
    """Projection de génération d'un mouvement validé, avec les MÊMES entrées
    (préflight rejoué, mouvement revérifié et apparié : compatibilité, jamais
    authenticité). Fonction PURE et déterministe ; aucun UUID, aucune
    provenance, aucun stade, aucune contrainte de validation en sortie."""
    inputs = dict(task_profile=task_profile, posture_baseline=posture_baseline, support_plan=support_plan,
                  context=context, taxonomy=taxonomy)
    planning, catalogue = _prepared(inputs)
    checked = _rechecked(movement, inputs, planning, catalogue)
    anchor = checked.anchor
    if anchor is None:
        indices, competencies = (), ()
    else:
        indices = anchor.segment_indices
        competencies = tuple(ProjectedMovementCompetency(
            competency_code=competency.competency_code,
            competency_label=competency.label,
            capabilities=tuple(ProjectedCapability(capability_token=capability.capability_token,
                                                   label=capability.label, definition=capability.definition)
                               for capability in competency.capabilities
                               if capability.definition_id in anchor.capability_definition_ids),
        ) for competency in catalogue.competencies if competency.competency_code in anchor.competency_codes)
    return MovementGenerationProjection(
        schema_version=checked.schema_version,
        policy_version=checked.policy_version,
        movement=checked.movement,
        movement_intent=checked.movement_intent,
        segment_indices=indices,
        competencies=competencies,
    )
