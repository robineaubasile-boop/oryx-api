"""Étape 6.2C1 : CALIBRATION DU SUPPORT pédagogique, segment par segment
(InteractionSupportPlan) — contrats, préflight, catalogue restreint,
validateur DÉTERMINISTE et projection de génération.

Question de 6-2C, et seulement elle : « pour chaque segment de la demande
courante, sous la posture déjà décidée, quels présupposés sûrs sont
réellement utiles, quels éléments du focus doivent être rendus explicitement
disponibles comme ponts conceptuels, et comment répartir le travail cognitif
de la tâche entre Oryx et l'utilisateur ? ». Ce module en porte les contrats
et les portes déterministes ; la proposition sémantique vient du
classificateur 6-2C2 (core/adaptation_support_classifier.py) :

    6-2A InteractionTaskProfile + 6-2B InteractionPostureBaseline
    + 6-1E PedagogicalResponseContext + 6-1B CurrentFocusTaxonomy
        -> prepare_support_planning (préflight) -> SupportPlanningInput
           (segments + RestrictedSupportCatalogue, seule matière de 6-2C2)
    SupportPlanProposal (NON FIABLE, sortie de 6-2C2) + les MÊMES entrées
        -> validate_support_plan_proposal -> InteractionSupportPlan (validé)
    InteractionSupportPlan + RestrictedSupportCatalogue
        -> project_support_plan -> SupportGenerationProjection

Frontière constitutionnelle. 6-2C ne choisit ni le stade de l'utilisateur,
ni une compétence cible, ni les présupposés sûrs (6-1D), ni la posture
(6-2B), ni un mouvement 6.3, ni une surface produit ; il n'active aucune
revalidation Step 5, ne génère aucun texte, ne crée ni observation, ni
preuve, ni trace de support réel. « Décrire, jamais prescrire. » La demande
explicite domine ; répondre est prioritaire sur tester ; questionner est un
outil, jamais une doctrine.

Invariants :

- Versions explicites et distinctes : SUPPORT_PLAN_SCHEMA_VERSION (structure
  des contrats), SUPPORT_POLICY_VERSION (vocabulaire d'allocation, règles de
  validation par posture) ; la politique du prompt 6-2C2 a sa propre
  version. Les versions des entrées sont vérifiées EXACTEMENT ; aucune
  compatibilité implicite.

- Préflight tâche <-> posture : le profil 6-2A est revérifié par la frontière
  publique de 6-2B (build_posture_baseline) ; le baseline reçu doit décrire
  EXACTEMENT la même interaction (versions source, request_status, nombre
  de segments, segment_index canonique, source_excerpt identique, posture et
  selection_basis des vocabulaires fermés) puis être IDENTIQUE au baseline
  que 6-2B produit pour ce profil (même doctrine que le préflight canonique
  de 6-1D). Ce contrôle vérifie, il ne répare jamais : un baseline divergent
  est refusé, jamais remplacé par la posture recalculée.

- Préflight contexte <-> taxonomie : le contexte canonique 6-1E est
  revérifié et projeté par sa frontière publique (project_pedagogical_context),
  la taxonomie par celle de 6-1B (validate_focus_proposal, proposition
  neutral synthétique). Puis identité EXACTE : taxonomy_release_id,
  taxonomy_spec_fingerprint et focus_policy_version de l'enveloppe ==
  ceux du catalogue ; chaque definition_id du contexte existe dans la
  taxonomie, appartient à la compétence annoncée, dans l'ordre taxonomique.
  Aucun rapprochement par competency_code, libellé ou capability_code,
  aucun remapping entre releases.

- RestrictedSupportCatalogue : immuable, éphémère, déterministe ; construit
  UNIQUEMENT depuis la projection 6-1E validée et la taxonomie validée. Il ne
  contient QUE la cible puis les supports de 6.1 (aucune autre compétence
  C1-C12, aucune autre capacité des 45) ; localized : uniquement les
  capacités du périmètre exact, chacune avec son token sémantique, son sens
  (libellé, définition, mapping_guidance) et ses safe_stages EXACTS (le
  SafeAssumptionCoverage correspondant) ; competency_only : aucun token
  inventé, la couverture competency_only (definition_id None) conservée.
  Ni contrainte de validation, ni provenance Step 5, ni identité de
  personne. Focus non résolu (neutral / ambiguous / composite) : aucune
  compétence, donc aucun présupposé ni pont ; la répartition cognitive
  reste possible (absence de personnalisation C1-C12 != absence de
  posture).

- Proposition NON FIABLE (SupportPlanProposal) : désignations par
  competency_code + scope_mode + tokens sémantiques ; jamais d'UUID, jamais
  de safe_stages, ni rationale, ni score, ni niveau, ni mouvement, ni
  surface. Le validateur retrouve les definition_id et copie les
  safe_stages EXACTS de 6.1 : le modèle ne peut ni ajouter, ni retirer, ni
  réordonner un stade.

- Présupposé pertinent (RelevantSafeAssumption) = UN SafeAssumptionCoverage
  exact (une capacité localized, ou la couverture competency_only None),
  réellement utile au segment et portant au moins un stade sûr. Pont
  conceptuel (ConceptualBridge) = périmètre déjà autorisé à rendre
  explicitement disponible parce qu'il est nécessaire au segment : jamais
  une faiblesse, un diagnostic, un prérequis, une remédiation ni une preuve.
  Un support 6.1 n'est jamais un pont automatique ; un même périmètre peut
  être à la fois présupposé et pont. Le savoir spécialisé hors C1-C12
  appartient à la tâche (opérations allouées), jamais à un pont inventé.

- Allocation cognitive : vocabulaire fermé OPERATION_ALLOCATIONS (oryx,
  user_reserved, joint) sur les COGNITIVE_OPERATIONS EXACTES du segment 6-2A
  (aucune opération inventée, aucun doublon) ; une opération non allouée
  n'exige aucune attribution particulière. Règles par posture :
    explain   : toute allocation explicite est oryx (jamais user_reserved
                ni joint : 6-2C ne transforme jamais explain en exercice) ;
    guide     : au moins une opération user_reserved, jamais joint ;
    co_reason : au moins une opération joint ;
    challenge : evaluate_existing_reasoning / stress_test / falsify, s'ils
                sont alloués, le sont à oryx ou joint (jamais réservés à
                l'utilisateur : Oryx participe réellement au challenge).
  Aucune notion de « quantité » d'opération n'est codée : le choix
  sémantique appartient à 6-2C2, le validateur vérifie l'appartenance et
  les règles.

- Gap QUALITATIF : aucun score, aucune distance de stade, aucun niveau de
  support, aucun calcul sur les stades. Les stades sûrs sont un PLANCHER de
  présupposés, jamais un plafond ; aucun présupposé n'est un diagnostic
  d'incapacité.

- Fail-closed : InvalidSupportPlanArgument (types top-level),
  UnsupportedSupportPlanVersion (version d'entrée non supportée),
  IncompatibleSupportPlanInputs (entrées non appariées),
  InvalidSupportPlanContent (entrée, plan ou catalogue structurellement
  incohérent), InvalidSupportPlanProposal (proposition refusée). Les
  erreurs des frontières amont sont chaînées. Jamais de plan partiel, jamais
  de correction sémantique, jamais de repli explain ni « sans
  personnalisation » sur corruption.

- PUR, éphémère, sans valeur probante : ni base, ni horloge, ni
  environnement, ni hasard, ni réseau, ni modèle, ni persistance. Mêmes
  entrées + même proposition => même plan. Le plan prévu n'est PAS le
  support réellement fourni (seul un système postérieur à la génération
  pourra tracer le support effectivement émis) : moins de support prévu ne
  prouve aucune autonomie, plus de support prévu aucune faiblesse.

- Projection de génération (SupportGenerationProjection) : dérivée du plan
  validé et du catalogue correspondant (revérifiés, appariés) ; sens
  sémantique seulement (codes, libellés, tokens, définitions, stades sûrs
  exacts, allocations) ; aucun UUID, aucune release, aucun fingerprint,
  aucune provenance, aucune contrainte de validation.

Ordres canoniques : segments dans l'ordre de 6-2A ; compétences cible puis
supports (ordre 6.1) ; capacités dans l'ordre du focus (taxonomique, jamais
un tri d'UUID) ; présupposés et ponts dans l'ordre du catalogue ;
allocations dans l'ordre de COGNITIVE_OPERATIONS. L'ordre du JSON proposé
n'est jamais une décision pédagogique.
"""
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from core.adaptation_assumptions import ASSUMPTION_STAGE_ORDER, SUPPORTING, TARGET
from core.adaptation_context import (
    PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
    PedagogicalResponseContext,
    PedagogicalResponseContextError,
    UnsupportedResponseContextVersion,
    project_pedagogical_context,
)
from core.adaptation_focus import (
    COMPETENCY_ONLY,
    FOCUS_SCHEMA_VERSION,
    LOCALIZED,
    NEUTRAL,
    RESOLUTION_STATUSES,
    RESOLVED,
    SCOPE_MODES,
    CurrentFocusTaxonomy,
    FocusError,
    FocusProposal,
    UnsupportedFocusPolicy,
    validate_focus_proposal,
)
from core.adaptation_posture import (
    CHALLENGE,
    CHALLENGE_TASK_OPERATIONS,
    CO_REASON,
    EXPLAIN,
    GUIDE,
    POSTURE_BASELINE_SCHEMA_VERSION,
    POSTURE_POLICY_VERSION,
    POSTURES,
    SELECTION_BASES,
    InteractionPostureBaseline,
    PostureBaselineError,
    SegmentPostureDecision,
    UnsupportedTaskProfileVersion,
    build_posture_baseline,
)
from core.adaptation_task import (
    COGNITIVE_OPERATIONS,
    NO_TASK,
    REQUEST_STATUSES,
    TASK_POLICY_VERSION,
    TASK_SCHEMA_VERSION,
    InteractionTaskProfile,
)

