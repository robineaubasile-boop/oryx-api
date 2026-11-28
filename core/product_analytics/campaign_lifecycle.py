"""Cycle de vie des campagnes : terminaison des campagnes échues, purge
exceptionnelle des preuves à l'échéance de leur campagne et suppression
définitive des places, cohortes et campagnes (V0-T08 Lot 2C, PR 4) —
DORMANT.

Tables product_campaigns (0022), product_campaign_cohorts et
product_cohort_seats (0023), product_activity_proof_registry (0024),
INCHANGÉES. La transaction appartient toujours à l'appelant (frontière
privée core/_activity_campaign_lifecycle.py, sans appelant) : flush
seulement, jamais commit() ni rollback() ; chaque écriture a lieu dans UN
SAVEPOINT. Trois parcours INDÉPENDANTS : aucun ne déclenche un autre (leur
coordination relève de la PR 5).

Trois horloges de conservation, jamais confondues : preuves terminales,
168 heures absolues depuis resolved_at (PR 1) ; événements et attributions,
90 jours absolus depuis occurred_at (PR 2) ; participations, visites,
interactions, places, cohortes et campagnes, 90 jours absolus depuis la FIN
EFFECTIVE de la campagne, calculés par la SEULE primitive
participation_lifecycle.participation_retention_deadline (PR 3, réutilisée
telle quelle). Décisions temporelles : horloge PostgreSQL
(proofs.server_clock) lue APRÈS les verrous dont elles dépendent ; égalité
exacte avec une échéance = échéance atteinte ; jamais de date fournie par
l'appelant, jamais d'échéance recalculée depuis l'instant de traitement.

A. Terminaison (une campagne par transaction) :

   select_due_campaign_candidates  planned / active dont close_by_at <=
                                   horloge, et campagnes hors contrat
                                   (participation_lifecycle._campaign_anomaly) ;
                                   ordre (close_by_at, id) ;
   terminate_due_campaign          campagne FOR UPDATE, relecture et
                                   validation (politique, statut, dates),
                                   horloge, puis UPDATE conditionnel de
                                   status (planned -> cancelled, active ->
                                   closed) et closed_at = close_by_at
                                   SEULEMENT : une terminaison tardive ne
                                   repousse jamais la fin effective
                                   (contrat de campaigns._closing_time) ;
   observe_campaign_termination    relecture sans écriture.

B. Purge exceptionnelle d'une preuve (une preuve par transaction) :

   select_campaign_expired_proof_candidates  preuves (tous états) dont la
                                   campagne est POTENTIELLEMENT échue ou
                                   hors contrat ; ordre (campaign_id, id) ;
   purge_campaign_expired_proof    protocole L0 / L1 / L2 (clés
                                   consultatives du bloc C et de la PR 2 en
                                   UN appel trié, participation, campagne,
                                   visite et interaction FOR SHARE, preuve
                                   FOR UPDATE, attribution et événement FOR
                                   SHARE), intégrité de la preuve et de ses
                                   rattachements, échéance de la CAMPAGNE,
                                   puis, dans UN SAVEPOINT englobant :
                                   résolution terminale éventuelle
                                   (finalization._resolve, instant réel
                                   PostgreSQL) et DELETE conditionnel de la
                                   seule preuve ;
   observe_campaign_proof_purge    relecture sans écriture.

   Résolutions admises (jamais choisies par l'appelant, jamais antidatées) :
   pending dont l'échéance de finalisation du bloc C est atteinte ->
   indeterminate / finalization_expired ; pending d'une campagne
   DÉFINITIVEMENT terminale dont cette échéance n'est pas atteinte
   (clôture anticipée, correction B1, propre à ce parcours) -> rejected /
   not_authorized ; certified -> motif de finalization._certification_problem
   (ordre du bloc C), seulement rejected / not_authorized ou indeterminate /
   finalization_expired (tout autre motif révèle une donnée stockée hors
   contrat : conflict, aucune mutation) ; recorded, rejected, indeterminate :
   jamais résolues de nouveau, supprimées sans UPDATE ; seule l'attente des
   168 heures est neutralisée par l'échéance parente. Aucun événement, aucune
   attribution n'est créé, modifié ni supprimé (chemin exclusivement
   destructif : aucun appel du chemin d'écriture d'événement du bloc C).

C. Purge définitive d'une campagne (une campagne par transaction) :

   select_purgeable_campaign_candidates  closed / cancelled dont l'échéance
                                   commune est POTENTIELLEMENT atteinte, et
                                   campagnes hors contrat ; ordre id ;
   purge_campaign                  campagne FOR UPDATE, statut terminal,
                                   échéance, dépendances physiques (EXISTS :
                                   preuves, attributions, participations,
                                   visites, interactions — jamais
                                   verrouillées ni résolues) et finalités
                                   logiques de TOUS les événements (lecture
                                   progressive, classification R5 bis
                                   partagée : retention.campaign_reference),
                                   cohorte puis places FOR UPDATE (ordre id),
                                   validation des invariants universels,
                                   relecture finale, horloge, puis DELETE
                                   places -> cohorte -> campagne dans UN
                                   SAVEPOINT, nombres de lignes exacts ;
   observe_campaign_purge          relecture conservatrice sans écriture.

   Budget de sécurité : 10 secondes cumulées (horloge MONOTONE, jamais une
   décision métier) depuis le début du traitement, contrôlé avant, pendant et
   après chaque lecture globale et avant toute suppression, jamais
   réinitialisé : dépassement -> unknown / scan_budget_exceeded, aucune
   mutation. Une instruction SQL en cours peut retarder le contrôle suivant :
   ce budget n'est pas une durée maximale absolue de la transaction.

Sélections : lecture seule, sans verrou ni écriture, jamais une
autorisation ; taille de page entière exacte 1..200 (défaut 50) ; curseurs
stricts issus de la SEULE photographie de sélection, de types distincts et
incompatibles ; jamais OFFSET.
"""
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import and_, delete, exists, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from core.models import (
    ProductActivityEvent,
    ProductActivityEventAttribution,
    ProductActivityProof,
    ProductActivitySession,
    ProductActivitySessionInteraction,
    ProductCampaign,
    ProductCampaignCohort,
    ProductCampaignParticipant,
    ProductCohortSeat,
)
from core.product_analytics import common, event_lifecycle, finalization, proofs, retention
from core.product_analytics import participation_lifecycle as pl
from core.product_analytics.common import (
    ACTIVE,
    CANCELLED,
    CLOSED,
    COHORT_POLICIES,
    NOTICE_VERSION_PATTERN,
    PLANNED,
    TERMINAL_CAMPAIGN_STATUSES,
    InvalidProductAnalyticsInput,
)

# --------------------------------------------------------------------------
# Vocabulaires fermés et bornes opérationnelles
# --------------------------------------------------------------------------

# Limites opérationnelles proposées (pas des décisions produit).
DEFAULT_PAGE_SIZE = pl.DEFAULT_PAGE_SIZE
MAX_PAGE_SIZE = pl.MAX_PAGE_SIZE

CAMPAIGN_TERMINATION = "campaign_termination"
CAMPAIGN_PROOF_PURGE = "campaign_proof_purge"
CAMPAIGN_PURGE = "campaign_purge"
SELECTIONS = (CAMPAIGN_TERMINATION, CAMPAIGN_PROOF_PURGE, CAMPAIGN_PURGE)

