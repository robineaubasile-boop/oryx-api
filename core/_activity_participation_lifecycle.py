"""Frontière privée de transaction du cycle de vie des participations,
visites et interactions (V0-T08 Lot 2C, PR 3) — DORMANTE.

AUCUN appelant de production (ni api.py, ni route, ni worker, ni script, ni
ordonnanceur, ni tâche périodique ; tests/test_activity_participation_lifecycle*.py) ;
aucune variable d'activation ; aucun accès F2 ; aucune boucle sur plusieurs
pages ; aucun retry automatique (cadence, parcours complets, réexamen des
échecs et surveillance : lot 2C, PR 5). N'importe que :

- core.product_analytics.participation_lifecycle (sélections, finalisation
  des visites, purge des participations, relectures) ;
- core._activity_transactions (garde-fous de propriété transactionnelle,
  inchangés) ;
- core._activity_event_finalization : sa primitive de transaction analytics
  (READ COMMITTED, lock_timeout 2 s, statement_timeout 5 s en SET LOCAL),
  réutilisée telle quelle ; ses points d'entrée historiques ne sont ni
  appelés ni modifiés.

Propriété de la transaction : chaque point d'entrée exige un
`sqlalchemy.Engine` PostgreSQL synchrone transactionnel (refus statique,
TypeError, AVANT toute connexion : Connection, Session, URL, autre SGBD,
moteur asynchrone, AUTOCOMMIT configuré, lecture seule), à partir duquel la
frontière ouvre ELLE-MÊME chaque transaction. Dans CHAQUE transaction
(sélection, finalisation, purge, relecture d'un COMMIT incertain), libpq
doit rapporter un bloc de transaction effectif (PQTRANS_INTRANS) avant tout
travail ; sinon rien n'est lu ni écrit : unknown (page : selected = False).

Une transaction DISTINCTE par candidat ; la sélection est une transaction de
lecture séparée, terminée avant tout traitement. Un échec isolé ne
compromet aucun autre candidat ; le curseur vient de la photographie de
sélection, jamais d'un traitement ; une sélection échouée n'avance aucun
curseur.

Issues d'une finalisation (vocabulaire fermé) : finalized (COMMIT CONFIRMÉ
seulement), already_finalized, not_due, not_admissible (retention_expired),
absent (après un COMMIT incertain : unconfirmed_commit, jamais attribuée),
conflict, unknown, unconfirmed (COMMIT incertain, état attendu observé :
JAMAIS finalized), still_present (COMMIT incertain, état initial intact
observé).

Issues d'une purge : purged (COMMIT CONFIRMÉ seulement), blocked, not_due,
not_admissible, absent (après un COMMIT incertain : unconfirmed_commit,
seulement si l'absence COMPLÈTE est observée), conflict, unknown. Aucune
issue still_present ni unconfirmed : une participation encore présente
après un COMMIT incertain est unknown.

Journaux : issues, motifs fermés et compteurs seulement (aucun identifiant,
aucun propriétaire, aucun document de conservation, aucun curseur).
"""
import logging
import uuid
from dataclasses import dataclass

from core import _activity_event_finalization as finalization_boundary
from core._activity_transactions import _owned, _require_engine
from core.product_analytics import participation_lifecycle

logger = logging.getLogger("oryx.activity_participation_lifecycle")

FINALIZED = participation_lifecycle.FINALIZED
ALREADY_FINALIZED = participation_lifecycle.ALREADY_FINALIZED
PURGED = participation_lifecycle.PURGED
BLOCKED = participation_lifecycle.BLOCKED
NOT_DUE = participation_lifecycle.NOT_DUE
NOT_ADMISSIBLE = participation_lifecycle.NOT_ADMISSIBLE
ABSENT = participation_lifecycle.ABSENT
CONFLICT = participation_lifecycle.CONFLICT
UNKNOWN = participation_lifecycle.UNKNOWN
UNCONFIRMED = "unconfirmed"
STILL_PRESENT = "still_present"
FINALIZATION_OUTCOMES = (FINALIZED, ALREADY_FINALIZED, NOT_DUE, NOT_ADMISSIBLE, ABSENT, CONFLICT, UNKNOWN,
                         UNCONFIRMED, STILL_PRESENT)
