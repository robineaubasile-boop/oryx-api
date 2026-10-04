"""Étape 6.4D : composition de la vue générale Progression (ProgressionView).

Question, et seulement elle : « comment assembler les projections
indépendantes de progression en une vue cohérente, sans créer de nouvelle
information pédagogique, sans convertir les métriques Academy / Rallye en
compétence et sans fabriquer de progression globale ? ».

    AcademyProgressSummary | None   (port : « qu'ai-je étudié ? »)
    RallyeSessionSummary | None     (port : « comment s'est passée cette session ? »)
    6-4A CurrentProgressProjection  (« qu'a permis d'établir mon raisonnement observé ? »)
        -> compose_progression_view -> ProgressionView

Frontière constitutionnelle. Trois objets pédagogiques INDÉPENDANTS, jamais
fusionnés, jamais convertis l'un dans l'autre : un parcours Academy terminé
ne prouve aucune compétence, un score Rallye ne détermine aucun stade, les
cartes C1-C12 ne deviennent jamais un pourcentage global. 6-4D COMPOSE ; il
n'infère rien : aucune correction croisée, aucune synthèse, aucun niveau,
score, pourcentage, moyenne, rang, XP, série, badge, plus forte / plus
faible compétence, ni prochaine étape.

Invariants :

- Versions explicites : PROGRESSION_VIEW_SCHEMA_VERSION /
  PROGRESSION_VIEW_POLICY_VERSION pour la vue ; une paire par port de
  composition (ACADEMY_PROGRESS_SUMMARY_*, RALLYE_SESSION_SUMMARY_*). La
  composition n'est définie QUE pour la projection 6-4A SUPPORTED_* et les
  versions exactes des ports : toute autre version =>
  UnsupportedProgressionViewVersion.

- AcademyProgressSummary est un PORT de composition, jamais le modèle
  métier Academy : 6-4D ne sait ni comment un module devient terminé, ni
  quel module est courant ou suivant, ni où Academy est stocké. Entiers
  exacts (jamais bool) ; total_modules >= 1 (jamais un total figé) ;
  0 <= completed_modules <= total_modules. Aucun ratio, aucune compétence.

- RallyeSessionSummary est un PORT de composition, jamais le moteur
  Rallye : 6-4D ne sait ni quelle session est la dernière, ni comment son
  score est calculé ou stocké. session_label : str exacte, non vide après
  strip, sans NUL, sur une seule ligne ; score_value / score_max : entiers
  exacts (jamais bool), score_max >= 1 (jamais un maximum figé),
  0 <= score_value <= score_max. Aucun ratio, aucune compétence, aucun
  diagnostic.

- academy / rallye None : « aucune projection fournie », jamais « jamais
  utilisé ».

- Compétences : EXACTEMENT les douze cartes 6-4A C1 -> C12, dans l'ordre
  conceptuel (aucun manque, doublon, ajout ni réordonnancement, jamais un
  tri par stade), réutilisées TELLES QUELLES (mêmes objets
  CompetencyCurrentProgress, aucune copie, aucune carte dérivée).

- Fonction PURE : aucune base, aucune session, aucun réseau, aucun modèle,
  aucune horloge, aucun hasard ; éphémère, aucune persistance, aucun
  branchement runtime. Même entrée => même vue.
"""
from dataclasses import dataclass

from core.adaptation_state import COMPETENCY_ORDER
from core.progress_projection import (
    CURRENT_PROGRESS_POLICY_VERSION,
    CURRENT_PROGRESS_SCHEMA_VERSION,
    CompetencyCurrentProgress,
    CurrentProgressProjection,
)

PROGRESSION_VIEW_SCHEMA_VERSION = "progression-view-v1"
PROGRESSION_VIEW_POLICY_VERSION = "progression-view-policy-1"

ACADEMY_PROGRESS_SUMMARY_SCHEMA_VERSION = "academy-progress-summary-v1"
ACADEMY_PROGRESS_SUMMARY_POLICY_VERSION = "academy-progress-summary-policy-1"

RALLYE_SESSION_SUMMARY_SCHEMA_VERSION = "rallye-session-summary-v1"
RALLYE_SESSION_SUMMARY_POLICY_VERSION = "rallye-session-summary-policy-1"

# Version amont (6-4A) pour laquelle cette composition est définie.
SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION = "current-progress-projection-v1"
SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"

# Fins de ligne refusées dans un libellé mono-ligne (str.splitlines).
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85  ")


class ProgressionViewError(Exception):
    """Erreur métier de 6-4D (vue générale) : jamais une vue partielle."""


class InvalidProgressionViewInput(ProgressionViewError):
    """Entrée de type inattendu, mal formée ou hors bornes (jamais réparée)."""


