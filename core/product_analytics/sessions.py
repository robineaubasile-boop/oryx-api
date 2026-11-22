"""Sessions d'activité produit (visites) des participants (V0-T08 Lot 2B-1)
— DORMANTES.

Table product_activity_sessions (migration 0023). DISTINCTES des sessions
d'authentification F2 : aucune lecture d'oryx_sessions, aucune durée
commune (15 jours absolus côté F2 ; 30 minutes d'inactivité ici), aucune
FK. Aucune route, aucun raccordement ; aucun événement n'est écrit ici
(session_started et activités métier : sous-lot 2B-2).

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
  finalisation) utilisent l'horloge serveur lue APRÈS les verrous ;
  l'appelant ne fournit aucune date.
- Inactivité : une session est vivante sur [last_activity_at,
  last_activity_at + 30 min) (idle_expires_at, imposé en base). Une
  interaction strictement avant idle_expires_at la prolonge ; à
  idle_expires_at ou après, elle est finalisée et une nouvelle visite
  s'ouvre.
- Observation : ouverture et prolongation seulement si la participation est
  enrolled, la campagne active et avant close_by_at, et maintenant dans
  [window_start_at, window_end_at). Après J+12 : refus, aucune session
  ouverte ni prolongée (aucune session ne prolonge les douze jours ;
  last_activity_at < window_end_at toujours).
- Rejeu : une interaction est identifiée par interaction_key. La clé qui a
  ouvert une session la désigne pour toujours (UNIQUE par participation) :
  son rejeu renvoie cette session, même finalisée, sans écriture ni
  nouvelle visite. Le rejeu de la dernière interaction d'une session ouverte
  est aussi sans écriture. Limite : le rejeu d'une interaction plus
  ancienne, autre que d'ouverture, se comporte comme une interaction neuve
  (prolongation si la session vit encore) ; la déduplication complète des
  interactions relève du registre d'événements (2B-2).
- Fermeture brutale du navigateur : aucune interaction n'arrive ; la session
  reste ouverte jusqu'à finalize_idle_sessions (ou jusqu'à la prochaine
  interaction du même environnement), qui pose finalized_at sans rien
  inventer : la durée de la visite reste [opened_at, last_activity_at].
  Aucun événement n'est créé.

Concurrence : verrous consultatifs (environnement de la participation, clé
d'interaction) puis participation et campagne en partage, puis session
ouverte FOR UPDATE ; deux onglets concurrents d'un même environnement
partagent donc une seule visite. L'index unique partiel (une session ouverte
par environnement) et l'UNIQUE de la clé d'ouverture restent la défense
finale (SAVEPOINT -> ActivitySessionConflict). Jamais de commit() ni de
rollback() de la transaction de l'appelant.
"""
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import ProductActivitySession, ProductCampaign, ProductCampaignParticipant
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
OPENING_KEY_UNIQUE = "uq_product_activity_sessions_opening_key"
OPEN_ENVIRONMENT_UNIQUE = "uq_product_activity_sessions_open_environment"

OPENED, CONTINUED, REPLAYED = "opened", "continued", "replayed"
TOUCH_OUTCOMES = (OPENED, CONTINUED, REPLAYED)
REFUSED, CONFLICT, FAILED = "refused", "conflict", "failed"
ATTEMPT_STATUSES = TOUCH_OUTCOMES + (REFUSED, CONFLICT, FAILED)


def environment_digest(environment_key: uuid.UUID) -> str:
    """Empreinte stockée de la clé d'environnement (jamais la clé brute)."""
    common.require_uuid(environment_key, "environment_key")
    return hashlib.sha256(ENVIRONMENT_DIGEST_NAMESPACE + environment_key.bytes).hexdigest()


@dataclass(frozen=True)
class SessionTouch:
    """session : la visite courante ; outcome : TOUCH_OUTCOMES ; finalized :
    session expirée du même environnement finalisée par cet appel."""
    session: ProductActivitySession
    outcome: str
    finalized: ProductActivitySession | None = None


def _require_observable(participation: ProductCampaignParticipant | None, campaign: ProductCampaign | None,
                        now: datetime) -> None:
    if participation is None or campaign is None:
        raise ActivitySessionRefused("aucune participation à cette campagne", reason="not_participant")
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


