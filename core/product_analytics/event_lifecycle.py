"""Cycle de vie des événements d'activité produit : expiration des
finalités de conservation et suppression physique des événements et de
leurs attributions (V0-T08 Lot 2C, PR 2) — DORMANT.

Table product_activity_events (0022), attributions définitives et registre
provisoire (0024), tous INCHANGÉS. Ce module ne crée, ne certifie, ne
finalise ni ne révise volontairement rien : il APPLIQUE les échéances déjà
stockées dans le document retention-scopes-v1 de chaque événement
(core/product_analytics/retention.py, inchangé). La transaction appartient
toujours à l'appelant (frontière privée core/_activity_event_lifecycle.py,
sans appelant) : flush seulement, jamais commit() ni rollback() ; chaque
écriture a lieu dans UN SAVEPOINT.

1. Sélections (lecture seule, sans verrou, jamais une autorisation) :

   select_expired_event_candidates  next_scope_expires_at <= horloge
                                    PostgreSQL, ordre (next_scope_expires_at,
                                    id), index existant
                                    ix_product_activity_events_next_scope_expires_at ;
   select_integrity_candidates      TOUS les événements, sans filtre
                                    temporel, ordre id (clé primaire) :
                                    parcours indépendant de la sélection
                                    nominale (anomalies qui lui sont
                                    invisibles comprises).

   Taille de page : entier exact, 1 <= limit <= 200, défaut 50 ; curseur
   strict issu de la SEULE photographie de la sélection (dernier candidat
   rendu), jamais OFFSET ; curseurs des deux sélections de types distincts
   (incompatibles). Candidats minimisés : identifiants internes seulement.

2. Expiration (expire_event_scopes), sous verrous, horloge PostgreSQL lue
   APRÈS les verrous (proofs.server_clock, clock_timestamp()) :

   - une finalité est expirée si expires_at <= horloge (égalité comprise) ;
   - aucune expirée : not_due, aucune écriture ;
   - événement HISTORIQUE (2A, nature non canonique, aucune attribution),
     certaines expirées seulement : document canonique reconstruit avec les
     finalités dont expires_at > horloge (retention.build_retention_document,
     retention.validate_scope_revision : ni ajout, ni prolongation),
     échéances recalculées (retention.retention_bounds : next avance,
     purge_after_at n'augmente jamais) ; UPDATE conditionnel de
     retention_scopes, next_scope_expires_at, purge_after_at et updated_at
     (= horloge) SEULEMENT, exactement une ligne : pruned ;
   - toutes expirées : suppression physique, dans UN SAVEPOINT, dans l'ordre
     imposé par les FK de 0024 : preuve recorded qui référence encore
     l'attribution (même avant ses 168 heures : la preuve ne prolonge jamais
     la conservation de l'événement), puis attribution, puis événement ;
     prédicats conditionnels, exactement une ligne chacun : purged ;
   - événement CERTIFIÉ (nature canonique events.CERTIFIED_OPERATIONS) :
     UNE finalité, celle de la campagne de son attribution ; jamais de
     retrait partiel, jamais modifié avant sa suppression.

   Aucun statut withdrawn / closed / cancelled n'avance une échéance ; aucune
   finalité n'est ajoutée ; aucune échéance n'est prolongée ; aucun document
   vide n'est stocké ; occurred_at reste la seule origine (aucune nouvelle
   durée calculée ici).

   Hors contrat (aucune écriture, conflict) : schéma non pris en charge,
   document ou échéances incohérents, finalité non rattachée à une
   participation de l'utilisateur couvrant occurred_at ou au-delà de
   l'autorisation de la campagne, événement certifié sans attribution ou à
   plusieurs finalités, événement historique avec attribution ou preuve,
   attribution ou preuve résiduelle discordante, identité ou dépendance
   modifiée entre les lectures. Horloge antérieure à updated_at (ou à une
   date des dépendances) : unknown / clock_incoherent, aucune écriture.

3. observe_event_retention : relecture SANS écriture (issue d'un COMMIT
   incertain). La frontière utilise le helper privé _observe_attempt, qui
   reprend les clés consultatives de l'opération TENTÉE (conservées en
   mémoire) avant toute lecture : une transaction encore en cours sur ces
   identités est attendue, même si la ligne semble avoir disparu.

4. Contrôle d'intégrité (inspect_event_integrity) : mêmes verrous (lignes en
   FOR SHARE), AUCUNE écriture, AUCUNE réparation, aucun rapport stocké ;
   valid / inconsistent / absent / unknown. Un événement simplement échu
   n'est pas incohérent. Détecter ne garantit pas l'effacement dans les
   délais : les anomalies relèvent de la procédure préalable à l'activation
   (lot 2C, PR 5).

Ordre global des verrous (docs/v0_t08_2c_pr2.md § 7) :

    0. L0 : lecture indicative SANS verrou de l'événement, de son attribution
       et de la preuve qui la référence ;
    1. union de TOUTES les clés nécessaires ; historique : clés de
       events.record_event (event_key, event_session_start,
       event_source_operation) ; certifié : clés du bloc C
       (finalization.lock_keys : activity_proof, event_key, opération
       canonique et opération historique équivalente), reconstruites même si
       la preuve a disparu ;
    2. pg_advisory_xact_lock : UN appel trié (common.lock_identities) ;
    3. L1 : relecture ; toute clé nécessaire non détenue : abandon ;
    4. participations FOR SHARE (campaign_id) ; 5. campagnes FOR SHARE (id) ;
    6. visite : aucun verrou (ni lue pour la décision, ni contrôlée par une
       FK lors d'un DELETE ou d'un UPDATE de colonnes non clés) ;
    7. preuve référençante FOR UPDATE ; 8. attribution FOR UPDATE ;
    9. événement FOR UPDATE ;
   10. L2 : relecture finale ; dépendance apparue, changée ou exigeant un
       verrou d'un rang antérieur : abandon ;
   11. validations ; 12. horloge ; 13. décision et écritures conditionnelles.
Jamais de clé consultative après le premier appel ; jamais de retour à un
rang antérieur.
"""
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from core.models import (
    ProductActivityEvent,
    ProductActivityEventAttribution,
    ProductActivityProof,
    ProductActivitySessionInteraction,
    ProductCampaign,
    ProductCampaignParticipant,
)
from core.product_analytics import common, events, finalization, proofs, retention
from core.product_analytics.common import (
    PRODUCT_EVENT_SCHEMA_VERSION,
    SERVER_BUSINESS,
    SOURCE_OPERATION_KINDS,
    InvalidProductAnalyticsInput,
    InvalidRetentionScopes,
    RetentionRevisionRefused,
)