class UnsupportedProgressionViewVersion(ProgressionViewError):
    """Version amont (6-4A ou port) pour laquelle la composition n'est pas
    définie."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class AcademyProgressSummary:
    """Port de composition Academy (« qu'ai-je étudié ? ») : produit par le
    futur propriétaire Academy. Ni compétence, ni stade, ni module courant
    ou suivant, ni ratio."""
    schema_version: str
    policy_version: str

    completed_modules: int
    total_modules: int


@dataclass(frozen=True, kw_only=True)
class RallyeSessionSummary:
    """Port de composition Rallye (« comment s'est passée cette session ? »)
    : produit par le futur propriétaire Rallye. Ni compétence, ni stade, ni
    diagnostic, ni ratio."""
    schema_version: str
    policy_version: str

    session_label: str

    score_value: int
    score_max: int


@dataclass(frozen=True, kw_only=True)
class ProgressionView:
    """Vue générale : trois objets indépendants juxtaposés, jamais fusionnés.
    competencies : les douze cartes 6-4A, C1 -> C12, telles quelles."""
    schema_version: str
    policy_version: str

    academy: AcademyProgressSummary | None
    rallye: RallyeSessionSummary | None

    competencies: tuple[CompetencyCurrentProgress, ...]


# --------------------------------------------------------------------------
# Validation (pure)
# --------------------------------------------------------------------------

def _exact_int(value) -> bool:
    return type(value) is int


def _check_versions(name: str, received: tuple, supported: tuple) -> None:
    if received != supported:
        raise UnsupportedProgressionViewVersion(f"{name} {received!r} ({supported!r} supportée)")


def _checked_academy(academy) -> AcademyProgressSummary | None:
    if academy is None:
        return None
    if type(academy) is not AcademyProgressSummary:
        raise InvalidProgressionViewInput(f"AcademyProgressSummary attendu, reçu {type(academy).__name__}")
    _check_versions("academy", (academy.schema_version, academy.policy_version),
                    (ACADEMY_PROGRESS_SUMMARY_SCHEMA_VERSION, ACADEMY_PROGRESS_SUMMARY_POLICY_VERSION))
    completed, total = academy.completed_modules, academy.total_modules
    if not _exact_int(completed) or not _exact_int(total):
        raise InvalidProgressionViewInput("academy : completed_modules et total_modules doivent être des int exacts")
    if total < 1:
        raise InvalidProgressionViewInput(f"academy : total_modules {total} < 1")
    if not 0 <= completed <= total:
        raise InvalidProgressionViewInput(f"academy : completed_modules {completed} hors de [0, {total}]")
    return academy


def _checked_rallye(rallye) -> RallyeSessionSummary | None:
    if rallye is None:
        return None
    if type(rallye) is not RallyeSessionSummary:
        raise InvalidProgressionViewInput(f"RallyeSessionSummary attendu, reçu {type(rallye).__name__}")
    _check_versions("rallye", (rallye.schema_version, rallye.policy_version),
                    (RALLYE_SESSION_SUMMARY_SCHEMA_VERSION, RALLYE_SESSION_SUMMARY_POLICY_VERSION))
    label = rallye.session_label
    if type(label) is not str or not label.strip() or "\x00" in label or _LINE_BREAKS & set(label):
        raise InvalidProgressionViewInput("rallye : session_label doit être une str non vide, sans NUL, mono-ligne")
    value, maximum = rallye.score_value, rallye.score_max
    if not _exact_int(value) or not _exact_int(maximum):
        raise InvalidProgressionViewInput("rallye : score_value et score_max doivent être des int exacts")
    if maximum < 1:
        raise InvalidProgressionViewInput(f"rallye : score_max {maximum} < 1")
    if not 0 <= value <= maximum:
        raise InvalidProgressionViewInput(f"rallye : score_value {value} hors de [0, {maximum}]")
    return rallye


def _checked_competencies(current) -> tuple:
    if type(current) is not CurrentProgressProjection:
        raise InvalidProgressionViewInput(f"CurrentProgressProjection attendue, reçu {type(current).__name__}")
    supported = (SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION)
    _check_versions("module 6-4A", (CURRENT_PROGRESS_SCHEMA_VERSION, CURRENT_PROGRESS_POLICY_VERSION), supported)
    _check_versions("projection 6-4A", (current.schema_version, current.policy_version), supported)
    cards = current.competencies
    if type(cards) is not tuple or any(type(card) is not CompetencyCurrentProgress for card in cards):
        raise InvalidProgressionViewInput("current.competencies : tuple de CompetencyCurrentProgress attendu")
    codes = tuple(card.competency_code for card in cards)
    if codes != COMPETENCY_ORDER:
        raise InvalidProgressionViewInput(f"current.competencies {codes!r} : exactement {COMPETENCY_ORDER} attendues,"
                                          " dans cet ordre (aucun manque, doublon ni réordonnancement)")
    return cards


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def compose_progression_view(
    *,
    current: CurrentProgressProjection,
    academy: AcademyProgressSummary | None = None,
    rallye: RallyeSessionSummary | None = None,
) -> ProgressionView:
    """Juxtapose les ports Academy / Rallye (optionnels) et les douze cartes
    6-4A C1 -> C12, sans aucune conversion ni correction croisée. Fonction
    PURE : même entrée => même vue.

    Erreurs : InvalidProgressionViewInput, UnsupportedProgressionViewVersion ;
    jamais de vue partielle."""
    competencies = _checked_competencies(current)
    return ProgressionView(
        schema_version=PROGRESSION_VIEW_SCHEMA_VERSION,
        policy_version=PROGRESSION_VIEW_POLICY_VERSION,
        academy=_checked_academy(academy),
        rallye=_checked_rallye(rallye),
        competencies=competencies,
    )