def _lock_participation(db, campaign_id, user_id):
    """Participation puis campagne, en partage (même ordre que les
    événements 2A). (None, None) si aucune participation."""
    found = db.execute(
        select(ProductCampaignParticipant)
        .where(ProductCampaignParticipant.campaign_id == campaign_id, ProductCampaignParticipant.user_id == user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if found is None:
        return None, None
    return found, campaigns.lock_campaign(db, campaign_id, shared=True)


def _by_opening_key(db, campaign_id, user_id, interaction_key) -> ProductActivitySession | None:
    return db.execute(select(ProductActivitySession).where(
        ProductActivitySession.campaign_id == campaign_id, ProductActivitySession.user_id == user_id,
        ProductActivitySession.opening_interaction_key == interaction_key)
        .execution_options(populate_existing=True)).scalar_one_or_none()


def _open_session(db, campaign_id, user_id, digest) -> ProductActivitySession | None:
    return db.execute(select(ProductActivitySession).where(
        ProductActivitySession.campaign_id == campaign_id, ProductActivitySession.user_id == user_id,
        ProductActivitySession.environment_digest == digest, ProductActivitySession.finalized_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()


def _flush(db, objects=()) -> None:
    try:
        with db.begin_nested():
            for item in objects:
                db.add(item)
            db.flush()
    except IntegrityError as exc:
        for constraint in (OPENING_KEY_UNIQUE, OPEN_ENVIRONMENT_UNIQUE):
            if common.is_unique_violation(exc, constraint):
                raise ActivitySessionConflict(f"unicité de session occupée ({constraint})") from None
        raise


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
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    digest = environment_digest(environment_key)
    common.require_uuid(interaction_key, "interaction_key")

    common.lock_identities(db, common.identity_lock_key("activity_session", campaign_id, user_id, digest),
                           common.identity_lock_key("activity_interaction", campaign_id, user_id, interaction_key))
    enrolled, campaign = _lock_participation(db, campaign_id, user_id)
    if enrolled is None:
        raise ActivitySessionRefused("aucune participation à cette campagne", reason="not_participant")
    replayed = _by_opening_key(db, campaign_id, user_id, interaction_key)
    if replayed is not None:
        if replayed.environment_digest != digest:
            raise ActivitySessionConflict("clé d'ouverture déjà utilisée dans un autre environnement")
        return SessionTouch(replayed, REPLAYED)
    current = _open_session(db, campaign_id, user_id, digest)
    now = common.utcnow()  # après TOUS les verrous
    if current is not None and now < current.idle_expires_at:
        if current.last_interaction_key == interaction_key:
            return SessionTouch(current, REPLAYED)
        _require_observable(enrolled, campaign, now)
        current.last_activity_at = now
        current.idle_expires_at = now + ACTIVITY_SESSION_INACTIVITY
        current.last_interaction_key = interaction_key
        _flush(db)
        return SessionTouch(current, CONTINUED)
    _require_observable(enrolled, campaign, now)
    finalized = None
    if current is not None:  # expirée : finalisée sans aucune activité inventée
        current.finalized_at = now
        finalized = current
    session = ProductActivitySession(
        id=uuid.uuid4(), campaign_id=campaign_id, user_id=user_id, environment_digest=digest,
        opening_interaction_key=interaction_key, last_interaction_key=interaction_key, opened_at=now,
        last_activity_at=now, idle_expires_at=now + ACTIVITY_SESSION_INACTIVITY, finalized_at=None)
    if finalized is not None:
        _flush(db)  # la finalisation libère l'index partiel avant l'ouverture
    _flush(db, [session])
    return SessionTouch(session, OPENED, finalized)


def finalize_idle_sessions(db, *, campaign_id: uuid.UUID) -> list:
    """Finalise les sessions ouvertes de la campagne dont l'inactivité a
    atteint trente minutes (fermeture brutale du navigateur comprise) :
    finalized_at = maintenant, aucun autre champ modifié, aucun événement.
    Fonctionne aussi après la clôture de la campagne. Retourne les sessions
    finalisées. Flush sans commit."""
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
    for session in finalized:
        session.finalized_at = now
    if finalized:
        db.flush()
    return finalized


@dataclass(frozen=True)
class SessionAttempt:
    """Issue d'une interaction ; jamais une exception. status :
    ATTEMPT_STATUSES ; reason : raison fermée (refus) ou nom de classe
    d'erreur ; session_id : visite courante (visit_id futur des
    événements 2B-2)."""
    status: str
    reason: str | None = None
    session_id: uuid.UUID | None = None


def attempt_session_touch(db, *, campaign_id: uuid.UUID, user_id: str, environment_key: uuid.UUID,
                          interaction_key: uuid.UUID) -> SessionAttempt:
    """Contrat non bloquant : n'interrompt jamais la requête de l'appelant ;
    toute écriture est isolée dans un SAVEPOINT annulé en cas d'échec."""
    try:
        with db.begin_nested():
            touch = touch_activity_session(db, campaign_id=campaign_id, user_id=user_id,
                                           environment_key=environment_key, interaction_key=interaction_key)
        return SessionAttempt(touch.outcome, None, touch.session.id)
    except ActivitySessionRefused as exc:
        return SessionAttempt(REFUSED, exc.reason)
    except ProductAnalyticsError as exc:
        return SessionAttempt(CONFLICT, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — panne technique : jamais propagée à la requête métier
        return SessionAttempt(FAILED, type(exc).__name__)
