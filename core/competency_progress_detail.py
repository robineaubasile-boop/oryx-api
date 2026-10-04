"""Étape 6.4D : composition du détail d'UNE compétence
(CompetencyProgressDetail).

Question, et seulement elle : « comment assembler les projections déjà
décidées pour UNE compétence, sans créer de narration causale
supplémentaire ? ».

    6-4A CompetencyCurrentProgress     (état actuel affiché)
    6-4D CurrentProgressLimitations    (limites actuelles déjà établies)
    6-4B3 ProgressWhyRendering         (exemples illustratifs rendus)
    6-4C3 ProgressHistoryProjection    (jalons historiques visibles)
        -> compose_competency_progress_detail -> CompetencyProgressDetail

Frontière constitutionnelle. Chaque partie a été décidée par sa propre
étape ; 6-4D les JUXTAPOSE, et seulement cela. Il ne relit aucune base, ne
recalcule aucune partie, n'ajoute, ne retire ni ne réordonne aucun élément,
et ne les relie jamais entre elles : aucun récit « tu étais X puis, grâce à
Y, tu es devenu Z » (why + history), aucun « malgré cette limite » (why +
limitations), aucune tension actuelle présentée comme cause d'une ancienne
révision (limitations + history). Aucun résumé, parcours, explication
causale, compteur (capacités, tensions, exemples, jalons), pourcentage,
score, recommandation, prochaine compétence ni action. Aucune trace de
support : « généré » n'est pas « montré ».

Invariants :

- Versions explicites : COMPETENCY_PROGRESS_DETAIL_SCHEMA_VERSION /
  COMPETENCY_PROGRESS_DETAIL_POLICY_VERSION. Composition définie QUE pour
  les versions amont SUPPORTED_* (6-4A, limitations, 6-4B3, 6-4C3), du
  module amont comme de l'objet reçu : toute autre version =>
  UnsupportedCompetencyProgressDetailVersion.

- Entrées : types exacts (une carte 6-4A, jamais toute la projection) ;
  sinon InvalidCompetencyProgressDetailInput. Les invariants de statut des
  limitations (not_applicable / unavailable => aucune capacité) sont
  revérifiés sur l'objet reçu.

- Identité : current.competency_code == limitations.competency_code ==
  why.competency_code == history.competency_code, compétence C1..C12 ;
  sinon IncompatibleCompetencyProgressDetailInputs. Les quatre objets sont
  réutilisés TELS QUELS.

- Fonction PURE : aucune base, aucune session, aucun réseau, aucun modèle,
  aucune horloge, aucun hasard ; éphémère, aucune persistance, aucun
  branchement runtime. Même entrée => même détail.
"""
from dataclasses import dataclass

from core.adaptation_state import COMPETENCY_ORDER
from core.progress_evidence_rendering import (
    PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
    PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
    ProgressWhyRendering,
)
from core.progress_history_projection import (
    PROGRESS_HISTORY_PROJECTION_POLICY_VERSION,
    PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION,
    ProgressHistoryProjection,
)
from core.progress_limitations import (
    CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION,
    CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION,
    LESS_OBSERVED_AVAILABLE,
    LESS_OBSERVED_STATUSES,
    CurrentProgressLimitations,
    LessObservedCapability,
    VisibleCurrentTension,
)
from core.progress_projection import (
    CURRENT_PROGRESS_POLICY_VERSION,
    CURRENT_PROGRESS_SCHEMA_VERSION,
    CompetencyCurrentProgress,
)

COMPETENCY_PROGRESS_DETAIL_SCHEMA_VERSION = "competency-progress-detail-v1"
COMPETENCY_PROGRESS_DETAIL_POLICY_VERSION = "competency-progress-detail-policy-1"

# Versions amont pour lesquelles cette composition est définie.
SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION = "current-progress-projection-v1"
SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"
SUPPORTED_CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION = "current-progress-limitations-v1"
SUPPORTED_CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION = "current-progress-limitations-policy-1"
SUPPORTED_PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION = "progress-evidence-rendering-v1"
SUPPORTED_PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION = "progress-evidence-rendering-policy-1"
SUPPORTED_PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION = "progress-history-projection-v1"
SUPPORTED_PROGRESS_HISTORY_PROJECTION_POLICY_VERSION = "progress-history-projection-policy-1"


class CompetencyProgressDetailError(Exception):
    """Erreur métier de 6-4D (détail) : jamais un détail partiel."""


class InvalidCompetencyProgressDetailInput(CompetencyProgressDetailError):
    """Entrée de type inattendu ou mal formée (jamais réparée)."""


class IncompatibleCompetencyProgressDetailInputs(CompetencyProgressDetailError):
    """Les quatre parties ne désignent pas la même compétence."""


class UnsupportedCompetencyProgressDetailVersion(CompetencyProgressDetailError):
    """Version amont pour laquelle cette composition n'est pas définie."""


# --------------------------------------------------------------------------
# Structure immuable
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class CompetencyProgressDetail:
    """Détail d'UNE compétence : quatre parties indépendantes juxtaposées,
    chacune telle que décidée par son étape, jamais reliées entre elles."""
    schema_version: str
    policy_version: str

    current: CompetencyCurrentProgress
    limitations: CurrentProgressLimitations
    why: ProgressWhyRendering
    history: ProgressHistoryProjection