# Issues communes.
ABSENT = "absent"
NOT_DUE = "not_due"
NOT_ADMISSIBLE = "not_admissible"
CONFLICT = "conflict"
UNKNOWN = "unknown"
# Parcours A (closed / cancelled : terminaison écrite par cet appel, flushée).
CLOSED_OUTCOME, CANCELLED_OUTCOME = CLOSED, CANCELLED
ALREADY_TERMINAL = "already_terminal"
TERMINATION_OUTCOMES = (CLOSED_OUTCOME, CANCELLED_OUTCOME, ALREADY_TERMINAL, NOT_DUE, ABSENT, CONFLICT, UNKNOWN)
# Parcours B.
PURGED = "purged"  # preuve déjà terminale supprimée par cet appel, écriture flushée
RESOLVED_AND_PURGED = "resolved_and_purged"  # preuve résolue puis supprimée dans le même SAVEPOINT
PROOF_PURGE_OUTCOMES = (PURGED, RESOLVED_AND_PURGED, NOT_ADMISSIBLE, NOT_DUE, ABSENT, CONFLICT, UNKNOWN)
# Parcours C.
BLOCKED = "blocked"  # dépendance encore présente : constatée, jamais résolue
CAMPAIGN_PURGE_OUTCOMES = (PURGED, BLOCKED, NOT_ADMISSIBLE, NOT_DUE, ABSENT, CONFLICT, UNKNOWN)
# Relectures.
PRESENT = "present"
UNSTABLE = "unstable"

# Motifs fermés.
DEADLINE_UNESTABLISHED = pl.DEADLINE_UNESTABLISHED  # campagne planned : aucune échéance établie
CAMPAIGN_NOT_TERMINAL = "campaign_not_terminal"  # purge définitive d'une campagne planned / active
PROOF_REMAINING = pl.PROOF_REMAINING
ATTRIBUTION_REMAINING = pl.ATTRIBUTION_REMAINING
PARTICIPATION_REMAINING = "participation_remaining"
SESSION_REMAINING = "session_remaining"
INTERACTION_REMAINING = "interaction_remaining"
EVENT_SCOPE_REMAINING = pl.EVENT_SCOPE_REMAINING
RETENTION_INCOHERENT = pl.RETENTION_INCOHERENT  # statut / dates de campagne, ou document de finalités
POLICY_INCOHERENT = pl.POLICY_INCOHERENT
COHORT_INCOHERENT = "cohort_incoherent"
SEAT_INCOHERENT = "seat_incoherent"
PARTICIPATION_INCOHERENT = pl.PARTICIPATION_INCOHERENT
SESSION_INCOHERENT = pl.SESSION_INCOHERENT
PROOF_INCOHERENT = "proof_incoherent"  # forme, chronologie ou rattachements de la preuve hors contrat
RECORD_INCOHERENT = finalization.RECORD_INCOHERENT  # événement / attribution d'une preuve recorded hors contrat
RESOLUTION_UNESTABLISHED = "resolution_unestablished"  # aucune résolution terminale conforme établie
IDENTITY_CHANGED = pl.IDENTITY_CHANGED
DEPENDENCY_CHANGED = pl.DEPENDENCY_CHANGED
CLOCK_INCOHERENT = pl.CLOCK_INCOHERENT
WRITE_UNCONFIRMED = pl.WRITE_UNCONFIRMED
SCAN_BUDGET_EXCEEDED = "scan_budget_exceeded"
REASONS = (DEADLINE_UNESTABLISHED, CAMPAIGN_NOT_TERMINAL, PROOF_REMAINING, ATTRIBUTION_REMAINING,
           PARTICIPATION_REMAINING, SESSION_REMAINING, INTERACTION_REMAINING, EVENT_SCOPE_REMAINING,
           RETENTION_INCOHERENT, POLICY_INCOHERENT, COHORT_INCOHERENT, SEAT_INCOHERENT, PARTICIPATION_INCOHERENT,
           SESSION_INCOHERENT, PROOF_INCOHERENT, RECORD_INCOHERENT, RESOLUTION_UNESTABLISHED, IDENTITY_CHANGED,
           DEPENDENCY_CHANGED, CLOCK_INCOHERENT, WRITE_UNCONFIRMED, SCAN_BUDGET_EXCEEDED)
BLOCKING_REASONS = (PROOF_REMAINING, ATTRIBUTION_REMAINING, PARTICIPATION_REMAINING, SESSION_REMAINING,
                    INTERACTION_REMAINING, EVENT_SCOPE_REMAINING)

# Résolutions terminales que la purge exceptionnelle peut écrire (aucune autre).
ADMITTED_RESOLUTIONS = ((proofs.REJECTED, proofs.NOT_AUTHORIZED), (proofs.INDETERMINATE, proofs.FINALIZATION_EXPIRED))

# Budget de sécurité du contrôle global (secondes, horloge monotone).
GLOBAL_SCAN_BUDGET_SECONDS = 10.0
EVENT_SCAN_BATCH = pl.EVENT_SCAN_BATCH
_monotonic = time.monotonic  # protection opérationnelle seulement ; remplaçable en test

# Preuve F2 canonique du rattachement d'une place (CHECK de 0023).
_BINDING_KIND, _BINDING_STATUS = "invitation_activation", "committed"
_STRUCTURE_ERRORS = pl._STRUCTURE_ERRORS


# --------------------------------------------------------------------------
# Contrats internes (aucun identifiant ni contenu dans les représentations)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, repr=False)
class TerminationCursor:
    """Dernière campagne rendue par la sélection A (photographie de la
    sélection). Interne, jamais journalisé."""
    position: datetime
    campaign_id: uuid.UUID

    def __repr__(self) -> str:
        return "TerminationCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CampaignProofCursor:
    """Dernière preuve rendue par la sélection B. Interne, jamais journalisé."""
    campaign_id: uuid.UUID
    proof_id: uuid.UUID

    def __repr__(self) -> str:
        return "CampaignProofCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CampaignPurgeCursor:
    """Dernière campagne rendue par la sélection C. Interne, jamais journalisé."""
    campaign_id: uuid.UUID

    def __repr__(self) -> str:
        return "CampaignPurgeCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CampaignPage:
    """Page A ou C : identifiants internes de campagne seulement ; next_cursor
    None quand la sélection est épuisée (page incomplète)."""
    selection: str
    campaign_ids: tuple
    next_cursor: object

    def __repr__(self) -> str:
        return f"CampaignPage({self.selection}, {len(self.campaign_ids)}, more={self.next_cursor is not None})"


@dataclass(frozen=True, slots=True, repr=False)
class CampaignProofPage:
    """Page B : identifiants internes de preuve seulement."""
    selection: str
    proof_ids: tuple
    next_cursor: CampaignProofCursor | None

    def __repr__(self) -> str:
        return f"CampaignProofPage({self.selection}, {len(self.proof_ids)}, more={self.next_cursor is not None})"


@dataclass(frozen=True, slots=True, repr=False)
class _TerminationAttempt:
    """Photographie COMPLÈTE de la campagne avant et après l'UPDATE (toutes
    ses colonnes). Jamais journalisée ni rendue à l'appelant final."""
    campaign_id: uuid.UUID
    before: tuple
    after: tuple

    def __repr__(self) -> str:
        return "_TerminationAttempt(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _ProofAttempt:
    """Clés consultatives ORIGINALES (union triée) et identité canonique de la
    preuve tentée (propriétaire, nature, opération)."""
    proof_id: uuid.UUID
    keys: tuple
    canonical: tuple

    def __repr__(self) -> str:
        return "_ProofAttempt(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _CampaignAttempt:
    """Identité historique de la campagne supprimée (aucune photographie des
    descendants)."""
    campaign_id: uuid.UUID

    def __repr__(self) -> str:
        return "_CampaignAttempt(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class TerminationDecision:
    """Issue du parcours A. outcome : TERMINATION_OUTCOMES ; reason : REASONS
    ou None ; written : cet appel a écrit (flush, NON durable avant le COMMIT
    de l'appelant) ; attempt : contexte privé de l'écriture tentée."""
    outcome: str
    reason: str | None = None
    written: bool = False
    attempt: _TerminationAttempt | None = None

    def __repr__(self) -> str:
        return f"TerminationDecision({self.outcome}, {self.reason}, written={self.written})"


