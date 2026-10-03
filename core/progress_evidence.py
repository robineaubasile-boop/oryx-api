"""Étape 6.4B1 : acquisition SÛRE et DÉTERMINISTE des observations positives
qui expliquent la carte de progression actuelle 6.4A (ProgressEvidenceSet).

Question, et seulement elle : « à partir d'une carte 6.4A et du même état
Step 5 capturé, quelles observations positives, actuelles et traçables ont
réellement servi à établir la claim affichée et peuvent devenir candidates à
une future explication utilisateur ? ».

    6-1A AdaptationStateSnapshot + 6-4A CurrentProgressProjection
    + lectures des services propriétaires (T6 -> T5 -> T3 -> T4 -> T2)
        -> acquire_progress_evidence -> ProgressEvidenceSet

Frontière constitutionnelle. Step 5 possède EXCLUSIVEMENT l'état
pédagogique ; 6-4A décide seule de la projection visible ; 6-4B1 relie
seulement cette carte aux observations positives DÉJÀ citées par Step 5
(refs positive_basis). 6-4B1 acquiert la provenance des preuves existantes,
il ne crée aucune vérité pédagogique nouvelle : il ne choisit ni ne modifie
aucun stade, ne recalcule ni claim ni confiance, ne déduit aucune faiblesse,
ne juge aucune force probante, ne produit aucun score, ne sélectionne aucun
« meilleur exemple » (le maximum d'exemples visibles appartient à 6-4B2), ne
rédige aucun texte utilisateur (6-4B3), n'écrit aucune SupportTrace et ne
persiste rien.

Invariants :

- Versions explicites : PROGRESS_EVIDENCE_SCHEMA_VERSION décrit le contrat de
  sortie ; PROGRESS_EVIDENCE_POLICY_VERSION décrit les règles d'acquisition.

- Carte canonique : progress doit être EXACTEMENT
  project_current_progress(state=state) (égalité structurelle complète des
  douze cartes) ; les règles 6-4A ne sont jamais recopiées. Carte absente,
  dupliquée, falsifiée (stade, couverture, capacité, libellé) =>
  IncompatibleProgressEvidenceInputs ; aucune réparation.

- Statuts (vocabulaire fermé, aucun autre en V1) :
    * no_state : carte sans état Step 5 (state_present False) ;
    * no_positive_basis : current_stage non_etabli (jamais une « preuve
      négative » recherchée : absence de base positive != faiblesse) ;
    * current_stage_not_established : la claim du stade courant est
      not_established ; JAMAIS d'emprunt aux claims inférieures ;
    * available : au moins une candidate sûre (jamais « preuve suffisante »,
      « bonne confiance » ni « niveau fort »).
  Les trois premiers sont des fonctions PURES de la carte et du snapshot :
  aucune lecture de base, basis_origin None, source_claim_stage None, ().

- Claim source : claim du stade courant direct => ses propres refs
  positive_basis (basis_origin direct, source = current_stage) ;
  implied_by_higher_claim => claim DIRECTE établie supérieure la plus proche
  dans l'ordre conceptuel (basis_origin inherited_from_higher_claim) ; aucune
  source, ou Mastery implied (aucun stade supérieur) =>
  InvalidProgressEvidenceState. Jamais une claim inférieure.

- Run expliqué : EXACTEMENT CompetencyAdaptationSnapshot.active_inference_run_id,
  relu explicitement (get_competency_inference / get_stage_claims /
  get_inference_basis_refs(run_id)), jamais via une API « active courante ».
  La vue reste COURANTE : get_validated_user_competency_state AVANT puis
  APRÈS l'acquisition doit désigner ce même run (sinon, ou chaîne devenue
  non courante, StaleProgressEvidence). Aucun retry, aucune boucle, aucun
  verrou : READ COMMITTED, cohérence par immutabilité des lignes relues.

- Claims T6 relues : stade, positive_basis_status et basis_mode des claims
  nécessaires (du stade courant jusqu'à la source) = ceux du snapshot ;
  basis_summary / scope_summary (audit-only) ne sont jamais lus.

- positive_basis : refs de la SEULE claim source, toutes source_kind
  observation avec source_observation_id ; dependency / transfer /
  revalidation, observation en double, claim directe sans ref, ref propre à
  une claim implied => InvalidProgressEvidenceState (jamais ignoré, jamais
  dédupliqué). Chaque observation appartient au snapshot T5 RÉELLEMENT
  consommé (get_longitudinal_inputs du run T5 du snapshot).

- Provenance par observation : même compétence, supportive (contradictory =>
  invalide), valid (invalidated => stale) ; run T3 completed / active
  (completed / superseded => stale, tout autre état impossible => invalide) ;
  événement finalized du même utilisateur (sinon invalide : un événement
  finalized ne redevient jamais open) ; observation_text str non vide sans
  NUL, conservé tel quel (jamais reformulé, tronqué ni analysé) ;
  elicitation_mode et support_level dans les vocabulaires T3, conservés sans
  jamais filtrer, scorer ni classer.

- Périmètre : celui de la carte, c'est-à-dire
  represented_capability_definition_ids de la claim du stade courant
  (identité sémantique = definition_id, jamais capability_code). Une
  observation localized porte les definition_id de ses mappings T4 (release
  de SON run T3, même compétence) présents dans le catalogue du snapshot
  (aucun : invalide) ; une observation competency_only n'a aucun mapping.
    * competency_only : admise seulement si la carte est competency_only ou
      mixed ; candidate sans capacité ;
    * localized : candidate portant l'intersection avec le périmètre de la
      carte, dans l'ordre du catalogue du snapshot ; intersection vide =>
      invalide pour une source Discovery / Comprehension / Application
      (6-1A y définit le périmètre comme l'union de ces bases) ; pour une
      source Mastery (directe ou héritée), le périmètre visible est celui du
      mastery_assessment, possiblement PLUS ÉTROIT que ses bases : la
      provenance reste validée mais l'observation ne devient pas candidate
      (y compris sur une carte competency_only, cas réel de T6-C1 : source
      d'un transfert localisée, cible competency_only) ;
    * mixed : au moins une candidate de chaque famille ;
    * zéro candidate restante => invalide (aucun statut de repli).
  Jeton de capacité : "{capability_code}@r{semantic_revision}" résolu par
  definition_id dans CompetencyAdaptationSnapshot.capabilities (jamais la
  release active globale, jamais un remapping par code).

- Ordre : celui, technique et stable, des refs de get_inference_basis_refs ;
  jetons éphémères evidence_1, evidence_2, ... (aucun UUID, aucune
  persistance). Aucun tri par force probante, date, aide ou mode.

- Exclusions : aucun identifiant d'utilisateur / run / observation /
  événement / release / définition / membership, aucun UUID, aucun
  horodatage, aucun evidence_strength, observation_role, local_stage,
  ordinal, error_type, residual_cognitive_work ni source_contribution_refs ;
  aucun score, poids, confiance, rang, compteur public ; aucun texte final.

- Lecture seule : uniquement des lectures des services propriétaires, sous
  db.no_autoflush ; jamais add / flush / commit / rollback / UPDATE /
  DELETE / INSERT ; aucun modèle, aucune migration, aucun cache, aucun
  branchement runtime.

Erreurs : InvalidProgressEvidenceArgument (types, code inconnu),
IncompatibleProgressEvidenceInputs (carte non canonique pour ce snapshot),
InvalidProgressEvidenceState (corruption / incohérence),
StaleProgressEvidence (évolution amont légitime : la vue n'est plus
courante). Jamais de résultat partiel.
"""
import uuid
from dataclasses import dataclass
from typing import NamedTuple

