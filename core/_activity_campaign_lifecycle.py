"""Frontière privée de transaction du cycle de vie des campagnes :
terminaison, purge exceptionnelle des preuves à l'échéance de leur campagne
et purge définitive des campagnes (V0-T08 Lot 2C, PR 4) — DORMANTE.

AUCUN appelant de production (ni api.py, ni route, ni worker, ni script, ni
ordonnanceur, ni tâche périodique ; tests/test_activity_campaign_lifecycle*.py) ;
aucune variable d'activation ; aucun accès F2 ; aucune boucle sur plusieurs
pages ; aucun retry automatique ; aucun parcours n'en déclenche un autre
(coordination, cadence, réexamen des échecs : lot 2C, PR 5). N'importe que :

- core.product_analytics.campaign_lifecycle (sélections, terminaison, purge
  exceptionnelle des preuves, purge des campagnes, relectures) ;
- core._activity_transactions (garde-fous de propriété transactionnelle,
  inchangés) ;
- core._activity_event_finalization : sa primitive de transaction analytics
  (READ COMMITTED, lock_timeout 2 s, statement_timeout 5 s en SET LOCAL),
  réutilisée telle quelle ; ses points d'entrée historiques ne sont ni
  appelés ni modifiés.

Propriété de la transaction : chaque point d'entrée exige un
`sqlalchemy.Engine` PostgreSQL synchrone transactionnel (refus statique,
TypeError, AVANT toute connexion), à partir duquel la frontière ouvre
ELLE-MÊME chaque transaction. Dans CHAQUE transaction (sélection, candidat,
relecture d'un COMMIT incertain), libpq doit rapporter un bloc de
transaction effectif (PQTRANS_INTRANS) avant tout travail ; sinon rien n'est
lu ni écrit : unknown (page : selected = False).

Une transaction DISTINCTE par candidat ; la sélection est une transaction de
lecture séparée, terminée avant tout traitement ; le curseur vient de la
photographie de sélection ; une sélection échouée n'avance aucun curseur.

Issues (vocabulaires fermés) — seul un COMMIT CONFIRMÉ autorise un succès :

- terminaison : closed, cancelled, already_terminal, not_due, absent
  (unconfirmed_commit après un COMMIT incertain), conflict, unknown,
  unconfirmed (COMMIT incertain, état attendu intégralement observé : JAMAIS
  closed ni cancelled), still_present (COMMIT incertain, état initial
  intégralement observé) ;
- purge exceptionnelle : purged, resolved_and_purged, not_admissible,
  not_due, absent (unconfirmed_commit seulement si l'absence de la preuve et
  de toute preuve de même identité canonique est établie), conflict,
  unknown ;
- purge de campagne : purged, blocked, not_admissible, not_due, absent
  (unconfirmed_commit seulement si la campagne, sa cohorte, ses places et
  toutes ses dépendances physiques et logiques sont absentes), conflict,
  unknown.

Journaux : issues, motifs fermés et compteurs seulement (aucun identifiant,
aucun code ni nom de campagne, aucun document de conservation, aucun
curseur).
"""
import logging
import uuid
from dataclasses import dataclass

from core import _activity_event_finalization as finalization_boundary
from core._activity_transactions import _owned, _require_engine
from core.product_analytics import campaign_lifecycle

logger = logging.getLogger("oryx.activity_campaign_lifecycle")

cl = campaign_lifecycle
CLOSED, CANCELLED, ALREADY_TERMINAL = cl.CLOSED_OUTCOME, cl.CANCELLED_OUTCOME, cl.ALREADY_TERMINAL
PURGED, RESOLVED_AND_PURGED, BLOCKED = cl.PURGED, cl.RESOLVED_AND_PURGED, cl.BLOCKED
NOT_DUE, NOT_ADMISSIBLE, ABSENT, CONFLICT, UNKNOWN = cl.NOT_DUE, cl.NOT_ADMISSIBLE, cl.ABSENT, cl.CONFLICT, cl.UNKNOWN
UNCONFIRMED = "unconfirmed"
STILL_PRESENT = "still_present"
TERMINATION_OUTCOMES = cl.TERMINATION_OUTCOMES + (UNCONFIRMED, STILL_PRESENT)
PROOF_PURGE_OUTCOMES = cl.PROOF_PURGE_OUTCOMES
CAMPAIGN_PURGE_OUTCOMES = cl.CAMPAIGN_PURGE_OUTCOMES

TECHNICAL_UNCERTAINTY = "technical_uncertainty"
UNCONFIRMED_COMMIT = "unconfirmed_commit"
OUTCOME_REASONS = cl.REASONS + (TECHNICAL_UNCERTAINTY, UNCONFIRMED_COMMIT)

_COMMITTED, _FAILED = finalization_boundary._COMMITTED, finalization_boundary._FAILED