@dataclass(frozen=True, slots=True, repr=False)
class ProofPurgeDecision:
    """Issue du parcours B. resolution : (état, motif) écrits avant la
    suppression dans le même SAVEPOINT (resolved_and_purged seulement)."""
    outcome: str
    reason: str | None = None
    written: bool = False
    resolution: tuple | None = None
    attempt: _ProofAttempt | None = None

    def __repr__(self) -> str:
        return f"ProofPurgeDecision({self.outcome}, {self.reason}, written={self.written})"


@dataclass(frozen=True, slots=True, repr=False)
class CampaignPurgeDecision:
    """Issue du parcours C. seats / cohorts : lignes supprimées (purged
    seulement, NON durables avant le COMMIT) ; scanned : documents de
    finalités examinés par les lectures globales (mesure, jamais une
    autorisation)."""
    outcome: str
    reason: str | None = None
    written: bool = False
    seats: int = 0
    cohorts: int = 0
    scanned: int = 0
    attempt: _CampaignAttempt | None = None

    def __repr__(self) -> str:
        return f"CampaignPurgeDecision({self.outcome}, {self.reason}, written={self.written})"


@dataclass(frozen=True, slots=True, repr=False)
class Observation:
    """Relecture sans écriture : PRESENT / ABSENT / UNSTABLE (UNKNOWN :
    lecture globale interrompue) ; state : photographie privée (terminaison,
    PRESENT seulement)."""
    outcome: str
    reason: str | None = None
    state: tuple | None = None

    def __repr__(self) -> str:
        return f"Observation({self.outcome}, {self.reason})"


# --------------------------------------------------------------------------
# Validation des entrées (avant tout accès à la base)
# --------------------------------------------------------------------------

def _require_limit(limit) -> int:
    return pl._require_limit(limit)


def _require_uuid(value, field: str) -> uuid.UUID:
    return pl._require_uuid(value, field)


def _require_cursor(after, cursor_type):
    """Curseur du SEUL parcours demandé (types exacts, incompatibles)."""
    if after is None:
        return None
    if type(after) is not cursor_type:
        raise InvalidProductAnalyticsInput(f"after : {cursor_type.__name__} requis")
    _require_uuid(after.campaign_id, "campaign_id")
    if cursor_type is TerminationCursor:
        if not isinstance(after.position, datetime) or after.position.utcoffset() is None:
            raise InvalidProductAnalyticsInput("position : datetime avec fuseau horaire requise")
    elif cursor_type is CampaignProofCursor:
        _require_uuid(after.proof_id, "proof_id")
    return after


# --------------------------------------------------------------------------
# Lectures, photographies et prédicats conditionnels
# --------------------------------------------------------------------------

def _campaign(db, campaign_id, *, mode=None):
    return pl._campaign(db, campaign_id, mode=mode)


def _one(db, model, *criteria, mode=None):
    return db.execute(pl._lock_mode(select(model).where(*criteria), mode)).scalar_one_or_none()


def _row(row) -> tuple | None:
    return pl._row(row)


def _same_row(table, snapshot: tuple) -> tuple:
    """Prédicat conditionnel : la ligne EXACTEMENT telle que photographiée."""
    return tuple(getattr(table, name) == value if value is not None else getattr(table, name).is_(None)
                 for name, value in snapshot)


def _values(snapshot: tuple | None):
    return None if snapshot is None else SimpleNamespace(**dict(snapshot))


class _Unconfirmed(Exception):
    """Levée DANS le SAVEPOINT : une écriture n'a pas touché le nombre de
    lignes établi sous verrou, ou l'état écrit diffère de l'état attendu."""


class _ClockRegressed(Exception):
    """Levée DANS le SAVEPOINT : horloge finale antérieure à la décision."""


def _affected(db, statement, expected: int) -> None:
    if db.execute(statement.execution_options(synchronize_session=False)).rowcount != expected:
        raise _Unconfirmed()


def _clock_problem(now: datetime, instants) -> bool:
    return pl._clock_problem(now, tuple(instant for instant in instants if instant is not None))


def _campaign_instants(campaign) -> tuple:
    return tuple(i for i in (campaign.created_at, campaign.activated_at, campaign.closed_at) if i is not None)


class _Budget:
    """Budget de sécurité MONOTONE d'une transaction de traitement : fixé une
    fois, jamais réinitialisé ; ne détermine aucune décision métier."""
    __slots__ = ("_limit",)

    def __init__(self):
        self._limit = _monotonic() + GLOBAL_SCAN_BUDGET_SECONDS

    def exceeded(self) -> bool:
        return _monotonic() >= self._limit


# --------------------------------------------------------------------------
# Sélections bornées (lecture seule, sans verrou)
# --------------------------------------------------------------------------

def _termination_query(now: datetime, limit: int, after: TerminationCursor | None):
    c = ProductCampaign
    statement = select(c.close_by_at, c.id).where(
        or_(and_(c.status.in_((PLANNED, ACTIVE)), c.close_by_at <= now), pl._campaign_anomaly()))
    if after is not None:
        statement = statement.where(tuple_(c.close_by_at, c.id) > tuple_(after.position, after.campaign_id))
    return statement.order_by(c.close_by_at, c.id).limit(limit)


def select_due_campaign_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                   after: TerminationCursor | None = None) -> CampaignPage:
    """Campagnes planned / active dont close_by_at est POTENTIELLEMENT
    atteinte, et campagnes hors contrat (indicatif : relues sous verrou)."""
    limit = _require_limit(limit)
    after = _require_cursor(after, TerminationCursor)
    now = proofs.server_clock(db)
    found = db.execute(_termination_query(now, limit, after)).all()
    next_cursor = TerminationCursor(found[-1][0], found[-1][1]) if len(found) == limit else None
    return CampaignPage(CAMPAIGN_TERMINATION, tuple(row[1] for row in found), next_cursor)


def _proof_query(now: datetime, limit: int, after: CampaignProofCursor | None):
    """Jointure registre -> campagne ; index ix_product_activity_proof_registry_campaign_status
    (campaign_id en tête) pour l'ordre (campaign_id, id)."""
    p, c = ProductActivityProof, ProductCampaign
    statement = (select(p.campaign_id, p.id).join(c, c.id == p.campaign_id)
                 .where(or_(and_(c.status != PLANNED, pl._potential_deadline() <= now), pl._campaign_anomaly())))
    if after is not None:
        statement = statement.where(tuple_(p.campaign_id, p.id) > tuple_(after.campaign_id, after.proof_id))
    return statement.order_by(p.campaign_id, p.id).limit(limit)


def select_campaign_expired_proof_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                             after: CampaignProofCursor | None = None) -> CampaignProofPage:
    """Preuves (tous états) dont la campagne est POTENTIELLEMENT échue, ou
    hors contrat (indicatif : chaque candidat est relu sous verrou)."""
    limit = _require_limit(limit)
    after = _require_cursor(after, CampaignProofCursor)
    now = proofs.server_clock(db)
    found = db.execute(_proof_query(now, limit, after)).all()
    next_cursor = CampaignProofCursor(found[-1][0], found[-1][1]) if len(found) == limit else None
    return CampaignProofPage(CAMPAIGN_PROOF_PURGE, tuple(row[1] for row in found), next_cursor)


def _purge_query(now: datetime, limit: int, after: CampaignPurgeCursor | None):
    c = ProductCampaign
    statement = select(c.id).where(or_(and_(c.status.in_(TERMINAL_CAMPAIGN_STATUSES), pl._potential_deadline() <= now),
                                       pl._campaign_anomaly()))
    if after is not None:
        statement = statement.where(c.id > after.campaign_id)
    return statement.order_by(c.id).limit(limit)


def select_purgeable_campaign_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                         after: CampaignPurgeCursor | None = None) -> CampaignPage:
    """Campagnes closed / cancelled POTENTIELLEMENT échues, et campagnes hors
    contrat (indicatif : chaque candidat est relu sous verrou)."""
    limit = _require_limit(limit)
    after = _require_cursor(after, CampaignPurgeCursor)
    now = proofs.server_clock(db)
    found = db.execute(_purge_query(now, limit, after)).scalars().all()
    next_cursor = CampaignPurgeCursor(found[-1]) if len(found) == limit else None
    return CampaignPage(CAMPAIGN_PURGE, tuple(found), next_cursor)


