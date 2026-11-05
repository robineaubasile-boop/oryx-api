"""Pont ORM PRIVÉ de la frontière d'autorisation pour Décrypter (R1 PR 9-A,
V2.2-02 / V2.2-03) — DORMANT.

Seul module de core/authz_f2 autorisé à construire une Session SQLAlchemy
(exception nominative de tests/test_authz_f2_boundary.py). Il fait exécuter
les services métier Décrypter historiques (core.assistant_delivery,
core.decryptage_cognitive_runtime, core.decryptage_progress, et par eux
core.cognitive_capture et core.interaction_identity) DANS la transaction
déjà ouverte et autorisée par AuthorizedTransactionRunner, sans jamais la
posséder :

    Session(bind=<connexion du runner>, autoflush=False,
            join_transaction_mode="rollback_only")

- aucune autre connexion, aucun moteur, jamais SessionLocal ni get_db
  (core.models importe core.db : cet import indirect ne donne aucun droit
  d'usage) ; aucun réseau, aucun fournisseur ;
- rollback_only : un Session.commit() ne serait pas propagé à la
  transaction du runner ; un Session.rollback() la terminerait. Mesuré
  (SQLAlchemy 2.1.3, psycopg2 2.9.13) : la transaction externe annulée par
  la Session, le bloc engine.begin() du runner se termine ENSUITE SANS
  erreur et sans COMMIT. Deux défenses : la Session du pont refuse
  commit(), rollback(), begin() non imbriqué, close(), reset(),
  invalidate() et connection() (TransactionOwnershipViolation), et aucun
  résultat n'est rendu si la transaction externe n'est plus exactement la
  même transaction active (TransactionLost : 503, alerte) ; une exception
  de flush avalée par un service est ainsi détectée. Mesuré aussi : une
  erreur d'INSTRUCTION avalée laisse la transaction PostgreSQL avortée
  alors que SQLAlchemy la croit active, et son COMMIT devient un ROLLBACK
  silencieux ; une sonde (instruction fermée de l'isolation, qui échoue
  sur une transaction avortée) précède donc tout résultat ;
- autoflush=False (identique à core.db.SessionLocal) : les services
  flushent explicitement ; le pont fait un flush() final explicite avant
  de construire la vue rendue ;
- SAVEPOINT : les begin_nested() des services (claim_delivery,
  register_or_resolve_conversation, open_event_idempotent) restent
  inchangés ; begin_nested() flushe d'abord les écritures en attente,
  hors du SAVEPOINT. Seule la violation UNIQUE prévue par chaque service
  est traduite localement ; toute autre erreur est propagée ;
- cycle de vie : connexion autorisée -> Session -> opération fermée ->
  flush final -> vue immuable -> fermeture de la Session -> retour au
  runner -> COMMIT externe par le runner -> succès. Jamais d'instance ORM
  vivante rendue. En échec : Session nettoyée, exception d'origine
  propagée (une erreur de nettoyage ne la masque jamais), ROLLBACK par le
  runner.

Inventaire FERMÉ des opérations (types exacts -> point d'entrée privé) :

| Opération | Services appelés (dans cet ordre) |
|---|---|
| TurnFinalization | claim_delivery ; si créée : capture_user_turn, answered_step (contribution), apply_construction_these_progress (construction_these + marqueur), bind_locked_analysis_session, record_turn_progress |
| DeliveryAcknowledgement | lock_owned_delivery ; [attestation C2 : identité de la livraison verrouillée, runtime V2, lien V2 verrouillé, couple statut / lien] ; provenance COMPLÈTE relue SANS verrou (5 events, SupportTrace et son event, AnalysisSessions) ; capture_delivered_response ; acknowledge_locked_delivery ; [attestation C2 : capture V2 exigée] |

Le propriétaire est TOUJOURS AuthorizedContext.principal.user_id : une
opération ne porte aucun user_id, aucune Session, connexion, fonction ni
SQL. Une livraison absente, étrangère ou d'une autre conversation, ou une
conversation d'un autre utilisateur : ABSENT (NOT_FOUND, rien n'est
écrit). Toute autre erreur métier ou PostgreSQL est propagée : le runner
annule tout (UNAVAILABLE), jamais converti en succès.

PR 9-A : aucune commande du registre de production n'atteignait ce module
(banc d'essai isolé des tests). R1 PR 9-C1-B : la commande dormante
FinalizeDecryptageCandidate l'atteint par les adaptateurs SQL (UNIQUE
importateur) et la troisième opération, CandidateFinalization. api.py ne
l'importe pas (F2 dormant).

R1 PR 9-C2 : la commande dormante AcknowledgeDecryptageDelivery atteint
l'opération DeliveryAcknowledgement INCHANGÉE (inventaire toujours de trois
opérations) avec une capacité AckAdmissionPort construite par les adaptateurs
APRÈS la preuve terminale C1-B. Le chemin est choisi par le type EXACT de la
capacité (CAPABILITIES : sonde historique OU attestation, tuple fermé) :
une attestation n'est jamais traitée comme une sonde, une sonde jamais comme
une attestation. Avec l'attestation : livraison introuvable sous verrou ->
AckProvenanceIncoherent (503, jamais ABSENT) ; identité de la livraison
verrouillée comparée à l'attestation (une fois), runtime V2, lien V2
verrouillé (ordre livraison -> lien, celui de la capture) et couple
pending / awaiting_delivery ou delivered / captured, AVANT la provenance
transitive et la capture ; capture V2 exigée après l'ACK. Le chemin
historique (sonde) garde ses comportements V1 / V2 / legacy.

R1 PR 9-C1-B — capacités et contrats communs (F-01) : ce module n'importe
plus core/authz_f2/_adapters.py (exceptions, sentinelles et contrats dans
core/authz_f2/_bridge_contracts.py). Chaque opération reçoit une capacité
SCELLÉE construite par les adaptateurs SQL pour CETTE connexion, CETTE
transaction et CE principal (CAPABILITIES) : TransactionProbe pour les deux
opérations historiques, FinalizationReadPort pour la finalisation. Toutes
les sondes de transaction passent par elle (le SQL reste dans les
adaptateurs) ; la relecture B2 n'est valide qu'UNE fois, dans le SAVEPOINT
C1 lié.

R1 PR 9-C1-A — primitives PRIVÉES de la future finalisation F2 (phases 3, 5,
6 et 7 du protocole figé), DORMANTES : aucune n'est atteinte par OPERATIONS,
par une commande du registre de production ni par _adapters ; l'inventaire
ci-dessus (deux opérations) est inchangé. Elles ne sont exercées que par le
banc d'essai isolé des tests (tests/authz_f2_pr9c1a_support.py).

| Phase | Primitive | Rôle |
|---|---|---|
| 3 / 8 | _business_savepoint(db) | SAVEPOINT métier C1 sur la Session du pont : refus s'il reste une écriture non flushée, sonde de la transaction du runner, puis SAVEPOINT ; sortie normale : flush + RELEASE + sonde ; _LocalRollback (motif FERMÉ) : sonde AVANT l'annulation (une erreur SQL avalée n'est jamais masquée), ROLLBACK TO SAVEPOINT, expunge_all(), sonde ; toute autre exception : ROLLBACK TO SAVEPOINT puis propagation (refus global, ROLLBACK du runner) |
| 5 | _prepare_turn(db, scope, user_id, delivery, ...) | décision cognitive verrouillée SANS écriture (dcr.prepare_locked_cognitive_turn), liée au SAVEPOINT C1 courant ; livraison du principal exigée |
| 6 | _PreparedTurn.context, _cognitive_context_matches | vue immuable et bornée de ce qui a été verrouillé (PreparedCognitiveContext) et comparaison PURE avec l'ancre / la session active d'un snapshot B2 (types de core.authz_f2.decryptage_context) |
| 7 | _apply_prepared_turn(db, scope, user_id, prepared, ...) | application EXACTE de la décision : dcr.apply_prepared_cognitive_turn, dp.apply_prepared_construction_these_progress sur la session préparée, bind_locked_analysis_session, record_turn_progress ; vue PreparedTurnApplied |

Garanties : une seule Session (celle du pont) ; jamais de COMMIT ni de
ROLLBACK global ; les SAVEPOINT historiques des services restent imbriqués
et inchangés ; une décision préparée n'est appliquée qu'une fois, dans le
même SAVEPOINT C1 encore actif, par le même principal — jamais après une
annulation locale (les verrous pris après le SAVEPOINT sont alors libérés
par PostgreSQL et les objets chargés dans le SAVEPOINT, qu'un ROLLBACK TO
SAVEPOINT SQLAlchemy n'expire pas, sont détachés par expunge_all()) ; les
verrous pris AVANT le SAVEPOINT restent détenus. N'écrit jamais la
réservation B1 (l'UPDATE reserved -> finalized appartient à _adapters.py,
PR 9-C1-B) ; aucun réseau.
"""
import json
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import assistant_delivery as ad
from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2 import decryptage_method_selection as dms
from core.authz_f2.principal import AuthorizedContext
from core.models import AnalysisSession, AssistantDelivery, CognitiveEvent, DecryptageCognitiveLink, SupportTrace

