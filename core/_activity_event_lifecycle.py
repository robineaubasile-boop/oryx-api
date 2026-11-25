"""Frontière privée de transaction du cycle de vie des événements d'activité
produit et de leurs attributions (V0-T08 Lot 2C, PR 2) — DORMANTE.

AUCUN appelant de production (ni api.py, ni route, ni worker, ni script, ni
ordonnanceur, ni tâche périodique ; tests/test_activity_event_lifecycle*.py) ;
aucune variable d'activation ; aucun accès F2 ; aucune boucle sur plusieurs
pages ; aucun retry automatique (cadence, parcours complets et surveillance :
lot 2C, PR 5). N'importe que :

- core.product_analytics.event_lifecycle (sélections, expiration, relecture,
  contrôle d'intégrité) ;
- core._activity_transactions (garde-fous de propriété transactionnelle,
  extraits sans changement de la frontière de la PR 1) ;
- core._activity_event_finalization : sa primitive de transaction analytics
  (READ COMMITTED, lock_timeout 2 s, statement_timeout 5 s en SET LOCAL),
  réutilisée telle quelle ; ses points d'entrée historiques ne sont ni
  appelés ni modifiés.

Propriété de la transaction : chaque point d'entrée exige un
`sqlalchemy.Engine` PostgreSQL synchrone transactionnel (refus statique,
TypeError, AVANT toute connexion : Connection, Session, URL, autre SGBD,
moteur asynchrone, AUTOCOMMIT configuré, lecture seule), à partir duquel la
frontière ouvre ELLE-MÊME chaque transaction. Dans CHAQUE transaction
(sélection nominale, traitement d'un candidat, sélection d'intégrité,
inspection, relecture d'un COMMIT incertain), libpq doit rapporter un bloc de
transaction effectif (PQTRANS_INTRANS) avant tout travail ; sinon rien n'est
lu ni écrit : unknown (page : selected = False). La validité d'une
transaction ne garantit jamais celle de la suivante, même sur le même moteur.

Une transaction DISTINCTE par candidat ; la sélection est une transaction de
lecture séparée, terminée avant tout traitement. Un échec isolé (délai de
verrou, interblocage, panne, COMMIT incertain) ne compromet aucun autre
candidat et ne bloque jamais les suivants (le curseur vient de la
photographie de sélection).

Issues d'un candidat (vocabulaire fermé) :

  pruned / purged  écriture de CE traitement, COMMIT CONFIRMÉ seulement ;
  not_due          aucune finalité échue sous verrou ;
  absent           événement absent (jamais une suppression attribuée à ce
                   traitement ; après un COMMIT incertain : unconfirmed_commit) ;
  conflict         état hors contrat, identité ou dépendance changée,
                   écriture conditionnelle non confirmée ;
  unknown          panne, délai, interblocage, horloge incohérente,
                   relecture impossible ou état différent ;
  unconfirmed      COMMIT incertain d'un retrait partiel, état révisé attendu
                   observé : JAMAIS compté comme pruned (l'observation ne
                   prouve pas que l'écriture est celle de ce traitement) ;
  still_present    COMMIT incertain, état initial intact observé (état
                   constaté, pas la preuve absolue de l'absence de COMMIT).

Contrôle d'intégrité (inspect_*) : valid / inconsistent / absent / unknown ;
aucune écriture, aucune réparation, aucun rapport stocké.

Journaux : issues, motifs fermés et compteurs seulement (aucun identifiant,
aucun propriétaire, aucun document de conservation, aucun curseur).
"""
import logging
import uuid
from dataclasses import dataclass

from core import _activity_event_finalization as finalization_boundary
from core._activity_transactions import _owned, _require_engine
from core.product_analytics import event_lifecycle

logger = logging.getLogger("oryx.activity_event_lifecycle")

PRUNED = event_lifecycle.PRUNED
PURGED = event_lifecycle.PURGED
NOT_DUE = event_lifecycle.NOT_DUE
ABSENT = event_lifecycle.ABSENT
CONFLICT = event_lifecycle.CONFLICT
UNKNOWN = event_lifecycle.UNKNOWN
UNCONFIRMED = "unconfirmed"
STILL_PRESENT = "still_present"
OUTCOMES = (PRUNED, PURGED, NOT_DUE, ABSENT, CONFLICT, UNKNOWN, UNCONFIRMED, STILL_PRESENT)