# --------------------------------------------------------------------------
# Parcours A : terminaison d'une campagne échue
# --------------------------------------------------------------------------

def terminate_due_campaign(db, *, campaign_id: uuid.UUID) -> TerminationDecision:
    """planned -> cancelled, active -> closed, closed_at = close_by_at, à
    partir de close_by_at (égalité comprise). Ordre : 1. campagne FOR UPDATE ;
    2. relecture ; 3. politique, dates, statut ; 4. horloge ; 5. close_by_at ;
    6. UPDATE conditionnel dans UN SAVEPOINT. Aucun autre verrou."""
    _require_uuid(campaign_id, "campaign_id")
    campaign = _campaign(db, campaign_id, mode="update")  # 1.
    if campaign is None:
        return TerminationDecision(ABSENT)
    before = _row(campaign)  # 2. photographie complète, en valeurs
    _, reason = pl._deadline_or_reason(campaign)  # 3. politique, statut, chronologie (source unique de la PR 3)
    if reason is not None and reason != DEADLINE_UNESTABLISHED:
        return TerminationDecision(CONFLICT, reason)
    now = proofs.server_clock(db)  # 4. APRÈS le verrou exclusif
    if _clock_problem(now, _campaign_instants(campaign)):
        return TerminationDecision(UNKNOWN, CLOCK_INCOHERENT)
    if campaign.status in TERMINAL_CAMPAIGN_STATUSES:
        return TerminationDecision(ALREADY_TERMINAL)  # closed_at jamais modifié
    if now < campaign.close_by_at:  # 5.
        return TerminationDecision(NOT_DUE)
    target = CANCELLED if campaign.status == PLANNED else CLOSED
    table = ProductCampaign
    statement = (update(table).where(*_same_row(table, before), table.status.in_((PLANNED, ACTIVE)),
                                     table.close_by_at <= now)
                 .values(status=target, closed_at=campaign.close_by_at))
    try:
        with db.begin_nested():  # 6.
            _affected(db, statement, 1)
    except _Unconfirmed:
        return TerminationDecision(CONFLICT, WRITE_UNCONFIRMED)
    db.expunge(campaign)
    written = {"status": target, "closed_at": campaign.close_by_at}
    after = tuple((name, written.get(name, value)) for name, value in before)
    return TerminationDecision(target, None, True, _TerminationAttempt(campaign_id, before, after))


def _termination_observation(db, campaign_id) -> Observation:
    """Relecture STABLE : sans verrou (L1) puis EN PARTAGE (L2, une
    terminaison encore en cours est attendue) ; L1 = L2 exigé."""
    first = _row(_campaign(db, campaign_id))
    second = _row(_campaign(db, campaign_id, mode="share"))
    if first is None and second is None:
        return Observation(ABSENT)
    if first != second:
        return Observation(UNSTABLE)
    return Observation(PRESENT, None, second)


def observe_campaign_termination(db, *, campaign_id: uuid.UUID) -> Observation:
    """État d'UNE campagne, SANS écriture."""
    _require_uuid(campaign_id, "campaign_id")
    return _termination_observation(db, campaign_id)


def _observe_termination_attempt(db, *, attempt: _TerminationAttempt) -> Observation:
    """Relecture d'un COMMIT incertain de terminaison."""
    if type(attempt) is not _TerminationAttempt:
        raise InvalidProductAnalyticsInput("attempt : contexte de terminaison requis")
    return _termination_observation(db, attempt.campaign_id)


# --------------------------------------------------------------------------
# Parcours B : purge exceptionnelle d'une preuve à l'échéance de sa campagne
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _ProofState:
    """Photographie EN VALEURS (toutes les colonnes) de la preuve et, si elle
    est recorded, de l'attribution et de l'événement qu'elle référence."""
    proof: tuple
    attribution: tuple | None
    event: tuple | None


def _proof(db, proof_id, *, mode=None):
    return _one(db, ProductActivityProof, ProductActivityProof.id == proof_id, mode=mode)


def _attribution(db, event_id, *, mode=None):
    return _one(db, ProductActivityEventAttribution, ProductActivityEventAttribution.event_id == event_id, mode=mode)


def _event(db, event_id, *, mode=None):
    return _one(db, ProductActivityEvent, ProductActivityEvent.id == event_id, mode=mode)


def _interaction(db, proof, *, mode=None):
    i = ProductActivitySessionInteraction
    return _one(db, i, i.campaign_id == proof.campaign_id, i.user_id == proof.user_id,
                i.interaction_key == proof.interaction_key, mode=mode)


def _proof_state(db, proof_id) -> _ProofState | None:
    """L0 / L1 : lectures SANS verrou, photographiées aussitôt en valeurs."""
    proof = _row(_proof(db, proof_id))
    if proof is None:
        return None
    event_id = dict(proof)["recorded_event_id"]
    if event_id is None:
        return _ProofState(proof, None, None)
    return _ProofState(proof, _row(_attribution(db, event_id)), _row(_event(db, event_id)))


def _required_keys(state: _ProofState) -> frozenset:
    """Union des clés consultatives : celles du bloc C pour la preuve
    (finalization.lock_keys : identité de la preuve, clé d'idempotence et
    opérations de l'événement) et, si un événement est référencé, celles de la
    PR 2 pour cet événement (event_lifecycle._required_keys)."""
    proof = _values(state.proof)
    keys = set(finalization.lock_keys(proof))
    if state.event is not None:
        keys |= event_lifecycle._required_keys(_values(state.event), _values(state.attribution), [proof])
    elif state.attribution is not None:
        keys.add(event_lifecycle._activity_proof_key(_values(state.attribution)))
    return frozenset(keys)


def _proof_identity(snapshot: tuple) -> tuple:
    return finalization._identity(_values(snapshot))


def _shape_problem(proof) -> str | None:
    """Forme d'état de la preuve (CHECK de 0024 repris en défense, motifs
    répartis par état terminal) et nature attestée (proofs.OPERATION_RULES)."""
    rule = proofs.OPERATION_RULES.get(proof.source_operation_kind) if type(
        proof.source_operation_kind) is str else None
    if rule is None or (proof.event_type, proof.surface) != rule or proof.interaction_kind != proofs.ACTIVITY:
        return PROOF_INCOHERENT
    status, reason = proof.status, proof.resolution_reason
    resolved = (proof.resolved_at, proof.purge_after_at)
    if status == proofs.PENDING:
        ok = proof.certified_at is None and resolved == (None, None) and proof.recorded_event_id is None \
            and reason is None
    elif status == proofs.CERTIFIED:
        ok = proof.certified_at is not None and resolved == (None, None) and proof.recorded_event_id is None \
            and reason is None
    elif status == proofs.RECORDED:
        ok = proof.certified_at is not None and None not in resolved and proof.recorded_event_id is not None \
            and reason is None
    elif status in (proofs.REJECTED, proofs.INDETERMINATE):
        allowed = proofs.REJECTION_REASONS if status == proofs.REJECTED else proofs.INDETERMINATE_REASONS
        ok = None not in resolved and proof.recorded_event_id is None and reason in allowed
    else:
        return PROOF_INCOHERENT
    if not ok:
        return PROOF_INCOHERENT
    if proof.certified_at is not None and not proof.created_at <= proof.certified_at:
        return PROOF_INCOHERENT
    if proof.resolved_at is not None and not (
            proof.created_at <= proof.resolved_at
            and (proof.certified_at is None or proof.certified_at <= proof.resolved_at)
            and proof.purge_after_at == proof.resolved_at + proofs.REGISTRY_RETENTION):
        return PROOF_INCOHERENT  # jamais une échéance individuelle recalculée
    return None


