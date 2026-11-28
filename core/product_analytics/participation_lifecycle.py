"""Cycle de vie des participations, visites et interactions : finalisation
bornée des visites inactives et purge atomique des participations échues
(V0-T08 Lot 2C, PR 3) — DORMANT.

Tables product_campaign_participants (0022), product_activity_sessions et
product_activity_session_interactions (0023), INCHANGÉES. La transaction
appartient toujours à l'appelant (frontière privée
core/_activity_participation_lifecycle.py, sans appelant) : flush
seulement, jamais commit() ni rollback() ; chaque écriture a lieu dans UN
SAVEPOINT.

Trois horloges INDÉPENDANTES (jamais confondues) : preuves terminales,
168 heures absolues depuis resolved_at (lot 2C PR 1) ; événements, 90 jours
absolus depuis occurred_at (PR 2) ; participations, visites et interactions,
90 jours absolus depuis la FIN EFFECTIVE de la campagne (ici). Ni la
finalisation d'une visite, ni la résolution tardive d'une preuve, ni un
retard de traitement ne modifient cette dernière échéance.

1. participation_retention_deadline(campaign) : échéance commune de
   conservation de toutes les participations d'une campagne (et de leurs
   visites et interactions), dérivée de la campagne seule :

     planned    aucune échéance établie (None ; PR 4) ;
     active     close_by_at + retention_days (échéance MAXIMALE : une
                campagne restée active au-delà de close_by_at a pris fin,
                au plus tard, à close_by_at) ;
     closed     min(closed_at, close_by_at) + retention_days ;
     cancelled  min(closed_at, close_by_at) + retention_days, activée ou
                non.

   Fin effective ramenée en UTC, puis timedelta(days=retention_days) :
   arithmétique Python sur des instants absolus (90 jours = 2 160 heures),
   jamais un intervalle SQL en jours civils. Politique, statut et dates
   validés AVANT toute échéance (RetentionDeadlineIncoherent sinon) :
   common.validate_policy ; statut du vocabulaire fermé ; created_at <
   close_by_at ; activated_at / closed_at cohérents avec le statut (closed :
   les deux ; cancelled : closed_at, activated_at facultatif ; active :
   activated_at seul ; planned : aucun) ; created_at <= activated_at <
   close_by_at ; activated_at <= closed_at ; created_at <= closed_at <=
   close_by_at. Aucune date inventée, aucune date absente remplacée par now
   ou close_by_at. L'égalité exacte avec l'échéance rend la conservation
   échue.

2. Parcours A — visites ouvertes inactives (une visite par transaction) :

   select_idle_session_candidates  finalized_at IS NULL et idle_expires_at
                                   <= horloge PostgreSQL ; ordre
                                   (campaign_id, idle_expires_at, id) ;
                                   index partiel existant
                                   ix_product_activity_sessions_open_idle_expiry ;
   finalize_idle_session           sous verrous (clé activity_session de
                                   sessions.py, participation FOR SHARE,
                                   campagne FOR SHARE, visite FOR UPDATE),
                                   horloge lue APRÈS eux : UPDATE
                                   conditionnel de finalized_at (= instant
                                   réel PostgreSQL) et end_reason
                                   ('inactivity') SEULEMENT ;
   observe_session_finalization    relecture sans écriture.

   Aucune interaction, aucun événement (ni session_ended), aucune autre
   colonne. La campagne n'a pas à être encore active ni la participation
   encore enrolled ; à partir de l'échéance de conservation : aucune
   écriture (not_admissible / retention_expired).

3. Parcours B — participations échues (une participation par transaction) :

   select_purgeable_participation_candidates  participations dont la
                                   campagne est POTENTIELLEMENT échue
                                   (échéance SQL en secondes absolues) OU
                                   porte une politique, un statut ou des
                                   dates hors contrat (signalée, jamais
                                   rendue invisible) ; ordre de la clé
                                   primaire (campaign_id, user_id) ;
   purge_participation             sous verrous (clé participation, ligne
                                   FOR UPDATE, campagne FOR SHARE) :
                                   échéance, dépendances physiques (preuves,
                                   attributions : EXISTS, jamais verrouillées
                                   ni résolues) et logiques (finalités des
                                   événements du même utilisateur), visites
                                   puis interactions FOR UPDATE (ordre de
                                   leur clé), relecture finale, horloge, puis
                                   DELETE ensemblistes et conditionnels des
                                   interactions (session_id NULL compris),
                                   des visites et de la participation, dans
                                   UN SAVEPOINT, nombres de lignes exacts ;
   observe_participation_purge     relecture conservatrice sans écriture.

   Aucun statut (enrolled, withdrawn, completed), aucune absence d'activité
   n'avance l'échéance : tous les inscrits restent conservés jusqu'à elle.
   Aucune preuve, aucune attribution, aucun événement, aucune campagne,
   cohorte, place, invitation ni donnée F2 n'est modifié ou supprimé.

Sélections : lecture seule, sans verrou, jamais une autorisation ; taille
de page entière exacte 1..200 (défaut 50) ; curseur strict issu de la SEULE
photographie de sélection ; jamais OFFSET ; curseurs des deux parcours de
types distincts (incompatibles).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Float, and_, delete, exists, func, literal, not_, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from core.models import (
    ProductActivityEvent,
    ProductActivityEventAttribution,
    ProductActivityProof,
    ProductActivitySession,
    ProductActivitySessionInteraction,
    ProductCampaign,
    ProductCampaignParticipant,
)
from core.product_analytics import common, proofs, retention, sessions
from core.product_analytics.common import (
    ACTIVE,
    ACTIVITY_SESSION_INACTIVITY,
    CAMPAIGN_STATUSES,
    CANCELLED,
    CLOSED,
    MEASUREMENT_POLICIES,
    PLANNED,
    RETENTION_POLICIES,
    InvalidProductAnalyticsInput,
    InvalidRetentionScopes,
    ProductAnalyticsError,
)

# --------------------------------------------------------------------------
# Vocabulaires fermés et bornes opérationnelles
# --------------------------------------------------------------------------

# Limites opérationnelles proposées (pas des décisions produit).
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

SESSION_FINALIZATION = "session_finalization"
PARTICIPATION_PURGE = "participation_purge"
SELECTIONS = (SESSION_FINALIZATION, PARTICIPATION_PURGE)

# Issues communes.
ABSENT = "absent"
NOT_DUE = "not_due"
NOT_ADMISSIBLE = "not_admissible"
CONFLICT = "conflict"
UNKNOWN = "unknown"
# Parcours A.
FINALIZED = "finalized"  # visite finalisée par cet appel, écriture flushée
ALREADY_FINALIZED = "already_finalized"
FINALIZATION_OUTCOMES = (FINALIZED, ALREADY_FINALIZED, NOT_DUE, NOT_ADMISSIBLE, ABSENT, CONFLICT, UNKNOWN)
# Parcours B.
PURGED = "purged"  # interactions, visites et participation supprimées par cet appel, écriture flushée
BLOCKED = "blocked"  # dépendance légitime encore présente : aucune suppression
PURGE_OUTCOMES = (PURGED, BLOCKED, NOT_DUE, NOT_ADMISSIBLE, ABSENT, CONFLICT, UNKNOWN)
# Relectures.
PRESENT = "present"
UNSTABLE = "unstable"  # relecture : état changé pendant l'observation

# Motifs fermés.
RETENTION_EXPIRED = "retention_expired"  # conservation de la participation échue : aucune écriture
DEADLINE_UNESTABLISHED = "deadline_unestablished"  # campagne planned : aucune échéance (PR 4)
PROOF_REMAINING = "proof_remaining"
ATTRIBUTION_REMAINING = "attribution_remaining"
EVENT_SCOPE_REMAINING = "event_scope_remaining"
RETENTION_INCOHERENT = "retention_incoherent"  # statut / dates de campagne, ou document de finalités illisible
POLICY_INCOHERENT = "policy_incoherent"  # politique de conservation ou de mesure hors registre
SESSION_INCOHERENT = "session_incoherent"  # invariants de la visite rompus, ou visite hors contrat
PARTICIPATION_INCOHERENT = "participation_incoherent"  # invariants universels de la participation rompus
IDENTITY_CHANGED = "identity_changed"
DEPENDENCY_CHANGED = "dependency_changed"  # dépendance apparue après son contrôle, ou violation de FK
CLOCK_INCOHERENT = "clock_incoherent"
WRITE_UNCONFIRMED = "write_unconfirmed"  # écriture conditionnelle : nombre de lignes inattendu
REASONS = (RETENTION_EXPIRED, DEADLINE_UNESTABLISHED, PROOF_REMAINING, ATTRIBUTION_REMAINING, EVENT_SCOPE_REMAINING,
           RETENTION_INCOHERENT, POLICY_INCOHERENT, SESSION_INCOHERENT, PARTICIPATION_INCOHERENT, IDENTITY_CHANGED,
           DEPENDENCY_CHANGED, CLOCK_INCOHERENT, WRITE_UNCONFIRMED)
BLOCKING_REASONS = (PROOF_REMAINING, ATTRIBUTION_REMAINING, EVENT_SCOPE_REMAINING)

SESSION_LOCK_KIND = "activity_session"  # clé de sessions._lock (égalité testée)
PARTICIPATION_LOCK_KIND = "participation"  # clé de participation._lock_identity_and_read et cohort
SECONDS_PER_DAY = 86400.0
_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
# Anomalies structurelles converties en motif (jamais une exception non maîtrisée).
_STRUCTURE_ERRORS = (InvalidRetentionScopes, InvalidProductAnalyticsInput, TypeError, ValueError, KeyError,
                     AttributeError)
# Lecture progressive des documents de conservation (curseur serveur).
EVENT_SCAN_BATCH = 500


class RetentionDeadlineIncoherent(ProductAnalyticsError):
    """Politique, statut ou dates de campagne hors contrat : aucune échéance
    de conservation n'est établie. reason : POLICY_INCOHERENT ou
    RETENTION_INCOHERENT."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------
