"""Étape 6.4C2 : compression DÉTERMINISTE de l'historique authentique 6.4C1
en jalons significatifs (ProgressMilestones).

Question, et seulement elle : « quels changements historiques significatifs
de l'état pédagogique courant peuvent être montrés comme jalons, sans
confondre historique et état actuel, sans exposer les oscillations
techniques et sans transformer une révision en jugement sur
l'utilisateur ? ».

    6-4C1 ProgressHistory -> project_progress_milestones -> ProgressMilestones

Frontière constitutionnelle. Step 5 a déjà tranché toutes les questions
cognitives (stade, stabilité, anti-oscillation, motifs de révision) ;
6-4C1 a copié la lignée adoptée ; 6-4C2 la COMPRESSE, et seulement cela. Il
ne relit ni ne revalide aucune preuve, ne juge aucune décision ancienne,
ne recalcule aucun stade, ne lit jamais unresolved_revision_context, motif,
tension ni revalidation : il observe uniquement les stades persistés, les
transitions et les causes. L'état ACTUEL appartient à 6-4A : le run courant
ne devient jamais un jalon.

Règles (policy progress-milestones-policy-1) :

- Validation fail-closed de l'entrée : ProgressHistory exacte, versions
  6-4C1 supportées, compétence C1..C12, entries tuple de
  HistoricalInferenceState, jetons history_1 .. history_N contigus, stades
  et causes des vocabulaires Step 5 (InvalidProgressMilestoneInput) ;
  première inférence sans previous_stage, transition ni cause ; ensuite
  previous_stage = current_stage précédent, transition = celle dérivée de
  l'ordre conceptuel, cause présente (InvalidProgressMilestoneHistory).
  Histoire vide (carte sans état) => aucun jalon.

- stage_established : parmi les entries ANTÉRIEURES au run courant, la
  PREMIÈRE fois qu'un stade positif (jamais non_etabli) est établi par une
  première inférence ou une montée (transition None ou upgraded). Un stade
  atteint par revised_down n'est jamais « établi » : il appartient à l'arc
  de révision, porté par la seule note de révision ; maintained ne crée
  jamais de jalon (il ne change aucun stade). Chaque stade positif apparaît
  au plus une fois, dans l'ordre chronologique de la lignée.

- Run courant jamais dupliqué : il ne crée jamais de stage_established (la
  carte 6-4A l'affiche). Simplification explicite : le jalon dont
  stage_code == current_stage est retiré SI aucun jalon stage_established
  d'un stade supérieur au current_stage ne le suit (il ne raconterait
  qu'une copie de la carte actuelle) ; il reste s'il a précédé
  l'établissement d'un stade supérieur (chemin vers ce stade). Règle de
  présentation, jamais une logique de preuve.

- Note de révision : le stade positif historique le plus haut de TOUTE la
  lignée (run courant inclus, ordre conceptuel STAGE_SEQUENCE) est une
  variable LOCALE, jamais exposée. Aucun stade positif, ou current_stage
  au moins aussi haut que lui (la situation a rattrapé l'histoire) => aucune
  note. Sinon : exactement UNE note revision, en dernier, portant les causes
  de TOUTES les transitions revised_down postérieures à la DERNIÈRE
  occurrence de ce stade (jamais la seule dernière révision), dédupliquées,
  dans l'ordre de sérialisation REVISION_CAUSE_ORDER (jamais une priorité ni
  une force) ; aucune => InvalidProgressMilestoneHistory (garde explicite).

- Exclusions : aucun jeton history_k (un jalon compresse une première
  occurrence ou agrège plusieurs runs : aucune provenance 1:1 inventée),
  aucun UUID, aucune date, durée ni vitesse, aucun compteur, score, rang,
  niveau, plus haut stade, ni from / to stage ; aucune borne arbitraire (au
  plus quatre stage_established et une note découlent de la sémantique).

- Fonction PURE : aucune base, aucune session, aucun modèle, aucune
  horloge, aucun hasard ; même ProgressHistory => mêmes jalons.
"""
from dataclasses import dataclass

from core.inference_service import (
    CLAIM_STAGES,
    COMPETENCY_CODES,
    CURRENT_STAGES,
    EVIDENCE_INTEGRITY_CHANGE,
    MAINTAINED,
    NEW_USER_EVIDENCE,
    NON_ETABLI,
    PEDAGOGICAL_REINTERPRETATION,
    REVISED_DOWN,
    STAGE_SEQUENCE,
    TRANSITION_CAUSES,
    TRANSITIONS,
    UPGRADED,
)
from core.progress_history import (
    HISTORY_TOKEN_PREFIX,
    HistoricalInferenceState,
    ProgressHistory,
)