def _origin_problem(proof, interaction, visit, participation, campaign) -> str | None:
    """Rattachements et chronologie UNIVERSELS d'une preuve émise par le
    bloc B (émission de l'origine, inscription et certification), quel que
    soit son état : interaction 'activity' de CETTE visite et de CET
    environnement, acceptée pendant l'observation historique, au plus tard à
    l'inscription ; inscription et certification strictement avant
    window_end_at et close_by_at."""
    if interaction is None or visit is None:
        return PROOF_INCOHERENT
    if ((visit.id, visit.campaign_id, visit.user_id) != (proof.visit_id, proof.campaign_id, proof.user_id)
            or interaction.interaction_kind != proofs.ACTIVITY or interaction.session_id != proof.visit_id
            or interaction.environment_digest != visit.environment_digest):
        return PROOF_INCOHERENT
    if not visit.opened_at <= interaction.recorded_at <= visit.last_activity_at:
        return PROOF_INCOHERENT
    if not finalization._accepted_during_observation(interaction.recorded_at, participation, campaign):
        return PROOF_INCOHERENT
    bound = min(participation.window_end_at, campaign.close_by_at)
    if not interaction.recorded_at <= proof.created_at < bound:
        return PROOF_INCOHERENT
    if proof.certified_at is not None and not proof.certified_at < bound:
        return PROOF_INCOHERENT
    return None


def _semantic_problem(proof, participation, campaign) -> str | None:
    """Correctif R1 (B2) : chronologie SÉMANTIQUE démontrable des motifs
    stockés, comparée aux bornes immuables avec la même horloge que la
    décision d'origine (PostgreSQL contre les bornes contractuelles) :

    - indeterminate / finalization_expired : écrit seulement à partir de
      finalization.finalization_deadline (égalité comprise : bloc C,
      _no_longer_admissible, résolution historique des pending et B1 de ce
      parcours) ;
    - rejected / window_expired : écrit seulement à partir de
      min(window_end_at, close_by_at) (certification du bloc B) ou pour une
      certification déjà au-delà de cette borne (bloc C, refusée plus haut) ;
    - recorded : finalisé strictement avant l'échéance de finalisation.

    Jamais reconstitué : un motif dépendant d'un ancien état d'autorisation
    (not_authorized), d'une relecture technique (technical_uncertainty) ou de
    l'origine (origin_unverified, visit_conflict...)."""
    if proof.resolved_at is None:
        return None
    deadline = finalization.finalization_deadline(participation, campaign)
    reason = proof.resolution_reason
    if proof.status == proofs.INDETERMINATE and reason == proofs.FINALIZATION_EXPIRED and proof.resolved_at < deadline:
        return PROOF_INCOHERENT
    if (proof.status == proofs.REJECTED and reason == proofs.WINDOW_EXPIRED
            and proof.resolved_at < min(participation.window_end_at, campaign.close_by_at)):
        return PROOF_INCOHERENT
    if proof.status == proofs.RECORDED and proof.resolved_at >= deadline:
        return PROOF_INCOHERENT
    return None


def _closure_instants(proof) -> tuple:
    """Correctif R1 (B2) : instants de la preuve qui exigent une campagne ACTIVE
    au moment de leur écriture (inscription, certification, finalisation
    recorded : verrous partagés de la campagne, sérialisés avec sa clôture) et
    doivent donc précéder closed_at. Jamais resolved_at d'une preuve rejected
    ou indeterminate, légitimement postérieure à la clôture."""
    instants = [proof.created_at, proof.certified_at]
    if proof.status == proofs.RECORDED:
        instants.append(proof.resolved_at)
    return tuple(i for i in instants if i is not None)


def _recorded_problem(db, proof, attribution, event, interaction, participation, campaign) -> str | None:
    """Correction B3 : contrôle canonique COMPLET de la PR 2
    (event_lifecycle._contract_problem, donc _certified_problem et
    _canonical_problem), indépendant de l'expiration de l'événement, après
    vérification explicite du chemin certifié, de l'unique finalité et de
    l'identité des rattachements."""
    if event is None or attribution is None or event_lifecycle._path(event) != event_lifecycle.CERTIFIED:
        return RECORD_INCOHERENT
    try:
        scopes = retention.parse_retention_scopes(event.retention_scopes)
    except _STRUCTURE_ERRORS:
        return RECORD_INCOHERENT
    if len(scopes) != 1 or scopes[0].campaign_id != proof.campaign_id:
        return RECORD_INCOHERENT
    if ((proof.id, proof.recorded_event_id) != (event.idempotency_key, event.id) or attribution.event_id != event.id
            or tuple(getattr(attribution, f) for f in finalization.IDENTITY_FIELDS)
            != tuple(getattr(proof, f) for f in finalization.IDENTITY_FIELDS)):
        return RECORD_INCOHERENT
    locked = event_lifecycle._Locked(event, attribution, proof, interaction, (),
                                     {proof.campaign_id: (participation, campaign)})
    try:
        problem = event_lifecycle._contract_problem(db, locked)
    except _STRUCTURE_ERRORS:
        return RECORD_INCOHERENT
    return RECORD_INCOHERENT if problem is not None else None


def _proof_instants(proof, interaction, attribution, event) -> tuple:
    instants = [interaction.recorded_at, proof.created_at, proof.certified_at, proof.resolved_at]
    if attribution is not None:
        instants.append(attribution.certified_at)
    if event is not None:
        instants += [event.occurred_at, event.recorded_at, event.updated_at]
    return tuple(i for i in instants if i is not None)


def _resolution(ctx_args, proof, participation, campaign, now):
    """(état, motif) de résolution terminale admissible, ou (None, motif de
    refus). Jamais inventée : B1 pour pending, ordre du bloc C pour certified."""
    if proof.status == proofs.PENDING:
        if now >= finalization.finalization_deadline(participation, campaign):
            return (proofs.INDETERMINATE, proofs.FINALIZATION_EXPIRED), None
        if campaign.status in TERMINAL_CAMPAIGN_STATUSES:
            return (proofs.REJECTED, proofs.NOT_AUTHORIZED), None  # B1 : clôture anticipée, parcours exceptionnel
        return None, RESOLUTION_UNESTABLISHED
    ctx = finalization._Locked(*ctx_args, None, None, None, now)
    problem = finalization._certification_problem(ctx)  # ordre réel des motifs du bloc C
    if problem is None:
        return None, RESOLUTION_UNESTABLISHED
    if problem not in ADMITTED_RESOLUTIONS:
        return None, PROOF_INCOHERENT  # refus du bloc C révélant une donnée stockée hors contrat
    return problem, None


def _db_row(db, table, proof_id) -> tuple | None:
    """Relecture SQL directe (jamais un état ORM) de toutes les colonnes."""
    columns = list(table.__table__.columns)
    found = db.execute(select(*columns).where(table.id == proof_id)).first()
    return None if found is None else tuple((c.key, found._mapping[c]) for c in columns)


def _delete_proof(db, proof, target, now: datetime, attempt: _ProofAttempt) -> ProofPurgeDecision:
    """Dans UN SAVEPOINT englobant : résolution éventuelle (SAVEPOINT interne
    de finalization._resolve), vérification de l'état écrit, DELETE
    conditionnel d'exactement une ligne, horloge finale. Tout échec annule la
    résolution ET la suppression."""
    table = ProductActivityProof
    snapshot = _row(proof)
    try:
        with db.begin_nested():
            expected = snapshot
            if target is not None:
                status, reason = target
                finalization._resolve(db, proof, status, reason, now)  # instant réel, certified_at conservé
                changes = {"status": status, "resolution_reason": reason, "resolved_at": now,
                           "purge_after_at": now + proofs.REGISTRY_RETENTION, "recorded_event_id": None}
                expected = tuple((name, changes.get(name, value)) for name, value in snapshot)
                if _db_row(db, table, proof.id) != expected:
                    raise _Unconfirmed()
            _affected(db, delete(table).where(*_same_row(table, expected)), 1)
            if proofs.server_clock(db) < now:
                raise _ClockRegressed()
    except _Unconfirmed:
        return ProofPurgeDecision(CONFLICT, WRITE_UNCONFIRMED)
    except _ClockRegressed:
        return ProofPurgeDecision(UNKNOWN, CLOCK_INCOHERENT)
    except IntegrityError:
        return ProofPurgeDecision(CONFLICT, DEPENDENCY_CHANGED)
    db.expunge(proof)
    return ProofPurgeDecision(RESOLVED_AND_PURGED if target is not None else PURGED, None, True, target, attempt)


