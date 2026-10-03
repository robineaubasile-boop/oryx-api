"""Étape 6.4C3 : rendu visible DÉTERMINISTE des jalons historiques 6.4C2
(ProgressHistoryProjection).

Question, et seulement elle : « comment montrer les jalons déjà choisis par
6.4C2 avec des formulations fixes, fidèles et non punitives ? ».

    6-4C2 ProgressMilestones -> project_progress_history -> ProgressHistoryProjection

Frontière constitutionnelle. 6-4C2 a décidé QUELS jalons existent ; 6-4C3
les FORMULE à partir de gabarits fermés, et seulement cela : il n'ajoute,
ne retire, ne fusionne ni ne réordonne aucun jalon, ne relit aucune base,
ne consulte aucun modèle et n'écrit aucune trace de support (« généré »
n'est pas « montré » : une future couche UI / runtime confirmera
l'affichage réel). Le rendu sûr appartient au backend : un frontend ne
reçoit jamais un code de stade, une transition ou une cause brute qu'il
pourrait transformer en indicateur de perte.

Invariants :

- Versions explicites : PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION décrit
  le contrat de sortie ; PROGRESS_HISTORY_PROJECTION_POLICY_VERSION les
  gabarits. Policy définie QUE pour les versions SUPPORTED_* (6-4C2, et
  6-4A pour ses libellés de stade) : toute autre version, du module amont
  ou de l'objet reçu => UnsupportedProgressHistoryProjectionVersion.

- Entrée stricte (aucune réparation) : ProgressMilestones exacte,
  compétence C1..C12, ProgressMilestone exactes ; stage_established :
  stade positif, aucune cause, chaque stade au plus une fois ; revision :
  aucun stade, causes non vides, dédupliquées, ordre REVISION_CAUSE_ORDER,
  au plus une note, toujours en dernier. Sinon
  InvalidProgressHistoryProjectionInput.

- stage_established : title = libellé visible EXACT de 6-4A
  (VISIBLE_STAGE_LABELS, jamais recopié), detail None.

- revision : un gabarit fixe par ensemble NON VIDE de causes (sept
  ensembles, tous définis) ; il décrit un ajustement de l'ESTIMATION
  d'Oryx, jamais une perte, une régression, une baisse ni un retour en
  arrière de l'utilisateur ; une correction d'intégrité est un ajustement
  Oryx.

- Exclusions : aucun code de stade, aucun stade de départ / d'arrivée,
  aucune transition ni cause brute, aucun plus haut stade, aucun jeton
  technique, aucun UUID, aucune date, durée ni vitesse, aucun compteur,
  score, pourcentage, rang ni niveau ; aucune recommandation ni prochaine
  étape.

- Fonction PURE : aucune base, aucun modèle, aucune horloge, aucun hasard ;
  mêmes jalons => même projection.
"""
from dataclasses import dataclass
from types import MappingProxyType

from core.inference_service import (
    CLAIM_STAGES,
    COMPETENCY_CODES,
    EVIDENCE_INTEGRITY_CHANGE,
    NEW_USER_EVIDENCE,
    PEDAGOGICAL_REINTERPRETATION,
)
from core.progress_milestones import (
    MILESTONE_KINDS,
    MILESTONE_REVISION,
    MILESTONE_STAGE_ESTABLISHED,
    PROGRESS_MILESTONES_POLICY_VERSION,
    PROGRESS_MILESTONES_SCHEMA_VERSION,
    REVISION_CAUSE_ORDER,
    ProgressMilestone,
    ProgressMilestones,
)
from core.progress_projection import CURRENT_PROGRESS_POLICY_VERSION, VISIBLE_STAGE_LABELS

PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION = "progress-history-projection-v1"
PROGRESS_HISTORY_PROJECTION_POLICY_VERSION = "progress-history-projection-policy-1"