SUPPORT_PLAN_SCHEMA_VERSION = "interaction-support-plan-v1"
SUPPORT_POLICY_VERSION = "support-policy-1"

# ProposedOperationAllocation.allocation / PlannedOperationAllocation.allocation
ORYX = "oryx"
USER_RESERVED = "user_reserved"
JOINT = "joint"
OPERATION_ALLOCATIONS = (ORYX, USER_RESERVED, JOINT)


class SupportPlanError(Exception):
    """Erreur métier de 6-2C : jamais un plan partiel, jamais une correction
    sémantique, jamais un repli explain ou « sans personnalisation »."""


class InvalidSupportPlanArgument(SupportPlanError):
    """Argument top-level d'un type inattendu."""


class UnsupportedSupportPlanVersion(SupportPlanError):
    """Version d'une entrée (6-2A, 6-2B, 6-1E, 6-1B) ou d'un plan non
    supportée par cette policy."""


class IncompatibleSupportPlanInputs(SupportPlanError):
    """Entrées valides isolément mais non appariées (tâche / posture,
    contexte / taxonomie, plan / catalogue) : jamais rapprochées."""


class InvalidSupportPlanContent(SupportPlanError):
    """Entrée, plan ou catalogue structurellement incohérent (jamais
    corrigé)."""


class InvalidSupportPlanProposal(SupportPlanError):
    """Proposition NON FIABLE refusée (jamais corrigée)."""


# --------------------------------------------------------------------------
# Catalogue restreint et entrée de 6-2C2 (sortie du préflight)
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class CatalogueCapability:
    """Une capacité AUTORISÉE par 6.1 (périmètre localized exact) : identité
    machine (definition_id, jamais montrée au modèle), token sémantique, sens
    et safe_stages EXACTS de son SafeAssumptionCoverage."""
    definition_id: uuid.UUID
    capability_token: str
    label: str
    definition: str
    mapping_guidance: Mapping
    safe_stages: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CatalogueCompetency:
    """Une compétence AUTORISÉE par 6.1 (cible ou support). localized :
    capacités du périmètre exact, competency_only_safe_stages None ;
    competency_only : aucune capacité, safe_stages de la couverture
    competency_only (definition_id None)."""
    role: str
    competency_code: str
    label: str
    central_question: str
    scope_mode: str
    capabilities: tuple[CatalogueCapability, ...]
    competency_only_safe_stages: tuple[str, ...] | None


@dataclass(frozen=True, kw_only=True)
class RestrictedSupportCatalogue:
    """Seul catalogue sémantique visible par 6-2C2 : cible puis supports de
    6.1, rien d'autre. Vide si le focus n'est pas résolu. Éphémère."""
    policy_version: str
    resolution_status: str
    competencies: tuple[CatalogueCompetency, ...]