# Contrats internes (aucun identifiant ni contenu dans les représentations)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, repr=False)
class SessionCursor:
    """Dernière visite rendue par la sélection A (photographie de la
    sélection, jamais l'état après traitement). Interne, jamais journalisé."""
    campaign_id: uuid.UUID
    position: datetime
    session_id: uuid.UUID

    def __repr__(self) -> str:
        return "SessionCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class ParticipationCursor:
    """Dernière participation rendue par la sélection B (ordre de la clé
    primaire). Interne, jamais journalisé."""
    campaign_id: uuid.UUID
    user_id: str

    def __repr__(self) -> str:
        return "ParticipationCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class SessionPage:
    """Page A : identifiants internes de visite seulement ; next_cursor None
    quand la sélection est épuisée (page incomplète)."""
    selection: str
    session_ids: tuple
    next_cursor: SessionCursor | None

    def __repr__(self) -> str:
        return f"SessionPage({self.selection}, {len(self.session_ids)}, more={self.next_cursor is not None})"


@dataclass(frozen=True, slots=True, repr=False)
class ParticipationPage:
    """Page B : couples (campaign_id, user_id) seulement."""
    selection: str
    participations: tuple
    next_cursor: ParticipationCursor | None

    def __repr__(self) -> str:
        return (f"ParticipationPage({self.selection}, {len(self.participations)}, "
                f"more={self.next_cursor is not None})")


