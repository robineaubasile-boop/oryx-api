"""Frontière privée de transaction du cycle de vie du registre provisoire des
preuves (V0-T08 Lot 2C, PR 1) — DORMANTE.

AUCUN appelant de production (ni api.py, ni route, ni worker, ni script, ni
ordonnanceur, ni tâche périodique ; tests/test_activity_proof_lifecycle*.py) ;
aucune variable d'activation ; aucun accès F2 ; aucune boucle sur plusieurs
pages (l'orchestration périodique relève de la PR 5 du lot 2C). N'importe que :

- core.product_analytics.proof_lifecycle (sélection bornée, purge) ;
- core.product_analytics.finalization (services du bloc C, INCHANGÉS) ;
- core._activity_event_finalization (frontière du bloc C, INCHANGÉE) :
  sa transaction analytics (READ COMMITTED, lock_timeout 2 s,
  statement_timeout 5 s) et son établissement de l'issue d'un COMMIT
  incertain sont réutilisés tels quels, jamais réécrits.

Une transaction DISTINCTE par preuve : un échec isolé (délai de verrou,
interblocage détecté, panne, COMMIT incertain) ne compromet aucun autre
candidat ; la sélection est une transaction de lecture courte, terminée
avant tout traitement (aucun verrou de sélection conservé).

Points d'entrée (aucun ne lève, sauf entrée invalide avant toute connexion) :

  recover_certified_proof / expire_pending_proof (engine, *, proof_id) :
      finalization.finalize_certified_proof /
      resolve_expired_pending_proof dans la frontière du bloc C ; l'écriture
      n'est attribuée à CE traitement que si la décision a écrit ET que
      l'issue établie (COMMIT confirmé, ou COMMIT incertain vérifié par
      relecture) est exactement cette décision ;
  purge_proof (engine, *, proof_id) :
      proof_lifecycle.purge_resolved_proof, puis COMMIT ; COMMIT incertain :
      relecture dans une NOUVELLE transaction (proof_lifecycle.observe_purge,
      même verrou) :
        - preuve absente -> ABSENT / unconfirmed_commit : elle n'est plus
          présente, sans que la suppression soit attribuée à ce traitement ;
        - preuve présente -> STILL_PRESENT / technical_uncertainty (retry
          explicite sûr ; AUCUN retry automatique) ;
        - relecture impossible -> UNKNOWN.
  recover_certified_page / expire_pending_page / purge_resolved_page
  (engine, *, limit, after) :
      UNE page de candidats (proof_lifecycle.select_*), chaque candidat dans
      sa propre transaction, compteurs fermés et curseur de la page suivante.

Compteurs (PageReport) : un candidat examiné compte dans EXACTEMENT une issue
; FINALIZED, RESOLVED et PURGED ne comptent que des écritures de CE traitement
dont le COMMIT est établi (jamais une écriture seulement flushée).

Journaux : sélection, issue et motif FERMÉS, compteurs seulement (aucun
identifiant, aucun propriétaire, aucun contenu, aucun curseur).
"""
import logging
import uuid
from dataclasses import dataclass

from core import _activity_event_finalization as finalization_boundary
from core.product_analytics import finalization, proof_lifecycle

logger = logging.getLogger("oryx.activity_proof_lifecycle")

# Issues d'un candidat (vocabulaire fermé).
FINALIZED = "finalized"  # recorded par ce traitement, COMMIT établi
RESOLVED = "resolved"  # rejected / indeterminate par ce traitement, sans événement, COMMIT établi
PURGED = "purged"  # supprimée par ce traitement, COMMIT confirmé
ABSENT = "absent"  # preuve absente (aucune suppression attribuée à ce traitement)
STILL_PRESENT = "still_present"  # COMMIT incertain d'une suppression qui n'a pas eu lieu
NOT_ADMISSIBLE = "not_admissible"  # rien à faire sous verrou (non échue, déjà résolue, non terminale...)
CONFLICT = "conflict"  # course perdue, identité changée, état enregistré incohérent
UNKNOWN = "unknown"  # issue technique inconnue (panne, délai, relecture impossible)
OUTCOMES = (FINALIZED, RESOLVED, PURGED, ABSENT, STILL_PRESENT, NOT_ADMISSIBLE, CONFLICT, UNKNOWN)

TECHNICAL_UNCERTAINTY = finalization.TECHNICAL_UNCERTAINTY
UNCONFIRMED_COMMIT = "unconfirmed_commit"
OUTCOME_REASONS = finalization.FINALIZATION_REASONS + proof_lifecycle.PURGE_REASONS + (UNCONFIRMED_COMMIT,)

