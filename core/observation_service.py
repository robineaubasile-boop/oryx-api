"""Service interne transactionnel des observations pédagogiques (T3-B,
niveau 3 : interprétation locale).

Seul point d'écriture applicatif de ObservationEvaluationRun et
PedagogicalObservation : toute mutation future de ces tables doit passer par
ce module (même philosophie que core/cognitive_capture.py pour T2).

Ce service n'est PAS l'évaluateur. Il reçoit une interprétation locale déjà
produite (compétence, polarité, force de preuve, local_stage...), la valide
structurellement et en garantit le lifecycle, l'atomicité, la provenance et
l'immutabilité. Il ne décide ni ne déduit rien : aucune inférence
longitudinale, aucun score, aucune confiance, aucune progression.

Invariants :

- Transactions : le service ne fait jamais commit() ni rollback(). Il
  valide, verrouille, vérifie l'état sous verrou, mute puis flush() ; la
  frontière transactionnelle appartient à l'appelant (un orchestrateur doit
  pouvoir combiner atomiquement écritures métier, cognitives et
  d'observation). Seule exception, locale et invisible pour l'appelant :
  l'INSERT d'un run est isolé dans un SAVEPOINT pour traduire un conflit de
  déduplication sans rendre inutilisable la transaction de l'appelant
  (voir start_evaluation_run).

- Lifecycle du run (execution_status / interpretation_status) :
      start     -> running   / candidate
      complete  -> completed / active     (l'ancien active -> superseded)
      fail      -> failed    / obsolete   (l'ancien active est inchangé)
  completed, failed, superseded et obsolete sont terminaux pour le run :
  plus d'observation ni de transition (une seconde transition est refusée,
  jamais un no-op). Un run completed sans aucune observation est une
  évaluation réussie n'ayant rien observé : jamais d'observation factice.
  Seul un CognitiveEvent finalized est évaluable.

- Observations : append-only et immuables. Après création, la seule
  mutation métier est valid -> invalidated (définitive, pas de
  revalidation). Une nouvelle conclusion = un nouveau run, jamais la
  réécriture d'un ancien raisonnement. La supersession appartient au run :
  les observations d'un run superseded ou obsolete restent intactes.

- Verrous et ordre global (anti-deadlock) :
      run muté  ->  CognitiveEvent parent  ->  run active courant
  * add_observation / fail_evaluation_run : verrou du seul run concerné.
  * complete_evaluation_run : run candidat, puis CognitiveEvent parent
    (sérialise toutes les activations d'un même événement), puis run active
    courant relu sous ce verrou parent.
  * start_evaluation_run : CognitiveEvent seul ; le run réévalué est lu
    sans verrou explicite (event_id et existence sont immuables).
  * invalidate_observation : verrou de la seule observation.
  Les runs sont verrouillés en SELECT ... FOR NO KEY UPDATE et non FOR
  UPDATE : l'INSERT d'un run qui réévalue un autre run (ou d'une
  observation) prend automatiquement un FOR KEY SHARE sur la ligne
  référencée (contrôle de FK PostgreSQL), qui entre en conflit avec FOR
  UPDATE mais pas avec FOR NO KEY UPDATE. Avec FOR UPDATE, start (détient
  l'événement, attend le run réévalué) et add_observation + complete sur ce
  même run (détient le run, attend l'événement) formeraient un cycle.
  FOR NO KEY UPDATE reste exclusif entre toutes les opérations du service
  (aucune ne modifie une colonne clé). Chaque verrou utilise
  populate_existing : un objet déjà présent dans la Session est rechargé
  après l'attente, l'état n'est jamais lu avant le verrou.

- Payloads JSON : validés strictement (types JSON exacts, flottants finis,
  clés str, pas de cycle, pas de NUL) puis copiés en profondeur ; rien
  n'est converti silencieusement. Logique volontairement dupliquée de T2-B
  plutôt que partagée, pour garder la frontière T2/T3 nette.

Les garanties d'immutabilité sont celles de ce service (aucun trigger en
base) : elles ne couvrent pas une modification directe des objets ORM par
un appelant qui contournerait ce module. Les contraintes PostgreSQL de
0006_observation_layer (CHECK, UNIQUE, index unique partiel « un seul
active par événement ») restent la défense finale.
"""
import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.models import CognitiveEvent, ObservationEvaluationRun, PedagogicalObservation