PROGRESS_MILESTONES_SCHEMA_VERSION = "progress-milestones-v1"
PROGRESS_MILESTONES_POLICY_VERSION = "progress-milestones-policy-1"

# Versions 6-4C1 pour lesquelles cette policy est définie.
SUPPORTED_PROGRESS_HISTORY_SCHEMA_VERSION = "progress-history-v1"
SUPPORTED_PROGRESS_HISTORY_POLICY_VERSION = "progress-history-policy-1"

# ProgressMilestone.milestone_kind : vocabulaire fermé V1.
MILESTONE_STAGE_ESTABLISHED = "stage_established"
MILESTONE_REVISION = "revision"
MILESTONE_KINDS = (MILESTONE_STAGE_ESTABLISHED, MILESTONE_REVISION)

# Ordre de SÉRIALISATION des causes d'une note de révision (TRANSITION_CAUSES
# est un frozenset) : ni priorité, ni force, ni gravité.
REVISION_CAUSE_ORDER = (NEW_USER_EVIDENCE, EVIDENCE_INTEGRITY_CHANGE, PEDAGOGICAL_REINTERPRETATION)


class ProgressMilestoneError(Exception):
    """Erreur métier de 6-4C2 : jamais une liste de jalons partielle."""


class InvalidProgressMilestoneInput(ProgressMilestoneError):
    """Entrée mal formée : type, version, compétence, jeton, vocabulaire."""


class InvalidProgressMilestoneHistory(ProgressMilestoneError):
    """Histoire bien formée mais incohérente : liens de stade, transition,
    cause, ou écart historique visible sans aucune révision."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ProgressMilestone:
    """Jalon interne. stage_established : stage_code dans CLAIM_STAGES,
    revision_causes () ; revision : stage_code None, revision_causes non
    vide, dédupliquées, ordre REVISION_CAUSE_ORDER."""
    milestone_kind: str
    stage_code: str | None
    revision_causes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ProgressMilestones:
    """Jalons d'UNE compétence, ordre de présentation (stades dans l'ordre
    chronologique de leur premier établissement, note de révision en
    dernier). Aucune identité, aucun jeton technique."""
    schema_version: str
    policy_version: str
    competency_code: str
    milestones: tuple[ProgressMilestone, ...]


# --------------------------------------------------------------------------
# Validation de l'histoire (pure)
# --------------------------------------------------------------------------

def _input_fail(detail: str) -> InvalidProgressMilestoneInput:
    return InvalidProgressMilestoneInput(detail)


def _above(first: str, second: str) -> bool:
    """first strictement au-dessus de second dans l'ordre conceptuel (tuple
    du service propriétaire, jamais un nombre exposé)."""
    return STAGE_SEQUENCE.index(first) > STAGE_SEQUENCE.index(second)


def _derived_transition(previous: str, current: str) -> str:
    if current == previous:
        return MAINTAINED
    return UPGRADED if _above(current, previous) else REVISED_DOWN


def _checked_entries(history) -> tuple:
    if type(history) is not ProgressHistory:
        raise _input_fail(f"ProgressHistory attendue, reçu {type(history).__name__}")
    if (history.schema_version, history.policy_version) != (SUPPORTED_PROGRESS_HISTORY_SCHEMA_VERSION,
                                                            SUPPORTED_PROGRESS_HISTORY_POLICY_VERSION):
        raise _input_fail(f"versions 6-4C1 {history.schema_version!r} / {history.policy_version!r} non supportées")
    if type(history.competency_code) is not str or history.competency_code not in COMPETENCY_CODES:
        raise _input_fail(f"competency_code {history.competency_code!r} hors de C1..C12")
    entries = history.entries
    if type(entries) is not tuple or any(type(entry) is not HistoricalInferenceState for entry in entries):
        raise _input_fail("entries : tuple de HistoricalInferenceState attendu")
    for position, entry in enumerate(entries, start=1):
        label = f"entry {position}"
        if entry.history_token != f"{HISTORY_TOKEN_PREFIX}{position}":
            raise _input_fail(f"{label} : jeton {entry.history_token!r} ({HISTORY_TOKEN_PREFIX}{position} attendu)")
        if type(entry.current_stage) is not str or entry.current_stage not in CURRENT_STAGES:
            raise _input_fail(f"{label} : current_stage {entry.current_stage!r} hors vocabulaire")
        if position == 1:
            if (entry.previous_stage, entry.transition, entry.transition_cause) != (None, None, None):
                raise InvalidProgressMilestoneHistory(f"{label} : première inférence avec previous_stage /"
                                                      " transition / transition_cause")
            continue
        for name, allowed in (("previous_stage", CURRENT_STAGES), ("transition", TRANSITIONS),
                              ("transition_cause", TRANSITION_CAUSES)):
            value = getattr(entry, name)
            if type(value) is not str or value not in allowed:
                raise _input_fail(f"{label} : {name} {value!r} hors vocabulaire")
        previous = entries[position - 2].current_stage
        if entry.previous_stage != previous:
            raise InvalidProgressMilestoneHistory(f"{label} : previous_stage {entry.previous_stage!r} != stade"
                                                  f" précédent {previous!r}")
        expected = _derived_transition(previous, entry.current_stage)
        if entry.transition != expected:
            raise InvalidProgressMilestoneHistory(f"{label} : transition {entry.transition!r} ({expected} dérivée)")
    return entries


# --------------------------------------------------------------------------
# Compression (pure)
# --------------------------------------------------------------------------

def _established_stages(entries: tuple) -> list:
    """Stades positifs établis (première inférence ou montée) AVANT le run
    courant, première fois seulement, ordre chronologique ; puis retrait du
    stade courant s'il ne précède aucun établissement supérieur."""
    current_stage = entries[-1].current_stage
    stages = []
    for entry in entries[:-1]:
        if entry.current_stage == NON_ETABLI or entry.transition not in (None, UPGRADED):
            continue
        if entry.current_stage not in stages:
            stages.append(entry.current_stage)
    if current_stage in stages:
        later = stages[stages.index(current_stage) + 1:]
        if not any(_above(stage, current_stage) for stage in later):
            stages.remove(current_stage)
    return stages