@dataclass(frozen=True, slots=True)
class Outcome:
    """Issue ÉTABLIE d'un candidat : outcome du vocabulaire de son parcours,
    motif OUTCOME_REASONS ou None ; resolution : (état, motif) écrits avant
    la suppression d'une preuve (resolved_and_purged, COMMIT confirmé
    seulement) ; seats / cohorts : lignes supprimées par une purge de
    campagne CONFIRMÉE (0 sinon). Aucun identifiant."""
    outcome: str
    reason: str | None = None
    resolution: tuple | None = None
    seats: int = 0
    cohorts: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class PageReport:
    """Bilan d'une page d'UN parcours : selected (la sélection a abouti),
    counts (un compteur par issue du parcours, examined = leur somme),
    absent_unconfirmed (parmi absent, ceux constatés après un COMMIT
    incertain), seats / cohorts (purges de campagne confirmées seulement),
    next_cursor (photographie de sélection ; None si épuisée ou échouée)."""
    selection: str
    selected: bool
    examined: int
    counts: tuple
    absent_unconfirmed: int
    seats: int
    cohorts: int
    next_cursor: object

    def counters(self) -> dict:
        return dict(self.counts)

    def __getattr__(self, name):
        counts = object.__getattribute__(self, "counts")
        for outcome, value in counts:
            if outcome == name:
                return value
        raise AttributeError(name)

    def __repr__(self) -> str:
        counts = " ".join(f"{name}={value}" for name, value in self.counts)
        return (f"PageReport({self.selection} selected={self.selected} examined={self.examined} {counts} "
                f"absent_unconfirmed={self.absent_unconfirmed} seats={self.seats} cohorts={self.cohorts} "
                f"more={self.next_cursor is not None})")