# --------------------------------------------------------------------------
# Vocabulaires fermés et bornes opérationnelles
# --------------------------------------------------------------------------

# Limites opérationnelles proposées (pas des décisions produit).
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

EVENT_EXPIRY = "event_expiry"
EVENT_INTEGRITY = "event_integrity"
SELECTIONS = (EVENT_EXPIRY, EVENT_INTEGRITY)

HISTORICAL, CERTIFIED = "historical", "certified"

# Issues du service d'expiration.
PRUNED = "pruned"  # finalités expirées retirées (événement historique), écriture flushée
PURGED = "purged"  # événement (+ attribution, + preuve) supprimés, écriture flushée
NOT_DUE = "not_due"
ABSENT = "absent"
CONFLICT = "conflict"
UNKNOWN = "unknown"
EXPIRY_OUTCOMES = (PRUNED, PURGED, NOT_DUE, ABSENT, CONFLICT, UNKNOWN)
# Observation et intégrité.
PRESENT = "present"
UNSTABLE = "unstable"  # relecture : état ou dépendances changés pendant l'observation
VALID = "valid"
INCONSISTENT = "inconsistent"
INTEGRITY_OUTCOMES = (VALID, INCONSISTENT, ABSENT, UNKNOWN)

IDENTITY_CHANGED = "identity_changed"  # identité relue différente de la lecture précédente
DEPENDENCY_CHANGED = "dependency_changed"  # dépendance apparue, disparue ou exigeant une clé / un rang non tenu
SCHEMA_UNSUPPORTED = "schema_unsupported"
RETENTION_INCOHERENT = "retention_incoherent"  # document, échéances ou révision incohérents
ATTACHMENT_INCOHERENT = "attachment_incoherent"  # campagne, politique, participation ou fenêtre non rattachées
CLASSIFICATION_INCOHERENT = "classification_incoherent"  # historique / certifié hors contrat
ATTRIBUTION_INCOHERENT = "attribution_incoherent"
PROOF_INCOHERENT = "proof_incoherent"
CERTIFIED_INCOHERENT = "certified_incoherent"  # champs canoniques du bloc C (livraison, dates, nature) altérés
ORIGIN_INCOHERENT = "origin_incoherent"  # interaction d'origine absente ou discordante (occurred_at, visite)
CLOCK_INCOHERENT = "clock_incoherent"
WRITE_UNCONFIRMED = "write_unconfirmed"  # écriture conditionnelle : nombre de lignes inattendu
REASONS = (IDENTITY_CHANGED, DEPENDENCY_CHANGED, SCHEMA_UNSUPPORTED, RETENTION_INCOHERENT, ATTACHMENT_INCOHERENT,
           CLASSIFICATION_INCOHERENT, ATTRIBUTION_INCOHERENT, PROOF_INCOHERENT, CERTIFIED_INCOHERENT,
           ORIGIN_INCOHERENT, CLOCK_INCOHERENT, WRITE_UNCONFIRMED)

IDENTITY_FIELDS = finalization.IDENTITY_FIELDS  # identité commune preuve / attribution (0024)
# Champs de l'événement repris par l'attribution (FK composite de 0024).
EVENT_IDENTITY_FIELDS = ("user_id", "visit_id", "event_type", "surface", "source_operation_kind",
                         "source_operation_id")
# Anomalies structurelles converties en motif (jamais une exception non maîtrisée).
_STRUCTURE_ERRORS = (InvalidRetentionScopes, InvalidProductAnalyticsInput, RetentionRevisionRefused, TypeError,
                     ValueError, KeyError, AttributeError)