@dataclass(frozen=True, kw_only=True)
class SupportSegmentInput:
    """Un segment à planifier : ancrage et exigences 6-2A, posture 6-2B."""
    segment_index: int
    source_excerpt: str
    explicit_intent: str
    cognitive_operations: tuple[str, ...]
    task_characteristics: tuple[str, ...]
    posture: str


@dataclass(frozen=True, kw_only=True)
class SupportPlanningInput:
    """Sortie du préflight : seule matière sémantique de 6-2C2 (aucun
    identifiant de personne, aucune provenance, aucune contrainte)."""
    request_status: str
    segments: tuple[SupportSegmentInput, ...]
    catalogue: RestrictedSupportCatalogue


# --------------------------------------------------------------------------
# Proposition NON FIABLE
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ProposedPedagogicalReference:
    """Périmètre du catalogue désigné par la proposition : tokens
    sémantiques (localized) ou aucun (competency_only). Jamais d'UUID ni de
    stade."""
    competency_code: str
    scope_mode: str
    capability_tokens: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ProposedOperationAllocation:
    operation: str
    allocation: str


@dataclass(frozen=True, kw_only=True)
class SegmentSupportProposal:
    segment_index: int
    relevant_safe_assumptions: tuple[ProposedPedagogicalReference, ...]
    conceptual_bridges: tuple[ProposedPedagogicalReference, ...]
    operation_allocations: tuple[ProposedOperationAllocation, ...]


@dataclass(frozen=True, kw_only=True)
class SupportPlanProposal:
    """Proposition NON FIABLE (sortie de 6-2C2) : inutilisable avant
    validate_support_plan_proposal."""
    schema_version: str
    policy_version: str
    segments: tuple[SegmentSupportProposal, ...]


# --------------------------------------------------------------------------
# Plan validé
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class RelevantSafeAssumption:
    """UN SafeAssumptionCoverage exact de 6.1 jugé utile au segment :
    capacité localized (definition_id) ou couverture competency_only (None) ;
    safe_stages copiés tels quels."""
    competency_code: str
    capability_definition_id: uuid.UUID | None
    safe_stages: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ConceptualBridge:
    """Périmètre déjà autorisé à rendre explicitement disponible pour CE
    segment. Jamais un diagnostic, un prérequis ni une preuve."""
    competency_code: str
    scope_mode: str
    capability_definition_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, kw_only=True)
class PlannedOperationAllocation:
    operation: str
    allocation: str


@dataclass(frozen=True, kw_only=True)
class SegmentSupportPlan:
    segment_index: int
    source_excerpt: str
    posture: str

    relevant_safe_assumptions: tuple[RelevantSafeAssumption, ...]
    conceptual_bridges: tuple[ConceptualBridge, ...]
    operation_allocations: tuple[PlannedOperationAllocation, ...]


@dataclass(frozen=True, kw_only=True)
class InteractionSupportPlan:
    """Support PRÉVU du tour courant, segment par segment. Éphémère, sans
    valeur probante, jamais une trace du support réellement fourni."""
    schema_version: str
    policy_version: str

    source_task_schema_version: str
    source_task_policy_version: str

    source_posture_schema_version: str
    source_posture_policy_version: str

    source_pedagogical_context_schema_version: str

    request_status: str
    pedagogical_resolution_status: str

    segments: tuple[SegmentSupportPlan, ...]


# --------------------------------------------------------------------------
# Projection de génération
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ProjectedCapability:
    capability_token: str
    label: str
    definition: str


@dataclass(frozen=True, kw_only=True)
class ProjectedSafeAssumption:
    competency_code: str
    competency_label: str
    capability: ProjectedCapability | None
    safe_stages: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ProjectedConceptualBridge:
    competency_code: str
    competency_label: str
    scope_mode: str
    capabilities: tuple[ProjectedCapability, ...]


@dataclass(frozen=True, kw_only=True)
class SegmentSupportProjection:
    segment_index: int
    source_excerpt: str
    posture: str

    relevant_safe_assumptions: tuple[ProjectedSafeAssumption, ...]
    conceptual_bridges: tuple[ProjectedConceptualBridge, ...]
    operation_allocations: tuple[PlannedOperationAllocation, ...]


@dataclass(frozen=True, kw_only=True)
class SupportGenerationProjection:
    """Vue minimale du plan pour le futur générateur : sens sémantique
    seulement, aucun UUID, aucune provenance. Éphémère, sans valeur
    probante."""
    schema_version: str
    policy_version: str
    request_status: str
    segments: tuple[SegmentSupportProjection, ...]


# --------------------------------------------------------------------------
# Outils de contrôle (purs)
# --------------------------------------------------------------------------

def _content_fail(path: str, message: str) -> InvalidSupportPlanContent:
    return InvalidSupportPlanContent(f"{path} : {message}")


def _incompatible(path: str, message: str) -> IncompatibleSupportPlanInputs:
    return IncompatibleSupportPlanInputs(f"{path} : {message} (aucun rapprochement)")


def _supported(value, path: str, expected: str) -> None:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value != expected:
        raise UnsupportedSupportPlanVersion(f"{path} {value!r} non supportée ({expected!r} attendue)")


def _is_index(value, position: int) -> bool:
    return type(value) is int and value == position


def _posture_violation(posture: str, allocations: tuple) -> str | None:
    """Règle de SUPPORT_POLICY_VERSION pour UNE posture ; None si respectée.
    allocations : PlannedOperationAllocation déjà dans le vocabulaire."""
    given = [allocation.allocation for allocation in allocations]
    if posture == EXPLAIN and any(value != ORYX for value in given):
        return f"{EXPLAIN} : toute opération allouée l'est à {ORYX} (aucun travail réservé ni partagé)"
    if posture == GUIDE:
        if JOINT in given:
            return f"{GUIDE} : {JOINT} interdit en {SUPPORT_POLICY_VERSION}"
        if USER_RESERVED not in given:
            return f"{GUIDE} : au moins une opération {USER_RESERVED} attendue"
    if posture == CO_REASON and JOINT not in given:
        return f"{CO_REASON} : au moins une opération {JOINT} attendue"
    if posture == CHALLENGE:
        reserved = [allocation.operation for allocation in allocations
                    if allocation.operation in CHALLENGE_TASK_OPERATIONS and allocation.allocation == USER_RESERVED]
        if reserved:
            return f"{CHALLENGE} : {reserved} réservé(s) à l'utilisateur ({ORYX} ou {JOINT} attendu)"
    return None


