"""Orchestration HTTP F2 Décrypter (R1 PR 9-C3) — composition isolée
seulement, DORMANTE.

Trois routes, définies EXCLUSIVEMENT dans l'application personnelle F2
isolée (core.authz_f2_http.isolated, avec une DecryptageHttpConfig) ; aucune
n'existe sur api:app, quelles que soient les variables d'environnement :

  POST /api/v2/me/decryptage/turns   soumission ou reprise ACTIVE d'une
                                     intention (ReserveDecryptageTurn, puis
                                     B2, C1-B et C2 selon la décision B1)
  POST /api/v2/me/decryptage/replay  ReadCanonicalDecryptageDelivery
  POST /api/v2/me/decryptage/ack     AcknowledgeDecryptageDelivery

Admission (le premier échec répond ; aucun refus d'admission n'appelle le
runner ni n'ouvre de transaction) :

  1. méthode et chemin (routeur : 404 / 405) ;
  2. provenance de mutation personnelle (Origin obligatoire et exact, Host
     canonique autorisé, Sec-Fetch-Site same-origin s'il est présent,
     Referer de même origine)                        -> 403 origin_refused ;
  3. en-tête Cookie, parseur strict de PR 3         -> 400 invalid_request ;
  4. CSRF oryx-f2-csrf-v1 de PR 3                   -> 503 / 403 csrf_invalid ;
  5. X-Oryx-Generation-Id unique, UUID canonique    -> 400 invalid_request ;
  6. aucune query                                   -> 400 invalid_request ;
  7. Content-Type application/json [; charset=utf-8] -> 415 invalid_request ;
  8. framing (Transfer-Encoding, Content-Length dupliqué ou mal formé) -> 400 ;
     Content-Length valide au-delà de la borne      -> 413 request_too_large ;
  9. flux REÇU borné (384 Kio pour /turns, 4096 octets pour /replay et /ack),
     jugé avant l'ajout de chaque fragment ; discordance, troncature,
     déconnexion -> 400 ; dépassement -> 413 ;
 10. JSON UTF-8 strict : objet, clés EXACTES, sans doublon, sans constante,
     sans nombre ; UUID canoniques (validateur de PR 3) ; commande typée
     construite et admise structurellement (bornes métier B1 inchangées :
     question 16 Kio, contexte 32 Kio, total 48 Kio)  -> 400 invalid_request ;
 11. cookie __Host-oryx-active-<generation_id> EXACT -> 401 session_invalid ;
 12. /turns seulement : place d'exécution locale, acquise SANS attente
                                                    -> 503 capacity_unavailable.

Aucun user_id, état, jeton, HMAC, epoch ni principal n'est lu dans le corps :
le propriétaire est exclusivement AuthorizedPrincipal.user_id, rendu par le
runner à CHAQUE commande (nouvelle autorisation F2 complète).

Orchestration de /turns (hors de la boucle événementielle : toute la chaîne
s'exécute dans un fil de travail ; aucune transaction n'est ouverte ici, le
runner en reste l'unique propriétaire et B2 n'en garde aucune pendant un appel
fournisseur) :

  A. budget C3 (270 s, horloge monotone injectée) déjà épuisé : 503 SANS
     réservation ;
  B. ReserveDecryptageTurn (B1). AUTHORIZED : principal exact, génération =
     sélecteur admis ; identité (user_id, session_id, generation_id) retenue.
     LeaseGranted (bail du même tour) : moins de 255 s restantes -> 503 sans
     déclaration ni libération (B1 gouverne la reprise) ; sinon B2.
     ReservationInProgress / ReservationRetryDeferred -> 202 ;
     ReservationBlocked -> 409 ; ReservationFinalized -> replay C2 ;
  C. DecryptageGenerationOrchestrator.generate (B2), construit par la
     composition avec CE runner : même justificatif, commande originale,
     résultat B1 autorisé. B2 garde toutes ses responsabilités ;
  D. GENERATED : moins de 15 s restantes -> 503 ; sinon
     FinalizeDecryptageCandidate (C1-B), NOUVELLE autorisation ; principal
     comparé à l'identité B1 ; le candidat ne quitte jamais le serveur ;
  E. finalisée (ou déjà finalisée) : moins de 5 s restantes -> 503 prudent ;
     sinon ReadCanonicalDecryptageDelivery (C2), principal comparé, vue
     exacte, identifiants comparés -> 200 (seule source publique du texte).

Les seuils sont des frontières COOPÉRATIVES : ils ne prouvent pas qu'une
transaction terminera dans le temps imparti et n'interrompent jamais C1-B ni
son COMMIT ; 270 s est un objectif, pas un SLA. Une déconnexion ou une
annulation ne prouve rien : le fil de travail poursuit, rien n'est présumé
annulé, aucun retry n'a lieu ; la reprise se fait par la MÊME intention.

Capacité : deux orchestrations /turns simultanées par instance (quota LOCAL,
jamais distribué ; B1 reste seul garant durable contre les attributions
concurrentes). Machine d'état d'une place (_TurnSlot, transitions sous verrou) :
acquise -> démarrée -> terminée, ou acquise -> abandonnée (soumission annulée
avant le démarrage du fil). La place est rendue EXACTEMENT une fois : par la
requête si le travail n'a jamais démarré, sinon par le fil dans son finally ;
l'annulation de la coroutine ne la libère jamais prématurément. /replay et
/ack ne consomment aucune place.

Réponses : 200 {"assistant_turn_id", "delivery_status", "payload",
"reservation_id"} dont payload est inséré OCTET POUR OCTET depuis
CanonicalDecryptageDelivery.payload_json (texte JSONB validé par C2, jamais
resérialisé, jamais reconstruit depuis le candidat B2) ; 202 {"status":
"in_progress", "reservation_id", "lease_expires_at"} ou {"status":
"retry_deferred", "reservation_id", "retry_not_before_at"} (UTC RFC 3339,
six chiffres de microsecondes, suffixe Z, aucun Retry-After) ; ACK 200
{"assistant_turn_id", "delivery_status": "delivered"}. Erreurs {"error":
code} : invalid_request, session_invalid, origin_refused, csrf_invalid,
not_found, idempotency_conflict, revision_conflict, reservation_blocked,
context_conflict, instrument_unavailable, request_too_large,
temporarily_unavailable, provider_unavailable, capacity_unavailable,
identity_link_required (ACT-L1-PR1 : 403 pour IDENTITY_LINK_REQUIRED de B1,
C1-B, C2 et pour le motif B2 IDENTITY_REQUIRED, jamais 401, 404, 409 ni
503 ; une session invalide garde son 401 prioritaire). Toute
réponse : no-store, no-cache, nosniff, no-referrer, aucun Set-Cookie, aucun
CORS. Les journaux ne portent que route, statut, code et motifs fermés :
jamais un corps, un texte, un jeton, un cookie, un HMAC ni du SQL.

L'ACK n'est JAMAIS appelé par /turns ni par /replay : il confirme une
présentation déclarée par l'interface first-party, jamais une lecture.

Ne lit aucune variable d'environnement, n'ouvre aucune connexion et
n'appelle aucun réseau à l'import ni à la construction (transports injectés).
"""
import dataclasses
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import SessionCredential
from core.auth_f2_http.cookies import CookieHeaderRejected, canonical_uuid, parse_cookie_headers
from core.auth_f2_http.provenance import ProvenanceRejected
from core.auth_f2_http.routes import NO_STORE_HEADERS, error_response, json_response
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.decryptage_candidate import MODEL_MAX_CHARS
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import (
    AUTHORIZED,
    IDEMPOTENCY_CONFLICT,
    IDENTITY_LINK_REQUIRED,
    NOT_FOUND,
    REVISION_CONFLICT,
    SESSION_INVALID,
    UNAVAILABLE,
    AuthorizationOutcome,
    AuthorizedTransactionRunner,
)
from core.authz_f2_http.provenance import personal_mutation_origin
from core.authz_f2_http.routes import (
    IDENTITY_LINK_CODE,
    IDENTITY_LINK_HTTP_STATUS,
    NAMESPACE,
    PersonalDependencies,
    Refusal,
    _announced_length,
    _check_content_type,
    _check_csrf,
    _generation_id,
    _read_body,
    _reject_constant,
    _reject_duplicates,
)

