"""Étape 6.4C1 : acquisition SÛRE de l'historique AUTHENTIQUE de l'état
pédagogique courant d'une compétence (ProgressHistory).

Question, et seulement elle : « à partir d'une carte 6.4A et du même état
Step 5 capturé, quelle est la suite des décisions T6 réellement ADOPTÉES qui
mène à l'état courant affiché ? ».

    6-1A AdaptationStateSnapshot + 6-4A CurrentProgressProjection
    + lecture du service propriétaire T6 (lignée predecessor)
        -> acquire_progress_history -> ProgressHistory

Frontière constitutionnelle. Step 5 possède EXCLUSIVEMENT l'état
pédagogique et son historique ; 6-4A possède la carte ACTUELLE ; 6-4C1
COPIE seulement la lignée officielle (chaîne predecessor des runs T6
adoptés, lue par get_inference_lineage) sous une forme
éphémère, sans identifiant. Il ne décide, ne recalcule ni ne juge aucun
stade (ancien ou courant), ne relit aucune observation, claim, tension,
ref ni dossier T5 pour reconstruire ou revalider l'histoire : un ancien run
reste un fait historique même si ses preuves ont été invalidées depuis
(« un ancien jalon peut rester historiquement vrai sans décrire la
situation actuelle »). Aucun jalon, aucun texte (6-4C2 / 6-4C3), aucune
trace de support, aucune persistance.

Invariants :

- Versions explicites : PROGRESS_HISTORY_SCHEMA_VERSION décrit le contrat
  de sortie ; PROGRESS_HISTORY_POLICY_VERSION les règles d'acquisition.

- Carte canonique : progress doit être EXACTEMENT
  project_current_progress(state=state) (égalité structurelle complète) ;
  les règles 6-4A ne sont jamais recopiées. Sinon
  IncompatibleProgressHistoryInputs, aucune réparation.

- Carte sans état Step 5 (state_present False) : aucune histoire, entries
  (), aucune lecture de base (jamais une histoire « non_etabli » inventée).

- Run expliqué : EXACTEMENT CompetencyAdaptationSnapshot.active_inference_run_id.
  get_validated_user_competency_state AVANT puis APRÈS la lecture de la
  lignée doit désigner ce même run (sinon, ou chaîne Step 5 devenue non
  courante, StaleProgressHistory). Aucun retry, aucune boucle, aucun
  verrou : READ COMMITTED, cohérence par immutabilité des runs adoptés.

- Lignée : celle du service propriétaire, déjà validée (liens, couple,
  lifecycle, transitions, causes) ; revérifiée ici a minima : tuple non vide
  de HistoricalInferenceRun, liens predecessor contigus de la première
  inférence au run courant, même utilisateur et même compétence, dernier
  run = run du snapshot avec le même current_stage. Toute incohérence =>
  InvalidProgressHistoryState.

- Sortie : entries oldest -> current (ordre des seuls liens predecessor,
  jamais d'un horodatage), dernier entry = état courant ; jetons éphémères
  strictement séquentiels history_1 .. history_N ; previous_stage,
  current_stage, transition, transition_cause copiés tels quels.

- Exclusions : aucun identifiant d'utilisateur / run / predecessor, aucun
  UUID, aucun horodatage, aucune durée ; ni tension_state, ni
  unresolved_revision_context, ni validation_needs, ni confiance, ni claim,
  ni preuve ; aucun score, rang, compteur public, plus haut stade, vitesse.

- Lecture seule : uniquement des lectures du service propriétaire, sous
  db.no_autoflush ; jamais add / flush / commit / rollback / UPDATE /
  DELETE / INSERT ; aucun modèle, aucune migration, aucun cache, aucun
  branchement runtime.

Erreurs : InvalidProgressHistoryArgument (types, code inconnu),
IncompatibleProgressHistoryInputs (carte non canonique pour ce snapshot),
InvalidProgressHistoryState (corruption / incohérence),
StaleProgressHistory (évolution amont légitime : la vue n'est plus
courante). Jamais de résultat partiel.
"""
import uuid
from dataclasses import dataclass

from core.adaptation_state import COMPETENCY_ORDER, AdaptationStateSnapshot, CompetencyAdaptationSnapshot
from core.inference_service import (
    HistoricalInferenceRun,
    InferenceServiceError,
    StaleInferenceChain,
    get_inference_lineage,
    get_validated_user_competency_state,
)
from core.progress_projection import (
    CompetencyCurrentProgress,
    CurrentProgressProjection,
    ProgressProjectionError,
    project_current_progress,
)

PROGRESS_HISTORY_SCHEMA_VERSION = "progress-history-v1"
PROGRESS_HISTORY_POLICY_VERSION = "progress-history-policy-1"

