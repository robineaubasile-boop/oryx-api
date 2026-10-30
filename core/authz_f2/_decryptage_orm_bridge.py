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
| DeliveryAcknowledgement | lock_owned_delivery ; provenance COMPLÈTE relue SANS verrou (5 events, SupportTrace et son event, AnalysisSessions) ; capture_delivered_response ; acknowledge_locked_delivery |

Le propriétaire est TOUJOURS AuthorizedContext.principal.user_id : une
opération ne porte aucun user_id, aucune Session, connexion, fonction ni
SQL. Une livraison absente, étrangère ou d'une autre conversation, ou une
conversation d'un autre utilisateur : ABSENT (NOT_FOUND, rien n'est
écrit). Toute autre erreur métier ou PostgreSQL est propagée : le runner
annule tout (UNAVAILABLE), jamais converti en succès.

Aucune commande du registre de production n'atteint ce module (registre à
quatorze commandes inchangé) : PR 9-A ne l'exerce que par le banc d'essai
isolé des tests. api.py ne l'importe pas (F2 dormant).
"""
import json
import logging
import uuid
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import assistant_delivery as ad
from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2.principal import AuthorizedContext
from core.models import AnalysisSession, CognitiveEvent, DecryptageCognitiveLink, SupportTrace

logger = logging.getLogger("oryx.authz_f2.decryptage_bridge")

# Mêmes valeurs que la route historique /decryptage (api.py), vérifiées par
# les tests : un seul tour assistant par tour utilisateur, méthode guidée.
DELIVERY_ORDINAL = 1
CONSTRUCTION_THESE = "construction_these"
_FINGERPRINT_CHARS = frozenset("0123456789abcdef")


class TransactionOwnershipViolation(_adapters.AdapterInvariantBroken):
    """Un service a tenté de piloter la transaction du runner."""

    alert = "decryptage_bridge_transaction_violation"


class TransactionLost(_adapters.AdapterInvariantBroken):
    """La transaction externe n'est plus la transaction active du runner :
    aucun succès ne peut être rendu."""

    alert = "decryptage_bridge_transaction_lost"


class ProvenanceIncoherent(_adapters.AdapterInvariantBroken):
    """Lien, event ou AnalysisSession d'une livraison propre qui
    n'appartiennent pas au même propriétaire : jamais verrouillés."""

    alert = "decryptage_bridge_provenance_incoherent"


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

def _finalize_turn(db, user_id: str, operation: TurnFinalization):
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
        return _adapters.ABSENT  # conversation d'un autre utilisateur : rien n'est écrit (ROLLBACK)
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


def _acknowledge(db, user_id: str, operation: DeliveryAcknowledgement):
    try:
        delivery = ad.lock_owned_delivery(db, assistant_turn_id=operation.assistant_turn_id, user_id=user_id,
                                          conversation_key=operation.conversation_key)
    except ad.AssistantDeliveryNotFound:
        return _adapters.ABSENT  # absente, étrangère ou autre conversation : indiscernables, aucun verrou
    _require_owned_provenance(db, delivery, user_id)
    link = dcr.capture_delivered_response(db, delivery=delivery)
    delivery = ad.acknowledge_locked_delivery(db, delivery=delivery, user_id=user_id)
    db.flush()  # flush final explicite, avant la vue rendue
    return DeliveryAcknowledged(delivery.id, delivery.status, link.response_action if link is not None else None)


# Inventaire FERMÉ : type exact d'opération -> point d'entrée privé.
OPERATIONS = MappingProxyType({
    TurnFinalization: _finalize_turn,
    DeliveryAcknowledgement: _acknowledge,
})


def _require_live(conn, outer) -> None:
    """La transaction du runner est toujours LA transaction active, et
    PostgreSQL l'accepte encore : une erreur SQL avalée la laisse avortée
    (SQLAlchemy la croit active ; mesuré : son COMMIT devient alors un
    ROLLBACK silencieux, sans erreur psycopg2). La sonde réutilise
    l'instruction fermée de l'isolation, qui échoue (InFailedSqlTransaction)
    sur une transaction avortée et revérifie READ COMMITTED."""
    if outer is None or not outer.is_active or conn.get_transaction() is not outer:
        raise TransactionLost("transaction externe inactive ou remplacée")
    _adapters.require_read_committed(conn, c.DECRYPTAGE_MUTATION)


def _execute(conn, ctx, operation):
    """Exécute UNE opération de l'inventaire dans la transaction du runner
    (adaptateur d'exécution, après T_authorization). Rend une vue immuable,
    ou ABSENT ; toute exception est propagée au runner (ROLLBACK)."""
    entry = OPERATIONS.get(type(operation))
    if entry is None:
        raise UnknownOperation("opération hors de l'inventaire fermé du pont")
    if type(ctx) is not AuthorizedContext:
        raise TypeError("pont : AuthorizedContext attendu")
    outer = conn.get_transaction()
    _require_live(conn, outer)
    session = _BridgeSession(conn)
    try:
        result = entry(session, ctx.principal.user_id, operation)  # flush final explicite, puis vue
        _require_live(conn, outer)
    except BaseException:
        try:
            session._release()
        except Exception as cleanup:  # jamais au détriment de l'échec d'origine
            logger.warning("decryptage_bridge cleanup_failed error=%s", type(cleanup).__name__)
        raise
    session._release()
    return result