# CognitiveEvent.status évaluable (vocabulaire T2).
FINALIZED = "finalized"

# ObservationEvaluationRun.execution_status
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

# ObservationEvaluationRun.interpretation_status
CANDIDATE = "candidate"
ACTIVE = "active"
SUPERSEDED = "superseded"
OBSOLETE = "obsolete"

# PedagogicalObservation.integrity_status
VALID = "valid"
INVALIDATED = "invalidated"

SUPPORTIVE = "supportive"
CONTRADICTORY = "contradictory"

# Vocabulaires fermés : identiques aux CHECK de 0006_observation_layer.
COMPETENCY_CODES = frozenset(f"C{i}" for i in range(1, 13))
OBSERVATION_ROLES = frozenset({"primary", "secondary"})
ELICITATION_MODES = frozenset({"prompted", "spontaneous"})
SUPPORT_LEVELS = frozenset({"none", "hinted", "guided", "answer_given"})
POLARITIES = frozenset({SUPPORTIVE, CONTRADICTORY})
EVIDENCE_STRENGTHS = frozenset({"weak", "medium", "strong"})
# mastery volontairement absent : jamais un stade local.
LOCAL_STAGES = frozenset({"none", "discovery", "comprehension", "application"})
CONTRADICTION_SCOPES = frozenset({"recognition", "comprehension", "application", "undetermined"})
ERROR_TYPES = frozenset({"conceptual", "procedural", "execution", "factual_premise"})
CAPABILITY_LOCALIZATIONS = frozenset({"localized", "competency_only"})

DEDUP_CONSTRAINT = "uq_observation_evaluation_runs_dedup_key"
# ordinal est un SMALLINT.
MAX_ORDINAL = 32767


class ObservationServiceError(Exception):
    """Erreur métier du service d'observations."""


class EventNotFound(ObservationServiceError):
    """Aucun CognitiveEvent pour cet identifiant."""


class EventNotEvaluable(ObservationServiceError):
    """Le CognitiveEvent n'est pas finalized (open ou abandoned)."""


class EvaluationRunNotFound(ObservationServiceError):
    """Aucun ObservationEvaluationRun pour cet identifiant."""


class ObservationNotFound(ObservationServiceError):
    """Aucune PedagogicalObservation pour cet identifiant."""


class InvalidObservationPayload(ObservationServiceError):
    """Entrée structurelle invalide : identifiant, chaîne vide, valeur hors
    vocabulaire, combinaison polarité / stade / portée incohérente, payload
    non JSON-compatible."""


class InvalidEvaluationState(ObservationServiceError):
    """Le run n'est pas dans l'état requis par l'opération (lu sous verrou)."""


class InvalidReevaluationTarget(ObservationServiceError):
    """re_evaluates_run_id désigne un run d'un autre CognitiveEvent."""


class DuplicateEvaluationRun(ObservationServiceError):
    """evaluation_dedup_key déjà utilisée : jamais de get_or_create, le
    doublon révèle un problème d'orchestration."""


class ObservationAlreadyInvalidated(ObservationServiceError):
    """L'observation est déjà invalidated : jamais de no-op."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_text(value, name: str) -> str:
    """str non vide après strip, sans NUL (refusé par PostgreSQL). La valeur
    est conservée telle quelle (aucune normalisation)."""
    if type(value) is not str or not value.strip():
        raise InvalidObservationPayload(f"{name} doit être une chaîne non vide")
    if "\x00" in value:
        raise InvalidObservationPayload(f"{name} : caractère NUL refusé")
    return value


def _optional_text(value, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_uuid(value, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidObservationPayload(f"{name} doit être un uuid.UUID")
    return value


def _optional_uuid(value, name: str) -> uuid.UUID | None:
    return None if value is None else _require_uuid(value, name)


def _require_choice(value, name: str, allowed: frozenset) -> str:
    """Type str exact : une Enum str dont la valeur serait dans le
    vocabulaire est refusée, jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise InvalidObservationPayload(f"{name} : valeur {value!r} hors vocabulaire {sorted(allowed)}")
    return value