logger = logging.getLogger("oryx.authz_f2.decryptage_bridge")

# Mêmes valeurs que la route historique /decryptage (api.py), vérifiées par
# les tests : un seul tour assistant par tour utilisateur, méthode guidée.
DELIVERY_ORDINAL = 1
CONSTRUCTION_THESE = "construction_these"
# R1 PR 9-C1-B : valeurs de livraison reconnues par la reconnaissance
# terminale des adaptateurs SQL (qui n'importent pas core.assistant_delivery).
DELIVERY_STATUSES = (ad.PENDING, ad.DELIVERED)
DELIVERED = ad.DELIVERED
DELIVERY_SCHEMA_VERSION = ad.ASSISTANT_DELIVERY_SCHEMA_VERSION
# R1 PR 9-C2 : provenance cognitive EXIGÉE par le replay et l'ACK F2 (mêmes
# raisons : les adaptateurs SQL n'importent pas les services). Runtime V2
# seul ; couples statut de livraison / état du lien admis, tous les autres
# refusés ; vocabulaire fermé des actions de réponse.
COGNITIVE_RUNTIME_V2 = dcr.COGNITIVE_RUNTIME_VERSION_V2
RUNTIME_VERSION_KEY = dcr.RUNTIME_VERSION_KEY
CANONICAL_COUPLES = ((ad.PENDING, dcr.AWAITING_DELIVERY), (ad.DELIVERED, dcr.CAPTURED))
RESPONSE_ACTIONS = dcr.RESPONSE_ACTIONS
_FINGERPRINT_CHARS = frozenset("0123456789abcdef")


class TransactionOwnershipViolation(contracts.AdapterInvariantBroken):
    """Un service a tenté de piloter la transaction du runner."""

    alert = "decryptage_bridge_transaction_violation"


# R1 PR 9-C1-B : défini une seule fois dans les contrats communs (même objet).
TransactionLost = contracts.TransactionLost


class ProvenanceIncoherent(contracts.AdapterInvariantBroken):
    """Lien, event ou AnalysisSession d'une livraison propre qui
    n'appartiennent pas au même propriétaire : jamais verrouillés."""

    alert = "decryptage_bridge_provenance_incoherent"


class AckProvenanceIncoherent(contracts.AdapterInvariantBroken):
    """R1 PR 9-C2 : livraison attestée qui, VERROUILLÉE, n'est plus une
    livraison canonique C1-B V2 (runtime, lien, version, couple statut / lien)
    ou dont l'ACK ne produit pas une capture V2 : refus fermé, ROLLBACK."""

    alert = "decryptage_bridge_ack_provenance_incoherent"


class UnknownOperation(TypeError):
    """Opération hors de l'inventaire fermé (ou sous-classe)."""


class _BridgeSession(Session):
    """Session du pont : configuration figée, transaction jamais possédée.
    begin_nested() (SAVEPOINT des services) reste permis ; seul le pont
    ferme la Session (_release)."""

    def __init__(self, conn):
        super().__init__(bind=conn, autoflush=False, join_transaction_mode="rollback_only")

    def _refuse(self, method):
        raise TransactionOwnershipViolation(f"Session.{method}() interdit : le runner possède la transaction")

    def commit(self):
        self._refuse("commit")

    def rollback(self):
        self._refuse("rollback")

    def begin(self, nested=False):
        if not nested:
            self._refuse("begin")
        return super().begin(nested=True)

    def close(self):
        self._refuse("close")

    def reset(self):
        self._refuse("reset")

    def invalidate(self):
        self._refuse("invalidate")

    def connection(self, *args, **kwargs):
        self._refuse("connection")

    def _release(self):
        super().close()

    def _materialize_savepoint(self):
        """R1 PR 9-C1-B : un SAVEPOINT de Session est émis PARESSEUSEMENT (au
        premier usage de la Session). Le SAVEPOINT C1 est matérialisé dès son
        ouverture, pour qu'aucune lecture faite sur la connexion du runner (sonde,
        relecture B2) ne puisse s'exécuter hors de lui. Réservé au pont."""
        return super().connection()


# --------------------------------------------------------------------------
# Opérations fermées (données pures, sans user_id) et vues rendues
# --------------------------------------------------------------------------

def _text(value, label: str, max_chars=None) -> str:
    if type(value) is not str or not value or value != value.strip() or (max_chars and len(value) > max_chars):
        raise ValueError(f"{label} : chaîne non vide sans blanc de bord attendue")
    return value


def _json_object(value, label: str) -> dict:
    """Texte JSON canonique d'un objet (immuable) ; relu à chaque exécution."""
    try:
        parsed = json.loads(value) if type(value) is str else None
    except ValueError:
        parsed = None
    if type(parsed) is not dict:
        raise ValueError(f"{label} : objet JSON attendu")
    return parsed


