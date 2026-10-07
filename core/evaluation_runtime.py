"""R1-D1C — Evaluation Runtime Orchestration : premier branchement de T3 réel
sur les CognitiveEvents Décrypter finalized.

Chaîne de vérité (une étape n'en contourne jamais une autre) :

    CognitiveEvent finalized (>= cutoff, versions supportées)
      -> D1A  build_evaluation_input   : provenance exacte, source_fingerprint
      -> taxonomie canonique oryx-v1   : load + verify + release active
      -> EvaluationRunInputManifest     : input_fingerprint, evaluation_dedup_key
      -> TX  start run initial + claim lease ; COMMIT
      -> SANS transaction : D1B (0..N observations strictes)
      -> si N > 0 : renew lease (TX courte) ; D1D batch (localisation)
      -> résultat canonique final       : output_fingerprint
      -> TX résultat : verify lease, add observations, mappings, complete
         (efface la lease) ; COMMIT — ou ROLLBACK TOTAL puis, seulement si
         la lease est encore détenue, TX séparée fail.

Aucune transaction ni Session n'est ouverte pendant un appel LLM. Toutes les
écritures T3 passent par core/observation_service.py (runs, observations,
lease) et les mappings par core/taxonomy_service.map_observation_capability
(T4-B, défense finale à la complétion). Après complete : STOP. Aucun T5 /
T6, aucune inférence, aucun état utilisateur global, aucune progression,
aucune route. Ce module n'est appelé que par le worker interne
(core/evaluation_worker.py), jamais par api.py : le tour utilisateur
Décrypter n'est jamais impacté.

Identités :

- input_fingerprint = SHA-256(JSON canonique du manifest) : version du
  contrat d'entrée, source_fingerprint (D1A), version_key + spec_fingerprint
  de la taxonomie (le spec_fingerprint T4-C couvre C1..C12, R1..R8, les 45
  capacités, révisions et mapping_guidance : aucun autre fingerprint de
  contexte), versions de normalisation / stade local / mapping / schéma /
  évaluateur / prompt, model_id. Connu AVANT D1B.
- evaluation_dedup_key = SHA-256({"trigger": "initial", "event_id",
  "input_fingerprint"}) : deux workers identiques obtiennent la même clé ;
  en outre, sous le verrou de l'event pris par start_evaluation_run,
  N'IMPORTE QUEL run initial existant (running, completed, failed) bloque un
  second run initial (jamais de retry automatique d'un failed).
- output_fingerprint = SHA-256 du résultat canonique final (contenu
  sémantique seulement : jamais d'UUID, d'horodatage ni d'identifiant
  fournisseur) ; zéro observation => SHA-256 de {"observations": []}.

Recovery : un run running / candidate dont la lease est absente ou expirée
est REPRIS (même run, jamais un nouveau) si et seulement si ses versions,
son model_id et sa release sont exactement ceux que ce worker sait exécuter
(sinon UnsupportedEvaluationRunVersion, aucune mutation). La release est
celle stockée dans le run (même retired, revérifiée conforme à oryx-v1),
jamais la release active courante. L'input_fingerprint reconstruit doit
être identique (sinon échec lease-aware input_fingerprint_mismatch).

Erreurs (failure_code, vocabulaire fermé FAILURE_CODES) : D1B
observations=[] est un SUCCÈS ; timeout / erreur fournisseur, JSON ou
contrat D1B / D1D invalide, incohérence de persistance => failed / obsolete
SI la lease est encore détenue. Lease perdue => le worker abandonne son
résultat sans AUCUNE mutation (lost_lease n'est jamais écrit). Crash brutal
=> aucun fail : le run reste running / candidate, la lease expire, recovery.

Journaux : identifiants techniques et compteurs uniquement, jamais de texte
utilisateur, de stimulus, d'aide, de prompt ni de réponse LLM.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from sqlalchemy import and_, exists, func, or_, select

from core import observation_service as svc
from core import taxonomy_service as tax
from core.capability_mapper import (
    LOCALIZED,
    build_capability_reference_context,
    build_mapping_request,
    validate_capability_mapping_result,
)
from core.evaluation_input import (
    SUPPORTED_ADMISSION_VERSION,
    SUPPORTED_EVENT_BUILDER_VERSION,
    SUPPORTED_EVENT_ORIGIN,
    SUPPORTED_EVENT_SCHEMA_VERSION,
    EvaluationInputBundle,
    EvaluationInputError,
    build_evaluation_input,
    sha256_hex,
)
from core.local_evaluator import (
    EvaluationOutputInvalid,
    EvaluatorRequest,
    build_competency_reference_context,
    build_evaluator_request,
    parse_json_object,
    validate_local_evaluation_result,
)
from core.models import CognitiveEvent, ObservationEvaluationRun
from core.pedagogy.taxonomy_bootstrap import TaxonomyBootstrapError, verify_taxonomy_v1
from core.pedagogy.taxonomy_v1 import EXPECTED_V1_FINGERPRINT, VERSION_KEY, TaxonomySpecError, load_taxonomy_v1

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Versions du pipeline (immuables pour V1 ; toute modification de prompt,
# de vocabulaire, de validation ou de paramètre fournisseur exige une
# nouvelle valeur, donc un nouvel input_fingerprint).
# --------------------------------------------------------------------------

EVALUATION_INPUT_SCHEMA_VERSION = "decryptage-evaluation-input-v1"
NORMALIZATION_VERSION = "decryptage-normalization-v1"
LOCAL_STAGE_VERSION = "decryptage-local-stage-v1"
CAPABILITY_MAPPING_VERSION = "decryptage-capability-mapping-v1"
EVALUATION_SCHEMA_VERSION = "decryptage-evaluation-schema-v1"
EVALUATOR_VERSION = "decryptage-local-evaluation-pipeline-v1"
PROMPT_SPEC_VERSION = "decryptage-t3-prompt-bundle-v1"

INITIAL_TRIGGER = "initial"

RUN_VERSIONS = MappingProxyType({
    "normalization_version": NORMALIZATION_VERSION,
    "local_stage_version": LOCAL_STAGE_VERSION,
    "capability_mapping_version": CAPABILITY_MAPPING_VERSION,
    "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
    "evaluator_version": EVALUATOR_VERSION,
    "prompt_spec_version": PROMPT_SPEC_VERSION,
})

# failure_code persistés (vocabulaire fermé). lost_lease n'est jamais écrit :
# un worker qui a perdu sa lease ne mute plus rien.
INVALID_EVALUATION_INPUT = "invalid_evaluation_input"
PROVIDER_ERROR = "provider_error"
PROVIDER_TIMEOUT = "provider_timeout"
INVALID_D1B_OUTPUT = "invalid_d1b_output"
INVALID_D1D_OUTPUT = "invalid_d1d_output"
INPUT_FINGERPRINT_MISMATCH = "input_fingerprint_mismatch"
INTERNAL_VALIDATION_ERROR = "internal_validation_error"
FAILURE_CODES = (INVALID_EVALUATION_INPUT, PROVIDER_ERROR, PROVIDER_TIMEOUT, INVALID_D1B_OUTPUT,
                 INVALID_D1D_OUTPUT, INPUT_FINGERPRINT_MISMATCH, INTERNAL_VALIDATION_ERROR)

# Issues de traitement (journaux / tests), jamais persistées.
COMPLETED = "completed"
FAILED = "failed"
LEASE_LOST = "lease_lost"
ALREADY_HANDLED = "already_handled"


class EvaluationRuntimeError(Exception):
    """Erreur d'orchestration R1-D1."""


