"""Gestion des campagnes de mesure produit (V0-T08 Lot 2A) — DORMANTE.

Machine d'état EXPLICITE et fermée :

    planned --activate_campaign--> active --close_campaign--> closed
    planned --cancel_campaign----> cancelled
    active  --cancel_campaign----> cancelled

closed et cancelled sont terminaux : aucune transition n'en sort (aucune
réouverture). Aucune transition implicite : seule activate_campaign fait
passer une campagne à active ; create_campaign crée toujours planned ;
aucune campagne n'est créée par un autre service.

Politiques : retention_days, retention_policy_version et
measurement_policy_version sont explicites à la création (aucune valeur par
défaut), cohérentes avec le registre fermé (common.RETENTION_POLICIES), et
modifiables uniquement à l'état planned (revise_campaign_policies) ; dès
l'activation elles sont immuables.

Concurrence : chaque transition verrouille la campagne (SELECT ... FOR
UPDATE) avant de lire son statut ; deux transitions concurrentes sont donc
sérialisées et la seconde voit l'état produit par la première. Deux
créations concurrentes du même code sont sérialisées par un verrou
consultatif d'identité. Horloge (audit R1) : maintenant est lu APRÈS ces
verrous, jamais avant une attente. Le service
ne fait jamais commit() ni rollback() de la transaction de l'appelant ;
l'INSERT de create_campaign est isolé dans un SAVEPOINT pour traduire une
course perdue sur uq_product_campaigns_code.

Lot 2B-1 : une campagne portant une cohorte (product_campaign_cohorts) ne
peut être activée que par cohort.activate_cohort_campaign ; activate_campaign
la refuse (aucun contournement du contrôle de cohorte complète).
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import ProductCampaign, ProductCampaignCohort
from core.product_analytics import common
from core.product_analytics.common import (
    ACTIVE,
    CANCELLED,
    CLOSED,
    CODE_PATTERN,
    PLANNED,
    CampaignCodeConflict,
    CampaignNotFound,
    CampaignPolicyLocked,
    CampaignTransitionRefused,
    InvalidProductAnalyticsInput,
)

CAMPAIGN_CODE_UNIQUE = "uq_product_campaigns_code"
NAME_MAX_LENGTH = 160
PURPOSE_MAX_LENGTH = 2000

# Transitions autorisées : (état de départ, état d'arrivée).
TRANSITIONS = frozenset({(PLANNED, ACTIVE), (ACTIVE, CLOSED), (PLANNED, CANCELLED), (ACTIVE, CANCELLED)})


def lock_campaign(db, campaign_id: uuid.UUID, *, shared: bool = False) -> ProductCampaign:
    """SELECT ... FOR UPDATE (ou FOR SHARE) de la campagne ;
    populate_existing recharge un objet déjà présent dans la Session."""
    common.require_uuid(campaign_id, "campaign_id")
    campaign = db.execute(
        select(ProductCampaign)
        .where(ProductCampaign.id == campaign_id)
        .with_for_update(read=shared)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if campaign is None:
        raise CampaignNotFound(f"campagne {campaign_id} inconnue")
    return campaign


def create_campaign(
    db,
    *,
    code: str,
    name: str,
    purpose: str,
    close_by_at: datetime,
    retention_days: int,
    retention_policy_version: str,
    measurement_policy_version: str,
) -> ProductCampaign:
    """Crée une campagne planned. Tous les champs sont explicites (aucune
    valeur par défaut, pas même la durée Vague 0 : l'appelant transmet
    common.WAVE_0_POLICY s'il le souhaite). close_by_at doit être
    postérieure à created_at. Code déjà pris : CampaignCodeConflict.
    Flush sans commit."""
    common.require_pattern(code, CODE_PATTERN, "code")
    common.require_text(name, "name", max_length=NAME_MAX_LENGTH)
    common.require_text(purpose, "purpose", max_length=PURPOSE_MAX_LENGTH)
    close_by_at = common.require_aware(close_by_at, "close_by_at")
    common.validate_policy(retention_days=retention_days, retention_policy_version=retention_policy_version,
                           measurement_policy_version=measurement_policy_version)

    common.lock_identities(db, common.identity_lock_key("campaign_code", code))
    if db.execute(select(ProductCampaign.id).where(ProductCampaign.code == code)).scalar_one_or_none() is not None:
        raise CampaignCodeConflict(f"code de campagne {code} déjà utilisé")
    now = common.utcnow()  # après le verrou d'identité
    if close_by_at <= now:
        raise InvalidProductAnalyticsInput("close_by_at : doit être postérieure à la création")
    campaign = ProductCampaign(
        id=uuid.uuid4(), code=code, name=name, purpose=purpose, status=PLANNED, created_at=now,
        activated_at=None, closed_at=None, close_by_at=close_by_at, retention_days=retention_days,
        retention_policy_version=retention_policy_version, measurement_policy_version=measurement_policy_version,
    )
    try:
        with db.begin_nested():
            db.add(campaign)
            db.flush()
    except IntegrityError as exc:
        if common.is_unique_violation(exc, CAMPAIGN_CODE_UNIQUE):
            raise CampaignCodeConflict(f"code de campagne {code} déjà utilisé") from None
        raise
    return campaign


def revise_campaign_policies(
    db,
    *,
    campaign_id: uuid.UUID,
    retention_days: int,
    retention_policy_version: str,
    measurement_policy_version: str,
) -> ProductCampaign:
    """Remplace les politiques d'une campagne ENCORE planned (sous verrou).
    Après activation (active, closed, cancelled) : CampaignPolicyLocked, sans
    aucune écriture. Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    common.validate_policy(retention_days=retention_days, retention_policy_version=retention_policy_version,
                           measurement_policy_version=measurement_policy_version)
    campaign = lock_campaign(db, campaign_id)
    if campaign.status != PLANNED:
        raise CampaignPolicyLocked(f"campagne {campaign_id} : politiques immuables à l'état {campaign.status}")
    campaign.retention_days = retention_days
    campaign.retention_policy_version = retention_policy_version
    campaign.measurement_policy_version = measurement_policy_version
    db.flush()
    return campaign


def _transition(campaign: ProductCampaign, target: str) -> None:
    if (campaign.status, target) not in TRANSITIONS:
        raise CampaignTransitionRefused(f"campagne {campaign.id} : {campaign.status} -> {target} refusée")


def has_cohort(db, campaign_id: uuid.UUID) -> bool:
    """La campagne porte-t-elle une cohorte (lot 2B-1) ? Lecture seule. Une
    cohorte n'est créée qu'à l'état planned sous verrou exclusif de la
    campagne et n'est jamais supprimée : la réponse est stable dès que la
    campagne est verrouillée (partage ou exclusif) hors de l'état planned."""
    return db.execute(select(ProductCampaignCohort.campaign_id).where(
        ProductCampaignCohort.campaign_id == campaign_id)).scalar_one_or_none() is not None


def activate_campaign(db, *, campaign_id: uuid.UUID) -> ProductCampaign:
    """planned -> active (activated_at = maintenant), seulement avant
    close_by_at. Campagne à cohorte (lot 2B-1) : refus, l'activation passe
    exclusivement par cohort.activate_cohort_campaign (contrôle de la
    cohorte complète et du calendrier). Flush sans commit."""
    campaign = lock_campaign(db, campaign_id)
    _transition(campaign, ACTIVE)
    if has_cohort(db, campaign.id):
        raise CampaignTransitionRefused(
            f"campagne {campaign.id} : campagne à cohorte, activation par activate_cohort_campaign exigée")
    return apply_activation(db, campaign, now=common.utcnow())  # après le verrou de la campagne


def apply_activation(db, campaign: ProductCampaign, *, now: datetime) -> ProductCampaign:
    """planned -> active d'une campagne DÉJÀ verrouillée (FOR UPDATE) par
    l'appelant, avec `now` lu après tous ses verrous. Usage interne du
    paquet (activate_campaign, cohort.activate_cohort_campaign)."""
    _transition(campaign, ACTIVE)
    if now >= campaign.close_by_at:
        raise CampaignTransitionRefused(f"campagne {campaign.id} : échéance close_by_at dépassée")
    campaign.status = ACTIVE
    campaign.activated_at = now
    db.flush()
    return campaign


def _closing_time(campaign: ProductCampaign) -> datetime:
    """Fin effective : maintenant, bornée par close_by_at (au-delà de
    l'échéance, aucun événement n'a pu être accepté : la mesure a pris fin
    à close_by_at)."""
    return min(common.utcnow(), campaign.close_by_at)


def close_campaign(db, *, campaign_id: uuid.UUID) -> ProductCampaign:
    """active -> closed (closed_at = fin effective). Flush sans commit."""
    campaign = lock_campaign(db, campaign_id)
    _transition(campaign, CLOSED)
    campaign.status = CLOSED
    campaign.closed_at = _closing_time(campaign)
    db.flush()
    return campaign


def cancel_campaign(db, *, campaign_id: uuid.UUID) -> ProductCampaign:
    """planned ou active -> cancelled (closed_at = fin effective). Les
    participants et événements existants sont conservés (purge : lot 2C).
    Flush sans commit."""
    campaign = lock_campaign(db, campaign_id)
    _transition(campaign, CANCELLED)
    campaign.status = CANCELLED
    campaign.closed_at = _closing_time(campaign)
    db.flush()
    return campaign