@dataclass(frozen=True, slots=True)
class TurnFinalization:
    """Finalisation d'UN tour Décrypter déjà généré : même composition de
    services que la transaction historique /decryptage (phase B), sous le
    principal autorisé. response_payload et financial_snapshot sont des
    textes JSON (objets) : la donnée reste immuable."""
    conversation_key: str
    client_turn_id: uuid.UUID
    request_fingerprint: str
    ticker: str
    user_text: str              # question déjà strippée ("" : déclencheur ou reprise)
    method_id: object           # str ou None
    step_marker: object         # marqueur du vocabulaire fermé ou None
    response_payload: str       # JSON canonique du payload public (clé "analysis" textuelle)
    financial_snapshot: object  # JSON (objet) des données financières, ou None

    def __post_init__(self):
        _text(self.conversation_key, "conversation_key", c.CONVERSATION_KEY_MAX_CHARS)
        if type(self.client_turn_id) is not uuid.UUID:
            raise ValueError("client_turn_id : uuid.UUID attendu")
        if (type(self.request_fingerprint) is not str or len(self.request_fingerprint) != 64
                or not set(self.request_fingerprint) <= _FINGERPRINT_CHARS):
            raise ValueError("request_fingerprint : SHA-256 hexadécimal attendu")
        _text(self.ticker, "ticker", c.TICKER_MAX_CHARS)
        if type(self.user_text) is not str or self.user_text != self.user_text.strip():
            raise ValueError("user_text : chaîne strippée attendue")
        if self.method_id is not None:
            _text(self.method_id, "method_id")
        if self.step_marker is not None and (type(self.step_marker) is not str
                                             or self.step_marker not in dp.DECRYPTAGE_STEP_MARKERS):
            raise ValueError("step_marker : marqueur du vocabulaire fermé ou None attendu")
        if type(_json_object(self.response_payload, "response_payload").get("analysis")) is not str:
            raise ValueError("response_payload : texte visible 'analysis' attendu")
        if self.financial_snapshot is not None:
            _json_object(self.financial_snapshot, "financial_snapshot")


@dataclass(frozen=True, slots=True)
class DeliveryAcknowledgement:
    """ACK d'une livraison du principal dans sa conversation."""
    assistant_turn_id: uuid.UUID
    conversation_key: str

    def __post_init__(self):
        if type(self.assistant_turn_id) is not uuid.UUID:
            raise ValueError("assistant_turn_id : uuid.UUID attendu")
        _text(self.conversation_key, "conversation_key", c.CONVERSATION_KEY_MAX_CHARS)


@dataclass(frozen=True, slots=True)
class TurnFinalized:
    """Vue fermée (aucun payload, aucune métadonnée privée)."""
    assistant_turn_id: uuid.UUID
    created: bool
    delivery_status: str
    input_action: object             # vocabulaire dcr.INPUT_ACTIONS ou None (tour non capturé)
    product_progress_action: object  # vocabulaire dp.PROGRESS_ACTIONS ou None
    analysis_session_id: object      # uuid.UUID ou None

    def __post_init__(self):
        if type(self.assistant_turn_id) is not uuid.UUID or type(self.created) is not bool:
            raise ValueError("tour : identifiant ou création hors contrat")
        if self.delivery_status not in (ad.PENDING, ad.DELIVERED):
            raise ValueError("tour : statut hors contrat")
        if self.input_action is not None and self.input_action not in dcr.INPUT_ACTIONS:
            raise ValueError("tour : classement hors contrat")
        if self.product_progress_action is not None and self.product_progress_action not in dp.PROGRESS_ACTIONS:
            raise ValueError("tour : progression hors contrat")
        if self.analysis_session_id is not None and type(self.analysis_session_id) is not uuid.UUID:
            raise ValueError("tour : AnalysisSession hors contrat")


@dataclass(frozen=True, slots=True)
class DeliveryAcknowledged:
    assistant_turn_id: uuid.UUID
    delivery_status: str
    response_action: object  # vocabulaire dcr.RESPONSE_ACTIONS, ou None (livraison legacy)

    def __post_init__(self):
        if type(self.assistant_turn_id) is not uuid.UUID or self.delivery_status != ad.DELIVERED:
            raise ValueError("ACK : identifiant ou statut hors contrat")
        if self.response_action is not None and self.response_action not in dcr.RESPONSE_ACTIONS:
            raise ValueError("ACK : action hors contrat")


# --------------------------------------------------------------------------
# Points d'entrée privés (Session du pont, user_id du principal autorisé)
# --------------------------------------------------------------------------

def _finalize_turn(db, user_id: str, operation: TurnFinalization, probe):
    payload = json.loads(operation.response_payload)
    facts = None if operation.financial_snapshot is None else json.loads(operation.financial_snapshot)
    try:
        delivery, created = ad.claim_delivery(
            db, user_id=user_id, conversation_key=operation.conversation_key, surface=ad.DECRYPTAGE_SURFACE,
            source_user_turn_id=operation.client_turn_id, delivery_ordinal=DELIVERY_ORDINAL,
            request_fingerprint=operation.request_fingerprint,
            visible_content_fingerprint=ad.visible_content_fingerprint(payload["analysis"]),
            response_payload=payload, private_metadata=dcr.runtime_private_metadata(operation.step_marker),
            delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION,
        )
    except ad.AssistantDeliveryOwnershipConflict:
        return contracts.ABSENT  # conversation d'un autre utilisateur : rien n'est écrit (ROLLBACK)
    # Même règle que la route historique : seul l'appel qui crée la livraison
    # capture le tour et fait progresser le produit.
    link = dcr.capture_user_turn(db, delivery=delivery, ticker=operation.ticker,
                                 user_text=operation.user_text) if created else None
    contribution = link is not None and link.input_action == dcr.CONTRIBUTION_APPENDED
    answered = dcr.answered_step(db, link=link) if contribution else None
    progress = None
    if created and operation.method_id == CONSTRUCTION_THESE and operation.step_marker is not None:
        progress = dp.apply_construction_these_progress(
            db, user_id=user_id, ticker=operation.ticker, step=operation.step_marker,
            thesis_text=operation.user_text if contribution else None, data=facts,
            user_contribution=contribution, answered_step=answered,
        )
        if progress is not None:
            ad.bind_locked_analysis_session(db, delivery=delivery, user_id=user_id,
                                            analysis_session_id=progress.analysis_session.id)
            if link is not None:
                dcr.record_turn_progress(db, link=link, progress_action=progress.action)
    db.flush()  # flush final explicite, avant la vue rendue
    return TurnFinalized(delivery.id, created, delivery.status, link.input_action if link is not None else None,
                         progress.action if progress is not None else None, delivery.analysis_session_id)


# Toutes les références cognitives d'un lien (FK vers cognitive_events et
# support_traces, migrations 0012 et 0013), V1 comme V2 : la liste est
# comparée aux FK réelles du modèle par tests/test_authz_f2_pr9a_boundary.py.
_LINK_EVENT_COLUMNS = (
    DecryptageCognitiveLink.input_event_id,
    DecryptageCognitiveLink.input_context_event_id,
    DecryptageCognitiveLink.response_context_event_id,
    DecryptageCognitiveLink.context_exit_event_id,
    DecryptageCognitiveLink.response_event_id,
)
_LINK_TRACE_COLUMN = DecryptageCognitiveLink.support_trace_id