class TaxonomyUnavailable(EvaluationRuntimeError):
    """Aucune release oryx-v1 persistée, ou aucune release active."""


class TaxonomyMismatch(EvaluationRuntimeError):
    """SPEC, release persistée ou release active non conformes à oryx-v1
    (fingerprint, contenu, identité) : aucun run n'est démarré."""


class UnsupportedEvaluationRunVersion(EvaluationRuntimeError):
    """Run existant d'une combinaison de versions / modèle / release que ce
    worker ne sait pas exécuter : aucune mutation."""


class InitialRunAlreadyExists(EvaluationRuntimeError):
    """Un run initial existe déjà pour cet event (quel que soit son
    statut)."""


class ProviderError(EvaluationRuntimeError):
    """Échec technique du fournisseur LLM (HTTP, refus, réponse tronquée).
    Message sans contenu de prompt ni de réponse."""


class ProviderTimeout(ProviderError):
    """Délai dépassé côté fournisseur."""


# --------------------------------------------------------------------------
# Taxonomie canonique
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TaxonomyContext:
    """Release oryx-v1 vérifiée : identité, SPEC canonique (validée par
    load_taxonomy_v1), memberships (capability_code, semantic_revision) ->
    membership_id de CETTE release, référentiel C1..C12 pour D1B."""
    release_id: uuid.UUID
    version_key: str
    spec_fingerprint: str
    spec: dict
    memberships: MappingProxyType
    competency_reference: tuple


