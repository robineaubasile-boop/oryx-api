"""Cycle de vie, résolution et purge du registre provisoire des preuves
(V0-T08 Lot 2C, PR 1) — DORMANT.

Table product_activity_proof_registry (migration 0024, inchangée). Ce
module ne certifie rien, ne finalise rien et ne calcule aucune échéance
nouvelle : il SÉLECTIONNE (lecture seule, bornée) les preuves résiduelles
et SUPPRIME les preuves terminales arrivées au terme de leur conservation.
La récupération des preuves certified et la résolution des preuves pending
expirées restent celles du bloc C (finalisation et résolution explicite de
core/product_analytics/finalization.py), appelées telles quelles par la
frontière privée hors du paquet (core/_activity_proof_lifecycle.py), elle-même
sans appelant. La transaction appartient toujours à l'appelant : flush
seulement, jamais commit() ni rollback() ; l'écriture a lieu dans un
SAVEPOINT.

1. Sélection (aucune écriture, aucun verrou, jamais une autorisation) :

   select_certified_candidates        preuves certified (toutes : soit
                                      finalisables, soit à résoudre par le
                                      bloc C) ; ordre (created_at, id) ;
   select_expired_pending_candidates  preuves pending dont l'échéance du
                                      bloc C est POTENTIELLEMENT atteinte :
                                      min(window_end_at + grâce, close_by_at)
                                      <= horloge PostgreSQL ; ordre
                                      (created_at, id) ;
   select_purgeable_candidates        preuves terminales dont purge_after_at
                                      <= horloge PostgreSQL ; ordre
                                      (purge_after_at, id).

   Taille de page strictement validée (entier exact, 1 <= limit <=
   MAX_PAGE_SIZE, défaut DEFAULT_PAGE_SIZE) ; pagination par curseur de
   position (ProofCursor : position et identifiant du DERNIER candidat rendu,
   strictement dépassés par la page suivante), jamais par OFFSET : un candidat
   en échec n'est jamais rendu deux fois dans une même passe et ne masque
   jamais les suivants. Candidats minimisés : identifiants internes de preuve
   seulement. Toute décision est REPRISE sous verrou au traitement.

2. Purge (purge_resolved_proof) : une preuve n'est supprimée que si, sous
   verrou et à l'horloge PostgreSQL lue APRÈS les verrous :

       status IN (recorded, rejected, indeterminate)
       resolved_at et purge_after_at renseignés
       purge_after_at = resolved_at + 168 heures ABSOLUES (règle de 0024)
       horloge >= resolved_at (sinon horloge incohérente : rien)
       purge_after_at <= horloge (égalité admise ; avant, même d'une
                                  microseconde : rien)

   Le DELETE reprend ces conditions en SQL (défense finale). Aucune date
   n'est modifiée, aucune durée n'est recalculée ; pending et certified ne
   sont jamais supprimées (elles suivent d'abord leur contrat de résolution).
   Aucune autre table n'est lue ni écrite : la preuve recorded référence
   l'attribution définitive (FK de 0024), jamais l'inverse ; événement et
   attribution restent intacts (ni suppression en cascade, ni désactivation
   de FK, ni tombstone).

3. observe_purge : même verrou, AUCUNE écriture (présence de la preuve après
   un COMMIT incertain).

Ordre des verrous de la purge (sous-ensemble ordonné des blocs B et C ;
docs/v0_t08_2c_pr1.md § 6) :

    0. lecture SANS verrou de la preuve (indicative : identité canonique) ;
    1. pg_advisory_xact_lock de l'identité canonique de la preuve
       ('activity_proof', propriétaire, nature, identifiant : la clé du bloc B,
       comprise dans l'ensemble trié du bloc C) — UNE clé ;
    2. preuve FOR UPDATE, rechargée ; identité comparée à la lecture 0 ;
    3. horloge : clock_timestamp() PostgreSQL (proofs.server_clock), APRÈS
       ces verrous : instant de décision ;
    4. DELETE de cette seule ligne (aucune table ne référence le registre :
       aucune attente après l'horloge).
Aucun verrou de participation, de campagne, de visite, d'utilisateur ni F2.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Float, and_, delete, func, literal, select, tuple_

from core.models import ProductActivityProof, ProductCampaign, ProductCampaignParticipant
from core.product_analytics import common, finalization, proofs
from core.product_analytics.common import InvalidProductAnalyticsInput

# --------------------------------------------------------------------------
# Vocabulaires fermés et bornes opérationnelles
# --------------------------------------------------------------------------

PENDING, CERTIFIED = proofs.PENDING, proofs.CERTIFIED
OPERATION_CONFLICT = proofs.OPERATION_CONFLICT  # course perdue du bloc C (aucune écriture)
RESOLVED_STATUSES = proofs.RESOLVED_STATUSES  # recorded, rejected, indeterminate
REGISTRY_RETENTION = proofs.REGISTRY_RETENTION  # 168 heures absolues (0024)
FINALIZATION_GRACE = finalization.FINALIZATION_GRACE  # échéance du bloc C, jamais une autre
IDENTITY_FIELDS = finalization.IDENTITY_FIELDS

# Limites opérationnelles proposées (pas des décisions produit).
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

CERTIFIED_RECOVERY = "certified_recovery"
PENDING_EXPIRY = "pending_expiry"
RESOLVED_PURGE = "resolved_purge"
SELECTIONS = (CERTIFIED_RECOVERY, PENDING_EXPIRY, RESOLVED_PURGE)

# Issues d'une purge (service) et de son observation.
PURGED = "purged"
ABSENT = proofs.ABSENT
NOT_PURGEABLE = "not_purgeable"
CONFLICT = "conflict"
PRESENT = "present"  # observation seulement
PURGE_OUTCOMES = (PURGED, ABSENT, NOT_PURGEABLE, CONFLICT, PRESENT)

NOT_RESOLVED = "not_resolved"  # pending / certified : jamais supprimables directement
NOT_DUE = "not_due"  # purge_after_at pas encore atteint
RETENTION_INCOHERENT = "retention_incoherent"  # échéance absente ou différente de resolved_at + 168 h
CLOCK_INCOHERENT = "clock_incoherent"  # horloge antérieure à la résolution
IDENTITY_CHANGED = "identity_changed"  # identité relue différente de la lecture indicative
DELETE_UNCONFIRMED = "delete_unconfirmed"  # le DELETE conditionnel n'a pas supprimé exactement une ligne
PURGE_REASONS = (NOT_RESOLVED, NOT_DUE, RETENTION_INCOHERENT, CLOCK_INCOHERENT, IDENTITY_CHANGED,
                 DELETE_UNCONFIRMED)


@dataclass(frozen=True, slots=True, repr=False)
class ProofCursor:
    """Position du DERNIER candidat rendu d'une sélection : la page suivante
    commence strictement après (position, proof_id). Interne ; jamais
    journalisé (repr masqué)."""
    selection: str
    position: datetime
    proof_id: uuid.UUID

    def __repr__(self) -> str:
        return f"ProofCursor({self.selection}, <private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CandidatePage:
    """Page de candidats : identifiants internes de preuve seulement, dans
    l'ordre de la sélection ; next_cursor None quand la sélection est épuisée
    (page incomplète)."""
    selection: str
    proof_ids: tuple
    next_cursor: ProofCursor | None

    def __repr__(self) -> str:
        return f"CandidatePage({self.selection}, {len(self.proof_ids)}, more={self.next_cursor is not None})"


@dataclass(frozen=True, slots=True)
class PurgeDecision:
    """Issue d'une purge ou d'une observation. outcome : PURGE_OUTCOMES ;
    reason : PURGE_REASONS ou None ; written : cet appel a supprimé la ligne
    (flush, NON durable avant le COMMIT de l'appelant) ; state : statut de la
    preuve lu sous verrou (None si absente ou supprimée)."""
    outcome: str
    reason: str | None = None
    written: bool = False
    state: str | None = None


# --------------------------------------------------------------------------
# Sélection bornée (lecture seule)
# --------------------------------------------------------------------------

def _require_limit(limit) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidProductAnalyticsInput(f"limit : entier exact de 1 à {MAX_PAGE_SIZE} requis")
    return limit


def _require_cursor(after, selection: str) -> ProofCursor | None:
    if after is None:
        return None
    if type(after) is not ProofCursor or after.selection != selection:
        raise InvalidProductAnalyticsInput("after : ProofCursor de cette sélection requis")
    common.require_aware(after.position, "position")
    common.require_uuid(after.proof_id, "proof_id")
    return after


def absolute_interval(delta):
    """Intervalle SQL en SECONDES absolues. Jamais un timedelta lié tel quel :
    psycopg2 le transmet en jours ('7 days'), que PostgreSQL ajoute en jours
    CIVILS du fuseau de session (167 ou 169 heures aux changements d'heure)."""
    return func.make_interval(0, 0, 0, 0, 0, 0, literal(delta.total_seconds(), Float()))


def _certified_query(now: datetime, limit: int, after: ProofCursor | None):
    del now  # toutes les preuves certified sont candidates (finalisables ou à résoudre)
    proof = ProductActivityProof
    statement = select(proof.id, proof.created_at).where(proof.status == CERTIFIED)
    if after is not None:
        statement = statement.where(tuple_(proof.created_at, proof.id) > tuple_(after.position, after.proof_id))
    return statement.order_by(proof.created_at, proof.id).limit(limit)


def _pending_query(now: datetime, limit: int, after: ProofCursor | None):
    """Même borne que finalization.finalization_deadline, en SQL (indicative ;
    la résolution du bloc C la reprend sous verrou)."""
    proof, participation, campaign = ProductActivityProof, ProductCampaignParticipant, ProductCampaign
    deadline = func.least(participation.window_end_at + absolute_interval(FINALIZATION_GRACE), campaign.close_by_at)
    statement = (select(proof.id, proof.created_at)
                 .join(participation, and_(participation.campaign_id == proof.campaign_id,
                                           participation.user_id == proof.user_id))
                 .join(campaign, campaign.id == proof.campaign_id)
                 .where(proof.status == PENDING, deadline <= now))
    if after is not None:
        statement = statement.where(tuple_(proof.created_at, proof.id) > tuple_(after.position, after.proof_id))
    return statement.order_by(proof.created_at, proof.id).limit(limit)


def _purge_query(now: datetime, limit: int, after: ProofCursor | None):
    """Index partiel ix_product_activity_proof_registry_purge_after_at
    (purge_after_at IS NOT NULL) : parcours borné dans l'ordre de l'index."""
    proof = ProductActivityProof
    statement = select(proof.id, proof.purge_after_at).where(proof.purge_after_at.is_not(None),
                                                             proof.purge_after_at <= now,
                                                             proof.status.in_(RESOLVED_STATUSES))
    if after is not None:
        statement = statement.where(tuple_(proof.purge_after_at, proof.id) > tuple_(after.position, after.proof_id))
    return statement.order_by(proof.purge_after_at, proof.id).limit(limit)


_QUERIES = {CERTIFIED_RECOVERY: _certified_query, PENDING_EXPIRY: _pending_query, RESOLVED_PURGE: _purge_query}


def _select(db, selection: str, limit, after) -> CandidatePage:
    limit = _require_limit(limit)
    after = _require_cursor(after, selection)
    now = proofs.server_clock(db)  # horloge PostgreSQL ; jamais fournie par l'appelant
    found = db.execute(_QUERIES[selection](now, limit, after)).all()
    proof_ids = tuple(row[0] for row in found)
    next_cursor = None
    if len(found) == limit:
        last_id, last_position = found[-1]
        next_cursor = ProofCursor(selection, last_position, last_id)
    return CandidatePage(selection, proof_ids, next_cursor)


def select_certified_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                after: ProofCursor | None = None) -> CandidatePage:
    """Preuves certified, à reprendre par la finalisation du bloc C."""
    return _select(db, CERTIFIED_RECOVERY, limit, after)


def select_expired_pending_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                      after: ProofCursor | None = None) -> CandidatePage:
    """Preuves pending dont l'échéance du bloc C est potentiellement atteinte,
    à reprendre par la résolution explicite du bloc C."""
    return _select(db, PENDING_EXPIRY, limit, after)


