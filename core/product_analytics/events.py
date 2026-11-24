"""Validation et persistance des événements d'activité produit (V0-T08
Lot 2A) — DORMANTE.

record_event est le seul point d'écriture de product_activity_events. Il
n'existe aucune fonction de mise à jour, de suppression ni de listing.

1. Validation stricte, AVANT tout accès à la base : paramètres nommés
   exacts (aucun champ libre supplémentaire possible), vocabulaires fermés
   (7 event_type, 3 sources, 4 outcomes, 4 delivery_status, surfaces,
   routes exécutées et types d'opération du lot 2A), cohérence par
   event_type (common.EVENT_TYPE_RULES) et règles transverses :
   - source_operation_kind / source_operation_id présents ensemble, et
     seulement pour la source server_business (opération métier canonique) ;
   - executed_route seulement pour server_business ;
   - outcome in_progress seulement pour server_business, et resolved_at
     NULL si et seulement si outcome = in_progress ;
   - delivery_status autre que not_applicable seulement pour
     server_business, et delivered_at présent si et seulement si
     delivery_status = confirmed ;
   - occurred_at <= resolved_at / delivered_at <= recorded_at (= maintenant).
   Aucun texte libre, aucune donnée financière, pédagogique ou personnelle
   autre que user_id n'est accepté.

2. Utilisateur existant (aucune création implicite).

   Verrous consultatifs d'identité (audit R1), en ordre croissant : clé
   d'idempotence, et selon le cas session_started de la visite, opération
   canonique. Deux écritures concurrentes d'une même identité sont
   sérialisées ici, avant toute décision.

3. Idempotence par (user_id, idempotency_key) : si la clé existe, l'événement
   stocké est revalidé (version de schéma, structure et échéances de la
   conservation) ; si au moins une finalité est expirée à l'instant présent
   (totalement ou partiellement : next_scope_expires_at <= maintenant),
   EventRetentionExpired, sans écriture, sans suppression et sans comparer
   ni renvoyer les données stockées ; sinon rattachement des finalités
   vérifié, puis l'événement est renvoyé tel quel lorsque la requête est
   identique (created=False, aucune écriture), EventIdempotencyCollision
   sinon. Une répétition ne modifie jamais l'événement stocké. La clé reste
   consommée tant que la ligne existe : sa suppression relève de la purge
   du lot 2C.

4. Autorisation (refus fermé, EventNotAuthorized) : au moins une
   participation enrolled de l'utilisateur, verrouillée en partage avec sa
   campagne (FOR SHARE : une clôture, une annulation ou un retrait
   concurrent attend, ou est vu), dans une campagne active, dont la fenêtre
   [window_start_at, window_end_at) et la période [activated_at,
   close_by_at) couvrent occurred_at, et dont l'échéance de conservation
   occurred_at + retention_days n'est pas déjà dépassée. Chaque campagne
   retenue devient une finalité de conservation.

   Politique de retard (audit R1) : AUCUNE fenêtre de grâce
   (LATE_EVENT_GRACE = 0). L'événement doit en outre être ENREGISTRÉ pendant
   que la campagne collecte (maintenant < close_by_at, quel que soit le
   statut : une campagne restée techniquement active après son échéance
   n'accepte plus rien) et pendant que la fenêtre de participation est
   ouverte (maintenant < window_end_at). Un événement antidaté dans la
   période autorisée mais transmis après l'une de ces échéances est refusé.
   Tant que la fenêtre et la campagne sont ouvertes, aucun retard maximal
   propre n'est imposé (point ouvert d'arbitrage).

   LATE_EVENT_GRACE = 0 est une RESTRICTION PROVISOIRE du service dormant,
   pas le contrat produit final. Le contrat produit Vague 0 admet les
   événements réellement survenus pendant la fenêtre, y compris ceux remis à
   la clôture d'une session commencée avant l'échéance (fin de session après
   30 minutes d'inactivité), sous réserve d'une validation fiable ; il exclut
   tout événement survenu après l'échéance. Ce mécanisme de remise (preuve de
   session, borne de remise, endpoint) relève du lot 2B et n'existe pas ici :
   le lot 2A ne le simule par aucune grâce générique. Invariant à conserver
   en 2B : aucune donnée survenue après window_end_at n'entre dans les
   métriques (déjà garanti ici par le filtre occurred_at < window_end_at).

   Horloge (audit R1) : maintenant est lu APRÈS l'acquisition de tous les
   verrous (identité, participations, campagnes). Une échéance franchie
   pendant une attente de verrou est donc vue et refusée.

5. Unicités métier : un seul session_started par (user_id, visit_id) ; un
   seul événement par opération canonique (user_id, source_operation_kind,
   source_operation_id) ; (bloc C) aucune nature historique pour une
   opération déjà enregistrée par le chemin certifié. Occupées par un autre
   événement : EventUniquenessConflict, sans écriture.

6. INSERT dans un SAVEPOINT : une course perdue sur la clé d'idempotence est
   relue et traitée comme au point 3 ; sur une unicité métier, elle lève
   EventUniquenessConflict ; toute autre IntegrityError est propagée. Le
   SAVEPOINT annulé laisse la transaction de l'appelant utilisable ; le
   service ne fait jamais commit() ni rollback() de celle-ci.

Lot 2B-2, bloc C (PR 3) — chemin CERTIFIÉ, second et dernier chemin
d'écriture, privé et contrôlé :

- record_event (ci-dessus) reste le contrat historique 2A, INCHANGÉ : ses
  natures server_business restent assistant_delivery / portfolio_write
  (common.EVENT_TYPE_RULES) ; il refuse toujours, avant tout accès à la
  base, les natures canoniques attestées par F2 (PR #250). Un événement
  écrit par lui ne peut recevoir aucune attribution (CHECK et FK de 0024) :
  il n'est JAMAIS une mesure métier certifiée.
- Les natures canoniques (CERTIFIED_OPERATIONS) ne sont écrites que par
  _stage_certified_event, appelé UNIQUEMENT par
  core/product_analytics/finalization.py (inventaire testé) avec une
  requête construite par _certified_request depuis une preuve certified
  persistée et verrouillée : source server_business, outcome succeeded,
  route f2, livraison imposée par la nature. Même validation que 2A
  (_validate_request, seule la règle diffère : nature canonique au lieu de
  la nature historique), même construction de ligne (_event_row), mêmes
  documents de conservation (retention). Aucun paramètre ne permet
  d'affaiblir une validation ; le chemin exige d'être exécuté dans le
  SAVEPOINT de l'appelant (atomicité événement / attribution / preuve),
  n'ouvre aucun SAVEPOINT propre et ne fait ni commit() ni rollback().
- Collision avec l'historique : une nature canonique et la nature 2A du même
  parcours (CERTIFIED_OPERATIONS : decryptage_delivery_ack ->
  assistant_delivery, portfolio_position_* -> portfolio_write) partagent
  leurs verrous consultatifs (_certified_lock_keys) ; un événement 2A portant
  le même identifiant d'opération est un conflit (_certified_conflict),
  jamais une correspondance présumée ni un double événement. Réciproquement,
  record_event refuse (EventUniquenessConflict, source_operation) une nature
  historique dont l'opération est déjà enregistrée par le chemin certifié
  (point 5, seul durcissement du contrat 2A).
"""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import ProductActivityEvent, ProductCampaign, ProductCampaignParticipant
from core.product_analytics import common, retention
from core.product_analytics.common import (
    ACTIVE,
    CONFIRMED,
    DELIVERY_STATUSES,
    ENROLLED,
    EVENT_TYPE_RULES,
    EVENT_TYPES,
    EXECUTED_ROUTES,
    IN_PROGRESS,
    NOT_APPLICABLE,
    OUTCOMES,
    PRODUCT_EVENT_SCHEMA_VERSION,
    SERVER_BUSINESS,
    SOURCES,
    SURFACES,
    EventIdempotencyCollision,
    EventNotAuthorized,
    EventRetentionExpired,
    EventUniquenessConflict,
    InvalidProductAnalyticsInput,
    UnsupportedProductEventSchema,
)