logger = logging.getLogger("oryx.authz_f2.http")

TURNS = "/me/decryptage/turns"
REPLAY = "/me/decryptage/replay"
ACK = "/me/decryptage/ack"
ROUTES = (TURNS, REPLAY, ACK)

# Bornes de TRANSPORT (les bornes métier restent celles de B1).
TURN_MAX_BODY_BYTES = 384 * 1024
IDENTITY_MAX_BODY_BYTES = 4096
TURN_KEYS = frozenset({"conversation_key", "client_turn_id", "ticker_input", "question", "context",
                       "last_method_id"})
IDENTITY_KEYS = frozenset({"reservation_id", "conversation_key", "assistant_turn_id"})

# Budget C3 (constantes architecturales figées, secondes, horloge monotone).
C3_BUDGET_SECONDS = 270.0
MIN_BEFORE_GENERATION_SECONDS = 255.0
MIN_BEFORE_FINALIZATION_SECONDS = 15.0
MIN_AFTER_FINALIZATION_SECONDS = 5.0

# Capacité locale d'une instance C3 isolée (jamais un quota distribué).
MAX_CONCURRENT_TURNS = 2

DELIVERED = c.CANONICAL_DELIVERY_STATUSES[1]
TRANSPORT_METHODS = (("eodhd", ("search", "fundamentals", "realtime")), ("fmp", ("profile", "ratios")),
                     ("claude", ("create_message",)))
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*", re.ASCII)
_ACK_FIELDS = ("assistant_turn_id", "delivery_status", "response_action")