def select_purgeable_candidates(db, *, limit: int = DEFAULT_PAGE_SIZE,
                                after: ProofCursor | None = None) -> CandidatePage:
    """Preuves terminales dont purge_after_at est potentiellement atteint, à
    reprendre par purge_resolved_proof."""
    return _select(db, RESOLVED_PURGE, limit, after)


# --------------------------------------------------------------------------
# Purge d'une preuve terminale échue
# --------------------------------------------------------------------------

def _identity(proof) -> tuple:
    return (proof.id,) + tuple(getattr(proof, field) for field in IDENTITY_FIELDS)


def proof_lock_key(proof) -> int:
    """Clé consultative canonique de la preuve : celle du bloc B
    (proofs.proof_lock_key) et la première de finalization.lock_keys."""
    return common.identity_lock_key(proofs.PROOF_LOCK_KIND, proof.user_id, proof.source_operation_kind,
                                    proof.source_operation_id)


def _by_id(db, proof_id, *, for_update: bool):
    statement = select(ProductActivityProof).where(ProductActivityProof.id == proof_id)
    if for_update:
        statement = statement.with_for_update()
    return db.execute(statement.execution_options(populate_existing=True)).scalar_one_or_none()


def _lock(db, proof_id) -> tuple:
    """Rangs 0 à 2 : (preuve verrouillée ou None, identité changée ?)."""
    hint = _by_id(db, proof_id, for_update=False)
    if hint is None:
        return None, False
    identity = _identity(hint)  # indicative : relue après le verrou
    common.lock_identities(db, proof_lock_key(hint))
    proof = _by_id(db, proof_id, for_update=True)
    if proof is None:
        return None, False
    return proof, _identity(proof) != identity


