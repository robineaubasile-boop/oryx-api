"""Cohorte Vague 0 pré-J0, calendrier d'admission et J0 individuel (V0-T08
Lot 2B-1) — DORMANTE.

Tables product_campaign_cohorts et product_cohort_seats (migration 0023).
Aucune route, aucun raccordement : ces services ne sont appelés par aucun
module applicatif (tests de dormance). Ils ne font jamais commit() ni
rollback() de la transaction de l'appelant.

Cycle d'une campagne à cohorte
------------------------------

1. define_cohort (campagne planned, verrou exclusif) : politique de cohorte
   (v0-cohort-v1 : cinq places exactement) et date limite d'admission
   admission_closes_at, EXPLICITE et IMMUABLE (aucun service ne la modifie ;
   une répétition identique est idempotente, une valeur différente est un
   CohortConflict). Exigences : admission_closes_at postérieure à
   maintenant ; close_by_at >= admission_closes_at + fenêtre d'observation de
   la politique de mesure (12 jours) + finalisation de session (30 minutes)
   (common.required_close_by_at) ; aucune participation déjà présente. La
   campagne n'est jamais prolongée pour s'adapter : close_by_at est fixée à
   sa création.

2. add_cohort_seat (campagne planned, verrou exclusif) : une place par
   invitation F2 sélectionnée, référencée par son identifiant canonique
   (invitations.id) ; aucune copie d'e-mail, de nom ni de token. Numéro
   attribué séquentiellement (1 à 5, plafond aussi imposé en base).
   Invitation révoquée ou expirée sans activation : refus. Même invitation
   répétée : la place existante est renvoyée (aucune écriture). Il n'existe
   AUCUNE fonction de retrait, de remplacement ni de réattribution de
   place : une erreur de sélection impose d'annuler la campagne et d'en créer
   une autre (cohorte fixe, sans remplacement).

3. activate_cohort_campaign : seul chemin d'activation d'une campagne à
   cohorte (campaigns.activate_campaign la refuse). Refus si la cohorte est
   absente, incomplète (autre chose que les places 1..5), incohérente
   (politique inconnue, invitation révoquée ou expirée sans activation,
   participation préexistante) ou si le calendrier ne tient plus
   (maintenant >= admission_closes_at, ou close_by_at incompatible).

4. bind_seat_to_activated_invitation (campagne planned ou active, avant la
   date limite) : rattache une place à l'utilisateur DÉDUIT de la preuve F2
   canonique — l'unique auth_operation 'invitation_activation' COMMITTED de
   l'invitation de la place (index uq_auth_operations_one_committed_invitation
   de 0017), invitation à l'état 'accepted'. Le service ne reçoit AUCUN
   user_id : aucune valeur du navigateur ne peut choisir l'utilisateur. Les
   FK composites de 0023 revérifient en base que l'opération est bien
   l'activation committed de CETTE invitation pour CET utilisateur.
   Rattachement immuable : une place déjà rattachée ailleurs, ou un
   utilisateur occupant déjà une autre place, lève CohortConflict.

5. record_seat_authorization (campagne planned ou active, avant la date
   limite) : trace d'autorisation EXPLICITE de participation
   (authorization_recorded_at = horloge serveur, notice_version), distincte
   du rattachement et de toute connexion : elle n'est jamais déduite d'une
   activation d'invitation ni d'une authentification. Exige une place
   rattachée à user_id. Immuable (même notice : idempotent ; autre notice :
   CohortConflict). Champ technique, jamais une preuve juridique de
   consentement.

6. admit_at_first_eligible_login : J0 individuel. Préconditions ajoutées au
   contrat 2A (campagne à cohorte ; place rattachée à user_id ; autorisation
   enregistrée ; campagne ACTIVE ; J0 < admission_closes_at ; aucune
   participation contradictoire), puis réutilisation TELLE QUELLE du corps de
   participation.enroll_at_first_eligible_login (first_eligible_login,
   via_cohort=True) : verrou d'identité, J0 = horloge serveur lue après les
   verrous, fenêtre [J0, J0 + 12 j) exacte, idempotence et contrat R4. Le J0
   effectivement écrit est revérifié contre la date limite ; s'il l'atteint,
   le SAVEPOINT est annulé (aucun J0 au-delà de la limite). Connexion répétée
   : la participation existante est renvoyée telle quelle (même J0, même
   fenêtre), y compris après la date limite, après J+12 ou après la clôture.

7. attempt_first_login_admission : contrat NON BLOQUANT destiné au futur
   raccordement (2B-2). Ne lève jamais d'Exception : refus métier ->
   not_eligible / conflict ; panne technique -> failed, SAVEPOINT annulé,
   aucun J0 écrit et AUCUN J0 antidaté à la reprise (le J0 est toujours
   l'horloge serveur de la tentative qui réussit). La première panne d'une
   place autorisée est marquée (admission_failure_first_at, au mieux, dans
   un SAVEPOINT distinct) ; le J0 établi ensuite est signalé incertain
   (j0_uncertain). L'accès normal à Oryx n'est jamais bloqué par ce
   contrat : l'appelant ignore simplement l'issue.

Concurrence et horloge (convention 2A) : verrous consultatifs d'identité
puis campagne (FOR SHARE ou FOR UPDATE) puis place, toujours dans cet ordre ;
maintenant est lu APRÈS tous les verrous dont dépend la décision. INSERT et
UPDATE isolés dans des SAVEPOINT pour traduire une course perdue sur une
contrainte UNIQUE (défense finale) en CohortConflict.

Dépendance F2 : ces services LISENT invitations et auth_operations (aucune
écriture, aucun verrou de ligne, aucun module F2 importé). F2 étant dormant,
aucune activation committed n'existe en production : l'admission réelle
reste inactive tant que F2 et la Vague 0 ne sont pas activés par décision
séparée.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import (
    AuthOperation,
    Invitation,
    ProductCampaign,
    ProductCampaignCohort,
    ProductCampaignParticipant,
    ProductCohortSeat,
)
from core.product_analytics import campaigns, common, participation
from core.product_analytics.common import (
    ACTIVE,
    COHORT_POLICIES,
    NOTICE_VERSION_PATTERN,
    PLANNED,
    AdmissionRefused,
    CohortConflict,
    CohortNotFound,
    CohortRefused,
    CohortSeatNotFound,
    ParticipationConflict,
    ProductAnalyticsError,
)

COHORT_PRIMARY_KEY = "product_campaign_cohorts_pkey"
SEAT_NUMBER_UNIQUE = "uq_product_cohort_seats_campaign_seat_number"
SEAT_INVITATION_UNIQUE = "uq_product_cohort_seats_campaign_invitation"
SEAT_USER_UNIQUE = "uq_product_cohort_seats_campaign_user"
SEAT_UNIQUES = (SEAT_NUMBER_UNIQUE, SEAT_INVITATION_UNIQUE, SEAT_USER_UNIQUE)

# Preuve F2 canonique du lien invitation -> utilisateur (migration 0017).
INVITATION_ACTIVATION = "invitation_activation"
COMMITTED = "committed"
INVITATION_ACCEPTED, INVITATION_PENDING, INVITATION_REVOKED = "accepted", "pending", "revoked"

# Campagnes dans lesquelles une place peut encore être rattachée / autorisée.
OPEN_CAMPAIGN_STATUSES = (PLANNED, ACTIVE)

# Issues du contrat non bloquant (vocabulaire fermé).
ADMITTED, ALREADY_ADMITTED, NOT_ELIGIBLE, CONFLICT, FAILED = (
    "admitted", "already_admitted", "not_eligible", "conflict", "failed")
ATTEMPT_STATUSES = (ADMITTED, ALREADY_ADMITTED, NOT_ELIGIBLE, CONFLICT, FAILED)


# --------------------------------------------------------------------------
# Lectures
# --------------------------------------------------------------------------

def _cohort(db, campaign_id) -> ProductCampaignCohort | None:
    return db.execute(select(ProductCampaignCohort).where(ProductCampaignCohort.campaign_id == campaign_id)
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def _require_cohort(db, campaign_id) -> ProductCampaignCohort:
    cohort = _cohort(db, campaign_id)
    if cohort is None:
        raise CohortNotFound(f"campagne {campaign_id} : aucune cohorte définie")
    return cohort


def _seats(db, campaign_id, *, lock_shared: bool = False) -> list:
    statement = (select(ProductCohortSeat).where(ProductCohortSeat.campaign_id == campaign_id)
                 .order_by(ProductCohortSeat.seat_number))
    if lock_shared:
        statement = statement.with_for_update(read=True)
    return db.execute(statement.execution_options(populate_existing=True)).scalars().all()


def _seat_where(db, *criteria, lock: str | None = None) -> ProductCohortSeat | None:
    statement = select(ProductCohortSeat).where(*criteria)
    if lock is not None:
        statement = statement.with_for_update(read=lock == "share")
    return db.execute(statement.execution_options(populate_existing=True)).scalar_one_or_none()


def _has_participations(db, campaign_id) -> bool:
    return db.execute(select(ProductCampaignParticipant.user_id).where(
        ProductCampaignParticipant.campaign_id == campaign_id).limit(1)).first() is not None


def _invitation(db, invitation_id) -> Invitation | None:
    """Lecture SEULE de l'invitation F2 (aucun verrou)."""
    return db.execute(select(Invitation).where(Invitation.id == invitation_id)).scalar_one_or_none()