VALID, INCONSISTENT = event_lifecycle.VALID, event_lifecycle.INCONSISTENT
INTEGRITY_OUTCOMES = event_lifecycle.INTEGRITY_OUTCOMES

TECHNICAL_UNCERTAINTY = "technical_uncertainty"
UNCONFIRMED_COMMIT = "unconfirmed_commit"
OUTCOME_REASONS = event_lifecycle.REASONS + (TECHNICAL_UNCERTAINTY, UNCONFIRMED_COMMIT)

_COMMITTED, _FAILED = finalization_boundary._COMMITTED, finalization_boundary._FAILED


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """Issue ÉTABLIE pour un candidat : outcome OUTCOMES (ou
    INTEGRITY_OUTCOMES pour une inspection), reason OUTCOME_REASONS ou None.
    Aucun identifiant."""
    outcome: str
    reason: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class ExpiryPageReport:
    """Bilan d'une page nominale : selected (la sélection a abouti), un
    compteur par issue, examined = leur somme, next_cursor (photographie de
    sélection ; None si épuisée ou illisible). repr sans curseur."""
    selection: str
    selected: bool
    examined: int
    pruned: int
    purged: int
    not_due: int
    absent: int
    conflict: int
    unknown: int
    unconfirmed: int
    still_present: int
    next_cursor: object

    def counters(self) -> dict:
        return {outcome: getattr(self, outcome) for outcome in OUTCOMES}

    def __repr__(self) -> str:
        counts = " ".join(f"{name}={value}" for name, value in self.counters().items())
        return (f"ExpiryPageReport({self.selection} selected={self.selected} examined={self.examined} {counts} "
                f"more={self.next_cursor is not None})")


@dataclass(frozen=True, slots=True, repr=False)
class IntegrityPageReport:
    """Bilan d'une page du contrôle d'intégrité (rapport distinct)."""
    selection: str
    selected: bool
    examined: int
    valid: int
    inconsistent: int
    absent: int
    unknown: int
    next_cursor: object

    def counters(self) -> dict:
        return {outcome: getattr(self, outcome) for outcome in INTEGRITY_OUTCOMES}

    def __repr__(self) -> str:
        counts = " ".join(f"{name}={value}" for name, value in self.counters().items())
        return (f"IntegrityPageReport({self.selection} selected={self.selected} examined={self.examined} {counts} "
                f"more={self.next_cursor is not None})")


def _require_event_id(event_id) -> uuid.UUID:
    if type(event_id) is not uuid.UUID:
        raise TypeError("event_id : uuid.UUID requis")
    return event_id


def _unknown() -> EventOutcome:
    return EventOutcome(UNKNOWN, TECHNICAL_UNCERTAINTY)


def _guarded(action) -> EventOutcome:
    try:
        return action()
    except Exception:  # noqa: BLE001 — jamais une exception pour l'appelant
        return _unknown()


def _log(message: str, *args) -> None:
    try:
        logger.info(message, *args)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Expiration d'un événement
# --------------------------------------------------------------------------

def _from_decision(decision) -> EventOutcome:
    return EventOutcome(decision.outcome, decision.reason)


def _reread(attempt, observation) -> EventOutcome:
    """Issue d'une écriture dont le COMMIT est incertain, d'après une relecture
    faite APRÈS les clés de l'opération tentée. Jamais pruned ni purged."""
    if observation.outcome == event_lifecycle.ABSENT:
        return EventOutcome(ABSENT, UNCONFIRMED_COMMIT)
    if attempt.after is not None and observation.state == attempt.after:
        return EventOutcome(UNCONFIRMED, UNCONFIRMED_COMMIT)
    if observation.state == attempt.before:
        return EventOutcome(STILL_PRESENT, TECHNICAL_UNCERTAINTY)
    return _unknown()