# --------------------------------------------------------------------------
# Contrats internes (aucun identifiant ni contenu dans les représentations)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, repr=False)
class ExpiryCursor:
    """Dernier candidat rendu de la sélection nominale (photographie de la
    sélection, jamais l'état après traitement). Interne, jamais journalisé."""
    position: datetime
    event_id: uuid.UUID

    def __repr__(self) -> str:
        return "ExpiryCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class IntegrityCursor:
    """Dernier candidat rendu du parcours d'intégrité (ordre id). Interne."""
    event_id: uuid.UUID

    def __repr__(self) -> str:
        return "IntegrityCursor(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class EventPage:
    """Page de candidats : identifiants internes d'événement seulement ;
    next_cursor None quand la sélection est épuisée (page incomplète)."""
    selection: str
    event_ids: tuple
    next_cursor: object

    def __repr__(self) -> str:
        return f"EventPage({self.selection}, {len(self.event_ids)}, more={self.next_cursor is not None})"


@dataclass(frozen=True, slots=True, repr=False)
class _EventState:
    """Photographie COMPLÈTE, en mémoire, de l'événement (toutes ses colonnes)
    et de ses dépendances (attribution, preuve référençante, interaction
    d'origine : toutes leurs colonnes), pour la relecture d'un COMMIT
    incertain ; jamais journalisée ni rendue à l'appelant final."""
    event: tuple
    attribution: tuple | None
    proof: tuple | None
    interaction: tuple | None

    def __repr__(self) -> str:
        return "_EventState(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _Attempt:
    """Contexte interne d'une écriture tentée : identités de verrouillage,
    état initial et état attendu (None : suppression)."""
    event_id: uuid.UUID
    keys: tuple
    before: _EventState
    after: _EventState | None

    def __repr__(self) -> str:
        return "_Attempt(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class EventDecision:
    """Issue du service. outcome : EXPIRY_OUTCOMES ; reason : REASONS ou None ;
    written : cet appel a écrit (flush, NON durable avant le COMMIT de
    l'appelant) ; attempt : contexte privé de l'écriture tentée."""
    outcome: str
    reason: str | None = None
    written: bool = False
    attempt: _Attempt | None = None

    def __repr__(self) -> str:
        return f"EventDecision({self.outcome}, {self.reason}, written={self.written})"


@dataclass(frozen=True, slots=True, repr=False)
class EventObservation:
    """Relecture sans écriture : outcome PRESENT / ABSENT ; state : photographie
    privée (None si absent)."""
    outcome: str
    state: _EventState | None = None

    def __repr__(self) -> str:
        return f"EventObservation({self.outcome})"


@dataclass(frozen=True, slots=True)
class IntegrityDecision:
    """Issue du contrôle d'intégrité : INTEGRITY_OUTCOMES, motif fermé."""
    outcome: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ProofIdentity:
    """Identité de preuve attendue par finalization.lock_keys, reconstruite
    depuis l'événement certifié (event.idempotency_key = identifiant de la
    preuve qui l'a produit), même si la preuve a déjà été purgée."""
    id: uuid.UUID
    user_id: str
    event_type: str
    visit_id: uuid.UUID
    source_operation_kind: str
    source_operation_id: uuid.UUID


@dataclass(slots=True)
class _Locked:
    event: ProductActivityEvent
    attribution: ProductActivityEventAttribution | None
    proof: ProductActivityProof | None
    interaction: ProductActivitySessionInteraction | None
    keys: tuple
    authorizations: dict  # campaign_id -> (participation, campagne), verrouillées en partage aux rangs 4-5


# --------------------------------------------------------------------------
# Validation des entrées (avant tout accès à la base)
# --------------------------------------------------------------------------

def _require_limit(limit) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidProductAnalyticsInput(f"limit : entier exact de 1 à {MAX_PAGE_SIZE} requis")
    return limit


def _require_cursor(after, cursor_type):
    if after is None:
        return None
    if type(after) is not cursor_type:
        raise InvalidProductAnalyticsInput(f"after : {cursor_type.__name__} requis")
    if cursor_type is ExpiryCursor:
        common.require_aware(after.position, "position")
    if type(after.event_id) is not uuid.UUID:
        raise InvalidProductAnalyticsInput("event_id : uuid.UUID requis")
    return after


def _require_event_id(event_id) -> uuid.UUID:
    if type(event_id) is not uuid.UUID:
        raise InvalidProductAnalyticsInput("event_id : uuid.UUID requis")
    return event_id


# --------------------------------------------------------------------------
# Sélections bornées (lecture seule, sans verrou)
# --------------------------------------------------------------------------

def _expiry_query(now: datetime, limit: int, after: ExpiryCursor | None):
    """Index ix_product_activity_events_next_scope_expires_at : parcours borné
    dans l'ordre de l'index (borne inférieure du curseur en condition d'index,
    départage par id)."""
    event = ProductActivityEvent
    statement = select(event.id, event.next_scope_expires_at).where(event.next_scope_expires_at <= now)
    if after is not None:
        statement = statement.where(event.next_scope_expires_at >= after.position,
                                    tuple_(event.next_scope_expires_at, event.id) > tuple_(after.position,
                                                                                           after.event_id))
    return statement.order_by(event.next_scope_expires_at, event.id).limit(limit)