@dataclass(frozen=True, slots=True, repr=False)
class _SessionAttempt:
    """Contexte interne d'une finalisation tentée : identités de
    verrouillage, photographie COMPLÈTE de la visite avant et après l'UPDATE
    (toutes ses colonnes). Jamais journalisé ni rendu à l'appelant final."""
    session_id: uuid.UUID
    campaign_id: uuid.UUID
    user_id: str
    key: int
    before: tuple
    after: tuple

    def __repr__(self) -> str:
        return "_SessionAttempt(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _PurgeAttempt:
    """Contexte interne d'une purge tentée : unité et clé de verrouillage
    seulement (aucune photographie des descendants)."""
    campaign_id: uuid.UUID
    user_id: str
    key: int

    def __repr__(self) -> str:
        return "_PurgeAttempt(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class SessionDecision:
    """Issue du parcours A. outcome : FINALIZATION_OUTCOMES ; reason : REASONS
    ou None ; written : cet appel a écrit (flush, NON durable avant le COMMIT
    de l'appelant) ; attempt : contexte privé de l'écriture tentée."""
    outcome: str
    reason: str | None = None
    written: bool = False
    attempt: _SessionAttempt | None = None

    def __repr__(self) -> str:
        return f"SessionDecision({self.outcome}, {self.reason}, written={self.written})"


@dataclass(frozen=True, slots=True, repr=False)
class PurgeDecision:
    """Issue du parcours B. outcome : PURGE_OUTCOMES ; interactions /
    sessions : nombres de lignes supprimées (purged seulement, NON durables
    avant le COMMIT) ; attempt : contexte privé de l'écriture tentée."""
    outcome: str
    reason: str | None = None
    written: bool = False
    interactions: int = 0
    sessions: int = 0
    attempt: _PurgeAttempt | None = None

    def __repr__(self) -> str:
        return f"PurgeDecision({self.outcome}, {self.reason}, written={self.written})"


@dataclass(frozen=True, slots=True, repr=False)
class SessionObservation:
    """Relecture sans écriture : PRESENT / ABSENT / UNSTABLE ; state :
    photographie privée de la visite (PRESENT seulement)."""
    outcome: str
    state: tuple | None = None

    def __repr__(self) -> str:
        return f"SessionObservation({self.outcome})"


@dataclass(frozen=True, slots=True)
class PurgeObservation:
    """Relecture conservatrice sans écriture : ABSENT seulement si la
    participation, ses visites, ses interactions et toute dépendance
    physique ou logique pertinente sont absentes ; PRESENT sinon. Aucune
    photographie, aucun compteur."""
    outcome: str


# --------------------------------------------------------------------------
# Validation des entrées (avant tout accès à la base)
# --------------------------------------------------------------------------

def _require_limit(limit) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidProductAnalyticsInput(f"limit : entier exact de 1 à {MAX_PAGE_SIZE} requis")
    return limit