IDEMPOTENCY_UNIQUE = "uq_product_activity_events_user_idempotency_key"
SESSION_START_UNIQUE = "uq_product_activity_events_visit_session_start"
SOURCE_OPERATION_UNIQUE = "uq_product_activity_events_source_operation"
SESSION_STARTED = "session_started"
# Politique de retard : aucune fenêtre de grâce après close_by_at ni après
# window_end_at. RESTRICTION PROVISOIRE du service dormant (pas le contrat
# produit final) : la remise en fin de session est à concevoir en 2B, sous
# validation fiable, et ne sera pas une grâce générique appliquée ici. Toute
# valeur non nulle est une décision du fondateur.
LATE_EVENT_GRACE = timedelta(0)

# Champs fournis par l'appelant : ce sont eux, et eux seuls, qu'une
# répétition idempotente doit reproduire à l'identique.
REQUEST_FIELDS = ("visit_id", "event_type", "surface", "executed_route", "source", "source_operation_kind",
                  "source_operation_id", "outcome", "delivery_status", "occurred_at", "resolved_at",
                  "delivered_at")

# Bloc C (2B-2 PR 3) : natures canoniques attestées par F2 (PR #250, couples
# fermés de 0024) -> (event_type, surface, nature historique 2A du même
# parcours). Éducation et Coach (assistant_delivery) n'en font PAS partie :
# aucune preuve de réussite F2 n'existe pour ces parcours.
CERTIFIED_OPERATIONS = {
    "decryptage_delivery_ack": ("decrypter_used", "decrypter", "assistant_delivery"),
    "portfolio_position_create": ("portfolio_used", "portfolio", "portfolio_write"),
    "portfolio_position_update": ("portfolio_used", "portfolio", "portfolio_write"),
    "portfolio_position_delete": ("portfolio_used", "portfolio", "portfolio_write"),
}
CERTIFIED_ROUTE = "f2"
CERTIFIED_OUTCOME = "succeeded"
# Seule surface dont l'opération certifiée est une livraison confirmée.
CERTIFIED_DELIVERIES = frozenset({"decrypter_used"})
# Conflits du chemin certifié (vocabulaire fermé, aucun identifiant).
CONFLICT_IDEMPOTENCY_KEY, CONFLICT_SOURCE_OPERATION, CONFLICT_HISTORICAL_OPERATION = (
    "idempotency_key", "source_operation", "historical_operation")