def select_expired_event_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                    after: ExpiryCursor | None = None) -> EventPage:
    """Événements dont au moins une finalité est POTENTIELLEMENT échue
    (indicatif : chaque candidat est relu sous verrou)."""
    limit = _require_limit(limit)
    after = _require_cursor(after, ExpiryCursor)
    now = proofs.server_clock(db)  # horloge PostgreSQL ; jamais fournie par l'appelant
    found = db.execute(_expiry_query(now, limit, after)).all()
    next_cursor = None
    if len(found) == limit:
        last_id, last_position = found[-1]
        next_cursor = ExpiryCursor(last_position, last_id)  # photographie de la sélection
    return EventPage(EVENT_EXPIRY, tuple(row[0] for row in found), next_cursor)


def select_integrity_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                after: IntegrityCursor | None = None) -> EventPage:
    """TOUS les événements, sans filtre temporel ni dépendance aux colonnes de
    la sélection nominale ; ordre de la clé primaire."""
    limit = _require_limit(limit)
    after = _require_cursor(after, IntegrityCursor)
    event = ProductActivityEvent
    statement = select(event.id)
    if after is not None:
        statement = statement.where(event.id > after.event_id)
    found = db.execute(statement.order_by(event.id).limit(limit)).scalars().all()
    next_cursor = IntegrityCursor(found[-1]) if len(found) == limit else None
    return EventPage(EVENT_INTEGRITY, tuple(found), next_cursor)


# --------------------------------------------------------------------------
# Lectures, clés et verrous
# --------------------------------------------------------------------------

def _lock_mode(statement, mode):
    if mode is not None:
        statement = statement.with_for_update(read=(mode == "share"))
    return statement.execution_options(populate_existing=True)  # jamais un état ORM périmé


def _event(db, event_id, *, mode=None):
    return db.execute(_lock_mode(select(ProductActivityEvent).where(ProductActivityEvent.id == event_id), mode)
                      ).scalar_one_or_none()


def _attribution(db, event_id, *, mode=None):
    return db.execute(_lock_mode(select(ProductActivityEventAttribution).where(
        ProductActivityEventAttribution.event_id == event_id), mode)).scalar_one_or_none()


def _referencing_proofs(db, event_id) -> list:
    """Preuves qui référencent l'attribution de cet événement (FK de 0024)."""
    return list(db.execute(_lock_mode(select(ProductActivityProof).where(
        ProductActivityProof.recorded_event_id == event_id).order_by(ProductActivityProof.id), None)).scalars())


def _interaction(db, attribution, *, mode=None):
    """Interaction d'origine de l'attribution (FK composite de 0024)."""
    if attribution is None:
        return None
    interactions = ProductActivitySessionInteraction
    return db.execute(_lock_mode(select(interactions).where(
        interactions.campaign_id == attribution.campaign_id, interactions.user_id == attribution.user_id,
        interactions.interaction_key == attribution.interaction_key), mode)).scalar_one_or_none()


def _row(row) -> tuple | None:
    """Toutes les colonnes d'une ligne (photographie complète)."""
    if row is None:
        return None
    return tuple((column.key, getattr(row, column.key)) for column in row.__table__.columns)


def _proof_by_id(db, proof_id, *, mode):
    return db.execute(_lock_mode(select(ProductActivityProof).where(ProductActivityProof.id == proof_id), mode)
                      ).scalar_one_or_none()


def _identity(event) -> tuple:
    return (event.id, event.user_id, event.idempotency_key) + tuple(
        getattr(event, field) for field in EVENT_IDENTITY_FIELDS)


def _path(event) -> str:
    """Chemin de l'événement : CERTIFIÉ pour une nature canonique (bloc C),
    HISTORIQUE sinon (2A). Jamais déduit du seul event_type."""
    return CERTIFIED if event.source_operation_kind in events.CERTIFIED_OPERATIONS else HISTORICAL


def _event_lock_keys(event) -> tuple:
    """Clés consultatives de l'événement, conventions EXACTES des écrivains :
    certifié -> finalization.lock_keys (activity_proof, event_key de
    l'identifiant de preuve, opération canonique, opération historique
    équivalente) ; historique -> events.record_event (event_key, et selon le
    cas event_session_start, event_source_operation)."""
    if _path(event) == CERTIFIED:
        return tuple(finalization.lock_keys(_ProofIdentity(
            event.idempotency_key, event.user_id, event.event_type, event.visit_id, event.source_operation_kind,
            event.source_operation_id)))
    probe = {"event_type": event.event_type, "visit_id": event.visit_id,
             "source_operation_kind": event.source_operation_kind,
             "source_operation_id": event.source_operation_id}
    return tuple(events._identity_locks(event.user_id, probe, event.idempotency_key))


def _activity_proof_key(row) -> int:
    """Clé du bloc B (proofs.proof_lock_key) d'une preuve ou d'une attribution."""
    return common.identity_lock_key(proofs.PROOF_LOCK_KIND, row.user_id, row.source_operation_kind,
                                    row.source_operation_id)


