"""Registre provisoire et certification des preuves d'utilisation
significative (V0-T08 Lot 2B-2, bloc B) — DORMANT.

Table product_activity_proof_registry (migration 0024, inchangée). Ce
module ne connaît AUCUN module de la frontière d'autorisation : il reçoit
d'un appelant unique (la frontière privée hors du paquet, seul importateur
autorisé, sans appelant de production) une opération métier DÉJÀ établie
après un COMMIT confirmé (CommittedOperation) et la clé de l'environnement
qui l'a exécutée. Il ne décide jamais qu'une opération a réussi : il refuse
d'enregistrer ou de certifier tout ce qui n'est pas un effet métier NOUVEAU,
rattaché à l'interaction 'activity' émise POUR CETTE opération, acceptée
pendant la fenêtre, et certifié avant les échéances.

Liaison causale interaction -> opération (audit de la PR #253) : l'origine
n'est JAMAIS désignée par l'appelant. open_origin l'émet lui-même pour une
opération établie : clé d'interaction générée ici (uuid4, jamais reçue),
propriétaire = celui de l'opération, visite déterminée par le service 2B-1
(touch_activity_session) dans l'environnement fourni. Le OriginSnapshot
rendu est une CAPACITÉ : il porte l'identité de l'opération pour laquelle il
a été émis (operation_kind, operation_id) et seul CET objet, enregistré à
l'émission, est accepté par register_pending (comparaison d'identité ; une
copie, un dataclasses.replace, un snapshot reconstruit ou lu par
read_origin, ou une origine émise pour une autre opération sont refusés,
origin_unverified, avant tout accès à la base). Une interaction ancienne,
d'un autre appareil ou de la même visite ne peut donc jamais devenir
l'origine d'une nouvelle opération ; une interaction n'est l'origine que
d'une seule opération.

Fonctions (la transaction appartient à l'appelant : flush seulement, jamais
commit() ni rollback() ; toute écriture dans un SAVEPOINT) :

- open_origin : émission de l'origine d'une opération établie à effet
  nouveau (voir ci-dessus) ; touch_activity_session puis relecture
  read_origin dans la même transaction. ProofRefused (motif fermé, rien
  d'écrit) hors observation (not_authorized / window_expired) ; rejeu ou
  no-op : no_new_effect, sans aucun accès à la base.
- read_origin : lecture SANS verrou ni écriture d'une interaction
  'activity' de la visite, acceptée dans [window_start_at, window_end_at),
  à partir de activated_at et avant close_by_at. Rend un OriginSnapshot NON
  émis (observation seulement : jamais accepté par register_pending) ou
  lève ProofRefused (motif fermé).
- register_pending : inscription IDEMPOTENTE de la preuve pending
  (created_at = horloge PostgreSQL lue après les verrous). Rien n'est écrit
  sans effet nouveau, sans propriétaire identique, sans origine ÉMISE pour
  cette opération et identique en base au snapshot, sans autorisation
  (participation enrolled, campagne active) ou après window_end_at /
  close_by_at. Une preuve existante de la même
  opération est rendue telle quelle si son identité est EXACTEMENT celle
  demandée ; sinon conflit, sans aucune écriture ni réattribution.
- certify_pending : transition contrôlée pending -> certified
  (certified_at = horloge PostgreSQL lue après les verrous), ou pending ->
  rejected (motif fermé, resolved_at, purge_after_at = resolved_at + 168 h)
  quand les verrous révèlent une origine modifiée, une autorisation perdue
  ou une échéance franchie. Une preuve certified est rendue sans écriture ;
  recorded / rejected / indeterminate ne sont jamais modifiées.
- settle : après une certification en échec ou dont le COMMIT est
  incertain, relecture sous verrou de la MÊME preuve (même identifiant,
  même identité) : certified est constatée telle quelle ; pending devient
  indeterminate / technical_uncertainty (terminal) ; rien d'autre n'est
  modifié.

Aucun événement définitif ni aucune attribution définitive n'est écrit ici
(bloc C) ; aucune suppression (lot 2C).

open_origin prend les verrous de touch_activity_session (ordre 2B-1 :
environnement et clé, participation, campagne, visite), dans sa propre
transaction ; register_pending, certify_pending et settle suivent :

Ordre global des verrous (identique aux services 2A / 2B-1, rang preuve
ajouté en dernier) :
  1. pg_advisory_xact_lock de l'identité canonique de la preuve
     (common.identity_lock_key('activity_proof', user_id, nature, id)) ;
  2. participation (campaign_id, user_id) FOR SHARE ;
  3. campagne FOR SHARE (campaigns.lock_campaign) ;
  4. visite et interaction : lectures sans verrou (lignes dont les colonnes
     lues sont immuables ; la clôture ultérieure d'une visite ne change rien
     à la validité historique de l'interaction) ;
  5. preuve FOR UPDATE ;
  6. horloge : clock_timestamp() PostgreSQL, APRÈS tous ces verrous.
settle ne prend que les rangs 1 et 5 (sous-ensemble ordonné). Une clôture
ou annulation de campagne (campagne FOR UPDATE), un retrait (participation
FOR UPDATE) ou une finalisation de visite (visite FOR UPDATE) concurrents
sont donc soit vus, soit mis en attente jusqu'au COMMIT de la décision.

Motifs : rejected pour no_new_effect, source_unverified, origin_unverified,
owner_conflict, visit_conflict, operation_conflict, window_expired,
not_authorized ; indeterminate pour technical_uncertainty.
finalization_expired relève du bloc C (fenêtre de finalisation) et n'est
jamais écrit ici.
"""
import dataclasses
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.models import (
    ProductActivityProof,
    ProductActivitySession,
    ProductActivitySessionInteraction,
    ProductCampaign,
    ProductCampaignParticipant,
)
from core.product_analytics import campaigns, common, sessions
from core.product_analytics.common import (
    ACTIVE,
    ENROLLED,
    ActivitySessionRefused,
    InvalidProductAnalyticsInput,
    ProductAnalyticsError,
)