def _verified_v1(db):
    try:
        spec, fingerprint = load_taxonomy_v1()
    except TaxonomySpecError as exc:
        raise TaxonomyMismatch(f"SPEC {VERSION_KEY} : {type(exc).__name__}") from None
    if fingerprint != EXPECTED_V1_FINGERPRINT:
        raise TaxonomyMismatch(f"SPEC {VERSION_KEY} : fingerprint inattendu")
    try:
        verified = verify_taxonomy_v1(db)
    except tax.TaxonomyReleaseNotFound:
        raise TaxonomyUnavailable(f"{VERSION_KEY} absente") from None
    except TaxonomyBootstrapError as exc:
        raise TaxonomyMismatch(f"{VERSION_KEY} persistée non conforme : {type(exc).__name__}") from None
    return spec, fingerprint, verified


def _context(db, spec: dict, fingerprint: str, release_id: uuid.UUID) -> TaxonomyContext:
    memberships = {(definition.capability_code, definition.semantic_revision): membership.id
                   for membership, definition in tax.get_release_capabilities(db, release_id=release_id)}
    return TaxonomyContext(release_id=release_id, version_key=VERSION_KEY, spec_fingerprint=fingerprint, spec=spec,
                           memberships=MappingProxyType(memberships),
                           competency_reference=tuple(build_competency_reference_context(spec)))


def load_active_taxonomy(db) -> TaxonomyContext:
    """Avant tout NOUVEAU run initial : SPEC canonique (fingerprint figé),
    oryx-v1 persistée strictement conforme, et release ACTIVE = cette même
    release (version_key, spec_fingerprint, id). Sinon fail closed."""
    spec, fingerprint, verified = _verified_v1(db)
    try:
        active = tax.get_active_release(db)
    except tax.InvalidTaxonomyState:
        raise TaxonomyMismatch("plusieurs releases actives") from None
    if active is None:
        raise TaxonomyUnavailable("aucune release active")
    if (active.version_key, active.spec_fingerprint, active.id) != (VERSION_KEY, EXPECTED_V1_FINGERPRINT,
                                                                    verified.release_id):
        raise TaxonomyMismatch(f"release active {active.version_key!r} différente de {VERSION_KEY}")
    return _context(db, spec, fingerprint, active.id)


def load_run_taxonomy(db, release_id: uuid.UUID) -> TaxonomyContext:
    """Recovery : la release STOCKÉE dans le run (même retired), jamais la
    release active courante ; elle doit être oryx-v1 strictement conforme.
    Une autre release => UnsupportedEvaluationRunVersion."""
    spec, fingerprint, verified = _verified_v1(db)
    if verified.release_id != release_id:
        raise UnsupportedEvaluationRunVersion(f"release {release_id} différente de {VERSION_KEY}")
    return _context(db, spec, fingerprint, release_id)


# --------------------------------------------------------------------------
# Manifest, fingerprints, résultat canonique
# --------------------------------------------------------------------------

