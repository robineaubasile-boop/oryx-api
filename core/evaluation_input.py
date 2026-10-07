"""R1-D1A — Evaluation Input Contract : provenance exacte et projection
évaluateur d'UN CognitiveEvent Décrypter finalized.

Point d'entrée : build_evaluation_input(db, event_id=...) ->
EvaluationInputBundle. Module 100 % LECTURE : aucun INSERT, UPDATE, DELETE,
flush, commit ni rollback, aucun appel LLM, aucun run, aucune observation,
aucun mapping T4. Rien n'est réparé : une donnée historique invalide ou d'une
version non supportée lève une exception dédiée (fail closed).

Deux contrats DISTINCTS :

A. EvaluationSourceSnapshot (source_snapshot) : provenance serveur complète
   et auditable (UUID réels de l'event, des contributions et des aides,
   versions T2, horodatages immuables, conversation de chaque contribution).
   source_fingerprint = SHA-256 de son JSON canonique (clés triées,
   séparateurs compacts, UTF-8, ensure_ascii=False, allow_nan=False ;
   contributions par phase, aides par sequence_no ; aucune donnée volatile :
   ni updated_at, ni horloge courante). Deux reconstructions du même event
   donnent le même hash.

B. LocalEvaluatorPayload (evaluator_payload) : la SEULE projection autorisée
   à sortir vers l'évaluateur. Tokens locaux (event_1, contribution_1,
   support_1...) ; jamais d'UUID interne, de user_id, de conversation_key,
   d'analysis_session_id, d'étape produit / marqueur, de clé de
   déduplication, d'horodatage système, de profil ou d'historique global, de
   UserStatement. Le bundle conserve le mapping token -> UUID réel.

Vérités causales :

- Stimulus : CognitiveEvent.stimulus_snapshot, tel que figé à l'ouverture
  (ce que l'utilisateur a réellement vu) ; jamais reconstruit depuis un
  prompt, l'AnalysisSession, un marqueur, des données de marché actuelles ou
  l'historique de chat.
- Aides disponibles AVANT une contribution : UNIQUEMENT
  contribution.support_refs_before (vérité capturée par T2 / R1-C4), jamais
  « created_at < occurred_at ». Les horodatages ne servent qu'à l'audit.
- Catalogue d'aides : union ordonnée (sequence_no) des SupportTrace
  référencés par au moins une contribution ; une aide jamais référencée
  (ex. rendue après la dernière contribution) n'est pas incluse.

D1A ne calcule RIEN d'interprétatif : ni compétence, task_kind, action,
rôle, sollicitation, niveau d'aide, travail résiduel, polarité, force de
preuve, stade, portée de contradiction, type d'erreur ni capacité.
Le task_kind T2 d'un event Décrypter reste NULL.

Versions supportées (R1-D1, fermées) : event Décrypter
(decryptage-event-builder-v1 / decryptage-event-admission-v1 /
cognitive-event-v2) dont CHAQUE contribution a été capturée par le runtime
decryptage-cognitive-runtime-v2 (R1-C4 : support_refs_before limités aux
aides réellement rendues dans la conversation du tour). Une contribution V1
(aides non scoppées par conversation) n'a pas cette garantie :
UnsupportedEvaluationInputVersion. Les versions sont recopiées ici (et non
importées du runtime) : un futur builder / runtime ne devient jamais
évaluable implicitement.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timezone
from types import MappingProxyType

from sqlalchemy import select

from core.models import AssistantDelivery, CognitiveEvent, DecryptageCognitiveLink, SupportTrace

EVALUATION_SOURCE_SCHEMA_VERSION = "evaluation-source-v1"

# Versions T2 / runtime supportées par R1-D1 (contrat fermé).
SUPPORTED_EVENT_ORIGIN = "decryptage"
SUPPORTED_EVENT_BUILDER_VERSION = "decryptage-event-builder-v1"
SUPPORTED_ADMISSION_VERSION = "decryptage-event-admission-v1"
SUPPORTED_EVENT_SCHEMA_VERSION = "cognitive-event-v2"
SUPPORTED_CAPTURE_VERSION = "decryptage-cognitive-runtime-v2"
LEGACY_CAPTURE_VERSIONS = frozenset({"decryptage-cognitive-runtime-v1"})
SUPPORTED_SURFACE = "decryptage"
SUPPORTED_SUPPORT_KIND = "assistant_response"
CONTRIBUTION_APPENDED = "contribution_appended"

FINALIZED = "finalized"

CONTRIBUTION_KEYS = frozenset({"contribution_id", "source_turn_ref", "phase", "surface", "session_ref",
                               "occurred_at", "text", "support_refs_before"})
STIMULUS_KEYS = frozenset({"visible_content"})
STIMULUS_PRICE_KEYS = frozenset({"visible_content", "price", "currency"})
SUPPORT_PAYLOAD_KEYS = frozenset({"visible_content"})

EVENT_TOKEN = "event_1"


class EvaluationInputError(Exception):
    """Erreur du contrat d'entrée D1A. Les messages ne contiennent que des
    identifiants techniques et des noms de champs, jamais de texte
    utilisateur, de stimulus ni d'aide."""