def _require_owned_provenance(db, delivery, user_id: str) -> None:
    """Propriété transitive, AVANT tout verrou de capture : le lien (enfant
    de la livraison verrouillée) et TOUTES ses références non nulles — les
    cinq events (entrée, contexte d'entrée, contexte de réponse, sortie de
    contexte V1, réponse), la SupportTrace et son event parent — ainsi que
    les AnalysisSessions désignées par la livraison et par chacun de ces
    events appartiennent au principal. Lectures simples, sans verrou, dans
    un ordre indifférent pour les verrous (aucune ligne n'est verrouillée) :
    une ligne d'autrui (donnée incohérente) n'est jamais verrouillée ; tout
    écart (propriétaire différent, référence introuvable) lève
    ProvenanceIncoherent, donc refus fermé et ROLLBACK intégral. Couvre
    aussi un ACK rejoué (livraison delivered, lien captured)."""
    link = db.execute(
        select(*_LINK_EVENT_COLUMNS, _LINK_TRACE_COLUMN)
        .where(DecryptageCognitiveLink.assistant_delivery_id == delivery.id)
    ).one_or_none()
    events = {e for e in (link[:len(_LINK_EVENT_COLUMNS)] if link is not None else ()) if e is not None}
    trace_id = link[len(_LINK_EVENT_COLUMNS)] if link is not None else None
    if trace_id is not None:
        parent = db.execute(
            select(SupportTrace.cognitive_event_id).where(SupportTrace.id == trace_id)
        ).scalar_one_or_none()
        if parent is None:
            raise ProvenanceIncoherent("SupportTrace de provenance introuvable")
        events.add(parent)  # event parent de l'aide : même contrôle que les autres
    sessions = {delivery.analysis_session_id} - {None}
    if events:
        rows = db.execute(
            select(CognitiveEvent.id, CognitiveEvent.user_id, CognitiveEvent.analysis_session_id)
            .where(CognitiveEvent.id.in_(events))
        ).all()
        if {row.id for row in rows} != events or any(row.user_id != user_id for row in rows):
            raise ProvenanceIncoherent("event de provenance d'un autre propriétaire ou introuvable")
        sessions |= {row.analysis_session_id for row in rows} - {None}
    if sessions:
        owners = db.execute(
            select(AnalysisSession.id, AnalysisSession.user_id).where(AnalysisSession.id.in_(sessions))
        ).all()
        if {row.id for row in owners} != sessions or any(row.user_id != user_id for row in owners):
            raise ProvenanceIncoherent("AnalysisSession de provenance d'un autre propriétaire ou introuvable")


def _require_attested_canonical(db, delivery, probe) -> None:
    """R1 PR 9-C2 : sur la livraison EFFECTIVEMENT verrouillée, AVANT la
    provenance transitive et toute capture : identité complète comparée à
    l'attestation (CapabilityRefused sinon), runtime V2 exigé, lien V2
    verrouillé (même ordre que la capture : livraison -> lien) et couple
    statut / état du lien admis. Aucune écriture."""
    probe.require_delivery(assistant_turn_id=delivery.id, user_id=delivery.user_id,
                           conversation_key=delivery.conversation_key, surface=delivery.surface,
                           source_user_turn_id=delivery.source_user_turn_id, delivery_ordinal=delivery.delivery_ordinal,
                           delivery_schema_version=delivery.delivery_schema_version)
    metadata = delivery.private_metadata
    if type(metadata) is not dict or metadata.get(RUNTIME_VERSION_KEY) != COGNITIVE_RUNTIME_V2:
        raise AckProvenanceIncoherent("livraison attestée hors runtime V2")
    link = db.execute(
        select(DecryptageCognitiveLink.capture_version, DecryptageCognitiveLink.capture_state)
        .where(DecryptageCognitiveLink.assistant_delivery_id == delivery.id)
        .with_for_update()
    ).one_or_none()
    if link is None or link.capture_version != COGNITIVE_RUNTIME_V2 \
            or (delivery.status, link.capture_state) not in CANONICAL_COUPLES:
        raise AckProvenanceIncoherent("lien cognitif de la livraison attestée absent ou incohérent")


def _acknowledge(db, user_id: str, operation: DeliveryAcknowledgement, probe):
    # R1 PR 9-C2 : chemin choisi par le type EXACT de la capacité, jamais par une
    # option : l'attestation C2 ne retombe jamais sur le chemin historique.
    attested = type(probe) is contracts.implementation(contracts.AckAdmissionPort)
    try:
        delivery = ad.lock_owned_delivery(db, assistant_turn_id=operation.assistant_turn_id, user_id=user_id,
                                          conversation_key=operation.conversation_key)
    except ad.AssistantDeliveryNotFound:
        if attested:  # livraison admise sous le verrou B1 puis introuvable : jamais un 404
            raise AckProvenanceIncoherent("livraison attestée introuvable sous verrou") from None
        return contracts.ABSENT  # absente, étrangère ou autre conversation : indiscernables, aucun verrou
    if attested:
        _require_attested_canonical(db, delivery, probe)
    _require_owned_provenance(db, delivery, user_id)
    link = dcr.capture_delivered_response(db, delivery=delivery)
    delivery = ad.acknowledge_locked_delivery(db, delivery=delivery, user_id=user_id)
    db.flush()  # flush final explicite, avant la vue rendue
    if attested and (link is None or link.capture_version != COGNITIVE_RUNTIME_V2
                     or link.capture_state != dcr.CAPTURED or link.response_action not in RESPONSE_ACTIONS):
        raise AckProvenanceIncoherent("ACK attesté sans capture V2")
    return DeliveryAcknowledged(delivery.id, delivery.status, link.response_action if link is not None else None)


def _visible_content_fingerprint(text: str) -> str:
    """R1 PR 9-C2 : empreinte visible HISTORIQUE (ad.visible_content_fingerprint)
    pour le contrôle du replay par les adaptateurs SQL (aucun accès base)."""
    return ad.visible_content_fingerprint(text)


# --------------------------------------------------------------------------
# R1 PR 9-C1-A — primitives privées de la future finalisation F2 (dormantes)
# --------------------------------------------------------------------------

# Motifs FERMÉS et PRIVÉS d'annulation LOCALE du SAVEPOINT C1 (aucun statut
# public nouveau). C1-A : context_changed ; C1-B (freeze V1.2.1) : les
# cinq autres. Leur traitement après l'annulation appartient aux adaptateurs
# SQL : context_changed / preparation_refused -> vue métier sans mutation ;
# legacy_collision -> relecture et blocage conditionnel prouvé ; lease_stale
# -> CONFLICT, intent_mismatch -> IDEMPOTENCY_CONFLICT, not_found -> ABSENT
# (ROLLBACK global du runner).
CONTEXT_CHANGED = "context_changed"
PREPARATION_REFUSED = "preparation_refused"
LEGACY_COLLISION = "legacy_collision"
LEASE_STALE = "lease_stale"
INTENT_MISMATCH = "intent_mismatch"
NOT_FOUND = "not_found"
LOCAL_ROLLBACK_REASONS = (CONTEXT_CHANGED, PREPARATION_REFUSED, LEGACY_COLLISION, LEASE_STALE, INTENT_MISMATCH,
                          NOT_FOUND)
# Classements V2 qu'une décision préparée peut porter (jamais V1).
_PREPARED_INPUT_ACTIONS = (dcr.NO_OPEN_EVENT, dcr.NO_USER_CONTRIBUTION, dcr.CONTRIBUTION_APPENDED,
                           dcr.CONTEXT_SWITCHED)