def purge_campaign_expired_proof(db, *, proof_id: uuid.UUID) -> ProofPurgeDecision:
    """Purge exceptionnelle d'UNE preuve dont la campagne a atteint
    l'échéance commune de conservation (voir l'en-tête). Ordre : 0. L0
    indicative ; 1-2. union des clés, UN appel trié ; 3. L1 ; 4.
    participation ; 5. campagne ; 6. visite ; 7. interaction (FOR SHARE) ;
    8. preuve FOR UPDATE, comparée aussitôt à L1 (correctif R1) ; 9.
    attribution ; 10. événement (FOR SHARE, références de L1 seulement) ; 11.
    L2 et validations ; 12. horloge ; 13. décision et écritures."""
    _require_uuid(proof_id, "proof_id")
    hint = _proof_state(db, proof_id)  # 0. L0
    if hint is None:
        return ProofPurgeDecision(ABSENT)
    keys = _required_keys(hint)
    common.lock_identities(db, *keys)  # 1-2. aucune clé après ce seul appel
    state = _proof_state(db, proof_id)  # 3. L1
    if state is None:
        return ProofPurgeDecision(ABSENT)
    if _proof_identity(state.proof) != _proof_identity(hint.proof):
        return ProofPurgeDecision(CONFLICT, IDENTITY_CHANGED)
    if not _required_keys(state) <= keys:
        return ProofPurgeDecision(CONFLICT, DEPENDENCY_CHANGED)  # nouvelle clé nécessaire : jamais acquise
    expected = _values(state.proof)
    participation = pl._participation(db, expected.campaign_id, expected.user_id, mode="share")  # 4.
    campaign = _campaign(db, expected.campaign_id, mode="share")  # 5.
    visit = pl._visit(db, expected.visit_id, mode="share")  # 6.
    interaction = _interaction(db, expected, mode="share")  # 7.
    proof = _proof(db, proof_id, mode="update")  # 8.
    # Correctif R1 (B1) : la preuve verrouillée est comparée à L1 AVANT tout verrou
    # de dépendance ; seules les références établies en L1 sont suivies ensuite.
    if proof is None:
        return ProofPurgeDecision(ABSENT)  # disparue sous les verrous
    if _row(proof) != state.proof:
        return ProofPurgeDecision(CONFLICT, DEPENDENCY_CHANGED)  # changée après L1 sans les clés : hors service
    event_id = expected.recorded_event_id  # référence de L1, jamais celle d'une ligne relue
    attribution = _attribution(db, event_id, mode="share") if event_id is not None else None  # 9.
    event = _event(db, event_id, mode="share") if event_id is not None else None  # 10.
    # 11. L2 : relecture finale sous tous les verrous
    if _ProofState(_row(proof), _row(attribution), _row(event)) != state:
        return ProofPurgeDecision(CONFLICT, DEPENDENCY_CHANGED)
    if participation is None or campaign is None:
        return ProofPurgeDecision(CONFLICT, PROOF_INCOHERENT)  # FK de 0024 : impossible sans écrivain hors service
    deadline, reason = pl._deadline_or_reason(campaign)
    if reason == DEADLINE_UNESTABLISHED:
        return ProofPurgeDecision(NOT_ADMISSIBLE, DEADLINE_UNESTABLISHED)
    if reason is not None:
        return ProofPurgeDecision(CONFLICT, reason)
    try:
        problem = (pl._participation_problem(participation, campaign) or _shape_problem(proof)
                   or (pl._session_problem(visit, participation, campaign) if visit is not None else PROOF_INCOHERENT)
                   or _origin_problem(proof, interaction, visit, participation, campaign))
        problem = problem or _semantic_problem(proof, participation, campaign)
        if problem is None and proof.status == proofs.RECORDED:
            problem = _recorded_problem(db, proof, attribution, event, interaction, participation, campaign)
        # Clôture (Python) antérieure à un instant qui exigeait une campagne active
        # (PostgreSQL) : corruption ou divergence d'horloges, indiscernables (L15).
        closed_late = campaign.closed_at is not None and any(
            instant > campaign.closed_at for instant in _closure_instants(proof))
    except _STRUCTURE_ERRORS:
        problem, closed_late = PROOF_INCOHERENT, False
    if problem is not None:
        return ProofPurgeDecision(CONFLICT, problem)
    if closed_late:
        return ProofPurgeDecision(UNKNOWN, CLOCK_INCOHERENT)
    now = proofs.server_clock(db)  # 12. APRÈS tous les verrous
    if _clock_problem(now, pl._stored_instants(campaign, participation, visit)
                      + _proof_instants(proof, interaction, attribution, event)):
        return ProofPurgeDecision(UNKNOWN, CLOCK_INCOHERENT)
    if now < deadline:
        return ProofPurgeDecision(NOT_DUE)
    target = None
    if proof.status in (proofs.PENDING, proofs.CERTIFIED):  # 13.
        target, refusal = _resolution((proof, participation, campaign, visit, interaction), proof, participation,
                                      campaign, now)
        if target is None:
            return ProofPurgeDecision(CONFLICT, refusal)
    attempt = _ProofAttempt(proof_id, tuple(sorted(keys)),
                            (proof.user_id, proof.source_operation_kind, proof.source_operation_id))
    return _delete_proof(db, proof, target, now, attempt)


def _canonical_exists(db, canonical: tuple) -> bool:
    user_id, kind, operation_id = canonical
    p = ProductActivityProof
    return db.execute(select(exists().where(p.user_id == user_id, p.source_operation_kind == kind,
                                            p.source_operation_id == operation_id))).scalar_one()


def _proof_observation(db, proof_id, keys, canonical) -> Observation:
    """Relecture conservatrice sous les clés fournies (une transaction encore
    en cours sur ces identités est attendue) : ABSENT seulement si la preuve
    et toute preuve de même identité canonique sont absentes, deux fois."""
    common.lock_identities(db, *keys)
    for _ in range(2):
        if _proof(db, proof_id) is not None or (canonical is not None and _canonical_exists(db, canonical)):
            return Observation(PRESENT)
    return Observation(ABSENT)


def observe_campaign_proof_purge(db, *, proof_id: uuid.UUID) -> Observation:
    """Présence d'UNE preuve sous les clés de sa lecture indicative, SANS
    écriture."""
    _require_uuid(proof_id, "proof_id")
    hint = _proof_state(db, proof_id)
    if hint is None:
        return Observation(ABSENT)
    proof = _values(hint.proof)
    return _proof_observation(db, proof_id, tuple(sorted(_required_keys(hint))),
                              (proof.user_id, proof.source_operation_kind, proof.source_operation_id))


def _observe_proof_attempt(db, *, attempt: _ProofAttempt) -> Observation:
    """Relecture d'un COMMIT incertain avec les clés ORIGINALES de l'opération
    tentée, même si la preuve a disparu : aucune clé tardive."""
    if type(attempt) is not _ProofAttempt:
        raise InvalidProductAnalyticsInput("attempt : contexte de purge de preuve requis")
    return _proof_observation(db, attempt.proof_id, attempt.keys, attempt.canonical)


# --------------------------------------------------------------------------
# Parcours C : purge définitive d'une campagne
# --------------------------------------------------------------------------