HISTORY_TOKEN_PREFIX = "history_"


class ProgressHistoryError(Exception):
    """Erreur métier de 6-4C1 : jamais un historique partiel."""


class InvalidProgressHistoryArgument(ProgressHistoryError):
    """Argument top-level d'un type inattendu ou compétence inconnue."""


class IncompatibleProgressHistoryInputs(ProgressHistoryError):
    """La projection fournie n'est pas EXACTEMENT la projection canonique
    6-4A du snapshot fourni."""


class InvalidProgressHistoryState(ProgressHistoryError):
    """Lignée incohérente ou corrompue : jamais réparée."""


class StaleProgressHistory(ProgressHistoryError):
    """Évolution amont légitime : la vue n'est plus l'état courant ; le
    runtime reconstruit snapshot, projection et historique (aucun retry)."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class HistoricalInferenceState:
    """Une décision T6 adoptée de la lignée, copiée telle quelle.
    history_token : référence éphémère (audit interne), jamais un
    identifiant persistant."""
    history_token: str
    previous_stage: str | None
    current_stage: str
    transition: str | None
    transition_cause: str | None


@dataclass(frozen=True, kw_only=True)
class ProgressHistory:
    """Lignée d'UNE compétence, oldest -> current (dernier entry = état
    courant). Aucune identité de personne, aucun identifiant technique."""
    schema_version: str
    policy_version: str
    competency_code: str
    entries: tuple[HistoricalInferenceState, ...]


# --------------------------------------------------------------------------
# Validation des entrées (pure)
# --------------------------------------------------------------------------

def _invalid(competency_code: str, detail: str) -> InvalidProgressHistoryState:
    return InvalidProgressHistoryState(f"{competency_code} : {detail}")


def _stale(competency_code: str, detail: str) -> StaleProgressHistory:
    return StaleProgressHistory(f"{competency_code} : {detail}")


def _check_arguments(state, progress, competency_code) -> None:
    if type(state) is not AdaptationStateSnapshot:
        raise InvalidProgressHistoryArgument(f"AdaptationStateSnapshot attendu, reçu {type(state).__name__}")
    if type(progress) is not CurrentProgressProjection:
        raise InvalidProgressHistoryArgument(f"CurrentProgressProjection attendue, reçu {type(progress).__name__}")
    if type(competency_code) is not str or competency_code not in COMPETENCY_ORDER:
        raise InvalidProgressHistoryArgument(f"competency_code {competency_code!r} hors de C1..C12")
    user_id = state.user_id
    if type(user_id) is not str or not user_id.strip() or "\x00" in user_id:
        raise InvalidProgressHistoryArgument("state.user_id doit être une chaîne non vide")


def _canonical_card(state: AdaptationStateSnapshot, progress: CurrentProgressProjection,
                    competency_code: str) -> CompetencyCurrentProgress:
    """La carte fournie, seulement si la projection fournie est EXACTEMENT la
    projection canonique 6-4A de ce snapshot (règles jamais recopiées)."""
    cards = progress.competencies
    if type(cards) is not tuple or any(type(card) is not CompetencyCurrentProgress for card in cards):
        raise IncompatibleProgressHistoryInputs("progress.competencies : tuple de CompetencyCurrentProgress attendu")
    matching = [card for card in cards if card.competency_code == competency_code]
    if len(matching) != 1:
        raise IncompatibleProgressHistoryInputs(f"{competency_code} : {len(matching)} carte(s), exactement une attendue")
    try:
        expected = project_current_progress(state=state)
    except ProgressProjectionError as exc:
        raise InvalidProgressHistoryState(f"snapshot non projetable par 6-4A : {exc!r}") from exc
    if progress != expected:
        raise IncompatibleProgressHistoryInputs(
            f"{competency_code} : projection fournie différente de project_current_progress(state)")
    return matching[0]


def _competency_snapshot(state: AdaptationStateSnapshot, card: CompetencyCurrentProgress) -> CompetencyAdaptationSnapshot:
    code = card.competency_code
    matching = [snapshot for snapshot in state.competencies if snapshot.competency_code == code]
    if len(matching) != 1:
        raise _invalid(code, f"{len(matching)} état(s) Step 5 dans le snapshot pour une carte présente")
    snapshot = matching[0]
    if type(snapshot.active_inference_run_id) is not uuid.UUID:
        raise _invalid(code, "active_inference_run_id non uuid.UUID")
    if snapshot.current_stage != card.stage_code:
        raise _invalid(code, f"current_stage {snapshot.current_stage!r} != stade de la carte {card.stage_code!r}")
    return snapshot


def _history(entries: tuple, competency_code: str) -> ProgressHistory:
    return ProgressHistory(
        schema_version=PROGRESS_HISTORY_SCHEMA_VERSION,
        policy_version=PROGRESS_HISTORY_POLICY_VERSION,
        competency_code=competency_code,
        entries=entries,
    )


# --------------------------------------------------------------------------
# Revalidation courante (Step 5) et lecture de la lignée
# --------------------------------------------------------------------------

def _revalidate(db, user_id: str, snapshot: CompetencyAdaptationSnapshot) -> None:
    """La carte représente encore l'état Step 5 COURANT : la lecture sûre
    désigne exactement le run T6 du snapshot. Aucun retry."""
    code = snapshot.competency_code
    try:
        validated = get_validated_user_competency_state(db, user_id=user_id, competency_code=code)
    except StaleInferenceChain as exc:
        raise _stale(code, f"chaîne Step 5 non courante ({exc})") from exc
    except InferenceServiceError as exc:
        raise _invalid(code, f"lecture validée impossible ({exc!r})") from exc
    if validated is None:
        raise _stale(code, "plus aucun état Step 5 validé")
    if validated.active_inference_run_id != snapshot.active_inference_run_id:
        raise _stale(code, f"run T6 courant {validated.active_inference_run_id} != run capturé"
                           f" {snapshot.active_inference_run_id}")
    if validated.current_stage != snapshot.current_stage:
        raise _invalid(code, "même run T6, current_stage divergent du snapshot")


def _read_lineage(db, snapshot: CompetencyAdaptationSnapshot) -> tuple:
    code = snapshot.competency_code
    try:
        return get_inference_lineage(db, run_id=snapshot.active_inference_run_id)
    except InferenceServiceError as exc:
        raise _invalid(code, f"lignée du run T6 {snapshot.active_inference_run_id} illisible ({exc!r})") from exc


def _check_lineage(user_id: str, snapshot: CompetencyAdaptationSnapshot, lineage) -> None:
    """Revérification minimale du contrat du service propriétaire : liens
    contigus, même couple, dernier run = run courant capturé."""
    code = snapshot.competency_code
    if type(lineage) is not tuple or not lineage or any(type(run) is not HistoricalInferenceRun for run in lineage):
        raise _invalid(code, "lignée : tuple non vide de HistoricalInferenceRun attendu")
    predecessor = None
    for run in lineage:
        if (run.user_id, run.competency_code) != (user_id, code):
            raise _invalid(code, f"lignée : run {run.inference_run_id} d'un autre couple")
        if run.predecessor_inference_run_id != predecessor:
            raise _invalid(code, f"lignée : run {run.inference_run_id} non relié au run précédent {predecessor}")
        predecessor = run.inference_run_id
    current = lineage[-1]
    if (current.inference_run_id, current.current_stage) != (snapshot.active_inference_run_id,
                                                            snapshot.current_stage):
        raise _invalid(code, f"lignée : dernier run {current.inference_run_id} ({current.current_stage}) différent"
                             f" du run capturé {snapshot.active_inference_run_id} ({snapshot.current_stage})")


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def acquire_progress_history(
    db,
    *,
    state: AdaptationStateSnapshot,
    progress: CurrentProgressProjection,
    competency_code: str,
) -> ProgressHistory:
    """Lignée adoptée de l'état Step 5 affiché par la carte 6-4A de
    competency_code, oldest -> current, sans identifiant.

    progress doit être EXACTEMENT project_current_progress(state=state).
    Carte sans état : entries (), aucune lecture de base. Sinon : lignée du
    run T6 du snapshot, revalidation Step 5 avant ET après. Lecture seule,
    aucune transaction possédée.

    Erreurs : InvalidProgressHistoryArgument, IncompatibleProgressHistoryInputs,
    InvalidProgressHistoryState, StaleProgressHistory ; jamais de résultat
    partiel, aucun retry."""
    _check_arguments(state, progress, competency_code)
    card = _canonical_card(state, progress, competency_code)
    if not card.state_present:
        return _history((), competency_code)
    snapshot = _competency_snapshot(state, card)

    with db.no_autoflush:
        _revalidate(db, state.user_id, snapshot)
        lineage = _read_lineage(db, snapshot)
        _revalidate(db, state.user_id, snapshot)
    _check_lineage(state.user_id, snapshot, lineage)
    return _history(tuple(HistoricalInferenceState(
        history_token=f"{HISTORY_TOKEN_PREFIX}{position}",
        previous_stage=run.previous_stage,
        current_stage=run.current_stage,
        transition=run.transition,
        transition_cause=run.transition_cause,
    ) for position, run in enumerate(lineage, start=1)), competency_code)