def build_input_manifest(*, source_fingerprint: str, taxonomy_version_key: str, taxonomy_spec_fingerprint: str,
                         model_id: str) -> dict:
    """EvaluationRunInputManifest (tout ce qui détermine le résultat)."""
    return {
        "evaluation_input_schema_version": EVALUATION_INPUT_SCHEMA_VERSION,
        "source_fingerprint": source_fingerprint,
        "taxonomy_version_key": taxonomy_version_key,
        "taxonomy_spec_fingerprint": taxonomy_spec_fingerprint,
        "normalization_version": NORMALIZATION_VERSION,
        "local_stage_version": LOCAL_STAGE_VERSION,
        "capability_mapping_version": CAPABILITY_MAPPING_VERSION,
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "model_id": model_id,
        "prompt_spec_version": PROMPT_SPEC_VERSION,
    }


def compute_input_fingerprint(manifest: dict) -> str:
    return sha256_hex(manifest)


def compute_evaluation_dedup_key(*, event_id: uuid.UUID, input_fingerprint: str,
                                 trigger: str = INITIAL_TRIGGER) -> str:
    return sha256_hex({"trigger": trigger, "event_id": str(event_id), "input_fingerprint": input_fingerprint})


def build_final_result(candidates: list, mappings: list) -> dict:
    """Résultat canonique final (D1B + localisation D1D), dans l'ordre des
    observations. Contenu sémantique uniquement : tokens locaux, jamais
    d'UUID, d'horodatage ni d'identifiant fournisseur."""
    by_token = {m["observation_token"]: m for m in mappings}
    observations = []
    for candidate in candidates:
        mapping = by_token[candidate["observation_token"]]
        observations.append({
            "competency_code": candidate["competency_code"],
            "observation_role": candidate["observation_role"],
            "task_kind": candidate["task_kind"],
            "primary_user_action": candidate["primary_user_action"],
            "contributive_user_actions": candidate["contributive_user_actions"],
            "elicitation_mode": candidate["elicitation_mode"],
            "support_level": candidate["support_level"],
            "residual_cognitive_work": candidate["residual_cognitive_work"],
            "polarity": candidate["polarity"],
            "evidence_strength": candidate["evidence_strength"],
            "local_stage": candidate["local_stage"],
            "contradiction_scope": candidate["contradiction_scope"],
            "error_type": candidate["error_type"],
            "source_contribution_tokens": candidate["source_contribution_tokens"],
            "observation_text": candidate["observation_text"],
            "capability_localization": mapping["capability_localization"],
            "capabilities": [f"{c['capability_code']}@{c['semantic_revision']}" for c in mapping["capabilities"]],
        })
    return {"observations": observations}


def compute_output_fingerprint(final_result: dict) -> str:
    return sha256_hex(final_result)


# --------------------------------------------------------------------------
# Traduction tokens -> provenance serveur (persistance)
# --------------------------------------------------------------------------

def _contribution_ref(bundle: EvaluationInputBundle, token: str) -> dict:
    return {"contribution_token": token, "contribution_id": bundle.contribution_ids[token],
            "phase": bundle.contribution_phases[token]}


def _action_ref(bundle: EvaluationInputBundle, action: dict) -> dict:
    return {"action": action["action"], "contribution_token": action["contribution_token"],
            "contribution_id": bundle.contribution_ids[action["contribution_token"]]}


def _persisted_fields(bundle: EvaluationInputBundle, candidate: dict) -> dict:
    """Champs JSONB persistés : les tokens locaux ET les UUID réels (audit
    serveur), jamais de texte supplémentaire."""
    residual = candidate["residual_cognitive_work"]
    return {
        "primary_user_action": _action_ref(bundle, candidate["primary_user_action"]),
        "contributive_user_actions": [_action_ref(bundle, a) for a in candidate["contributive_user_actions"]],
        "source_contribution_refs": [_contribution_ref(bundle, t) for t in candidate["source_contribution_tokens"]],
        "residual_cognitive_work": {
            "operations_left_to_user": list(residual["operations_left_to_user"]),
            "materially_used_support_refs": [
                {"support_token": r["support_token"], "support_trace_id": bundle.support_ids[r["support_token"]],
                 "contribution_token": r["contribution_token"],
                 "contribution_id": bundle.contribution_ids[r["contribution_token"]]}
                for r in residual["materially_used_support_refs"]
            ],
            "summary": residual["summary"],
        },
    }