# --------------------------------------------------------------------------
# Configuration fermée
# --------------------------------------------------------------------------

def valid_model(value) -> bool:
    """Nom de modèle Claude fourni EXPLICITEMENT par la composition (jamais par
    le client, jamais lu dans l'environnement) : ASCII sans blanc, borne B2."""
    return type(value) is str and 0 < len(value) <= MODEL_MAX_CHARS and _MODEL_RE.fullmatch(value) is not None


def _finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True, slots=True, repr=False)
class DecryptageHttpConfig:
    """Configuration COMPLÈTE de C3 : trousseau Décrypter DÉDIÉ, transports
    conformes aux interfaces B2, modèle explicite, horloge monotone (budget C3
    et budget B2) et seuil facultatif de fraîcheur du cours (B2, None par
    défaut : comportement B2 inchangé). Toute pièce manquante ou invalide :
    refus à la construction. Jamais sérialisée, repr() sans contenu."""
    decryptage_keyring: DecryptageKeyring
    eodhd: object
    fmp: object
    claude: object
    model: str
    monotonic: object = time.monotonic
    price_recent_max_age: object = None

    def __post_init__(self):
        if type(self.decryptage_keyring) is not DecryptageKeyring:
            raise TypeError("C3 : trousseau Décrypter dédié (type exact) attendu")
        for name, methods in TRANSPORT_METHODS:
            transport = getattr(self, name)
            if transport is None or not all(callable(getattr(transport, m, None)) for m in methods):
                raise TypeError("C3 : transport fournisseur incomplet")
        if not valid_model(self.model):
            raise ValueError("C3 : modèle Claude explicite et valide attendu")
        if not callable(self.monotonic):
            raise TypeError("C3 : horloge monotone attendue")
        if not _finite_number(self.monotonic()):
            raise ValueError("C3 : horloge monotone hors contrat")
        if self.price_recent_max_age is not None and (type(self.price_recent_max_age) is not timedelta
                                                      or self.price_recent_max_age <= timedelta(0)):
            raise ValueError("C3 : seuil de fraîcheur du cours hors contrat")

    def __repr__(self) -> str:
        return "DecryptageHttpConfig(<private>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("DecryptageHttpConfig n'est pas sérialisable")


# --------------------------------------------------------------------------
# Capacité d'exécution locale (deux places) et propriété d'une place
# --------------------------------------------------------------------------

ACQUIRED = "acquired"    # place acquise, travail non démarré
STARTED = "started"      # le fil de travail a démarré : il possède la place
FINISHED = "finished"    # le fil a rendu la place (finally)
ABANDONED = "abandoned"  # soumission annulée avant démarrage : la requête a rendu la place


class _TurnCapacity:
    """Places /turns d'UNE instance ; toutes les transitions sous un verrou."""

    __slots__ = ("_lock", "_size", "_in_use", "_released")

    def __init__(self, size: int):
        if type(size) is not int or size < 1:
            raise ValueError("capacité : entier strictement positif attendu")
        self._lock = threading.Lock()
        self._size = size
        self._in_use = 0
        self._released = 0

    @property
    def size(self) -> int:
        return self._size

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @property
    def released(self) -> int:
        with self._lock:
            return self._released

    def try_acquire(self):
        """_TurnSlot ou None, sans jamais attendre."""
        with self._lock:
            if self._in_use >= self._size:
                return None
            self._in_use += 1
            return _TurnSlot(self)


class _TurnSlot:
    """UNE place : acquired -> started -> finished, ou acquired -> abandoned.
    Seules ces deux transitions terminales rendent la place : jamais deux fois."""

    __slots__ = ("_capacity", "_state")

    def __init__(self, capacity: _TurnCapacity):
        self._capacity = capacity
        self._state = ACQUIRED

    @property
    def state(self) -> str:
        with self._capacity._lock:
            return self._state

    def start(self) -> bool:
        """Appelé par le fil de travail AVANT tout effet : True s'il devient
        propriétaire de la place ; False si la soumission a déjà été abandonnée."""
        with self._capacity._lock:
            if self._state != ACQUIRED:
                return False
            self._state = STARTED
            return True

    def finish(self) -> None:
        """finally du fil de travail : rend la place s'il en est propriétaire."""
        with self._capacity._lock:
            if self._state == STARTED:
                self._state = FINISHED
                self._release()

    def abandon(self) -> bool:
        """Requête (finally) : rend la place SEULEMENT si le travail n'a jamais
        démarré ; sinon sans effet (le fil la rendra)."""
        with self._capacity._lock:
            if self._state != ACQUIRED:
                return False
            self._state = ABANDONED
            self._release()
            return True

    def _release(self) -> None:
        capacity = self._capacity
        capacity._in_use -= 1
        capacity._released += 1