def _committed_activation(db, invitation_id) -> AuthOperation | None:
    """Preuve F2 canonique : l'unique activation committed de l'invitation
    (unicité garantie par uq_auth_operations_one_committed_invitation).
    Lecture SEULE, aucun verrou."""
    return db.execute(select(AuthOperation).where(
        AuthOperation.invitation_id == invitation_id, AuthOperation.kind == INVITATION_ACTIVATION,
        AuthOperation.status == COMMITTED)).scalar_one_or_none()


def _invitation_usable(invitation: Invitation | None, now: datetime) -> bool:
    """Invitation encore capable de produire (ou ayant produit) une
    activation : acceptée, ou en attente et non expirée."""
    if invitation is None or invitation.status == INVITATION_REVOKED:
        return False
    return invitation.status == INVITATION_ACCEPTED or (
        invitation.status == INVITATION_PENDING and now < invitation.expires_at)


def _check_calendar(campaign: ProductCampaign, admission_closes_at: datetime, now: datetime) -> None:
    if admission_closes_at <= now:
        raise CohortRefused(f"campagne {campaign.id} : date limite d'admission atteinte", reason="admission_closed")
    required = common.required_close_by_at(admission_closes_at, campaign.measurement_policy_version)
    if required is None or campaign.close_by_at < required:
        raise CohortRefused(
            f"campagne {campaign.id} : close_by_at ne couvre pas le dernier J0, la fenêtre d'observation et la "
            "finalisation", reason="calendar_incompatible")


