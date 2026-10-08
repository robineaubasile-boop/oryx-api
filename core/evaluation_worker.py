"""Worker interne R1-D1 « oryx-evaluation-worker » : boucle durable,
adossée à PostgreSQL, qui évalue (T3 local) les CognitiveEvents Décrypter
finalized via core/evaluation_runtime.py.

Démarrage (service Railway dédié, même repo, même Postgres, sans domaine
public, sans FastAPI) :

    python -m core.evaluation_worker

Configuration (TOUTES obligatoires ; absente ou invalide => le worker
REFUSE DE DÉMARRER, jamais de valeur par défaut implicite) :

- DATABASE_URL : même base que l'API (schéma >= 0014_evaluation_run_leases,
  vérifié au démarrage) ;
- ANTHROPIC_API_KEY : clé du fournisseur (jamais journalisée) ;
- ORYX_EVALUATION_MODEL : model_id unique de D1B et D1D (persisté dans le
  run, inclus dans l'input_fingerprint) ;
- ORYX_T3_INITIAL_CUTOFF : instant UTC ISO-8601 EXPLICITE (ex.
  2026-10-07T20:00:00Z ou 2026-10-07T20:00:00+00:00). Seuls les events
  fermés à partir de cet instant (closed_at >= cutoff) sont évalués : aucun
  backfill silencieux des events historiques. Jamais now(), jamais epoch ;
- ORYX_EVALUATION_POLL_SECONDS : pause entre deux cycles sans travail
  (entier, 1..3600) ;
- ORYX_EVALUATION_LEASE_SECONDS : durée de lease (entier, 60..86400) ;
- ORYX_EVALUATION_LLM_TIMEOUT_SECONDS : timeout d'UN appel LLM (entier,
  10..3600), avec ORYX_EVALUATION_LEASE_SECONDS >= timeout +
  LEASE_MARGIN_SECONDS (une lease couvre toujours un appel complet).

Boucle : reprise prioritaire des runs running / candidate sans lease valide
(recovery du même run, avec le bundle de SES versions persistées : V1
legacy ou V2), puis découverte des nouveaux events éligibles (nouveaux runs
initiaux toujours V2, cf. core/evaluation_runtime.py), puis
pause si rien n'a été traité (aucune boucle active). SIGTERM / SIGINT :
arrêt propre après l'élément en cours (un arrêt brutal ne fait jamais échouer
un run : sa lease expire et il est repris). Aucune route, aucun T5 / T6.
"""
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from core.evaluation_input import EvaluationInputError
from core.evaluation_runtime import (
    TaxonomyMismatch,
    TaxonomyUnavailable,
    UnsupportedEvaluationRunVersion,
    discover_new_events,
    discover_recovery_runs,
    process_new_event,
    process_recovery_run,
)

logger = logging.getLogger("core.evaluation_runtime")

CUTOFF_ENV = "ORYX_T3_INITIAL_CUTOFF"
POLL_ENV = "ORYX_EVALUATION_POLL_SECONDS"
LEASE_ENV = "ORYX_EVALUATION_LEASE_SECONDS"
LLM_TIMEOUT_ENV = "ORYX_EVALUATION_LLM_TIMEOUT_SECONDS"
MODEL_ENV = "ORYX_EVALUATION_MODEL"
API_KEY_ENV = "ANTHROPIC_API_KEY"
DATABASE_ENV = "DATABASE_URL"

POLL_RANGE = (1, 3600)
LEASE_RANGE = (60, 86400)
LLM_TIMEOUT_RANGE = (10, 3600)
LEASE_MARGIN_SECONDS = 60
BATCH_SIZE = 10
LEASE_COLUMNS = frozenset({"lease_token", "lease_expires_at"})


class WorkerConfigError(Exception):
    """Configuration absente ou invalide : le worker ne démarre pas. Le
    message ne contient jamais la valeur d'un secret."""


@dataclass(frozen=True)
class WorkerConfig:
    cutoff: datetime
    poll_seconds: int
    lease_seconds: int
    llm_timeout_seconds: int
    model_id: str


def parse_cutoff(raw) -> datetime:
    """UTC ISO-8601 explicite : suffixe Z ou décalage +00:00 obligatoire ;
    aucun fuseau implicite, aucune valeur de repli."""
    if type(raw) is not str or not raw.strip():
        raise WorkerConfigError(f"{CUTOFF_ENV} manquant")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise WorkerConfigError(f"{CUTOFF_ENV} : ISO-8601 invalide") from None
    if "T" not in text or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(None):
        raise WorkerConfigError(f"{CUTOFF_ENV} : date-heure UTC explicite requise (Z ou +00:00)")
    return value.astimezone(timezone.utc)


def _parse_int(environ, name: str, bounds: tuple) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        raise WorkerConfigError(f"{name} manquant")
    text = raw.strip()
    if not text.isascii() or not text.isdigit():
        raise WorkerConfigError(f"{name} : entier positif attendu")
    value = int(text)
    if not bounds[0] <= value <= bounds[1]:
        raise WorkerConfigError(f"{name} : valeur hors de [{bounds[0]}, {bounds[1]}]")
    return value