async def _submit(work, submit=None):
    """Soumission au fil de travail (hors de la boucle événementielle). Point
    unique, remplaçable par les tests de course annulation / démarrage.
    ACT-L1 / F04-C : submit = soumetteur injecté (PersonalDependencies.submit) ;
    None : run_in_threadpool historique."""
    return await (run_in_threadpool if submit is None else submit)(work)


# --------------------------------------------------------------------------
# Budget C3 (coopératif)
# --------------------------------------------------------------------------

def _read_clock(clock):
    try:
        value = clock()
    except Exception:  # horloge défaillante : budget réputé épuisé (refus fermé)
        return None
    return value if _finite_number(value) else None


class _Budget:
    """270 s depuis `started` (après admission et attribution de la place,
    avant la soumission) ; toute lecture impossible : budget épuisé."""

    __slots__ = ("_clock", "_started")

    def __init__(self, clock, started):
        self._clock = clock
        self._started = started

    def remaining(self) -> float:
        now = _read_clock(self._clock)
        if self._started is None or now is None:
            return 0.0
        return C3_BUDGET_SECONDS - (now - self._started)


# --------------------------------------------------------------------------
# Réponses fermées
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, repr=False)
class _Reply:
    """status HTTP ; code d'erreur, corps JSON (dict) ou octets canoniques."""
    status: int
    code: object = None
    body: object = None

    def __repr__(self) -> str:
        return f"_Reply(status={self.status}, code={self.code})"


def _error(status: int, code: str) -> _Reply:
    return _Reply(status, code)


UNAVAILABLE_REPLY = _error(503, "temporarily_unavailable")
# ACT-L1-PR1 : réponse unique de IDENTITY_LINK_REQUIRED (runner) et du motif B2 équivalent.
IDENTITY_LINK_REPLY = _error(IDENTITY_LINK_HTTP_STATUS, IDENTITY_LINK_CODE)
_B1_REFUSALS = MappingProxyType({SESSION_INVALID: _error(401, "session_invalid"), NOT_FOUND: _error(404, "not_found"),
                                 IDEMPOTENCY_CONFLICT: _error(409, "idempotency_conflict"),
                                 REVISION_CONFLICT: _error(409, "revision_conflict"), UNAVAILABLE: UNAVAILABLE_REPLY,
                                 IDENTITY_LINK_REQUIRED: IDENTITY_LINK_REPLY})
# C1-B : même matrice fermée que B1 pour les statuts du runner.
_C1B_REFUSALS = _B1_REFUSALS
# C2 (replay et ACK) : IDEMPOTENCY_CONFLICT n'est pas inventorié -> 503.
_C2_REFUSALS = MappingProxyType({SESSION_INVALID: _error(401, "session_invalid"), NOT_FOUND: _error(404, "not_found"),
                                 REVISION_CONFLICT: _error(409, "revision_conflict"), UNAVAILABLE: UNAVAILABLE_REPLY,
                                 IDENTITY_LINK_REQUIRED: IDENTITY_LINK_REPLY})

_CONTEXT_CONFLICT = _error(409, "context_conflict")
# Refus B2 (vocabulaire FERMÉ dg.REFUSAL_REASONS) -> réponse ; les motifs
# d'invariant donnent 503 ET une alerte interne (aucune donnée privée).
_B2_REFUSALS = MappingProxyType({
    dg.INSTRUMENT_NOT_FOUND: _error(422, "instrument_unavailable"),
    dg.INSTRUMENT_AMBIGUOUS: _error(422, "instrument_unavailable"),
    dg.CONTEXT_CHANGED: _CONTEXT_CONFLICT,
    **{reason: _CONTEXT_CONFLICT for reason in dc.REFUSAL_REASONS},
    dg.SESSION_REFUSED: _error(401, "session_invalid"),
    dg.IDENTITY_REQUIRED: IDENTITY_LINK_REPLY,
    dg.RESERVATION_NOT_FOUND: _error(404, "not_found"),
    dg.INTENT_MISMATCH: _error(409, "revision_conflict"),
    dg.LEASE_STALE: _error(409, "revision_conflict"),
    dg.BUDGET_EXHAUSTED: UNAVAILABLE_REPLY,
    dg.LEASE_EXPIRED: UNAVAILABLE_REPLY,
    dg.AUTHORIZATION_UNAVAILABLE: UNAVAILABLE_REPLY,
    dg.OWNER_CHANGED: UNAVAILABLE_REPLY,
    dg.LEASE_NOT_GRANTED: UNAVAILABLE_REPLY,
    dg.LEASE_MISMATCH: UNAVAILABLE_REPLY,
    dg.LEASE_ALREADY_USED: UNAVAILABLE_REPLY,
})
B2_ALERT_REASONS = frozenset({dg.OWNER_CHANGED, dg.LEASE_NOT_GRANTED, dg.LEASE_MISMATCH, dg.LEASE_ALREADY_USED})
# Échec fournisseur : seule une déclaration CONFIRMÉE est présentée comme telle.
_PROVIDER_REPLIES = MappingProxyType({dg.DECLARED: _error(503, "provider_unavailable"),
                                      dg.DECLARATION_REFUSED: UNAVAILABLE_REPLY,
                                      dg.DECLARATION_UNCERTAIN: UNAVAILABLE_REPLY})