def _revision_causes(entries: tuple) -> tuple:
    """Causes de l'écart encore visible entre l'état courant et le stade
    positif historique le plus haut (variable locale), ou () si aucun écart.
    Toutes les révisions depuis la DERNIÈRE occurrence de ce stade."""
    current_stage = entries[-1].current_stage
    highest = None
    for entry in entries:
        if entry.current_stage in CLAIM_STAGES and (highest is None or _above(entry.current_stage, highest)):
            highest = entry.current_stage
    if highest is None or not _above(highest, current_stage):
        return ()
    last = None
    for index, entry in enumerate(entries):
        if entry.current_stage == highest:
            last = index
    causes = {entry.transition_cause for entry in entries[last + 1:] if entry.transition == REVISED_DOWN}
    if not causes:
        raise InvalidProgressMilestoneHistory("état courant sous le stade historique le plus haut sans aucune"
                                              " transition revised_down depuis sa dernière occurrence")
    return tuple(cause for cause in REVISION_CAUSE_ORDER if cause in causes)


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def project_progress_milestones(*, history: ProgressHistory) -> ProgressMilestones:
    """Jalons significatifs de l'historique 6-4C1 : premiers établissements
    de stades antérieurs au run courant (sans dupliquer la carte 6-4A), puis
    au plus une note de révision si un écart historique reste visible.
    Fonction PURE : même histoire => mêmes jalons.

    Erreurs : InvalidProgressMilestoneInput, InvalidProgressMilestoneHistory ;
    jamais de liste partielle."""
    entries = _checked_entries(history)
    milestones = []
    if entries:
        milestones.extend(ProgressMilestone(milestone_kind=MILESTONE_STAGE_ESTABLISHED, stage_code=stage,
                                            revision_causes=()) for stage in _established_stages(entries))
        causes = _revision_causes(entries)
        if causes:
            milestones.append(ProgressMilestone(milestone_kind=MILESTONE_REVISION, stage_code=None,
                                                revision_causes=causes))
    return ProgressMilestones(
        schema_version=PROGRESS_MILESTONES_SCHEMA_VERSION,
        policy_version=PROGRESS_MILESTONES_POLICY_VERSION,
        competency_code=history.competency_code,
        milestones=tuple(milestones),
    )
