"""Finalisation atomique des preuves certifiées en événements définitifs
(V0-T08 Lot 2B-2, bloc C) — DORMANTE.

Transforme une preuve `certified` PERSISTÉE dans
product_activity_proof_registry (bloc B, inchangé) en un événement
définitif : dans UNE transaction PostgreSQL et UN SAVEPOINT,

    1. INSERT product_activity_events (événement métier certifié) ;
    2. INSERT product_activity_event_attributions (attribution concordante) ;
    3. UPDATE de la preuve : certified -> recorded.

Ordre imposé par les FK non différables de 0024 : événement -> attribution
-> preuve. L'échec d'une écriture annule les trois (le SAVEPOINT est annulé,
la transaction de l'appelant reste utilisable). Le service ne fait jamais
commit() ni rollback() : un succès n'est durable qu'après le COMMIT de
l'appelant (frontière privée core/_activity_event_finalization.py, sans
appelant de production).

Entrée UNIQUE : l'identifiant interne de la preuve. L'identité de
l'événement n'est jamais fournie par l'appelant : propriétaire, campagne,
visite, interaction, nature et identifiant d'opération, instants et
finalité de conservation sont relus depuis la preuve et ses relations
PostgreSQL, sous verrou. Le bloc C ne lit AUCUNE table F2, ne rejoue rien,
ne certifie rien, n'émet aucune interaction, ne prolonge ni ne rouvre
aucune visite, ne modifie ni campagne ni participation.

Projection (preuve P, interaction I de P, campagne C de P) :

    user_id, visit_id                  = P.user_id, P.visit_id
    idempotency_key                    = P.id (clé serveur, stable entre retries)
    event_type, surface                = ceux de P (couple fermé de 0024)
    source / outcome / executed_route  = server_business / succeeded / f2
    source_operation_kind / _id        = ceux de P (nature canonique PR #250)
    occurred_at                        = I.recorded_at (observation serveur
                                         post-COMMIT F2 de l'activité)
    resolved_at                        = P.certified_at
    delivery_status / delivered_at     = Décrypter : confirmed / I.recorded_at
                                         (instant serveur d'observation de la
                                         livraison déjà confirmée, jamais
                                         l'instant de réception navigateur) ;
                                         Portfolio : not_applicable / NULL
    recorded_at = updated_at           = instant de finalisation (PostgreSQL)
    conservation                       = UNE finalité, campagne C,
                                         occurred_at + retention_days (90 j)

Attribution : les neuf champs d'identité de P, event_id, certified_at =
P.certified_at (égalité imposée ici, non contrainte en SQL). Preuve :
recorded, recorded_event_id, resolved_at = instant de finalisation,
resolution_reason NULL, purge_after_at = resolved_at + 168 heures ;
certified_at jamais modifié.

Chronologie exigée : I.recorded_at <= P.created_at <= P.certified_at <=
instant de finalisation ; sinon aucun événement.

Instant de décision (R1) : recorded_at de l'événement et resolved_at de la
preuve = horloge PostgreSQL lue APRÈS tous les verrous explicites. Les
écritures peuvent encore attendre (index unique tenu par une transaction
non committée d'un écrivain hors service, contrôle de FK) : une ULTIME
horloge est lue après les trois écritures, DANS le même SAVEPOINT, avant
son RELEASE ; la décision n'est conservée que si elle est encore admissible
à cet instant (strictement avant l'échéance). Sinon le SAVEPOINT est annulé
(aucun événement, aucune attribution, aucune preuve recorded) et la preuve
est résolue à l'instant de l'ultime horloge (indeterminate /
finalization_expired), jamais antidatée ; horloge régressive : aucune
écriture. Aucun instant de COMMIT n'est garanti (PostgreSQL ne le permet
pas) : le contrat porte sur la décision admissible après les attentes SQL
des écritures.

États (finalize_certified_proof) :

    pending        jamais enregistrable ; inchangée (sa résolution est
                   explicite : resolve_expired_pending_proof)
    certified      admissible -> recorded ;
                   motif certain -> rejected (source_unverified,
                   visit_conflict, origin_unverified, window_expired,
                   not_authorized, operation_conflict) ;
                   échéance de finalisation dépassée (avant les écritures
                   ou à l'ultime horloge) ou incohérence chronologique
                   stockée -> indeterminate (finalization_expired /
                   technical_uncertainty) ;
                   horloge régressive ou course SQL perdue : AUCUNE écriture
                   (retry sûr possible, jamais automatique)
    recorded       terminal, idempotent : événement et attribution relus et
                   vérifiés, jamais recréés ni modifiés ; relecture refusée
                   hors conservation (retention_expired) ou si l'état stocké
                   est incohérent (record_incoherent), sans rien renvoyer
    rejected,
    indeterminate  terminaux : jamais ressuscités

Autorisation d'une PREMIÈRE finalisation : participation enrolled, campagne
active (une clôture, une annulation ou un retrait antérieurs à la décision
l'interdisent : rejected / not_authorized ; une preuve déjà recorded n'est
jamais réécrite pour autant) ; interaction acceptée pendant l'observation
historique ; certification strictement avant window_end_at et close_by_at ;
finalité de conservation ouverte. Seule la campagne de la preuve est une
finalité : aucune autre campagne du même utilisateur n'est rattachée.

Échéance de finalisation (marge réservée aux preuves DÉJÀ certifiées, jamais
une grâce générique de events.record_event, dont LATE_EVENT_GRACE reste 0) :

    deadline = min(window_end_at + SESSION_FINALIZATION_GRACE, close_by_at)

    Arbitrage R1 : la fin de la visite d'origine (inactivité ou
    déconnexion) ne raccourcit PAS cette échéance. Le bloc B a déjà établi un
    nouvel effet métier réel, une origine valide (interaction acceptée dans
    la fenêtre, sur cette visite) et une certification strictement antérieure
    à window_end_at et close_by_at ; la clôture ultérieure de la visite ne
    détruit pas cette validité historique. Règle propre aux preuves
    certifiées : ni grâce générique de events.record_event (LATE_EVENT_GRACE
    reste 0), ni arbitrage sur de futurs événements de clôture de session.
    L'identité et la provenance de la visite restent contrôlées. La décision
    est prise strictement avant `deadline` ; à la borne exacte ou après :
    aucune création (indeterminate / finalization_expired). Ni J0, ni la
    fenêtre, ni close_by_at, ni la visite ne sont modifiés.

resolve_expired_pending_proof : pending -> indeterminate /
finalization_expired seulement à partir de la même échéance (aucune
certification légitime ne peut plus survenir ; une certification en cours
est sérialisée par le verrou consultatif de la preuve) ; certified_at reste
NULL ; avant : inchangée.

observe_proof : même relecture sous verrous, AUCUNE écriture (confirmation
de l'issue d'un COMMIT incertain par la frontière privée).

Conflits (aucune réparation, aucune adoption d'un état partiel, aucune
correspondance présumée avec un événement historique) : clé d'idempotence
P.id occupée, opération canonique déjà enregistrée (événement ou
attribution), même identifiant d'opération enregistré par le chemin 2A
(assistant_delivery / portfolio_write) -> rejected / operation_conflict
(visit_conflict si l'occupant porte une autre visite). Course perdue sur une
UNIQUE (écrivain hors service contournant les verrous consultatifs, qui
committe) : SAVEPOINT annulé, AUCUNE écriture, certified /
operation_conflict (la décision n'est jamais prise sur un état antérieur à
l'attente). S'il annule, l'INSERT réussit après l'attente : l'ultime
horloge décide (voir « Instant de décision »).

Ordre global des verrous (aucun cycle ; voir docs/v0_t08_2b2_pr3.md § 5) :

    0. lecture SANS verrou de la preuve (indicative : clés à verrouiller) ;
    1. pg_advisory_xact_lock, UN appel trié (common.lock_identities) :
       identité de la preuve ('activity_proof', propriétaire, nature, id :
       celle du bloc B), clé d'idempotence de l'événement ('event_key', P.id),
       opération canonique et opération historique du même parcours
       ('event_source_operation' : celles de events.record_event) ;
    2. participation (campaign_id, user_id) FOR SHARE ;
    3. campagne FOR SHARE (campaigns.lock_campaign) ;
    4. visite d'origine FOR SHARE (provenance contrôlée sur une ligne stable ;
       le contrôle de FK de visite de l'attribution, 0024, n'attend jamais
       une transition 2B-1, qui prend la visite FOR UPDATE, après l'horloge) ;
    5. interaction : lecture sans verrou (immuable) ;
    6. preuve FOR UPDATE, rechargée ; identité comparée à la lecture 0 ;
    7. événements et attributions : lectures (sérialisées par le rang 1) ;
    8. preuve certified seulement : utilisateur FOR KEY SHARE, en DERNIER (le
       contrôle de FK user_id de l'INSERT, imposé par 0022, n'attend plus
       après l'horloge ; une écriture Portfolio F2 du même utilisateur, qui
       prend users FOR UPDATE, attend au plus la fin de cette transaction,
       comme pour l'INSERT de events.record_event) ;
    9. horloge : clock_timestamp() PostgreSQL (proofs.server_clock), APRÈS
       tous ces verrous : instant de décision ;
   10. écritures, dans UN SAVEPOINT, puis ULTIME horloge avant son RELEASE.
Aucune transaction ne détient un verrou F2 en attendant un verrou analytics :
aucun cycle (docs/v0_t08_2b2_pr3.md § 5).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import (
    ProductActivityEvent,
    ProductActivityEventAttribution,
    ProductActivityProof,
    ProductActivitySession,
    ProductActivitySessionInteraction,
    ProductCampaignParticipant,
    User,
)
from core.product_analytics import campaigns, common, events, proofs, retention
from core.product_analytics.common import (
    ACTIVE,
    ENROLLED,
    PRODUCT_EVENT_SCHEMA_VERSION,
    InvalidProductAnalyticsInput,
    InvalidRetentionScopes,
)

# --------------------------------------------------------------------------
# Vocabulaires fermés
# --------------------------------------------------------------------------

ABSENT = proofs.ABSENT
PENDING, CERTIFIED, RECORDED, REJECTED, INDETERMINATE = (proofs.PENDING, proofs.CERTIFIED, proofs.RECORDED,
                                                         proofs.REJECTED, proofs.INDETERMINATE)
FINALIZATION_STATES = proofs.DECISION_STATES
# Motifs d'une décision : ceux du registre (0024) et deux constats de
# relecture qui ne sont JAMAIS écrits (aucune donnée renvoyée).
RETENTION_EXPIRED = "retention_expired"
RECORD_INCOHERENT = "record_incoherent"
FINALIZATION_REASONS = proofs.RESOLUTION_REASONS + (RETENTION_EXPIRED, RECORD_INCOHERENT)
TECHNICAL_UNCERTAINTY = proofs.TECHNICAL_UNCERTAINTY

# Marge maximale de finalisation (aucune marge propre à ce bloc).
FINALIZATION_GRACE = common.SESSION_FINALIZATION_GRACE

# Champs d'identité communs à la preuve et à l'attribution (0024).
IDENTITY_FIELDS = ("campaign_id", "user_id", "visit_id", "interaction_key", "interaction_kind", "event_type",
                   "surface", "source_operation_kind", "source_operation_id")

ATTRIBUTION_SOURCE_OPERATION_UNIQUE = "uq_product_activity_event_attributions_source_operation"
ATTRIBUTION_PRIMARY_KEY = "product_activity_event_attributions_pkey"
# Courses perdues sur une unicité (écrivain hors service) : aucune écriture.
RACE_CONSTRAINTS = (events.IDEMPOTENCY_UNIQUE, events.SOURCE_OPERATION_UNIQUE, ATTRIBUTION_SOURCE_OPERATION_UNIQUE,
                    ATTRIBUTION_PRIMARY_KEY)


@dataclass(frozen=True, slots=True)
class FinalizationDecision:
    """Issue d'un appel. state : FINALIZATION_STATES (état de la preuve
    après l'appel, ABSENT si elle n'existe pas) ; reason :
    FINALIZATION_REASONS ou None ; written : cet appel a écrit (flush, NON
    durable avant le COMMIT de l'appelant) ; event_id : l'événement définitif
    VÉRIFIÉ (recorded seulement) ; resolved_at : résolution de la preuve."""
    state: str
    reason: str | None = None
    written: bool = False
    proof_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True)
class _Locked:
    proof: ProductActivityProof
    participation: ProductCampaignParticipant | None
    campaign: object
    visit: ProductActivitySession | None
    interaction: ProductActivitySessionInteraction | None
    recorded_event: ProductActivityEvent | None
    recorded_attribution: ProductActivityEventAttribution | None
    conflict: str | None
    now: datetime


# --------------------------------------------------------------------------
# Échéances
# --------------------------------------------------------------------------

def finalization_deadline(participation, campaign) -> datetime:
    """Borne STRICTE de la décision de finalisation d'une preuve certifiée
    (voir l'en-tête) ; au-delà, aucune certification légitime ne peut plus
    survenir : une preuve pending peut être résolue. La visite d'origine
    n'intervient pas (arbitrage R1)."""
    return min(participation.window_end_at + FINALIZATION_GRACE, campaign.close_by_at)


# --------------------------------------------------------------------------
# Verrous et relecture
# --------------------------------------------------------------------------

def _identity(proof) -> tuple:
    return (proof.id,) + tuple(getattr(proof, field) for field in IDENTITY_FIELDS)


def _event_probe(proof) -> dict:
    """Champs de requête qui déterminent les verrous d'événement."""
    return {"event_type": proof.event_type, "visit_id": proof.visit_id,
            "source_operation_kind": proof.source_operation_kind, "source_operation_id": proof.source_operation_id}


def lock_keys(proof) -> list:
    """Clés consultatives du rang 1 (un seul appel trié) : identité de la
    preuve (bloc B) et, pour une nature certifiable, les clés de l'événement
    (events.record_event) et de l'opération historique du même parcours."""
    keys = [common.identity_lock_key(proofs.PROOF_LOCK_KIND, proof.user_id, proof.source_operation_kind,
                                     proof.source_operation_id)]
    if proof.source_operation_kind in events.CERTIFIED_OPERATIONS:
        keys += events._certified_lock_keys(proof.user_id, proof.id, _event_probe(proof))
    return keys


def _shared(statement):
    return statement.with_for_update(read=True).execution_options(populate_existing=True)


def _lock(db, proof_id: uuid.UUID) -> _Locked | None:
    """Rangs 0 à 9 de l'ordre global (voir l'en-tête). None : aucune preuve (ou identité
    changée pendant l'attente)."""
    hint = db.execute(select(ProductActivityProof).where(ProductActivityProof.id == proof_id)
                      .execution_options(populate_existing=True)).scalar_one_or_none()
    if hint is None:
        return None
    identity = _identity(hint)  # indicative : relue après les verrous
    _, campaign_id, user_id, visit_id, interaction_key = identity[:5]
    common.lock_identities(db, *lock_keys(hint))
    participation = db.execute(_shared(select(ProductCampaignParticipant).where(
        ProductCampaignParticipant.campaign_id == campaign_id,
        ProductCampaignParticipant.user_id == user_id))).scalar_one_or_none()
    campaign = campaigns.lock_campaign(db, campaign_id, shared=True) if participation is not None else None
    visit = db.execute(_shared(select(ProductActivitySession).where(ProductActivitySession.id == visit_id))
                       ).scalar_one_or_none()
    interaction = db.execute(select(ProductActivitySessionInteraction).where(
        ProductActivitySessionInteraction.campaign_id == campaign_id,
        ProductActivitySessionInteraction.user_id == user_id,
        ProductActivitySessionInteraction.interaction_key == interaction_key)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    proof = db.execute(select(ProductActivityProof).where(ProductActivityProof.id == proof_id)
                       .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if proof is None or _identity(proof) != identity:
        return None
    recorded_event = recorded_attribution = None
    if proof.recorded_event_id is not None:
        recorded_event = db.execute(select(ProductActivityEvent).where(
            ProductActivityEvent.id == proof.recorded_event_id).execution_options(populate_existing=True)
        ).scalar_one_or_none()
        recorded_attribution = db.execute(select(ProductActivityEventAttribution).where(
            ProductActivityEventAttribution.event_id == proof.recorded_event_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()
    conflict = None
    if proof.status == CERTIFIED:  # seul état qui peut écrire un événement
        conflict = _conflict(db, proof)
        # DERNIER verrou : le contrôle de FK user_id de l'INSERT n'attendra
        # plus après l'horloge ; tenu jusqu'au COMMIT (quelques ms).
        db.execute(select(User.id).where(User.id == user_id).with_for_update(read=True, key_share=True))
    now = proofs.server_clock(db)  # APRÈS tous les verrous
    return _Locked(proof, participation, campaign, visit, interaction, recorded_event, recorded_attribution,
                   conflict, now)


def _conflict(db, proof) -> str | None:
    """Motif d'un occupant existant de la clé ou de l'opération (lecture
    seule, sous le rang 1), ou None. Jamais d'adoption."""
    if proof.source_operation_kind not in events.CERTIFIED_OPERATIONS:
        return None  # refusée avant (source_unverified)
    reason, occupant = events._certified_conflict(db, user_id=proof.user_id, idempotency_key=proof.id,
                                                  request=_event_probe(proof))
    if reason is not None:
        if reason == events.CONFLICT_SOURCE_OPERATION and occupant.visit_id != proof.visit_id:
            return proofs.VISIT_CONFLICT
        return proofs.OPERATION_CONFLICT
    attribution = db.execute(select(ProductActivityEventAttribution).where(
        ProductActivityEventAttribution.user_id == proof.user_id,
        ProductActivityEventAttribution.source_operation_kind == proof.source_operation_kind,
        ProductActivityEventAttribution.source_operation_id == proof.source_operation_id)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if attribution is not None:
        return proofs.VISIT_CONFLICT if attribution.visit_id != proof.visit_id else proofs.OPERATION_CONFLICT
    return None


# --------------------------------------------------------------------------
# Décisions
# --------------------------------------------------------------------------

def _decision(proof, *, written: bool = False, reason: str | None = None, event_id=None) -> FinalizationDecision:
    return FinalizationDecision(proof.status, reason, written, proof.id, event_id, proof.resolved_at)


def _terminal(ctx: _Locked) -> FinalizationDecision:
    """rejected / indeterminate : jamais ressuscitées ; motif stocké, ou
    refus de relecture hors conservation du registre."""
    proof = ctx.proof
    if proof.purge_after_at <= ctx.now:
        return _decision(proof, reason=RETENTION_EXPIRED)
    return _decision(proof, reason=proof.resolution_reason)


def _recorded_problem(db, ctx: _Locked) -> str | None:
    """L'événement et l'attribution existants sont-ils EXACTEMENT ceux que
    cette preuve a produits, et encore conservés ? Lecture seule ; aucune
    donnée comparée ni renvoyée hors conservation."""
    proof, event, attribution = ctx.proof, ctx.recorded_event, ctx.recorded_attribution
    if event is None or attribution is None or ctx.interaction is None or ctx.campaign is None:
        return RECORD_INCOHERENT
    if event.schema_version != PRODUCT_EVENT_SCHEMA_VERSION:
        return RECORD_INCOHERENT
    try:
        scopes = retention.validate_retention_columns(event.retention_scopes,
                                                      next_scope_expires_at=event.next_scope_expires_at,
                                                      purge_after_at=event.purge_after_at)
    except (InvalidRetentionScopes, InvalidProductAnalyticsInput):
        return RECORD_INCOHERENT
    if event.next_scope_expires_at <= ctx.now:
        return RETENTION_EXPIRED  # contrat 2A : aucune relecture d'un événement hors conservation
    try:
        expected = events._certified_request(
            user_id=proof.user_id, visit_id=proof.visit_id, idempotency_key=proof.id,
            source_operation_kind=proof.source_operation_kind, source_operation_id=proof.source_operation_id,
            occurred_at=ctx.interaction.recorded_at, resolved_at=proof.certified_at)
    except InvalidProductAnalyticsInput:
        return RECORD_INCOHERENT
    if any(getattr(event, field) != expected[field] for field in events.REQUEST_FIELDS):
        return RECORD_INCOHERENT
    if (event.id, event.user_id, event.idempotency_key, event.recorded_at, event.updated_at) != (
            proof.recorded_event_id, proof.user_id, proof.id, proof.resolved_at, proof.resolved_at):
        return RECORD_INCOHERENT
    if (tuple(getattr(attribution, field) for field in IDENTITY_FIELDS) != _identity(proof)[1:]
            or attribution.event_id != event.id or attribution.certified_at != proof.certified_at):
        return RECORD_INCOHERENT
    if scopes != (retention.campaign_scope(ctx.campaign, event.occurred_at),):
        return RECORD_INCOHERENT
    try:
        retention.verify_scopes_against_campaigns(db, scopes, user_id=proof.user_id, occurred_at=event.occurred_at)
    except InvalidRetentionScopes:
        return RECORD_INCOHERENT
    return None


def _recorded(db, ctx: _Locked) -> FinalizationDecision:
    """recorded : terminal et idempotent ; rien n'est recréé ni modifié."""
    proof = ctx.proof
    if proof.purge_after_at <= ctx.now:
        return _decision(proof, reason=RETENTION_EXPIRED)  # registre hors conservation : rien n'est relu
    problem = _recorded_problem(db, ctx)
    if problem is not None:
        return _decision(proof, reason=problem)
    return _decision(proof, event_id=ctx.recorded_event.id)


def _existing(db, ctx: _Locked) -> FinalizationDecision:
    """Décision sans écriture pour l'état stocké."""
    status = ctx.proof.status
    if status == RECORDED:
        return _recorded(db, ctx)
    if status in (REJECTED, INDETERMINATE):
        return _terminal(ctx)
    return _decision(ctx.proof)  # pending, certified


def _accepted_during_observation(recorded_at, participation, campaign) -> bool:
    """Interaction acceptée à partir de l'activation, dans [window_start_at,
    window_end_at) et avant close_by_at (bornes immuables)."""
    return (campaign.activated_at is not None and campaign.activated_at <= recorded_at
            and participation.window_start_at <= recorded_at < participation.window_end_at
            and recorded_at < campaign.close_by_at)


def _certification_problem(ctx: _Locked) -> tuple | None:
    """(état terminal, motif) qui interdit DÉFINITIVEMENT la finalisation de
    cette preuve certified, ou None. Contrôles sous verrou, horloge lue
    après les verrous."""
    proof, participation, campaign, visit, interaction = (ctx.proof, ctx.participation, ctx.campaign, ctx.visit,
                                                          ctx.interaction)
    rule = events.CERTIFIED_OPERATIONS.get(proof.source_operation_kind)
    if rule is None or (proof.event_type, proof.surface) != rule[:2]:
        return REJECTED, proofs.SOURCE_UNVERIFIED  # nature que F2 n'atteste pas (PR #250)
    if participation is None or campaign is None:
        return REJECTED, proofs.NOT_AUTHORIZED
    if visit is None or (visit.campaign_id, visit.user_id) != (proof.campaign_id, proof.user_id):
        return REJECTED, proofs.VISIT_CONFLICT
    if interaction is None or interaction.interaction_kind != proofs.ACTIVITY:
        return REJECTED, proofs.ORIGIN_UNVERIFIED
    if interaction.session_id != proof.visit_id:
        return REJECTED, proofs.VISIT_CONFLICT
    if not interaction.recorded_at <= proof.created_at <= proof.certified_at:
        return INDETERMINATE, proofs.TECHNICAL_UNCERTAINTY  # chronologie stockée incohérente : aucune date inventée
    if not _accepted_during_observation(interaction.recorded_at, participation, campaign):
        return REJECTED, proofs.ORIGIN_UNVERIFIED
    if proof.certified_at >= participation.window_end_at or proof.certified_at >= campaign.close_by_at:
        return REJECTED, proofs.WINDOW_EXPIRED  # certification hors échéance : jamais admissible
    if participation.participation_status != ENROLLED or campaign.status != ACTIVE:
        return REJECTED, proofs.NOT_AUTHORIZED  # retrait, clôture ou annulation antérieurs à la décision
    if ctx.now >= finalization_deadline(participation, campaign):
        return INDETERMINATE, proofs.FINALIZATION_EXPIRED
    return None


def _no_longer_admissible(ctx: _Locked, scope, instant: datetime) -> tuple | None:
    """La décision prise à ctx.now est-elle encore admissible à l'ultime
    horloge `instant`, lue après les écritures (qui ont pu attendre) ?
    (état, motif) sinon. Les autres conditions sont tenues par les verrous
    (participation, campagne, visite, preuve) et ne peuvent pas changer."""
    if instant < ctx.now:
        return CERTIFIED, proofs.TECHNICAL_UNCERTAINTY  # horloge régressive : rien d'écrit
    if instant >= finalization_deadline(ctx.participation, ctx.campaign):
        return INDETERMINATE, proofs.FINALIZATION_EXPIRED
    if scope.expires_at <= instant:
        return REJECTED, proofs.NOT_AUTHORIZED  # conservation autorisée échue
    return None


class _StaleDecision(Exception):
    """Levée DANS le SAVEPOINT pour l'annuler entièrement."""

    def __init__(self, problem: tuple, instant: datetime):
        super().__init__(problem)
        self.problem, self.instant = problem, instant


def _resolve(db, proof, status: str, reason: str, now: datetime) -> FinalizationDecision:
    """Résolution terminale (certified_at éventuel conservé)."""
    def apply():
        proof.status = status
        proof.resolution_reason = reason
        proof.resolved_at = now
        proof.purge_after_at = now + proofs.REGISTRY_RETENTION
        proof.recorded_event_id = None
    with db.begin_nested():
        apply()
        db.flush()
    return _decision(proof, written=True, reason=reason)


def _attribution(proof, event) -> ProductActivityEventAttribution:
    return ProductActivityEventAttribution(event_id=event.id, certified_at=proof.certified_at,
                                           **{field: getattr(proof, field) for field in IDENTITY_FIELDS})


def _mark_recorded(proof, event_id: uuid.UUID, now: datetime) -> None:
    proof.status = RECORDED
    proof.recorded_event_id = event_id
    proof.resolved_at = now
    proof.resolution_reason = None
    proof.purge_after_at = now + proofs.REGISTRY_RETENTION


def _record(db, ctx: _Locked, request: dict, scope) -> FinalizationDecision:
    """Les trois écritures dans UN SAVEPOINT (ordre des FK de 0024), puis
    l'ultime horloge, dans ce SAVEPOINT, avant son RELEASE."""
    proof, now = ctx.proof, ctx.now
    try:
        with db.begin_nested():
            event = events._stage_certified_event(db, user_id=proof.user_id, idempotency_key=proof.id,
                                                  request=request, scope=scope, now=now)  # 1. événement
            db.add(_attribution(proof, event))  # 2. attribution
            db.flush()
            _mark_recorded(proof, event.id, now)  # 3. preuve
            db.flush()
            final = proofs.server_clock(db)  # 4. après toute attente SQL des écritures
            problem = _no_longer_admissible(ctx, scope, final)
            if problem is not None:
                raise _StaleDecision(problem, final)  # les trois écritures sont annulées
    except _StaleDecision as stale:
        state, reason = stale.problem
        if state == CERTIFIED:
            return FinalizationDecision(CERTIFIED, reason, False, proof.id)
        return _resolve(db, proof, state, reason, stale.instant)  # instant réel, jamais antidaté
    except IntegrityError as exc:
        if any(common.is_unique_violation(exc, constraint) for constraint in RACE_CONSTRAINTS):
            # Écrivain hors service : les trois écritures sont annulées ;
            # aucune décision sur un état antérieur à l'attente.
            return FinalizationDecision(CERTIFIED, proofs.OPERATION_CONFLICT, False, proof.id)
        raise
    return FinalizationDecision(RECORDED, None, True, proof.id, event.id, now)


# --------------------------------------------------------------------------
# Points d'entrée (la transaction appartient à l'appelant)
# --------------------------------------------------------------------------

def finalize_certified_proof(db, *, proof_id: uuid.UUID) -> FinalizationDecision:
    """Finalisation atomique d'une preuve certified (voir l'en-tête)."""
    common.require_uuid(proof_id, "proof_id")
    ctx = _lock(db, proof_id)
    if ctx is None:
        return FinalizationDecision(ABSENT)
    proof, now = ctx.proof, ctx.now
    if proof.status != CERTIFIED:
        return _existing(db, ctx)  # pending : jamais enregistrable ; résolues : jamais ressuscitées
    if now < proof.certified_at:
        return _decision(proof, reason=proofs.TECHNICAL_UNCERTAINTY)  # horloge régressive : rien d'écrit
    problem = _certification_problem(ctx)
    if problem is not None:
        return _resolve(db, proof, *problem, now)
    request = events._certified_request(
        user_id=proof.user_id, visit_id=proof.visit_id, idempotency_key=proof.id,
        source_operation_kind=proof.source_operation_kind, source_operation_id=proof.source_operation_id,
        occurred_at=ctx.interaction.recorded_at, resolved_at=proof.certified_at)
    scope = retention.campaign_scope(ctx.campaign, request["occurred_at"])
    if scope.expires_at <= now:
        return _resolve(db, proof, REJECTED, proofs.NOT_AUTHORIZED, now)  # conservation autorisée échue
    retention.verify_scopes_against_campaigns(db, [scope], user_id=proof.user_id, occurred_at=request["occurred_at"])
    if ctx.conflict is not None:
        return _resolve(db, proof, REJECTED, ctx.conflict, now)
    return _record(db, ctx, request, scope)


def resolve_expired_pending_proof(db, *, proof_id: uuid.UUID) -> FinalizationDecision:
    """pending -> indeterminate / finalization_expired, seulement une fois la
    période de finalisation définitivement close ; tout autre état est rendu
    sans écriture."""
    common.require_uuid(proof_id, "proof_id")
    ctx = _lock(db, proof_id)
    if ctx is None:
        return FinalizationDecision(ABSENT)
    proof, now = ctx.proof, ctx.now
    if proof.status != PENDING:
        return _existing(db, ctx)
    if ctx.participation is None or ctx.campaign is None or now < proof.created_at:
        return _decision(proof, reason=proofs.TECHNICAL_UNCERTAINTY)  # aucune date inventée
    if now < finalization_deadline(ctx.participation, ctx.campaign):
        return _decision(proof)  # une certification légitime reste possible : jamais résolue arbitrairement
    return _resolve(db, proof, INDETERMINATE, proofs.FINALIZATION_EXPIRED, now)


def observe_proof(db, *, proof_id: uuid.UUID) -> FinalizationDecision:
    """État vérifié de la preuve, sous les mêmes verrous, SANS écriture
    (confirmation d'un COMMIT incertain)."""
    common.require_uuid(proof_id, "proof_id")
    ctx = _lock(db, proof_id)
    if ctx is None:
        return FinalizationDecision(ABSENT)
    return _existing(db, ctx)