# --------------------------------------------------------------------------
# Vocabulaires fermés (identiques aux CHECK de la migration 0024)
# --------------------------------------------------------------------------

PENDING, CERTIFIED, RECORDED, REJECTED, INDETERMINATE = "pending", "certified", "recorded", "rejected", "indeterminate"
PROOF_STATUSES = (PENDING, CERTIFIED, RECORDED, REJECTED, INDETERMINATE)
RESOLVED_STATUSES = (RECORDED, REJECTED, INDETERMINATE)
# État d'une décision quand AUCUNE preuve de cette origine n'existe (rien
# inscrit, ou preuve de la même opération rattachée à une autre origine).
ABSENT = "absent"
DECISION_STATES = (ABSENT,) + PROOF_STATUSES

NO_NEW_EFFECT = "no_new_effect"
SOURCE_UNVERIFIED = "source_unverified"
ORIGIN_UNVERIFIED = "origin_unverified"
OWNER_CONFLICT = "owner_conflict"
VISIT_CONFLICT = "visit_conflict"
OPERATION_CONFLICT = "operation_conflict"
WINDOW_EXPIRED = "window_expired"
FINALIZATION_EXPIRED = "finalization_expired"
NOT_AUTHORIZED = "not_authorized"
TECHNICAL_UNCERTAINTY = "technical_uncertainty"
RESOLUTION_REASONS = (NO_NEW_EFFECT, SOURCE_UNVERIFIED, ORIGIN_UNVERIFIED, OWNER_CONFLICT, VISIT_CONFLICT,
                      OPERATION_CONFLICT, WINDOW_EXPIRED, FINALIZATION_EXPIRED, NOT_AUTHORIZED, TECHNICAL_UNCERTAINTY)
# Répartition par état terminal (non imposée en SQL : décision de ce bloc).
REJECTION_REASONS = (NO_NEW_EFFECT, SOURCE_UNVERIFIED, ORIGIN_UNVERIFIED, OWNER_CONFLICT, VISIT_CONFLICT,
                     OPERATION_CONFLICT, WINDOW_EXPIRED, NOT_AUTHORIZED)
INDETERMINATE_REASONS = (TECHNICAL_UNCERTAINTY, FINALIZATION_EXPIRED)