# --------------------------------------------------------------------------
# Préflight tâche <-> posture
# --------------------------------------------------------------------------

def _canonical_posture(task_profile: InteractionTaskProfile) -> InteractionPostureBaseline:
    """Profil 6-2A revérifié par la frontière publique de 6-2B ; ses erreurs
    sont chaînées."""
    try:
        return build_posture_baseline(task_profile)
    except UnsupportedTaskProfileVersion as exc:
        raise UnsupportedSupportPlanVersion(f"profil 6-2A : {exc}") from exc
    except PostureBaselineError as exc:
        raise InvalidSupportPlanContent(f"profil 6-2A : {exc}") from exc


def _check_task_and_posture(task_profile: InteractionTaskProfile,
                            posture_baseline: InteractionPostureBaseline) -> None:
    expected = _canonical_posture(task_profile)
    _supported(posture_baseline.schema_version, "posture.schema_version", POSTURE_BASELINE_SCHEMA_VERSION)
    _supported(posture_baseline.policy_version, "posture.policy_version", POSTURE_POLICY_VERSION)
    for name, task_value in (("schema_version", task_profile.schema_version),
                             ("policy_version", task_profile.policy_version)):
        value = getattr(posture_baseline, f"source_task_{name}")
        if type(value) is not str or value != task_value:
            raise _incompatible(f"posture.source_task_{name}", f"{value!r} pour un profil {task_value!r}")
    if type(posture_baseline.request_status) is not str or posture_baseline.request_status != (
            task_profile.request_status):
        raise _incompatible("request_status", f"posture {posture_baseline.request_status!r}, tâche "
                                              f"{task_profile.request_status!r}")
    decisions = posture_baseline.segments
    if type(decisions) is not tuple:
        raise _content_fail("posture.segments", f"tuple attendu, reçu {type(decisions).__name__}")
    if len(decisions) != len(task_profile.segments):
        raise _incompatible("segments", f"{len(decisions)} décision(s) de posture pour "
                                        f"{len(task_profile.segments)} segment(s) de tâche")
    for index, (decision, segment) in enumerate(zip(decisions, task_profile.segments)):
        path = f"posture.segments[{index}]"
        if type(decision) is not SegmentPostureDecision:
            raise _content_fail(path, f"SegmentPostureDecision attendue, reçu {type(decision).__name__}")
        if not _is_index(decision.segment_index, index):
            raise _incompatible(f"{path}.segment_index", f"{decision.segment_index!r} au lieu de {index}")
        if type(decision.source_excerpt) is not str or decision.source_excerpt != segment.source_excerpt:
            raise _incompatible(f"{path}.source_excerpt", "extrait différent du segment 6-2A")
        if type(decision.posture) is not str or decision.posture not in POSTURES:
            raise _content_fail(f"{path}.posture", f"{decision.posture!r} hors vocabulaire {list(POSTURES)}")
        if type(decision.selection_basis) is not str or decision.selection_basis not in SELECTION_BASES:
            raise _content_fail(f"{path}.selection_basis",
                                f"{decision.selection_basis!r} hors vocabulaire {list(SELECTION_BASES)}")
    if posture_baseline != expected:
        diverging = [index for index, (got, wanted) in enumerate(zip(decisions, expected.segments)) if got != wanted]
        raise _incompatible(f"posture.segments{diverging}",
                            f"posture ou selection_basis hors des règles de {POSTURE_POLICY_VERSION} pour ce"
                            " profil (jamais recalculée à la place de 6-2B)")


# --------------------------------------------------------------------------
# Préflight contexte <-> taxonomie, catalogue restreint
# --------------------------------------------------------------------------

def _projected_context(context: PedagogicalResponseContext):
    try:
        return project_pedagogical_context(context)
    except UnsupportedResponseContextVersion as exc:
        raise UnsupportedSupportPlanVersion(f"contexte 6-1E : {exc}") from exc
    except PedagogicalResponseContextError as exc:
        raise InvalidSupportPlanContent(f"contexte 6-1E : {exc}") from exc


def _check_taxonomy(taxonomy: CurrentFocusTaxonomy) -> None:
    """Frontière publique 6-1B : catalogue revérifié contre la SPEC
    canonique de son identité (aucune base, aucun modèle)."""
    try:
        validate_focus_proposal(FocusProposal(
            schema_version=FOCUS_SCHEMA_VERSION,
            policy_version=taxonomy.focus_policy_version,
            resolution_status=NEUTRAL,
            target=None,
            supporting=(),
        ), taxonomy)
    except UnsupportedFocusPolicy as exc:
        raise UnsupportedSupportPlanVersion(f"taxonomie : {exc}") from exc
    except FocusError as exc:
        raise InvalidSupportPlanContent(f"taxonomie : {exc}") from exc