def _required_keys(event, attribution, proof_rows) -> set:
    keys = set(_event_lock_keys(event))
    if attribution is not None:
        keys.add(_activity_proof_key(attribution))
    keys |= {_activity_proof_key(proof) for proof in proof_rows}
    return keys


def _scope_campaigns(event) -> set:
    try:
        return {scope.campaign_id for scope in retention.parse_retention_scopes(event.retention_scopes)}
    except _STRUCTURE_ERRORS:
        return set()  # document illisible : refusé à la validation


def _dependency_campaigns(event, attribution, proof_rows) -> set:
    campaigns = _scope_campaigns(event)
    if attribution is not None:
        campaigns.add(attribution.campaign_id)
    return campaigns | {proof.campaign_id for proof in proof_rows}


def _lock(db, event_id: uuid.UUID, *, write: bool):
    """Rangs 0 à 10. Rend _Locked, ABSENT, ou un motif d'abandon."""
    row_mode = "update" if write else "share"
    # 0. L0 indicative
    hint = _event(db, event_id)
    if hint is None:
        return ABSENT
    identity = _identity(hint)
    keys = _required_keys(hint, _attribution(db, event_id), _referencing_proofs(db, event_id))
    # 1-2. union de toutes les clés, UN appel trié
    common.lock_identities(db, *keys)
    # 3. L1 sous les clés
    event = _event(db, event_id)
    if event is None:
        return ABSENT
    if _identity(event) != identity:
        return IDENTITY_CHANGED
    attribution, proof_rows = _attribution(db, event_id), _referencing_proofs(db, event_id)
    if not _required_keys(event, attribution, proof_rows) <= keys or len(proof_rows) > 1:
        return DEPENDENCY_CHANGED  # clé nécessaire non détenue : jamais acquise tardivement
    campaigns = _dependency_campaigns(event, attribution, proof_rows)
    proof_id = proof_rows[0].id if proof_rows else None
    # 4-5. participations puis campagnes, en partage, ordre déterministe
    authorizations = {}
    if campaigns:
        participations = db.execute(_lock_mode(select(ProductCampaignParticipant).where(
            ProductCampaignParticipant.user_id == event.user_id,
            ProductCampaignParticipant.campaign_id.in_(sorted(campaigns)))
            .order_by(ProductCampaignParticipant.campaign_id), "share")).scalars().all()
        locked_campaigns = {c.id: c for c in db.execute(_lock_mode(select(ProductCampaign).where(
            ProductCampaign.id.in_(sorted(campaigns))).order_by(ProductCampaign.id), "share")).scalars().all()}
        authorizations = {p.campaign_id: (p, locked_campaigns.get(p.campaign_id)) for p in participations}
    # 6. interaction d'origine de l'attribution relue en L1, EN PARTAGE (aucun
    #    service ne la verrouille, la modifie ni la supprime : aucun contact) ;
    #    visite : aucun verrou.
    attribution_l1, proof_l1 = _row(attribution), _row(proof_rows[0]) if proof_rows else None
    interaction = _interaction(db, attribution, mode="share")
    # 7. preuve référençante ; 8. attribution ; 9. événement
    proof = _proof_by_id(db, proof_id, mode=row_mode) if proof_id is not None else None
    locked_attribution = _attribution(db, event_id, mode=row_mode) if attribution is not None else None
    event = _event(db, event_id, mode=row_mode)
    # 10. L2 : relecture finale, sous tous les verrous
    if event is None:
        return ABSENT
    if _identity(event) != identity:
        return IDENTITY_CHANGED
    if attribution is None:
        locked_attribution = _attribution(db, event_id)
        if locked_attribution is not None:
            return DEPENDENCY_CHANGED  # apparue trop tard pour son rang (8 < 9)
    elif _row(locked_attribution) != attribution_l1:
        return DEPENDENCY_CHANGED  # disparue ou modifiée : l'interaction verrouillée n'est plus la sienne
    final_proofs = _referencing_proofs(db, event_id)
    if any(row.id != proof_id for row in final_proofs):
        return DEPENDENCY_CHANGED  # nouvelle preuve : son rang (7) est passé
    if proof is not None and (not final_proofs or proof.recorded_event_id != event.id):
        proof = None  # disparue (ou détachée) entre les lectures : pas un conflit en soi
    elif proof is not None and _row(proof) != proof_l1:
        return DEPENDENCY_CHANGED
    final_proof_rows = [proof] if proof is not None else []
    if (not _required_keys(event, locked_attribution, final_proof_rows) <= keys
            or not _dependency_campaigns(event, locked_attribution, final_proof_rows) <= campaigns):
        return DEPENDENCY_CHANGED
    return _Locked(event, locked_attribution, proof, interaction, tuple(sorted(keys)), authorizations)


# --------------------------------------------------------------------------
# Contrat de l'état stocké (lecture seule)
# --------------------------------------------------------------------------

def _scopes(event) -> tuple:
    return retention.validate_retention_columns(event.retention_scopes,
                                                next_scope_expires_at=event.next_scope_expires_at,
                                                purge_after_at=event.purge_after_at)