def _require_uuid(value, field: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise InvalidProductAnalyticsInput(f"{field} : uuid.UUID requis")
    return value


def _require_user_id(value) -> str:
    if type(value) is not str:
        raise InvalidProductAnalyticsInput("user_id : identifiant non vide requis")
    return common.require_user_id(value)


def _require_cursor(after, cursor_type):
    if after is None:
        return None
    if type(after) is not cursor_type:
        raise InvalidProductAnalyticsInput(f"after : {cursor_type.__name__} requis")
    _require_uuid(after.campaign_id, "campaign_id")
    if cursor_type is SessionCursor:
        if not isinstance(after.position, datetime) or after.position.utcoffset() is None:
            raise InvalidProductAnalyticsInput("position : datetime avec fuseau horaire requise")
        _require_uuid(after.session_id, "session_id")
    else:
        _require_user_id(after.user_id)
    return after


# --------------------------------------------------------------------------
# Échéance de conservation des participations
# --------------------------------------------------------------------------

def _aware(value, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RetentionDeadlineIncoherent(f"campagne : {field} absente ou sans fuseau", reason=RETENTION_INCOHERENT)
    return value.astimezone(timezone.utc)


def participation_retention_deadline(campaign) -> datetime | None:
    """Échéance commune de conservation des participations de `campaign`
    (voir l'en-tête), ou None pour une campagne planned. Lève
    RetentionDeadlineIncoherent si la politique, le statut ou les dates sont
    hors contrat. Aucun accès à la base, aucune horloge."""
    try:
        common.validate_policy(retention_days=campaign.retention_days,
                               retention_policy_version=campaign.retention_policy_version,
                               measurement_policy_version=campaign.measurement_policy_version)
    except InvalidProductAnalyticsInput:
        raise RetentionDeadlineIncoherent("campagne : politique hors registre", reason=POLICY_INCOHERENT) from None
    status = campaign.status
    if type(status) is not str or status not in CAMPAIGN_STATUSES:
        raise RetentionDeadlineIncoherent("campagne : statut hors vocabulaire", reason=RETENTION_INCOHERENT)
    created_at = _aware(campaign.created_at, "created_at")
    close_by_at = _aware(campaign.close_by_at, "close_by_at")
    activated_at = _aware(campaign.activated_at, "activated_at", optional=True)
    closed_at = _aware(campaign.closed_at, "closed_at", optional=True)

    def incoherent(message):
        return RetentionDeadlineIncoherent(f"campagne : {message}", reason=RETENTION_INCOHERENT)

    if not created_at < close_by_at:
        raise incoherent("close_by_at non postérieure à la création")
    if activated_at is not None and not created_at <= activated_at < close_by_at:
        raise incoherent("activation hors de [created_at, close_by_at)")
    if closed_at is not None and not (created_at <= closed_at <= close_by_at
                                      and (activated_at is None or activated_at <= closed_at)):
        raise incoherent("clôture hors de la chronologie")
    if status == PLANNED:
        if activated_at is not None or closed_at is not None:
            raise incoherent("planned avec une activation ou une clôture")
        return None
    if status == ACTIVE:
        if activated_at is None or closed_at is not None:
            raise incoherent("active sans activation, ou déjà clôturée")
        effective_end = close_by_at  # fin effective MAXIMALE
    else:
        if closed_at is None:
            raise incoherent("état terminal sans closed_at")
        if status == CLOSED and activated_at is None:
            raise incoherent("closed sans activation")
        effective_end = min(closed_at, close_by_at)  # jamais prolongée par un retard de clôture
    return effective_end.astimezone(timezone.utc) + timedelta(days=campaign.retention_days)


def _deadline_or_reason(campaign):
    """(échéance, None) ou (None, motif) ; planned : (None, DEADLINE_UNESTABLISHED)."""
    try:
        deadline = participation_retention_deadline(campaign)
    except RetentionDeadlineIncoherent as exc:
        return None, exc.reason
    except _STRUCTURE_ERRORS:
        return None, RETENTION_INCOHERENT
    return (deadline, None) if deadline is not None else (None, DEADLINE_UNESTABLISHED)


# --------------------------------------------------------------------------
# Sélections bornées (lecture seule, sans verrou)
# --------------------------------------------------------------------------

def _absolute_seconds(expression):
    """Intervalle SQL en SECONDES absolues (jamais un timedelta lié : psycopg2
    le transmettrait en jours, ajoutés en jours CIVILS du fuseau de session)."""
    return func.make_interval(0, 0, 0, 0, 0, 0, expression)


def _idle_query(now: datetime, limit: int, after: SessionCursor | None):
    """Index partiel ix_product_activity_sessions_open_idle_expiry
    (campaign_id, idle_expires_at) WHERE finalized_at IS NULL : borne du
    curseur en condition d'index, départage par id."""
    visit = ProductActivitySession
    statement = select(visit.campaign_id, visit.idle_expires_at, visit.id).where(
        visit.finalized_at.is_(None), visit.idle_expires_at <= now)
    if after is not None:
        statement = statement.where(
            tuple_(visit.campaign_id, visit.idle_expires_at) >= tuple_(after.campaign_id, after.position),
            tuple_(visit.campaign_id, visit.idle_expires_at, visit.id) > tuple_(after.campaign_id, after.position,
                                                                                after.session_id))
    return statement.order_by(visit.campaign_id, visit.idle_expires_at, visit.id).limit(limit)


def select_idle_session_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                   after: SessionCursor | None = None) -> SessionPage:
    """Visites ouvertes dont l'inactivité est POTENTIELLEMENT échue
    (indicatif : chaque candidat est relu sous verrou)."""
    limit = _require_limit(limit)
    after = _require_cursor(after, SessionCursor)
    now = proofs.server_clock(db)  # horloge PostgreSQL ; jamais fournie par l'appelant
    found = db.execute(_idle_query(now, limit, after)).all()
    next_cursor = None
    if len(found) == limit:
        campaign_id, position, session_id = found[-1]
        next_cursor = SessionCursor(campaign_id, position, session_id)  # photographie de la sélection
    return SessionPage(SESSION_FINALIZATION, tuple(row[2] for row in found), next_cursor)


def _campaign_anomaly():
    """Politique, statut ou dates que participation_retention_deadline refuse,
    pour toute ligne admise par les CHECK de 0022 (couverture testée) :
    signalés quelle que soit l'horloge, jamais rendus invisibles."""
    c = ProductCampaign
    known_policy = tuple_(c.retention_policy_version, c.retention_days).in_(sorted(RETENTION_POLICIES.items()))
    return or_(
        not_(known_policy), not_(c.measurement_policy_version.in_(MEASUREMENT_POLICIES)),
        not_(c.status.in_(CAMPAIGN_STATUSES)),
        c.close_by_at <= c.created_at,
        and_(c.activated_at.is_not(None), or_(c.activated_at < c.created_at, c.activated_at >= c.close_by_at)),
        and_(c.closed_at.is_not(None), or_(c.closed_at < c.created_at, c.closed_at > c.close_by_at)),
        and_(c.activated_at.is_not(None), c.closed_at.is_not(None), c.closed_at < c.activated_at),
        and_(c.status == PLANNED, or_(c.activated_at.is_not(None), c.closed_at.is_not(None))),
        and_(c.status == ACTIVE, or_(c.activated_at.is_(None), c.closed_at.is_not(None))),
        and_(c.status == CLOSED, or_(c.activated_at.is_(None), c.closed_at.is_(None))),
        and_(c.status == CANCELLED, c.closed_at.is_(None)),
    )


def _potential_deadline():
    """Échéance indicative en SQL (secondes absolues) : fin effective bornée
    par close_by_at + retention_days."""
    c = ProductCampaign
    effective_end = func.least(func.coalesce(c.closed_at, c.close_by_at), c.close_by_at)
    return effective_end + _absolute_seconds(c.retention_days * literal(SECONDS_PER_DAY, Float()))


def _purge_query(now: datetime, limit: int, after: ParticipationCursor | None):
    """Clé primaire de product_campaign_participants : parcours borné dans
    son ordre ; jointure sur la campagne (filtre indicatif)."""
    p, c = ProductCampaignParticipant, ProductCampaign
    statement = (select(p.campaign_id, p.user_id).join(c, c.id == p.campaign_id)
                 .where(or_(and_(c.status != PLANNED, _potential_deadline() <= now), _campaign_anomaly())))
    if after is not None:
        statement = statement.where(tuple_(p.campaign_id, p.user_id) > tuple_(after.campaign_id, after.user_id))
    return statement.order_by(p.campaign_id, p.user_id).limit(limit)


def select_purgeable_participation_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                              after: ParticipationCursor | None = None) -> ParticipationPage:
    """Participations POTENTIELLEMENT purgeables, ou dont la campagne est
    hors contrat (indicatif : chaque candidat est relu sous verrou)."""
    limit = _require_limit(limit)
    after = _require_cursor(after, ParticipationCursor)
    now = proofs.server_clock(db)
    found = db.execute(_purge_query(now, limit, after)).all()
    next_cursor = ParticipationCursor(found[-1][0], found[-1][1]) if len(found) == limit else None
    return ParticipationPage(PARTICIPATION_PURGE, tuple((row[0], row[1]) for row in found), next_cursor)


# --------------------------------------------------------------------------
# Lectures et verrous
# --------------------------------------------------------------------------