# Versions amont pour lesquelles ces gabarits sont définis.
SUPPORTED_PROGRESS_MILESTONES_SCHEMA_VERSION = "progress-milestones-v1"
SUPPORTED_PROGRESS_MILESTONES_POLICY_VERSION = "progress-milestones-policy-1"
SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"

# Gabarits de la note de révision : un par ensemble NON VIDE de causes
# (clé : causes dans l'ordre REVISION_CAUSE_ORDER) -> (title, detail).
REVISION_TEMPLATES = MappingProxyType({
    (NEW_USER_EVIDENCE,): (
        "Nouvelle évaluation",
        "De nouveaux éléments observés ont conduit Oryx à réviser son estimation."),
    (EVIDENCE_INTEGRITY_CHANGE,): (
        "Ajustement Oryx",
        "Une correction des éléments disponibles a conduit Oryx à ajuster son estimation."),
    (PEDAGOGICAL_REINTERPRETATION,): (
        "Réévaluation Oryx",
        "Une réinterprétation pédagogique des éléments disponibles a conduit Oryx à ajuster son estimation."),
    (NEW_USER_EVIDENCE, EVIDENCE_INTEGRITY_CHANGE): (
        "Ajustement de l’estimation",
        "Des corrections des éléments disponibles et de nouveaux éléments observés ont conduit Oryx à ajuster"
        " son estimation."),
    (NEW_USER_EVIDENCE, PEDAGOGICAL_REINTERPRETATION): (
        "Réévaluation de l’estimation",
        "De nouveaux éléments observés et une réinterprétation pédagogique ont conduit Oryx à ajuster son"
        " estimation."),
    (EVIDENCE_INTEGRITY_CHANGE, PEDAGOGICAL_REINTERPRETATION): (
        "Réévaluation de l’estimation",
        "Des corrections des éléments disponibles et une réinterprétation pédagogique ont conduit Oryx à"
        " ajuster son estimation."),
    (NEW_USER_EVIDENCE, EVIDENCE_INTEGRITY_CHANGE, PEDAGOGICAL_REINTERPRETATION): (
        "Ajustement de l’estimation",
        "Plusieurs évolutions des éléments disponibles et de leur interprétation ont conduit Oryx à ajuster"
        " son estimation."),
})


class ProgressHistoryProjectionError(Exception):
    """Erreur métier de 6-4C3 : jamais une projection partielle."""


class InvalidProgressHistoryProjectionInput(ProgressHistoryProjectionError):
    """Jalons 6-4C2 mal formés ou incohérents : jamais réparés."""