from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateSnapshot,
    CompetencyAdaptationSnapshot,
)
from core.cognitive_capture import FINALIZED, CognitiveCaptureError, get_event
from core.inference_service import (
    ACTIVE,
    BASIS_MODE_NONE,
    CLAIM_STAGES,
    COMPETENCY_ONLY,
    COMPLETED,
    DIRECT,
    ESTABLISHED,
    IMPLIED_BY_HIGHER_CLAIM,
    LOCALIZED,
    MASTERY,
    NON_ETABLI,
    NOT_ESTABLISHED,
    POSITIVE_BASIS,
    SOURCE_OBSERVATION,
    SUPERSEDED,
    InferenceServiceError,
    StaleInferenceChain,
    get_competency_inference,
    get_inference_basis_refs,
    get_stage_claims,
    get_validated_user_competency_state,
)
from core.longitudinal_service import LongitudinalServiceError, get_longitudinal_assessment, get_longitudinal_inputs
from core.observation_service import (
    CONTRADICTORY,
    ELICITATION_MODES,
    INVALIDATED,
    SUPPORT_LEVELS,
    SUPPORTIVE,
    VALID,
    ObservationServiceError,
    get_evaluation_run,
    get_observation,
)
from core.progress_projection import (
    COVERAGE_COMPETENCY_ONLY,
    COVERAGE_LOCALIZED,
    COVERAGE_MIXED,
    CompetencyCurrentProgress,
    CurrentProgressProjection,
    ProgressProjectionError,
    project_current_progress,
)
from core.taxonomy_service import TaxonomyServiceError, get_observation_capabilities