def _flush_or_conflict(db, objects=()) -> None:
    """flush dans un SAVEPOINT ; violation d'une unicité de cohorte ->
    CohortConflict (le SAVEPOINT annulé laisse la transaction utilisable)."""
    try:
        with db.begin_nested():
            for item in objects:
                db.add(item)
            db.flush()
    except IntegrityError as exc:
        for constraint in SEAT_UNIQUES + (COHORT_PRIMARY_KEY,):
            if common.is_unique_violation(exc, constraint):
                raise CohortConflict(f"unicité de cohorte occupée ({constraint})") from None
        raise


# --------------------------------------------------------------------------
# 1. Définition de la cohorte et calendrier d'admission
# --------------------------------------------------------------------------

def define_cohort(
    db,
    *,
    campaign_id: uuid.UUID,
    cohort_policy_version: str,
    admission_closes_at: datetime,
) -> tuple[ProductCampaignCohort, bool]:
    """Définit la cohorte d'une campagne planned. Retourne (cohorte, created).
    Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_choice(cohort_policy_version, tuple(COHORT_POLICIES), "cohort_policy_version")
    admission_closes_at = common.require_aware(admission_closes_at, "admission_closes_at")

    campaign = campaigns.lock_campaign(db, campaign_id)
    existing = _cohort(db, campaign_id)
    if existing is not None:
        if (existing.cohort_policy_version, existing.admission_closes_at) != (cohort_policy_version,
                                                                              admission_closes_at):
            raise CohortConflict(f"campagne {campaign_id} : cohorte déjà définie autrement (immuable)")
        return existing, False
    if campaign.status != PLANNED:
        raise CohortRefused(f"campagne {campaign_id} : cohorte définissable seulement à l'état planned",
                            reason="campaign_not_planned")
    if _has_participations(db, campaign_id):
        raise CohortRefused(f"campagne {campaign_id} : participations déjà présentes", reason="participation_exists")
    now = common.utcnow()  # après le verrou exclusif de la campagne
    _check_calendar(campaign, admission_closes_at, now)
    cohort = ProductCampaignCohort(
        campaign_id=campaign_id, cohort_policy_version=cohort_policy_version,
        seat_count=COHORT_POLICIES[cohort_policy_version].seat_count, admission_closes_at=admission_closes_at,
        created_at=now)
    _flush_or_conflict(db, [cohort])
    return cohort, True


# --------------------------------------------------------------------------
# 2. Places sélectionnées
# --------------------------------------------------------------------------

def add_cohort_seat(db, *, campaign_id: uuid.UUID, invitation_id: uuid.UUID) -> tuple[ProductCohortSeat, bool]:
    """Ajoute la place de l'invitation F2 sélectionnée (campagne planned).
    Retourne (place, created). Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_uuid(invitation_id, "invitation_id")

    campaign = campaigns.lock_campaign(db, campaign_id)  # sérialise la numérotation
    cohort = _require_cohort(db, campaign_id)
    existing = _seat_where(db, ProductCohortSeat.campaign_id == campaign_id,
                           ProductCohortSeat.invitation_id == invitation_id)
    if existing is not None:
        return existing, False
    if campaign.status != PLANNED:
        raise CohortRefused(f"campagne {campaign_id} : places fixées hors de l'état planned (aucun ajout ni "
                            "remplacement)", reason="campaign_not_planned")
    seats = _seats(db, campaign_id)
    if len(seats) >= cohort.seat_count:
        raise CohortRefused(f"campagne {campaign_id} : cohorte complète ({cohort.seat_count} places)",
                            reason="cohort_full")
    now = common.utcnow()  # après le verrou exclusif de la campagne
    if not _invitation_usable(_invitation(db, invitation_id), now):
        raise CohortRefused("invitation inconnue, révoquée ou expirée", reason="invitation_unavailable")
    seat = ProductCohortSeat(id=uuid.uuid4(), campaign_id=campaign_id, seat_number=len(seats) + 1,
                             invitation_id=invitation_id, created_at=now)
    _flush_or_conflict(db, [seat])
    return seat, True