def purge_problem(proof, now: datetime) -> str | None:
    """Motif qui interdit la suppression de `proof` à l'instant `now` (lu
    après les verrous), ou None. Aucune date n'est modifiée ni recalculée."""
    if proof.status not in RESOLVED_STATUSES:
        return NOT_RESOLVED
    if proof.resolved_at is None or proof.purge_after_at is None:
        return RETENTION_INCOHERENT
    if proof.purge_after_at != proof.resolved_at + REGISTRY_RETENTION:
        return RETENTION_INCOHERENT  # durée absolue de 0024 seulement, jamais une autre
    if now < proof.resolved_at:
        return CLOCK_INCOHERENT  # horloge régressive : aucune suppression
    if now < proof.purge_after_at:
        return NOT_DUE
    return None


class _Unconfirmed(Exception):
    """Levée DANS le SAVEPOINT : le DELETE n'a pas supprimé exactement une ligne."""


def purge_resolved_proof(db, *, proof_id: uuid.UUID) -> PurgeDecision:
    """Suppression physique d'UNE preuve terminale dont la conservation est
    échue (voir l'en-tête). Flush dans un SAVEPOINT, jamais de COMMIT."""
    common.require_uuid(proof_id, "proof_id")
    proof, changed = _lock(db, proof_id)
    if proof is None:
        return PurgeDecision(ABSENT)
    if changed:
        return PurgeDecision(CONFLICT, IDENTITY_CHANGED, False, proof.status)
    now = proofs.server_clock(db)  # APRÈS les verrous : instant de décision
    problem = purge_problem(proof, now)
    if problem is not None:
        return PurgeDecision(NOT_PURGEABLE, problem, False, proof.status)
    table = ProductActivityProof
    statement = (delete(table)
                 .where(table.id == proof.id, table.status.in_(RESOLVED_STATUSES),
                        table.resolved_at.is_not(None),
                        table.purge_after_at == table.resolved_at + absolute_interval(REGISTRY_RETENTION),
                        table.purge_after_at <= now)
                 .execution_options(synchronize_session=False))
    try:
        with db.begin_nested():
            if db.execute(statement).rowcount != 1:
                raise _Unconfirmed()
    except _Unconfirmed:
        return PurgeDecision(CONFLICT, DELETE_UNCONFIRMED, False, proof.status)
    db.expunge(proof)
    return PurgeDecision(PURGED, None, True, None)


def observe_purge(db, *, proof_id: uuid.UUID) -> PurgeDecision:
    """Présence de la preuve, sous le même verrou consultatif, SANS écriture
    (issue d'un COMMIT incertain : une transaction encore active sur le
    serveur est attendue)."""
    common.require_uuid(proof_id, "proof_id")
    hint = _by_id(db, proof_id, for_update=False)
    if hint is None:
        return PurgeDecision(ABSENT)
    common.lock_identities(db, proof_lock_key(hint))
    proof = _by_id(db, proof_id, for_update=False)
    if proof is None:
        return PurgeDecision(ABSENT)
    return PurgeDecision(PRESENT, None, False, proof.status)
