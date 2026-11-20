"""Participation aux campagnes de mesure (V0-T08 Lot 2A) — DORMANTE.

Deux points d'inscription, mêmes garde-fous (utilisateur existant, verrou
d'identité, campagne verrouillée en partage, horloge lue après les verrous,
une seule participation par couple, autorisation technique exigée) :

- enroll_at_first_eligible_login : chemin Vague 0 destiné au futur
  raccordement (2B). L'appelant ne fournit AUCUNE date de fenêtre : le J0
  individuel est l'horloge serveur lue APRÈS les verrous, dans la
  transaction de la première connexion authentifiée et éligible ; la
  fenêtre vaut exactement [J0, J0 + durée d'observation de la politique de
  mesure de la campagne) — 12 jours pour v0-measurement-v1 ;
- enroll_participant : fenêtre explicite fournie par l'appelant, réservée
  aux scénarios explicitement contrôlés (outillage, tests) ; inchangée.

Inscription à fenêtre explicite (enroll_participant) :

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

Concurrence et horloge (audit R1) : deux inscriptions concurrentes du même
couple (campagne, utilisateur) sont sérialisées par un verrou consultatif
d'identité ; la campagne est ensuite verrouillée en partage ; maintenant est
lu APRÈS ces deux verrous, de sorte qu'une échéance (close_by_at) ou un début
de fenêtre franchi pendant une attente de verrou est vu et refusé.

Première connexion éligible (enroll_at_first_eligible_login) :

- campagne ACTIVE exigée (pas planned) : un J0 pris avant l'activation
  amputerait la fenêtre de jours pendant lesquels aucun événement ne peut
  être accepté ; la connexion n'est alors pas éligible (refus, rien
  d'écrit) et une connexion ultérieure, campagne active, deviendra le J0 ;
- J0 = utcnow() lu après le verrou d'identité et le verrou partagé de la
  campagne ; window_start_at = enrolled_at = J0 ; window_end_at = J0 +
  OBSERVATION_WINDOWS[measurement_policy_version], exactement, sans marge
  arbitraire ; politique de mesure sans fenêtre déclarée : refus ;
- J0 + durée > close_by_at : refus (ParticipationRefused), sans fenêtre
  tronquée et sans jamais prolonger la campagne (aucun service ne modifie
  close_by_at après la création) ;
- authorization_recorded_at <= J0 exigé ; ni lui ni notice_version ne valent
  consentement juridique ;
- connexion répétée, ou première connexion concurrente perdante (sérialisée
  par le verrou d'identité) : la participation existante est renvoyée TELLE
  QUELLE (created=False, même J0, même fenêtre, aucune écriture), y compris
  après la fin de la fenêtre ; aucune réinitialisation. Participation retirée
  ou terminée, ou trace d'autorisation différente : ParticipationConflict.

Préconditions pour le raccordement 2B (non vérifiables ici, à garantir par
l'orchestrateur) :

1. close_by_at est fixée une fois pour toutes à la création. Pour que des
   testeurs démarrant à des dates différentes disposent tous de 12 jours
   complets, la campagne doit être créée avec close_by_at >= (dernier J0
   admis) + 12 jours (+ la marge de remise des sessions que 2B définira) ;
   tout J0 au-delà de close_by_at − 12 jours est refusé. Une campagne active
   n'est jamais prolongée : un recrutement plus tardif exige une décision et,
   le cas échéant, une nouvelle campagne.
2. La campagne doit être ACTIVE avant la première connexion éligible du
   premier testeur.
3. L'appel doit avoir lieu dans la transaction de la requête authentifiée
   (identité établie par la frontière F2), avant tout appel externe ; le
   service ne lit aucune session et ne connaît aucun principal.
4. L'éligibilité Vague 0 (qui est testeur) et la trace d'autorisation
   (authorization_recorded_at, notice_version) sont établies en amont ; la
   même trace doit être transmise à chaque connexion.
5. La règle de session (30 minutes d'inactivité) et la remise d'événements à
   la clôture d'une session commencée avant l'échéance relèvent de 2B : rien
   ici ne les implémente (voir events.LATE_EVENT_GRACE).

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
    OBSERVATION_WINDOWS,
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

    existing = _lock_identity_and_read(db, campaign_id, user_id)
    if existing is not None:
        return _same_or_conflict(existing, request), False

    campaign = lock_campaign(db, campaign_id, shared=True)
    if campaign.status not in ENROLLABLE_CAMPAIGN_STATUSES:
        raise ParticipationRefused(f"campagne {campaign_id} : inscription impossible à l'état {campaign.status}")
    now = common.utcnow()  # après le verrou d'identité et le verrou de la campagne
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
    return _insert(db, participation, lambda winner: _same_or_conflict(winner, request))


def _lock_identity_and_read(db, campaign_id, user_id) -> ProductCampaignParticipant | None:
    """Utilisateur existant, puis verrou consultatif d'identité (campagne,
    utilisateur), puis relecture de la participation validée."""
    common.require_existing_user(db, user_id)
    common.lock_identities(db, common.identity_lock_key("participation", campaign_id, user_id))
    return _participation(db, campaign_id, user_id)


def _insert(db, participation: ProductCampaignParticipant, on_existing) -> tuple[ProductCampaignParticipant, bool]:
    """INSERT isolé dans un SAVEPOINT. Course perdue sur la PK (écrivain non
    sérialisé uniquement) : relecture de la ligne gagnante et on_existing."""
    try:
        with db.begin_nested():
            db.add(participation)
            db.flush()
    except IntegrityError as exc:
        if not common.is_unique_violation(exc, PARTICIPANT_PRIMARY_KEY):
            raise
        winner = _participation(db, participation.campaign_id, participation.user_id)
        if winner is None:
            raise
        return on_existing(winner), False
    return participation, True


def _same_login_or_conflict(existing: ProductCampaignParticipant, authorization: dict) -> ProductCampaignParticipant:
    """Connexion répétée : participation renvoyée telle quelle (fenêtre
    jamais recalculée) si elle est enrolled et porte la même trace
    d'autorisation ; ParticipationConflict sinon."""
    if (existing.participation_status != ENROLLED
            or any(getattr(existing, field) != value for field, value in authorization.items())):
        raise ParticipationConflict(
            f"participation existante incompatible pour la campagne {existing.campaign_id}")
    return existing