# --------------------------------------------------------------------------
# Découverte
# --------------------------------------------------------------------------

def discover_recovery_runs(db, *, limit: int, exclude=frozenset()) -> list:
    """Runs running / candidate sans lease valide (absente ou expirée,
    horloge PostgreSQL), les plus anciens d'abord. Lecture seule."""
    query = (
        select(ObservationEvaluationRun.id)
        .where(ObservationEvaluationRun.execution_status == svc.RUNNING,
               ObservationEvaluationRun.interpretation_status == svc.CANDIDATE,
               or_(ObservationEvaluationRun.lease_token.is_(None),
                   ObservationEvaluationRun.lease_expires_at <= func.clock_timestamp()))
        .order_by(ObservationEvaluationRun.started_at.asc(), ObservationEvaluationRun.id.asc())
        .limit(limit)
    )
    if exclude:
        query = query.where(ObservationEvaluationRun.id.not_in(list(exclude)))
    return list(db.execute(query).scalars())


def discover_new_events(db, *, cutoff: datetime, limit: int, exclude=frozenset()) -> list:
    """Events Décrypter finalized, fermés à partir du cutoff (>=), des
    versions T2 supportées, sans AUCUN run initial (running, completed,
    failed). Lecture seule ; l'éligibilité fine (snapshots, runtime V2) est
    revérifiée par D1A."""
    has_initial_run = exists().where(and_(ObservationEvaluationRun.event_id == CognitiveEvent.id,
                                          ObservationEvaluationRun.trigger == INITIAL_TRIGGER))
    query = (
        select(CognitiveEvent.id)
        .where(CognitiveEvent.event_origin == SUPPORTED_EVENT_ORIGIN,
               CognitiveEvent.status == svc.FINALIZED,
               CognitiveEvent.closed_at >= cutoff,
               CognitiveEvent.event_builder_version == SUPPORTED_EVENT_BUILDER_VERSION,
               CognitiveEvent.admission_version == SUPPORTED_ADMISSION_VERSION,
               CognitiveEvent.event_schema_version == SUPPORTED_EVENT_SCHEMA_VERSION,
               ~has_initial_run)
        .order_by(CognitiveEvent.closed_at.asc(), CognitiveEvent.id.asc())
        .limit(limit)
    )
    if exclude:
        query = query.where(CognitiveEvent.id.not_in(list(exclude)))
    return list(db.execute(query).scalars())


# --------------------------------------------------------------------------
# Orchestration (chaque étape DB : sa propre Session, commitée et fermée
# avant tout appel fournisseur)
# --------------------------------------------------------------------------

def _require_single_initial_run(db, event_id: uuid.UUID) -> None:
    """Sous le verrou de l'event (pris par start_evaluation_run) : notre run
    doit être l'UNIQUE run initial de l'event."""
    count = db.execute(
        select(func.count()).select_from(ObservationEvaluationRun)
        .where(ObservationEvaluationRun.event_id == event_id,
               ObservationEvaluationRun.trigger == INITIAL_TRIGGER)
    ).scalar_one()
    if count != 1:
        raise InitialRunAlreadyExists(f"event {event_id} : {count} runs initiaux")


def _fail_run(sessions, run_id: uuid.UUID, lease_token: uuid.UUID, failure_code: str) -> str:
    """Échec lease-aware, dans sa propre transaction ; lease perdue => aucune
    mutation."""
    with sessions() as db:
        try:
            svc.fail_evaluation_run(db, run_id=run_id, failure_code=failure_code, lease_token=lease_token)
            db.commit()
        except svc.LostEvaluationLease:
            db.rollback()
            logger.info("[R1-D1] lease_lost run=%s", run_id)
            return LEASE_LOST
    logger.info("[R1-D1] run_failed run=%s failure_code=%s", run_id, failure_code)
    return FAILED


