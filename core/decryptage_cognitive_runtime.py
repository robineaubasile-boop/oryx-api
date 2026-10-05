"""Orchestration runtime de la capture cognitive T2 sur Décrypter (R1-C2).

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
- ce module : QUAND ouvrir, continuer, fermer un CognitiveEvent Décrypter
  et quoi y rattacher, tracé dans decryptage_cognitive_links.

Ce module ne fait AUCUN T3+ : ni évaluation, ni PedagogicalObservation, ni
taxonomie / capacités, ni longitudinal, ni inférence, ni Step 6. Il
n'attribue ni compétence (C1-C12), ni stade, ni force de preuve, ni niveau
d'aide pédagogique, ni polarité, ni justesse, et n'interprète jamais une
réponse. task_kind reste NULL : le marqueur d'étape produit n'est ni un
task_kind ni une compétence.

Doctrine (à ne pas confondre) :

- DecryptageCognitiveLink n'est pas une preuve : c'est de la provenance ;
- CognitiveEvent n'est pas un message : un event couvre plusieurs tours ;
- delivered (ACK) signifie « inséré dans l'interface », jamais lu / compris ;
- la conversation_key d'origine d'un event n'est pas une frontière
  cognitive : une reprise dans une nouvelle conversation peut continuer le
  même event (session_ref de la contribution = conversation du tour) ;
- same marker = continuation : convention de segmentation V1 faute de
  frontière plus fine, pas une assertion pédagogique ;
- frontière ambiguë (marqueur absent, inconnu ou rétrograde) : on conserve
  l'event open plutôt que de fragmenter ; la réponse visible devient un
  SupportTrace (« ce contenu a été disponible avant une future
  contribution », pas « c'était une aide substantielle »).

Le texte utilisateur d'un tour répond à l'étape PRÉCÉDENTE ; le marqueur de
la réponse indique l'étape que la réponse ouvre. D'où deux moments :

1. capture_user_turn, dans la transaction /decryptage du worker gagnant
   (après claim_delivery, AVANT la progression produit) : sort de la
   production utilisateur (contribution, changement de contexte) et
   création de la ligne de lien en awaiting_delivery ;
2. capture_delivered_response, dans la transaction ACK, AVANT
   acknowledge_delivery : lifecycle de l'event selon le marqueur de la
   réponse réellement delivered, SupportTrace éventuel, lien -> captured.
   Tout échec annule l'ACK (la livraison reste pending, le lien
   awaiting_delivery, aucune mutation partielle).

Compatibilité d'un event open (V1) : même user_id, event_origin decryptage,
politique de ce module (event_builder_version), status open, même
analysis_session_id que l'AnalysisSession active du ticker canonique du
tour. Jamais une recherche par conversation_key. Plusieurs events open
compatibles : fail closed (AmbiguousOpenDecryptageEvents), jamais de choix
« le plus récent » ni « le premier ».

Transactions : ce module ne commit ni ne rollback jamais, n'ouvre aucune
Session, ne fait aucun appel externe et n'importe pas FastAPI. Il verrouille,
mute et flush ; la transaction appartient à api.py.

Ordre unique des verrous (aucun chemin inverse) :
AssistantDelivery -> DecryptageCognitiveLink -> AnalysisSession ->
CognitiveEvent -> SupportTrace (via le verrou de l'event parent, pris par
cognitive_capture). Dans /decryptage, la livraison est la ligne insérée par
claim_delivery et le lien une ligne neuve (invisible aux autres
transactions jusqu'au commit) ; l'AnalysisSession active est verrouillée
AVANT les events, puis mise à jour par la progression produit (déjà
verrouillée).

Limites V1 documentées :

- provenance intra-event uniquement : SupportTrace est event-local et
  support_refs_before ne voit que les aides du même event. La provenance
  cognitive transversale (entre events / surfaces) reste un chantier
  ultérieur, à résoudre avant de considérer T3 fiable sur l'autonomie
  inter-event ;
- un tour dont la résolution du ticker, les données financières ou Claude
  échouent AVANT la livraison canonique n'est pas capturé en T2 (absence de
  capture != contradiction pédagogique) ;
- un tour sans texte (déclencheur, reprise) n'est jamais une contribution.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from core import cognitive_capture
from core.decryptage_progress import DECRYPTAGE_STEP_MARKERS, FINAL_STEP
from core.models import AnalysisSession, AssistantDelivery, CognitiveEvent, DecryptageCognitiveLink

logger = logging.getLogger(__name__)

COGNITIVE_RUNTIME_VERSION = "decryptage-cognitive-runtime-v1"
CAPTURE_VERSION = COGNITIVE_RUNTIME_VERSION
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

NO_OPEN_EVENT = "no_open_event"
NO_USER_CONTRIBUTION = "no_user_contribution"
CONTRIBUTION_APPENDED = "contribution_appended"
EVENT_CLOSED_CONTEXT_CHANGE = "event_closed_context_change"
EVENT_ABANDONED_CONTEXT_CHANGE = "event_abandoned_context_change"
INPUT_ACTIONS = (NO_OPEN_EVENT, NO_USER_CONTRIBUTION, CONTRIBUTION_APPENDED, EVENT_CLOSED_CONTEXT_CHANGE,
                 EVENT_ABANDONED_CONTEXT_CHANGE)

OPENED_EVENT = "opened_event"
CONTINUED_EVENT = "continued_event"
CONTINUED_WITHOUT_BOUNDARY_SIGNAL = "continued_without_boundary_signal"
TRANSITIONED_EVENT = "transitioned_event"
CLOSED_TERMINAL = "closed_terminal"
NO_COGNITIVE_ACTION = "no_cognitive_action"
RESPONSE_ACTIONS = (OPENED_EVENT, CONTINUED_EVENT, CONTINUED_WITHOUT_BOUNDARY_SIGNAL, TRANSITIONED_EVENT,
                    CLOSED_TERMINAL, NO_COGNITIVE_ACTION)
OPENING_ACTIONS = (OPENED_EVENT, TRANSITIONED_EVENT)

# Étapes qui ouvrent une tâche cognitive utilisateur. swot_final complète
# l'AnalysisSession mais n'est pas une nouvelle tâche : jamais d'event.
TASK_STEPS = tuple(step for step in DECRYPTAGE_STEP_MARKERS if step != FINAL_STEP)


class DecryptageCognitiveRuntimeError(Exception):
    """Erreur du runtime R1-C2 : toujours fail closed (rollback de la
    transaction appelante, aucune réparation silencieuse)."""


class CognitiveLinkInvariantError(DecryptageCognitiveRuntimeError):
    """Invariant livraison <-> lien cassé : livraison R1-C2 sans lien, lien
    sur une livraison legacy, couple d'états interdit, version inconnue,
    lien déjà présent à la création, event de référence incohérent."""


class AmbiguousOpenDecryptageEvents(DecryptageCognitiveRuntimeError):
    """Plusieurs CognitiveEvents Décrypter open compatibles : aucun choix
    heuristique, fail closed."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def runtime_private_metadata(step_marker: str | None) -> dict:
    """private_metadata d'une nouvelle AssistantDelivery R1-C2 : marqueur
    d'étape privé (jamais visible, jamais une preuve) + opt-in explicite de
    la capture cognitive. Une livraison sans cognitive_runtime_version est
    legacy (R1-C1) et n'est jamais capturée rétroactivement."""
    if step_marker is not None and step_marker not in DECRYPTAGE_STEP_MARKERS:
        raise DecryptageCognitiveRuntimeError("marqueur d'étape hors vocabulaire fermé")
    return {STEP_MARKER_KEY: step_marker, RUNTIME_VERSION_KEY: COGNITIVE_RUNTIME_VERSION}