# Ordre de constat : de la dépendance la plus en amont (preuve, PR 1 / PR 4 B)
# à la participation nue (PR 3) ; les FK de 0023 / 0024 rattachant visites,
# interactions, preuves et attributions à la participation, chaque motif reste
# atteignable et désigne le traitement encore nécessaire.
_PHYSICAL = ((ProductActivityProof, PROOF_REMAINING), (ProductActivityEventAttribution, ATTRIBUTION_REMAINING),
             (ProductActivitySession, SESSION_REMAINING), (ProductActivitySessionInteraction, INTERACTION_REMAINING),
             (ProductCampaignParticipant, PARTICIPATION_REMAINING))


def _physical_problem(db, campaign_id) -> str | None:
    """Dépendances physiques CONSTATÉES (EXISTS), jamais verrouillées ni
    résolues ; interactions de clôture sans visite comprises."""
    for model, reason in _PHYSICAL:
        if db.execute(select(exists().where(model.campaign_id == campaign_id))).scalar_one():
            return reason
    return None


def _event_scope_scan(db, campaign_id, budget: _Budget) -> tuple:
    """(motif ou None, documents examinés) : finalités de TOUS les événements,
    quel que soit leur utilisateur, lues progressivement (curseur serveur,
    aucune ligne ORM), classées par retention.campaign_reference (R5 bis,
    exactement celle de la PR 3). RETENTION_INCOHERENT l'emporte sur
    EVENT_SCOPE_REMAINING ; lecture interrompue : SCAN_BUDGET_EXCEEDED."""
    if budget.exceeded():
        return SCAN_BUDGET_EXCEEDED, 0
    statement = (select(ProductActivityEvent.retention_scopes).order_by(ProductActivityEvent.id)
                 .execution_options(yield_per=EVENT_SCAN_BATCH))
    remaining, scanned = False, 0
    with db.execute(statement) as result:  # curseur serveur refermé sur tout retour ou exception
        for document in result.scalars():
            scanned += 1
            reference = retention.campaign_reference(document, campaign_id)
            if reference == retention.REFERENCE_INCOHERENT:
                return RETENTION_INCOHERENT, scanned
            remaining = remaining or reference == retention.REFERENCE_PRESENT
            if budget.exceeded():
                return SCAN_BUDGET_EXCEEDED, scanned
    if budget.exceeded():
        return SCAN_BUDGET_EXCEEDED, scanned
    return (EVENT_SCOPE_REMAINING if remaining else None), scanned


def _dependency_problem(db, campaign_id, budget: _Budget) -> tuple:
    problem = _physical_problem(db, campaign_id)
    if problem is not None:
        return problem, 0
    return _event_scope_scan(db, campaign_id, budget)


def _seat_problem(seat, campaign, cohort) -> bool:
    """Invariants UNIVERSELS d'une place (services de cohort.py et CHECK de
    0023), sans réévaluer l'éligibilité de son invitation ni imposer les
    conditions d'activation : une place créée après close_by_at sur une
    campagne planned, une marque d'échec d'admission tardive, une place non
    rattachée, rattachée sans autorisation ou autorisée sans J0 restent
    admises."""
    if seat.campaign_id != cohort.campaign_id or type(seat.id) is not uuid.UUID or type(
            seat.invitation_id) is not uuid.UUID:
        return True
    if not cohort.created_at <= seat.created_at:
        return True  # ajout sérialisé après la définition (campagne FOR UPDATE)
    if campaign.activated_at is not None and not seat.created_at <= campaign.activated_at:
        return True  # places figées à l'activation
    binding = (seat.binding_operation_id, seat.binding_operation_kind, seat.binding_operation_status,
               seat.binding_committed_at, seat.bound_at)
    if seat.user_id is None:
        if any(value is not None for value in binding) or seat.authorization_recorded_at is not None:
            return True
    else:
        if (None in binding or seat.binding_operation_kind != _BINDING_KIND
                or seat.binding_operation_status != _BINDING_STATUS):
            return True
        if not (seat.binding_committed_at <= seat.bound_at and seat.created_at <= seat.bound_at
                < cohort.admission_closes_at):
            return True  # rattachement refusé à partir de la date limite (même instant serveur)
    if (seat.authorization_recorded_at is None) != (seat.notice_version is None):
        return True
    if seat.authorization_recorded_at is not None:
        if (seat.bound_at is None or type(seat.notice_version) is not str
                or not NOTICE_VERSION_PATTERN.fullmatch(seat.notice_version)
                or not seat.bound_at <= seat.authorization_recorded_at < cohort.admission_closes_at):
            return True
    if seat.admission_failure_first_at is not None and (
            seat.authorization_recorded_at is None
            or not seat.authorization_recorded_at <= seat.admission_failure_first_at):
        return True
    return False


def _cohort_problem(campaign, cohort, seats) -> str | None:
    """Invariants UNIVERSELS de la cohorte et de ses places (jamais
    cohort.cohort_violations, critère d'ACTIVATION) : politique reconnue,
    nombre contractuel, rattachement, chronologie de définition, numérotation
    séquentielle 1..n ; une campagne historiquement activée porte ses
    seat_count places ; une cohorte jamais activée en porte de 0 à seat_count."""
    if cohort is None:
        return SEAT_INCOHERENT if seats else None  # FK de 0023 : impossible sans écrivain hors service
    try:
        version = cohort.cohort_policy_version
        policy = COHORT_POLICIES.get(version) if type(version) is str else None
        if policy is None or cohort.seat_count != policy.seat_count or cohort.campaign_id != campaign.id:
            return COHORT_INCOHERENT
        if not campaign.created_at <= cohort.created_at < cohort.admission_closes_at <= campaign.close_by_at:
            return COHORT_INCOHERENT
        # Correctif R1 (R1) : contrat historique de calendrier de define_cohort (et
        # d'activate_cohort_campaign) : close_by_at couvre la dernière admission, la
        # fenêtre d'observation et la finalisation (common.required_close_by_at, avec
        # la politique de mesure de la campagne ; le registre n'en compte qu'une, que
        # revise_campaign_policies ne peut donc pas remplacer par une autre fenêtre).
        required = common.required_close_by_at(cohort.admission_closes_at, campaign.measurement_policy_version)
        if required is None or campaign.close_by_at < required:
            return COHORT_INCOHERENT
        if campaign.activated_at is not None and not cohort.created_at <= campaign.activated_at:
            return COHORT_INCOHERENT  # définition exigée à l'état planned
        numbers = sorted(seat.seat_number for seat in seats)
        if numbers != list(range(1, len(seats) + 1)) or len(seats) > cohort.seat_count:
            return SEAT_INCOHERENT
        if campaign.activated_at is not None and len(seats) != cohort.seat_count:
            return COHORT_INCOHERENT  # cohorte activée devenue incomplète (aucun service ne retire de place)
        users = [seat.user_id for seat in seats if seat.user_id is not None]
        if len({seat.invitation_id for seat in seats}) != len(seats) or len(set(users)) != len(users):
            return SEAT_INCOHERENT
        if any(_seat_problem(seat, campaign, cohort) for seat in seats):
            return SEAT_INCOHERENT
    except _STRUCTURE_ERRORS:
        return COHORT_INCOHERENT
    return None


def _cohort_instants(cohort, seats) -> tuple:
    instants = [cohort.created_at] if cohort is not None else []
    for seat in seats:
        instants += [seat.created_at, seat.binding_committed_at, seat.bound_at, seat.authorization_recorded_at,
                     seat.admission_failure_first_at]
    return tuple(i for i in instants if i is not None)


def _purge_outcome(problem: str, scanned: int) -> CampaignPurgeDecision:
    if problem in BLOCKING_REASONS:
        return CampaignPurgeDecision(BLOCKED, problem, scanned=scanned)
    if problem == SCAN_BUDGET_EXCEEDED:
        return CampaignPurgeDecision(UNKNOWN, problem, scanned=scanned)
    return CampaignPurgeDecision(CONFLICT, problem, scanned=scanned)