def _lock_mode(statement, mode):
    if mode is not None:
        statement = statement.with_for_update(read=(mode == "share"))
    return statement.execution_options(populate_existing=True)  # jamais un état ORM périmé


def _visit(db, session_id, *, mode=None):
    return db.execute(_lock_mode(select(ProductActivitySession).where(ProductActivitySession.id == session_id),
                                 mode)).scalar_one_or_none()


def _participation(db, campaign_id, user_id, *, mode=None):
    p = ProductCampaignParticipant
    return db.execute(_lock_mode(select(p).where(p.campaign_id == campaign_id, p.user_id == user_id), mode)
                      ).scalar_one_or_none()


def _campaign(db, campaign_id, *, mode=None):
    return db.execute(_lock_mode(select(ProductCampaign).where(ProductCampaign.id == campaign_id), mode)
                      ).scalar_one_or_none()


def _row(row) -> tuple | None:
    """Toutes les colonnes d'une ligne (photographie complète)."""
    if row is None:
        return None
    return tuple((column.key, getattr(row, column.key)) for column in row.__table__.columns)


def session_lock_key(campaign_id, user_id, environment_digest) -> int:
    """Clé consultative EXACTE de sessions._lock (environnement de la
    participation) : sérialise avec touch_activity_session,
    close_activity_session et l'émission d'origine du bloc B."""
    return common.identity_lock_key(SESSION_LOCK_KIND, campaign_id, user_id, environment_digest)


def participation_lock_key(campaign_id, user_id) -> int:
    """Clé consultative EXACTE des inscriptions et admissions
    (participation._lock_identity_and_read, cohort.admit_at_first_eligible_login)."""
    return common.identity_lock_key(PARTICIPATION_LOCK_KIND, campaign_id, user_id)


def _visit_identity(visit) -> tuple:
    return visit.id, visit.campaign_id, visit.user_id, visit.environment_digest


def _participation_problem(participation, campaign) -> str | None:
    """Invariants UNIVERSELS d'une participation, communs aux deux chemins
    d'inscription (participation.enroll_participant, inscription générique ;
    participation.first_eligible_login, première connexion et cohorte) et
    admis ou non par les CHECK de 0022 (correctif R1 B2). Lecture seule, sans
    horloge :

    - trace d'autorisation présente (authorization_recorded_at, notice_version
      au format fermé) ;
    - authorization_recorded_at <= enrolled_at <= window_start_at <
      window_end_at (égalités admises : J0 = inscription = début) ;
    - campagne : created_at <= enrolled_at < close_by_at, window_end_at <=
      close_by_at ; enrolled_at <= closed_at (l'inscription exige une campagne
      planned ou active, sérialisée avec la clôture) ;
    - statut du vocabulaire fermé (aucun statut n'est exigé ni préféré).

    Jamais imposés (contrats propres à la première connexion ou à la cohorte) :
    durée de douze jours, J0 = enrolled_at = window_start_at, J0 >= activation.
    Une fenêtre contractuelle dépassant closed_at (clôture anticipée) est
    légitime."""
    try:
        if participation.participation_status not in common.PARTICIPATION_STATUSES:
            return PARTICIPATION_INCOHERENT
        notice = participation.notice_version
        if (participation.authorization_recorded_at is None or type(notice) is not str
                or not common.NOTICE_VERSION_PATTERN.fullmatch(notice)):
            return PARTICIPATION_INCOHERENT
        if not (participation.authorization_recorded_at <= participation.enrolled_at
                <= participation.window_start_at < participation.window_end_at):
            return PARTICIPATION_INCOHERENT
        if not (campaign.created_at <= participation.enrolled_at < campaign.close_by_at
                and participation.window_end_at <= campaign.close_by_at):
            return PARTICIPATION_INCOHERENT
        if campaign.closed_at is not None and participation.enrolled_at > campaign.closed_at:
            return PARTICIPATION_INCOHERENT
    except _STRUCTURE_ERRORS:
        return PARTICIPATION_INCOHERENT
    return None


def _stored_instants(campaign, participation, visit=None) -> tuple:
    """Instants STOCKÉS qui doivent précéder (ou égaler) l'horloge de décision :
    création, activation et clôture de la campagne, autorisation et inscription
    de la participation, ouverture, activité et fin de la visite. Jamais
    window_start_at / window_end_at ni close_by_at (bornes contractuelles,
    légitimement futures)."""
    instants = [campaign.created_at, campaign.activated_at, campaign.closed_at,
                participation.authorization_recorded_at, participation.enrolled_at]
    if visit is not None:
        instants += [visit.opened_at, visit.last_activity_at, visit.finalized_at]
    return tuple(instant for instant in instants if instant is not None)


def _clock_problem(now: datetime, instants) -> bool:
    """Horloge de décision antérieure à un instant stocké : chronologie stockée
    ou horloge incohérente ; aucune décision, aucune écriture, jamais
    d'antidatage (correctif R1 B1)."""
    return any(now < instant for instant in instants)


def _session_problem(visit, participation, campaign) -> str | None:
    """Invariants de la visite (0023 et sessions.py), relus sous verrou :
    empreinte, chronologie, inactivité de 30 minutes, fin cohérente, visite
    ouverte pendant l'observation d'une campagne activée."""
    try:
        digest = visit.environment_digest
        if (type(digest) is not str or len(digest) != 64 or not set(digest) <= _DIGEST_CHARACTERS
                or not visit.opened_at <= visit.last_activity_at
                or visit.idle_expires_at != visit.last_activity_at + ACTIVITY_SESSION_INACTIVITY):
            return SESSION_INCOHERENT
        if (visit.finalized_at is None) != (visit.end_reason is None):
            return SESSION_INCOHERENT
        if visit.end_reason == sessions.INACTIVITY and visit.finalized_at < visit.idle_expires_at:
            return SESSION_INCOHERENT
        if visit.end_reason == sessions.LOGOUT and not (visit.last_activity_at <= visit.finalized_at
                                                         < visit.idle_expires_at):
            return SESSION_INCOHERENT
        if visit.end_reason is not None and visit.end_reason not in sessions.END_REASONS:
            return SESSION_INCOHERENT
        if campaign.activated_at is None:
            return SESSION_INCOHERENT  # visite d'une campagne jamais activée : hors contrat
        if not (campaign.activated_at <= visit.opened_at and participation.window_start_at <= visit.opened_at
                and participation.enrolled_at <= visit.opened_at
                and visit.last_activity_at < participation.window_end_at
                and visit.last_activity_at < campaign.close_by_at):
            return SESSION_INCOHERENT
        # Correctif R1 B1 : aucune activité après la clôture effective (touch exige
        # une campagne active, sérialisé avec la clôture : égalité admise). La
        # FINALISATION, elle, peut légitimement suivre la clôture.
        if campaign.closed_at is not None and visit.last_activity_at > campaign.closed_at:
            return SESSION_INCOHERENT
    except _STRUCTURE_ERRORS:
        return SESSION_INCOHERENT
    return None