def enroll_at_first_eligible_login(
    db,
    *,
    campaign_id: uuid.UUID,
    user_id: str,
    authorization_recorded_at: datetime,
    notice_version: str,
) -> tuple[ProductCampaignParticipant, bool]:
    """Inscription Vague 0 à la première connexion authentifiée et éligible.
    J0 serveur fiable lu après les verrous, fenêtre [J0, J0 + 12 j) exacte.
    Retourne (participation, created) ; voir la docstring du module."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    authorization_recorded_at = common.require_aware(authorization_recorded_at, "authorization_recorded_at")
    common.require_pattern(notice_version, NOTICE_VERSION_PATTERN, "notice_version")
    authorization = {"authorization_recorded_at": authorization_recorded_at, "notice_version": notice_version}

    existing = _lock_identity_and_read(db, campaign_id, user_id)
    if existing is not None:
        return _same_login_or_conflict(existing, authorization), False

    campaign = lock_campaign(db, campaign_id, shared=True)
    if campaign.status != ACTIVE:
        raise ParticipationRefused(f"campagne {campaign_id} : première connexion non éligible à l'état "
                                   f"{campaign.status}")
    observation = OBSERVATION_WINDOWS.get(campaign.measurement_policy_version)
    if observation is None:
        raise ParticipationRefused(f"campagne {campaign_id} : aucune fenêtre d'observation pour sa politique")
    j0 = common.utcnow()  # J0 serveur : après le verrou d'identité et le verrou de la campagne
    if authorization_recorded_at > j0:
        raise ParticipationRefused("authorization_recorded_at : postérieure à la première connexion")
    window_end_at = j0 + observation
    if window_end_at > campaign.close_by_at:
        raise ParticipationRefused(f"campagne {campaign_id} : close_by_at ne couvre pas la fenêtre complète")

    participation = ProductCampaignParticipant(
        campaign_id=campaign_id, user_id=user_id, participation_status=ENROLLED, enrolled_at=j0,
        window_start_at=j0, window_end_at=window_end_at, **authorization)
    return _insert(db, participation, lambda winner: _same_login_or_conflict(winner, authorization))


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