def rfc3339_utc(value) -> str:
    """UTC, RFC 3339, exactement six chiffres de microsecondes, suffixe Z."""
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("instant avec fuseau attendu")
    at = value.astimezone(timezone.utc)
    return (f"{at.year:04d}-{at.month:02d}-{at.day:02d}T{at.hour:02d}:{at.minute:02d}:{at.second:02d}."
            f"{at.microsecond:06d}Z")


def _strict_json(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
                      parse_float=Decimal, parse_int=Decimal)


def canonical_body(view) -> bytes:
    """Enveloppe contrôlée ; payload_json inséré tel quel (jamais resérialisé).
    Le résultat est relu strictement (nombres en Decimal) AVANT émission :
    toute anomalie -> ValueError (503 ; jamais une réparation)."""
    payload = view.payload_json
    if type(payload) is not bytes or not payload:
        raise ValueError("payload canonique hors contrat")
    body = (b'{"assistant_turn_id":"' + str(view.assistant_turn_id).encode("ascii")
            + b'","delivery_status":"' + view.delivery_status.encode("ascii")
            + b'","payload":' + payload
            + b',"reservation_id":"' + str(view.reservation_id).encode("ascii") + b'"}')
    try:
        parsed = _strict_json(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("enveloppe canonique illisible") from None
    if (type(parsed) is not dict or set(parsed) != {"assistant_turn_id", "delivery_status", "payload", "reservation_id"}
            or type(parsed["payload"]) is not dict or parsed["assistant_turn_id"] != str(view.assistant_turn_id)
            or parsed["reservation_id"] != str(view.reservation_id)
            or parsed["delivery_status"] != view.delivery_status):
        raise ValueError("enveloppe canonique hors contrat")
    return body


def acknowledged(value, assistant_turn_id) -> bool:
    """Vue ACK historique du pont (type exact vérifié par l'adaptateur SQL, que
    la couche HTTP ne nomme pas : frontière d'import figée) : dataclass gelée
    aux trois champs exacts, livraison demandée, statut delivered."""
    kind = type(value)
    if not dataclasses.is_dataclass(kind) or kind.__name__ != "DeliveryAcknowledged" \
            or not kind.__dataclass_params__.frozen:
        return False
    return (tuple(f.name for f in dataclasses.fields(kind)) == _ACK_FIELDS
            and type(value.assistant_turn_id) is uuid.UUID and value.assistant_turn_id == assistant_turn_id
            and value.delivery_status == DELIVERED)


# --------------------------------------------------------------------------
# Continuité du principal
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Identity:
    """Identité serveur retenue à la première autorisation B1 confirmée."""
    user_id: str
    session_id: uuid.UUID
    generation_id: uuid.UUID

    @classmethod
    def of(cls, principal) -> "_Identity":
        return cls(principal.user_id, principal.session_id, principal.generation_id)

    def matches(self, principal) -> bool:
        return (type(principal) is AuthorizedPrincipal and principal.user_id == self.user_id
                and principal.session_id == self.session_id and principal.generation_id == self.generation_id)


def _alert(label: str, alert: str) -> _Reply:
    logger.error("authz_f2 http %s status=503 alert=%s", label, alert)
    return UNAVAILABLE_REPLY


# --------------------------------------------------------------------------
# Orchestration (fil de travail)
# --------------------------------------------------------------------------

class _Orchestration:
    """runner : l'UNIQUE AuthorizedTransactionRunner de la composition ;
    generator : DecryptageGenerationOrchestrator construit avec CE runner."""

    __slots__ = ("_runner", "_generator", "_clock")

    def __init__(self, runner, generator, clock):
        self._runner, self._generator, self._clock = runner, generator, clock

    def __repr__(self) -> str:
        return "_Orchestration()"

    def __reduce__(self):
        raise TypeError("orchestration C3 non sérialisable")

    # -- /turns -------------------------------------------------------------

    def turn(self, credential, command, generation_id, started) -> _Reply:
        label = "decryptage.turns"
        budget = _Budget(self._clock, started)
        if budget.remaining() <= 0.0:
            logger.info("authz_f2 http %s budget=exhausted_before_reservation", label)
            return UNAVAILABLE_REPLY
        reserved = self._runner.run(credential, command)
        if type(reserved) is not AuthorizationOutcome:
            return _alert(label, "b1_outcome_out_of_contract")
        if reserved.status != AUTHORIZED:
            return _B1_REFUSALS.get(reserved.status) or _alert(label, "b1_status_out_of_contract")
        principal = reserved.principal
        if type(principal) is not AuthorizedPrincipal or principal.generation_id != generation_id:
            return _alert(label, "b1_principal_mismatch")
        identity = _Identity.of(principal)
        view = reserved.value
        kind = type(view)
        if kind is dr.LeaseGranted:
            lease = view.lease
            if type(lease) is not dr.DecryptageLease or (lease.conversation_key, lease.source_user_turn_id) != (
                    command.conversation_key, command.client_turn_id):
                return _alert(label, "b1_lease_mismatch")
            return self._generate(label, credential, command, reserved, identity, lease, budget)
        if kind is dr.ReservationInProgress:
            return _Reply(202, body={"status": "in_progress", "reservation_id": str(view.reservation_id),
                                     "lease_expires_at": rfc3339_utc(view.lease_expires_at)})
        if kind is dr.ReservationRetryDeferred:
            return _Reply(202, body={"status": "retry_deferred", "reservation_id": str(view.reservation_id),
                                     "retry_not_before_at": rfc3339_utc(view.retry_not_before_at)})
        if kind is dr.ReservationBlocked:
            return _error(409, "reservation_blocked")
        if kind is dr.ReservationFinalized:
            return self._replay(label, credential, identity, generation_id, view.reservation_id,
                                command.conversation_key, view.assistant_delivery_id)
        return _alert(label, "b1_view_out_of_contract")

    def _generate(self, label, credential, command, reserved, identity, lease, budget) -> _Reply:
        if budget.remaining() < MIN_BEFORE_GENERATION_SECONDS:
            # Bail committé : aucune déclaration fournisseur, aucune libération ;
            # B1 gouverne la reprise depuis son état durable.
            logger.info("authz_f2 http %s budget=insufficient_before_generation", label)
            return UNAVAILABLE_REPLY
        generated = self._generator.generate(credential, command, reserved)
        if type(generated) is not dg.GenerationOutcome:
            return _alert(label, "b2_outcome_out_of_contract")
        if generated.status == dg.REFUSED:
            if generated.reason in B2_ALERT_REASONS:
                return _alert(label, f"b2_{generated.reason}")
            return _B2_REFUSALS.get(generated.reason) or _alert(label, "b2_reason_out_of_contract")
        if generated.status == dg.PROVIDER_FAILED:
            return _PROVIDER_REPLIES.get(generated.declaration) or _alert(label, "b2_declaration_out_of_contract")
        if generated.status != dg.GENERATED:
            return _alert(label, "b2_status_out_of_contract")
        candidate = generated.candidate
        if candidate.owner_user_id != identity.user_id or (
                candidate.lease.reservation_id, candidate.lease.lease_epoch) != (lease.reservation_id,
                                                                                 lease.lease_epoch):
            return _alert(label, "b2_candidate_mismatch")
        if budget.remaining() < MIN_BEFORE_FINALIZATION_SECONDS:
            logger.info("authz_f2 http %s budget=insufficient_before_finalization", label)
            return UNAVAILABLE_REPLY
        return self._finalize(label, credential, command, identity, lease, candidate, budget)

    def _finalize(self, label, credential, command, identity, lease, candidate, budget) -> _Reply:
        try:
            finalize = c.FinalizeDecryptageCandidate(candidate)
        except (TypeError, ValueError):
            return _alert(label, "c1b_candidate_refused")
        finalized = self._runner.run(credential, finalize)  # NOUVELLE autorisation F2 complète
        if type(finalized) is not AuthorizationOutcome:
            return _alert(label, "c1b_outcome_out_of_contract")
        if finalized.status != AUTHORIZED:
            # UNAVAILABLE : COMMIT peut-être effectué ; jamais un rollback prétendu.
            return _C1B_REFUSALS.get(finalized.status) or _alert(label, "c1b_status_out_of_contract")
        if not identity.matches(finalized.principal):
            return _alert(label, "c1b_principal_mismatch")
        view = finalized.value
        kind = type(view)
        if kind in (dfin.DecryptageFinalized, dfin.AlreadyFinalized):
            if view.reservation_id != lease.reservation_id:
                return _alert(label, "c1b_reservation_mismatch")
            if budget.remaining() < MIN_AFTER_FINALIZATION_SECONDS:
                logger.info("authz_f2 http %s budget=insufficient_after_finalization", label)
                return UNAVAILABLE_REPLY
            return self._replay(label, credential, identity, identity.generation_id, view.reservation_id,
                                command.conversation_key, view.assistant_delivery_id)
        if kind in (dfin.FinalizationContextChanged, dfin.FinalizationPreparationRefused):
            if view.reservation_id != lease.reservation_id:
                return _alert(label, "c1b_reservation_mismatch")
            return _CONTEXT_CONFLICT
        if kind in (dfin.LegacyCollisionBlocked, dr.ReservationBlocked):
            if view.reservation_id != lease.reservation_id:  # C3-V12-02 : identité de réservation cohérente
                return _alert(label, "c1b_reservation_mismatch")
            return _error(409, "reservation_blocked")
        return _alert(label, "c1b_view_out_of_contract")

    # -- C2 -------------------------------------------------------------------

    def _replay(self, label, credential, identity, generation_id, reservation_id, conversation_key,
                assistant_turn_id) -> _Reply:
        try:
            command = c.ReadCanonicalDecryptageDelivery(reservation_id, conversation_key, assistant_turn_id)
        except (TypeError, ValueError):
            return _alert(label, "c2_identity_refused")
        return self._read(label, credential, identity, generation_id, command)

    def _read(self, label, credential, identity, generation_id, command) -> _Reply:
        replayed = self._runner.run(credential, command)
        if type(replayed) is not AuthorizationOutcome:
            return _alert(label, "c2_outcome_out_of_contract")
        if replayed.status != AUTHORIZED:
            return _C2_REFUSALS.get(replayed.status) or _alert(label, "c2_status_out_of_contract")
        principal = replayed.principal
        if type(principal) is not AuthorizedPrincipal or principal.generation_id != generation_id or (
                identity is not None and not identity.matches(principal)):
            return _alert(label, "c2_principal_mismatch")
        view = replayed.value
        if type(view) is not c.CanonicalDecryptageDelivery or view.reservation_id != command.reservation_id \
                or view.assistant_turn_id != command.assistant_turn_id \
                or view.delivery_status not in c.CANONICAL_DELIVERY_STATUSES:
            return _alert(label, "c2_view_mismatch")
        try:
            body = canonical_body(view)
        except ValueError:
            return _alert(label, "c2_payload_out_of_contract")
        return _Reply(200, body=body)

    # -- /replay et /ack ------------------------------------------------------

    def replay(self, credential, generation_id, command) -> _Reply:
        return self._read("decryptage.replay", credential, None, generation_id, command)

    def acknowledge(self, credential, generation_id, command) -> _Reply:
        label = "decryptage.ack"
        outcome = self._runner.run(credential, command)
        if type(outcome) is not AuthorizationOutcome:
            return _alert(label, "c2_outcome_out_of_contract")
        if outcome.status != AUTHORIZED:
            # UNAVAILABLE : COMMIT peut-être effectué ; un nouvel ACK explicite
            # retrouvera l'état durable (idempotent).
            return _C2_REFUSALS.get(outcome.status) or _alert(label, "c2_status_out_of_contract")
        principal = outcome.principal
        if type(principal) is not AuthorizedPrincipal or principal.generation_id != generation_id:
            return _alert(label, "c2_principal_mismatch")
        if not acknowledged(outcome.value, command.assistant_turn_id):
            return _alert(label, "c2_ack_out_of_contract")
        return _Reply(200, body={"assistant_turn_id": str(command.assistant_turn_id), "delivery_status": DELIVERED})


# --------------------------------------------------------------------------
# Admission HTTP
# --------------------------------------------------------------------------

def _reject_number(_):
    raise ValueError("nombre JSON refusé")


def _json_object(body: bytes, keys: frozenset) -> dict:
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
                          parse_int=_reject_number, parse_float=_reject_number)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal(400, "invalid_request") from None
    if type(data) is not dict or set(data) != keys:
        raise Refusal(400, "invalid_request")
    return data