PROGRESS_EVIDENCE_SCHEMA_VERSION = "progress-evidence-set-v1"
PROGRESS_EVIDENCE_POLICY_VERSION = "progress-evidence-policy-1"

# ProgressEvidenceSet.evidence_status : vocabulaire fermé V1.
EVIDENCE_NO_STATE = "no_state"
EVIDENCE_NO_POSITIVE_BASIS = "no_positive_basis"
EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED = "current_stage_not_established"
EVIDENCE_AVAILABLE = "available"
EVIDENCE_STATUSES = (
    EVIDENCE_NO_STATE,
    EVIDENCE_NO_POSITIVE_BASIS,
    EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED,
    EVIDENCE_AVAILABLE,
)

# ProgressEvidenceSet.basis_origin : vocabulaire fermé V1 (None hors available).
BASIS_DIRECT = "direct"
BASIS_INHERITED_FROM_HIGHER_CLAIM = "inherited_from_higher_claim"
BASIS_ORIGINS = (BASIS_DIRECT, BASIS_INHERITED_FROM_HIGHER_CLAIM)

# ProgressEvidenceCandidate.scope_mode : vocabulaire fermé V1.
SCOPE_LOCALIZED = "localized"
SCOPE_COMPETENCY_ONLY = "competency_only"
SCOPE_MODES = (SCOPE_LOCALIZED, SCOPE_COMPETENCY_ONLY)

# (positive_basis_status, basis_mode) cohérents d'une claim Step 5.
_CLAIM_MODES = frozenset({(ESTABLISHED, DIRECT), (ESTABLISHED, IMPLIED_BY_HIGHER_CLAIM),
                          (NOT_ESTABLISHED, BASIS_MODE_NONE)})


class ProgressEvidenceError(Exception):
    """Erreur métier de 6-4B1 : jamais un ensemble de preuves partiel."""


class InvalidProgressEvidenceArgument(ProgressEvidenceError):
    """Argument top-level d'un type inattendu ou compétence inconnue."""


class IncompatibleProgressEvidenceInputs(ProgressEvidenceError):
    """La projection fournie n'est pas EXACTEMENT la projection canonique
    6-4A du snapshot fourni."""


class InvalidProgressEvidenceState(ProgressEvidenceError):
    """Chaîne probante incohérente ou corrompue : jamais réparée."""