_STEP_MAX_CHARS = 32


class PendingWritesBeforeSavepoint(contracts.AdapterInvariantBroken):
    """Écriture ORM non flushée à l'ouverture du SAVEPOINT C1 (begin_nested()
    la flusherait HORS du SAVEPOINT) : refus, jamais un flush implicite."""

    alert = "decryptage_bridge_pending_writes"


class PreparedTurnUnusable(contracts.AdapterInvariantBroken):
    """Décision préparée ou SAVEPOINT C1 inutilisable : hors du SAVEPOINT
    courant, après une annulation locale, seconde application, autre
    principal, préparation qui écrit, construction directe."""

    alert = "decryptage_bridge_prepared_turn_unusable"


class _LocalRollback(Exception):
    """Seule demande d'annulation LOCALE du SAVEPOINT C1, levée par le code
    du pont pour un motif FERMÉ (LOCAL_ROLLBACK_REASONS). Toute autre
    exception annule le SAVEPOINT puis est propagée (refus global)."""

    def __init__(self, reason: str, detail=None):
        if type(reason) is not str or reason not in LOCAL_ROLLBACK_REASONS:
            raise ValueError("motif d'annulation locale hors vocabulaire fermé")
        # Seul preparation_refused porte un détail : le motif B2 fermé.
        if (reason == PREPARATION_REFUSED) != (detail is not None) or (
                detail is not None and (type(detail) is not str or detail not in dc.REFUSAL_REASONS)):
            raise ValueError("détail d'annulation locale hors contrat")
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


_OPEN, _RELEASED, _ROLLED_BACK, _FAILED = "open", "released", "rolled_back", "failed"


class _SavepointScope:
    """État du SAVEPOINT C1 rendu par _business_savepoint : ouvert, libéré
    (RELEASE), annulé localement (motif fermé) ou en échec (exception
    propagée). Non sérialisable ; aucune donnée métier."""

    __slots__ = ("_db", "_transaction", "_savepoint", "_state", "_reason", "_detail")

    def __init__(self, db):
        self._db, self._transaction, self._savepoint = db, None, None
        self._state, self._reason, self._detail = _OPEN, None, None

    def __reduce_ex__(self, protocol):
        raise TypeError("SAVEPOINT C1 non sérialisable")

    def __repr__(self) -> str:
        return f"_SavepointScope(state={self._state})"

    @property
    def released(self) -> bool:
        return self._state == _RELEASED

    @property
    def rolled_back(self) -> bool:
        return self._state == _ROLLED_BACK

    @property
    def reason(self):
        return self._reason

    @property
    def detail(self):
        return self._detail

    @property
    def is_open(self) -> bool:
        return self._state == _OPEN

    def _require_current(self, db) -> None:
        """Ce SAVEPOINT est ouvert, actif et le SAVEPOINT courant de CETTE
        Session (aucun autre ouvert au-dessus)."""
        transaction, savepoint = self._transaction, self._savepoint
        if (self._state != _OPEN or db is not self._db or transaction is None or not transaction.is_active
                or db.get_nested_transaction() is not transaction or savepoint is None or not savepoint.is_active
                or db.bind.get_nested_transaction() is not savepoint):
            raise PreparedTurnUnusable("hors du SAVEPOINT C1 courant")


def _has_pending_writes(db) -> bool:
    return bool(db.new or db.dirty or db.deleted)


@contextmanager
def _business_savepoint(db, probe):
    """SAVEPOINT métier C1 (phases 3 et 8), sur la Session du pont, dans la
    transaction du runner ; jamais imbriqué dans un autre SAVEPOINT.

    Ouverture : aucune écriture ORM en attente (PendingWritesBeforeSavepoint)
    puis sonde, puis SAVEPOINT. Sortie normale : flush, RELEASE SAVEPOINT,
    sonde ; scope.released. _LocalRollback (motif fermé) : sonde AVANT
    l'annulation — une erreur SQL avalée dans le SAVEPOINT reste un refus
    global —, ROLLBACK TO SAVEPOINT, expunge_all() (aucune référence ORM du
    SAVEPOINT annulé n'est réutilisable : SQLAlchemy n'expire pas les objets
    seulement chargés), sonde ; scope.rolled_back, la transaction du runner
    continue. Toute autre exception : ROLLBACK TO SAVEPOINT, scope en échec,
    exception propagée (le runner annule tout). Jamais de COMMIT ni de
    ROLLBACK global ; les SAVEPOINT des services s'imbriquent dedans.

    R1 PR 9-C1-B : chaque sonde passe par la capacité TransactionProbe de
    l'opération (SQL dans les adaptateurs, jamais ici), liée à la connexion
    de CETTE Session. Si la capacité est le FinalizationReadPort de la
    finalisation, il est lié UNE fois à ce SAVEPOINT (bind_scope) : sa
    relecture B2 n'est valide que dans ce SAVEPOINT ouvert et courant."""
    if type(db) is not _BridgeSession:
        raise TypeError("SAVEPOINT C1 : Session du pont attendue")
    _require_probe(probe, db.bind)
    if db.get_nested_transaction() is not None:
        raise PreparedTurnUnusable("SAVEPOINT C1 imbriqué dans un autre SAVEPOINT")
    if _has_pending_writes(db):
        raise PendingWritesBeforeSavepoint("écriture non flushée avant le SAVEPOINT C1")
    probe.require_live()
    scope = _SavepointScope(db)
    try:
        with db.begin_nested() as transaction:
            scope._transaction = transaction
            db._materialize_savepoint()  # SAVEPOINT émis MAINTENANT sur la connexion du runner
            scope._savepoint = db.bind.get_nested_transaction()
            if type(probe) is contracts.implementation(contracts.FinalizationReadPort):
                probe.bind_scope(scope)  # association unique port <-> CE SAVEPOINT
            try:
                yield scope
            except _LocalRollback:
                probe.require_live()  # sous-transaction avortée : refus global, jamais une annulation locale
                raise
            db.flush()
    except _LocalRollback as refusal:
        scope._state, scope._reason, scope._detail = _ROLLED_BACK, refusal.reason, refusal.detail
        db.expunge_all()
        probe.require_live()
        logger.info("decryptage_bridge savepoint=rolled_back reason=%s", refusal.reason)
        return
    except BaseException:
        scope._state = _FAILED
        raise
    scope._state = _RELEASED
    probe.require_live()