class EventNotEvaluable(EvaluationInputError):
    """Event introuvable, non finalized (open, abandoned) ou finalized sans
    aucune contribution."""


class InvalidEvaluationInput(EvaluationInputError):
    """Snapshot ou référence invalide (stimulus, contribution, aide,
    provenance de capture) : jamais réparé."""


class UnsupportedEvaluationInputVersion(EvaluationInputError):
    """Event ou contribution produit par une version T2 / runtime que R1-D1
    ne sait pas évaluer (origine, builder, admission, schéma, runtime V1)."""


@dataclass(frozen=True)
class EvaluationInputBundle:
    """Résultat de D1A. source_snapshot / evaluator_payload sont des
    structures JSON (dict / list / str / int / float / None) que l'appelant
    ne doit pas muter ; les mappings token -> UUID sont en lecture seule.

    contribution_support_before : token de contribution -> tokens des aides
    causalement disponibles avant elle (validation D1B par contribution)."""
    event_id: uuid.UUID
    source_snapshot: dict
    source_fingerprint: str
    evaluator_payload: dict
    contribution_ids: MappingProxyType
    contribution_phases: MappingProxyType
    support_ids: MappingProxyType
    contribution_support_before: MappingProxyType


# --------------------------------------------------------------------------
# Canonicalisation (partagée par D1A / D1C)
# --------------------------------------------------------------------------

def canonical_json(value) -> bytes:
    """JSON canonique strict : clés triées, séparateurs compacts, Unicode
    préservé, NaN / infini refusés, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_hex(value) -> str:
    """SHA-256 hexadécimal minuscule du JSON canonique de value."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _invalid(event_id, message: str):
    raise InvalidEvaluationInput(f"event {event_id} : {message}")