def _catalogue(context: PedagogicalResponseContext, taxonomy: CurrentFocusTaxonomy) -> RestrictedSupportCatalogue:
    projection = _projected_context(context)
    _check_taxonomy(taxonomy)
    envelope = context.envelope
    for name in ("taxonomy_release_id", "taxonomy_spec_fingerprint", "focus_policy_version"):
        if getattr(envelope, name) != getattr(taxonomy, name):
            raise _incompatible(name, f"contexte {getattr(envelope, name)!r}, taxonomie {getattr(taxonomy, name)!r}"
                                      " (aucun remapping entre releases)")
    competencies = {competency.competency_code: competency for competency in taxonomy.competencies}
    capabilities = {capability.definition_id: capability for capability in taxonomy.capabilities}
    position = {capability.definition_id: index for index, capability in enumerate(taxonomy.capabilities)}

    allowed = []
    parts = () if projection.target is None else (projection.target, *projection.supporting)
    for index, part in enumerate(parts):
        path = "target" if index == 0 else f"supporting[{index - 1}]"
        code = part.competency_code
        ids = part.capability_definition_ids
        for definition_id in ids:
            capability = capabilities.get(definition_id)
            if capability is None:
                raise _incompatible(path, f"definition_id {definition_id} absent de la taxonomie")
            if capability.competency_code != code:
                raise _incompatible(path, f"{capability.capability_code} ({definition_id}) n'appartient pas à {code}")
        if list(ids) != sorted(ids, key=position.__getitem__):
            raise _incompatible(path, "capacités hors de l'ordre taxonomique (jamais réordonnées)")
        coverages = {coverage.capability_definition_id: coverage.safe_stages for coverage in part.safe_assumptions}
        competency = competencies[code]
        allowed.append(CatalogueCompetency(
            role=part.role,
            competency_code=code,
            label=competency.label,
            central_question=competency.central_question,
            scope_mode=part.scope_mode,
            capabilities=tuple(CatalogueCapability(
                definition_id=definition_id,
                capability_token=f"{capabilities[definition_id].capability_code}"
                                 f"@r{capabilities[definition_id].semantic_revision}",
                label=capabilities[definition_id].label,
                definition=capabilities[definition_id].definition,
                mapping_guidance=capabilities[definition_id].mapping_guidance,
                safe_stages=coverages[definition_id],
            ) for definition_id in ids),
            competency_only_safe_stages=coverages[None] if part.scope_mode == COMPETENCY_ONLY else None,
        ))
    return RestrictedSupportCatalogue(policy_version=SUPPORT_POLICY_VERSION,
                                      resolution_status=projection.resolution_status,
                                      competencies=tuple(allowed))


def _preflight(task_profile, posture_baseline, context, taxonomy) -> SupportPlanningInput:
    for value, expected, name in ((task_profile, InteractionTaskProfile, "task_profile"),
                                  (posture_baseline, InteractionPostureBaseline, "posture_baseline"),
                                  (context, PedagogicalResponseContext, "context"),
                                  (taxonomy, CurrentFocusTaxonomy, "taxonomy")):
        if type(value) is not expected:
            raise InvalidSupportPlanArgument(f"{name} : {expected.__name__} attendu, reçu {type(value).__name__}")
    _check_task_and_posture(task_profile, posture_baseline)
    catalogue = _catalogue(context, taxonomy)
    return SupportPlanningInput(
        request_status=task_profile.request_status,
        segments=tuple(SupportSegmentInput(
            segment_index=index,
            source_excerpt=segment.source_excerpt,
            explicit_intent=segment.explicit_intent,
            cognitive_operations=segment.cognitive_operations,
            task_characteristics=segment.task_characteristics,
            posture=decision.posture,
        ) for index, (segment, decision) in enumerate(zip(task_profile.segments, posture_baseline.segments))),
        catalogue=catalogue,
    )


# --------------------------------------------------------------------------
# Validation de la proposition (pure)
# --------------------------------------------------------------------------

def _proposal_fail(path: str, message: str) -> InvalidSupportPlanProposal:
    return InvalidSupportPlanProposal(f"{path} : {message}")


def _tuple(value, path: str) -> tuple:
    if type(value) is not tuple:
        raise _proposal_fail(path, f"tuple attendu, reçu {type(value).__name__}")
    return value


def _reference(proposed, catalogue: RestrictedSupportCatalogue, path: str):
    """Référence NON FIABLE -> (compétence du catalogue, capacités
    désignées dans l'ordre du catalogue)."""
    if type(proposed) is not ProposedPedagogicalReference:
        raise _proposal_fail(path, f"ProposedPedagogicalReference attendue, reçu {type(proposed).__name__}")
    allowed = {competency.competency_code: competency for competency in catalogue.competencies}
    code = proposed.competency_code
    if type(code) is not str or code not in allowed:
        raise _proposal_fail(f"{path}.competency_code",
                             f"{code!r} hors du catalogue restreint {list(allowed)} (aucune compétence hors 6.1)")
    competency = allowed[code]
    if type(proposed.scope_mode) is not str or proposed.scope_mode != competency.scope_mode:
        raise _proposal_fail(f"{path}.scope_mode",
                             f"{proposed.scope_mode!r} au lieu du périmètre 6.1 {competency.scope_mode!r}")
    tokens = _tuple(proposed.capability_tokens, f"{path}.capability_tokens")
    if competency.scope_mode == COMPETENCY_ONLY:
        if tokens:
            raise _proposal_fail(path, f"{COMPETENCY_ONLY} avec {len(tokens)} token(s) (aucun token inventé)")
        return competency, ()
    if not tokens:
        raise _proposal_fail(path, f"{LOCALIZED} sans capacité")
    by_token = {capability.capability_token: capability for capability in competency.capabilities}
    owners = {capability.capability_token: other.competency_code
              for other in catalogue.competencies for capability in other.capabilities}
    for index, token in enumerate(tokens):
        if type(token) is not str or token not in by_token:
            where = (f"appartient à {owners[token]}, pas à {code}" if type(token) is str and token in owners
                     else "absent du catalogue restreint (aucune capacité hors du périmètre 6.1)")
            raise _proposal_fail(f"{path}.capability_tokens[{index}]", f"{token!r} {where}")
    if len(set(tokens)) != len(tokens):
        raise _proposal_fail(f"{path}.capability_tokens", "token en double")
    return competency, tuple(capability for capability in competency.capabilities
                             if capability.capability_token in tokens)


def _references(values, catalogue: RestrictedSupportCatalogue, path: str) -> list:
    """Références distinctes par compétence, dans l'ordre du catalogue."""
    resolved = {}
    for index, proposed in enumerate(_tuple(values, path)):
        competency, chosen = _reference(proposed, catalogue, f"{path}[{index}]")
        if competency.competency_code in resolved:
            raise _proposal_fail(f"{path}[{index}]", f"{competency.competency_code} en double (capacités d'une"
                                                     " compétence regroupées dans UNE référence)")
        resolved[competency.competency_code] = (competency, chosen)
    return [resolved[c.competency_code] for c in catalogue.competencies if c.competency_code in resolved]


def _safe_assumptions(values, catalogue, path: str) -> tuple[RelevantSafeAssumption, ...]:
    assumptions = []
    for competency, chosen in _references(values, catalogue, path):
        coverages = ([(capability.definition_id, capability.safe_stages, capability.capability_token)
                      for capability in chosen] if competency.scope_mode == LOCALIZED
                     else [(None, competency.competency_only_safe_stages, COMPETENCY_ONLY)])
        for definition_id, safe_stages, name in coverages:
            if not safe_stages:
                raise _proposal_fail(path, f"{competency.competency_code} {name} : aucun stade sûr à présupposer"
                                           " (ni présupposé ni diagnostic ; un pont reste possible)")
            assumptions.append(RelevantSafeAssumption(competency_code=competency.competency_code,
                                                      capability_definition_id=definition_id,
                                                      safe_stages=safe_stages))
    return tuple(assumptions)