# --------------------------------------------------------------------------
# Parcours A : finalisation d'une visite inactive
# --------------------------------------------------------------------------

class _Unconfirmed(Exception):
    """Levée DANS le SAVEPOINT : une écriture n'a pas touché le nombre de
    lignes établi sous verrou."""


def _affected(db, statement, expected: int) -> None:
    if db.execute(statement.execution_options(synchronize_session=False)).rowcount != expected:
        raise _Unconfirmed()


def _finalize(db, visit, key: int, now: datetime) -> SessionDecision:
    """UPDATE conditionnel de finalized_at et end_reason SEULEMENT, dans UN
    SAVEPOINT, exactement une ligne."""
    table = ProductActivitySession
    before = _row(visit)
    statement = (update(table).where(
        table.id == visit.id, table.campaign_id == visit.campaign_id, table.user_id == visit.user_id,
        table.environment_digest == visit.environment_digest, table.opened_at == visit.opened_at,
        table.last_activity_at == visit.last_activity_at, table.idle_expires_at == visit.idle_expires_at,
        table.finalized_at.is_(None), table.end_reason.is_(None), table.idle_expires_at <= now)
        .values(finalized_at=now, end_reason=sessions.INACTIVITY))
    try:
        with db.begin_nested():
            _affected(db, statement, 1)
    except _Unconfirmed:
        return SessionDecision(CONFLICT, WRITE_UNCONFIRMED)
    db.expunge(visit)
    after = tuple((name, {"finalized_at": now, "end_reason": sessions.INACTIVITY}.get(name, value))
                  for name, value in before)
    return SessionDecision(FINALIZED, None, True, _SessionAttempt(visit.id, visit.campaign_id, visit.user_id, key,
                                                                  before, after))


def finalize_idle_session(db, *, session_id: uuid.UUID) -> SessionDecision:
    """Finalise ('inactivity') UNE visite dont l'inactivité est échue, si la
    conservation de sa participation est encore valide. Ordre des verrous :
    0. lecture indicative ; 1. clé activity_session ; 2. participation FOR
    SHARE ; 3. campagne FOR SHARE ; 4. visite FOR UPDATE ; 5. relecture et
    validations ; 6. horloge ; 7. décision ; 8. UPDATE conditionnel."""
    _require_uuid(session_id, "session_id")
    hint = _visit(db, session_id)  # 0. indicative
    if hint is None:
        return SessionDecision(ABSENT)
    identity = _visit_identity(hint)
    _, campaign_id, user_id, digest = identity
    key = session_lock_key(campaign_id, user_id, digest)
    common.lock_identities(db, key)  # 1. clé EXACTE de sessions.py, la seule
    participation = _participation(db, campaign_id, user_id, mode="share")  # 2.
    campaign = _campaign(db, campaign_id, mode="share") if participation is not None else None  # 3.
    visit = _visit(db, session_id, mode="update")  # 4.
    # 5. relecture sous verrous
    if visit is None:
        return SessionDecision(ABSENT)
    if _visit_identity(visit) != identity:
        return SessionDecision(CONFLICT, IDENTITY_CHANGED)
    if participation is None or campaign is None:
        return SessionDecision(CONFLICT, SESSION_INCOHERENT)  # FK : impossible sans écrivain hors service
    problem = _session_problem(visit, participation, campaign)
    if problem is not None:
        return SessionDecision(CONFLICT, problem)
    deadline, reason = _deadline_or_reason(campaign)
    if deadline is None:
        return SessionDecision(CONFLICT, SESSION_INCOHERENT if reason == DEADLINE_UNESTABLISHED else reason)
    problem = _participation_problem(participation, campaign)
    if problem is not None:
        return SessionDecision(CONFLICT, problem)
    now = proofs.server_clock(db)  # 6. APRÈS tous les verrous : instant de décision
    if _clock_problem(now, _stored_instants(campaign, participation, visit)):
        return SessionDecision(UNKNOWN, CLOCK_INCOHERENT)  # jamais d'antidatage ni d'activité inventée
    # 7. décision
    if now >= deadline:
        return SessionDecision(NOT_ADMISSIBLE, RETENTION_EXPIRED)  # égalité comprise : aucune écriture
    if visit.finalized_at is not None:
        return SessionDecision(ALREADY_FINALIZED)
    if now < visit.idle_expires_at:
        return SessionDecision(NOT_DUE)
    return _finalize(db, visit, key, now)  # 8.