def _validate(*, user_id, visit_id, idempotency_key, event_type, surface, executed_route, source,
              source_operation_kind, source_operation_id, outcome, delivery_status, occurred_at, resolved_at,
              delivered_at) -> dict:
    """Requête du contrat historique 2A : règles common.EVENT_TYPE_RULES."""
    return _validate_request(EVENT_TYPE_RULES, user_id=user_id, visit_id=visit_id, idempotency_key=idempotency_key,
                             event_type=event_type, surface=surface, executed_route=executed_route, source=source,
                             source_operation_kind=source_operation_kind, source_operation_id=source_operation_id,
                             outcome=outcome, delivery_status=delivery_status, occurred_at=occurred_at,
                             resolved_at=resolved_at, delivered_at=delivered_at)


def _validate_request(rules, *, user_id, visit_id, idempotency_key, event_type, surface, executed_route, source,
                      source_operation_kind, source_operation_id, outcome, delivery_status, occurred_at,
                      resolved_at, delivered_at) -> dict:
    """Validation commune aux deux chemins ; seule la table de règles
    diffère (EVENT_TYPE_RULES pour 2A, règle canonique fermée pour le bloc
    C). Aucune option ne relâche un contrôle."""
    common.require_user_id(user_id)
    common.require_uuid(visit_id, "visit_id")
    common.require_uuid(idempotency_key, "idempotency_key")
    common.require_choice(event_type, EVENT_TYPES, "event_type")
    common.require_choice(source, SOURCES, "source")
    common.require_choice(outcome, OUTCOMES, "outcome")
    common.require_choice(delivery_status, DELIVERY_STATUSES, "delivery_status")
    common.require_choice(surface, SURFACES, "surface")
    if executed_route is not None:
        common.require_choice(executed_route, EXECUTED_ROUTES, "executed_route")
    occurred_at = common.require_aware(occurred_at, "occurred_at")
    resolved_at = common.require_optional_aware(resolved_at, "resolved_at")
    delivered_at = common.require_optional_aware(delivered_at, "delivered_at")

    rule = rules.get(event_type)
    if rule is None:
        raise InvalidProductAnalyticsInput(f"event_type : {event_type} non admis sur ce chemin")
    if source not in rule.sources:
        raise InvalidProductAnalyticsInput(f"source : {source} non admise pour {event_type}")
    if rule.surface is not None and surface != rule.surface:
        raise InvalidProductAnalyticsInput(f"surface : {rule.surface} requise pour {event_type}")
    business = source == SERVER_BUSINESS
    if (source_operation_kind is None) != (source_operation_id is None):
        raise InvalidProductAnalyticsInput("source_operation_kind / source_operation_id : paire incomplète")
    if business:
        if source_operation_kind != rule.operation_kind:
            raise InvalidProductAnalyticsInput(f"source_operation_kind : {rule.operation_kind} requis")
        common.require_uuid(source_operation_id, "source_operation_id")
    elif source_operation_kind is not None:
        raise InvalidProductAnalyticsInput("source_operation_kind : réservé à la source server_business")
    if executed_route is not None and not business:
        raise InvalidProductAnalyticsInput("executed_route : réservé à la source server_business")
    if event_type == SESSION_STARTED and outcome != "succeeded":
        raise InvalidProductAnalyticsInput("outcome : succeeded requis pour session_started")
    if outcome == IN_PROGRESS and not business:
        raise InvalidProductAnalyticsInput("outcome : in_progress réservé à la source server_business")
    if (resolved_at is None) != (outcome == IN_PROGRESS):
        raise InvalidProductAnalyticsInput("resolved_at : requis si et seulement si l'outcome est terminal")
    if delivery_status != NOT_APPLICABLE and not business:
        raise InvalidProductAnalyticsInput("delivery_status : réservé à la source server_business")
    if (delivered_at is not None) != (delivery_status == CONFIRMED):
        raise InvalidProductAnalyticsInput("delivered_at : requis si et seulement si la livraison est confirmée")
    for field, value in (("resolved_at", resolved_at), ("delivered_at", delivered_at)):
        if value is not None and value < occurred_at:
            raise InvalidProductAnalyticsInput(f"{field} : antérieure à occurred_at")
    return {"visit_id": visit_id, "event_type": event_type, "surface": surface, "executed_route": executed_route,
            "source": source, "source_operation_kind": source_operation_kind,
            "source_operation_id": source_operation_id, "outcome": outcome, "delivery_status": delivery_status,
            "occurred_at": occurred_at, "resolved_at": resolved_at, "delivered_at": delivered_at}