def _runtime_version(delivery: AssistantDelivery) -> str | None:
    version = delivery.private_metadata.get(RUNTIME_VERSION_KEY)
    if version is not None and version != COGNITIVE_RUNTIME_VERSION:
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


def _lock_open_events(db, *, user_id: str, analysis_session_id=None) -> list[CognitiveEvent]:
    """Events Décrypter open de ce runtime pour user_id (éventuellement
    restreints à une AnalysisSession), verrouillés dans l'ordre des id. En
    READ COMMITTED, un event fermé par une transaction concurrente pendant
    l'attente du verrou est réévalué et exclu : jamais d'état obsolète."""
    query = (
        select(CognitiveEvent)
        .where(CognitiveEvent.user_id == user_id,
               CognitiveEvent.event_origin == EVENT_ORIGIN,
               CognitiveEvent.event_builder_version == EVENT_BUILDER_VERSION,
               CognitiveEvent.status == cognitive_capture.OPEN)
        .order_by(CognitiveEvent.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if analysis_session_id is not None:
        query = query.where(CognitiveEvent.analysis_session_id == analysis_session_id)
    return list(db.execute(query).scalars())


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
    """Ferme un event quitté : finalized s'il porte au moins une
    contribution utilisateur, abandoned s'il est vide (un event finalized
    vide serait un faux candidat à l'évaluation)."""
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
    ni marqueur, ni private_metadata) : le lien porte la relation."""
    return cognitive_capture.add_support_trace(
        db, event_id=event.id, support_kind=SUPPORT_KIND,
        support_payload={"visible_content": delivery.response_payload["analysis"]},
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def capture_user_turn(db, *, delivery: AssistantDelivery, ticker: str, user_text: str) -> DecryptageCognitiveLink:
    """Côté entrée, transaction /decryptage du worker GAGNANT uniquement
    (livraison créée par CET appel, avant la progression produit).

    1. AnalysisSession active du ticker canonique (avant mutation),
       verrouillée ; events Décrypter open de l'utilisateur, verrouillés.
    2. Plus d'un event open compatible (même AnalysisSession active) :
       AmbiguousOpenDecryptageEvents.
    3. Events open d'un autre contexte (autre AnalysisSession, ou aucune
       session active pour ce ticker) : quittés, finalized si travail
       présent, abandoned si vides. Le message du nouveau contexte n'y est
       jamais ajouté. L'AnalysisSession quittée n'est pas touchée.
    4. Event compatible + texte : contribution (contribution_id =
       source_turn_ref = client_turn_id, session_ref = conversation du
       TOUR, support_refs_before capturé par cognitive_capture). Sans
       texte (déclencheur / reprise) : aucune contribution.
    5. Ligne de lien awaiting_delivery.

    Mute et flush ; jamais de commit."""
    if _runtime_version(delivery) is None:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : livraison sans opt-in R1-C2")
    if delivery.status != "pending":
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : livraison déjà {delivery.status}")
    if _lock_link(db, delivery.id) is not None:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : lien déjà présent")

    active = _lock_active_analysis_session(db, user_id=delivery.user_id, ticker=ticker)
    open_events = _lock_open_events(db, user_id=delivery.user_id)
    compatible = [e for e in open_events if active is not None and e.analysis_session_id == active.id]
    exited = [e for e in open_events if e not in compatible]
    if len(compatible) > 1:
        raise AmbiguousOpenDecryptageEvents(
            f"assistant turn {delivery.id} : {len(compatible)} events open compatibles")

    closures = [(event.id, _close_event(db, event)) for event in exited]
    if len(closures) > 1:
        logger.warning("[R1-C2] assistant_turn=%s anomaly=multiple_context_exits events=%s",
                       delivery.id, [str(event_id) for event_id, _ in closures])
    exited_event_id = closures[0][0] if len(closures) == 1 else None

    input_event_id = None
    if compatible:
        (event,) = compatible
        if user_text:
            cognitive_capture.append_user_contribution(
                db, event_id=event.id, contribution_id=delivery.source_user_turn_id,
                source_turn_ref=delivery.source_user_turn_id, surface=SURFACE,
                session_ref=delivery.conversation_key, text_excerpt=user_text,
            )
            input_action, input_event_id = CONTRIBUTION_APPENDED, event.id
        else:
            input_action = NO_USER_CONTRIBUTION
    elif closures:
        any_finalized = any(status == cognitive_capture.FINALIZED for _, status in closures)
        input_action = EVENT_CLOSED_CONTEXT_CHANGE if any_finalized else EVENT_ABANDONED_CONTEXT_CHANGE
    else:
        input_action = NO_OPEN_EVENT

    link = DecryptageCognitiveLink(
        assistant_delivery_id=delivery.id,
        capture_version=CAPTURE_VERSION,
        input_action=input_action,
        input_event_id=input_event_id,
        context_exit_event_id=exited_event_id,
        capture_state=AWAITING_DELIVERY,
        response_action=None,
        response_event_id=None,
        support_trace_id=None,
        created_at=_utcnow(),
        captured_at=None,
    )
    db.add(link)
    db.flush()
    for event_id, status in closures:
        logger.info("[R1-C2] assistant_turn=%s context_exit event=%s status=%s", delivery.id, event_id, status)
    logger.info("[R1-C2] assistant_turn=%s input_action=%s event=%s", delivery.id, input_action, input_event_id)
    return link


def capture_delivered_response(db, *, delivery: AssistantDelivery) -> DecryptageCognitiveLink | None:
    """Côté réponse, transaction ACK, sous le verrou de la livraison
    (lock_delivery_for_ack) et AVANT acknowledge_delivery.

    - livraison legacy (sans cognitive_runtime_version) : aucun lien
      attendu, None (ACK historique inchangé) ; un lien présent = invariant
      cassé ;
    - livraison R1-C2 sans lien : CognitiveLinkInvariantError ;
    - delivered + captured : no-op idempotent (retry ACK) ;
    - pending + awaiting_delivery : capture ci-dessous ;
    - pending + captured, delivered + awaiting_delivery : invariant cassé.

    Le marqueur n'est un signal de frontière que si la progression produit
    l'a appliqué (livraison liée à une AnalysisSession). L'event de
    référence est l'UNIQUE event open compatible de cette AnalysisSession
    (verrouillée) ; plusieurs : fail closed.

    - aucun event de référence : marqueur de tâche -> opened_event ; sinon
      (absent, swot_final, non appliqué) -> no_cognitive_action ;
    - marqueur absent / inconnu / non appliqué -> SupportTrace,
      continued_without_boundary_signal (l'event reste open) ;
    - même marqueur -> SupportTrace, continued_event ;
    - swot_final -> fermeture de l'event, closed_terminal (aucun event
      swot_final, aucun SupportTrace) ;
    - marqueur suivant -> fermeture de l'event puis ouverture du nouvel
      event (stimulus = cette réponse, qui n'est PAS un SupportTrace de
      l'ancien), transitioned_event ;
    - marqueur rétrograde -> SupportTrace, continued_without_boundary_signal,
      diagnostic technique seulement.

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
    if link.capture_version != CAPTURE_VERSION:
        raise CognitiveLinkInvariantError(f"assistant turn {delivery.id} : capture_version inconnue")
    if delivery.status == "delivered" and link.capture_state == CAPTURED:
        return link
    if not (delivery.status == "pending" and link.capture_state == AWAITING_DELIVERY):
        raise CognitiveLinkInvariantError(
            f"assistant turn {delivery.id} : couple interdit {delivery.status}/{link.capture_state}")

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

    link.response_action = action
    link.response_event_id = response_event.id if response_event is not None else None
    link.support_trace_id = trace.id if trace is not None else None
    link.captured_at = _utcnow()
    link.capture_state = CAPTURED
    db.flush()
    logger.info("[R1-C2] assistant_turn=%s action=%s marker=%s from=%s event=%s support_trace=%s",
                delivery.id, action, marker or "absent", from_step, link.response_event_id, link.support_trace_id)
    return link