def _renew(sessions, run_id: uuid.UUID, lease_token: uuid.UUID, lease_seconds: int) -> bool:
    with sessions() as db:
        try:
            svc.renew_evaluation_lease(db, run_id=run_id, lease_token=lease_token, lease_seconds=lease_seconds)
            db.commit()
        except svc.LostEvaluationLease:
            db.rollback()
            logger.info("[R1-D1] lease_lost run=%s", run_id)
            return False
    logger.info("[R1-D1] lease_renewed run=%s", run_id)
    return True


def _call(provider, request: EvaluatorRequest):
    """Appel fournisseur ; retourne (texte, None) ou (None, failure_code).
    Toute autre Exception du fournisseur (réponse inattendue du SDK...) est
    un échec fournisseur : jamais un run laissé en boucle de recovery. Un
    arrêt du processus (BaseException hors Exception) n'est pas rattrapé."""
    try:
        return provider.complete(request), None
    except ProviderTimeout:
        return None, PROVIDER_TIMEOUT
    except ProviderError:
        return None, PROVIDER_ERROR
    except Exception as exc:  # noqa: BLE001 — classé, jamais propagé
        logger.warning("[R1-D1] provider_unexpected_error error=%s", type(exc).__name__)
        return None, PROVIDER_ERROR


def _validated(validate, invalid_code: str):
    """(résultat, None) ou (None, failure_code) : sortie hors contrat =>
    invalid_code ; erreur interne inattendue => internal_validation_error."""
    try:
        return validate(), None
    except EvaluationOutputInvalid:
        return None, invalid_code
    except Exception as exc:  # noqa: BLE001 — classé, jamais propagé
        logger.warning("[R1-D1] validation_unexpected_error error=%s", type(exc).__name__)
        return None, INTERNAL_VALIDATION_ERROR


def _persist(sessions, *, run_id, lease_token, bundle, taxonomy, input_fingerprint, candidates, mappings,
             output_fingerprint) -> str:
    """TX résultat unique : verify lease (verrou du run conservé jusqu'au
    commit), revalidation de l'entrée et des versions, observations,
    mappings, complete (efface la lease), COMMIT. Tout échec => ROLLBACK
    TOTAL, puis fail lease-aware dans une transaction séparée."""
    by_token = {m["observation_token"]: m for m in mappings}
    with sessions() as db:
        try:
            run = svc.verify_evaluation_lease(db, run_id=run_id, lease_token=lease_token)
            versions = {name: getattr(run, name) for name in RUN_VERSIONS}
            if (run.input_fingerprint != input_fingerprint or versions != dict(RUN_VERSIONS)
                    or run.pedagogical_taxonomy_release_id != taxonomy.release_id or run.event_id != bundle.event_id):
                raise EvaluationRuntimeError(f"run {run_id} : entrée ou versions divergentes")
            for candidate in candidates:
                mapping = by_token[candidate["observation_token"]]
                observation = svc.add_observation(
                    db, run_id=run_id, lease_token=lease_token,
                    competency_code=candidate["competency_code"],
                    observation_role=candidate["observation_role"],
                    task_kind=candidate["task_kind"],
                    elicitation_mode=candidate["elicitation_mode"],
                    support_level=candidate["support_level"],
                    polarity=candidate["polarity"],
                    evidence_strength=candidate["evidence_strength"],
                    local_stage=candidate["local_stage"],
                    contradiction_scope=candidate["contradiction_scope"],
                    error_type=candidate["error_type"],
                    observation_text=candidate["observation_text"],
                    capability_localization=mapping["capability_localization"],
                    **_persisted_fields(bundle, candidate),
                )
                for capability in mapping["capabilities"]:
                    membership_id = taxonomy.memberships.get((capability["capability_code"],
                                                              capability["semantic_revision"]))
                    if membership_id is None:
                        raise EvaluationRuntimeError(f"run {run_id} : révision sémantique hors de la release")
                    tax.map_observation_capability(db, observation_id=observation.id,
                                                   capability_membership_id=membership_id)
            svc.complete_evaluation_run(db, run_id=run_id, output_fingerprint=output_fingerprint,
                                        lease_token=lease_token)
            db.commit()
        except svc.LostEvaluationLease:
            db.rollback()
            logger.info("[R1-D1] lease_lost run=%s", run_id)
            return LEASE_LOST
        except Exception as exc:  # noqa: BLE001 — rollback total puis échec lease-aware
            db.rollback()
            logger.warning("[R1-D1] persist_rejected run=%s error=%s", run_id, type(exc).__name__)
            return _fail_run(sessions, run_id, lease_token, INTERNAL_VALIDATION_ERROR)
    logger.info("[R1-D1] run_completed run=%s observations=%d", run_id, len(candidates))
    return COMPLETED


