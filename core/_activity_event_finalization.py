"""Frontière privée de transaction du bloc C : finalisation atomique des
preuves certifiées (V0-T08 Lot 2B-2, bloc C) — DORMANTE.

AUCUN appelant de production (ni api.py, ni route, ni worker, ni script, ni
ordonnanceur ; tests/test_activity_event_finalization*.py) ; aucune
variable d'activation ; n'importe que le service
core.product_analytics.finalization (jamais F2, jamais la frontière du bloc
B). Le service ne possède jamais la transaction ; cette frontière l'ouvre,
la committe et établit l'issue réelle :

finalize_in_transaction(engine, *, proof_id) /
expire_pending_in_transaction(engine, *, proof_id) :

  1. transaction analytics DISTINCTE (READ COMMITTED, lock_timeout 2 s,
     statement_timeout 5 s en SET LOCAL, garde-fous du bloc B) :
     finalization.finalize_certified_proof / resolve_expired_pending_proof,
     puis COMMIT ;
  2. COMMIT confirmé : l'issue est celle de la décision ;
     rien à écrire (written=False) : l'état lu sous verrou suffit, même si la
     confirmation du COMMIT est perdue ;
  3. COMMIT incertain d'une écriture : AUCUN succès annoncé. Nouvelle
     transaction de LECTURE (finalization.observe_proof, mêmes verrous : une
     transaction encore en cours sur le serveur est attendue) :
       - même preuve, même état, même motif, même événement vérifié (preuve,
         événement, attribution et identité concordants), même instant de
         résolution -> issue confirmée ;
       - preuve encore dans son état antérieur (certified / pending) -> le
         COMMIT n'a pas eu lieu : état relu, motif technical_uncertainty (un
         retry explicite reste possible avant l'échéance ; AUCUN retry
         automatique) ;
       - toute autre lecture, ou lecture impossible -> UNKNOWN.
  4. Échec avant COMMIT (exception, délai de verrou, base indisponible) :
     rien n'est committé ; UNKNOWN / technical_uncertainty (jamais présumé).

Ne lève jamais (sauf type d'entrée invalide, avant toute connexion).
Journaux : état et motif FERMÉS seulement (aucun identifiant, aucun
propriétaire, aucun contenu).

Futur raccordement (hors périmètre) : F2 et l'analytics devront partager la
même configuration PostgreSQL canonique (idéalement le même moteur) ; cette
frontière ne le vérifie pas et ne modifie pas le runner F2.
"""
import logging
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.product_analytics import finalization

logger = logging.getLogger("oryx.activity_finalization")

# Garde-fous de chaque transaction analytics (SET LOCAL), identiques au bloc B.
ANALYTICS_GUARDS = ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCAL lock_timeout = '2s'",
                    "SET LOCAL statement_timeout = '5s'")

_COMMITTED, _FAILED, _UNCERTAIN = "committed", "failed", "uncertain"

# Issue quand aucune lecture fiable ne permet de conclure.
UNKNOWN = "unknown"
REPORT_STATES = finalization.FINALIZATION_STATES + (UNKNOWN,)
# États à partir desquels une écriture du bloc C peut avoir lieu.
_PRIOR_STATES = (finalization.CERTIFIED, finalization.PENDING)


@dataclass(frozen=True, slots=True)
class FinalizationReport:
    """Issue ÉTABLIE : state REPORT_STATES, reason
    finalization.FINALIZATION_REASONS ou None, event_id : événement
    définitif committé et vérifié (recorded seulement)."""
    state: str
    reason: str | None = None
    event_id: uuid.UUID | None = None


def _transaction(engine, work) -> tuple:
    """(issue, valeur) d'une transaction analytics ; jamais d'exception."""
    try:
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    except Exception:  # noqa: BLE001 — moteur inutilisable
        return _FAILED, None
    try:
        try:
            for guard in ANALYTICS_GUARDS:
                session.execute(sa.text(guard))
            value = work(session)
        except Exception:  # noqa: BLE001 — rien n'est committé
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 — connexion perdue : le serveur annule
                pass
            return _FAILED, None
        try:
            session.commit()
        except Exception:  # noqa: BLE001 — confirmation perdue : jamais présumée
            return _UNCERTAIN, value
        return _COMMITTED, value
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _report(decision) -> FinalizationReport:
    return FinalizationReport(decision.state, decision.reason, decision.event_id)


def _same_outcome(observed, decision) -> bool:
    return (observed.proof_id, observed.state, observed.reason, observed.event_id, observed.resolved_at) == (
        decision.proof_id, decision.state, decision.reason, decision.event_id, decision.resolved_at)


def _establish(engine, work, proof_id) -> FinalizationReport:
    status, decision = _transaction(engine, lambda db: work(db, proof_id=proof_id))
    if status == _FAILED:
        return FinalizationReport(UNKNOWN, finalization.TECHNICAL_UNCERTAINTY)
    if status == _COMMITTED or not decision.written:
        return _report(decision)
    # COMMIT incertain d'une écriture : relecture dans une NOUVELLE transaction.
    status, observed = _transaction(engine, lambda db: finalization.observe_proof(db, proof_id=proof_id))
    if status == _FAILED:  # lecture seule : un COMMIT perdu ne l'invalide pas
        return FinalizationReport(UNKNOWN, finalization.TECHNICAL_UNCERTAINTY)
    if _same_outcome(observed, decision):
        return _report(decision)  # réellement committée et vérifiée
    if observed.proof_id == decision.proof_id and observed.state in _PRIOR_STATES and observed.resolved_at is None:
        return FinalizationReport(observed.state, finalization.TECHNICAL_UNCERTAINTY)  # non committée
    return FinalizationReport(UNKNOWN, finalization.TECHNICAL_UNCERTAINTY)


def _log(report: FinalizationReport) -> None:
    level = logging.INFO if report.state in (finalization.RECORDED, finalization.PENDING) else logging.WARNING
    logger.log(level, "activity_finalization state=%s reason=%s", report.state, report.reason)


def _run(engine, work, proof_id) -> FinalizationReport:
    if type(proof_id) is not uuid.UUID:
        raise TypeError("proof_id : uuid.UUID requis")
    try:
        report = _establish(engine, work, proof_id)
    except Exception:  # noqa: BLE001 — jamais une exception pour l'appelant
        report = FinalizationReport(UNKNOWN, finalization.TECHNICAL_UNCERTAINTY)
    try:
        _log(report)
    except Exception:  # noqa: BLE001
        pass
    return report


def finalize_in_transaction(engine, *, proof_id: uuid.UUID) -> FinalizationReport:
    """Finalisation atomique d'une preuve certified, issue établie."""
    return _run(engine, finalization.finalize_certified_proof, proof_id)


def expire_pending_in_transaction(engine, *, proof_id: uuid.UUID) -> FinalizationReport:
    """Résolution explicite d'une preuve pending dont la période de
    finalisation est close, issue établie."""
    return _run(engine, finalization.resolve_expired_pending_proof, proof_id)