# --------------------------------------------------------------------------
# 3. Activation d'une campagne à cohorte
# --------------------------------------------------------------------------

def cohort_violations(db, campaign: ProductCampaign, cohort: ProductCampaignCohort, seats: list,
                      now: datetime) -> list:
    """Critères de cohorte complète et cohérente non respectés (liste vide :
    conforme). Lecture seule."""
    policy = COHORT_POLICIES.get(cohort.cohort_policy_version)
    numbers = [seat.seat_number for seat in seats]
    checks = (
        ("politique de cohorte inconnue", policy is not None),
        ("nombre de places de la politique", policy is not None and cohort.seat_count == policy.seat_count),
        ("places 1..N exactement", numbers == list(range(1, cohort.seat_count + 1))),
        ("invitations distinctes", len({seat.invitation_id for seat in seats}) == len(seats)),
        ("identités distinctes", len({s.user_id for s in seats if s.user_id is not None})
         == len([s for s in seats if s.user_id is not None])),
        ("invitation révoquée ou expirée",
         all(seat.user_id is not None or _invitation_usable(_invitation(db, seat.invitation_id), now)
             for seat in seats)),
        ("participation préexistante", not _has_participations(db, campaign.id)),
    )
    return [name for name, ok in checks if not ok]


def activate_cohort_campaign(db, *, campaign_id: uuid.UUID) -> ProductCampaign:
    """planned -> active d'une campagne à cohorte, seulement si la cohorte
    est complète et cohérente et si le calendrier tient. Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    campaign = campaigns.lock_campaign(db, campaign_id)
    if campaign.status != PLANNED:
        raise CohortRefused(f"campagne {campaign_id} : activation impossible à l'état {campaign.status}",
                            reason="campaign_not_planned")
    cohort = _cohort(db, campaign_id)
    if cohort is None:
        raise CohortRefused(f"campagne {campaign_id} : aucune cohorte définie", reason="cohort_missing")
    seats = _seats(db, campaign_id, lock_shared=True)
    now = common.utcnow()  # après le verrou de la campagne et des places
    if len(seats) != cohort.seat_count:
        raise CohortRefused(f"campagne {campaign_id} : cohorte incomplète ({len(seats)}/{cohort.seat_count})",
                            reason="cohort_incomplete")
    violations = cohort_violations(db, campaign, cohort, seats, now)
    if violations:
        raise CohortRefused(f"campagne {campaign_id} : cohorte incohérente : {', '.join(violations)}",
                            reason="cohort_incoherent")
    _check_calendar(campaign, cohort.admission_closes_at, now)
    return campaigns.apply_activation(db, campaign, now=now)


# --------------------------------------------------------------------------
# 4. Rattachement par preuve F2 et 5. autorisation explicite
# --------------------------------------------------------------------------

def _lock_open_campaign_and_cohort(db, campaign_id) -> tuple[ProductCampaign, ProductCampaignCohort]:
    campaign = campaigns.lock_campaign(db, campaign_id, shared=True)
    cohort = _require_cohort(db, campaign_id)
    if campaign.status not in OPEN_CAMPAIGN_STATUSES:
        raise CohortRefused(f"campagne {campaign_id} : cohorte figée à l'état {campaign.status}",
                            reason="campaign_not_open")
    return campaign, cohort


def bind_seat_to_activated_invitation(db, *, campaign_id: uuid.UUID,
                                      seat_id: uuid.UUID) -> tuple[ProductCohortSeat, bool]:
    """Rattache la place à l'utilisateur de l'activation F2 committed de son
    invitation. Aucun user_id n'est accepté en entrée. Retourne (place,
    created). Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_uuid(seat_id, "seat_id")
    seat = _seat_where(db, ProductCohortSeat.id == seat_id, ProductCohortSeat.campaign_id == campaign_id)
    if seat is None:
        raise CohortSeatNotFound(f"place {seat_id} inconnue dans la campagne {campaign_id}")
    proof = _committed_activation(db, seat.invitation_id)
    if proof is None or proof.user_id is None or proof.committed_at is None:
        raise CohortRefused(f"place {seat_id} : aucune activation F2 committed de son invitation",
                            reason="invitation_not_activated")
    invitation = _invitation(db, seat.invitation_id)
    if invitation is None or invitation.status != INVITATION_ACCEPTED:
        raise CohortRefused(f"place {seat_id} : invitation non acceptée", reason="invitation_not_activated")

    common.lock_identities(db, common.identity_lock_key("cohort_seat", seat_id),
                           common.identity_lock_key("cohort_seat_user", campaign_id, proof.user_id))
    campaign, cohort = _lock_open_campaign_and_cohort(db, campaign_id)
    seat = _seat_where(db, ProductCohortSeat.id == seat_id, lock="update")
    now = common.utcnow()  # après les verrous d'identité, de la campagne et de la place
    if seat.user_id is not None:
        if (seat.user_id, seat.binding_operation_id) == (proof.user_id, proof.id):
            return seat, False
        raise CohortConflict(f"place {seat_id} : déjà rattachée (aucune réattribution)")
    if now >= cohort.admission_closes_at:
        raise CohortRefused(f"campagne {campaign_id} : date limite d'admission atteinte", reason="admission_closed")
    other = _seat_where(db, ProductCohortSeat.campaign_id == campaign_id, ProductCohortSeat.user_id == proof.user_id)
    if other is not None:
        raise CohortConflict(f"campagne {campaign_id} : identité déjà rattachée à la place {other.id}")
    if now < proof.committed_at:
        raise CohortRefused(f"place {seat_id} : horloge antérieure à l'activation", reason="clock_regression")
    seat.user_id = proof.user_id
    seat.binding_operation_id = proof.id
    seat.binding_operation_kind = INVITATION_ACTIVATION
    seat.binding_operation_status = COMMITTED
    seat.binding_committed_at = proof.committed_at
    seat.bound_at = now
    try:
        _flush_or_conflict(db)
    except CohortConflict:
        db.expire(seat)
        raise
    return seat, True