def execute_run(sessions, *, run_id: uuid.UUID, lease_token: uuid.UUID, bundle: EvaluationInputBundle,
                taxonomy: TaxonomyContext, input_fingerprint: str, provider, lease_seconds: int) -> str:
    """D1B -> (renew -> D1D) -> TX résultat, pour un run dont ce worker
    détient la lease. Aucune Session ouverte pendant les appels fournisseur."""
    request = build_evaluator_request(bundle.evaluator_payload, list(taxonomy.competency_reference))
    text, failure = _call(provider, request)
    if failure is None:
        candidates, failure = _validated(
            lambda: validate_local_evaluation_result(parse_json_object(text), bundle.evaluator_payload),
            INVALID_D1B_OUTPUT)
    if failure is not None:
        return _fail_run(sessions, run_id, lease_token, failure)
    logger.info("[R1-D1] d1b_completed run=%s observations=%d", run_id, len(candidates))

    mappings = []
    if candidates:
        if not _renew(sessions, run_id, lease_token, lease_seconds):
            return LEASE_LOST
        context = build_capability_reference_context(taxonomy.spec, {c["competency_code"] for c in candidates})
        text, failure = _call(provider, build_mapping_request(candidates, context, bundle.evaluator_payload))
        if failure is None:
            mappings, failure = _validated(
                lambda: validate_capability_mapping_result(parse_json_object(text), candidates, context),
                INVALID_D1D_OUTPUT)
        if failure is not None:
            return _fail_run(sessions, run_id, lease_token, failure)
        localized = sum(1 for m in mappings if m["capability_localization"] == LOCALIZED)
        logger.info("[R1-D1] d1d_completed run=%s localized=%d competency_only=%d", run_id, localized,
                    len(mappings) - localized)

    output_fingerprint = compute_output_fingerprint(build_final_result(candidates, mappings))
    return _persist(sessions, run_id=run_id, lease_token=lease_token, bundle=bundle, taxonomy=taxonomy,
                    input_fingerprint=input_fingerprint, candidates=candidates, mappings=mappings,
                    output_fingerprint=output_fingerprint)


