"""Orchestration runtime de la capture cognitive T2 sur Décrypter (R1-C2,
affinité conversationnelle R1-C4).

Premier branchement réel de la capture cognitive (core/cognitive_capture.py)
sur une surface produit, volontairement FERMÉ à Décrypter : « Décrypter-
specific, structurally extensible ». Les primitives CognitiveEvent /
SupportTrace restent transversales ; seule cette orchestration est
spécifique.

Répartition des vérités :

- core/assistant_delivery.py : vérité de livraison (pending -> delivered) ;
- core/decryptage_progress.py : vérité de progression produit
  (AnalysisSession construction_these, marqueurs d'étape) ;
- core/cognitive_capture.py : primitives T2 (events, contributions, aides) ;
- ce module : QUAND ouvrir, continuer, fermer un CognitiveEvent Décrypter,
  quoi y rattacher et quelles aides étaient réellement disponibles, tracé
  dans decryptage_cognitive_links.

Ce module ne fait AUCUN T3+ : ni évaluation, ni PedagogicalObservation, ni
taxonomie / capacités, ni longitudinal, ni inférence, ni Step 6. Il
n'attribue ni compétence (C1-C12), ni stade, ni force de preuve, ni niveau
d'aide pédagogique, ni polarité, ni justesse, et n'interprète jamais une
réponse. task_kind reste NULL : le marqueur d'étape produit n'est ni un
task_kind ni une compétence.

Doctrine (à ne pas confondre) :

- DecryptageCognitiveLink n'est pas une preuve : c'est de la provenance ;
- CognitiveEvent n'est pas un message : un event couvre plusieurs tours et
  peut couvrir plusieurs conversations ; conversation != CognitiveEvent !=
  AnalysisSession ;
- delivered (ACK) signifie « inséré dans l'interface », jamais lu / compris ;
- nouvelle conversation != reset pédagogique : une reprise dans une nouvelle
  conversation continue l'event open de l'AnalysisSession (session_ref de la
  contribution = conversation du tour) ;
- same marker = continuation : convention de segmentation faute de frontière
  plus fine, pas une assertion pédagogique ;
- frontière ambiguë (marqueur absent, inconnu ou rétrograde) : on conserve
  l'event open plutôt que de fragmenter ; la réponse visible devient un
  SupportTrace (« ce contenu a été disponible avant une future
  contribution », pas « c'était une aide substantielle »).

Le texte utilisateur d'un tour répond à la tâche PRÉCÉDEMMENT rendue dans SA
conversation ; le marqueur de la réponse indique l'étape que la réponse
ouvre. D'où deux moments :

1. capture_user_turn, dans la transaction /decryptage du worker gagnant
   (après claim_delivery, AVANT la progression produit) : sort de la
   production utilisateur et création de la ligne de lien en
   awaiting_delivery ;
2. capture_delivered_response, dans la transaction ACK, AVANT
   acknowledge_delivery : lifecycle de l'event selon la réponse réellement
   delivered, SupportTrace éventuel, lien -> captured. Tout échec annule
   l'ACK (la livraison reste pending, le lien awaiting_delivery).

R1-C4 — runtime V2 (decryptage-cognitive-runtime-v2), seul runtime des
nouveaux tours :

- Ancre conversationnelle : l'état cognitif courant d'une conversation est
  déterminé UNIQUEMENT par sa dernière livraison V2 delivered dont le lien
  est captured (_conversation_anchor_event_id). opened_event,
  continued_event, continued_without_boundary_signal, transitioned_event =>
  ancrée à response_event_id ; closed_terminal, no_cognitive_action,
  stale_delivery => aucune ancre. On ne remonte JAMAIS plus loin : une ancre
  est un état courant, pas « le dernier response_event_id non NULL ».
- input_context_event_id fige, à l'arrivée du tour, l'ancre de la
  conversation AVANT le tour ; response_context_event_id fige, sous le même
  verrou, l'event EXISTANT auquel la réponse assistant du tour est destinée
  (l'ancre pour un tour ancré ; sinon l'event open de l'AnalysisSession
  cible, ou NULL). C'est le seul candidat de continuation à l'ACK.
- answered_step (non persisté) : étape de l'event d'ancrage d'une
  contribution, dérivée de sa provenance (_event_step). La progression
  produit l'utilise pour le UserStatement et n'avance que si
  answered_step == current_step (core/decryptage_progress.py).
- Contribution : uniquement à l'event EXACT d'ancrage, jamais à « l'event
  open compatible » de l'AnalysisSession.
- Context switch (ticker demandé différent du ticker de l'AnalysisSession
  de l'ancre) : DETACH uniquement (context_switched). L'ancien event n'est ni fermé, ni
  enrichi : une navigation UI n'est pas une frontière cognitive. Le tour
  ENTIER est navigation, même s'il contient du contenu (« Revenons à LVMH.
  Pour le moat, je pense que… ») : aucun parsing, aucun découpage.
- Conversation stale : MÊME ticker que l'ancre mais état cognitif obsolète
  (ancre fermée, ou sa tentative n'est plus l'AnalysisSession active du
  ticker : terminée, abandonnée, remplacée par une autre tentative) =>
  StaleConversationContext (HTTP 409 stale_conversation_context), fail
  closed : aucune écriture, jamais de rattachement au nouvel event. Pré-
  vérification optionnelle avant Claude (precheck_conversation_context) ;
  la vérification qui fait foi est refaite sous verrou dans
  capture_user_turn.
- Late ACK : si la cible figée (response_context_event_id) a été fermée
  avant l'ACK, ou si, sans cible figée, un event est apparu depuis la
  capture, la réponse réellement rendue est delivered mais stale_delivery :
  aucun SupportTrace, aucun event, aucune mutation de l'event courant.
- support_refs_before conversation-scoped : seules les aides de CET event
  rendues (delivered + captured) dans la conversation du tour, en ordre
  chronologique ; SupportTrace reste unique (jamais dupliqué par
  conversation), la provenance est dérivée via AssistantDelivery +
  DecryptageCognitiveLink.
- Frontière cognitive : un event n'est fermé que par une vraie frontière de
  son AnalysisSession (marqueur suivant / swot_final que la progression
  produit a effectivement appliqué, c.-à-d. égal au current_step de la
  session à l'ACK) ; jamais par une navigation. Plus d'un event open
  compatible : fail closed (AmbiguousOpenDecryptageEvents).

Les liens / livraisons V1 (decryptage-cognitive-runtime-v1) restent lisibles
selon leur ancienne sémantique (event_closed_/event_abandoned_context_change,
context_exit_event_id) ; l'ACK d'une livraison V1 encore pending applique la
sémantique R1-C2 d'origine. Aucune conversion V1 -> V2, aucun backfill.

Transactions : ce module ne commit ni ne rollback jamais, n'ouvre aucune
Session, ne fait aucun appel externe et n'importe pas FastAPI. Il verrouille,
mute et flush ; la transaction appartient à api.py.

Ordre unique des verrous (aucun chemin inverse) :
AssistantDelivery -> DecryptageCognitiveLink -> AnalysisSession ->
CognitiveEvent -> SupportTrace (via le verrou de l'event parent, pris par
cognitive_capture). Dans /decryptage, la livraison est la ligne insérée par
claim_delivery et le lien une ligne neuve ; l'AnalysisSession active est
verrouillée, puis l'ancre est lue, puis les events open de cette session ET
l'ancre (même d'une autre AnalysisSession, context switch) sont verrouillés
en une instruction triée par id : le lien référence l'ancre par FK (verrou
KEY SHARE implicite), qui ne doit jamais être pris hors de cet ordre. Une
transaction ne verrouille qu'UNE AnalysisSession, toujours avant ses
events ; elle n'attend jamais une AnalysisSession en détenant un event.

Limites documentées :

- provenance intra-event uniquement (SupportTrace event-local) ;
- un tour dont la résolution du ticker, les données financières ou Claude
  échouent AVANT la livraison canonique n'est pas capturé en T2 ;
- un tour sans texte (déclencheur, reprise) n'est jamais une contribution ;
- un tour sans cible figée dont l'AnalysisSession voit apparaître un event
  avant son ACK devient stale_delivery (aucun rattachement), même si la
  réponse aurait pu y être pertinente.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select

from core import cognitive_capture
from core.decryptage_progress import DECRYPTAGE_STEP_MARKERS, FINAL_STEP, find_active_analysis_session
from core.models import AnalysisSession, AssistantDelivery, CognitiveEvent, DecryptageCognitiveLink, SupportTrace

logger = logging.getLogger(__name__)

COGNITIVE_RUNTIME_VERSION_V1 = "decryptage-cognitive-runtime-v1"
COGNITIVE_RUNTIME_VERSION_V2 = "decryptage-cognitive-runtime-v2"
# Runtime de TOUTE nouvelle livraison (R1-C4).
COGNITIVE_RUNTIME_VERSION = COGNITIVE_RUNTIME_VERSION_V2
CAPTURE_VERSION = COGNITIVE_RUNTIME_VERSION
SUPPORTED_RUNTIME_VERSIONS = (COGNITIVE_RUNTIME_VERSION_V1, COGNITIVE_RUNTIME_VERSION_V2)
EVENT_BUILDER_VERSION = "decryptage-event-builder-v1"
ADMISSION_VERSION = "decryptage-event-admission-v1"
EVENT_ORIGIN = "decryptage"
SURFACE = "decryptage"
SUPPORT_KIND = "assistant_response"
SEGMENTATION_ORDINAL = 1

RUNTIME_VERSION_KEY = "cognitive_runtime_version"
STEP_MARKER_KEY = "decryptage_step_marker"

AWAITING_DELIVERY = "awaiting_delivery"
CAPTURED = "captured"
DELIVERED = "delivered"

NO_OPEN_EVENT = "no_open_event"
NO_USER_CONTRIBUTION = "no_user_contribution"
CONTRIBUTION_APPENDED = "contribution_appended"
# V1 uniquement (historique, lisible, jamais écrit en V2).
EVENT_CLOSED_CONTEXT_CHANGE = "event_closed_context_change"
EVENT_ABANDONED_CONTEXT_CHANGE = "event_abandoned_context_change"
# V2 (R1-C4) : la conversation se détache de son ancre, laissée intacte.
CONTEXT_SWITCHED = "context_switched"
INPUT_ACTIONS = (NO_OPEN_EVENT, NO_USER_CONTRIBUTION, CONTRIBUTION_APPENDED, EVENT_CLOSED_CONTEXT_CHANGE,
                 EVENT_ABANDONED_CONTEXT_CHANGE, CONTEXT_SWITCHED)
# Tours ANCRÉS dans l'AnalysisSession du tour : leur réponse dépend de
# l'event de contexte (stale_delivery s'il est fermé avant l'ACK).
ANCHORED_INPUT_ACTIONS = (CONTRIBUTION_APPENDED, NO_USER_CONTRIBUTION)

OPENED_EVENT = "opened_event"
CONTINUED_EVENT = "continued_event"
CONTINUED_WITHOUT_BOUNDARY_SIGNAL = "continued_without_boundary_signal"
TRANSITIONED_EVENT = "transitioned_event"
CLOSED_TERMINAL = "closed_terminal"
NO_COGNITIVE_ACTION = "no_cognitive_action"
# V2 (R1-C4) : réponse rendue mais destinée à un event fermé entre-temps.
STALE_DELIVERY = "stale_delivery"
RESPONSE_ACTIONS = (OPENED_EVENT, CONTINUED_EVENT, CONTINUED_WITHOUT_BOUNDARY_SIGNAL, TRANSITIONED_EVENT,
                    CLOSED_TERMINAL, NO_COGNITIVE_ACTION, STALE_DELIVERY)
OPENING_ACTIONS = (OPENED_EVENT, TRANSITIONED_EVENT)
# Ancre conversationnelle après une livraison captured (R1-C4).
ANCHORING_RESPONSE_ACTIONS = (OPENED_EVENT, CONTINUED_EVENT, CONTINUED_WITHOUT_BOUNDARY_SIGNAL, TRANSITIONED_EVENT)
NON_ANCHORING_RESPONSE_ACTIONS = (CLOSED_TERMINAL, NO_COGNITIVE_ACTION, STALE_DELIVERY)

# Étapes qui ouvrent une tâche cognitive utilisateur. swot_final complète
# l'AnalysisSession mais n'est pas une nouvelle tâche : jamais d'event.
TASK_STEPS = tuple(step for step in DECRYPTAGE_STEP_MARKERS if step != FINAL_STEP)


class DecryptageCognitiveRuntimeError(Exception):
    """Erreur du runtime R1-C2 : toujours fail closed (rollback de la
    transaction appelante, aucune réparation silencieuse)."""


class CognitiveLinkInvariantError(DecryptageCognitiveRuntimeError):
    """Invariant livraison <-> lien cassé : livraison R1-C2 sans lien, lien
    sur une livraison legacy, couple d'états interdit, version inconnue ou
    incohérente, lien déjà présent à la création, event de référence
    incohérent."""


class AmbiguousOpenDecryptageEvents(DecryptageCognitiveRuntimeError):
    """Plusieurs CognitiveEvents Décrypter open compatibles : aucun choix
    heuristique, fail closed."""


class StaleConversationContext(DecryptageCognitiveRuntimeError):
    """R1-C4 : la conversation est ancrée à un event de l'AnalysisSession du
    tour qui n'est plus open (l'analyse a avancé dans une autre
    conversation). Fail closed (HTTP 409 stale_conversation_context) : aucune
    contribution, aucun rattachement au nouvel event, aucune écriture.
    expected_event_id : identifiant technique de l'ancre (jamais exposé au
    client)."""

    def __init__(self, expected_event_id):
        super().__init__(f"event d'ancrage {expected_event_id} n'est plus le contexte cognitif valide")
        self.expected_event_id = expected_event_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def runtime_private_metadata(step_marker: str | None) -> dict:
    """private_metadata d'une nouvelle AssistantDelivery Décrypter : marqueur
    d'étape privé (jamais visible, jamais une preuve) + opt-in explicite de
    la capture cognitive, toujours au runtime COURANT (V2). Une livraison
    sans cognitive_runtime_version est legacy (R1-C1) et n'est jamais
    capturée rétroactivement."""
    if step_marker is not None and step_marker not in DECRYPTAGE_STEP_MARKERS:
        raise DecryptageCognitiveRuntimeError("marqueur d'étape hors vocabulaire fermé")
    return {STEP_MARKER_KEY: step_marker, RUNTIME_VERSION_KEY: COGNITIVE_RUNTIME_VERSION}


def _runtime_version(delivery: AssistantDelivery) -> str | None:
    """None (legacy R1-C1), V1 ou V2 ; toute autre valeur : fail closed."""
    version = delivery.private_metadata.get(RUNTIME_VERSION_KEY)
    if version is not None and version not in SUPPORTED_RUNTIME_VERSIONS:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : cognitive_runtime_version inconnue")
    return version


def _step_marker(delivery: AssistantDelivery) -> str | None:
    """Marqueur privé de la livraison ; une valeur hors vocabulaire n'est
    jamais réparée (ni fuzzy-match, ni mapping) : traitée comme absente."""
    marker = delivery.private_metadata.get(STEP_MARKER_KEY)
    if marker is not None and marker not in DECRYPTAGE_STEP_MARKERS:
        logger.warning("[R1-C2] assistant_turn=%s anomaly=unknown_marker", delivery.id)
        return None
    return marker


# --------------------------------------------------------------------------
# Ancre conversationnelle (R1-C4)
# --------------------------------------------------------------------------

def _anchor_event_id(response_action: str, response_event_id):
    """Ancre courante après UNE livraison captured, fonction pure : l'event
    de la réponse pour une action d'ancrage, None pour closed_terminal /
    no_cognitive_action / stale_delivery. Toute autre combinaison est un
    invariant cassé (jamais devinée)."""
    if response_action in ANCHORING_RESPONSE_ACTIONS:
        if response_event_id is None:
            raise CognitiveLinkInvariantError(f"{response_action} sans response_event_id")
        return response_event_id
    if response_action in NON_ANCHORING_RESPONSE_ACTIONS:
        if response_event_id is not None:
            raise CognitiveLinkInvariantError(f"{response_action} avec response_event_id")
        return None
    raise CognitiveLinkInvariantError("response_action inconnue pour une ancre")


def _conversation_anchor_event_id(db, *, user_id: str, conversation_key: str):
    """Event d'ancrage courant de (user_id, conversation_key), ou None.

    Déterminé UNIQUEMENT par la DERNIÈRE livraison V2 de cette conversation
    à la fois delivered et captured (ordre de capture = ordre de rendu
    confirmé : captured_at, puis delivered_at, generated_at, id pour un ordre
    total déterministe). Si cette livraison ne porte pas d'ancre
    (closed_terminal, no_cognitive_action, stale_delivery) : None, sans
    jamais remonter à une livraison antérieure. Les livraisons V1 (sémantique
    R1-C2) et legacy ne définissent jamais l'ancre V2 (aucune conversion)."""
    row = db.execute(
        select(DecryptageCognitiveLink.response_action, DecryptageCognitiveLink.response_event_id)
        .join(AssistantDelivery, AssistantDelivery.id == DecryptageCognitiveLink.assistant_delivery_id)
        .where(AssistantDelivery.user_id == user_id,
               AssistantDelivery.conversation_key == conversation_key,
               AssistantDelivery.surface == SURFACE,
               AssistantDelivery.status == DELIVERED,
               DecryptageCognitiveLink.capture_state == CAPTURED,
               DecryptageCognitiveLink.capture_version == COGNITIVE_RUNTIME_VERSION_V2)
        .order_by(DecryptageCognitiveLink.captured_at.desc(), AssistantDelivery.delivered_at.desc(),
                  AssistantDelivery.generated_at.desc(), AssistantDelivery.id.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    return _anchor_event_id(row.response_action, row.response_event_id)


def _conversation_support_refs(db, *, event_id, user_id: str, conversation_key: str) -> tuple:
    """Aides de CET event réellement rendues dans CETTE conversation :
    SupportTrace rattachés par decryptage_cognitive_links.support_trace_id à
    une livraison delivered (lien captured) de (user_id, conversation_key),
    ORDER BY sequence_no. Appelée sous le verrou de l'event (aucune aide
    concurrente ne peut s'insérer) : toutes ont été créées avant la
    contribution. La provenance est factuelle (V1 comme V2) : une aide
    rendue dans une autre conversation n'est jamais héritée."""
    return tuple(db.execute(
        select(SupportTrace.id)
        .join(DecryptageCognitiveLink, DecryptageCognitiveLink.support_trace_id == SupportTrace.id)
        .join(AssistantDelivery, AssistantDelivery.id == DecryptageCognitiveLink.assistant_delivery_id)
        .where(SupportTrace.cognitive_event_id == event_id,
               AssistantDelivery.user_id == user_id,
               AssistantDelivery.conversation_key == conversation_key,
               AssistantDelivery.surface == SURFACE,
               AssistantDelivery.status == DELIVERED,
               DecryptageCognitiveLink.capture_state == CAPTURED)
        .order_by(SupportTrace.sequence_no.asc())
    ).scalars())


# --------------------------------------------------------------------------
# Lectures verrouillées
# --------------------------------------------------------------------------

def _lock_link(db, delivery_id) -> DecryptageCognitiveLink | None:
    return db.execute(
        select(DecryptageCognitiveLink)
        .where(DecryptageCognitiveLink.assistant_delivery_id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _lock_active_analysis_session(db, *, user_id: str, ticker: str) -> AnalysisSession | None:
    """AnalysisSession in_progress de user+ticker, verrouillée, avec le même
    ordre déterministe que find_active_analysis_session (la progression
    produit la relira ensuite, sous ce verrou). Lecture de l'état AVANT la
    mutation produit du tour."""
    sessions = db.execute(
        select(AnalysisSession)
        .where(AnalysisSession.user_id == user_id, AnalysisSession.ticker == ticker,
               AnalysisSession.status == "in_progress")
        .order_by(AnalysisSession.started_at.desc(), AnalysisSession.updated_at.desc(), AnalysisSession.id.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()
    return sessions[0] if sessions else None


def _lock_analysis_session(db, analysis_session_id) -> AnalysisSession:
    session = db.execute(
        select(AnalysisSession)
        .where(AnalysisSession.id == analysis_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if session is None:
        raise CognitiveLinkInvariantError(f"analysis_session {analysis_session_id} introuvable")
    return session


def _lock_open_events(db, *, user_id: str, analysis_session_id) -> list[CognitiveEvent]:
    """Events Décrypter open de ce runtime pour user_id dans UNE
    AnalysisSession, verrouillés dans l'ordre des id. En READ COMMITTED, un
    event fermé par une transaction concurrente pendant l'attente du verrou
    est réévalué et exclu : jamais d'état obsolète."""
    return list(db.execute(
        select(CognitiveEvent)
        .where(CognitiveEvent.user_id == user_id,
               CognitiveEvent.event_origin == EVENT_ORIGIN,
               CognitiveEvent.event_builder_version == EVENT_BUILDER_VERSION,
               CognitiveEvent.status == cognitive_capture.OPEN,
               CognitiveEvent.analysis_session_id == analysis_session_id)
        .order_by(CognitiveEvent.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars())


def _lock_turn_events(db, *, user_id: str, analysis_session_id, anchor_id) -> list[CognitiveEvent]:
    """Côté entrée (R1-C4) : verrouille en UNE instruction, triée par id, les
    events Décrypter open de ce runtime dans l'AnalysisSession active ET
    l'event d'ancrage de la conversation (même fermé, même d'une autre
    AnalysisSession). Les verrous sont pris dans l'ordre du tri (LockRows
    au-dessus du Sort) : deux tours qui touchent les mêmes events les
    verrouillent dans le même ordre, sans interblocage. READ COMMITTED : un
    event fermé pendant l'attente est relu dans son état commité (il ne
    reste sélectionné que s'il est l'ancre, avec son statut réel)."""
    criteria = []
    if analysis_session_id is not None:
        criteria.append((CognitiveEvent.status == cognitive_capture.OPEN)
                        & (CognitiveEvent.analysis_session_id == analysis_session_id))
    if anchor_id is not None:
        criteria.append(CognitiveEvent.id == anchor_id)
    if not criteria:
        return []
    return list(db.execute(
        select(CognitiveEvent)
        .where(CognitiveEvent.user_id == user_id,
               CognitiveEvent.event_origin == EVENT_ORIGIN,
               CognitiveEvent.event_builder_version == EVENT_BUILDER_VERSION,
               or_(*criteria))
        .order_by(CognitiveEvent.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars())


def _event_session_id(db, event_id):
    """analysis_session_id (immuable) d'un event Décrypter de ce runtime,
    lecture simple sans verrou."""
    session_id = db.execute(
        select(CognitiveEvent.analysis_session_id)
        .where(CognitiveEvent.id == event_id, CognitiveEvent.event_origin == EVENT_ORIGIN,
               CognitiveEvent.event_builder_version == EVENT_BUILDER_VERSION)
    ).scalar_one_or_none()
    if session_id is None:
        raise CognitiveLinkInvariantError(f"event {event_id} : event Décrypter introuvable ou sans AnalysisSession")
    return session_id


def _session_ticker(db, analysis_session_id) -> str:
    """Ticker (immuable) d'une AnalysisSession, lecture simple sans verrou :
    une transaction ne verrouille jamais qu'UNE AnalysisSession (la cible)."""
    ticker = db.execute(
        select(AnalysisSession.ticker).where(AnalysisSession.id == analysis_session_id)
    ).scalar_one_or_none()
    if ticker is None:
        raise CognitiveLinkInvariantError(f"analysis_session {analysis_session_id} introuvable")
    return ticker


def _event_step(db, event: CognitiveEvent) -> str:
    """Étape produit d'un event ouvert par ce runtime : marqueur privé de la
    livraison qui l'a ouvert (via son lien opened/transitioned). Jamais
    stockée dans l'event (ni task_kind, ni stimulus)."""
    marker = db.execute(
        select(AssistantDelivery.private_metadata[STEP_MARKER_KEY].astext)
        .join(DecryptageCognitiveLink,
              DecryptageCognitiveLink.assistant_delivery_id == AssistantDelivery.id)
        .where(DecryptageCognitiveLink.response_event_id == event.id,
               DecryptageCognitiveLink.response_action.in_(OPENING_ACTIONS))
    ).scalar_one_or_none()
    if marker not in TASK_STEPS:
        raise CognitiveLinkInvariantError(f"event {event.id} : livraison d'ouverture introuvable ou incohérente")
    return marker


# --------------------------------------------------------------------------
# Mutations T2 (primitives de cognitive_capture uniquement)
# --------------------------------------------------------------------------

def _close_event(db, event: CognitiveEvent) -> str:
    """Ferme un event sur une vraie frontière cognitive : finalized s'il
    porte au moins une contribution utilisateur, abandoned s'il est vide (un
    event finalized vide serait un faux candidat à l'évaluation)."""
    if event.user_work_snapshot:
        cognitive_capture.finalize_event(db, event_id=event.id)
        return cognitive_capture.FINALIZED
    cognitive_capture.abandon_event(db, event_id=event.id)
    return cognitive_capture.ABANDONED


def _visible_stimulus(response_payload: dict) -> dict:
    """Stimulus factuel minimal : uniquement ce que le frontend Décrypter
    affiche de la réponse (texte visible, prix + devise du bandeau si un
    prix est affiché). Jamais de prompt, marqueur, private_metadata,
    diagnostic ni inférence."""
    stimulus = {"visible_content": response_payload["analysis"]}
    if response_payload.get("price") is not None:
        stimulus["price"] = response_payload["price"]
        stimulus["currency"] = response_payload.get("currency")
    return stimulus


def _open_event(db, delivery: AssistantDelivery) -> CognitiveEvent:
    event = cognitive_capture.open_event_idempotent(
        db,
        user_id=delivery.user_id,
        conversation_key=delivery.conversation_key,
        source_turn_refs=(delivery.id,),
        segmentation_ordinal=SEGMENTATION_ORDINAL,
        event_origin=EVENT_ORIGIN,
        stimulus_snapshot=_visible_stimulus(delivery.response_payload),
        event_builder_version=EVENT_BUILDER_VERSION,
        admission_version=ADMISSION_VERSION,
        task_kind=None,
        analysis_session_id=delivery.analysis_session_id,
    )
    if event.status != cognitive_capture.OPEN:
        raise CognitiveLinkInvariantError(f"event {event.id} déjà fermé pour l'assistant turn {delivery.id}")
    return event


def _support_trace(db, event: CognitiveEvent, delivery: AssistantDelivery):
    """La réponse réellement rendue, et rien d'autre (ni assistant_turn_id,
    ni marqueur, ni private_metadata) : le lien porte la relation (et donc
    la conversation où elle a été rendue)."""
    return cognitive_capture.add_support_trace(
        db, event_id=event.id, support_kind=SUPPORT_KIND,
        support_payload={"visible_content": delivery.response_payload["analysis"]},
    )


def _append_contribution(db, *, event: CognitiveEvent, delivery: AssistantDelivery, user_text: str) -> None:
    """Contribution du tour à l'event d'ancrage, avec les seules aides
    rendues dans la conversation du tour (décision du runtime, vérifiée par
    la primitive : aides persistées de CET event)."""
    support_refs = _conversation_support_refs(db, event_id=event.id, user_id=delivery.user_id,
                                              conversation_key=delivery.conversation_key)
    cognitive_capture.append_user_contribution(
        db, event_id=event.id, contribution_id=delivery.source_user_turn_id,
        source_turn_ref=delivery.source_user_turn_id, surface=SURFACE,
        session_ref=delivery.conversation_key, text_excerpt=user_text,
        support_refs_before=support_refs,
    )
    logger.info("[R1-C4] event=%s support_refs conversation=%s count=%d", event.id, delivery.conversation_key,
                len(support_refs))


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def precheck_conversation_context(db, *, user_id: str, conversation_key: str, ticker: str) -> None:
    """Pré-vérification R1-C4, AVANT les appels externes (Claude) : simple
    optimisation pour éviter un appel inutile, lecture seule et sans verrou.
    Même sémantique que capture_user_turn : ticker demandé différent de
    celui de l'ancre => navigation possible (jamais stale) ; MÊME ticker et
    état cognitif obsolète (ancre fermée, ou sa tentative n'est plus
    l'AnalysisSession active du ticker) => StaleConversationContext. Ne fait
    jamais foi : capture_user_turn refait la vérification sous verrou dans
    la transaction gagnante (une course entre ce contrôle et le retour de
    Claude reste fail closed). Aucune écriture."""
    anchor_id = _conversation_anchor_event_id(db, user_id=user_id, conversation_key=conversation_key)
    if anchor_id is None:
        return
    anchor = db.execute(
        select(CognitiveEvent.analysis_session_id, CognitiveEvent.status).where(CognitiveEvent.id == anchor_id)
    ).one()
    if anchor.analysis_session_id is None or _session_ticker(db, anchor.analysis_session_id) != ticker:
        return
    active = find_active_analysis_session(db, user_id, ticker)
    if (anchor.status != cognitive_capture.OPEN or active is None or active.id != anchor.analysis_session_id):
        logger.warning("[R1-C4] assistant_turn=none stale_conversation_context expected_event=%s phase=precheck",
                       anchor_id)
        raise StaleConversationContext(anchor_id)


def capture_user_turn(db, *, delivery: AssistantDelivery, ticker: str, user_text: str) -> DecryptageCognitiveLink:
    """Côté entrée (runtime V2), transaction /decryptage du worker GAGNANT
    uniquement (livraison créée par CET appel, avant la progression produit).

    1. AnalysisSession active du ticker canonique (avant mutation),
       verrouillée.
    2. Ancre de la conversation du tour (_conversation_anchor_event_id), lue
       APRÈS ce verrou (un ACK concurrent de la même AnalysisSession est
       sérialisé dessus).
    3. Verrouillage, en UNE instruction triée par id, des events Décrypter
       open de l'AnalysisSession active ET de l'ancre (quel que soit son
       statut / sa session) : ordre global déterministe AnalysisSession ->
       events par id. Le lien référence l'ancre (FK, verrou KEY SHARE
       implicite) : la verrouiller ici, dans le même ordre que les autres
       events, évite tout ordre inverse (deux conversations qui changent de
       ticker « en croix »). Plus d'un event open compatible =>
       AmbiguousOpenDecryptageEvents.
    4. Classement (input_context_event_id = ancre AVANT le tour) :
       - aucune ancre : no_open_event (même avec du texte : rien n'a été
         rendu dans cette conversation à quoi répondre) ;
       - ticker demandé DIFFÉRENT du ticker de l'AnalysisSession de l'ancre :
         context_switched (vrai changement de contexte produit) ; l'ancre
         n'est ni fermée ni enrichie (seulement verrouillée le temps de
         cette courte transaction) ; le tour entier est navigation (aucun
         parsing) ;
       - MÊME ticker mais état cognitif obsolète, constaté sous verrou :
         ancre fermée, ou sa tentative n'est plus l'AnalysisSession active
         du ticker (terminée, abandonnée, ou une autre tentative est active)
         : StaleConversationContext, rien n'est écrit (l'appelant annule
         toute la transaction, livraison comprise) ;
       - ancre open + texte : contribution à CET event exact
         (contribution_id = source_turn_ref = client_turn_id, session_ref =
         conversation du tour, support_refs_before = aides rendues dans
         cette conversation) ;
       - ancre open sans texte (déclencheur / reprise) : no_user_contribution.
    5. response_context_event_id, figé ICI sous verrou : event existant
       auquel la réponse assistant du tour est destinée. Tour ancré
       (contribution / reprise) : l'ancre ; context_switched / no_open_event
       : l'event open de l'AnalysisSession cible s'il existe, sinon NULL.
       À l'ACK, c'est le SEUL candidat de continuation : aucun event apparu
       après cette capture ne peut devenir rétroactivement la cible.
    6. Ligne de lien awaiting_delivery (context_exit_event_id toujours NULL
       en V2 : aucune fermeture sur navigation).

    Mute et flush ; jamais de commit."""
    version = _runtime_version(delivery)
    if version is None:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : livraison sans opt-in R1-C2")
    if version != COGNITIVE_RUNTIME_VERSION:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : runtime {version} non capturable en V2")
    if delivery.status != "pending":
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : livraison déjà {delivery.status}")
    if _lock_link(db, delivery.id) is not None:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : lien déjà présent")

    active = _lock_active_analysis_session(db, user_id=delivery.user_id, ticker=ticker)
    anchor_id = _conversation_anchor_event_id(db, user_id=delivery.user_id,
                                              conversation_key=delivery.conversation_key)
    locked = _lock_turn_events(db, user_id=delivery.user_id, analysis_session_id=active.id if active else None,
                               anchor_id=anchor_id)
    compatible = [e for e in locked if active is not None and e.analysis_session_id == active.id
                  and e.status == cognitive_capture.OPEN]
    if len(compatible) > 1:
        raise AmbiguousOpenDecryptageEvents(
            f"assistant turn {delivery.id} : {len(compatible)} events open compatibles")

    anchor = None
    if anchor_id is not None:
        anchor = next((e for e in locked if e.id == anchor_id), None)
        if anchor is None or anchor.analysis_session_id is None:
            raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : ancre {anchor_id} incohérente")

    input_event_id = None
    if anchor is None:
        input_action = NO_OPEN_EVENT
    elif _session_ticker(db, anchor.analysis_session_id) != ticker:
        # Vrai changement de contexte produit (ticker) : détachement
        # seulement, l'ancre reste telle quelle (elle peut rester le
        # contexte d'autres conversations).
        input_action = CONTEXT_SWITCHED
        logger.info("[R1-C4] assistant_turn=%s input_action=context_switched from_event=%s", delivery.id,
                    anchor_id)
    elif anchor.status != cognitive_capture.OPEN or active is None or anchor.analysis_session_id != active.id:
        # Même ticker, état cognitif obsolète (constaté sous verrou) : une
        # autre conversation a fait avancer / terminé l'analyse, ou une autre
        # tentative du ticker est active. Jamais de rattachement ailleurs.
        logger.warning("[R1-C4] assistant_turn=%s stale_conversation_context expected_event=%s", delivery.id,
                       anchor_id)
        raise StaleConversationContext(anchor_id)
    elif user_text:
        _append_contribution(db, event=anchor, delivery=delivery, user_text=user_text)
        input_action, input_event_id = CONTRIBUTION_APPENDED, anchor.id
    else:
        input_action = NO_USER_CONTRIBUTION
    # Cible figée de la réponse : verrouillée ci-dessus (compatible), FK prise
    # sur une ligne déjà détenue (aucun verrou hors ordre).
    if input_action in ANCHORED_INPUT_ACTIONS:
        response_context_id = anchor.id
    else:
        response_context_id = compatible[0].id if compatible else None

    link = DecryptageCognitiveLink(
        assistant_delivery_id=delivery.id,
        capture_version=CAPTURE_VERSION,
        input_action=input_action,
        input_event_id=input_event_id,
        input_context_event_id=anchor_id,
        response_context_event_id=response_context_id,
        context_exit_event_id=None,
        capture_state=AWAITING_DELIVERY,
        response_action=None,
        response_event_id=None,
        support_trace_id=None,
        created_at=_utcnow(),
        captured_at=None,
    )
    db.add(link)
    db.flush()
    logger.info("[R1-C2] assistant_turn=%s input_action=%s event=%s context_event=%s response_context=%s",
                delivery.id, input_action, input_event_id, anchor_id, response_context_id)
    return link


def answered_step(db, *, link: DecryptageCognitiveLink) -> str | None:
    """Étape cognitive à laquelle la contribution du tour répond réellement
    (R1-C4) : étape de l'event d'ancrage (input_context_event_id), dérivée de
    la provenance de l'event (marqueur de la livraison qui l'a ouvert,
    _event_step). Jamais déduite du current_step produit, du marqueur
    entrant ni du texte. None si le tour n'est pas une contribution."""
    if link.input_action != CONTRIBUTION_APPENDED:
        return None
    if link.input_event_id is None or link.input_event_id != link.input_context_event_id:
        raise CognitiveLinkInvariantError(f"assistant turn {link.assistant_delivery_id} : contribution sans ancre")
    event = db.get(CognitiveEvent, link.input_event_id)
    if event is None:
        raise CognitiveLinkInvariantError(f"event {link.input_event_id} introuvable")
    return _event_step(db, event)


def capture_delivered_response(db, *, delivery: AssistantDelivery) -> DecryptageCognitiveLink | None:
    """Côté réponse, transaction ACK, sous le verrou de la livraison
    (lock_delivery_for_ack) et AVANT acknowledge_delivery.

    - livraison legacy (sans cognitive_runtime_version) : aucun lien
      attendu, None (ACK historique inchangé) ; un lien présent = invariant
      cassé ;
    - livraison V1 / V2 sans lien : CognitiveLinkInvariantError ;
    - capture_version différente du runtime de la livraison : invariant
      cassé (aucune conversion V1 -> V2) ;
    - delivered + captured : no-op idempotent (retry ACK) ;
    - pending + awaiting_delivery : capture selon le runtime de la livraison
      (V1 : sémantique R1-C2 d'origine ; V2 : R1-C4) ;
    - pending + captured, delivered + awaiting_delivery : invariant cassé.

    Mute et flush ; jamais de commit. Toute exception doit annuler la
    transaction ACK entière."""
    version = _runtime_version(delivery)
    link = _lock_link(db, delivery.id)
    if version is None:
        if link is not None:
            raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : lien sur une livraison legacy")
        return None
    if link is None:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : livraison R1-C2 sans lien cognitif")
    if link.capture_version != version:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : capture_version incohérente")
    if delivery.status == "delivered" and link.capture_state == CAPTURED:
        return link
    if not (delivery.status == "pending" and link.capture_state == AWAITING_DELIVERY):
        raise CognitiveLinkInvariantError(
            f"assistant turn {delivery.id} : couple interdit {delivery.status}/{link.capture_state}")
    if version == COGNITIVE_RUNTIME_VERSION_V1:
        return _capture_response_v1(db, delivery=delivery, link=link)
    return _capture_response_v2(db, delivery=delivery, link=link)


def _record_response(db, *, delivery, link, action, response_event, trace, marker, from_step):
    link.response_action = action
    link.response_event_id = response_event.id if response_event is not None else None
    link.support_trace_id = trace.id if trace is not None else None
    link.captured_at = _utcnow()
    link.capture_state = CAPTURED
    db.flush()
    logger.info("[R1-C2] assistant_turn=%s action=%s marker=%s from=%s event=%s support_trace=%s",
                delivery.id, action, marker or "absent", from_step, link.response_event_id, link.support_trace_id)
    return link


def _capture_response_v2(db, *, delivery: AssistantDelivery, link: DecryptageCognitiveLink):
    """Capture R1-C4 de la réponse réellement delivered.

    Cible de la réponse : response_context_event_id, figé sous verrou à la
    capture côté entrée, quel que soit input_action. Jamais remplacé par
    « l'event open actuel ».

    Sous verrous AnalysisSession -> events open (plus d'un : fail closed) :

    - cible figée qui n'est plus open : stale_delivery (aucun SupportTrace,
      aucun event ouvert ni fermé, l'event courant intact) ;
    - aucune cible figée mais un event open est apparu depuis la capture :
      stale_delivery également (jamais de continuation opportuniste, jamais
      un second event open dans l'AnalysisSession) ;
    - aucune cible figée et aucun event open : marqueur de tâche que la
      progression produit a effectivement appliqué (= current_step de la
      session) -> opened_event ; sinon no_cognitive_action ;
    - marqueur absent / inconnu / non appliqué -> SupportTrace,
      continued_without_boundary_signal ;
    - même marqueur -> SupportTrace, continued_event ;
    - rétrograde -> SupportTrace, continued_without_boundary_signal ;
    - marqueur suivant / saut / swot_final : frontière cognitive SEULEMENT si
      la progression produit l'a appliqué (marqueur = current_step de la
      session, ce qui exige une vraie contribution, R1-C4) : transition
      (ancien event finalized / abandoned, nouvel event dont la réponse est
      le stimulus) ou fermeture terminale (aucun event swot_final) ; sinon
      continuation sans frontière (anomalie boundary_not_applied)."""
    marker = _step_marker(delivery)
    boundary = marker if delivery.analysis_session_id is not None else None
    if marker is not None and boundary is None:
        logger.info("[R1-C2] assistant_turn=%s marker=%s without_product_progress", delivery.id, marker)

    expected_id = link.response_context_event_id
    if link.input_action in ANCHORED_INPUT_ACTIONS and (expected_id is None
                                                        or expected_id != link.input_context_event_id):
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : tour ancré sans cible de réponse")
    context_session_id = delivery.analysis_session_id
    if expected_id is not None:
        expected_session_id = _event_session_id(db, expected_id)
        if context_session_id is not None and expected_session_id != context_session_id:
            raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : AnalysisSession incohérente")
        context_session_id = expected_session_id

    session = reference = None
    if context_session_id is not None:
        session = _lock_analysis_session(db, context_session_id)
        candidates = _lock_open_events(db, user_id=delivery.user_id, analysis_session_id=context_session_id)
        if len(candidates) > 1:
            raise AmbiguousOpenDecryptageEvents(
                f"assistant turn {delivery.id} : {len(candidates)} events open compatibles")
        reference = candidates[0] if candidates else None

    if (expected_id is not None and (reference is None or reference.id != expected_id)) or (
            expected_id is None and reference is not None):
        # La réponse a réellement été rendue (delivered) mais sa cible figée
        # a été fermée, ou un event est apparu après la capture : jamais
        # rattachée à l'event courant.
        logger.warning("[R1-C4] assistant_turn=%s action=stale_delivery expected_event=%s current_event=%s",
                       delivery.id, expected_id, reference.id if reference is not None else None)
        return _record_response(db, delivery=delivery, link=link, action=STALE_DELIVERY, response_event=None,
                                trace=None, marker=marker, from_step=None)

    applied = boundary is not None and session is not None and session.current_step == boundary
    response_event = trace = from_step = None
    if reference is None:
        if boundary in TASK_STEPS and applied:
            response_event = _open_event(db, delivery)
            action = OPENED_EVENT
        else:
            if boundary in TASK_STEPS:
                logger.warning("[R1-C4] assistant_turn=%s anomaly=boundary_not_applied marker=%s current_step=%s",
                               delivery.id, boundary, session.current_step)
            action = NO_COGNITIVE_ACTION
    else:
        from_step = _event_step(db, reference)
        if boundary is None:
            action = CONTINUED_WITHOUT_BOUNDARY_SIGNAL
        elif boundary == from_step:
            action = CONTINUED_EVENT
        elif DECRYPTAGE_STEP_MARKERS.index(boundary) < DECRYPTAGE_STEP_MARKERS.index(from_step):
            logger.warning("[R1-C2] assistant_turn=%s anomaly=retrograde_marker from=%s to=%s event=%s",
                           delivery.id, from_step, boundary, reference.id)
            action = CONTINUED_WITHOUT_BOUNDARY_SIGNAL
        elif not applied:
            # Claude seul ne crée jamais de frontière : la progression produit
            # a refusé ce marqueur (aucune contribution réelle).
            logger.warning("[R1-C4] assistant_turn=%s anomaly=boundary_not_applied marker=%s current_step=%s "
                           "event=%s", delivery.id, boundary, session.current_step, reference.id)
            action = CONTINUED_WITHOUT_BOUNDARY_SIGNAL
        elif boundary == FINAL_STEP:
            _close_event(db, reference)
            action = CLOSED_TERMINAL
        else:
            if DECRYPTAGE_STEP_MARKERS.index(boundary) != DECRYPTAGE_STEP_MARKERS.index(from_step) + 1:
                logger.info("[R1-C2] assistant_turn=%s anomaly=step_skip from=%s to=%s",
                            delivery.id, from_step, boundary)
            _close_event(db, reference)
            response_event = _open_event(db, delivery)
            action = TRANSITIONED_EVENT
        if action in (CONTINUED_EVENT, CONTINUED_WITHOUT_BOUNDARY_SIGNAL):
            trace = _support_trace(db, reference, delivery)
            response_event = reference
    return _record_response(db, delivery=delivery, link=link, action=action, response_event=response_event,
                            trace=trace, marker=marker, from_step=from_step)


def _capture_response_v1(db, *, delivery: AssistantDelivery, link: DecryptageCognitiveLink):
    """ACK d'une livraison V1 encore pending : sémantique R1-C2 d'origine,
    inchangée (event de référence = unique event open compatible de
    l'AnalysisSession ; aucune notion d'ancre ni de stale_delivery)."""
    marker = _step_marker(delivery)
    boundary = marker if delivery.analysis_session_id is not None else None
    if marker is not None and boundary is None:
        logger.info("[R1-C2] assistant_turn=%s marker=%s without_product_progress", delivery.id, marker)

    context_session_id = delivery.analysis_session_id
    if link.input_event_id is not None:
        input_session_id = db.execute(
            select(CognitiveEvent.analysis_session_id).where(CognitiveEvent.id == link.input_event_id)
        ).scalar_one()
        if context_session_id is not None and input_session_id != context_session_id:
            raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : AnalysisSession incohérente")
        context_session_id = input_session_id

    reference = None
    if context_session_id is not None:
        _lock_analysis_session(db, context_session_id)
        candidates = _lock_open_events(db, user_id=delivery.user_id, analysis_session_id=context_session_id)
        if len(candidates) > 1:
            raise AmbiguousOpenDecryptageEvents(
                f"assistant turn {delivery.id} : {len(candidates)} events open compatibles")
        reference = candidates[0] if candidates else None
    if link.input_event_id is not None and (reference is None or reference.id != link.input_event_id):
        logger.warning("[R1-C2] assistant_turn=%s anomaly=input_event_no_longer_open event=%s",
                       delivery.id, link.input_event_id)

    response_event = trace = None
    from_step = None
    if reference is None:
        if boundary in TASK_STEPS:
            response_event = _open_event(db, delivery)
            action = OPENED_EVENT
        else:
            action = NO_COGNITIVE_ACTION
    else:
        from_step = _event_step(db, reference)
        if boundary is None:
            action = CONTINUED_WITHOUT_BOUNDARY_SIGNAL
        elif boundary == from_step:
            action = CONTINUED_EVENT
        elif boundary == FINAL_STEP:
            _close_event(db, reference)
            action = CLOSED_TERMINAL
        elif DECRYPTAGE_STEP_MARKERS.index(boundary) > DECRYPTAGE_STEP_MARKERS.index(from_step):
            if DECRYPTAGE_STEP_MARKERS.index(boundary) != DECRYPTAGE_STEP_MARKERS.index(from_step) + 1:
                logger.info("[R1-C2] assistant_turn=%s anomaly=step_skip from=%s to=%s",
                            delivery.id, from_step, boundary)
            _close_event(db, reference)
            response_event = _open_event(db, delivery)
            action = TRANSITIONED_EVENT
        else:
            logger.warning("[R1-C2] assistant_turn=%s anomaly=retrograde_marker from=%s to=%s event=%s",
                           delivery.id, from_step, boundary, reference.id)
            action = CONTINUED_WITHOUT_BOUNDARY_SIGNAL
        if action in (CONTINUED_EVENT, CONTINUED_WITHOUT_BOUNDARY_SIGNAL):
            trace = _support_trace(db, reference, delivery)
            response_event = reference
    return _record_response(db, delivery=delivery, link=link, action=action, response_event=response_event,
                            trace=trace, marker=marker, from_step=from_step)