def _bridges(values, catalogue, path: str) -> tuple[ConceptualBridge, ...]:
    return tuple(ConceptualBridge(competency_code=competency.competency_code, scope_mode=competency.scope_mode,
                                  capability_definition_ids=tuple(c.definition_id for c in chosen))
                 for competency, chosen in _references(values, catalogue, path))


def _allocations(values, segment: SupportSegmentInput, path: str) -> tuple[PlannedOperationAllocation, ...]:
    chosen = {}
    for index, proposed in enumerate(_tuple(values, path)):
        name = f"{path}[{index}]"
        if type(proposed) is not ProposedOperationAllocation:
            raise _proposal_fail(name, f"ProposedOperationAllocation attendue, reçu {type(proposed).__name__}")
        operation, allocation = proposed.operation, proposed.allocation
        if type(operation) is not str or operation not in COGNITIVE_OPERATIONS:
            raise _proposal_fail(f"{name}.operation", f"{operation!r} hors vocabulaire 6-2A")
        if operation not in segment.cognitive_operations:
            raise _proposal_fail(f"{name}.operation", f"{operation!r} absente du segment 6-2A "
                                                      f"{list(segment.cognitive_operations)}"
                                                      " (aucune opération inventée)")
        if type(allocation) is not str or allocation not in OPERATION_ALLOCATIONS:
            raise _proposal_fail(f"{name}.allocation", f"{allocation!r} hors vocabulaire {list(OPERATION_ALLOCATIONS)}")
        if operation in chosen:
            raise _proposal_fail(name, f"{operation!r} en double")
        chosen[operation] = allocation
    return tuple(PlannedOperationAllocation(operation=operation, allocation=chosen[operation])
                 for operation in COGNITIVE_OPERATIONS if operation in chosen)


def _segment_plan(proposed, segment: SupportSegmentInput, catalogue, path: str) -> SegmentSupportPlan:
    if type(proposed) is not SegmentSupportProposal:
        raise _proposal_fail(path, f"SegmentSupportProposal attendue, reçu {type(proposed).__name__}")
    if not _is_index(proposed.segment_index, segment.segment_index):
        raise _proposal_fail(f"{path}.segment_index", f"{proposed.segment_index!r} au lieu de {segment.segment_index}"
                                                      " (un segment par segment 6-2A, dans l'ordre)")
    allocations = _allocations(proposed.operation_allocations, segment, f"{path}.operation_allocations")
    violation = _posture_violation(segment.posture, allocations)
    if violation:
        raise _proposal_fail(path, violation)
    return SegmentSupportPlan(
        segment_index=segment.segment_index,
        source_excerpt=segment.source_excerpt,
        posture=segment.posture,
        relevant_safe_assumptions=_safe_assumptions(proposed.relevant_safe_assumptions, catalogue,
                                                    f"{path}.relevant_safe_assumptions"),
        conceptual_bridges=_bridges(proposed.conceptual_bridges, catalogue, f"{path}.conceptual_bridges"),
        operation_allocations=allocations,
    )


# --------------------------------------------------------------------------
# Revérification d'un plan et d'un catalogue fournis (projection)
# --------------------------------------------------------------------------

def _stages(value, path: str) -> tuple:
    if (type(value) is not tuple or any(type(s) is not str or s not in ASSUMPTION_STAGE_ORDER for s in value)
            or value != tuple(s for s in ASSUMPTION_STAGE_ORDER if s in value)):
        raise _content_fail(path, f"safe_stages {value!r} : stades distincts, ordre conceptuel attendus")
    return value


def _check_catalogue(catalogue: RestrictedSupportCatalogue) -> None:
    """Structure du catalogue (compatibilité, jamais authenticité : seul
    prepare_support_planning le relie à la taxonomie et au contexte)."""
    _supported(catalogue.policy_version, "catalogue.policy_version", SUPPORT_POLICY_VERSION)
    status = catalogue.resolution_status
    if type(status) is not str or status not in RESOLUTION_STATUSES:
        raise _content_fail("catalogue.resolution_status", f"{status!r} hors vocabulaire")
    competencies = catalogue.competencies
    if type(competencies) is not tuple or any(type(c) is not CatalogueCompetency for c in competencies):
        raise _content_fail("catalogue.competencies", "tuple de CatalogueCompetency attendu")
    if (status == RESOLVED) != bool(competencies):
        raise _content_fail("catalogue", f"{status} et {len(competencies)} compétence(s) incohérents")
    seen_codes, seen_ids, seen_tokens = set(), set(), set()
    for index, competency in enumerate(competencies):
        path = f"catalogue.competencies[{index}]"
        if competency.role != (TARGET if index == 0 else SUPPORTING):
            raise _content_fail(path, f"role {competency.role!r} (cible puis supports attendus)")
        for name in ("competency_code", "label", "central_question"):
            if type(getattr(competency, name)) is not str or not getattr(competency, name).strip():
                raise _content_fail(f"{path}.{name}", "texte non vide attendu")
        if competency.competency_code in seen_codes:
            raise _content_fail(path, f"{competency.competency_code} en double")
        seen_codes.add(competency.competency_code)
        if type(competency.scope_mode) is not str or competency.scope_mode not in SCOPE_MODES:
            raise _content_fail(f"{path}.scope_mode", f"{competency.scope_mode!r} hors vocabulaire")
        capabilities = competency.capabilities
        if type(capabilities) is not tuple or any(type(c) is not CatalogueCapability for c in capabilities):
            raise _content_fail(f"{path}.capabilities", "tuple de CatalogueCapability attendu")
        if competency.scope_mode == LOCALIZED:
            if not capabilities or competency.competency_only_safe_stages is not None:
                raise _content_fail(path, f"{LOCALIZED} : capacités et aucune couverture competency_only attendues")
        else:
            if capabilities:
                raise _content_fail(path, f"{COMPETENCY_ONLY} avec {len(capabilities)} capacité(s)")
            _stages(competency.competency_only_safe_stages, f"{path}.competency_only_safe_stages")
        for position, capability in enumerate(capabilities):
            name = f"{path}.capabilities[{position}]"
            if type(capability.definition_id) is not uuid.UUID or capability.definition_id in seen_ids:
                raise _content_fail(name, "definition_id uuid.UUID unique attendu")
            seen_ids.add(capability.definition_id)
            for field in ("capability_token", "label", "definition"):
                if type(getattr(capability, field)) is not str or not getattr(capability, field).strip():
                    raise _content_fail(f"{name}.{field}", "texte non vide attendu")
            if capability.capability_token in seen_tokens:
                raise _content_fail(name, f"{capability.capability_token} en double")
            seen_tokens.add(capability.capability_token)
            _stages(capability.safe_stages, f"{name}.safe_stages")