def _uuid(value) -> uuid.UUID:
    parsed = canonical_uuid(value)
    if parsed is None:
        raise Refusal(400, "invalid_request")
    return parsed


def turn_command(body: bytes) -> c.ReserveDecryptageTurn:
    data = _json_object(body, TURN_KEYS)
    turn_id = _uuid(data["client_turn_id"])
    texts = (data["conversation_key"], data["ticker_input"], data["question"], data["context"])
    if any(type(v) is not str for v in texts) or not (data["last_method_id"] is None
                                                       or type(data["last_method_id"]) is str):
        raise Refusal(400, "invalid_request")
    try:
        return c.ReserveDecryptageTurn(data["conversation_key"], turn_id, data["ticker_input"], data["question"],
                                       data["context"], data["last_method_id"])
    except (TypeError, ValueError):
        raise Refusal(400, "invalid_request") from None


def identity_command(body: bytes, cls):
    data = _json_object(body, IDENTITY_KEYS)
    reservation_id, assistant_turn_id = _uuid(data["reservation_id"]), _uuid(data["assistant_turn_id"])
    if type(data["conversation_key"]) is not str:
        raise Refusal(400, "invalid_request")
    try:
        return cls(reservation_id, data["conversation_key"], assistant_turn_id)
    except (TypeError, ValueError):
        raise Refusal(400, "invalid_request") from None