def _canonical_problem(event, interaction, authorization) -> str | None:
    """Invariants canoniques du bloc C, indépendants de la conservation (une
    échéance légitimement raccourcie reste admise) : origine = interaction
    'activity' de la visite, occurred_at = son instant d'acceptation ; champs
    de requête EXACTEMENT ceux de events._certified_request (nature, source,
    outcome, route, livraison, delivered_at, resolved_at) ; recorded_at =
    updated_at (instant de finalisation, jamais réécrit) ; chronologie
    occurred_at <= resolved_at <= recorded_at ; bornes du bloc C sur la
    participation et la campagne de l'attribution : origine acceptée pendant
    l'observation, certification (resolved_at) strictement avant window_end_at
    et close_by_at, finalisation (recorded_at) strictement avant
    finalization.finalization_deadline. Vérifiable aussi après la purge
    nominale de la preuve."""
    if (interaction is None or interaction.interaction_kind != proofs.ACTIVITY
            or interaction.session_id != event.visit_id or interaction.recorded_at != event.occurred_at):
        return ORIGIN_INCOHERENT
    try:
        expected = events._certified_request(
            user_id=event.user_id, visit_id=event.visit_id, idempotency_key=event.idempotency_key,
            source_operation_kind=event.source_operation_kind, source_operation_id=event.source_operation_id,
            occurred_at=interaction.recorded_at, resolved_at=event.resolved_at)
    except _STRUCTURE_ERRORS:
        return CERTIFIED_INCOHERENT
    if any(getattr(event, field) != expected[field] for field in events.REQUEST_FIELDS):
        return CERTIFIED_INCOHERENT
    if event.updated_at != event.recorded_at or not event.occurred_at <= event.resolved_at <= event.recorded_at:
        return CERTIFIED_INCOHERENT
    participation, campaign = authorization if authorization is not None else (None, None)
    if participation is None or campaign is None:
        return ATTACHMENT_INCOHERENT
    if not finalization._accepted_during_observation(event.occurred_at, participation, campaign):
        return ORIGIN_INCOHERENT
    if (event.resolved_at >= participation.window_end_at or event.resolved_at >= campaign.close_by_at
            or event.recorded_at >= finalization.finalization_deadline(participation, campaign)):
        return CERTIFIED_INCOHERENT
    return None


def _certified_problem(event, attribution, proof, scopes, interaction, authorizations) -> str | None:
    rule = events.CERTIFIED_OPERATIONS[event.source_operation_kind]
    if ((event.event_type, event.surface) != rule[:2] or event.source != SERVER_BUSINESS
            or event.outcome != events.CERTIFIED_OUTCOME or event.executed_route != events.CERTIFIED_ROUTE):
        return CLASSIFICATION_INCOHERENT
    if attribution is None or len(scopes) != 1:
        return CLASSIFICATION_INCOHERENT  # attribution obligatoire, UNE finalité
    if (tuple(getattr(attribution, field) for field in EVENT_IDENTITY_FIELDS)
            != tuple(getattr(event, field) for field in EVENT_IDENTITY_FIELDS)
            or attribution.event_id != event.id or scopes[0].campaign_id != attribution.campaign_id
            or attribution.certified_at != event.resolved_at):
        return ATTRIBUTION_INCOHERENT
    canonical = _canonical_problem(event, interaction, authorizations.get(attribution.campaign_id))
    if canonical is not None:
        return canonical
    if proof is not None and (
            proof.status != proofs.RECORDED or proof.id != event.idempotency_key
            or proof.recorded_event_id != event.id
            or tuple(getattr(proof, field) for field in IDENTITY_FIELDS)
            != tuple(getattr(attribution, field) for field in IDENTITY_FIELDS)
            or proof.certified_at != attribution.certified_at or proof.resolved_at != event.recorded_at):
        return PROOF_INCOHERENT  # jamais supprimée par ce mécanisme
    return None


def _contract_problem(db, locked: _Locked) -> str | None:
    """Motif qui place l'état stocké HORS CONTRAT, ou None. Lecture seule ;
    aucune décision temporelle (un événement échu n'est pas incohérent)."""
    event, attribution, proof = locked.event, locked.attribution, locked.proof
    if event.schema_version != PRODUCT_EVENT_SCHEMA_VERSION:
        return SCHEMA_UNSUPPORTED
    try:
        scopes = _scopes(event)
    except _STRUCTURE_ERRORS:
        return RETENTION_INCOHERENT
    try:
        retention.verify_scopes_against_campaigns(db, scopes, user_id=event.user_id, occurred_at=event.occurred_at)
    except _STRUCTURE_ERRORS:
        return ATTACHMENT_INCOHERENT
    if _path(event) == CERTIFIED:
        return _certified_problem(event, attribution, proof, scopes, locked.interaction, locked.authorizations)
    if attribution is not None or proof is not None:
        return CLASSIFICATION_INCOHERENT  # un événement historique n'a jamais d'attribution
    if event.source_operation_kind is not None and event.source_operation_kind not in SOURCE_OPERATION_KINDS:
        return CLASSIFICATION_INCOHERENT
    return None