@dataclass(frozen=True, slots=True)
class PreparedCognitiveContext:
    """Vue immuable et bornée de la décision préparée (phase 6) : ce qui a
    été verrouillé et sera appliqué tel quel. Identifiants techniques et
    vocabulaires fermés seulement ; jamais de texte utilisateur ni
    d'instance ORM."""
    ticker: str
    analysis_session_id: object         # uuid.UUID ou None (absence préparée)
    current_step: object                # étape ouverte verrouillée (str) ou None
    anchor_event_id: object             # uuid.UUID ou None
    anchor_analysis_session_id: object  # uuid.UUID ou None
    input_action: str                   # _PREPARED_INPUT_ACTIONS
    contribution_event_id: object       # uuid.UUID (= ancre) ou None
    response_context_event_id: object   # uuid.UUID ou None
    answered_step: object               # dp.ANSWERABLE_STEPS (contribution) ou None

    def __post_init__(self):
        _text(self.ticker, "ticker", c.TICKER_MAX_CHARS)
        for name in ("analysis_session_id", "anchor_event_id", "anchor_analysis_session_id",
                     "contribution_event_id", "response_context_event_id"):
            value = getattr(self, name)
            if value is not None and type(value) is not uuid.UUID:
                raise ValueError(f"contexte préparé : {name} hors contrat")
        if self.current_step is not None and (type(self.current_step) is not str
                                              or len(self.current_step) > _STEP_MAX_CHARS):
            raise ValueError("contexte préparé : étape hors contrat")
        if self.analysis_session_id is None and self.current_step is not None:
            raise ValueError("contexte préparé : étape sans session")
        if type(self.input_action) is not str or self.input_action not in _PREPARED_INPUT_ACTIONS:
            raise ValueError("contexte préparé : classement hors contrat")
        if (self.anchor_event_id is None) != (self.anchor_analysis_session_id is None) or (
                (self.anchor_event_id is None) != (self.input_action == dcr.NO_OPEN_EVENT)):
            raise ValueError("contexte préparé : ancre incohérente")
        contribution = self.input_action == dcr.CONTRIBUTION_APPENDED
        if contribution != (self.contribution_event_id is not None) or (
                contribution and self.contribution_event_id != self.anchor_event_id):
            raise ValueError("contexte préparé : contribution incohérente")
        if contribution != (self.answered_step is not None) or (
                contribution and self.answered_step not in dp.ANSWERABLE_STEPS):
            raise ValueError("contexte préparé : étape répondue incohérente")
        if self.input_action in dcr.ANCHORED_INPUT_ACTIONS and (
                self.response_context_event_id != self.anchor_event_id
                or self.analysis_session_id != self.anchor_analysis_session_id):
            raise ValueError("contexte préparé : tour ancré incohérent")

    def __reduce__(self):
        raise TypeError("PreparedCognitiveContext n'est pas sérialisable")


def _context_of(decision) -> PreparedCognitiveContext:
    session = decision.analysis_session
    return PreparedCognitiveContext(
        decision.ticker, session.id if session is not None else None,
        session.current_step if session is not None else None, decision.anchor_event_id,
        decision.anchor_analysis_session_id, decision.input_action, decision.contribution_event_id,
        decision.response_context_event_id, decision.answered_step)


_HANDLE_TOKEN = object()
_HANDLE_PREPARED, _HANDLE_CONSUMED = "prepared", "consumed"