def _require_uuid(value, field: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise TypeError(f"{field} : uuid.UUID requis")
    return value


def _log(message: str, *args) -> None:
    try:
        logger.info(message, *args)
    except Exception:  # noqa: BLE001
        pass


def _guarded(action, unknown):
    try:
        return action()
    except Exception:  # noqa: BLE001 — jamais une exception pour l'appelant
        return unknown


def _unknown() -> Outcome:
    return Outcome(UNKNOWN, TECHNICAL_UNCERTAINTY)


def _attempt(engine, work, observe, established):
    """Traitement d'UN candidat dans sa transaction ; COMMIT incertain d'une
    écriture : relecture dans une NOUVELLE transaction, vérifiée pour
    elle-même, puis `established(decision, observation)`."""
    status, decision = finalization_boundary._transaction(engine, _owned(work))
    if status == _FAILED:
        return _unknown()  # rien n'est committé
    if status == _COMMITTED or not decision.written:
        return established(decision, None)
    attempt = decision.attempt
    status, observation = finalization_boundary._transaction(engine, _owned(lambda db: observe(db, attempt=attempt)))
    if status == _FAILED:
        return _unknown()
    return established(decision, observation)


def _select(engine, select, limit, after):
    status, page = finalization_boundary._transaction(engine, _owned(lambda db: select(db, limit=limit, after=after)))
    return (status != _FAILED), page  # lecture seule : un COMMIT perdu ne l'invalide pas


def _page(engine, selection, select, items, single, outcomes, limit, after, cursor_type) -> PageReport:
    """UNE page d'un parcours, chaque candidat dans sa propre transaction."""
    cl._require_limit(limit)
    cl._require_cursor(after, cursor_type)
    selected, page = _select(engine, select, limit, after)
    counts, absent_unconfirmed, seats, cohorts = dict.fromkeys(outcomes, 0), 0, 0, 0
    for item in (items(page) if selected else ()):
        outcome = single(item)
        counts[outcome.outcome] += 1
        absent_unconfirmed += outcome.outcome == ABSENT and outcome.reason == UNCONFIRMED_COMMIT
        seats, cohorts = seats + outcome.seats, cohorts + outcome.cohorts
    report = PageReport(selection, selected, sum(counts.values()), tuple(counts.items()), absent_unconfirmed, seats,
                        cohorts, page.next_cursor if selected else None)
    _log("activity_campaign_lifecycle selection=%s selected=%s examined=%d " +
         " ".join(f"{outcome}=%d" for outcome in outcomes) + " absent_unconfirmed=%d seats=%d cohorts=%d",
         selection, selected, report.examined, *counts.values(), absent_unconfirmed, seats, cohorts)
    return report


# --------------------------------------------------------------------------
# Parcours A : terminaison
# --------------------------------------------------------------------------

def _termination_established(decision, observation) -> Outcome:
    """COMMIT incertain : unconfirmed seulement si l'état attendu est
    intégralement observé, still_present seulement si l'état initial l'est ;
    jamais closed ni cancelled."""
    if observation is None:
        return Outcome(decision.outcome, decision.reason)
    attempt = decision.attempt
    if observation.outcome == cl.ABSENT:
        return Outcome(ABSENT, UNCONFIRMED_COMMIT)
    if observation.outcome == cl.PRESENT and observation.state == attempt.after:
        return Outcome(UNCONFIRMED, UNCONFIRMED_COMMIT)
    if observation.outcome == cl.PRESENT and observation.state == attempt.before:
        return Outcome(STILL_PRESENT, TECHNICAL_UNCERTAINTY)
    return _unknown()


def terminate_campaign(engine, *, campaign_id: uuid.UUID) -> Outcome:
    """Termine UNE campagne échue, issue établie."""
    engine, campaign_id = _require_engine(engine), _require_uuid(campaign_id, "campaign_id")
    outcome = _guarded(lambda: _attempt(
        engine, lambda db: cl.terminate_due_campaign(db, campaign_id=campaign_id), cl._observe_termination_attempt,
        _termination_established), _unknown())
    _log("activity_campaign_termination outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


def terminate_campaigns_page(engine, *, limit: int = cl.DEFAULT_PAGE_SIZE,
                             after: cl.TerminationCursor | None = None) -> PageReport:
    """UNE page du parcours A."""
    engine = _require_engine(engine)
    return _page(engine, cl.CAMPAIGN_TERMINATION, cl.select_due_campaign_candidates, lambda page: page.campaign_ids,
                 lambda campaign_id: terminate_campaign(engine, campaign_id=campaign_id), TERMINATION_OUTCOMES,
                 limit, after, cl.TerminationCursor)


# --------------------------------------------------------------------------
# Parcours B : purge exceptionnelle d'une preuve
# --------------------------------------------------------------------------

def _proof_established(decision, observation) -> Outcome:
    """COMMIT incertain : absent / unconfirmed_commit seulement si l'absence
    est établie sous les clés originales ; jamais purged."""
    if observation is None:
        return Outcome(decision.outcome, decision.reason, decision.resolution if decision.written else None)
    if observation.outcome == cl.ABSENT:
        return Outcome(ABSENT, UNCONFIRMED_COMMIT)
    return _unknown()


def purge_campaign_proof(engine, *, proof_id: uuid.UUID) -> Outcome:
    """Purge exceptionnelle d'UNE preuve, issue établie."""
    engine, proof_id = _require_engine(engine), _require_uuid(proof_id, "proof_id")
    outcome = _guarded(lambda: _attempt(
        engine, lambda db: cl.purge_campaign_expired_proof(db, proof_id=proof_id), cl._observe_proof_attempt,
        _proof_established), _unknown())
    _log("activity_campaign_proof_purge outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


def purge_campaign_proofs_page(engine, *, limit: int = cl.DEFAULT_PAGE_SIZE,
                               after: cl.CampaignProofCursor | None = None) -> PageReport:
    """UNE page du parcours B."""
    engine = _require_engine(engine)
    return _page(engine, cl.CAMPAIGN_PROOF_PURGE, cl.select_campaign_expired_proof_candidates,
                 lambda page: page.proof_ids, lambda proof_id: purge_campaign_proof(engine, proof_id=proof_id),
                 PROOF_PURGE_OUTCOMES, limit, after, cl.CampaignProofCursor)


# --------------------------------------------------------------------------
# Parcours C : purge définitive d'une campagne
# --------------------------------------------------------------------------

def _campaign_established(decision, observation) -> Outcome:
    """COMMIT incertain : absent / unconfirmed_commit seulement si l'absence
    COMPLÈTE est établie (finalités comprises, lecture globale achevée) ;
    jamais purged."""
    if observation is None:
        return Outcome(decision.outcome, decision.reason, None, decision.seats, decision.cohorts)
    if observation.outcome == cl.ABSENT:
        return Outcome(ABSENT, UNCONFIRMED_COMMIT)
    return _unknown()


def purge_campaign(engine, *, campaign_id: uuid.UUID) -> Outcome:
    """Purge définitive d'UNE campagne, de sa cohorte et de ses places, issue
    établie."""
    engine, campaign_id = _require_engine(engine), _require_uuid(campaign_id, "campaign_id")
    outcome = _guarded(lambda: _attempt(
        engine, lambda db: cl.purge_campaign(db, campaign_id=campaign_id), cl._observe_campaign_attempt,
        _campaign_established), _unknown())
    _log("activity_campaign_purge outcome=%s reason=%s seats=%d cohorts=%d", outcome.outcome, outcome.reason,
         outcome.seats, outcome.cohorts)
    return outcome


def purge_campaigns_page(engine, *, limit: int = cl.DEFAULT_PAGE_SIZE,
                         after: cl.CampaignPurgeCursor | None = None) -> PageReport:
    """UNE page du parcours C."""
    engine = _require_engine(engine)
    return _page(engine, cl.CAMPAIGN_PURGE, cl.select_purgeable_campaign_candidates, lambda page: page.campaign_ids,
                 lambda campaign_id: purge_campaign(engine, campaign_id=campaign_id), CAMPAIGN_PURGE_OUTCOMES,
                 limit, after, cl.CampaignPurgeCursor)