_RESOLUTIONS = (finalization.REJECTED, finalization.INDETERMINATE)
_PRIOR_STATES = (finalization.PENDING, finalization.CERTIFIED)
_PURGE_MAPPING = {proof_lifecycle.PURGED: PURGED, proof_lifecycle.ABSENT: ABSENT,
                  proof_lifecycle.NOT_PURGEABLE: NOT_ADMISSIBLE, proof_lifecycle.CONFLICT: CONFLICT}


@dataclass(frozen=True, slots=True)
class LifecycleOutcome:
    """Issue ÉTABLIE pour un candidat. outcome : OUTCOMES ; state : statut de
    la preuve après l'appel (finalization.FINALIZATION_STATES, 'unknown', ou
    None) ; reason : OUTCOME_REASONS ou None. Aucun identifiant."""
    outcome: str
    state: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class PageReport:
    """Bilan d'une page : selection, selected (la lecture des candidats a
    abouti), un compteur par issue, examined = leur somme, next_cursor
    (proof_lifecycle.ProofCursor, None quand la sélection est épuisée ou
    illisible). Interne ; repr sans curseur."""
    selection: str
    selected: bool
    examined: int
    finalized: int
    resolved: int
    purged: int
    absent: int
    still_present: int
    not_admissible: int
    conflict: int
    unknown: int
    next_cursor: object

    def counters(self) -> dict:
        return {outcome: getattr(self, outcome) for outcome in OUTCOMES}

    def __repr__(self) -> str:
        counts = " ".join(f"{name}={value}" for name, value in self.counters().items())
        return (f"PageReport({self.selection} selected={self.selected} examined={self.examined} {counts} "
                f"more={self.next_cursor is not None})")


def _require_proof_id(proof_id) -> uuid.UUID:
    if type(proof_id) is not uuid.UUID:
        raise TypeError("proof_id : uuid.UUID requis")
    return proof_id


# --------------------------------------------------------------------------
# Récupération et résolution : bloc C, frontière du bloc C
# --------------------------------------------------------------------------

def _classify(report, decision) -> LifecycleOutcome:
    """Issue d'un appel au bloc C. `decision` : celle de la transaction
    d'écriture (None si elle a échoué avant de rendre une décision)."""
    ours = (decision is not None and decision.written
            and report == finalization_boundary._report(decision))  # écriture de CE traitement, COMMIT établi
    state, reason = report.state, report.reason
    if state == finalization_boundary.UNKNOWN:
        return LifecycleOutcome(UNKNOWN, state, reason)
    if ours and state == finalization.RECORDED:
        return LifecycleOutcome(FINALIZED, state, reason)
    if ours and state in _RESOLUTIONS:
        return LifecycleOutcome(RESOLVED, state, reason)
    if state == finalization.ABSENT:
        return LifecycleOutcome(ABSENT, state, reason)
    if state in _PRIOR_STATES and reason == TECHNICAL_UNCERTAINTY:
        return LifecycleOutcome(UNKNOWN, state, reason)  # horloge, COMMIT non effectué : retry explicite
    if (state == finalization.CERTIFIED and reason == proof_lifecycle.OPERATION_CONFLICT) or \
            reason == finalization.RECORD_INCOHERENT:
        return LifecycleOutcome(CONFLICT, state, reason)
    return LifecycleOutcome(NOT_ADMISSIBLE, state, reason)


def _through_block_c(engine, service, proof_id) -> LifecycleOutcome:
    captured = []

    def work(db, *, proof_id):
        decision = service(db, proof_id=proof_id)
        captured.append(decision)
        return decision
    report = finalization_boundary._run(engine, work, proof_id)
    return _classify(report, captured[0] if captured else None)


def recover_certified_proof(engine, *, proof_id: uuid.UUID) -> LifecycleOutcome:
    """Reprise d'une preuve certified PERSISTÉE par le bloc C, inchangé :
    recorded (événement + attribution + preuve, une transaction), ou
    résolution par ses motifs fermés ; aucun rejeu F2 ni bloc B."""
    proof_id = _require_proof_id(proof_id)
    return _guarded(lambda: _through_block_c(engine, finalization.finalize_certified_proof, proof_id))


def expire_pending_proof(engine, *, proof_id: uuid.UUID) -> LifecycleOutcome:
    """pending -> indeterminate / finalization_expired par le bloc C, à partir
    de son échéance seulement ; avant : aucune écriture."""
    proof_id = _require_proof_id(proof_id)
    return _guarded(lambda: _through_block_c(engine, finalization.resolve_expired_pending_proof, proof_id))


