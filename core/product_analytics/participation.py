"""Participation aux campagnes de mesure (V0-T08 Lot 2A) — DORMANTE.

Inscription (enroll_participant) :

- utilisateur existant (aucune création implicite) ; campagne existante,
  verrouillée en partage (FOR SHARE) et à l'état planned ou active, avant
  son échéance close_by_at ;
- une seule participation par (campagne, utilisateur) : une répétition
  identique renvoie la participation existante (created=False) ; des
  données différentes lèvent ParticipationConflict ;
- fenêtre d'observation semi-ouverte [window_start_at, window_end_at)
  EXPLICITE et non vide, jamais rétroactive (window_start_at >= instant
  d'inscription et >= authorization_recorded_at), contenue dans la
  campagne (window_end_at <= close_by_at) ;
- authorization_recorded_at et notice_version sont EXIGÉS comme traces
  techniques de l'étape d'autorisation menée en amont. Ce ne sont jamais une
  preuve juridique de consentement : le service n'en déduit rien d'autre
  que leur présence et leur cohérence temporelle.

Retrait (withdraw_participant) : enrolled -> withdrawn, sous verrou. La
ligne est conservée (dénominateur des rapports futurs) ; aucun événement
n'est accepté après le retrait.

Aucune fonction de suppression : un participant sans événement reste
inscrit et compte dans les dénominateurs des rapports futurs (lot 2C).
Flush sans commit ; seul l'INSERT est isolé dans un SAVEPOINT.
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import ProductCampaignParticipant
from core.product_analytics import common
from core.product_analytics.campaigns import lock_campaign
from core.product_analytics.common import (
    ACTIVE,
    ENROLLED,
    NOTICE_VERSION_PATTERN,
    PLANNED,
    WITHDRAWN,
    InvalidProductAnalyticsInput,
    ParticipationConflict,
    ParticipationNotFound,
    ParticipationRefused,
)

PARTICIPANT_PRIMARY_KEY = "product_campaign_participants_pkey"
ENROLLABLE_CAMPAIGN_STATUSES = (PLANNED, ACTIVE)


def _participation(db, campaign_id, user_id, *, lock: bool = False) -> ProductCampaignParticipant | None:
    statement = select(ProductCampaignParticipant).where(
        ProductCampaignParticipant.campaign_id == campaign_id, ProductCampaignParticipant.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement.execution_options(populate_existing=True)).scalar_one_or_none()


def _same_or_conflict(existing: ProductCampaignParticipant, request: dict) -> ProductCampaignParticipant:
    if (existing.participation_status != ENROLLED
            or any(getattr(existing, field) != value for field, value in request.items())):
        # Données différentes, ou participation déjà retirée / terminée :
        # jamais de réinscription ni de remplacement silencieux.
        raise ParticipationConflict(
            f"participation existante incompatible pour la campagne {existing.campaign_id}")
    return existing


def enroll_participant(
    db,
    *,
    campaign_id: uuid.UUID,
    user_id: str,
    window_start_at: datetime,
    window_end_at: datetime,
    authorization_recorded_at: datetime,
    notice_version: str,
) -> tuple[ProductCampaignParticipant, bool]:
    """Inscrit user_id à la campagne. Retourne (participation, created)."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    window_start_at = common.require_aware(window_start_at, "window_start_at")
    window_end_at = common.require_aware(window_end_at, "window_end_at")
    authorization_recorded_at = common.require_aware(authorization_recorded_at, "authorization_recorded_at")
    common.require_pattern(notice_version, NOTICE_VERSION_PATTERN, "notice_version")
    if window_end_at <= window_start_at:
        raise InvalidProductAnalyticsInput("window_end_at : la fenêtre d'observation doit être non vide")
    if authorization_recorded_at > window_start_at:
        raise InvalidProductAnalyticsInput("window_start_at : antérieure à l'autorisation enregistrée")
    request = {"window_start_at": window_start_at, "window_end_at": window_end_at,
               "authorization_recorded_at": authorization_recorded_at, "notice_version": notice_version}

    common.require_existing_user(db, user_id)
    existing = _participation(db, campaign_id, user_id)
    if existing is not None:
        return _same_or_conflict(existing, request), False

    campaign = lock_campaign(db, campaign_id, shared=True)
    if campaign.status not in ENROLLABLE_CAMPAIGN_STATUSES:
        raise ParticipationRefused(f"campagne {campaign_id} : inscription impossible à l'état {campaign.status}")
    now = common.utcnow()
    if now >= campaign.close_by_at:
        raise ParticipationRefused(f"campagne {campaign_id} : échéance close_by_at dépassée")
    if authorization_recorded_at > now:
        raise ParticipationRefused("authorization_recorded_at : postérieure à l'inscription")
    if window_start_at < now:
        raise ParticipationRefused("window_start_at : fenêtre d'observation rétroactive")
    if window_end_at > campaign.close_by_at:
        raise ParticipationRefused("window_end_at : postérieure à l'échéance de la campagne")

    participation = ProductCampaignParticipant(
        campaign_id=campaign_id, user_id=user_id, participation_status=ENROLLED, enrolled_at=now, **request)
    try:
        with db.begin_nested():
            db.add(participation)
            db.flush()
    except IntegrityError as exc:
        if not common.is_unique_violation(exc, PARTICIPANT_PRIMARY_KEY):
            raise
        winner = _participation(db, campaign_id, user_id)
        if winner is None:
            raise
        return _same_or_conflict(winner, request), False
    return participation, True


def withdraw_participant(db, *, campaign_id: uuid.UUID, user_id: str) -> ProductCampaignParticipant:
    """enrolled -> withdrawn (sous verrou) ; withdrawn : no-op idempotent ;
    completed : ParticipationRefused. La ligne est conservée."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    participation = _participation(db, campaign_id, user_id, lock=True)
    if participation is None:
        raise ParticipationNotFound(f"aucune participation à la campagne {campaign_id}")
    if participation.participation_status == WITHDRAWN:
        return participation
    if participation.participation_status != ENROLLED:
        raise ParticipationRefused(
            f"campagne {campaign_id} : retrait impossible à l'état {participation.participation_status}")
    participation.participation_status = WITHDRAWN
    db.flush()
    return participation