# --------------------------------------------------------------------------
# Validation (pure)
# --------------------------------------------------------------------------

def _check_types(current, limitations, why, history) -> None:
    for name, value, expected in (("current", current, CompetencyCurrentProgress),
                                  ("limitations", limitations, CurrentProgressLimitations),
                                  ("why", why, ProgressWhyRendering),
                                  ("history", history, ProgressHistoryProjection)):
        if type(value) is not expected:
            raise InvalidCompetencyProgressDetailInput(f"{name} : {expected.__name__} attendu,"
                                                       f" reçu {type(value).__name__}")


def _check_versions(limitations: CurrentProgressLimitations, why: ProgressWhyRendering,
                    history: ProgressHistoryProjection) -> None:
    limitations_supported = (SUPPORTED_CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION,
                             SUPPORTED_CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION)
    why_supported = (SUPPORTED_PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
                     SUPPORTED_PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION)
    history_supported = (SUPPORTED_PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION,
                         SUPPORTED_PROGRESS_HISTORY_PROJECTION_POLICY_VERSION)
    checks = (
        ("module 6-4A", (CURRENT_PROGRESS_SCHEMA_VERSION, CURRENT_PROGRESS_POLICY_VERSION),
         (SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION)),
        ("module limitations",
         (CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION, CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION),
         limitations_supported),
        ("limitations", (limitations.schema_version, limitations.policy_version), limitations_supported),
        ("module 6-4B3", (PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION, PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION),
         why_supported),
        ("why", (why.schema_version, why.policy_version), why_supported),
        ("module 6-4C3", (PROGRESS_HISTORY_PROJECTION_SCHEMA_VERSION, PROGRESS_HISTORY_PROJECTION_POLICY_VERSION),
         history_supported),
        ("history", (history.schema_version, history.policy_version), history_supported),
    )
    for name, received, supported in checks:
        if received != supported:
            raise UnsupportedCompetencyProgressDetailVersion(f"{name} {received!r} ({supported!r} supportée)")


def _check_limitations(limitations: CurrentProgressLimitations) -> None:
    """Invariants de statut du contrat des limitations, revérifiés sur
    l'objet reçu (jamais recalculés)."""
    status, capabilities, tensions = (limitations.less_observed_status, limitations.less_observed_capabilities,
                                      limitations.current_tensions)
    if type(status) is not str or status not in LESS_OBSERVED_STATUSES:
        raise InvalidCompetencyProgressDetailInput(f"limitations : less_observed_status {status!r} hors vocabulaire")
    if type(capabilities) is not tuple or any(type(c) is not LessObservedCapability for c in capabilities):
        raise InvalidCompetencyProgressDetailInput("limitations : tuple de LessObservedCapability attendu")
    if status != LESS_OBSERVED_AVAILABLE and capabilities:
        raise InvalidCompetencyProgressDetailInput(f"limitations {status} avec des capacités")
    if type(tensions) is not tuple or any(type(t) is not VisibleCurrentTension for t in tensions):
        raise InvalidCompetencyProgressDetailInput("limitations : tuple de VisibleCurrentTension attendu")


def _check_identity(current: CompetencyCurrentProgress, limitations: CurrentProgressLimitations,
                    why: ProgressWhyRendering, history: ProgressHistoryProjection) -> None:
    code = current.competency_code
    if type(code) is not str or code not in COMPETENCY_ORDER:
        raise InvalidCompetencyProgressDetailInput(f"current.competency_code {code!r} hors de C1..C12")
    codes = {"limitations": limitations.competency_code, "why": why.competency_code,
             "history": history.competency_code}
    mismatched = {name: other for name, other in codes.items() if other != code}
    if mismatched:
        raise IncompatibleCompetencyProgressDetailInputs(f"current {code!r} != {mismatched!r}"
                                                         " (une seule compétence par détail)")


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def compose_competency_progress_detail(
    *,
    current: CompetencyCurrentProgress,
    limitations: CurrentProgressLimitations,
    why: ProgressWhyRendering,
    history: ProgressHistoryProjection,
) -> CompetencyProgressDetail:
    """Juxtapose la carte 6-4A, les limitations actuelles, le rendu 6-4B3 et
    l'historique 6-4C3 d'UNE même compétence, tels quels. Fonction PURE :
    même entrée => même détail.

    Erreurs : InvalidCompetencyProgressDetailInput,
    UnsupportedCompetencyProgressDetailVersion,
    IncompatibleCompetencyProgressDetailInputs ; jamais de détail partiel."""
    _check_types(current, limitations, why, history)
    _check_versions(limitations, why, history)
    _check_limitations(limitations)
    _check_identity(current, limitations, why, history)
    return CompetencyProgressDetail(
        schema_version=COMPETENCY_PROGRESS_DETAIL_SCHEMA_VERSION,
        policy_version=COMPETENCY_PROGRESS_DETAIL_POLICY_VERSION,
        current=current,
        limitations=limitations,
        why=why,
        history=history,
    )