def record_seat_authorization(db, *, campaign_id: uuid.UUID, user_id: str,
                              notice_version: str) -> tuple[ProductCohortSeat, bool]:
    """Enregistre l'autorisation explicite de participation du titulaire de
    la place. user_id doit être celui du principal F2 authentifié de la
    requête (précondition du futur raccordement) ; il n'est accepté que s'il
    est déjà rattaché à une place par preuve F2. Retourne (place, created).
    Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    common.require_pattern(notice_version, NOTICE_VERSION_PATTERN, "notice_version")

    common.lock_identities(db, common.identity_lock_key("cohort_seat_user", campaign_id, user_id))
    campaign, cohort = _lock_open_campaign_and_cohort(db, campaign_id)
    seat = _seat_where(db, ProductCohortSeat.campaign_id == campaign_id, ProductCohortSeat.user_id == user_id,
                       lock="update")
    if seat is None:
        raise CohortRefused(f"campagne {campaign_id} : aucune place rattachée à cet utilisateur",
                            reason="seat_not_bound")
    now = common.utcnow()  # après les verrous
    if seat.authorization_recorded_at is not None:
        if seat.notice_version == notice_version:
            return seat, False
        raise CohortConflict(f"place {seat.id} : autorisation déjà enregistrée (immuable)")
    if now >= cohort.admission_closes_at:
        raise CohortRefused(f"campagne {campaign_id} : date limite d'admission atteinte", reason="admission_closed")
    if now < seat.bound_at:
        raise CohortRefused(f"place {seat.id} : horloge antérieure au rattachement", reason="clock_regression")
    seat.authorization_recorded_at = now
    seat.notice_version = notice_version
    db.flush()
    return seat, True


# --------------------------------------------------------------------------
# 6. J0 individuel
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Admission:
    participation: ProductCampaignParticipant
    created: bool
    j0_uncertain: bool


class _J0AtOrAfterAdmissionDeadline(Exception):
    """Annule le SAVEPOINT d'un J0 qui aurait atteint la date limite."""


