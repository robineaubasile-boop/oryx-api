"""Sessions d'activité produit (visites) des participants (V0-T08 Lot 2B-1)
— DORMANTES.

Tables product_activity_sessions et product_activity_session_interactions
(migration 0023). DISTINCTES des sessions d'authentification F2 : aucune
lecture d'oryx_sessions, aucune durée commune (15 jours absolus côté F2 ;
30 minutes d'inactivité ici), aucune FK. Aucune route, aucun raccordement ;
aucun événement n'est écrit ici (session_started et activités métier :
sous-lot 2B-2).

Règles
------

- Identité : user_id est celui du principal F2 authentifié (précondition du
  futur raccordement) ; un même utilisateur garde la même identité sur
  tous ses appareils — le service ne crée jamais d'utilisateur.
- Environnement de navigation : environment_key, UUID opaque propre à un
  environnement (profil de navigateur), partagé par ses onglets et ses
  rafraîchissements. Seule son empreinte SHA-256 est stockée
  (environment_digest). Deux environnements réellement distincts ont des
  visites distinctes ; plusieurs onglets ou un rafraîchissement du même
  environnement prolongent la MÊME visite. La source de cette clé (cookie
  serveur dédié, par exemple) est un choix du raccordement 2B-2 ; une clé
  fournie par le client ne peut qu'augmenter le nombre de visites de son
  propre titulaire, jamais changer d'utilisateur.
- Horloge : toutes les décisions (ouverture, prolongation, expiration,
  clôture, finalisation) utilisent l'horloge serveur lue APRÈS les verrous ;
  l'appelant ne fournit aucune date.
- Inactivité : une session est vivante sur [last_activity_at,
  last_activity_at + 30 min) (idle_expires_at, imposé en base). Une
  interaction NOUVELLE strictement avant idle_expires_at la prolonge ; à
  idle_expires_at ou après, la visite est finalisée ('inactivity') et une
  nouvelle visite s'ouvre.
- Observation : ouverture et prolongation seulement si la participation est
  enrolled, la campagne active et avant close_by_at, et maintenant dans
  [window_start_at, window_end_at). Après J+12 : refus, aucune session
  ouverte ni prolongée (last_activity_at < window_end_at toujours).
- Rejeu (révision R1 de l'audit) : chaque clé d'interaction ACCEPTÉE est
  inscrite dans product_activity_session_interactions (PK par
  participation), dans le même SAVEPOINT que l'effet qu'elle produit. Tout
  rejeu d'une clé déjà acceptée — la plus récente, une plus ancienne, avant
  ou après l'expiration, ou concurrent — renvoie la visite d'origine sans
  AUCUNE écriture : ni nouvelle visite, ni prolongation, ni clôture. Une clé
  rejouée dans un autre environnement, ou avec un autre type (activité /
  clôture), lève ActivitySessionConflict. Une interaction refusée (hors
  observation) n'est pas inscrite : son rejeu est jugé à nouveau, avec la
  même règle d'observation, et ne peut donc rien créer après J+12.
- Clôture volontaire (déconnexion) : close_activity_session clôt la visite
  ouverte de CET environnement ('logout', finalized_at = maintenant, avant
  l'expiration), sans modifier last_activity_at, la participation ni aucune
  autre visite. Si la visite était déjà expirée, elle est finalisée comme
  'inactivity'. La clé de clôture est inscrite (même sans visite ouverte) :
  son rejeu ne clôt jamais une visite ouverte plus tard. Une déconnexion et
  une interaction concurrentes du même environnement sont sérialisées.
- Fermeture brutale du navigateur : aucune interaction n'arrive ; la session
  reste ouverte jusqu'à finalize_idle_sessions (ou jusqu'à la prochaine
  interaction du même environnement), qui pose finalized_at sans rien
  inventer : la durée de la visite reste [opened_at, last_activity_at].
  Aucun événement n'est créé.

Concurrence : verrous consultatifs (environnement de la participation, clé
d'interaction) puis participation et campagne en partage, puis session
ouverte FOR UPDATE. Toute mutation a lieu À L'INTÉRIEUR d'un SAVEPOINT
(begin_nested() vide d'abord les changements en attente de la Session : une
mutation faite avant lui serait écrite hors SAVEPOINT) ; une violation de
contrainte annule ce seul SAVEPOINT et laisse la transaction de l'appelant
utilisable. Index unique partiel (une session ouverte par environnement) et
PK du registre : défense finale (-> ActivitySessionConflict). Jamais de
commit() ni de rollback() de la transaction de l'appelant.

Conservation : les lignes du registre ne contiennent qu'une clé aléatoire,
son type, l'empreinte d'environnement, la session et un instant serveur ;
elles suivent la conservation de leur participation (purge : lot 2C).
"""
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import (
    ProductActivitySession,
    ProductActivitySessionInteraction,
    ProductCampaign,
    ProductCampaignParticipant,
)
from core.product_analytics import campaigns, common
from core.product_analytics.common import (
    ACTIVE,
    ACTIVITY_SESSION_INACTIVITY,
    ENROLLED,
    ActivitySessionConflict,
    ActivitySessionRefused,
    ProductAnalyticsError,
)