def _session_observation(db, session_id, campaign_id, user_id, digest, key) -> SessionObservation:
    """Relecture STABLE sans écriture, ordre compatible (rangs 1 à 4, lignes
    EN PARTAGE) : clé fournie, participation, campagne, visite lue (L1) puis
    verrouillée (L2) ; L1 = L2 exigé, et l'identité observée (dont
    environment_digest) doit être celle de la clé détenue, sinon UNSTABLE :
    jamais de clé supplémentaire (correctif R1, relecture complémentaire)."""
    common.lock_identities(db, key)
    participation = _participation(db, campaign_id, user_id, mode="share")
    if participation is not None:
        _campaign(db, campaign_id, mode="share")
    first = _row(_visit(db, session_id))
    second = _row(_visit(db, session_id, mode="share"))
    if first is None and second is None:
        return SessionObservation(ABSENT)
    if first != second or participation is None:
        return SessionObservation(UNSTABLE)  # changée pendant l'observation, ou visite sans participation
    values = dict(second)
    if (values["id"], values["campaign_id"], values["user_id"], values["environment_digest"]) != (
            session_id, campaign_id, user_id, digest) or session_lock_key(campaign_id, user_id, digest) != key:
        return SessionObservation(UNSTABLE)  # identité exigeant une autre clé que celle détenue
    return SessionObservation(PRESENT, second)


def observe_session_finalization(db, *, session_id: uuid.UUID) -> SessionObservation:
    """État d'UNE visite sous la clé de sa lecture indicative, SANS écriture."""
    _require_uuid(session_id, "session_id")
    hint = _visit(db, session_id)
    if hint is None:
        return SessionObservation(ABSENT)
    _, campaign_id, user_id, digest = _visit_identity(hint)
    return _session_observation(db, session_id, campaign_id, user_id, digest,
                                session_lock_key(campaign_id, user_id, digest))


def _observe_session_attempt(db, *, attempt: _SessionAttempt) -> SessionObservation:
    """Relecture d'un COMMIT incertain avec la clé et les identités de
    l'opération TENTÉE (contexte en mémoire) : aucune clé supplémentaire."""
    if type(attempt) is not _SessionAttempt:
        raise InvalidProductAnalyticsInput("attempt : contexte de finalisation requis")
    return _session_observation(db, attempt.session_id, attempt.campaign_id, attempt.user_id,
                                dict(attempt.before)["environment_digest"], attempt.key)


# --------------------------------------------------------------------------
# Parcours B : purge atomique d'une participation
# --------------------------------------------------------------------------

def _exists(db, model, campaign_id, user_id) -> bool:
    return db.execute(select(exists().where(model.campaign_id == campaign_id, model.user_id == user_id))
                      ).scalar_one()


# Correctif R5 bis : lecture tolérante et classification EXTRAITES vers
# retention.py (lot 2C, PR 4) sans changement sémantique, pour être partagées
# exactement avec la purge définitive des campagnes ; ce module conserve son
# périmètre (événements du même utilisateur).
_lenient_campaigns = retention.lenient_campaigns


def _event_scope_problem(db, campaign_id, user_id) -> str | None:
    """Finalités logiques des événements du même utilisateur, lues
    progressivement (curseur serveur, aucune ligne ORM) :

    - document conforme (retention.parse_retention_scopes) avec une finalité
      de la campagne, échue ou non : EVENT_SCOPE_REMAINING (bloquant) ;
    - document conforme sans finalité de la campagne : aucun effet (une
      anomalie de rattachement ou d'échéance d'une AUTRE campagne relève du
      contrôle d'intégrité de la PR 2) ;
    - document hors contrat dont la structure reste lisible : bloquant
      (RETENTION_INCOHERENT) seulement s'il désigne la campagne ;
    - structure illisible : RETENTION_INCOHERENT (la campagne ne peut pas
      être exclue).
    RETENTION_INCOHERENT l'emporte sur EVENT_SCOPE_REMAINING."""
    statement = (select(ProductActivityEvent.retention_scopes).where(ProductActivityEvent.user_id == user_id)
                 .order_by(ProductActivityEvent.id).execution_options(yield_per=EVENT_SCAN_BATCH))
    remaining = False
    with db.execute(statement) as result:  # curseur serveur refermé même sur un retour anticipé
        for document in result.scalars():
            reference = retention.campaign_reference(document, campaign_id)  # parse_retention_scopes, puis R5 bis
            if reference == retention.REFERENCE_INCOHERENT:
                return RETENTION_INCOHERENT
            remaining = remaining or reference == retention.REFERENCE_PRESENT
    return EVENT_SCOPE_REMAINING if remaining else None


def _dependency_problem(db, campaign_id, user_id) -> str | None:
    """Dépendances CONSTATÉES, jamais verrouillées ni résolues : preuves (tous
    états), attributions (FK de 0024 vers la participation), finalités des
    événements (aucune FK)."""
    if _exists(db, ProductActivityProof, campaign_id, user_id):
        return PROOF_REMAINING
    if _exists(db, ProductActivityEventAttribution, campaign_id, user_id):
        return ATTRIBUTION_REMAINING
    return _event_scope_problem(db, campaign_id, user_id)


def _lock_descendants(db, model, order, campaign_id, user_id) -> int:
    """FOR UPDATE de toutes les lignes du couple, dans l'ordre déterministe de
    leur clé, en UNE instruction ensembliste ; rend leur nombre."""
    locked = (select(order).where(model.campaign_id == campaign_id, model.user_id == user_id)
              .order_by(order).with_for_update())
    return db.execute(select(func.count()).select_from(locked.subquery())).scalar_one()


def _same_participation(participation):
    """Prédicat conditionnel : la participation telle que relue sous verrou."""
    table = ProductCampaignParticipant
    return tuple(getattr(table, name) == value if value is not None else getattr(table, name).is_(None)
                 for name, value in _row(participation))


def _blocked_or_conflict(problem: str) -> PurgeDecision:
    return PurgeDecision(BLOCKED if problem in BLOCKING_REASONS else CONFLICT, problem)