def _check_plan_segment(segment, index: int, catalogue: RestrictedSupportCatalogue) -> None:
    path = f"segments[{index}]"
    if type(segment) is not SegmentSupportPlan:
        raise _content_fail(path, f"SegmentSupportPlan attendu, reçu {type(segment).__name__}")
    if not _is_index(segment.segment_index, index):
        raise _content_fail(f"{path}.segment_index", f"{segment.segment_index!r} au lieu de {index}")
    if type(segment.source_excerpt) is not str or not segment.source_excerpt.strip():
        raise _content_fail(f"{path}.source_excerpt", "extrait non vide attendu")
    if type(segment.posture) is not str or segment.posture not in POSTURES:
        raise _content_fail(f"{path}.posture", f"{segment.posture!r} hors vocabulaire")

    # Ordre canonique attendu : compétences, puis capacités, du catalogue.
    order = []
    for competency in catalogue.competencies:
        order += ([((competency.competency_code, c.definition_id), c.safe_stages) for c in competency.capabilities]
                  or [((competency.competency_code, None), competency.competency_only_safe_stages)])
    assumptions = segment.relevant_safe_assumptions
    if type(assumptions) is not tuple or any(
            type(a) is not RelevantSafeAssumption or type(a.competency_code) is not str
            or type(a.capability_definition_id) not in (uuid.UUID, type(None)) for a in assumptions):
        raise _content_fail(f"{path}.relevant_safe_assumptions", "tuple de RelevantSafeAssumption attendu")
    keys = [(a.competency_code, a.capability_definition_id) for a in assumptions]
    expected = [(key, stages) for key, stages in order if key in keys]
    if len(set(keys)) != len(keys) or keys != [key for key, _ in expected]:
        raise _incompatible(f"{path}.relevant_safe_assumptions",
                            "périmètres hors du catalogue, en double ou hors de son ordre")
    for assumption, (_, stages) in zip(assumptions, expected):
        if assumption.safe_stages != stages or not stages:
            raise _incompatible(f"{path}.relevant_safe_assumptions",
                                f"{assumption.competency_code} : safe_stages différents de 6.1 ou vides")

    bridges = segment.conceptual_bridges
    if type(bridges) is not tuple or any(type(b) is not ConceptualBridge or type(b.competency_code) is not str
                                         for b in bridges):
        raise _content_fail(f"{path}.conceptual_bridges", "tuple de ConceptualBridge attendu")
    allowed = {competency.competency_code: competency for competency in catalogue.competencies}
    codes = [bridge.competency_code for bridge in bridges]
    if len(set(codes)) != len(codes) or codes != [c for c in allowed if c in codes]:
        raise _incompatible(f"{path}.conceptual_bridges", "compétences hors du catalogue, en double ou hors ordre")
    for bridge in bridges:
        competency = allowed[bridge.competency_code]
        ids = bridge.capability_definition_ids
        catalogued = [capability.definition_id for capability in competency.capabilities]
        if (bridge.scope_mode != competency.scope_mode or type(ids) is not tuple
                or (competency.scope_mode == LOCALIZED) != bool(ids)
                or list(ids) != [i for i in catalogued if i in ids] or len(set(ids)) != len(ids)):
            raise _incompatible(f"{path}.conceptual_bridges",
                                f"{bridge.competency_code} : périmètre hors du catalogue restreint")

    allocations = segment.operation_allocations
    if type(allocations) is not tuple or any(type(a) is not PlannedOperationAllocation for a in allocations):
        raise _content_fail(f"{path}.operation_allocations", "tuple de PlannedOperationAllocation attendu")
    operations = [allocation.operation for allocation in allocations]
    if (any(type(o) is not str or o not in COGNITIVE_OPERATIONS for o in operations)
            or any(type(a.allocation) is not str or a.allocation not in OPERATION_ALLOCATIONS for a in allocations)
            or operations != [o for o in COGNITIVE_OPERATIONS if o in operations]):
        raise _content_fail(f"{path}.operation_allocations", "vocabulaire, unicité ou ordre canonique violé")
    violation = _posture_violation(segment.posture, allocations)
    if violation:
        raise _content_fail(path, violation)


def _check_plan(plan: InteractionSupportPlan, catalogue: RestrictedSupportCatalogue) -> None:
    _supported(plan.schema_version, "plan.schema_version", SUPPORT_PLAN_SCHEMA_VERSION)
    _supported(plan.policy_version, "plan.policy_version", SUPPORT_POLICY_VERSION)
    _supported(plan.source_task_schema_version, "plan.source_task_schema_version", TASK_SCHEMA_VERSION)
    _supported(plan.source_task_policy_version, "plan.source_task_policy_version", TASK_POLICY_VERSION)
    _supported(plan.source_posture_schema_version, "plan.source_posture_schema_version",
               POSTURE_BASELINE_SCHEMA_VERSION)
    _supported(plan.source_posture_policy_version, "plan.source_posture_policy_version", POSTURE_POLICY_VERSION)
    _supported(plan.source_pedagogical_context_schema_version, "plan.source_pedagogical_context_schema_version",
               PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION)
    _check_catalogue(catalogue)
    if (type(plan.pedagogical_resolution_status) is not str
            or plan.pedagogical_resolution_status != catalogue.resolution_status):
        raise _incompatible("pedagogical_resolution_status",
                            f"plan {plan.pedagogical_resolution_status!r}, catalogue {catalogue.resolution_status!r}")
    status = plan.request_status
    if type(status) is not str or status not in REQUEST_STATUSES:
        raise _content_fail("request_status", f"{status!r} hors vocabulaire")
    segments = plan.segments
    if type(segments) is not tuple or (status == NO_TASK) == bool(segments):
        raise _content_fail("segments", f"{status} et {segments!r} incohérents")
    for index, segment in enumerate(segments):
        _check_plan_segment(segment, index, catalogue)


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def prepare_support_planning(
    *,
    task_profile: InteractionTaskProfile,
    posture_baseline: InteractionPostureBaseline,
    context: PedagogicalResponseContext,
    taxonomy: CurrentFocusTaxonomy,
) -> SupportPlanningInput:
    """Préflight DÉTERMINISTE de 6-2C : revérifie et apparie tâche <->
    posture et contexte <-> taxonomie, puis construit le
    RestrictedSupportCatalogue et les segments à planifier. Fonction PURE.

    Erreurs : InvalidSupportPlanArgument, UnsupportedSupportPlanVersion,
    IncompatibleSupportPlanInputs, InvalidSupportPlanContent (erreurs amont
    chaînées). Aucun appel modèle ; rien n'est corrigé."""
    return _preflight(task_profile, posture_baseline, context, taxonomy)