def process_new_event(sessions, *, event_id: uuid.UUID, provider, lease_seconds: int) -> str:
    """Nouveau run initial pour un event découvert. Lève EvaluationInputError
    (D1A), TaxonomyUnavailable / TaxonomyMismatch (aucun run démarré)."""
    with sessions() as db:
        bundle = build_evaluation_input(db, event_id=event_id)
        taxonomy = load_active_taxonomy(db)
        db.rollback()
    manifest = build_input_manifest(source_fingerprint=bundle.source_fingerprint,
                                    taxonomy_version_key=taxonomy.version_key,
                                    taxonomy_spec_fingerprint=taxonomy.spec_fingerprint, model_id=provider.model_id)
    input_fingerprint = compute_input_fingerprint(manifest)
    dedup_key = compute_evaluation_dedup_key(event_id=event_id, input_fingerprint=input_fingerprint)

    with sessions() as db:
        try:
            run = svc.start_evaluation_run(
                db, event_id=event_id, trigger=INITIAL_TRIGGER, evaluation_dedup_key=dedup_key,
                input_fingerprint=input_fingerprint, pedagogical_taxonomy_release_id=taxonomy.release_id,
                model_id=provider.model_id, re_evaluates_run_id=None, **RUN_VERSIONS)
            _require_single_initial_run(db, event_id)
            run = svc.claim_evaluation_lease(db, run_id=run.id, lease_seconds=lease_seconds)
            run_id, lease_token = run.id, run.lease_token
            db.commit()
        except (svc.DuplicateEvaluationRun, InitialRunAlreadyExists):
            db.rollback()
            logger.info("[R1-D1] initial_run_exists event=%s", event_id)
            return ALREADY_HANDLED
    logger.info("[R1-D1] run_started run=%s event=%s", run_id, event_id)
    logger.info("[R1-D1] lease_claimed run=%s", run_id)
    return execute_run(sessions, run_id=run_id, lease_token=lease_token, bundle=bundle, taxonomy=taxonomy,
                       input_fingerprint=input_fingerprint, provider=provider, lease_seconds=lease_seconds)


def check_run_supported(run: ObservationEvaluationRun, model_id: str) -> None:
    """Le worker ne reprend que les versions EXACTES qu'il sait exécuter."""
    versions = {name: getattr(run, name) for name in RUN_VERSIONS}
    if (run.trigger != INITIAL_TRIGGER or run.re_evaluates_run_id is not None or versions != dict(RUN_VERSIONS)
            or run.model_id != model_id or run.pedagogical_taxonomy_release_id is None):
        raise UnsupportedEvaluationRunVersion(f"run {run.id} : versions non exécutables par ce worker")


def process_recovery_run(sessions, *, run_id: uuid.UUID, provider, lease_seconds: int) -> str:
    """Reprise du MÊME run running / candidate sans lease valide. Lève
    UnsupportedEvaluationRunVersion ou TaxonomyUnavailable / TaxonomyMismatch
    AVANT toute mutation."""
    with sessions() as db:
        run = svc.get_evaluation_run(db, run_id=run_id)
        if run.execution_status != svc.RUNNING or run.interpretation_status != svc.CANDIDATE:
            return ALREADY_HANDLED
        check_run_supported(run, provider.model_id)
        taxonomy = load_run_taxonomy(db, run.pedagogical_taxonomy_release_id)
        event_id, stored_fingerprint, stored_dedup = run.event_id, run.input_fingerprint, run.evaluation_dedup_key
        try:
            run = svc.claim_evaluation_lease(db, run_id=run_id, lease_seconds=lease_seconds)
            lease_token = run.lease_token
            db.commit()
        except (svc.AlreadyLeased, svc.InvalidEvaluationState):
            db.rollback()
            return ALREADY_HANDLED
    logger.info("[R1-D1] recovery run=%s event=%s", run_id, event_id)
    logger.info("[R1-D1] lease_claimed run=%s", run_id)

    try:
        with sessions() as db:
            bundle = build_evaluation_input(db, event_id=event_id)
            db.rollback()
    except EvaluationInputError:
        return _fail_run(sessions, run_id, lease_token, INVALID_EVALUATION_INPUT)
    manifest = build_input_manifest(source_fingerprint=bundle.source_fingerprint,
                                    taxonomy_version_key=taxonomy.version_key,
                                    taxonomy_spec_fingerprint=taxonomy.spec_fingerprint, model_id=provider.model_id)
    input_fingerprint = compute_input_fingerprint(manifest)
    if (input_fingerprint != stored_fingerprint
            or compute_evaluation_dedup_key(event_id=event_id, input_fingerprint=input_fingerprint) != stored_dedup):
        return _fail_run(sessions, run_id, lease_token, INPUT_FINGERPRINT_MISMATCH)
    return execute_run(sessions, run_id=run_id, lease_token=lease_token, bundle=bundle, taxonomy=taxonomy,
                       input_fingerprint=input_fingerprint, provider=provider, lease_seconds=lease_seconds)