def _delete(db, participation, counts: tuple, key: int) -> PurgeDecision:
    """Suppression atomique, dans UN SAVEPOINT : interactions du couple
    (session_id NULL compris), visites du couple, participation ; nombres de
    lignes EXACTEMENT ceux établis sous verrou."""
    campaign_id, user_id = participation.campaign_id, participation.user_id
    interactions, visits = counts
    i, v = ProductActivitySessionInteraction, ProductActivitySession
    try:
        with db.begin_nested():
            _affected(db, delete(i).where(i.campaign_id == campaign_id, i.user_id == user_id), interactions)
            _affected(db, delete(v).where(v.campaign_id == campaign_id, v.user_id == user_id), visits)
            _affected(db, delete(ProductCampaignParticipant).where(*_same_participation(participation)), 1)
    except _Unconfirmed:
        return PurgeDecision(CONFLICT, WRITE_UNCONFIRMED)
    except IntegrityError:
        return PurgeDecision(CONFLICT, DEPENDENCY_CHANGED)  # référence apparue hors service : rien n'est supprimé
    db.expunge(participation)
    return PurgeDecision(PURGED, None, True, interactions, visits, _PurgeAttempt(campaign_id, user_id, key))


def purge_participation(db, *, campaign_id: uuid.UUID, user_id: str) -> PurgeDecision:
    """Purge atomique d'UNE participation échue et de ses descendants. Ordre
    des verrous : 0. lecture indicative ; 1. clé participation ; 2.
    participation FOR UPDATE ; 3. campagne FOR SHARE ; 4. échéance ; 5.
    preuves, attributions, finalités (constatées) ; 6. visites FOR UPDATE ;
    7. interactions FOR UPDATE ; 8. relecture finale, nombres attendus ; 9.
    horloge ; 10. admissibilité définitive ; 11. suppression atomique."""
    _require_uuid(campaign_id, "campaign_id")
    _require_user_id(user_id)
    if _participation(db, campaign_id, user_id) is None:  # 0. indicative
        return PurgeDecision(ABSENT)
    key = participation_lock_key(campaign_id, user_id)
    common.lock_identities(db, key)  # 1. la seule clé
    participation = _participation(db, campaign_id, user_id, mode="update")  # 2.
    if participation is None:
        return PurgeDecision(ABSENT)
    if (participation.campaign_id, participation.user_id) != (campaign_id, user_id):
        return PurgeDecision(CONFLICT, IDENTITY_CHANGED)
    campaign = _campaign(db, campaign_id, mode="share")  # 3.
    if campaign is None:
        return PurgeDecision(CONFLICT, RETENTION_INCOHERENT)
    deadline, reason = _deadline_or_reason(campaign)  # 4. contrôle préliminaire
    if reason == DEADLINE_UNESTABLISHED:
        return PurgeDecision(NOT_ADMISSIBLE, DEADLINE_UNESTABLISHED)
    if reason is not None:
        return PurgeDecision(CONFLICT, reason)
    problem = _participation_problem(participation, campaign)
    if problem is not None:
        return PurgeDecision(CONFLICT, problem)  # jamais une autre échéance calculée pour contourner
    preliminary = proofs.server_clock(db)  # après les verrous des rangs 1 à 3
    if _clock_problem(preliminary, _stored_instants(campaign, participation)):
        return PurgeDecision(UNKNOWN, CLOCK_INCOHERENT)
    if preliminary < deadline:
        return PurgeDecision(NOT_DUE)  # aucun descendant verrouillé
    problem = _dependency_problem(db, campaign_id, user_id)  # 5.
    if problem is not None:
        return _blocked_or_conflict(problem)
    visits = _lock_descendants(db, ProductActivitySession, ProductActivitySession.id, campaign_id, user_id)  # 6.
    interactions = _lock_descendants(db, ProductActivitySessionInteraction,  # 7.
                                     ProductActivitySessionInteraction.interaction_key, campaign_id, user_id)
    # 8. relecture finale sous tous les verrous
    if _row(_participation(db, campaign_id, user_id)) != _row(participation):
        return PurgeDecision(CONFLICT, IDENTITY_CHANGED)
    if _dependency_problem(db, campaign_id, user_id) is not None:
        return PurgeDecision(CONFLICT, DEPENDENCY_CHANGED)  # apparue après son contrôle (hors service)
    now = proofs.server_clock(db)  # 9. instant de décision
    if now < preliminary:
        return PurgeDecision(UNKNOWN, CLOCK_INCOHERENT)
    if now < deadline:  # 10.
        return PurgeDecision(NOT_DUE)
    return _delete(db, participation, (interactions, visits), key)  # 11.


def _purge_observation(db, campaign_id, user_id, key) -> PurgeObservation:
    """Relecture CONSERVATRICE sans écriture, sous la clé de l'opération :
    participation (en partage) puis descendants et dépendances. ABSENT
    seulement si tout est absent ; sinon PRESENT (jamais une photographie)."""
    common.lock_identities(db, key)
    if _participation(db, campaign_id, user_id, mode="share") is not None:
        return PurgeObservation(PRESENT)
    for model in (ProductActivitySession, ProductActivitySessionInteraction, ProductActivityProof,
                  ProductActivityEventAttribution):
        if _exists(db, model, campaign_id, user_id):
            return PurgeObservation(PRESENT)
    if _event_scope_problem(db, campaign_id, user_id) is not None:
        return PurgeObservation(PRESENT)
    if _participation(db, campaign_id, user_id) is not None:
        return PurgeObservation(PRESENT)  # réapparue pendant l'observation
    return PurgeObservation(ABSENT)


def observe_participation_purge(db, *, campaign_id: uuid.UUID, user_id: str) -> PurgeObservation:
    """Présence de la participation, de ses descendants et de ses dépendances,
    SANS écriture."""
    _require_uuid(campaign_id, "campaign_id")
    _require_user_id(user_id)
    return _purge_observation(db, campaign_id, user_id, participation_lock_key(campaign_id, user_id))


def _observe_purge_attempt(db, *, attempt: _PurgeAttempt) -> PurgeObservation:
    """Relecture d'un COMMIT incertain avec la clé de l'opération TENTÉE."""
    if type(attempt) is not _PurgeAttempt:
        raise InvalidProductAnalyticsInput("attempt : contexte de purge requis")
    return _purge_observation(db, attempt.campaign_id, attempt.user_id, attempt.key)