def validate_support_plan_proposal(
    proposal: SupportPlanProposal,
    *,
    task_profile: InteractionTaskProfile,
    posture_baseline: InteractionPostureBaseline,
    context: PedagogicalResponseContext,
    taxonomy: CurrentFocusTaxonomy,
) -> InteractionSupportPlan:
    """SupportPlanProposal NON FIABLE -> InteractionSupportPlan validé.
    Fonction PURE : mêmes entrées + même proposition => même plan.

    Le préflight complet est rejoué sur les entrées reçues (jamais un
    catalogue fourni à la main). Puis : versions exactes de la proposition ;
    un segment proposé par segment 6-2A, segment_index canonique ;
    références du seul catalogue restreint (compétence autorisée, périmètre
    6.1 exact, tokens existants de CETTE compétence, aucun doublon) ;
    présupposés copiés EXACTEMENT des SafeAssumptionCoverage (au moins un
    stade sûr) ; ponts dans le périmètre autorisé ; allocations sur les
    opérations du segment, vocabulaire fermé, règles de posture. Ensembles
    canonicalisés (ordre du catalogue, ordre de COGNITIVE_OPERATIONS).
    Proposition invalide => InvalidSupportPlanProposal ; jamais un plan
    partiel ni corrigé."""
    planning = _preflight(task_profile, posture_baseline, context, taxonomy)
    if type(proposal) is not SupportPlanProposal:
        raise InvalidSupportPlanArgument(f"SupportPlanProposal attendue, reçu {type(proposal).__name__}")
    for name, expected in (("schema_version", SUPPORT_PLAN_SCHEMA_VERSION),
                           ("policy_version", SUPPORT_POLICY_VERSION)):
        value = getattr(proposal, name)
        if type(value) is not str or value != expected:
            raise _proposal_fail(name, f"{value!r} au lieu de {expected!r}")
    proposed = _tuple(proposal.segments, "segments")
    if len(proposed) != len(planning.segments):
        raise _proposal_fail("segments", f"{len(proposed)} segment(s) proposé(s) pour {len(planning.segments)}"
                                         " segment(s) 6-2A")
    return InteractionSupportPlan(
        schema_version=SUPPORT_PLAN_SCHEMA_VERSION,
        policy_version=SUPPORT_POLICY_VERSION,
        source_task_schema_version=task_profile.schema_version,
        source_task_policy_version=task_profile.policy_version,
        source_posture_schema_version=posture_baseline.schema_version,
        source_posture_policy_version=posture_baseline.policy_version,
        source_pedagogical_context_schema_version=context.schema_version,
        request_status=planning.request_status,
        pedagogical_resolution_status=planning.catalogue.resolution_status,
        segments=tuple(_segment_plan(segment_proposal, segment, planning.catalogue, f"segments[{index}]")
                       for index, (segment_proposal, segment) in enumerate(zip(proposed, planning.segments))),
    )


def project_support_plan(
    plan: InteractionSupportPlan,
    *,
    catalogue: RestrictedSupportCatalogue,
) -> SupportGenerationProjection:
    """Projection de génération d'un plan validé, avec le catalogue restreint
    du MÊME préflight. Plan et catalogue sont revérifiés et appariés
    (compatibilité, jamais authenticité). Fonction PURE et déterministe ;
    aucun UUID, aucune provenance, aucune contrainte de validation en
    sortie."""
    if type(plan) is not InteractionSupportPlan:
        raise InvalidSupportPlanArgument(f"InteractionSupportPlan attendu, reçu {type(plan).__name__}")
    if type(catalogue) is not RestrictedSupportCatalogue:
        raise InvalidSupportPlanArgument(f"RestrictedSupportCatalogue attendu, reçu {type(catalogue).__name__}")
    _check_plan(plan, catalogue)
    competencies = {competency.competency_code: competency for competency in catalogue.competencies}
    capabilities = {c.definition_id: c for competency in catalogue.competencies for c in competency.capabilities}

    def capability(definition_id) -> ProjectedCapability:
        source = capabilities[definition_id]
        return ProjectedCapability(capability_token=source.capability_token, label=source.label,
                                   definition=source.definition)

    return SupportGenerationProjection(
        schema_version=plan.schema_version,
        policy_version=plan.policy_version,
        request_status=plan.request_status,
        segments=tuple(SegmentSupportProjection(
            segment_index=segment.segment_index,
            source_excerpt=segment.source_excerpt,
            posture=segment.posture,
            relevant_safe_assumptions=tuple(ProjectedSafeAssumption(
                competency_code=assumption.competency_code,
                competency_label=competencies[assumption.competency_code].label,
                capability=(None if assumption.capability_definition_id is None
                            else capability(assumption.capability_definition_id)),
                safe_stages=assumption.safe_stages,
            ) for assumption in segment.relevant_safe_assumptions),
            conceptual_bridges=tuple(ProjectedConceptualBridge(
                competency_code=bridge.competency_code,
                competency_label=competencies[bridge.competency_code].label,
                scope_mode=bridge.scope_mode,
                capabilities=tuple(capability(i) for i in bridge.capability_definition_ids),
            ) for bridge in segment.conceptual_bridges),
            operation_allocations=segment.operation_allocations,
        ) for segment in plan.segments),
    )