def _optional_choice(value, name: str, allowed: frozenset) -> str | None:
    return None if value is None else _require_choice(value, name, allowed)


def _json_copy(value, path: str, active: frozenset):
    """Copie profonde d'une valeur JSON-compatible, ou
    InvalidObservationPayload. Types exacts pour les scalaires : un objet
    qui ne serait JSON que par conversion (clé non str, Enum, Decimal,
    datetime, tuple, set, NaN...) est refusé plutôt que transformé.
    PostgreSQL refuse \\u0000 dans un JSONB : refusé ici plutôt qu'au
    flush."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidObservationPayload(f"{path} : nombre non fini refusé")
        return value
    if type(value) is str:
        if "\x00" in value:
            raise InvalidObservationPayload(f"{path} : caractère NUL refusé")
        return value
    if isinstance(value, (dict, list)):
        if id(value) in active:
            raise InvalidObservationPayload(f"{path} : référence circulaire")
        active = active | {id(value)}
        if isinstance(value, list):
            return [_json_copy(item, f"{path}[{i}]", active) for i, item in enumerate(value)]
        copy = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise InvalidObservationPayload(f"{path} : clé {key!r} refusée (str sans NUL attendue)")
            copy[key] = _json_copy(item, f"{path}.{key}", active)
        return copy
    raise InvalidObservationPayload(f"{path} : type {type(value).__name__} non JSON-compatible")


def _json_root(value, name: str, root: type):
    """Racine imposée (dict ou list) puis copie profonde validée. {} et []
    sont valides."""
    if not isinstance(value, root):
        raise InvalidObservationPayload(f"{name} doit être un {root.__name__}")
    return _json_copy(value, name, frozenset())


def _check_polarity(polarity: str, local_stage, contradiction_scope) -> None:
    """supportive => local_stage renseigné (none compris) et pas de
    contradiction_scope ; contradictory => pas de local_stage et
    contradiction_scope renseigné. La force de preuve est indépendante."""
    if polarity == SUPPORTIVE:
        if local_stage is None:
            raise InvalidObservationPayload("supportive : local_stage obligatoire")
        if contradiction_scope is not None:
            raise InvalidObservationPayload("supportive : contradiction_scope doit être None")
    else:
        if local_stage is not None:
            raise InvalidObservationPayload("contradictory : local_stage doit être None")
        if contradiction_scope is None:
            raise InvalidObservationPayload("contradictory : contradiction_scope obligatoire")


def _is_dedup_violation(exc: IntegrityError) -> bool:
    """Vrai seulement pour une violation de la contrainte UNIQUE
    d'evaluation_dedup_key (nom de contrainte rapporté par PostgreSQL)."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == DEDUP_CONSTRAINT


# --------------------------------------------------------------------------
# Verrouillage
# --------------------------------------------------------------------------