def _clock_problem(locked: _Locked, now: datetime) -> bool:
    """Horloge antérieure à une date stockée de l'événement ou de ses
    dépendances : aucune décision (jamais d'antidatage)."""
    instants = [locked.event.updated_at, locked.event.recorded_at]
    if locked.attribution is not None:
        instants.append(locked.attribution.certified_at)
    if locked.proof is not None:
        instants.append(locked.proof.resolved_at)
    return any(now < instant for instant in instants)


def _state(event, attribution, proof, interaction) -> _EventState:
    return _EventState(_row(event), _row(attribution), _row(proof), _row(interaction))


def _revised(state: _EventState, **values) -> _EventState:
    """Photographie attendue après le retrait : seules les colonnes écrites
    changent, toutes les autres sont reprises de l'état initial."""
    event = tuple((key, values.get(key, value)) for key, value in state.event)
    return replace(state, event=event)


# --------------------------------------------------------------------------
# Écritures conditionnelles (UN SAVEPOINT chacune)
# --------------------------------------------------------------------------

class _Unconfirmed(Exception):
    """Levée DANS le SAVEPOINT : une écriture n'a pas touché exactement une ligne."""


def _exactly_one(db, statement) -> None:
    if db.execute(statement.execution_options(synchronize_session=False)).rowcount != 1:
        raise _Unconfirmed()


def _same_event(event):
    """Prédicat conditionnel : l'événement tel que relu sous verrou."""
    table = ProductActivityEvent
    return (table.id == event.id, table.user_id == event.user_id, table.idempotency_key == event.idempotency_key,
            table.retention_scopes == event.retention_scopes,
            table.next_scope_expires_at == event.next_scope_expires_at,
            table.purge_after_at == event.purge_after_at, table.updated_at == event.updated_at)


def _prune(db, locked: _Locked, remaining: tuple, now: datetime) -> EventDecision:
    """Retrait des finalités expirées d'un événement HISTORIQUE."""
    event = locked.event
    try:
        document = retention.build_retention_document(remaining)
        retention.validate_scope_revision(event.retention_scopes, document)  # ni ajout ni prolongation
        next_expiry, purge_after = retention.retention_bounds(remaining)
    except _STRUCTURE_ERRORS:
        return EventDecision(CONFLICT, RETENTION_INCOHERENT)
    if purge_after > event.purge_after_at or next_expiry <= event.next_scope_expires_at or next_expiry <= now:
        return EventDecision(CONFLICT, RETENTION_INCOHERENT)  # défense : jamais d'allongement
    before = _state(event, None, None, None)
    statement = (update(ProductActivityEvent)
                 .where(*_same_event(event), ProductActivityEvent.next_scope_expires_at <= now)
                 .values(retention_scopes=document, next_scope_expires_at=next_expiry, purge_after_at=purge_after,
                         updated_at=now))
    try:
        with db.begin_nested():
            _exactly_one(db, statement)
    except _Unconfirmed:
        return EventDecision(CONFLICT, WRITE_UNCONFIRMED)
    db.expunge(event)
    after = _revised(before, retention_scopes=document, next_scope_expires_at=next_expiry, purge_after_at=purge_after,
                     updated_at=now)
    return EventDecision(PRUNED, None, True, _Attempt(event.id, locked.keys, before, after))


def _purge(db, locked: _Locked, now: datetime) -> EventDecision:
    """Suppression atomique : preuve référençante, attribution, événement
    (ordre des FK de 0024), dans UN SAVEPOINT."""
    event, attribution, proof = locked.event, locked.attribution, locked.proof
    before = _state(event, attribution, proof, locked.interaction)
    try:
        with db.begin_nested():
            if proof is not None:
                registry = ProductActivityProof
                _exactly_one(db, delete(registry).where(
                    registry.id == proof.id, registry.status == proofs.RECORDED,
                    registry.recorded_event_id == event.id, registry.resolved_at == proof.resolved_at,
                    registry.purge_after_at == proof.purge_after_at, registry.certified_at == proof.certified_at,
                    *(getattr(registry, field) == getattr(attribution, field) for field in IDENTITY_FIELDS)))
            if attribution is not None:
                attributions = ProductActivityEventAttribution
                _exactly_one(db, delete(attributions).where(
                    attributions.event_id == event.id, attributions.certified_at == attribution.certified_at,
                    *(getattr(attributions, field) == getattr(attribution, field) for field in IDENTITY_FIELDS)))
            _exactly_one(db, delete(ProductActivityEvent).where(
                *_same_event(event), ProductActivityEvent.purge_after_at <= now))
    except _Unconfirmed:
        return EventDecision(CONFLICT, WRITE_UNCONFIRMED)
    except IntegrityError:
        return EventDecision(CONFLICT, DEPENDENCY_CHANGED)  # référence apparue hors service : rien n'est supprimé
    for row in (proof, attribution, event, locked.interaction):
        if row is not None:
            db.expunge(row)
    return EventDecision(PURGED, None, True, _Attempt(event.id, locked.keys, before, None))


# --------------------------------------------------------------------------
# Points d'entrée (la transaction appartient à l'appelant)
# --------------------------------------------------------------------------