def _text(event_id, value, path: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        _invalid(event_id, f"{path} doit être une chaîne non vide sans NUL")
    return value


def _uuid_text(event_id, value, path: str) -> str:
    """UUID sous sa forme canonique str(uuid.UUID(...)) : aucune forme
    alternative (majuscules, accolades) n'est normalisée silencieusement."""
    if type(value) is not str:
        _invalid(event_id, f"{path} doit être un UUID canonique")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        canonical = None
    if canonical != value:
        _invalid(event_id, f"{path} doit être un UUID canonique")
    return value


def _utc(value) -> str | None:
    """Horodatage immuable en UTC explicite (indépendant du fuseau de la
    connexion PostgreSQL)."""
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _validate_stimulus(event_id, stimulus) -> dict:
    """Stimulus Décrypter (_visible_stimulus) : texte visible, et prix +
    devise du bandeau si un prix a été affiché."""
    if type(stimulus) is not dict or set(stimulus) not in (STIMULUS_KEYS, STIMULUS_PRICE_KEYS):
        _invalid(event_id, "stimulus_snapshot : structure non supportée")
    _text(event_id, stimulus["visible_content"], "stimulus_snapshot.visible_content")
    if "price" in stimulus:
        price = stimulus["price"]
        if type(price) not in (int, float) or price != price or price in (float("inf"), float("-inf")):
            _invalid(event_id, "stimulus_snapshot.price doit être un nombre fini")
        if stimulus["currency"] is not None:
            _text(event_id, stimulus["currency"], "stimulus_snapshot.currency")
    return dict(stimulus)


def _validate_contribution(event_id, index: int, item) -> dict:
    path = f"user_work_snapshot[{index}]"
    if type(item) is not dict or set(item) != CONTRIBUTION_KEYS:
        _invalid(event_id, f"{path} : clés attendues {sorted(CONTRIBUTION_KEYS)}")
    _uuid_text(event_id, item["contribution_id"], f"{path}.contribution_id")
    _uuid_text(event_id, item["source_turn_ref"], f"{path}.source_turn_ref")
    if type(item["phase"]) is not int or item["phase"] != index + 1:
        _invalid(event_id, f"{path}.phase doit valoir {index + 1}")
    if item["surface"] != SUPPORTED_SURFACE:
        raise UnsupportedEvaluationInputVersion(f"event {event_id} : {path}.surface non supportée")
    _text(event_id, item["session_ref"], f"{path}.session_ref")
    _text(event_id, item["occurred_at"], f"{path}.occurred_at")
    _text(event_id, item["text"], f"{path}.text")
    refs = item["support_refs_before"]
    if type(refs) is not list:
        _invalid(event_id, f"{path}.support_refs_before doit être une liste")
    for position, ref in enumerate(refs):
        _uuid_text(event_id, ref, f"{path}.support_refs_before[{position}]")
    if len(set(refs)) != len(refs):
        _invalid(event_id, f"{path}.support_refs_before : doublon")
    return item


def _validate_support(event_id, trace: SupportTrace) -> None:
    if trace.support_kind != SUPPORTED_SUPPORT_KIND:
        raise UnsupportedEvaluationInputVersion(
            f"event {event_id} : support_kind de l'aide {trace.sequence_no} non supporté")
    payload = trace.support_payload
    if type(payload) is not dict or set(payload) != SUPPORT_PAYLOAD_KEYS:
        _invalid(event_id, f"support_payload de l'aide {trace.sequence_no} : structure non supportée")
    _text(event_id, payload["visible_content"], f"support[{trace.sequence_no}].visible_content")


def _check_event(event: CognitiveEvent | None, event_id) -> CognitiveEvent:
    if event is None:
        raise EventNotEvaluable(f"event {event_id} introuvable")
    if event.status != FINALIZED:
        raise EventNotEvaluable(f"event {event_id} est {event.status} ({FINALIZED} requis)")
    versions = (event.event_origin, event.event_builder_version, event.admission_version, event.event_schema_version)
    supported = (SUPPORTED_EVENT_ORIGIN, SUPPORTED_EVENT_BUILDER_VERSION, SUPPORTED_ADMISSION_VERSION,
                 SUPPORTED_EVENT_SCHEMA_VERSION)
    if versions != supported:
        raise UnsupportedEvaluationInputVersion(f"event {event_id} : versions {list(versions)} non supportées")
    if event.task_kind is not None:
        _invalid(event_id, "task_kind T2 doit rester NULL pour un event Décrypter")
    if event.closed_at is None:
        _invalid(event_id, "closed_at manquant pour un event finalized")
    if type(event.user_work_snapshot) is not list:
        _invalid(event_id, "user_work_snapshot doit être une liste")
    if not event.user_work_snapshot:
        raise EventNotEvaluable(f"event {event_id} finalized sans contribution")
    return event


def _check_capture_provenance(db, event: CognitiveEvent, contributions: list) -> dict:
    """Chaque contribution doit avoir été capturée par EXACTEMENT un lien
    Décrypter contribution_appended du runtime V2, sur cet event, pour le
    même tour, la même conversation et le même utilisateur ; aucun lien
    contribution_appended de l'event ne doit rester orphelin. Lecture seule.
    Retourne contribution_id -> capture_version."""
    rows = db.execute(
        select(DecryptageCognitiveLink.capture_version, AssistantDelivery.source_user_turn_id,
               AssistantDelivery.conversation_key, AssistantDelivery.user_id)
        .join(AssistantDelivery, AssistantDelivery.id == DecryptageCognitiveLink.assistant_delivery_id)
        .where(DecryptageCognitiveLink.input_event_id == event.id,
               DecryptageCognitiveLink.input_action == CONTRIBUTION_APPENDED)
    ).all()
    by_turn = {}
    for capture_version, turn_id, conversation_key, user_id in rows:
        by_turn.setdefault(str(turn_id), []).append((capture_version, conversation_key, user_id))
    expected = {item["contribution_id"] for item in contributions}
    orphans = set(by_turn) - expected
    if orphans:
        _invalid(event.id, f"{len(orphans)} lien(s) contribution_appended sans contribution correspondante")
    versions = {}
    for item in contributions:
        links = by_turn.get(item["contribution_id"], [])
        if len(links) != 1:
            _invalid(event.id, f"contribution phase {item['phase']} : {len(links)} lien(s) de capture (1 requis)")
        ((capture_version, conversation_key, user_id),) = links
        if capture_version in LEGACY_CAPTURE_VERSIONS:
            raise UnsupportedEvaluationInputVersion(
                f"event {event.id} : contribution phase {item['phase']} capturée par {capture_version}")
        if capture_version != SUPPORTED_CAPTURE_VERSION:
            raise UnsupportedEvaluationInputVersion(
                f"event {event.id} : runtime de capture {capture_version!r} non supporté")
        if item["source_turn_ref"] != item["contribution_id"]:
            _invalid(event.id, f"contribution phase {item['phase']} : source_turn_ref incohérent")
        if conversation_key != item["session_ref"] or user_id != event.user_id:
            _invalid(event.id, f"contribution phase {item['phase']} : provenance de capture incohérente")
        versions[item["contribution_id"]] = capture_version
    return versions


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def build_evaluation_input(db, *, event_id: uuid.UUID) -> EvaluationInputBundle:
    """Construit, en LECTURE SEULE, la provenance (EvaluationSourceSnapshot),
    son fingerprint et la projection évaluateur (LocalEvaluatorPayload) d'un
    CognitiveEvent Décrypter finalized. Lève EventNotEvaluable,
    InvalidEvaluationInput ou UnsupportedEvaluationInputVersion ; aucune
    écriture, aucun flush, aucun verrou (un event finalized est immuable,
    ses aides append-only)."""
    if type(event_id) is not uuid.UUID:
        raise InvalidEvaluationInput("event_id doit être un uuid.UUID")
    event = _check_event(db.get(CognitiveEvent, event_id), event_id)
    stimulus = _validate_stimulus(event.id, event.stimulus_snapshot)
    contributions = [_validate_contribution(event.id, index, item)
                     for index, item in enumerate(event.user_work_snapshot)]
    if len({item["contribution_id"] for item in contributions}) != len(contributions):
        _invalid(event.id, "contribution_id en double")

    traces = {str(trace.id): trace for trace in db.execute(
        select(SupportTrace)
        .where(SupportTrace.cognitive_event_id == event.id)
        .order_by(SupportTrace.sequence_no.asc())
    ).scalars()}
    for item in contributions:
        refs = item["support_refs_before"]
        unknown = [ref for ref in refs if ref not in traces]
        if unknown:
            _invalid(event.id, f"contribution phase {item['phase']} : {len(unknown)} aide(s) hors de cet event")
        sequence = [traces[ref].sequence_no for ref in refs]
        if sequence != sorted(sequence):
            _invalid(event.id, f"contribution phase {item['phase']} : aides hors ordre chronologique")
    capture_versions = _check_capture_provenance(db, event, contributions)

    referenced = {ref for item in contributions for ref in item["support_refs_before"]}
    catalog = [trace for trace in traces.values() if str(trace.id) in referenced]
    for trace in catalog:
        _validate_support(event.id, trace)

    support_tokens = {str(trace.id): f"support_{index}" for index, trace in enumerate(catalog, start=1)}
    contribution_tokens = {item["contribution_id"]: f"contribution_{item['phase']}" for item in contributions}

    source_snapshot = {
        "schema_version": EVALUATION_SOURCE_SCHEMA_VERSION,
        "event": {
            "event_id": str(event.id),
            "event_origin": event.event_origin,
            "event_builder_version": event.event_builder_version,
            "admission_version": event.admission_version,
            "event_schema_version": event.event_schema_version,
            "task_kind": event.task_kind,
            "status": event.status,
            "user_id": event.user_id,
            "analysis_session_id": None if event.analysis_session_id is None else str(event.analysis_session_id),
            "conversation_key": event.conversation_key,
            "started_at": _utc(event.started_at),
            "closed_at": _utc(event.closed_at),
        },
        "stimulus_snapshot": stimulus,
        "contributions": [
            {
                "contribution_id": item["contribution_id"],
                "phase": item["phase"],
                "source_turn_ref": item["source_turn_ref"],
                "surface": item["surface"],
                "session_ref": item["session_ref"],
                "occurred_at": item["occurred_at"],
                "capture_version": capture_versions[item["contribution_id"]],
                "text": item["text"],
                "support_refs_before": list(item["support_refs_before"]),
            }
            for item in contributions
        ],
        "supports": [
            {
                "support_trace_id": str(trace.id),
                "sequence_no": trace.sequence_no,
                "support_kind": trace.support_kind,
                "support_payload": dict(trace.support_payload),
                "created_at": _utc(trace.created_at),
            }
            for trace in catalog
        ],
    }
    evaluator_payload = {
        "event_token": EVENT_TOKEN,
        "stimulus": dict(stimulus),
        "contributions": [
            {
                "contribution_token": contribution_tokens[item["contribution_id"]],
                "phase": item["phase"],
                "text": item["text"],
                "support_before": [support_tokens[ref] for ref in item["support_refs_before"]],
            }
            for item in contributions
        ],
        "support_catalog": [
            {
                "support_token": support_tokens[str(trace.id)],
                "sequence_no": trace.sequence_no,
                "support_kind": trace.support_kind,
                "visible_content": trace.support_payload["visible_content"],
            }
            for trace in catalog
        ],
    }
    # Copie canonique : le bundle ne partage aucun objet avec les lignes ORM.
    source_snapshot = json.loads(canonical_json(source_snapshot))
    evaluator_payload = json.loads(canonical_json(evaluator_payload))
    return EvaluationInputBundle(
        event_id=event.id,
        source_snapshot=source_snapshot,
        source_fingerprint=sha256_hex(source_snapshot),
        evaluator_payload=evaluator_payload,
        contribution_ids=MappingProxyType({token: cid for cid, token in contribution_tokens.items()}),
        contribution_phases=MappingProxyType({contribution_tokens[item["contribution_id"]]: item["phase"]
                                              for item in contributions}),
        support_ids=MappingProxyType({token: sid for sid, token in support_tokens.items()}),
        contribution_support_before=MappingProxyType({
            contribution_tokens[item["contribution_id"]]: tuple(support_tokens[ref]
                                                                for ref in item["support_refs_before"])
            for item in contributions
        }),
    )