def _expire(engine, event_id) -> EventOutcome:
    status, decision = finalization_boundary._transaction(
        engine, _owned(lambda db: event_lifecycle.expire_event_scopes(db, event_id=event_id)))
    if status == _FAILED:
        return _unknown()  # rien n'est committé
    if status == _COMMITTED or not decision.written:
        return _from_decision(decision)  # pruned / purged : COMMIT confirmé seulement
    # COMMIT incertain d'une écriture : relecture dans une NOUVELLE transaction,
    # vérifiée pour elle-même, sous les clés de l'opération tentée.
    attempt = decision.attempt
    status, observation = finalization_boundary._transaction(
        engine, _owned(lambda db: event_lifecycle._observe_attempt(db, attempt=attempt)))
    if status == _FAILED:
        return _unknown()
    return _reread(attempt, observation)


def expire_event(engine, *, event_id: uuid.UUID) -> EventOutcome:
    """Applique les échéances stockées d'UN événement, issue établie."""
    engine, event_id = _require_engine(engine), _require_event_id(event_id)
    outcome = _guarded(lambda: _expire(engine, event_id))
    _log("activity_event_expiry outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


# --------------------------------------------------------------------------
# Contrôle d'intégrité d'un événement
# --------------------------------------------------------------------------

def _inspect(engine, event_id) -> EventOutcome:
    status, decision = finalization_boundary._transaction(
        engine, _owned(lambda db: event_lifecycle.inspect_event_integrity(db, event_id=event_id)))
    if status == _FAILED:  # lecture seule : un COMMIT perdu ne l'invalide pas
        return _unknown()
    return EventOutcome(decision.outcome, decision.reason)


def inspect_event(engine, *, event_id: uuid.UUID) -> EventOutcome:
    """Contrôle d'intégrité d'UN événement, sans écriture."""
    engine, event_id = _require_engine(engine), _require_event_id(event_id)
    outcome = _guarded(lambda: _inspect(engine, event_id))
    _log("activity_event_integrity outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def _select(engine, select, limit, after):
    status, page = finalization_boundary._transaction(engine, _owned(lambda db: select(db, limit=limit, after=after)))
    return (status != _FAILED), page  # lecture seule : un COMMIT perdu ne l'invalide pas


def expire_events_page(engine, *, limit: int = event_lifecycle.DEFAULT_PAGE_SIZE,
                       after: event_lifecycle.ExpiryCursor | None = None) -> ExpiryPageReport:
    """UNE page de la sélection nominale, chaque candidat dans sa propre
    transaction."""
    engine = _require_engine(engine)  # entrées validées AVANT toute connexion
    event_lifecycle._require_limit(limit)
    event_lifecycle._require_cursor(after, event_lifecycle.ExpiryCursor)
    selected, page = _select(engine, event_lifecycle.select_expired_event_candidates, limit, after)
    counts = dict.fromkeys(OUTCOMES, 0)
    for event_id in (page.event_ids if selected else ()):
        counts[expire_event(engine, event_id=event_id).outcome] += 1
    report = ExpiryPageReport(event_lifecycle.EVENT_EXPIRY, selected, sum(counts.values()),
                              next_cursor=page.next_cursor if selected else None, **counts)
    _log("activity_event_lifecycle selection=%s selected=%s examined=%d " +
         " ".join(f"{outcome}=%d" for outcome in OUTCOMES), report.selection, selected, report.examined,
         *counts.values())
    return report


def inspect_events_page(engine, *, limit: int = event_lifecycle.DEFAULT_PAGE_SIZE,
                        after: event_lifecycle.IntegrityCursor | None = None) -> IntegrityPageReport:
    """UNE page du parcours d'intégrité (indépendant de la sélection
    nominale), chaque candidat dans sa propre transaction de lecture."""
    engine = _require_engine(engine)
    event_lifecycle._require_limit(limit)
    event_lifecycle._require_cursor(after, event_lifecycle.IntegrityCursor)
    selected, page = _select(engine, event_lifecycle.select_integrity_candidates, limit, after)
    counts = dict.fromkeys(INTEGRITY_OUTCOMES, 0)
    for event_id in (page.event_ids if selected else ()):
        counts[inspect_event(engine, event_id=event_id).outcome] += 1
    report = IntegrityPageReport(event_lifecycle.EVENT_INTEGRITY, selected, sum(counts.values()),
                                 next_cursor=page.next_cursor if selected else None, **counts)
    _log("activity_event_integrity_page selection=%s selected=%s examined=%d " +
         " ".join(f"{outcome}=%d" for outcome in INTEGRITY_OUTCOMES), report.selection, selected,
         report.examined, *counts.values())
    return report