def _event_by_key(db, user_id, idempotency_key) -> ProductActivityEvent | None:
    return db.execute(
        select(ProductActivityEvent)
        .where(ProductActivityEvent.user_id == user_id, ProductActivityEvent.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _canonical_or_collision(db, event: ProductActivityEvent, request: dict, *, now: datetime) -> ProductActivityEvent:
    """Répétition : l'événement stocké est revalidé (version, conservation,
    échéances à l'instant `now`, lu après les verrous) puis renvoyé tel quel
    si la requête est identique. Lecture seule."""
    if event.schema_version != PRODUCT_EVENT_SCHEMA_VERSION:
        raise UnsupportedProductEventSchema(f"événement {event.id} : version de schéma non prise en charge")
    scopes = retention.validate_retention_columns(event.retention_scopes,
                                                  next_scope_expires_at=event.next_scope_expires_at,
                                                  purge_after_at=event.purge_after_at)
    # next_scope_expires_at = première échéance : une seule finalité expirée
    # suffit à refuser (expiration partielle comprise). Contrôle AVANT toute
    # comparaison : les données d'un événement hors conservation ne sont ni
    # comparées ni renvoyées.
    if event.next_scope_expires_at <= now:
        raise EventRetentionExpired(f"événement {event.id} : finalité de conservation expirée")
    retention.verify_scopes_against_campaigns(db, scopes, user_id=event.user_id, occurred_at=event.occurred_at)
    if any(getattr(event, field) != request[field] for field in REQUEST_FIELDS):
        raise EventIdempotencyCollision(
            f"événement {event.id} : clé d'idempotence réutilisée avec d'autres données")
    return event


def _lock_authorizations(db, *, user_id: str, occurred_at: datetime) -> list:
    """Participations enrolled couvrant occurred_at et leurs campagnes, sous
    verrous partagés (participations puis campagnes, ordre déterministe).
    Retourne [(participation, campagne)]. Aucune décision temporelle ici."""
    participations = db.execute(
        select(ProductCampaignParticipant)
        .where(ProductCampaignParticipant.user_id == user_id,
               ProductCampaignParticipant.participation_status == ENROLLED,
               ProductCampaignParticipant.window_start_at <= occurred_at,
               ProductCampaignParticipant.window_end_at > occurred_at)
        .order_by(ProductCampaignParticipant.campaign_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalars().all()
    if not participations:
        return []
    campaigns = {campaign.id: campaign for campaign in db.execute(
        select(ProductCampaign)
        .where(ProductCampaign.id.in_([p.campaign_id for p in participations]))
        .order_by(ProductCampaign.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalars().all()}
    return [(participation, campaigns[participation.campaign_id]) for participation in participations]


def _authorized_scopes(authorizations, *, occurred_at: datetime, now: datetime) -> list:
    """Finalités des autorisations encore ouvertes à l'instant `now` (lu
    APRÈS les verrous) : campagne active, occurred_at dans [activated_at,
    close_by_at) (occurred_at dans la fenêtre : filtre SQL de
    _lock_authorizations, fenêtres immuables), enregistrement avant
    close_by_at et avant window_end_at (LATE_EVENT_GRACE = 0), conservation
    non échue."""
    scopes = []
    for participation, campaign in authorizations:
        if campaign.status != ACTIVE or campaign.activated_at is None:
            continue
        if not campaign.activated_at <= occurred_at < campaign.close_by_at:
            continue
        if now >= campaign.close_by_at + LATE_EVENT_GRACE or now >= participation.window_end_at + LATE_EVENT_GRACE:
            continue
        scope = retention.campaign_scope(campaign, occurred_at)
        if scope.expires_at <= now:
            continue
        scopes.append(scope)
    return scopes


def _source_operation_lock_key(user_id: str, kind: str, operation_id: uuid.UUID) -> int:
    return common.identity_lock_key("event_source_operation", user_id, kind, operation_id)


def _identity_locks(user_id: str, request: dict, idempotency_key: uuid.UUID) -> list:
    keys = [common.identity_lock_key("event_key", user_id, idempotency_key)]
    if request["event_type"] == SESSION_STARTED:
        keys.append(common.identity_lock_key("event_session_start", user_id, request["visit_id"]))
    if request["source_operation_kind"] is not None:
        keys.append(_source_operation_lock_key(user_id, request["source_operation_kind"],
                                               request["source_operation_id"]))
    return keys


def _business_uniqueness(db, *, user_id: str, request: dict) -> None:
    if request["event_type"] == SESSION_STARTED:
        taken = db.execute(select(ProductActivityEvent.id).where(
            ProductActivityEvent.user_id == user_id, ProductActivityEvent.visit_id == request["visit_id"],
            ProductActivityEvent.event_type == SESSION_STARTED)).scalar_one_or_none()
        if taken is not None:
            raise EventUniquenessConflict("session_started déjà enregistré pour cette visite",
                                          reason="session_start")
    if request["source_operation_kind"] is not None:
        taken = db.execute(select(ProductActivityEvent.id).where(
            ProductActivityEvent.user_id == user_id,
            ProductActivityEvent.source_operation_kind == request["source_operation_kind"],
            ProductActivityEvent.source_operation_id == request["source_operation_id"])).scalar_one_or_none()
        if taken is not None:
            raise EventUniquenessConflict("opération métier canonique déjà enregistrée", reason="source_operation")
        # Bloc C : la même opération déjà enregistrée par le chemin certifié
        # (nature canonique du même parcours) n'est jamais doublée par une
        # nature historique. Même verrou consultatif (_certified_lock_keys) :
        # sérialisé avec la finalisation.
        canonical = [kind for kind, rule in CERTIFIED_OPERATIONS.items()
                     if rule[2] == request["source_operation_kind"]]
        if canonical and db.execute(select(ProductActivityEvent.id).where(
                ProductActivityEvent.user_id == user_id,
                ProductActivityEvent.source_operation_kind.in_(canonical),
                ProductActivityEvent.source_operation_id == request["source_operation_id"])).first() is not None:
            raise EventUniquenessConflict("opération métier déjà enregistrée par le chemin certifié",
                                          reason="source_operation")


def _event_row(*, user_id: str, idempotency_key: uuid.UUID, request: dict, document: dict, next_expiry: datetime,
               purge_after: datetime, now: datetime) -> ProductActivityEvent:
    """Ligne d'événement (non ajoutée à la Session) : construction commune
    aux deux chemins d'écriture."""
    return ProductActivityEvent(
        id=uuid.uuid4(), user_id=user_id, idempotency_key=idempotency_key, recorded_at=now, updated_at=now,
        retention_scopes=document, next_scope_expires_at=next_expiry, purge_after_at=purge_after,
        schema_version=PRODUCT_EVENT_SCHEMA_VERSION, **request)


def record_event(
    db,
    *,
    user_id: str,
    visit_id: uuid.UUID,
    idempotency_key: uuid.UUID,
    event_type: str,
    surface: str,
    executed_route: str | None,
    source: str,
    source_operation_kind: str | None,
    source_operation_id: uuid.UUID | None,
    outcome: str,
    delivery_status: str,
    occurred_at: datetime,
    resolved_at: datetime | None,
    delivered_at: datetime | None,
) -> tuple[ProductActivityEvent, bool]:
    """Enregistre un événement. Retourne (événement, created)."""
    request = _validate(user_id=user_id, visit_id=visit_id, idempotency_key=idempotency_key, event_type=event_type,
                        surface=surface, executed_route=executed_route, source=source,
                        source_operation_kind=source_operation_kind, source_operation_id=source_operation_id,
                        outcome=outcome, delivery_status=delivery_status, occurred_at=occurred_at,
                        resolved_at=resolved_at, delivered_at=delivered_at)

    common.require_existing_user(db, user_id)
    common.lock_identities(db, *_identity_locks(user_id, request, idempotency_key))
    existing = _event_by_key(db, user_id, idempotency_key)
    if existing is not None:
        return _canonical_or_collision(db, existing, request, now=common.utcnow()), False

    authorizations = _lock_authorizations(db, user_id=user_id, occurred_at=request["occurred_at"])
    now = common.utcnow()  # après TOUS les verrous : identité, participations, campagnes
    for field in ("occurred_at", "resolved_at", "delivered_at"):
        if request[field] is not None and request[field] > now:
            raise InvalidProductAnalyticsInput(f"{field} : postérieure à l'enregistrement")
    scopes = _authorized_scopes(authorizations, occurred_at=request["occurred_at"], now=now)
    if not scopes:
        raise EventNotAuthorized("aucune participation active ne couvre cet événement")
    document = retention.build_retention_document(scopes)
    next_expiry, purge_after = retention.retention_bounds(scopes)
    _business_uniqueness(db, user_id=user_id, request=request)

    event = _event_row(user_id=user_id, idempotency_key=idempotency_key, request=request, document=document,
                       next_expiry=next_expiry, purge_after=purge_after, now=now)
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError as exc:
        if common.is_unique_violation(exc, IDEMPOTENCY_UNIQUE):
            # Défense finale (écrivain hors service) : relecture et décision
            # avec une horloge relue après l'attente sur l'index.
            winner = _event_by_key(db, user_id, idempotency_key)
            if winner is None:
                raise
            return _canonical_or_collision(db, winner, request, now=common.utcnow()), False
        if common.is_unique_violation(exc, SESSION_START_UNIQUE):
            raise EventUniquenessConflict("session_started déjà enregistré pour cette visite",
                                          reason="session_start") from None
        if common.is_unique_violation(exc, SOURCE_OPERATION_UNIQUE):
            raise EventUniquenessConflict("opération métier canonique déjà enregistrée",
                                          reason="source_operation") from None
        raise
    return event, True


# --------------------------------------------------------------------------
# Lot 2B-2, bloc C : chemin certifié (privé ; seul appelant :
# core/product_analytics/finalization.py, inventaire testé)
# --------------------------------------------------------------------------

def _certified_request(*, user_id: str, visit_id: uuid.UUID, idempotency_key: uuid.UUID, source_operation_kind: str,
                       source_operation_id: uuid.UUID, occurred_at: datetime, resolved_at: datetime) -> dict:
    """Requête d'un événement métier CERTIFIÉ, entièrement déterminée par la
    nature canonique : source server_business, outcome succeeded, route f2 ;
    Décrypter : livraison confirmed, delivered_at = occurred_at (instant
    serveur d'observation de la livraison déjà confirmée, jamais l'instant de
    réception par le navigateur, inconnu) ; Portfolio : not_applicable. Les
    valeurs proviennent de la preuve verrouillée (finalization.py), jamais
    d'une affirmation de réussite. Même validation que 2A, avec la seule
    règle canonique de cette nature."""
    common.require_choice(source_operation_kind, tuple(CERTIFIED_OPERATIONS), "source_operation_kind")
    event_type, surface, _historical = CERTIFIED_OPERATIONS[source_operation_kind]
    rules = {event_type: common.EventTypeRule((SERVER_BUSINESS,), surface, source_operation_kind)}
    delivered = event_type in CERTIFIED_DELIVERIES
    if resolved_at is None:
        raise InvalidProductAnalyticsInput("resolved_at : instant de certification requis")
    return _validate_request(
        rules, user_id=user_id, visit_id=visit_id, idempotency_key=idempotency_key, event_type=event_type,
        surface=surface, executed_route=CERTIFIED_ROUTE, source=SERVER_BUSINESS,
        source_operation_kind=source_operation_kind, source_operation_id=source_operation_id,
        outcome=CERTIFIED_OUTCOME, delivery_status=CONFIRMED if delivered else NOT_APPLICABLE,
        occurred_at=occurred_at, resolved_at=resolved_at, delivered_at=occurred_at if delivered else None)


def _certified_lock_keys(user_id: str, idempotency_key: uuid.UUID, request: dict) -> list:
    """Verrous consultatifs d'événement du chemin certifié : ceux de
    record_event pour cette requête (clé d'idempotence, opération canonique)
    ET l'opération historique 2A du même parcours et du même identifiant
    (une écriture 2A concurrente de cette opération est sérialisée). À
    acquérir dans le MÊME appel trié que les autres verrous de l'appelant."""
    historical = CERTIFIED_OPERATIONS[request["source_operation_kind"]][2]
    return _identity_locks(user_id, request, idempotency_key) + [
        _source_operation_lock_key(user_id, historical, request["source_operation_id"])]


def _certified_conflict(db, *, user_id: str, idempotency_key: uuid.UUID, request: dict) -> tuple:
    """(motif, événement) d'un événement EXISTANT qui interdit l'écriture
    certifiée, ou (None, None). Lecture seule, sous les verrous de
    _certified_lock_keys. Jamais de rattachement présumé : la clé
    d'idempotence occupée, l'opération canonique occupée ou la même opération
    enregistrée par le chemin 2A sont des conflits."""
    by_key = _event_by_key(db, user_id, idempotency_key)
    if by_key is not None:
        return CONFLICT_IDEMPOTENCY_KEY, by_key
    historical = CERTIFIED_OPERATIONS[request["source_operation_kind"]][2]
    for reason, kind in ((CONFLICT_SOURCE_OPERATION, request["source_operation_kind"]),
                         (CONFLICT_HISTORICAL_OPERATION, historical)):
        taken = db.execute(select(ProductActivityEvent).where(
            ProductActivityEvent.user_id == user_id, ProductActivityEvent.source_operation_kind == kind,
            ProductActivityEvent.source_operation_id == request["source_operation_id"])
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if taken is not None:
            return reason, taken
    return None, None


def _stage_certified_event(db, *, user_id: str, idempotency_key: uuid.UUID, request: dict, scope,
                           now: datetime) -> ProductActivityEvent:
    """INSERT (flush) de l'événement certifié DANS le SAVEPOINT de
    l'appelant, qui l'annule avec ses autres écritures. `request` : celle de
    _certified_request, revalidée ici ; `scope` : finalité de conservation de
    la campagne de la preuve (retention.campaign_scope), encore ouverte à
    `now`. N'ouvre aucun SAVEPOINT, ne fait ni commit() ni rollback()."""
    if not db.in_nested_transaction():
        raise InvalidProductAnalyticsInput("chemin certifié : SAVEPOINT de l'appelant requis")
    expected = _certified_request(
        user_id=user_id, visit_id=request["visit_id"], idempotency_key=idempotency_key,
        source_operation_kind=request["source_operation_kind"], source_operation_id=request["source_operation_id"],
        occurred_at=request["occurred_at"], resolved_at=request["resolved_at"])
    if request != expected:
        raise InvalidProductAnalyticsInput("chemin certifié : requête non canonique")
    for field in ("occurred_at", "resolved_at", "delivered_at"):
        if expected[field] is not None and expected[field] > now:
            raise InvalidProductAnalyticsInput(f"{field} : postérieure à l'enregistrement")
    # Rattachement de la finalité (campagne, politique, fenêtre, échéance <=
    # occurred_at + retention_days) : vérifié par l'appelant
    # (retention.verify_scopes_against_campaigns) sous ses verrous.
    if type(scope) is not retention.RetentionScope or scope.expires_at <= now:
        raise InvalidProductAnalyticsInput("chemin certifié : finalité de conservation ouverte requise")
    document = retention.build_retention_document([scope])
    next_expiry, purge_after = retention.retention_bounds([scope])
    event = _event_row(user_id=user_id, idempotency_key=idempotency_key, request=expected, document=document,
                       next_expiry=next_expiry, purge_after=purge_after, now=now)
    db.add(event)
    db.flush()
    return event