ACTIVITY = "activity"  # seule nature d'interaction admise comme origine (jamais 'close')

# Natures d'opération CERTIFIABLES aujourd'hui -> (event_type, surface) du
# couple fermé de 0024. assistant_delivery (Éducation, Coach) n'en fait PAS
# partie : aucune preuve de réussite n'existe encore pour ces parcours.
OPERATION_RULES = {
    "decryptage_delivery_ack": ("decrypter_used", "decrypter"),
    "portfolio_position_create": ("portfolio_used", "portfolio"),
    "portfolio_position_update": ("portfolio_used", "portfolio"),
    "portfolio_position_delete": ("portfolio_used", "portfolio"),
}
OPERATION_KINDS = tuple(OPERATION_RULES)

# Conservation d'une preuve résolue : 7 x 24 heures ABSOLUES (CHECK 0024).
REGISTRY_RETENTION = timedelta(hours=168)
SOURCE_OPERATION_UNIQUE = "uq_product_activity_proof_registry_source_operation"
PROOF_LOCK_KIND = "activity_proof"


class ProofRefused(ProductAnalyticsError):
    """Origine non admissible (open_origin, read_origin) : rien n'est lu
    au-delà, rien n'est écrit. reason : RESOLUTION_REASONS."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------
# Contrats internes (valeurs fermées, sans contenu utilisateur)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class OriginSnapshot:
    """Interaction 'activity' acceptée. recorded_at : instant d'acceptation
    serveur. operation_kind / operation_id : identité de l'opération pour
    laquelle open_origin l'a émise (None : simple observation, read_origin).
    Seul l'objet émis lui-même vaut origine (voir l'en-tête)."""
    campaign_id: uuid.UUID
    user_id: str
    visit_id: uuid.UUID
    interaction_key: uuid.UUID
    recorded_at: datetime
    operation_kind: str | None = None
    operation_id: uuid.UUID | None = None

    def __repr__(self) -> str:
        return "OriginSnapshot(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CommittedOperation:
    """Opération métier établie par l'appelant APRÈS un COMMIT confirmé :
    propriétaire serveur, nature canonique (OPERATION_KINDS), identifiant
    canonique et effet nouveau (False pour un rejeu ou un no-op)."""
    owner_user_id: str
    operation_kind: str
    operation_id: uuid.UUID
    new_effect: bool

    def __repr__(self) -> str:
        return f"CommittedOperation({self.operation_kind}, new_effect={self.new_effect})"


@dataclass(frozen=True, slots=True)
class ProofDecision:
    """Issue d'un appel. state : DECISION_STATES (état de LA preuve de cette
    origine après l'appel, ABSENT si aucune) ; reason : motif fermé ou None ;
    written : cet appel a écrit (flush) ; proof_id / created_at /
    certified_at : ceux de la preuve, si elle existe."""
    state: str
    reason: str | None = None
    written: bool = False
    proof_id: uuid.UUID | None = None
    created_at: datetime | None = None
    certified_at: datetime | None = None


def _absent(reason: str) -> ProofDecision:
    return ProofDecision(ABSENT, reason)


def _decision(proof: ProductActivityProof, *, written: bool, reason: str | None = None) -> ProofDecision:
    return ProofDecision(proof.status, reason if reason is not None else proof.resolution_reason, written, proof.id,
                         proof.created_at, proof.certified_at)


# --------------------------------------------------------------------------
# Validation et horloge
# --------------------------------------------------------------------------

def server_clock(db) -> datetime:
    """Horloge serveur PostgreSQL (clock_timestamp(), pas now() : l'instant
    réel de la lecture, après les attentes de verrou), seule source de
    created_at, certified_at et resolved_at. Remplaçable en test."""
    return db.execute(select(func.clock_timestamp())).scalar_one().astimezone(timezone.utc)


def _require_origin(origin) -> OriginSnapshot:
    if type(origin) is not OriginSnapshot:
        raise InvalidProductAnalyticsInput("origin : OriginSnapshot requis")
    common.require_uuid(origin.campaign_id, "campaign_id")
    common.require_user_id(origin.user_id)
    common.require_uuid(origin.visit_id, "visit_id")
    common.require_uuid(origin.interaction_key, "interaction_key")
    common.require_aware(origin.recorded_at, "recorded_at")
    if origin.operation_kind is not None or origin.operation_id is not None:
        common.require_choice(origin.operation_kind, OPERATION_KINDS, "operation_kind")
        if type(origin.operation_id) is not uuid.UUID:
            raise InvalidProductAnalyticsInput("operation_id : uuid.UUID requis")
    return origin


def _require_operation(operation) -> CommittedOperation:
    if type(operation) is not CommittedOperation:
        raise InvalidProductAnalyticsInput("operation : CommittedOperation requise")
    common.require_user_id(operation.owner_user_id)
    common.require_choice(operation.operation_kind, OPERATION_KINDS, "operation_kind")
    if type(operation.operation_id) is not uuid.UUID:
        raise InvalidProductAnalyticsInput("operation_id : uuid.UUID requis")
    if type(operation.new_effect) is not bool:
        raise InvalidProductAnalyticsInput("new_effect : booléen exact requis")
    return operation


# Origines émises par open_origin, encore référencées par leur appelant :
# clé d'interaction -> L'OBJET émis (références faibles : une origine
# abandonnée disparaît avec son dernier détenteur). Jamais persisté.
_ISSUED: "weakref.WeakValueDictionary[uuid.UUID, OriginSnapshot]" = weakref.WeakValueDictionary()


def _issue(observed: OriginSnapshot, operation: CommittedOperation) -> OriginSnapshot:
    """Lie une interaction observée à l'opération pour laquelle elle vient
    d'être acceptée et enregistre l'objet émis (seul accepté ensuite)."""
    issued = dataclasses.replace(observed, operation_kind=operation.operation_kind,
                                 operation_id=operation.operation_id)
    _ISSUED[issued.interaction_key] = issued
    return issued


def _issued_for(origin: OriginSnapshot, operation: CommittedOperation) -> bool:
    """L'origine est-elle EXACTEMENT l'objet émis pour CETTE opération ?"""
    return (_ISSUED.get(origin.interaction_key) is origin
            and (origin.operation_kind, origin.operation_id) == (operation.operation_kind, operation.operation_id))


def proof_lock_key(operation: CommittedOperation) -> int:
    return common.identity_lock_key(PROOF_LOCK_KIND, operation.owner_user_id, operation.operation_kind,
                                    operation.operation_id)


# --------------------------------------------------------------------------
# Lectures
# --------------------------------------------------------------------------

def _participation(db, campaign_id, user_id, *, shared_lock: bool) -> ProductCampaignParticipant | None:
    statement = select(ProductCampaignParticipant).where(ProductCampaignParticipant.campaign_id == campaign_id,
                                                         ProductCampaignParticipant.user_id == user_id)
    if shared_lock:
        statement = statement.with_for_update(read=True)
    return db.execute(statement.execution_options(populate_existing=True)).scalar_one_or_none()


def _accepted_during_observation(recorded_at: datetime, participation, campaign) -> bool:
    """Interaction acceptée à partir de l'activation, dans [window_start_at,
    window_end_at) et avant close_by_at (bornes immuables)."""
    return (campaign.activated_at is not None and campaign.activated_at <= recorded_at
            and participation.window_start_at <= recorded_at < participation.window_end_at
            and recorded_at < campaign.close_by_at)


def _origin_problem(db, *, campaign_id, user_id, visit_id, interaction_key, participation, campaign,
                    recorded_at=None) -> tuple:
    """(motif ou None, interaction) : visite de CETTE participation,
    interaction 'activity' de CETTE visite acceptée pendant l'observation, et
    instant d'acceptation identique au snapshot quand il est fourni."""
    if participation is None or campaign is None:
        return NOT_AUTHORIZED, None
    visit = db.execute(select(ProductActivitySession).where(ProductActivitySession.id == visit_id)
                       .execution_options(populate_existing=True)).scalar_one_or_none()
    if visit is None or (visit.campaign_id, visit.user_id) != (campaign_id, user_id):
        return VISIT_CONFLICT, None
    interaction = db.execute(select(ProductActivitySessionInteraction).where(
        ProductActivitySessionInteraction.campaign_id == campaign_id,
        ProductActivitySessionInteraction.user_id == user_id,
        ProductActivitySessionInteraction.interaction_key == interaction_key)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if interaction is None or interaction.interaction_kind != ACTIVITY:
        return ORIGIN_UNVERIFIED, None
    if interaction.session_id != visit_id:
        return VISIT_CONFLICT, None
    if recorded_at is not None and interaction.recorded_at != recorded_at:
        return ORIGIN_UNVERIFIED, None
    if not _accepted_during_observation(interaction.recorded_at, participation, campaign):
        return ORIGIN_UNVERIFIED, None
    return None, interaction


def read_origin(db, *, campaign_id: uuid.UUID, user_id: str, visit_id: uuid.UUID,
                interaction_key: uuid.UUID) -> OriginSnapshot:
    """Origine déjà acceptée, lue AVANT l'opération métier : lecture seule,
    aucun verrou. ProofRefused si elle n'est pas admissible."""
    common.require_uuid(campaign_id, "campaign_id")
    common.require_user_id(user_id)
    common.require_uuid(visit_id, "visit_id")
    common.require_uuid(interaction_key, "interaction_key")
    participation = _participation(db, campaign_id, user_id, shared_lock=False)
    campaign = None
    if participation is not None:
        campaign = db.execute(select(ProductCampaign).where(ProductCampaign.id == campaign_id)
                              .execution_options(populate_existing=True)).scalar_one_or_none()
    reason, interaction = _origin_problem(db, campaign_id=campaign_id, user_id=user_id, visit_id=visit_id,
                                          interaction_key=interaction_key, participation=participation,
                                          campaign=campaign)
    if reason is not None:
        raise ProofRefused("origine non admissible", reason=reason)
    return OriginSnapshot(campaign_id, user_id, visit_id, interaction_key,
                          interaction.recorded_at.astimezone(timezone.utc))


# Refus 2B-1 (SESSION_REFUSAL_REASONS) -> motif fermé du registre.
SESSION_REFUSALS = {
    "not_participant": NOT_AUTHORIZED,
    "participation_inactive": NOT_AUTHORIZED,
    "campaign_not_active": NOT_AUTHORIZED,
    "before_observation": NOT_AUTHORIZED,
    "campaign_closed": WINDOW_EXPIRED,
    "observation_ended": WINDOW_EXPIRED,
}


def open_origin(db, *, campaign_id: uuid.UUID, environment_key: uuid.UUID,
                operation: CommittedOperation) -> OriginSnapshot:
    """Émet l'origine de `operation` (voir l'en-tête) : interaction
    'activity' NOUVELLE (clé générée ici) du propriétaire de l'opération,
    visite déterminée par le service 2B-1 dans `environment_key`. Rend
    l'OriginSnapshot émis, lié à l'opération, ou lève ProofRefused (rien
    d'écrit). Flush sans commit."""
    operation = _require_operation(operation)
    common.require_uuid(campaign_id, "campaign_id")
    common.require_uuid(environment_key, "environment_key")
    if not operation.new_effect:
        raise ProofRefused("aucun effet nouveau", reason=NO_NEW_EFFECT)  # rejeu, no-op : aucune interaction
    interaction_key = uuid.uuid4()  # jamais fournie par l'appelant
    try:
        touched = sessions.touch_activity_session(db, campaign_id=campaign_id, user_id=operation.owner_user_id,
                                                  environment_key=environment_key, interaction_key=interaction_key)
    except ActivitySessionRefused as refused:
        raise ProofRefused("interaction hors observation",
                           reason=SESSION_REFUSALS.get(refused.reason, ORIGIN_UNVERIFIED)) from None
    if touched.outcome not in (sessions.OPENED, sessions.CONTINUED):
        raise ProofRefused("interaction non nouvelle", reason=ORIGIN_UNVERIFIED)  # clé neuve : jamais un rejeu
    observed = read_origin(db, campaign_id=campaign_id, user_id=operation.owner_user_id,
                           visit_id=touched.session.id, interaction_key=interaction_key)
    return _issue(observed, operation)


def _identity(origin: OriginSnapshot, operation: CommittedOperation) -> dict:
    event_type, surface = OPERATION_RULES[operation.operation_kind]
    return {"campaign_id": origin.campaign_id, "user_id": operation.owner_user_id, "visit_id": origin.visit_id,
            "interaction_key": origin.interaction_key, "interaction_kind": ACTIVITY, "event_type": event_type,
            "surface": surface, "source_operation_kind": operation.operation_kind,
            "source_operation_id": operation.operation_id}


def _identity_mismatch(proof: ProductActivityProof, identity: dict) -> str | None:
    """None si la preuve stockée a EXACTEMENT cette identité ; sinon le motif
    du conflit (la preuve stockée n'est jamais modifiée)."""
    if all(getattr(proof, field) == value for field, value in identity.items()):
        return None
    if proof.user_id != identity["user_id"]:
        return OWNER_CONFLICT
    if proof.campaign_id == identity["campaign_id"] and proof.visit_id != identity["visit_id"]:
        return VISIT_CONFLICT
    return OPERATION_CONFLICT


def _proof_for_update(db, operation: CommittedOperation) -> ProductActivityProof | None:
    return db.execute(select(ProductActivityProof).where(
        ProductActivityProof.user_id == operation.owner_user_id,
        ProductActivityProof.source_operation_kind == operation.operation_kind,
        ProductActivityProof.source_operation_id == operation.operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()


def _lock_scope(db, origin: OriginSnapshot, operation: CommittedOperation) -> tuple:
    """Rangs 1 à 3 de l'ordre global : identité, participation, campagne."""
    common.lock_identities(db, proof_lock_key(operation))
    participation = _participation(db, origin.campaign_id, origin.user_id, shared_lock=True)
    campaign = campaigns.lock_campaign(db, origin.campaign_id, shared=True) if participation is not None else None
    return participation, campaign


def _eligibility_problem(participation, campaign, now: datetime) -> str | None:
    """Autorisation et échéances à l'instant `now` (lu après les verrous)."""
    if participation.participation_status != ENROLLED or campaign.status != ACTIVE:
        return NOT_AUTHORIZED
    if now >= participation.window_end_at or now >= campaign.close_by_at:
        return WINDOW_EXPIRED
    return None


def _write(db, apply) -> None:
    with db.begin_nested():
        apply()
        db.flush()


def _resolve(proof: ProductActivityProof, status: str, reason: str, now: datetime) -> None:
    """Résolution terminale : certified_at éventuel conservé."""
    proof.status = status
    proof.resolution_reason = reason
    proof.resolved_at = now
    proof.purge_after_at = now + REGISTRY_RETENTION
    proof.recorded_event_id = None


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

def register_pending(db, *, origin: OriginSnapshot, operation: CommittedOperation) -> ProofDecision:
    """Inscription idempotente de la preuve pending (voir l'en-tête)."""
    origin, operation = _require_origin(origin), _require_operation(operation)
    if not operation.new_effect:
        return _absent(NO_NEW_EFFECT)  # rejeu ou no-op : aucune trace, aucune lecture
    if operation.owner_user_id != origin.user_id:
        return _absent(OWNER_CONFLICT)
    if not _issued_for(origin, operation):
        return _absent(ORIGIN_UNVERIFIED)  # origine non émise pour CETTE opération : aucune lecture
    participation, campaign = _lock_scope(db, origin, operation)
    problem, _ = _origin_problem(db, campaign_id=origin.campaign_id, user_id=origin.user_id,
                                 visit_id=origin.visit_id, interaction_key=origin.interaction_key,
                                 participation=participation, campaign=campaign, recorded_at=origin.recorded_at)
    existing = _proof_for_update(db, operation)
    now = server_clock(db)  # après TOUS les verrous
    identity = _identity(origin, operation)
    if existing is not None:
        mismatch = _identity_mismatch(existing, identity)
        return _absent(mismatch) if mismatch is not None else _decision(existing, written=False)
    if problem is not None:
        return _absent(problem)
    if now < origin.recorded_at:  # horloges incohérentes : causalité non établie
        return _absent(TECHNICAL_UNCERTAINTY)
    refusal = _eligibility_problem(participation, campaign, now)
    if refusal is not None:
        return _absent(refusal)
    proof = ProductActivityProof(id=uuid.uuid4(), status=PENDING, created_at=now, certified_at=None,
                                 resolved_at=None, recorded_event_id=None, resolution_reason=None,
                                 purge_after_at=None, **identity)
    try:
        _write(db, lambda: db.add(proof))
    except IntegrityError as exc:
        if not common.is_unique_violation(exc, SOURCE_OPERATION_UNIQUE):
            raise
        # Défense finale (écrivain hors service) : relecture, jamais d'écrasement.
        winner = _proof_for_update(db, operation)
        if winner is None:
            raise
        mismatch = _identity_mismatch(winner, identity)
        return _absent(mismatch) if mismatch is not None else _decision(winner, written=False)
    return _decision(proof, written=True)


def certify_pending(db, *, origin: OriginSnapshot, operation: CommittedOperation,
                    proof_id: uuid.UUID) -> ProofDecision:
    """pending -> certified, ou pending -> rejected (voir l'en-tête)."""
    origin, operation = _require_origin(origin), _require_operation(operation)
    common.require_uuid(proof_id, "proof_id")
    if not operation.new_effect:
        return _absent(NO_NEW_EFFECT)
    if operation.owner_user_id != origin.user_id:
        return _absent(OWNER_CONFLICT)
    participation, campaign = _lock_scope(db, origin, operation)
    problem, _ = _origin_problem(db, campaign_id=origin.campaign_id, user_id=origin.user_id,
                                 visit_id=origin.visit_id, interaction_key=origin.interaction_key,
                                 participation=participation, campaign=campaign, recorded_at=origin.recorded_at)
    proof = _proof_for_update(db, operation)
    now = server_clock(db)  # après TOUS les verrous : instant de certification
    if proof is None:
        return _absent(TECHNICAL_UNCERTAINTY)  # la preuve inscrite n'est pas (ou plus) là : rien n'est créé ici
    mismatch = _identity_mismatch(proof, _identity(origin, operation))
    if mismatch is not None or proof.id != proof_id:
        return _absent(mismatch or OPERATION_CONFLICT)  # jamais la preuve d'une autre origine
    if proof.status != PENDING:
        return _decision(proof, written=False)  # certified : idempotent ; résolue : jamais ressuscitée
    if now < proof.created_at:
        return _decision(proof, written=False, reason=TECHNICAL_UNCERTAINTY)  # aucune date inventée
    reason = problem if problem is not None else _eligibility_problem(participation, campaign, now)
    if reason is not None:
        _write(db, lambda: _resolve(proof, REJECTED, reason, now))
        return _decision(proof, written=True)

    def certify():
        proof.status = CERTIFIED
        proof.certified_at = now
    _write(db, certify)
    return _decision(proof, written=True)


def settle(db, *, origin: OriginSnapshot, operation: CommittedOperation, proof_id: uuid.UUID) -> ProofDecision:
    """Après une certification en échec ou incertaine : relecture sous
    verrou de la MÊME preuve. certified constatée ; pending ->
    indeterminate / technical_uncertainty ; sinon aucune écriture."""
    origin, operation = _require_origin(origin), _require_operation(operation)
    common.require_uuid(proof_id, "proof_id")
    common.lock_identities(db, proof_lock_key(operation))  # rang 1, puis rang 5 : sous-ensemble ordonné
    proof = _proof_for_update(db, operation)
    now = server_clock(db)
    if proof is None:
        return _absent(TECHNICAL_UNCERTAINTY)
    mismatch = _identity_mismatch(proof, _identity(origin, operation))
    if mismatch is not None or proof.id != proof_id:
        return _absent(mismatch or OPERATION_CONFLICT)
    if proof.status != PENDING or now < proof.created_at:
        return _decision(proof, written=False)
    _write(db, lambda: _resolve(proof, INDETERMINATE, TECHNICAL_UNCERTAINTY, now))
    return _decision(proof, written=True)