ENVIRONMENT_DIGEST_NAMESPACE = b"oryx-v0t08-activity-environment-v1\x00"
OPEN_ENVIRONMENT_UNIQUE = "uq_product_activity_sessions_open_environment"
INTERACTION_PRIMARY_KEY = "product_activity_session_interactions_pkey"

INACTIVITY, LOGOUT = "inactivity", "logout"
END_REASONS = (INACTIVITY, LOGOUT)
ACTIVITY, CLOSE = "activity", "close"
INTERACTION_KINDS = (ACTIVITY, CLOSE)

OPENED, CONTINUED, REPLAYED = "opened", "continued", "replayed"
TOUCH_OUTCOMES = (OPENED, CONTINUED, REPLAYED)
CLOSED, EXPIRED, NO_OPEN_SESSION = "closed", "expired", "no_open_session"
CLOSE_OUTCOMES = (CLOSED, EXPIRED, NO_OPEN_SESSION, REPLAYED)
REFUSED, CONFLICT, FAILED = "refused", "conflict", "failed"
ATTEMPT_STATUSES = TOUCH_OUTCOMES + (CLOSED, EXPIRED, NO_OPEN_SESSION, REFUSED, CONFLICT, FAILED)


def environment_digest(environment_key: uuid.UUID) -> str:
    """Empreinte stockée de la clé d'environnement (jamais la clé brute)."""
    common.require_uuid(environment_key, "environment_key")
    return hashlib.sha256(ENVIRONMENT_DIGEST_NAMESPACE + environment_key.bytes).hexdigest()


@dataclass(frozen=True)
class SessionTouch:
    """session : la visite courante (ou d'origine, pour un rejeu) ; outcome :
    TOUCH_OUTCOMES ; finalized : session expirée du même environnement
    finalisée par cet appel."""
    session: ProductActivitySession
    outcome: str
    finalized: ProductActivitySession | None = None


@dataclass(frozen=True)
class SessionClose:
    """outcome : CLOSE_OUTCOMES ; session : visite clôturée ou finalisée
    (None si aucune visite n'était ouverte dans cet environnement)."""
    outcome: str
    session: ProductActivitySession | None = None


def _require_observable(participation: ProductCampaignParticipant, campaign: ProductCampaign, now: datetime) -> None:
    if participation.participation_status != ENROLLED:
        raise ActivitySessionRefused("participation inactive", reason="participation_inactive")
    if campaign.status != ACTIVE:
        raise ActivitySessionRefused(f"campagne à l'état {campaign.status}", reason="campaign_not_active")
    if now >= campaign.close_by_at:
        raise ActivitySessionRefused("échéance close_by_at atteinte", reason="campaign_closed")
    if now < participation.window_start_at:
        raise ActivitySessionRefused("avant le J0", reason="before_observation")
    if now >= participation.window_end_at:
        raise ActivitySessionRefused("fenêtre d'observation terminée", reason="observation_ended")


def _validate(campaign_id, user_id, environment_key, interaction_key) -> str:
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    digest = environment_digest(environment_key)
    common.require_uuid(interaction_key, "interaction_key")
    return digest