def _delete_campaign(db, campaign, cohort_row, seat_rows, scanned: int) -> CampaignPurgeDecision:
    """Suppression atomique, dans UN SAVEPOINT : places, cohorte, campagne
    (ordre des FK de 0022 / 0023), nombres de lignes EXACTEMENT ceux établis
    sous verrou ; violation de FK : rien n'est supprimé."""
    seats_table, cohorts_table, campaigns_table = ProductCohortSeat, ProductCampaignCohort, ProductCampaign
    campaign_row = _row(campaign)
    try:
        with db.begin_nested():
            _affected(db, delete(seats_table).where(seats_table.campaign_id == campaign.id), len(seat_rows))
            if cohort_row is not None:
                _affected(db, delete(cohorts_table).where(*_same_row(cohorts_table, cohort_row)), 1)
            _affected(db, delete(campaigns_table).where(*_same_row(campaigns_table, campaign_row)), 1)
    except _Unconfirmed:
        return CampaignPurgeDecision(CONFLICT, WRITE_UNCONFIRMED, scanned=scanned)
    except IntegrityError:
        return CampaignPurgeDecision(CONFLICT, DEPENDENCY_CHANGED, scanned=scanned)
    return CampaignPurgeDecision(PURGED, None, True, len(seat_rows), int(cohort_row is not None), scanned,
                                 _CampaignAttempt(campaign.id))


def purge_campaign(db, *, campaign_id: uuid.UUID) -> CampaignPurgeDecision:
    """Suppression définitive d'UNE campagne terminale échue, de sa cohorte et
    de ses places. Ordre : 1. campagne FOR UPDATE ; 2. statut, politiques,
    chronologie ; 3. horloge préliminaire ; 4. échéance ; 5. dépendances
    physiques et logiques (constatées, sans verrou) ; 6. cohorte FOR UPDATE ;
    7. places FOR UPDATE (ordre id) ; 8. validation ; 9. relecture finale ; 10.
    horloge ; 11. échéance et budget ; 12. suppression atomique. Aucun verrou
    de participation, aucune clé consultative (ni de preuve, ni d'événement,
    ni de code de campagne)."""
    _require_uuid(campaign_id, "campaign_id")
    budget = _Budget()  # fixé une fois pour toute la transaction de traitement
    campaign = _campaign(db, campaign_id, mode="update")  # 1.
    if campaign is None:
        return CampaignPurgeDecision(ABSENT)
    before = _row(campaign)
    deadline, reason = pl._deadline_or_reason(campaign)  # 2.
    if reason == DEADLINE_UNESTABLISHED:
        return CampaignPurgeDecision(NOT_ADMISSIBLE, DEADLINE_UNESTABLISHED)
    if reason is not None:
        return CampaignPurgeDecision(CONFLICT, reason)
    if campaign.status not in TERMINAL_CAMPAIGN_STATUSES:
        return CampaignPurgeDecision(NOT_ADMISSIBLE, CAMPAIGN_NOT_TERMINAL)  # terminaison préalable exigée
    preliminary = proofs.server_clock(db)  # 3.
    if _clock_problem(preliminary, _campaign_instants(campaign)):
        return CampaignPurgeDecision(UNKNOWN, CLOCK_INCOHERENT)
    if preliminary < deadline:  # 4.
        return CampaignPurgeDecision(NOT_DUE)
    problem, scanned = _dependency_problem(db, campaign_id, budget)  # 5.
    if problem is not None:
        return _purge_outcome(problem, scanned)
    cohort = _one(db, ProductCampaignCohort, ProductCampaignCohort.campaign_id == campaign_id, mode="update")  # 6.
    seats = db.execute(pl._lock_mode(select(ProductCohortSeat).where(ProductCohortSeat.campaign_id == campaign_id)
                                     .order_by(ProductCohortSeat.id), "update")).scalars().all()  # 7.
    cohort_row, seat_rows = _row(cohort), tuple(_row(seat) for seat in seats)
    problem = _cohort_problem(campaign, cohort, seats)  # 8.
    if problem is not None:
        return CampaignPurgeDecision(CONFLICT, problem, scanned=scanned)
    # 9. relecture finale sous tous les verrous
    if _row(_campaign(db, campaign_id)) != before:
        return CampaignPurgeDecision(CONFLICT, IDENTITY_CHANGED, scanned=scanned)
    if budget.exceeded():
        return CampaignPurgeDecision(UNKNOWN, SCAN_BUDGET_EXCEEDED, scanned=scanned)  # avant la seconde lecture
    problem, rescanned = _dependency_problem(db, campaign_id, budget)
    scanned += rescanned
    if problem == SCAN_BUDGET_EXCEEDED:
        return CampaignPurgeDecision(UNKNOWN, SCAN_BUDGET_EXCEEDED, scanned=scanned)
    if problem is not None:
        return CampaignPurgeDecision(CONFLICT, DEPENDENCY_CHANGED, scanned=scanned)  # apparue après son contrôle
    now = proofs.server_clock(db)  # 10.
    if now < preliminary or _clock_problem(now, _campaign_instants(campaign) + _cohort_instants(cohort, seats)):
        return CampaignPurgeDecision(UNKNOWN, CLOCK_INCOHERENT, scanned=scanned)
    if now < deadline:  # 11.
        return CampaignPurgeDecision(NOT_DUE, scanned=scanned)
    if budget.exceeded():
        return CampaignPurgeDecision(UNKNOWN, SCAN_BUDGET_EXCEEDED, scanned=scanned)
    decision = _delete_campaign(db, campaign, cohort_row, seat_rows, scanned)  # 12.
    if decision.written:
        for row in (campaign, cohort, *seats):
            if row is not None:
                db.expunge(row)
    return decision


def _campaign_observation(db, campaign_id) -> Observation:
    """Relecture CONSERVATRICE sans écriture : identité historique (en
    partage : une purge encore en cours est attendue), cohorte, places,
    dépendances physiques, finalités de tous les événements (budget propre à
    cette transaction), identité historique de nouveau. ABSENT seulement si
    tout est absent ; lecture globale interrompue : UNKNOWN."""
    budget = _Budget()
    if _campaign(db, campaign_id, mode="share") is not None:
        return Observation(PRESENT)
    for model in (ProductCampaignCohort, ProductCohortSeat):
        if db.execute(select(exists().where(model.campaign_id == campaign_id))).scalar_one():
            return Observation(PRESENT)
    problem, _ = _dependency_problem(db, campaign_id, budget)
    if problem == SCAN_BUDGET_EXCEEDED:
        return Observation(UNKNOWN, SCAN_BUDGET_EXCEEDED)
    if problem is not None:
        return Observation(PRESENT, problem)  # finalité résiduelle ou document ambigu : absence non établie
    if _campaign(db, campaign_id) is not None:
        return Observation(PRESENT)  # réapparue pendant l'observation
    if budget.exceeded():
        # Correctif R1 (R2) : budget contrôlé après la DERNIÈRE opération, avant toute
        # conclusion d'absence complète ; une relecture hors budget n'établit rien.
        return Observation(UNKNOWN, SCAN_BUDGET_EXCEEDED)
    return Observation(ABSENT)


def observe_campaign_purge(db, *, campaign_id: uuid.UUID) -> Observation:
    """Présence de la campagne, de ses descendants et de ses dépendances,
    SANS écriture."""
    _require_uuid(campaign_id, "campaign_id")
    return _campaign_observation(db, campaign_id)


def _observe_campaign_attempt(db, *, attempt: _CampaignAttempt) -> Observation:
    """Relecture d'un COMMIT incertain de purge de campagne."""
    if type(attempt) is not _CampaignAttempt:
        raise InvalidProductAnalyticsInput("attempt : contexte de purge de campagne requis")
    return _campaign_observation(db, attempt.campaign_id)