def _lock_event(db, event_id: uuid.UUID) -> CognitiveEvent:
    """SELECT ... FOR UPDATE sur le CognitiveEvent (verrou parent qui
    sérialise la création et l'activation des runs d'un même événement ;
    même mode que T2-B)."""
    event = db.execute(
        select(CognitiveEvent)
        .where(CognitiveEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if event is None:
        raise EventNotFound(str(event_id))
    return event


def _lock_run(db, run_id: uuid.UUID) -> ObservationEvaluationRun:
    """SELECT ... FOR NO KEY UPDATE sur le run (voir la docstring du module
    pour le choix de ce mode). L'état n'est lu qu'une fois le verrou obtenu."""
    run = db.execute(
        select(ObservationEvaluationRun)
        .where(ObservationEvaluationRun.id == run_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None:
        raise EvaluationRunNotFound(str(run_id))
    return run


def _lock_open_run(db, run_id: uuid.UUID) -> ObservationEvaluationRun:
    """Verrouille le run puis exige running / candidate."""
    run = _lock_run(db, run_id)
    if run.execution_status != RUNNING or run.interpretation_status != CANDIDATE:
        raise InvalidEvaluationState(
            f"{run_id} est {run.execution_status} / {run.interpretation_status}"
            f" (running / candidate requis)")
    return run


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def start_evaluation_run(
    db,
    *,
    event_id: uuid.UUID,
    trigger: str,
    evaluation_dedup_key: str,
    normalization_version: str,
    local_stage_version: str,
    capability_mapping_version: str,
    evaluation_schema_version: str,
    evaluator_version: str,
    input_fingerprint: str,
    pedagogical_taxonomy_release_id: uuid.UUID | None = None,
    model_id: str | None = None,
    prompt_spec_version: str | None = None,
    re_evaluates_run_id: uuid.UUID | None = None,
) -> ObservationEvaluationRun:
    """Crée un run running / candidate pour un CognitiveEvent finalized.

    Le CognitiveEvent est verrouillé (FOR UPDATE) avant de lire son statut :
    open et abandoned sont refusés (EventNotEvaluable).

    Réévaluation : re_evaluates_run_id doit désigner un run existant du MÊME
    événement (pas forcément l'active : l'historique reste réévaluable). Il
    est lu sans être modifié ; la supersession éventuelle n'a lieu qu'à la
    réussite du nouveau run (complete_evaluation_run).

    Déduplication : evaluation_dedup_key déjà présente =>
    DuplicateEvaluationRun, jamais le retour silencieux de l'ancien run.
    1. pré-vérification sous le verrou de l'événement : deux starts du même
       événement sont sérialisés, le second voit la clé du premier une fois
       celui-ci commité (READ COMMITTED) ;
    2. défense finale : l'UNIQUE uq_observation_evaluation_runs_dedup_key.
       Race résiduelle (même clé pour deux événements différents, ou
       isolation REPEATABLE READ / SERIALIZABLE) : l'INSERT est isolé dans
       un SAVEPOINT ; seule une violation de CETTE contrainte est traduite
       en DuplicateEvaluationRun, après retour au savepoint, de sorte que
       la transaction de l'appelant reste utilisable. Toute autre
       IntegrityError est propagée telle quelle.
    """
    _require_uuid(event_id, "event_id")
    _require_text(trigger, "trigger")
    _require_text(evaluation_dedup_key, "evaluation_dedup_key")
    _require_text(normalization_version, "normalization_version")
    _require_text(local_stage_version, "local_stage_version")
    _require_text(capability_mapping_version, "capability_mapping_version")
    _require_text(evaluation_schema_version, "evaluation_schema_version")
    _require_text(evaluator_version, "evaluator_version")
    _require_text(input_fingerprint, "input_fingerprint")
    _optional_uuid(pedagogical_taxonomy_release_id, "pedagogical_taxonomy_release_id")
    _optional_text(model_id, "model_id")
    _optional_text(prompt_spec_version, "prompt_spec_version")
    _optional_uuid(re_evaluates_run_id, "re_evaluates_run_id")

    event = _lock_event(db, event_id)
    if event.status != FINALIZED:
        raise EventNotEvaluable(f"{event_id} est {event.status} ({FINALIZED} requis)")

    if re_evaluates_run_id is not None:
        target = db.get(ObservationEvaluationRun, re_evaluates_run_id)
        if target is None:
            raise EvaluationRunNotFound(str(re_evaluates_run_id))
        if target.event_id != event.id:
            raise InvalidReevaluationTarget(
                f"{re_evaluates_run_id} appartient à l'événement {target.event_id}, pas à {event.id}")

    duplicate = db.execute(
        select(ObservationEvaluationRun.id)
        .where(ObservationEvaluationRun.evaluation_dedup_key == evaluation_dedup_key)
    ).first()
    if duplicate is not None:
        raise DuplicateEvaluationRun(evaluation_dedup_key)

    now = _utcnow()
    run = ObservationEvaluationRun(
        event_id=event.id,
        execution_status=RUNNING,
        interpretation_status=CANDIDATE,
        trigger=trigger,
        re_evaluates_run_id=re_evaluates_run_id,
        evaluation_dedup_key=evaluation_dedup_key,
        normalization_version=normalization_version,
        local_stage_version=local_stage_version,
        pedagogical_taxonomy_release_id=pedagogical_taxonomy_release_id,
        capability_mapping_version=capability_mapping_version,
        evaluation_schema_version=evaluation_schema_version,
        evaluator_version=evaluator_version,
        model_id=model_id,
        prompt_spec_version=prompt_spec_version,
        input_fingerprint=input_fingerprint,
        output_fingerprint=None,
        started_at=now,
        completed_at=None,
        created_at=now,
        failure_code=None,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError as exc:
        if _is_dedup_violation(exc):
            raise DuplicateEvaluationRun(evaluation_dedup_key) from exc
        raise
    return run


def add_observation(
    db,
    *,
    run_id: uuid.UUID,
    competency_code: str,
    observation_role: str,
    task_kind: str | None,
    primary_user_action: dict,
    contributive_user_actions: list,
    elicitation_mode: str,
    support_level: str,
    source_contribution_refs: list,
    residual_cognitive_work: dict,
    polarity: str,
    evidence_strength: str,
    local_stage: str | None,
    contradiction_scope: str | None,
    error_type: str | None,
    observation_text: str,
    capability_localization: str,
) -> PedagogicalObservation:
    """Ajoute une observation (integrity_status valid) à un run running /
    candidate. Tout est validé avant le verrou ; les payloads sont copiés.

    ordinal est alloué ici (1, puis MAX + 1) sous le verrou du run parent,
    qui sérialise les ajouts concurrents ;
    UNIQUE(evaluation_run_id, ordinal) reste la seconde défense."""
    _require_uuid(run_id, "run_id")
    _require_choice(competency_code, "competency_code", COMPETENCY_CODES)
    _require_choice(observation_role, "observation_role", OBSERVATION_ROLES)
    _optional_text(task_kind, "task_kind")
    primary = _json_root(primary_user_action, "primary_user_action", dict)
    contributive = _json_root(contributive_user_actions, "contributive_user_actions", list)
    _require_choice(elicitation_mode, "elicitation_mode", ELICITATION_MODES)
    _require_choice(support_level, "support_level", SUPPORT_LEVELS)
    refs = _json_root(source_contribution_refs, "source_contribution_refs", list)
    residual = _json_root(residual_cognitive_work, "residual_cognitive_work", dict)
    _require_choice(polarity, "polarity", POLARITIES)
    _require_choice(evidence_strength, "evidence_strength", EVIDENCE_STRENGTHS)
    _optional_choice(local_stage, "local_stage", LOCAL_STAGES)
    _optional_choice(contradiction_scope, "contradiction_scope", CONTRADICTION_SCOPES)
    _check_polarity(polarity, local_stage, contradiction_scope)
    _optional_choice(error_type, "error_type", ERROR_TYPES)
    _require_text(observation_text, "observation_text")
    _require_choice(capability_localization, "capability_localization", CAPABILITY_LOCALIZATIONS)

    run = _lock_open_run(db, run_id)
    last = db.execute(
        select(func.max(PedagogicalObservation.ordinal))
        .where(PedagogicalObservation.evaluation_run_id == run.id)
    ).scalar_one()
    ordinal = (last or 0) + 1
    if ordinal > MAX_ORDINAL:
        raise InvalidEvaluationState(f"{run_id} : nombre maximal d'observations atteint")

    observation = PedagogicalObservation(
        evaluation_run_id=run.id,
        ordinal=ordinal,
        competency_code=competency_code,
        observation_role=observation_role,
        task_kind=task_kind,
        primary_user_action=primary,
        contributive_user_actions=contributive,
        elicitation_mode=elicitation_mode,
        support_level=support_level,
        source_contribution_refs=refs,
        residual_cognitive_work=residual,
        polarity=polarity,
        evidence_strength=evidence_strength,
        local_stage=local_stage,
        contradiction_scope=contradiction_scope,
        error_type=error_type,
        observation_text=observation_text,
        capability_localization=capability_localization,
        integrity_status=VALID,
        invalidated_at=None,
        invalidation_reason=None,
        created_at=_utcnow(),
    )
    db.add(observation)
    db.flush()
    return observation


def complete_evaluation_run(
    db,
    *,
    run_id: uuid.UUID,
    output_fingerprint: str,
) -> ObservationEvaluationRun:
    """running / candidate -> completed / active, atomiquement avec la
    supersession de l'ancien active du même événement (s'il existe). Zéro
    observation est un succès.

    Ordre des verrous :
    1. le run candidat (FOR NO KEY UPDATE), état vérifié ;
    2. le CognitiveEvent parent (FOR UPDATE) : sérialise toutes les
       activations de l'événement. Le candidat n'a pas à être revalidé :
       son verrou est détenu depuis l'étape 1 et seule une transaction qui
       le détient peut le faire changer d'état ;
    3. le run active courant, relu SOUS le verrou parent (donc à jour
       d'une activation concurrente commitée) et verrouillé.
    Deux candidats du même événement complétés concurremment : le second
    attend le verrou parent, puis supersede légitimement le premier.

    L'ancien active passe superseded et est flushé AVANT l'activation du
    nouveau : l'index unique partiel (non différable) est vérifié ligne par
    ligne et le flush SQLAlchemy n'ordonne pas les UPDATE d'une même table
    selon l'ordre des mutations. Ni l'ancien run ni ses observations ne sont
    supprimés ou modifiés au-delà de interpretation_status."""
    _require_uuid(run_id, "run_id")
    _require_text(output_fingerprint, "output_fingerprint")

    run = _lock_open_run(db, run_id)
    _lock_event(db, run.event_id)
    previous = db.execute(
        select(ObservationEvaluationRun)
        .where(
            ObservationEvaluationRun.event_id == run.event_id,
            ObservationEvaluationRun.interpretation_status == ACTIVE,
        )
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if previous is not None:
        previous.interpretation_status = SUPERSEDED
        db.flush()

    run.execution_status = COMPLETED
    run.interpretation_status = ACTIVE
    run.completed_at = _utcnow()
    run.output_fingerprint = output_fingerprint
    run.failure_code = None
    db.flush()
    return run


def fail_evaluation_run(
    db,
    *,
    run_id: uuid.UUID,
    failure_code: str,
) -> ObservationEvaluationRun:
    """running / candidate -> failed / obsolete. N'affecte jamais l'active
    courant (aucun verrou parent requis) ; les observations déjà ajoutées
    sont conservées pour l'audit (jamais de DELETE) et ne deviendront jamais
    l'interprétation active."""
    _require_uuid(run_id, "run_id")
    _require_text(failure_code, "failure_code")

    run = _lock_open_run(db, run_id)
    run.execution_status = FAILED
    run.interpretation_status = OBSOLETE
    run.completed_at = _utcnow()
    run.failure_code = failure_code
    run.output_fingerprint = None
    db.flush()
    return run


def invalidate_observation(
    db,
    *,
    observation_id: uuid.UUID,
    reason: str,
) -> PedagogicalObservation:
    """valid -> invalidated, définitif. L'observation est verrouillée (FOR
    UPDATE) avant de lire integrity_status ; une seconde invalidation lève
    ObservationAlreadyInvalidated. Indépendant du lifecycle du run (une
    observation d'un run superseded ou obsolete peut être invalidée)."""
    _require_uuid(observation_id, "observation_id")
    _require_text(reason, "reason")

    observation = db.execute(
        select(PedagogicalObservation)
        .where(PedagogicalObservation.id == observation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if observation is None:
        raise ObservationNotFound(str(observation_id))
    if observation.integrity_status != VALID:
        raise ObservationAlreadyInvalidated(f"{observation_id} est {observation.integrity_status}")

    observation.integrity_status = INVALIDATED
    observation.invalidated_at = _utcnow()
    observation.invalidation_reason = reason
    db.flush()
    return observation


def get_evaluation_run(db, *, run_id: uuid.UUID) -> ObservationEvaluationRun:
    """Lecture simple, sans verrou."""
    _require_uuid(run_id, "run_id")
    run = db.get(ObservationEvaluationRun, run_id)
    if run is None:
        raise EvaluationRunNotFound(str(run_id))
    return run


def get_observations(db, *, run_id: uuid.UUID) -> list[PedagogicalObservation]:
    """Toutes les observations du run, invalidated comprises (historique),
    toujours ORDER BY ordinal ASC."""
    run = get_evaluation_run(db, run_id=run_id)
    return list(db.execute(
        select(PedagogicalObservation)
        .where(PedagogicalObservation.evaluation_run_id == run.id)
        .order_by(PedagogicalObservation.ordinal.asc())
    ).scalars())