def expire_event_scopes(db, *, event_id: uuid.UUID) -> EventDecision:
    """Applique les échéances stockées d'UN événement (voir l'en-tête)."""
    _require_event_id(event_id)
    locked = _lock(db, event_id, write=True)
    if locked == ABSENT:
        return EventDecision(ABSENT)
    if isinstance(locked, str):
        return EventDecision(CONFLICT, locked)
    problem = _contract_problem(db, locked)
    if problem is not None:
        return EventDecision(CONFLICT, problem)
    now = proofs.server_clock(db)  # APRÈS tous les verrous : instant de décision
    if _clock_problem(locked, now):
        return EventDecision(UNKNOWN, CLOCK_INCOHERENT)
    scopes = _scopes(locked.event)
    remaining = tuple(scope for scope in scopes if scope.expires_at > now)  # expirée : expires_at <= now
    if len(remaining) == len(scopes):
        return EventDecision(NOT_DUE)
    if not remaining:
        return _purge(db, locked, now)
    if _path(locked.event) == CERTIFIED:
        return EventDecision(CONFLICT, CLASSIFICATION_INCOHERENT)  # jamais de retrait partiel (une finalité)
    return _prune(db, locked, remaining, now)


def _snapshot(db, event_id, *, mode=None):
    """(événement, attribution, preuves référençantes, interaction), lus dans
    l'ordre des rangs 6 à 9 lorsqu'ils sont verrouillés."""
    attribution = _attribution(db, event_id)
    interaction = _interaction(db, attribution, mode=mode)
    proof_rows = _referencing_proofs(db, event_id)
    if mode is not None:
        proof_rows = [p for p in (_proof_by_id(db, row.id, mode=mode) for row in proof_rows) if p is not None]
        attribution = _attribution(db, event_id, mode=mode) if attribution is not None else None
    return _event(db, event_id, mode=mode), attribution, proof_rows, interaction


def _observe(db, event_id: uuid.UUID, keys) -> EventObservation:
    """Relecture STABLE sans écriture : clés fournies d'abord (une transaction
    encore en cours sur ces identités est attendue), lecture L1, verrous de
    lignes EN PARTAGE dans l'ordre global (interaction, preuve, attribution,
    événement : sous-ensemble ordonné des rangs 6 à 9, aucune clé
    supplémentaire), relecture L2 ; L1 et L2 doivent être identiques et ne
    réclamer aucune clé non détenue, sinon UNSTABLE."""
    keys = tuple(keys)
    common.lock_identities(db, *keys)
    first_event, attribution, proof_rows, interaction = _snapshot(db, event_id)
    if first_event is None:
        return EventObservation(ABSENT)
    if len(proof_rows) > 1 or not _required_keys(first_event, attribution, proof_rows) <= set(keys):
        return EventObservation(UNSTABLE)
    # Photographie capturée en valeurs AVANT la relecture (l'ORM rafraîchit les mêmes objets en place).
    first = _state(first_event, attribution, proof_rows[0] if proof_rows else None, interaction)
    locked_event, attribution, proof_rows, interaction = _snapshot(db, event_id, mode="share")
    if locked_event is None or len(proof_rows) > 1:
        return EventObservation(UNSTABLE)
    second = _state(locked_event, attribution, proof_rows[0] if proof_rows else None, interaction)
    if second != first or [row.id for row in _referencing_proofs(db, event_id)] != [row.id for row in proof_rows]:
        return EventObservation(UNSTABLE)  # changé pendant l'observation : aucune conclusion
    return EventObservation(PRESENT, second)


def observe_event_retention(db, *, event_id: uuid.UUID) -> EventObservation:
    """État de l'événement et de ses dépendances sous les clés de sa lecture
    indicative, SANS écriture."""
    _require_event_id(event_id)
    hint = _event(db, event_id)
    if hint is None:
        return EventObservation(ABSENT)
    keys = _required_keys(hint, _attribution(db, event_id), _referencing_proofs(db, event_id))
    observation = _observe(db, event_id, sorted(keys))
    return EventObservation(ABSENT) if observation.outcome == ABSENT else observation


def _observe_attempt(db, *, attempt: _Attempt) -> EventObservation:
    """Relecture d'un COMMIT incertain avec les clés de l'opération TENTÉE
    (contexte en mémoire) : la disparition apparente de la ligne ne contourne
    jamais la sérialisation."""
    if type(attempt) is not _Attempt:
        raise InvalidProductAnalyticsInput("attempt : contexte d'écriture requis")
    return _observe(db, attempt.event_id, attempt.keys)


def inspect_event_integrity(db, *, event_id: uuid.UUID) -> IntegrityDecision:
    """Contrôle indépendant d'UN événement, sur un état cohérent (mêmes clés,
    lignes en partage), sans écriture ni réparation."""
    _require_event_id(event_id)
    locked = _lock(db, event_id, write=False)
    if locked == ABSENT:
        return IntegrityDecision(ABSENT)
    if isinstance(locked, str):
        return IntegrityDecision(UNKNOWN, locked)  # état instable : aucune conclusion
    problem = _contract_problem(db, locked)
    if problem is not None:
        return IntegrityDecision(INCONSISTENT, problem)
    return IntegrityDecision(VALID)