def _response(label: str, reply) -> Response:
    if type(reply) is not _Reply:
        reply = UNAVAILABLE_REPLY
    if reply.code is not None:
        logger.info("authz_f2 http %s status=%s code=%s", label, reply.status, reply.code)
        return error_response(reply.status, reply.code)
    logger.info("authz_f2 http %s status=%s", label, reply.status)
    if type(reply.body) is bytes:
        return Response(content=reply.body, status_code=reply.status, media_type="application/json",
                        headers=NO_STORE_HEADERS)
    return json_response(reply.status, reply.body)


def build_decryptage_router(deps: PersonalDependencies, config: DecryptageHttpConfig) -> APIRouter:
    """Les trois routes C3 autour du runner UNIQUE de la composition :
    l'orchestrateur B2 est construit ICI avec deps.runner (jamais fourni de
    l'extérieur, jamais lié à un autre runner)."""
    if type(config) is not DecryptageHttpConfig:
        raise TypeError("C3 : DecryptageHttpConfig attendue")
    if type(deps) is not PersonalDependencies or type(deps.runner) is not AuthorizedTransactionRunner:
        raise TypeError("C3 : runner F2 de la composition attendu")
    generator = dg.DecryptageGenerationOrchestrator(deps.runner, eodhd=config.eodhd, fmp=config.fmp,
                                                    claude=config.claude, model=config.model,
                                                    clock=config.monotonic,
                                                    price_recent_max_age=config.price_recent_max_age)
    flow = _Orchestration(deps.runner, generator, config.monotonic)
    capacity = _TurnCapacity(MAX_CONCURRENT_TURNS)
    router = APIRouter(prefix=NAMESPACE)

    async def admit(request: Request, limit: int, parse):
        """Contrôles 2 à 11 : (justificatif, génération, commande) ou Refusal."""
        try:
            origin = personal_mutation_origin(request.headers, deps.allowed_origins)
        except ProvenanceRejected:
            raise Refusal(403, "origin_refused") from None
        try:
            cookies = parse_cookie_headers(request.headers.getlist("cookie"))
        except CookieHeaderRejected:
            raise Refusal(400, "invalid_request") from None
        _check_csrf(request, deps, origin, cookies)
        generation_id = _generation_id(request)
        if request.scope.get("query_string"):
            raise Refusal(400, "invalid_request")
        _check_content_type(request)
        announced = _announced_length(request, limit=limit)
        command = parse(await _read_body(request, announced, limit=limit))
        secret = cookies.actives.get(generation_id)
        if secret is None:
            raise Refusal(401, "session_invalid")
        return SessionCredential(generation_id, Redacted(secret)), generation_id, command

    @router.post(TURNS, include_in_schema=False)
    async def turns(request: Request):
        label = "decryptage.turns"
        try:
            credential, generation_id, command = await admit(request, TURN_MAX_BODY_BYTES, turn_command)
            slot = capacity.try_acquire()
            if slot is None:
                raise Refusal(503, "capacity_unavailable")  # avant tout appel B1
        except Refusal as refusal:
            logger.info("authz_f2 http %s status=%s code=%s", label, refusal.http_status, refusal.code)
            return error_response(refusal.http_status, refusal.code)
        started = _read_clock(config.monotonic)  # début du budget : avant la soumission

        def work():
            if not slot.start():
                return None  # soumission abandonnée avant démarrage : aucun effet
            try:
                return flow.turn(credential, command, generation_id, started)
            except Exception as exc:  # refus fermé : aucun détail, aucun contenu
                logger.warning("authz_f2 http %s worker_error type=%s", label, type(exc).__name__)
                return UNAVAILABLE_REPLY
            finally:
                slot.finish()

        try:
            # F04-C : le soumetteur injecté est transmis à _submit ; sans injection,
            # appel historique inchangé (_submit reste remplaçable par les tests).
            reply = await _submit(work) if deps.submit is None else await _submit(work, deps.submit)
        except Exception as exc:  # noqa: BLE001 — soumission impossible : refus fermé
            logger.warning("authz_f2 http %s submit_error type=%s", label, type(exc).__name__)
            reply = UNAVAILABLE_REPLY
        finally:
            slot.abandon()  # sans effet si le fil a démarré : il rendra la place
        return _response(label, reply)

    async def single(label: str, request: Request, cls, perform):
        try:
            credential, generation_id, command = await admit(
                request, IDENTITY_MAX_BODY_BYTES, lambda body: identity_command(body, cls))
        except Refusal as refusal:
            logger.info("authz_f2 http %s status=%s code=%s", label, refusal.http_status, refusal.code)
            return error_response(refusal.http_status, refusal.code)
        try:
            reply = await (run_in_threadpool if deps.submit is None else deps.submit)(
                perform, credential, generation_id, command)
        except Exception as exc:  # refus fermé : aucun détail
            logger.warning("authz_f2 http %s runner_error type=%s", label, type(exc).__name__)
            reply = UNAVAILABLE_REPLY
        return _response(label, reply)

    @router.post(REPLAY, include_in_schema=False)
    async def replay(request: Request):
        return await single("decryptage.replay", request, c.ReadCanonicalDecryptageDelivery, flow.replay)

    @router.post(ACK, include_in_schema=False)
    async def ack(request: Request):
        return await single("decryptage.ack", request, c.AcknowledgeDecryptageDelivery, flow.acknowledge)

    return router