class StaleProgressEvidence(ProgressEvidenceError):
    """Évolution amont légitime : la vue n'est plus l'état courant ; le
    runtime reconstruit snapshot, projection et preuves (aucun retry ici)."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceCandidate:
    """Observation positive réellement citée par la claim source, candidate
    à une future explication. evidence_token : référence éphémère ;
    observation_text : matière INTERNE (jamais un texte utilisateur) ;
    capability_tokens : "{capability_code}@r{semantic_revision}" dans l'ordre
    du catalogue du snapshot, () pour competency_only ; elicitation_mode et
    support_level : provenance cognitive, jamais un filtre ni un score."""
    evidence_token: str
    observation_text: str
    scope_mode: str
    capability_tokens: tuple[str, ...]
    elicitation_mode: str
    support_level: str


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceSet:
    """Candidats de preuve d'UNE carte 6-4A. Aucune identité de personne,
    aucun identifiant technique, aucun compteur."""
    schema_version: str
    policy_version: str

    competency_code: str
    evidence_status: str

    basis_origin: str | None
    source_claim_stage: str | None

    candidates: tuple[ProgressEvidenceCandidate, ...]


# --------------------------------------------------------------------------
# Enregistrements acquis : copies immuables des lignes lues
# --------------------------------------------------------------------------

class _ClaimRecord(NamedTuple):
    id: uuid.UUID
    inference_run_id: uuid.UUID
    stage: str
    positive_basis_status: str
    basis_mode: str


class _RefRecord(NamedTuple):
    inference_run_id: uuid.UUID
    stage_claim_id: uuid.UUID | None
    ref_role: str
    source_kind: str
    source_observation_id: uuid.UUID | None


class _MappingRecord(NamedTuple):
    taxonomy_release_id: uuid.UUID
    definition_id: uuid.UUID
    competency_code: str


class _ObservationRecord(NamedTuple):
    id: uuid.UUID
    competency_code: str
    polarity: str
    integrity_status: str
    capability_localization: str
    observation_text: object
    elicitation_mode: object
    support_level: object
    evaluation_run_id: uuid.UUID
    run_id: uuid.UUID
    run_execution_status: str
    run_interpretation_status: str
    run_taxonomy_release_id: uuid.UUID | None
    run_event_id: uuid.UUID
    event_id: uuid.UUID
    event_user_id: str
    event_status: str
    mappings: tuple


# --------------------------------------------------------------------------
# Validation des entrées (pure)
# --------------------------------------------------------------------------

def _invalid(competency_code: str, detail: str) -> InvalidProgressEvidenceState:
    return InvalidProgressEvidenceState(f"{competency_code} : {detail}")


def _stale(competency_code: str, detail: str) -> StaleProgressEvidence:
    return StaleProgressEvidence(f"{competency_code} : {detail}")


def _check_arguments(state, progress, competency_code) -> None:
    if type(state) is not AdaptationStateSnapshot:
        raise InvalidProgressEvidenceArgument(f"AdaptationStateSnapshot attendu, reçu {type(state).__name__}")
    if type(progress) is not CurrentProgressProjection:
        raise InvalidProgressEvidenceArgument(f"CurrentProgressProjection attendue, reçu {type(progress).__name__}")
    if type(competency_code) is not str or competency_code not in COMPETENCY_ORDER:
        raise InvalidProgressEvidenceArgument(f"competency_code {competency_code!r} hors de C1..C12")
    user_id = state.user_id
    if type(user_id) is not str or not user_id.strip() or "\x00" in user_id:
        raise InvalidProgressEvidenceArgument("state.user_id doit être une chaîne non vide")


def _canonical_card(state: AdaptationStateSnapshot, progress: CurrentProgressProjection,
                    competency_code: str) -> CompetencyCurrentProgress:
    """La carte fournie, seulement si la projection fournie est EXACTEMENT la
    projection canonique 6-4A de ce snapshot (règles jamais recopiées)."""
    cards = progress.competencies
    if type(cards) is not tuple or any(type(card) is not CompetencyCurrentProgress for card in cards):
        raise IncompatibleProgressEvidenceInputs("progress.competencies : tuple de CompetencyCurrentProgress attendu")
    matching = [card for card in cards if card.competency_code == competency_code]
    if len(matching) != 1:
        raise IncompatibleProgressEvidenceInputs(f"{competency_code} : {len(matching)} carte(s), exactement une attendue")
    try:
        expected = project_current_progress(state=state)
    except ProgressProjectionError as exc:
        raise InvalidProgressEvidenceState(f"snapshot non projetable par 6-4A : {exc!r}") from exc
    if progress != expected:
        raise IncompatibleProgressEvidenceInputs(
            f"{competency_code} : projection fournie différente de project_current_progress(state)")
    return matching[0]


def _competency_snapshot(state: AdaptationStateSnapshot, competency_code: str) -> CompetencyAdaptationSnapshot:
    matching = [snapshot for snapshot in state.competencies if snapshot.competency_code == competency_code]
    if len(matching) != 1:
        raise _invalid(competency_code, f"{len(matching)} état(s) Step 5 dans le snapshot pour une carte présente")
    snapshot = matching[0]
    for name in ("active_inference_run_id", "longitudinal_assessment_run_id"):
        if type(getattr(snapshot, name)) is not uuid.UUID:
            raise _invalid(competency_code, f"{name} non uuid.UUID")
    return snapshot


def _snapshot_claims(snapshot: CompetencyAdaptationSnapshot) -> dict:
    """stage -> AdaptationStageClaim, exactement discovery -> mastery ; statut
    et basis_mode cohérents (vocabulaire Step 5) pour le stade courant et
    les stades SUPÉRIEURS seulement (seuls candidats de claim source)."""
    code = snapshot.competency_code
    claims = snapshot.claims
    if type(claims) is not tuple or any(type(claim) is not AdaptationStageClaim for claim in claims):
        raise _invalid(code, "claims : tuple d'AdaptationStageClaim attendu")
    if tuple(claim.stage for claim in claims) != CLAIM_STAGES:
        raise _invalid(code, f"claims : exactement {CLAIM_STAGES} attendues")
    for claim in claims[CLAIM_STAGES.index(snapshot.current_stage):]:
        if (claim.status, claim.basis_mode) not in _CLAIM_MODES:
            raise _invalid(code, f"{claim.stage} : {claim.status!r} / {claim.basis_mode!r} incohérents")
    return {claim.stage: claim for claim in claims}


def _source_stage(code: str, current_stage: str, claims: dict) -> str:
    """Stade de la claim qui porte RÉELLEMENT la base positive de la claim
    courante établie : elle-même si directe, sinon la claim directe établie
    supérieure la plus proche. Jamais une claim inférieure."""
    current = claims[current_stage]
    if current.basis_mode == DIRECT:
        return current_stage
    higher = CLAIM_STAGES[CLAIM_STAGES.index(current_stage) + 1:]
    if not higher:
        raise _invalid(code, f"{MASTERY} {IMPLIED_BY_HIGHER_CLAIM} : aucun stade supérieur")
    source = next((stage for stage in higher
                   if claims[stage].status == ESTABLISHED and claims[stage].basis_mode == DIRECT), None)
    if source is None:
        raise _invalid(code, f"{current_stage} {IMPLIED_BY_HIGHER_CLAIM} sans claim directe établie supérieure")
    return source


def _empty(competency_code: str, status: str) -> ProgressEvidenceSet:
    return ProgressEvidenceSet(
        schema_version=PROGRESS_EVIDENCE_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_POLICY_VERSION,
        competency_code=competency_code,
        evidence_status=status,
        basis_origin=None,
        source_claim_stage=None,
        candidates=(),
    )


# --------------------------------------------------------------------------
# Revalidation courante (Step 5) et lectures des services propriétaires
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
    if validated.longitudinal_assessment_run_id != snapshot.longitudinal_assessment_run_id:
        raise _invalid(code, "même run T6, run T5 parent divergent du snapshot")


def _read_t6(db, run_id: uuid.UUID) -> tuple:
    """(run, claims, refs) du run CAPTURÉ (jamais l'active courante)."""
    run = get_competency_inference(db, run_id=run_id)
    claims = tuple(_ClaimRecord(c.id, c.inference_run_id, c.stage, c.positive_basis_status, c.basis_mode)
                   for c in get_stage_claims(db, run_id=run_id))
    refs = tuple(_RefRecord(r.inference_run_id, r.stage_claim_id, r.ref_role, r.source_kind, r.source_observation_id)
                 for r in get_inference_basis_refs(db, run_id=run_id))
    return run, claims, refs


def _read_observation(db, observation_id: uuid.UUID) -> _ObservationRecord:
    observation = get_observation(db, observation_id=observation_id)
    evaluation_run = get_evaluation_run(db, run_id=observation.evaluation_run_id)
    event = get_event(db, event_id=evaluation_run.event_id)
    return _ObservationRecord(
        id=observation.id,
        competency_code=observation.competency_code,
        polarity=observation.polarity,
        integrity_status=observation.integrity_status,
        capability_localization=observation.capability_localization,
        observation_text=observation.observation_text,
        elicitation_mode=observation.elicitation_mode,
        support_level=observation.support_level,
        evaluation_run_id=observation.evaluation_run_id,
        run_id=evaluation_run.id,
        run_execution_status=evaluation_run.execution_status,
        run_interpretation_status=evaluation_run.interpretation_status,
        run_taxonomy_release_id=evaluation_run.pedagogical_taxonomy_release_id,
        run_event_id=evaluation_run.event_id,
        event_id=event.id,
        event_user_id=event.user_id,
        event_status=event.status,
        mappings=tuple(_MappingRecord(membership.taxonomy_release_id, definition.id, definition.competency_code)
                       for _, membership, definition in get_observation_capabilities(
                           db, observation_id=observation_id)),
    )


# --------------------------------------------------------------------------
# Vérifications de la chaîne probante (pures)
# --------------------------------------------------------------------------

def _check_run(user_id: str, snapshot: CompetencyAdaptationSnapshot, run) -> None:
    code = snapshot.competency_code
    if (run.id, run.user_id, run.competency_code, run.longitudinal_assessment_run_id, run.current_stage) != (
            snapshot.active_inference_run_id, user_id, code, snapshot.longitudinal_assessment_run_id,
            snapshot.current_stage):
        raise _invalid(code, f"run T6 relu {run.id} divergent du snapshot capturé")
    if run.execution_status != COMPLETED:
        raise _invalid(code, f"run T6 {run.id} {run.execution_status} ({COMPLETED} attendu)")
    if run.interpretation_status == SUPERSEDED:
        raise _stale(code, f"run T6 {run.id} {SUPERSEDED} pendant l'acquisition")
    if run.interpretation_status != ACTIVE:
        raise _invalid(code, f"run T6 {run.id} {run.interpretation_status} ({ACTIVE} attendu)")


def _check_claims(code: str, run_id: uuid.UUID, rows: tuple, claims: dict, current_stage: str,
                  source_stage: str) -> dict:
    """stage -> claim T6 ; les claims du stade courant jusqu'à la source sont
    EXACTEMENT celles du snapshot (stade, statut, basis_mode)."""
    by_stage = {}
    for row in rows:
        if row.inference_run_id != run_id:
            raise _invalid(code, f"claim {row.id} d'un autre run que {run_id}")
        if row.stage not in CLAIM_STAGES or row.stage in by_stage:
            raise _invalid(code, f"claim T6 {row.stage!r} hors vocabulaire ou en double")
        by_stage[row.stage] = row
    if len(by_stage) != len(CLAIM_STAGES):
        raise _invalid(code, f"run T6 {run_id} : {len(by_stage)} claim(s), exactement {len(CLAIM_STAGES)} attendues")
    for stage in CLAIM_STAGES[CLAIM_STAGES.index(current_stage):CLAIM_STAGES.index(source_stage) + 1]:
        row, claim = by_stage[stage], claims[stage]
        if (row.positive_basis_status, row.basis_mode) != (claim.status, claim.basis_mode):
            raise _invalid(code, f"claim T6 {stage} ({row.positive_basis_status} / {row.basis_mode}) divergente"
                                 f" du snapshot ({claim.status} / {claim.basis_mode})")
    return by_stage


def _positive_observation_ids(code: str, run_id: uuid.UUID, refs: tuple, by_stage: dict, current_stage: str,
                              source_stage: str) -> tuple:
    """observation_id des refs positive_basis de la SEULE claim source, dans
    l'ordre technique des refs. Aucune ref propre aux claims entre le stade
    courant et la source ; observation seulement ; aucun doublon."""
    between = {by_stage[stage].id: stage for stage in
               CLAIM_STAGES[CLAIM_STAGES.index(current_stage):CLAIM_STAGES.index(source_stage)]}
    source_id = by_stage[source_stage].id
    observation_ids = []
    for ref in refs:
        if ref.inference_run_id != run_id:
            raise _invalid(code, f"ref d'un autre run que {run_id}")
        if ref.ref_role != POSITIVE_BASIS:
            continue
        if ref.stage_claim_id in between:
            raise _invalid(code, f"claim {between[ref.stage_claim_id]} sans base directe avec une ref positive_basis")
        if ref.stage_claim_id != source_id:
            continue
        if ref.source_kind != SOURCE_OBSERVATION or ref.source_observation_id is None:
            raise _invalid(code, f"ref positive_basis {source_stage} : source {ref.source_kind!r}"
                                 f" ({SOURCE_OBSERVATION} requise)")
        if ref.source_observation_id in observation_ids:
            raise _invalid(code, f"ref positive_basis {source_stage} en double vers {ref.source_observation_id}")
        observation_ids.append(ref.source_observation_id)
    if not observation_ids:
        raise _invalid(code, f"claim {source_stage} {DIRECT} {ESTABLISHED} sans ref positive_basis")
    return tuple(observation_ids)


def _check_parent(user_id: str, snapshot: CompetencyAdaptationSnapshot, parent) -> None:
    code = snapshot.competency_code
    if (parent.id, parent.user_id, parent.competency_code) != (snapshot.longitudinal_assessment_run_id, user_id, code):
        raise _invalid(code, f"run T5 relu {parent.id} divergent du snapshot capturé")
    if parent.execution_status != COMPLETED:
        raise _invalid(code, f"run T5 {parent.id} {parent.execution_status} ({COMPLETED} attendu)")
    if parent.interpretation_status == SUPERSEDED:
        raise _stale(code, f"run T5 {parent.id} {SUPERSEDED} pendant l'acquisition")
    if parent.interpretation_status != ACTIVE:
        raise _invalid(code, f"run T5 {parent.id} {parent.interpretation_status} ({ACTIVE} attendu)")


def _check_observation(user_id: str, code: str, observation_id: uuid.UUID, record: _ObservationRecord) -> None:
    """Provenance d'UNE observation positive : stale pour une évolution amont
    légitime, invalide pour toute incohérence."""
    label = f"observation {observation_id}"
    if record.id != observation_id:
        raise _invalid(code, f"{label} relue sous l'identifiant {record.id}")
    if record.competency_code != code:
        raise _invalid(code, f"{label} de {record.competency_code} citée en positive_basis")
    if record.polarity == CONTRADICTORY:
        raise _invalid(code, f"{label} {CONTRADICTORY} citée en positive_basis")
    if record.polarity != SUPPORTIVE:
        raise _invalid(code, f"{label} : polarité {record.polarity!r} inconnue")
    if record.integrity_status == INVALIDATED:
        raise _stale(code, f"{label} {INVALIDATED} depuis la capture")
    if record.integrity_status != VALID:
        raise _invalid(code, f"{label} : integrity_status {record.integrity_status!r} inconnu")
    if record.run_id != record.evaluation_run_id:
        raise _invalid(code, f"{label} : run T3 relu {record.run_id} != {record.evaluation_run_id}")
    if record.run_execution_status != COMPLETED:
        raise _invalid(code, f"{label} : run T3 {record.run_execution_status} ({COMPLETED} attendu)")
    if record.run_interpretation_status == SUPERSEDED:
        raise _stale(code, f"{label} : run T3 {SUPERSEDED} depuis la capture")
    if record.run_interpretation_status != ACTIVE:
        raise _invalid(code, f"{label} : run T3 {record.run_interpretation_status} ({ACTIVE} attendu)")
    if record.event_id != record.run_event_id:
        raise _invalid(code, f"{label} : événement relu {record.event_id} != {record.run_event_id}")
    if record.event_user_id != user_id:
        raise _invalid(code, f"{label} : événement d'un autre utilisateur")
    if record.event_status != FINALIZED:
        raise _invalid(code, f"{label} : événement {record.event_status} ({FINALIZED} attendu)")
    content = record.observation_text
    if type(content) is not str or not content.strip() or "\x00" in content:
        raise _invalid(code, f"{label} : observation_text vide, non str ou avec NUL")
    if type(record.elicitation_mode) is not str or record.elicitation_mode not in ELICITATION_MODES:
        raise _invalid(code, f"{label} : elicitation_mode {record.elicitation_mode!r} hors vocabulaire")
    if type(record.support_level) is not str or record.support_level not in SUPPORT_LEVELS:
        raise _invalid(code, f"{label} : support_level {record.support_level!r} hors vocabulaire")


def _compatible_definitions(code: str, record: _ObservationRecord, catalogue: dict) -> frozenset:
    """definition_id d'une observation localized : mappings T4 de la release
    de SON run T3 et de la compétence, présents dans le catalogue du
    snapshot ; jamais par capability_code. competency_only : aucun mapping."""
    label = f"observation {record.id}"
    if record.capability_localization == COMPETENCY_ONLY:
        if record.mappings:
            raise _invalid(code, f"{label} {COMPETENCY_ONLY} avec {len(record.mappings)} mapping(s)")
        return frozenset()
    if record.capability_localization != LOCALIZED:
        raise _invalid(code, f"{label} : capability_localization {record.capability_localization!r} inconnue")
    if record.run_taxonomy_release_id is None:
        raise _invalid(code, f"{label} : run T3 sans release de taxonomie")
    compatible = frozenset(m.definition_id for m in record.mappings
                           if m.taxonomy_release_id == record.run_taxonomy_release_id
                           and m.competency_code == code and m.definition_id in catalogue)
    if not compatible:
        raise _invalid(code, f"{label} {LOCALIZED} sans capacité compatible avec le catalogue du snapshot")
    return compatible


def _candidates(code: str, card: CompetencyCurrentProgress, snapshot: CompetencyAdaptationSnapshot,
                represented: tuple, source_stage: str, records: tuple) -> tuple:
    """Candidates dans l'ordre des refs ; cohérence avec la couverture de la
    carte ; règle Mastery (périmètre visible possiblement plus étroit)."""
    catalogue = {capability.definition_id: capability for capability in snapshot.capabilities}
    visible = frozenset(represented)
    mode = card.coverage_mode
    if mode not in (COVERAGE_LOCALIZED, COVERAGE_COMPETENCY_ONLY, COVERAGE_MIXED):
        raise _invalid(code, f"carte {mode!r} pour une claim {ESTABLISHED}")
    candidates, families = [], set()
    for record in records:
        compatible = _compatible_definitions(code, record, catalogue)
        if record.capability_localization == COMPETENCY_ONLY:
            if mode == COVERAGE_LOCALIZED:
                raise _invalid(code, f"observation {record.id} {COMPETENCY_ONLY} sur une carte {COVERAGE_LOCALIZED}")
            scope_mode, tokens = SCOPE_COMPETENCY_ONLY, ()
        else:
            shown = compatible & visible
            if not shown:
                if source_stage == MASTERY:
                    continue  # provenance validée, hors du périmètre canonique visible
                raise _invalid(code, f"observation {record.id} {LOCALIZED} hors du périmètre de la carte {mode}"
                                     f" (source {source_stage})")
            scope_mode = SCOPE_LOCALIZED
            tokens = tuple(f"{capability.capability_code}@r{capability.semantic_revision}"
                           for capability in snapshot.capabilities if capability.definition_id in shown)
        families.add(scope_mode)
        candidates.append(ProgressEvidenceCandidate(
            evidence_token=f"evidence_{len(candidates) + 1}",
            observation_text=record.observation_text,
            scope_mode=scope_mode,
            capability_tokens=tokens,
            elicitation_mode=record.elicitation_mode,
            support_level=record.support_level,
        ))
    if mode == COVERAGE_MIXED and families != set(SCOPE_MODES):
        raise _invalid(code, f"carte {COVERAGE_MIXED} sans candidate de chaque famille"
                             f" ({tuple(m for m in SCOPE_MODES if m in families)})")
    if not candidates:
        raise _invalid(code, f"claim {source_stage} sans aucune candidate dans le périmètre de la carte")
    return tuple(candidates)


# --------------------------------------------------------------------------
# Acquisition d'une claim établie
# --------------------------------------------------------------------------

def _acquire(db, user_id: str, card: CompetencyCurrentProgress, snapshot: CompetencyAdaptationSnapshot,
             claims: dict, source_stage: str) -> tuple:
    code, run_id = snapshot.competency_code, snapshot.active_inference_run_id
    current_stage = snapshot.current_stage
    try:
        run, claim_rows, refs = _read_t6(db, run_id)
        _check_run(user_id, snapshot, run)
        by_stage = _check_claims(code, run_id, claim_rows, claims, current_stage, source_stage)
        observation_ids = _positive_observation_ids(code, run_id, refs, by_stage, current_stage, source_stage)
        parent = get_longitudinal_assessment(db, run_id=snapshot.longitudinal_assessment_run_id)
        _check_parent(user_id, snapshot, parent)
        consumed = frozenset(i.observation_id for i in get_longitudinal_inputs(db, run_id=parent.id))
        outside = [str(i) for i in observation_ids if i not in consumed]
        if outside:
            raise _invalid(code, f"observations {outside} hors du snapshot T5 {parent.id}")
        records = []
        for observation_id in observation_ids:
            record = _read_observation(db, observation_id)
            _check_observation(user_id, code, observation_id, record)
            records.append(record)
    except (InferenceServiceError, LongitudinalServiceError, ObservationServiceError, TaxonomyServiceError,
            CognitiveCaptureError) as exc:
        raise _invalid(code, f"run T6 {run_id} : lecture impossible ({exc!r})") from exc
    return _candidates(code, card, snapshot, claims[current_stage].represented_capability_definition_ids,
                       source_stage, tuple(records))


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def acquire_progress_evidence(
    db,
    *,
    state: AdaptationStateSnapshot,
    progress: CurrentProgressProjection,
    competency_code: str,
) -> ProgressEvidenceSet:
    """Observations positives réellement citées par la claim qui établit la
    carte 6-4A de competency_code, candidates à une future explication.

    progress doit être EXACTEMENT project_current_progress(state=state).
    no_state / no_positive_basis / current_stage_not_established : purs,
    aucune lecture de base. available : run T6 du snapshot relu
    explicitement, revalidation Step 5 avant ET après, chaîne T6 -> T5 -> T3
    -> T4 -> T2 vérifiée. Lecture seule, aucune transaction possédée.

    Erreurs : InvalidProgressEvidenceArgument, IncompatibleProgressEvidenceInputs,
    InvalidProgressEvidenceState, StaleProgressEvidence ; jamais de résultat
    partiel, aucun retry."""
    _check_arguments(state, progress, competency_code)
    card = _canonical_card(state, progress, competency_code)
    if not card.state_present:
        return _empty(competency_code, EVIDENCE_NO_STATE)
    if card.stage_code == NON_ETABLI:
        return _empty(competency_code, EVIDENCE_NO_POSITIVE_BASIS)
    snapshot = _competency_snapshot(state, competency_code)
    claims = _snapshot_claims(snapshot)
    current_stage = snapshot.current_stage
    if current_stage != card.stage_code:
        raise _invalid(competency_code, f"current_stage {current_stage!r} != stade de la carte {card.stage_code!r}")
    if claims[current_stage].status == NOT_ESTABLISHED:
        return _empty(competency_code, EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED)
    source_stage = _source_stage(competency_code, current_stage, claims)

    with db.no_autoflush:
        _revalidate(db, state.user_id, snapshot)
        candidates = _acquire(db, state.user_id, card, snapshot, claims, source_stage)
        _revalidate(db, state.user_id, snapshot)
    return ProgressEvidenceSet(
        schema_version=PROGRESS_EVIDENCE_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_POLICY_VERSION,
        competency_code=competency_code,
        evidence_status=EVIDENCE_AVAILABLE,
        basis_origin=BASIS_DIRECT if source_stage == current_stage else BASIS_INHERITED_FROM_HIGHER_CLAIM,
        source_claim_stage=source_stage,
        candidates=candidates,
    )