class UnsupportedProgressHistoryProjectionVersion(ProgressHistoryProjectionError):
    """Version amont (6-4C2 ou libellés 6-4A) pour laquelle aucun gabarit
    n'est défini."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class VisibleProgressMilestone:
    """Jalon visible : milestone_kind du vocabulaire 6-4C2, title et detail
    issus de gabarits fixes (detail None pour un stade)."""
    milestone_kind: str
    title: str
    detail: str | None


@dataclass(frozen=True, kw_only=True)
class ProgressHistoryProjection:
    """Historique visible d'UNE compétence, dans l'ordre des jalons 6-4C2.
    Aucune identité, aucun code technique."""
    schema_version: str
    policy_version: str
    competency_code: str
    milestones: tuple[VisibleProgressMilestone, ...]


# --------------------------------------------------------------------------
# Validation (pure)
# --------------------------------------------------------------------------

def _fail(detail: str) -> InvalidProgressHistoryProjectionInput:
    return InvalidProgressHistoryProjectionInput(detail)


def _check_versions(milestones: ProgressMilestones) -> None:
    for name, actual, supported in (
            ("module 6-4C2 schema", PROGRESS_MILESTONES_SCHEMA_VERSION, SUPPORTED_PROGRESS_MILESTONES_SCHEMA_VERSION),
            ("module 6-4C2 policy", PROGRESS_MILESTONES_POLICY_VERSION, SUPPORTED_PROGRESS_MILESTONES_POLICY_VERSION),
            ("module 6-4A policy", CURRENT_PROGRESS_POLICY_VERSION, SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION),
            ("jalons schema", milestones.schema_version, SUPPORTED_PROGRESS_MILESTONES_SCHEMA_VERSION),
            ("jalons policy", milestones.policy_version, SUPPORTED_PROGRESS_MILESTONES_POLICY_VERSION)):
        if actual != supported:
            raise UnsupportedProgressHistoryProjectionVersion(f"{name} {actual!r} ({supported!r} supportée)")


def _checked_milestones(milestones) -> tuple:
    if type(milestones) is not ProgressMilestones:
        raise _fail(f"ProgressMilestones attendue, reçu {type(milestones).__name__}")
    _check_versions(milestones)
    if type(milestones.competency_code) is not str or milestones.competency_code not in COMPETENCY_CODES:
        raise _fail(f"competency_code {milestones.competency_code!r} hors de C1..C12")
    items = milestones.milestones
    if type(items) is not tuple or any(type(item) is not ProgressMilestone for item in items):
        raise _fail("milestones : tuple de ProgressMilestone attendu")
    stages = []
    for position, item in enumerate(items, start=1):
        label = f"jalon {position}"
        if type(item.milestone_kind) is not str or item.milestone_kind not in MILESTONE_KINDS:
            raise _fail(f"{label} : milestone_kind {item.milestone_kind!r} hors vocabulaire")
        causes = item.revision_causes
        if type(causes) is not tuple:
            raise _fail(f"{label} : revision_causes tuple attendu")
        if item.milestone_kind == MILESTONE_STAGE_ESTABLISHED:
            if type(item.stage_code) is not str or item.stage_code not in CLAIM_STAGES:
                raise _fail(f"{label} : stage_code {item.stage_code!r} hors des stades positifs")
            if item.stage_code in stages:
                raise _fail(f"{label} : stade {item.stage_code} déjà établi (au plus une fois)")
            if causes:
                raise _fail(f"{label} : {MILESTONE_STAGE_ESTABLISHED} avec des causes de révision")
            stages.append(item.stage_code)
            continue
        if item.stage_code is not None:
            raise _fail(f"{label} : {MILESTONE_REVISION} avec un stage_code")
        if position != len(items):
            raise _fail(f"{label} : {MILESTONE_REVISION} ailleurs qu'en dernier (au plus une note)")
        if (any(type(cause) is not str for cause in causes) or causes not in REVISION_TEMPLATES
                or causes != tuple(c for c in REVISION_CAUSE_ORDER if c in causes)):
            raise _fail(f"{label} : revision_causes {causes!r} vides, inconnues, en double ou hors de l'ordre"
                        " de sérialisation")
    return items


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def _visible(item: ProgressMilestone) -> VisibleProgressMilestone:
    if item.milestone_kind == MILESTONE_STAGE_ESTABLISHED:
        return VisibleProgressMilestone(milestone_kind=item.milestone_kind,
                                        title=VISIBLE_STAGE_LABELS[item.stage_code], detail=None)
    title, detail = REVISION_TEMPLATES[item.revision_causes]
    return VisibleProgressMilestone(milestone_kind=item.milestone_kind, title=title, detail=detail)


def project_progress_history(*, milestones: ProgressMilestones) -> ProgressHistoryProjection:
    """Jalons 6-4C2 -> jalons visibles, par gabarits fixes, dans le même
    ordre. Fonction PURE : mêmes jalons => même projection.

    Erreurs : InvalidProgressHistoryProjectionInput,
    UnsupportedProgressHistoryProjectionVersion ; jamais de projection
    partielle."""
    items = _checked_milestones(milestones)
    return ProgressHistoryProjection(
        schema_version=PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION,
        policy_version=PROGRESS_HISTORY_PROJECTION_POLICY_VERSION,
        competency_code=milestones.competency_code,
        milestones=tuple(_visible(item) for item in items),
    )