def load_config(environ) -> WorkerConfig:
    cutoff = parse_cutoff(environ.get(CUTOFF_ENV))
    poll = _parse_int(environ, POLL_ENV, POLL_RANGE)
    lease = _parse_int(environ, LEASE_ENV, LEASE_RANGE)
    timeout = _parse_int(environ, LLM_TIMEOUT_ENV, LLM_TIMEOUT_RANGE)
    if lease < timeout + LEASE_MARGIN_SECONDS:
        raise WorkerConfigError(f"{LEASE_ENV} doit être >= {LLM_TIMEOUT_ENV} + {LEASE_MARGIN_SECONDS}")
    model = (environ.get(MODEL_ENV) or "").strip()
    if not model or "\x00" in model:
        raise WorkerConfigError(f"{MODEL_ENV} manquant")
    for name in (API_KEY_ENV, DATABASE_ENV):
        if not (environ.get(name) or "").strip():
            raise WorkerConfigError(f"{name} manquant")
    return WorkerConfig(cutoff=cutoff, poll_seconds=poll, lease_seconds=lease, llm_timeout_seconds=timeout,
                        model_id=model)


def check_schema(engine) -> None:
    """Fail closed si la migration 0014 n'est pas appliquée."""
    columns = {column["name"] for column in inspect(engine).get_columns("observation_evaluation_runs")}
    if not LEASE_COLUMNS <= columns:
        raise WorkerConfigError("schéma antérieur à 0014_evaluation_run_leases")


@dataclass
class WorkerState:
    """Éléments écartés pour la durée du processus (aucune écriture en base :
    une entrée invalide ou une version non exécutable n'est jamais
    « réparée » ni marquée)."""
    excluded_events: set
    excluded_runs: set


def run_once(sessions, config: WorkerConfig, provider, state: WorkerState, stop: threading.Event) -> int:
    """Un cycle : recovery d'abord, puis nouveaux events. Retourne le nombre
    d'éléments traités."""
    processed = 0
    with sessions() as db:
        run_ids = discover_recovery_runs(db, limit=BATCH_SIZE, exclude=state.excluded_runs)
    for run_id in run_ids:
        if stop.is_set():
            return processed
        try:
            outcome = process_recovery_run(sessions, run_id=run_id, provider=provider,
                                           lease_seconds=config.lease_seconds)
            logger.info("[R1-D1] recovery_outcome run=%s outcome=%s", run_id, outcome)
        except UnsupportedEvaluationRunVersion:
            state.excluded_runs.add(run_id)
            logger.warning("[R1-D1] unsupported_version run=%s", run_id)
        except (TaxonomyUnavailable, TaxonomyMismatch) as exc:
            state.excluded_runs.add(run_id)
            logger.error("[R1-D1] taxonomy_unavailable run=%s error=%s", run_id, type(exc).__name__)
        except SQLAlchemyError:
            raise  # base indisponible : cycle suivant
        except Exception as exc:  # noqa: BLE001 — jamais de blocage de la file
            state.excluded_runs.add(run_id)
            logger.error("[R1-D1] recovery_error run=%s error=%s", run_id, type(exc).__name__)
        processed += 1

    with sessions() as db:
        event_ids = discover_new_events(db, cutoff=config.cutoff, limit=BATCH_SIZE, exclude=state.excluded_events)
    for event_id in event_ids:
        if stop.is_set():
            return processed
        logger.info("[R1-D1] discovered event=%s", event_id)
        try:
            outcome = process_new_event(sessions, event_id=event_id, provider=provider,
                                        lease_seconds=config.lease_seconds)
            logger.info("[R1-D1] event_outcome event=%s outcome=%s", event_id, outcome)
        except EvaluationInputError as exc:
            state.excluded_events.add(event_id)
            logger.warning("[R1-D1] not_evaluable event=%s error=%s", event_id, type(exc).__name__)
        except (TaxonomyUnavailable, TaxonomyMismatch) as exc:
            # Aucun run démarré sous une taxonomie inconnue : on réessaie au
            # prochain cycle (la release peut être activée entre-temps).
            logger.error("[R1-D1] taxonomy_unavailable error=%s", type(exc).__name__)
            return processed
        except SQLAlchemyError:
            raise  # base indisponible : cycle suivant
        except Exception as exc:  # noqa: BLE001 — jamais de blocage de la file
            state.excluded_events.add(event_id)
            logger.error("[R1-D1] event_error event=%s error=%s", event_id, type(exc).__name__)
        processed += 1
    return processed


def run_forever(sessions, config: WorkerConfig, provider, stop: threading.Event) -> None:
    state = WorkerState(excluded_events=set(), excluded_runs=set())
    logger.info("[R1-D1] worker_started cutoff=%s model=%s poll=%ss lease=%ss", config.cutoff.isoformat(),
                config.model_id, config.poll_seconds, config.lease_seconds)
    while not stop.is_set():
        try:
            processed = run_once(sessions, config, provider, state, stop)
        except Exception as exc:  # noqa: BLE001 — la boucle survit ; aucun détail de donnée journalisé
            logger.error("[R1-D1] cycle_error error=%s", type(exc).__name__)
            processed = 0
        if not processed:
            stop.wait(config.poll_seconds)
    logger.info("[R1-D1] worker_stopped")


def main(environ=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    environ = os.environ if environ is None else environ
    try:
        config = load_config(environ)
    except WorkerConfigError as exc:
        logger.error("[R1-D1] worker_refused_to_start reason=%s", exc)
        return 2

    from core.db import SessionLocal, engine  # après validation : DATABASE_URL présent
    from core.evaluation_provider import AnthropicEvaluationProvider

    if engine is None or SessionLocal is None:
        logger.error("[R1-D1] worker_refused_to_start reason=%s manquant", DATABASE_ENV)
        return 2
    try:
        check_schema(engine)
    except WorkerConfigError as exc:
        logger.error("[R1-D1] worker_refused_to_start reason=%s", exc)
        return 2
    provider = AnthropicEvaluationProvider(model_id=config.model_id, timeout_seconds=config.llm_timeout_seconds,
                                           api_key=environ[API_KEY_ENV].strip())
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    run_forever(SessionLocal, config, provider, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