# --------------------------------------------------------------------------
# Purge
# --------------------------------------------------------------------------

def _from_purge(decision) -> LifecycleOutcome:
    gone = decision.outcome in (proof_lifecycle.PURGED, proof_lifecycle.ABSENT)
    return LifecycleOutcome(_PURGE_MAPPING[decision.outcome], finalization.ABSENT if gone else decision.state,
                            decision.reason)


def _purge(engine, proof_id) -> LifecycleOutcome:
    status, decision = finalization_boundary._transaction(
        engine, lambda db: proof_lifecycle.purge_resolved_proof(db, proof_id=proof_id))
    if status == finalization_boundary._FAILED:
        return LifecycleOutcome(UNKNOWN, finalization_boundary.UNKNOWN, TECHNICAL_UNCERTAINTY)  # rien de committé
    if status == finalization_boundary._COMMITTED or not decision.written:
        return _from_purge(decision)
    # COMMIT incertain d'une suppression : relecture dans une NOUVELLE transaction.
    status, observed = finalization_boundary._transaction(
        engine, lambda db: proof_lifecycle.observe_purge(db, proof_id=proof_id))
    if status == finalization_boundary._FAILED:
        return LifecycleOutcome(UNKNOWN, finalization_boundary.UNKNOWN, TECHNICAL_UNCERTAINTY)
    if observed.outcome == proof_lifecycle.ABSENT:
        return LifecycleOutcome(ABSENT, finalization.ABSENT, UNCONFIRMED_COMMIT)  # jamais attribuée à ce traitement
    return LifecycleOutcome(STILL_PRESENT, observed.state, TECHNICAL_UNCERTAINTY)


def purge_proof(engine, *, proof_id: uuid.UUID) -> LifecycleOutcome:
    """Suppression d'une preuve terminale échue, issue établie."""
    proof_id = _require_proof_id(proof_id)
    outcome = _guarded(lambda: _purge(engine, proof_id))
    _log("activity_proof_purge outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def _guarded(action) -> LifecycleOutcome:
    try:
        return action()
    except Exception:  # noqa: BLE001 — jamais une exception pour l'appelant
        return LifecycleOutcome(UNKNOWN, finalization_boundary.UNKNOWN, TECHNICAL_UNCERTAINTY)


def _log(message: str, *args) -> None:
    try:
        logger.info(message, *args)
    except Exception:  # noqa: BLE001
        pass


def _page(engine, selection: str, select, process, limit, after) -> PageReport:
    # Entrée validée AVANT toute connexion (InvalidProductAnalyticsInput).
    proof_lifecycle._require_limit(limit)
    proof_lifecycle._require_cursor(after, selection)
    status, page = finalization_boundary._transaction(engine, lambda db: select(db, limit=limit, after=after))
    counts = dict.fromkeys(OUTCOMES, 0)
    selected = status != finalization_boundary._FAILED  # lecture seule : un COMMIT perdu ne l'invalide pas
    for proof_id in (page.proof_ids if selected else ()):
        counts[process(engine, proof_id=proof_id).outcome] += 1
    report = PageReport(selection, selected, sum(counts.values()), next_cursor=page.next_cursor if selected else None,
                        **counts)
    _log("activity_proof_lifecycle selection=%s selected=%s examined=%d " +
         " ".join(f"{outcome}=%d" for outcome in OUTCOMES), selection, selected, report.examined,
         *counts.values())
    return report


def recover_certified_page(engine, *, limit: int = proof_lifecycle.DEFAULT_PAGE_SIZE,
                           after: proof_lifecycle.ProofCursor | None = None) -> PageReport:
    return _page(engine, proof_lifecycle.CERTIFIED_RECOVERY, proof_lifecycle.select_certified_candidates,
                 recover_certified_proof, limit, after)


def expire_pending_page(engine, *, limit: int = proof_lifecycle.DEFAULT_PAGE_SIZE,
                        after: proof_lifecycle.ProofCursor | None = None) -> PageReport:
    return _page(engine, proof_lifecycle.PENDING_EXPIRY, proof_lifecycle.select_expired_pending_candidates,
                 expire_pending_proof, limit, after)


def purge_resolved_page(engine, *, limit: int = proof_lifecycle.DEFAULT_PAGE_SIZE,
                        after: proof_lifecycle.ProofCursor | None = None) -> PageReport:
    return _page(engine, proof_lifecycle.RESOLVED_PURGE, proof_lifecycle.select_purgeable_candidates,
                 purge_proof, limit, after)