def _lock(db, campaign_id, user_id, digest, interaction_key):
    """Verrous consultatifs (environnement, clé), puis participation et
    campagne en partage (même ordre que les événements 2A). Refus sans
    participation."""
    common.lock_identities(db, common.identity_lock_key("activity_session", campaign_id, user_id, digest),
                           common.identity_lock_key("activity_interaction", campaign_id, user_id, interaction_key))
    enrolled = db.execute(
        select(ProductCampaignParticipant)
        .where(ProductCampaignParticipant.campaign_id == campaign_id, ProductCampaignParticipant.user_id == user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if enrolled is None:
        raise ActivitySessionRefused("aucune participation à cette campagne", reason="not_participant")
    return enrolled, campaigns.lock_campaign(db, campaign_id, shared=True)


def _accepted(db, campaign_id, user_id, interaction_key, *, kind: str,
              digest: str) -> ProductActivitySessionInteraction | None:
    """Clé déjà acceptée ? Même environnement et même type exigés."""
    seen = db.execute(select(ProductActivitySessionInteraction).where(
        ProductActivitySessionInteraction.campaign_id == campaign_id,
        ProductActivitySessionInteraction.user_id == user_id,
        ProductActivitySessionInteraction.interaction_key == interaction_key)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if seen is not None and (seen.environment_digest, seen.interaction_kind) != (digest, kind):
        raise ActivitySessionConflict(
            "clé d'interaction déjà utilisée dans un autre environnement ou pour un autre type d'interaction")
    return seen


def _session(db, session_id) -> ProductActivitySession | None:
    if session_id is None:
        return None
    return db.execute(select(ProductActivitySession).where(ProductActivitySession.id == session_id)
                      .execution_options(populate_existing=True)).scalar_one()


def _open_session(db, campaign_id, user_id, digest) -> ProductActivitySession | None:
    return db.execute(select(ProductActivitySession).where(
        ProductActivitySession.campaign_id == campaign_id, ProductActivitySession.user_id == user_id,
        ProductActivitySession.environment_digest == digest, ProductActivitySession.finalized_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()


def _write(db, apply) -> None:
    """Mutations ET flush à l'intérieur d'un même SAVEPOINT. Violation
    d'unicité -> ActivitySessionConflict ; toute autre IntegrityError est
    propagée ; dans les deux cas seul le SAVEPOINT est annulé."""
    try:
        with db.begin_nested():
            apply()
            db.flush()
    except IntegrityError as exc:
        for constraint in (OPEN_ENVIRONMENT_UNIQUE, INTERACTION_PRIMARY_KEY):
            if common.is_unique_violation(exc, constraint):
                raise ActivitySessionConflict(f"unicité de session occupée ({constraint})") from None
        raise


def _record(db, *, campaign_id, user_id, interaction_key, kind, digest, session_id, now) -> None:
    db.add(ProductActivitySessionInteraction(
        campaign_id=campaign_id, user_id=user_id, interaction_key=interaction_key, interaction_kind=kind,
        environment_digest=digest, session_id=session_id, recorded_at=now))


def touch_activity_session(
    db,
    *,
    campaign_id: uuid.UUID,
    user_id: str,
    environment_key: uuid.UUID,
    interaction_key: uuid.UUID,
) -> SessionTouch:
    """Interaction d'un participant dans un environnement : ouvre, prolonge
    ou rejoue sa visite. Lève ActivitySessionRefused (rien d'écrit) hors
    observation. Flush sans commit."""
    digest = _validate(campaign_id, user_id, environment_key, interaction_key)
    enrolled, campaign = _lock(db, campaign_id, user_id, digest, interaction_key)
    seen = _accepted(db, campaign_id, user_id, interaction_key, kind=ACTIVITY, digest=digest)
    if seen is not None:
        return SessionTouch(_session(db, seen.session_id), REPLAYED)
    current = _open_session(db, campaign_id, user_id, digest)
    now = common.utcnow()  # après TOUS les verrous
    _require_observable(enrolled, campaign, now)
    record = {"campaign_id": campaign_id, "user_id": user_id, "interaction_key": interaction_key, "kind": ACTIVITY,
              "digest": digest, "now": now}
    if current is not None and now < current.idle_expires_at:
        def extend():
            current.last_activity_at = now
            current.idle_expires_at = now + ACTIVITY_SESSION_INACTIVITY
            _record(db, session_id=current.id, **record)
        _write(db, extend)
        return SessionTouch(current, CONTINUED)
    session = ProductActivitySession(
        id=uuid.uuid4(), campaign_id=campaign_id, user_id=user_id, environment_digest=digest, opened_at=now,
        last_activity_at=now, idle_expires_at=now + ACTIVITY_SESSION_INACTIVITY, finalized_at=None, end_reason=None)

    def reopen():
        if current is not None:  # expirée : finalisée sans aucune activité inventée
            current.finalized_at = now
            current.end_reason = INACTIVITY
            db.flush()  # libère l'index partiel avant l'ouverture
        db.add(session)
        db.flush()
        _record(db, session_id=session.id, **record)
    _write(db, reopen)
    return SessionTouch(session, OPENED, current)


def close_activity_session(
    db,
    *,
    campaign_id: uuid.UUID,
    user_id: str,
    environment_key: uuid.UUID,
    interaction_key: uuid.UUID,
) -> SessionClose:
    """Déconnexion volontaire dans un environnement : clôt sa visite ouverte
    ('logout') sans rien inventer, ou la finalise ('inactivity') si elle
    était déjà expirée. N'exige pas l'observation en cours (aucune activité
    n'est créée) ; exige une participation. Flush sans commit."""
    digest = _validate(campaign_id, user_id, environment_key, interaction_key)
    _lock(db, campaign_id, user_id, digest, interaction_key)
    seen = _accepted(db, campaign_id, user_id, interaction_key, kind=CLOSE, digest=digest)
    if seen is not None:
        return SessionClose(REPLAYED, _session(db, seen.session_id))
    current = _open_session(db, campaign_id, user_id, digest)
    now = common.utcnow()  # après TOUS les verrous
    if current is None:
        outcome = NO_OPEN_SESSION
    elif now < current.idle_expires_at:
        outcome = CLOSED
    else:
        outcome = EXPIRED

    def close():
        if outcome == CLOSED:
            # Jamais avant la dernière activité (horloge serveur monotone attendue).
            current.finalized_at = max(now, current.last_activity_at)
            current.end_reason = LOGOUT
        elif outcome == EXPIRED:
            current.finalized_at = now
            current.end_reason = INACTIVITY
        _record(db, campaign_id=campaign_id, user_id=user_id, interaction_key=interaction_key, kind=CLOSE,
                digest=digest, session_id=current.id if current is not None else None, now=now)
    _write(db, close)
    return SessionClose(outcome, current)


def finalize_idle_sessions(db, *, campaign_id: uuid.UUID) -> list:
    """Finalise ('inactivity') les sessions ouvertes de la campagne dont
    l'inactivité a atteint trente minutes (fermeture brutale du navigateur
    comprise) : finalized_at = maintenant, aucun autre champ modifié, aucun
    événement. Fonctionne aussi après la clôture de la campagne. Retourne
    les sessions finalisées. Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    candidates = db.execute(
        select(ProductActivitySession)
        .where(ProductActivitySession.campaign_id == campaign_id, ProductActivitySession.finalized_at.is_(None),
               ProductActivitySession.idle_expires_at <= common.utcnow())
        .order_by(ProductActivitySession.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()
    now = common.utcnow()  # après les verrous : une prolongation concurrente est vue
    finalized = [s for s in candidates if s.finalized_at is None and s.idle_expires_at <= now]
    if finalized:
        def finalize():
            for session in finalized:
                session.finalized_at = now
                session.end_reason = INACTIVITY
        _write(db, finalize)
    return finalized


@dataclass(frozen=True)
class SessionAttempt:
    """Issue d'une interaction ou d'une clôture ; jamais une exception.
    status : ATTEMPT_STATUSES ; reason : raison fermée (refus) ou nom de
    classe d'erreur ; session_id : visite concernée (visit_id futur des
    événements 2B-2)."""
    status: str
    reason: str | None = None
    session_id: uuid.UUID | None = None


def _attempt(db, operation, **kwargs) -> SessionAttempt:
    try:
        with db.begin_nested():
            result = operation(db, **kwargs)
        return SessionAttempt(result.outcome, None, result.session.id if result.session is not None else None)
    except ActivitySessionRefused as exc:
        return SessionAttempt(REFUSED, exc.reason)
    except ProductAnalyticsError as exc:
        return SessionAttempt(CONFLICT, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — panne technique : jamais propagée à la requête métier
        return SessionAttempt(FAILED, type(exc).__name__)


def attempt_session_touch(db, *, campaign_id: uuid.UUID, user_id: str, environment_key: uuid.UUID,
                          interaction_key: uuid.UUID) -> SessionAttempt:
    """Contrat non bloquant de touch_activity_session : n'interrompt jamais
    la requête de l'appelant ; toute écriture est isolée dans un SAVEPOINT
    annulé en cas d'échec."""
    return _attempt(db, touch_activity_session, campaign_id=campaign_id, user_id=user_id,
                    environment_key=environment_key, interaction_key=interaction_key)


def attempt_session_close(db, *, campaign_id: uuid.UUID, user_id: str, environment_key: uuid.UUID,
                          interaction_key: uuid.UUID) -> SessionAttempt:
    """Contrat non bloquant de close_activity_session (future déconnexion
    F2, hors périmètre) : n'interrompt jamais la déconnexion."""
    return _attempt(db, close_activity_session, campaign_id=campaign_id, user_id=user_id,
                    environment_key=environment_key, interaction_key=interaction_key)