class _PreparedTurn:
    """Décision préparée DANS un SAVEPOINT C1 précis, pour un principal
    précis : PreparedCognitiveTurn (core.decryptage_cognitive_runtime) +
    liaison au SAVEPOINT + vue immuable `context`. Construite seulement par
    _prepare_turn, immuable, à usage unique (consommée avant toute
    mutation), non sérialisable, sans texte dans son repr()."""

    __slots__ = ("_scope", "_user_id", "_decision", "_context", "_state")

    def __init__(self, token, scope, user_id, decision):
        if token is not _HANDLE_TOKEN:
            raise PreparedTurnUnusable("décision construite hors de _prepare_turn")
        for name, value in (("_scope", scope), ("_user_id", user_id), ("_decision", decision),
                            ("_context", _context_of(decision)), ("_state", _HANDLE_PREPARED)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("décision préparée immuable")

    def __delattr__(self, name):
        raise AttributeError("décision préparée immuable")

    def __reduce_ex__(self, protocol):
        raise TypeError("décision préparée non sérialisable")

    def __copy__(self):
        raise TypeError("décision préparée non copiable")

    def __deepcopy__(self, memo):
        raise TypeError("décision préparée non copiable")

    def __repr__(self) -> str:
        return f"_PreparedTurn(input_action={self._context.input_action}, state={self._state})"

    @property
    def context(self) -> PreparedCognitiveContext:
        return self._context

    def _consume(self, db, scope, user_id: str):
        if self._state != _HANDLE_PREPARED:
            raise PreparedTurnUnusable("décision préparée déjà appliquée")
        object.__setattr__(self, "_state", _HANDLE_CONSUMED)
        if scope is not self._scope or user_id != self._user_id:
            raise PreparedTurnUnusable("décision préparée hors de son SAVEPOINT ou de son principal")
        scope._require_current(db)
        return self._decision


def _prepare_turn(db, scope, user_id: str, delivery, *, ticker: str, user_text: str) -> _PreparedTurn:
    """Phase 5 : décision cognitive verrouillée, SANS écriture, dans le
    SAVEPOINT C1 courant. delivery : la livraison pending du principal
    (revendiquée dans ce SAVEPOINT, phase 4 — C1-B). Aucune écriture ORM en
    attente avant ni après (une préparation qui écrirait est un refus
    fermé) ; plusieurs sessions actives ou events open compatibles, ancre
    stale : exceptions du runtime cognitif propagées."""
    scope._require_current(db)
    if type(delivery) is not AssistantDelivery or delivery.user_id != user_id:
        raise ProvenanceIncoherent("livraison préparée hors du principal autorisé")
    _text(ticker, "ticker", c.TICKER_MAX_CHARS)
    if type(user_text) is not str or user_text != user_text.strip():
        raise ValueError("user_text : chaîne strippée attendue")
    if _has_pending_writes(db):
        raise PendingWritesBeforeSavepoint("écriture non flushée avant la préparation")
    decision = dcr.prepare_locked_cognitive_turn(db, delivery=delivery, ticker=ticker, user_text=user_text)
    if _has_pending_writes(db):
        raise PreparedTurnUnusable("la préparation cognitive a produit une écriture")
    scope._require_current(db)
    return _PreparedTurn(_HANDLE_TOKEN, scope, user_id, decision)


def _cognitive_context_matches(context: PreparedCognitiveContext, *, ticker: str, anchor,
                               active_session) -> bool:
    """Phase 6 (partie cognitive), fonction PURE : la décision préparée
    décrit-elle EXACTEMENT le contexte d'un snapshot B2 (ticker résolu,
    dc.PreparationAnchor, dc.PreparationActiveSession ou None) ?

    - session active : même identifiant ET même étape ouverte, ou absente
      des deux côtés ;
    - ancre none : aucune ancre préparée (no_open_event) ;
    - ancre other_ticker : même event, même AnalysisSession, context_switched ;
    - ancre current : même event, même AnalysisSession, tour ancré ;
    - ancre unbound : jamais (la préparation refuse une ancre sans session).
    Le niveau, sa révision, les faits, contributions et la thèse du snapshot
    ne sont PAS revalidés ici (PR 9-C1-B)."""
    if type(context) is not PreparedCognitiveContext or type(anchor) is not dc.PreparationAnchor or (
            active_session is not None and type(active_session) is not dc.PreparationActiveSession):
        raise TypeError("revalidation : types fermés attendus")
    if context.ticker != ticker:
        return False
    expected = (None, None) if active_session is None else (active_session.analysis_session_id,
                                                            active_session.current_step)
    if (context.analysis_session_id, context.current_step) != expected:
        return False
    if anchor.state == dc.ANCHOR_NONE:
        return context.anchor_event_id is None and context.input_action == dcr.NO_OPEN_EVENT
    if anchor.state not in (dc.ANCHOR_OTHER_TICKER, dc.ANCHOR_CURRENT):
        return False
    if context.anchor_event_id != anchor.event_id or context.anchor_analysis_session_id != anchor.analysis_session_id:
        return False
    if anchor.state == dc.ANCHOR_OTHER_TICKER:
        return context.input_action == dcr.CONTEXT_SWITCHED
    return context.input_action in dcr.ANCHORED_INPUT_ACTIONS


@dataclass(frozen=True, slots=True)
class PreparedTurnApplied:
    """Vue fermée de la phase 7 (aucun payload, aucun texte)."""
    assistant_turn_id: uuid.UUID
    input_action: str
    product_progress_action: object  # dp.PROGRESS_ACTIONS ou None
    analysis_session_id: object      # uuid.UUID ou None

    def __post_init__(self):
        if type(self.assistant_turn_id) is not uuid.UUID or self.input_action not in _PREPARED_INPUT_ACTIONS:
            raise ValueError("application : tour hors contrat")
        if self.product_progress_action is not None and self.product_progress_action not in dp.PROGRESS_ACTIONS:
            raise ValueError("application : progression hors contrat")
        if self.analysis_session_id is not None and type(self.analysis_session_id) is not uuid.UUID:
            raise ValueError("application : AnalysisSession hors contrat")

    def __reduce__(self):
        raise TypeError("PreparedTurnApplied n'est pas sérialisable")


def _apply_prepared_turn(db, scope, user_id: str, prepared, *, method_id, step_marker,
                         financial_snapshot) -> PreparedTurnApplied:
    """Phase 7 : applique EXACTEMENT la décision préparée, dans le même
    SAVEPOINT C1 encore courant, pour le même principal, une seule fois.
    Même composition que la finalisation historique (après claim) :
    contribution + lien (dcr.apply_prepared_cognitive_turn), progression
    construction_these sur la session PRÉPARÉE
    (dp.apply_prepared_construction_these_progress : jamais de relecture de
    « la session active »), rattachement de la livraison, décision de
    progression sur le lien ; flush. Aucune écriture de la réservation B1."""
    if type(prepared) is not _PreparedTurn:
        raise PreparedTurnUnusable("décision préparée attendue")
    if method_id is not None:
        _text(method_id, "method_id")
    if step_marker is not None and (type(step_marker) is not str or step_marker not in dp.DECRYPTAGE_STEP_MARKERS):
        raise ValueError("step_marker : marqueur du vocabulaire fermé ou None attendu")
    facts = None if financial_snapshot is None else _json_object(financial_snapshot, "financial_snapshot")
    decision = prepared._consume(db, scope, user_id)
    link = dcr.apply_prepared_cognitive_turn(db, prepared=decision)
    contribution = link.input_action == dcr.CONTRIBUTION_APPENDED
    answered = decision.answered_step if contribution else None
    progress = None
    if method_id == CONSTRUCTION_THESE and step_marker is not None:
        progress = dp.apply_prepared_construction_these_progress(
            db, analysis_session=decision.analysis_session, user_id=user_id, ticker=decision.ticker,
            step=step_marker, thesis_text=decision.user_text if contribution else None, data=facts,
            user_contribution=contribution, answered_step=answered,
        )
        if progress is not None:
            if decision.analysis_session is not None and progress.analysis_session is not decision.analysis_session:
                raise PreparedTurnUnusable("progression appliquée à une autre AnalysisSession")
            ad.bind_locked_analysis_session(db, delivery=decision.delivery, user_id=user_id,
                                            analysis_session_id=progress.analysis_session.id)
            dcr.record_turn_progress(db, link=link, progress_action=progress.action)
    db.flush()
    scope._require_current(db)
    return PreparedTurnApplied(decision.delivery.id, link.input_action,
                               progress.action if progress is not None else None,
                               decision.delivery.analysis_session_id)


# --------------------------------------------------------------------------
# R1 PR 9-C1-B — finalisation atomique d'un candidat B2 (troisième opération)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, repr=False)
class CandidateFinalization:
    """Opération PRIVÉE : finaliser UN candidat B2 déjà vérifié sous le
    verrou de sa réservation par les adaptateurs SQL (HMAC, epoch, jeton,
    échéance). Données pures : aucun user_id (principal autorisé), aucune
    Session, connexion, fonction ni SQL."""
    candidate: dcand.GeneratedDecryptageCandidate

    def __post_init__(self):
        c._revalidate_candidate(self.candidate)

    def __repr__(self) -> str:
        return f"CandidateFinalization({self.candidate!r})"


@dataclass(frozen=True, slots=True)
class FinalizationRolledBack:
    """Vue fermée : SAVEPOINT C1 annulé localement pour un motif fermé ;
    aucune écriture de la finalisation ne subsiste. Les adaptateurs SQL en
    décident l'issue (vue métier, blocage prouvé ou ROLLBACK global)."""
    reason: str
    detail: object  # motif B2 fermé (preparation_refused) ou None

    def __post_init__(self):
        _LocalRollback(self.reason, self.detail)  # même validation fermée


def _delivery_request_fingerprint(candidate) -> str:
    """SHA-256 historique de la livraison (core.assistant_delivery.
    decryptage_request_fingerprint) : identité SERVEUR, entrées ORIGINALES
    normalisées du tour et niveau du snapshot du candidat (jamais le niveau
    courant du profil). Distinct du HMAC d'intention B1 et de l'empreinte du
    snapshot B2."""
    return ad.decryptage_request_fingerprint(**candidate.delivery_fingerprint_inputs())


def _exactly(exc, expected) -> bool:
    return type(exc) is expected


def _same_preparation(candidate, view, context) -> bool:
    """Phase 6 complète : snapshot B2 RECONSTRUIT depuis la vue PostgreSQL
    relue dans le SAVEPOINT (méthode recalculée par le sélecteur partagé
    avec la génération B2) ET identique à celui du candidat (contenu
    canonique ET empreinte), PUIS décision cognitive préparée compatible
    avec ce snapshot. Les deux conditions sont exigées."""
    turn, snapshot = candidate.turn, candidate.preparation
    selection = dms.select_method(has_active_session=view.active_session is not None, question=turn.question,
                                  context=turn.context, last_method_id=turn.last_method_id,
                                  market_name=candidate.market.name, ticker=view.ticker)
    rebuilt = dcand.build_snapshot(view, turn.ticker_input, selection.method_id, selection.basis)
    if rebuilt.fingerprint != snapshot.fingerprint or rebuilt.canonical() != snapshot.canonical():
        return False
    return _cognitive_context_matches(context, ticker=snapshot.ticker, anchor=snapshot.anchor,
                                      active_session=snapshot.active_session)


def _finalize_candidate(db, user_id: str, operation: CandidateFinalization, probe):
    """Phases 6 à 13 du protocole figé (les phases 1 à 5 et 14 à 17
    appartiennent au runner et aux adaptateurs SQL) :

    SAVEPOINT C1 -> claim_delivery provisoire (une livraison préexistante
    n'est JAMAIS adoptée : created=False ou AssistantTurnIdentityCollision
    -> annulation locale legacy_collision) -> Prepare (seules
    AmbiguousActiveAnalysisSessions et StaleConversationContext deviennent
    preparation_refused) -> UNE relecture B2 par le FinalizationReadPort
    (lease_stale / preparation_refused) -> snapshot reconstruit et comparé
    + décision cognitive compatible (sinon context_changed) -> Apply
    (décision préparée, progression sur la session préparée) -> flush,
    RELEASE. Toute autre erreur : ROLLBACK TO SAVEPOINT puis propagation
    (refus global). N'écrit jamais la réservation."""
    candidate = operation.candidate
    if candidate.owner_user_id != user_id:
        raise ProvenanceIncoherent("candidat d'un autre principal")
    turn, snapshot = candidate.turn, candidate.preparation
    payload = candidate.payload.as_dict()
    request_fingerprint = _delivery_request_fingerprint(candidate)
    facts = json.dumps(dfin.progress_facts(candidate.market), allow_nan=False)
    applied = None
    with _business_savepoint(db, probe) as scope:
        try:
            delivery, created = ad.claim_delivery(
                db, user_id=user_id, conversation_key=turn.conversation_key, surface=ad.DECRYPTAGE_SURFACE,
                source_user_turn_id=turn.client_turn_id, delivery_ordinal=DELIVERY_ORDINAL,
                request_fingerprint=request_fingerprint,
                visible_content_fingerprint=ad.visible_content_fingerprint(payload["analysis"]),
                response_payload=payload, private_metadata=dcr.runtime_private_metadata(candidate.step_marker),
                delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION,
            )
        except ad.AssistantTurnIdentityCollision as exc:
            if not _exactly(exc, ad.AssistantTurnIdentityCollision):
                raise
            raise _LocalRollback(LEGACY_COLLISION) from None
        if not created:  # livraison préexistante (même empreinte comprise) : jamais adoptée
            raise _LocalRollback(LEGACY_COLLISION)
        try:
            prepared = _prepare_turn(db, scope, user_id, delivery, ticker=snapshot.ticker, user_text=turn.question)
        except dcr.AmbiguousActiveAnalysisSessions as exc:
            if not _exactly(exc, dcr.AmbiguousActiveAnalysisSessions):
                raise
            raise _LocalRollback(PREPARATION_REFUSED, dc.MULTIPLE_ACTIVE_SESSIONS) from None
        except dcr.StaleConversationContext as exc:
            if not _exactly(exc, dcr.StaleConversationContext):
                raise
            raise _LocalRollback(PREPARATION_REFUSED, dc.STALE_CONVERSATION_CONTEXT) from None
        current = probe.read_preparation(db, scope)
        if current is contracts.CONFLICT:
            raise _LocalRollback(LEASE_STALE)
        if current is contracts.IDEMPOTENCY_CONFLICT:
            raise _LocalRollback(INTENT_MISMATCH)
        if current is contracts.ABSENT:
            raise _LocalRollback(NOT_FOUND)
        if type(current) is dc.DecryptagePreparationRefused:
            raise _LocalRollback(PREPARATION_REFUSED, current.reason)
        if type(current) is not dc.DecryptagePreparationView:
            raise contracts.AdapterInvariantBroken("relecture de préparation hors contrat")
        if not _same_preparation(candidate, current, prepared.context):
            raise _LocalRollback(CONTEXT_CHANGED)
        applied = _apply_prepared_turn(db, scope, user_id, prepared, method_id=snapshot.method_id,
                                       step_marker=candidate.step_marker, financial_snapshot=facts)
    if scope.rolled_back:
        return FinalizationRolledBack(scope.reason, scope.detail)
    if not scope.released or type(applied) is not PreparedTurnApplied:
        raise contracts.AdapterInvariantBroken("SAVEPOINT C1 dans un état inattendu")
    return applied


# Inventaire FERMÉ : type exact d'opération -> point d'entrée privé.
OPERATIONS = MappingProxyType({
    TurnFinalization: _finalize_turn,
    DeliveryAcknowledgement: _acknowledge,
    CandidateFinalization: _finalize_candidate,
})
# Contrats EXACTS admis par opération (tuple fermé ; la capacité doit être
# l'implémentation exacte de l'un d'eux) : la finalisation historique n'a que
# la sonde ; seule la finalisation C1-B reçoit la relecture B2. R1 PR 9-C2 :
# l'ACK admet EXACTEMENT la sonde (chemin historique PR 9-A) OU l'attestation
# C2 (AckAdmissionPort) ; aucun autre contrat, aucune sous-classe.
CAPABILITIES = MappingProxyType({
    TurnFinalization: (contracts.TransactionProbe,),
    DeliveryAcknowledgement: (contracts.TransactionProbe, contracts.AckAdmissionPort),
    CandidateFinalization: (contracts.FinalizationReadPort,),
})


def _admitted_capabilities(operation) -> frozenset:
    """Classes EXACTES admises pour cette opération (implémentations chargées)."""
    return frozenset(contracts.implementation(k) for k in CAPABILITIES[type(operation)]) - {None}


def _require_probe(probe, conn, user_id=None) -> None:
    """Capacité scellée (classe EXACTE implémentée par les adaptateurs SQL),
    ouverte, liée à CETTE connexion (et, si donné, à ce principal)."""
    admitted = {contracts.implementation(k) for k in contracts.CONTRACTS} - {None}
    if type(probe) not in admitted or not probe.bound_to(conn, probe.user_id if user_id is None else user_id):
        raise contracts.CapabilityRefused("capacité de transaction hors contrat")


def _require_live(probe) -> None:
    """La transaction du runner est toujours LA transaction active, et
    PostgreSQL l'accepte encore (sonde de la capacité : une erreur SQL
    avalée laisse la transaction avortée alors que SQLAlchemy la croit
    active ; mesuré : son COMMIT devient alors un ROLLBACK silencieux)."""
    probe.require_live()


def _execute(conn, ctx, operation, probe):
    """Exécute UNE opération de l'inventaire dans la transaction du runner
    (adaptateur d'exécution, après T_authorization), avec la capacité
    EXACTE de cette opération, construite par les adaptateurs SQL pour
    cette connexion et ce principal. Rend une vue immuable, ou ABSENT ;
    toute exception est propagée au runner (ROLLBACK)."""
    entry = OPERATIONS.get(type(operation))
    if entry is None:
        raise UnknownOperation("opération hors de l'inventaire fermé du pont")
    if type(ctx) is not AuthorizedContext:
        raise TypeError("pont : AuthorizedContext attendu")
    if type(probe) not in _admitted_capabilities(operation):
        raise contracts.CapabilityRefused("capacité non admise pour cette opération")
    _require_probe(probe, conn, ctx.principal.user_id)
    _require_live(probe)
    session = _BridgeSession(conn)
    try:
        result = entry(session, ctx.principal.user_id, operation, probe)  # flush final explicite, puis vue
        _require_live(probe)
    except BaseException:
        try:
            session._release()
        except Exception as cleanup:  # jamais au détriment de l'échec d'origine
            logger.warning("decryptage_bridge cleanup_failed error=%s", type(cleanup).__name__)
        raise
    session._release()
    return result