def _j0_uncertain(seat: ProductCohortSeat, j0: datetime) -> bool:
    return seat.admission_failure_first_at is not None and seat.admission_failure_first_at < j0


def admit_at_first_eligible_login(db, *, campaign_id: uuid.UUID, user_id: str) -> Admission:
    """Admission Vague 0 à la première connexion authentifiée et éligible.
    user_id : principal F2 authentifié de la requête (jamais une valeur du
    navigateur). Lève AdmissionRefused (rien d'écrit), ParticipationConflict
    ou ProductAnalyticsError. Flush sans commit."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    common.require_existing_user(db, user_id)

    # Même verrou d'identité que first_eligible_login (réentrant dans la
    # transaction), puis campagne en partage, puis place en partage.
    common.lock_identities(db, common.identity_lock_key("participation", campaign_id, user_id))
    campaign = campaigns.lock_campaign(db, campaign_id, shared=True)
    cohort = _cohort(db, campaign_id)
    if cohort is None:
        raise AdmissionRefused(f"campagne {campaign_id} : aucune cohorte", reason="not_a_cohort_campaign")
    seat = _seat_where(db, ProductCohortSeat.campaign_id == campaign_id, ProductCohortSeat.user_id == user_id,
                       lock="share")
    if seat is None:
        raise AdmissionRefused(f"campagne {campaign_id} : utilisateur hors cohorte", reason="not_in_cohort")
    if seat.authorization_recorded_at is None:
        raise AdmissionRefused(f"place {seat.id} : autorisation de participation absente",
                               reason="authorization_missing")
    authorization = {"authorization_recorded_at": seat.authorization_recorded_at,
                     "notice_version": seat.notice_version}
    existing = participation._participation(db, campaign_id, user_id)
    if existing is None:
        if campaign.status != ACTIVE:
            raise AdmissionRefused(f"campagne {campaign_id} : connexion non éligible à l'état {campaign.status}",
                                   reason="campaign_not_active")
        if common.utcnow() >= cohort.admission_closes_at:  # après tous les verrous
            raise AdmissionRefused(f"campagne {campaign_id} : date limite d'admission atteinte",
                                   reason="admission_closed")
    try:
        with db.begin_nested():
            enrolled, created = participation.first_eligible_login(
                db, campaign_id=campaign_id, user_id=user_id, via_cohort=True, **authorization)
            if created and enrolled.enrolled_at >= cohort.admission_closes_at:
                raise _J0AtOrAfterAdmissionDeadline()
    except _J0AtOrAfterAdmissionDeadline:
        raise AdmissionRefused(f"campagne {campaign_id} : date limite d'admission atteinte",
                               reason="admission_closed") from None
    if not created and not (seat.authorization_recorded_at <= enrolled.window_start_at
                            < cohort.admission_closes_at):
        raise ParticipationConflict(
            f"participation existante incompatible avec la cohorte de la campagne {campaign_id}")
    return Admission(participation=enrolled, created=created, j0_uncertain=_j0_uncertain(seat,
                                                                                         enrolled.window_start_at))


# --------------------------------------------------------------------------
# 7. Contrat non bloquant
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AdmissionAttempt:
    """Issue d'une tentative d'admission ; jamais une exception.

    status : ATTEMPT_STATUSES ; reason : raison fermée (refus) ou nom de
    classe d'erreur (conflit, panne) — jamais un message ni une valeur
    libre ; j0 / window_end_at : participation établie ou existante ;
    j0_uncertain : une panne antérieure enregistrée précède ce J0."""
    status: str
    reason: str | None = None
    j0: datetime | None = None
    window_end_at: datetime | None = None
    j0_uncertain: bool = False


def _mark_admission_failure(db, campaign_id, user_id) -> None:
    """Au mieux : première panne technique d'une place autorisée et encore
    sans participation. SAVEPOINT distinct ; toute erreur est ignorée."""
    try:
        with db.begin_nested():
            seat = _seat_where(db, ProductCohortSeat.campaign_id == campaign_id, ProductCohortSeat.user_id == user_id,
                               lock="update")
            if (seat is None or seat.authorization_recorded_at is None or seat.admission_failure_first_at is not None
                    or participation._participation(db, campaign_id, user_id) is not None):
                return
            now = common.utcnow()
            if now >= seat.authorization_recorded_at:
                seat.admission_failure_first_at = now
                db.flush()
    except Exception:  # noqa: BLE001 — contrat non bloquant : la marque est facultative
        return


def attempt_first_login_admission(db, *, campaign_id: uuid.UUID, user_id: str) -> AdmissionAttempt:
    """Contrat non bloquant de la future instrumentation de connexion :
    n'interrompt jamais la requête de l'appelant (aucune Exception levée) ;
    aucune écriture hors SAVEPOINT validé ; aucun J0 antidaté."""
    try:
        common.require_uuid(campaign_id, "campaign_id")
        common.require_user_id(user_id)
        # Pré-filtre sans verrou : l'immense majorité des connexions (hors
        # cohorte) ne prend aucun verrou analytique.
        if _seat_where(db, ProductCohortSeat.campaign_id == campaign_id,
                       ProductCohortSeat.user_id == user_id) is None:
            return AdmissionAttempt(NOT_ELIGIBLE, "not_in_cohort")
        with db.begin_nested():
            admission = admit_at_first_eligible_login(db, campaign_id=campaign_id, user_id=user_id)
        enrolled = admission.participation
        return AdmissionAttempt(ADMITTED if admission.created else ALREADY_ADMITTED, None, enrolled.window_start_at,
                                enrolled.window_end_at, admission.j0_uncertain)
    except AdmissionRefused as exc:
        return AdmissionAttempt(NOT_ELIGIBLE, exc.reason)
    except ProductAnalyticsError as exc:
        return AdmissionAttempt(CONFLICT, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — panne technique : jamais propagée à la requête métier
        _mark_admission_failure(db, campaign_id, user_id)
        return AdmissionAttempt(FAILED, type(exc).__name__)