PURGE_OUTCOMES = (PURGED, BLOCKED, NOT_DUE, NOT_ADMISSIBLE, ABSENT, CONFLICT, UNKNOWN)

TECHNICAL_UNCERTAINTY = "technical_uncertainty"
UNCONFIRMED_COMMIT = "unconfirmed_commit"
OUTCOME_REASONS = participation_lifecycle.REASONS + (TECHNICAL_UNCERTAINTY, UNCONFIRMED_COMMIT)

_COMMITTED, _FAILED = finalization_boundary._COMMITTED, finalization_boundary._FAILED


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Issue ÉTABLIE d'une finalisation : FINALIZATION_OUTCOMES, motif
    OUTCOME_REASONS ou None. Aucun identifiant."""
    outcome: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PurgeOutcome:
    """Issue ÉTABLIE d'une purge : PURGE_OUTCOMES, motif OUTCOME_REASONS ou
    None ; interactions / sessions : lignes supprimées, rapportées après un
    COMMIT CONFIRMÉ seulement (0 sinon). Aucun identifiant."""
    outcome: str
    reason: str | None = None
    interactions: int = 0
    sessions: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class FinalizationPageReport:
    """Bilan d'une page du parcours A : selected (la sélection a abouti), un
    compteur par issue, examined = leur somme ; absent_unconfirmed : parmi
    absent, ceux constatés après un COMMIT incertain ; next_cursor :
    photographie de sélection (None si épuisée ou sélection échouée)."""
    selection: str
    selected: bool
    examined: int
    finalized: int
    already_finalized: int
    not_due: int
    not_admissible: int
    absent: int
    conflict: int
    unknown: int
    unconfirmed: int
    still_present: int
    absent_unconfirmed: int
    next_cursor: object

    def counters(self) -> dict:
        return {outcome: getattr(self, outcome) for outcome in FINALIZATION_OUTCOMES}

    def __repr__(self) -> str:
        counts = " ".join(f"{name}={value}" for name, value in self.counters().items())
        return (f"FinalizationPageReport({self.selection} selected={self.selected} examined={self.examined} "
                f"{counts} absent_unconfirmed={self.absent_unconfirmed} more={self.next_cursor is not None})")


@dataclass(frozen=True, slots=True, repr=False)
class PurgePageReport:
    """Bilan d'une page du parcours B (rapport distinct) ; interactions /
    sessions : descendants supprimés par les purges CONFIRMÉES de la page
    (jamais des candidats)."""
    selection: str
    selected: bool
    examined: int
    purged: int
    blocked: int
    not_due: int
    not_admissible: int
    absent: int
    conflict: int
    unknown: int
    absent_unconfirmed: int
    interactions: int
    sessions: int
    next_cursor: object

    def counters(self) -> dict:
        return {outcome: getattr(self, outcome) for outcome in PURGE_OUTCOMES}

    def __repr__(self) -> str:
        counts = " ".join(f"{name}={value}" for name, value in self.counters().items())
        return (f"PurgePageReport({self.selection} selected={self.selected} examined={self.examined} {counts} "
                f"absent_unconfirmed={self.absent_unconfirmed} interactions={self.interactions} "
                f"sessions={self.sessions} more={self.next_cursor is not None})")


def _require_uuid(value, field: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise TypeError(f"{field} : uuid.UUID requis")
    return value


def _require_user_id(value) -> str:
    if type(value) is not str:
        raise TypeError("user_id : str requis")
    participation_lifecycle._require_user_id(value)
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


def _select(engine, select, limit, after):
    status, page = finalization_boundary._transaction(engine, _owned(lambda db: select(db, limit=limit, after=after)))
    return (status != _FAILED), page  # lecture seule : un COMMIT perdu ne l'invalide pas


# --------------------------------------------------------------------------
# Parcours A : finalisation d'une visite inactive
# --------------------------------------------------------------------------

def _session_unknown() -> SessionOutcome:
    return SessionOutcome(UNKNOWN, TECHNICAL_UNCERTAINTY)


def _session_reread(attempt, observation) -> SessionOutcome:
    """Issue d'une finalisation dont le COMMIT est incertain, d'après une
    relecture STABLE de la photographie COMPLÈTE de la visite, sous la clé de
    l'opération tentée : unconfirmed seulement si l'état attendu est observé,
    still_present seulement si l'état initial l'est ; jamais finalized."""
    if observation.outcome == participation_lifecycle.ABSENT:
        return SessionOutcome(ABSENT, UNCONFIRMED_COMMIT)
    if observation.outcome != participation_lifecycle.PRESENT:
        return _session_unknown()
    if observation.state == attempt.after:
        return SessionOutcome(UNCONFIRMED, UNCONFIRMED_COMMIT)
    if observation.state == attempt.before:
        return SessionOutcome(STILL_PRESENT, TECHNICAL_UNCERTAINTY)
    return _session_unknown()


def _finalize(engine, session_id) -> SessionOutcome:
    status, decision = finalization_boundary._transaction(
        engine, _owned(lambda db: participation_lifecycle.finalize_idle_session(db, session_id=session_id)))
    if status == _FAILED:
        return _session_unknown()  # rien n'est committé
    if status == _COMMITTED or not decision.written:
        return SessionOutcome(decision.outcome, decision.reason)  # finalized : COMMIT confirmé seulement
    # COMMIT incertain d'une écriture : relecture dans une NOUVELLE transaction,
    # vérifiée pour elle-même, sous la clé de l'opération tentée.
    attempt = decision.attempt
    status, observation = finalization_boundary._transaction(
        engine, _owned(lambda db: participation_lifecycle._observe_session_attempt(db, attempt=attempt)))
    if status == _FAILED:
        return _session_unknown()
    return _session_reread(attempt, observation)


def finalize_session(engine, *, session_id: uuid.UUID) -> SessionOutcome:
    """Finalise UNE visite inactive, issue établie."""
    engine, session_id = _require_engine(engine), _require_uuid(session_id, "session_id")
    outcome = _guarded(lambda: _finalize(engine, session_id), _session_unknown())
    _log("activity_session_finalization outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


def finalize_sessions_page(engine, *, limit: int = participation_lifecycle.DEFAULT_PAGE_SIZE,
                           after: participation_lifecycle.SessionCursor | None = None) -> FinalizationPageReport:
    """UNE page du parcours A, chaque visite dans sa propre transaction."""
    engine = _require_engine(engine)  # entrées validées AVANT toute connexion
    participation_lifecycle._require_limit(limit)
    participation_lifecycle._require_cursor(after, participation_lifecycle.SessionCursor)
    selected, page = _select(engine, participation_lifecycle.select_idle_session_candidates, limit, after)
    counts, absent_unconfirmed = dict.fromkeys(FINALIZATION_OUTCOMES, 0), 0
    for session_id in (page.session_ids if selected else ()):
        outcome = finalize_session(engine, session_id=session_id)
        counts[outcome.outcome] += 1
        absent_unconfirmed += outcome.outcome == ABSENT and outcome.reason == UNCONFIRMED_COMMIT
    report = FinalizationPageReport(participation_lifecycle.SESSION_FINALIZATION, selected, sum(counts.values()),
                                    absent_unconfirmed=absent_unconfirmed,
                                    next_cursor=page.next_cursor if selected else None, **counts)
    _log("activity_session_lifecycle selection=%s selected=%s examined=%d " +
         " ".join(f"{outcome}=%d" for outcome in FINALIZATION_OUTCOMES) + " absent_unconfirmed=%d",
         report.selection, selected, report.examined, *counts.values(), absent_unconfirmed)
    return report


# --------------------------------------------------------------------------
# Parcours B : purge d'une participation
# --------------------------------------------------------------------------

def _purge_unknown() -> PurgeOutcome:
    return PurgeOutcome(UNKNOWN, TECHNICAL_UNCERTAINTY)


def _purge(engine, campaign_id, user_id) -> PurgeOutcome:
    status, decision = finalization_boundary._transaction(engine, _owned(
        lambda db: participation_lifecycle.purge_participation(db, campaign_id=campaign_id, user_id=user_id)))
    if status == _FAILED:
        return _purge_unknown()  # rien n'est committé
    if status == _COMMITTED:
        return PurgeOutcome(decision.outcome, decision.reason, decision.interactions, decision.sessions)
    if not decision.written:
        return PurgeOutcome(decision.outcome, decision.reason)
    # COMMIT incertain d'une purge : relecture CONSERVATRICE ; seule l'absence
    # complète est rapportée (absent / unconfirmed_commit), jamais purged.
    attempt = decision.attempt
    status, observation = finalization_boundary._transaction(
        engine, _owned(lambda db: participation_lifecycle._observe_purge_attempt(db, attempt=attempt)))
    if status == _FAILED or observation.outcome != participation_lifecycle.ABSENT:
        return _purge_unknown()  # encore présente, ou relecture impossible
    return PurgeOutcome(ABSENT, UNCONFIRMED_COMMIT)


def purge_participation(engine, *, campaign_id: uuid.UUID, user_id: str) -> PurgeOutcome:
    """Purge UNE participation échue et ses descendants, issue établie."""
    engine = _require_engine(engine)
    campaign_id, user_id = _require_uuid(campaign_id, "campaign_id"), _require_user_id(user_id)
    outcome = _guarded(lambda: _purge(engine, campaign_id, user_id), _purge_unknown())
    _log("activity_participation_purge outcome=%s reason=%s", outcome.outcome, outcome.reason)
    return outcome


def purge_participations_page(engine, *, limit: int = participation_lifecycle.DEFAULT_PAGE_SIZE,
                              after: participation_lifecycle.ParticipationCursor | None = None) -> PurgePageReport:
    """UNE page du parcours B, chaque participation dans sa propre transaction."""
    engine = _require_engine(engine)
    participation_lifecycle._require_limit(limit)
    participation_lifecycle._require_cursor(after, participation_lifecycle.ParticipationCursor)
    selected, page = _select(engine, participation_lifecycle.select_purgeable_participation_candidates, limit, after)
    counts, absent_unconfirmed, interactions, visits = dict.fromkeys(PURGE_OUTCOMES, 0), 0, 0, 0
    for campaign_id, user_id in (page.participations if selected else ()):
        outcome = purge_participation(engine, campaign_id=campaign_id, user_id=user_id)
        counts[outcome.outcome] += 1
        absent_unconfirmed += outcome.outcome == ABSENT and outcome.reason == UNCONFIRMED_COMMIT
        interactions, visits = interactions + outcome.interactions, visits + outcome.sessions
    report = PurgePageReport(participation_lifecycle.PARTICIPATION_PURGE, selected, sum(counts.values()),
                             absent_unconfirmed=absent_unconfirmed, interactions=interactions, sessions=visits,
                             next_cursor=page.next_cursor if selected else None, **counts)
    _log("activity_participation_lifecycle selection=%s selected=%s examined=%d " +
         " ".join(f"{outcome}=%d" for outcome in PURGE_OUTCOMES) +
         " absent_unconfirmed=%d interactions=%d sessions=%d",
         report.selection, selected, report.examined, *counts.values(), absent_unconfirmed, interactions, visits)
    return report
