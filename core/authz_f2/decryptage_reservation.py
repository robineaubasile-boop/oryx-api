"""Réservations Décrypter F2 : politique de bail, jeton, capacité et vues
fermées (R1 PR 9-B1) — module PUR, dormant.

Politique figée (serveur seulement, jamais fournie par le client) :

| Paramètre | Valeur |
|---|---|
| Durée de bail | 360 secondes |
| Attributions maximales | 3 |
| Backoff après l'échec de l'attribution 1 / 2 | 30 s / 120 s |
| Renouvellement | interdit |
| Purge automatique | interdite |

États : reserved, blocked (legacy_collision | attempts_exhausted,
terminal), finalized (PR 9-C ; B1 ne réalise jamais reserved -> finalized).

Jeton de bail (LeaseToken) : 32 octets aléatoires (secrets.token_bytes,
256 bits), jamais affiché par repr() / str(), jamais sérialisé (pickle,
copie, dataclasses.asdict, encodeurs JSON : refus), jamais stocké en clair.
PostgreSQL n'en garde que lease_token_hash = SHA-256(préfixe de domaine ||
jeton), hexadécimal minuscule, comparé en temps constant. Ce jeton n'est
PAS un justificatif F2 : il ne confère aucun droit sans nouvelle
autorisation complète du runner.

DecryptageLease : capacité interne IMMUABLE rendue dans LeaseGranted, qui
n'existe qu'après le COMMIT confirmé du runner (AuthorizationOutcome
AUTHORIZED). Un AUTHORIZED du runner n'autorise PAS à lui seul une
génération : seul un LeaseGranted portant une capacité valide le permettra
au futur orchestrateur (PR 9-B2).
"""
import dataclasses
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

LEASE_DURATION = timedelta(seconds=360)
MAX_ATTEMPTS = 3
# Backoff appliqué après l'échec déclaré de l'attribution n (1 ou 2) ;
# après l'échec de la troisième : blocage attempts_exhausted.
BACKOFFS = {1: timedelta(seconds=30), 2: timedelta(seconds=120)}
MAX_EPOCH = 2**63 - 1  # lease_epoch BIGINT : jamais de débordement

RESERVED = "reserved"
BLOCKED = "blocked"
FINALIZED = "finalized"
STATES = (RESERVED, BLOCKED, FINALIZED)
LEGACY_COLLISION = "legacy_collision"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
BLOCKED_REASONS = (LEGACY_COLLISION, ATTEMPTS_EXHAUSTED)

# Classification FERMÉE d'un échec fournisseur connu déclaré par le serveur
# (aucun effet sur la politique : le backoff dépend du seul rang de
# l'attribution ; la classification est validée et journalisée par nom).
MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
GENERATION_UNAVAILABLE = "generation_unavailable"
GENERATION_UNUSABLE = "generation_unusable"
FAILURE_CLASSES = (MARKET_DATA_UNAVAILABLE, GENERATION_UNAVAILABLE, GENERATION_UNUSABLE)

TOKEN_BYTES = 32
_TOKEN_DOMAIN = b"ORYX-DECRYPTAGE-LEASE-TOKEN\x00"


def backoff_after(attempt: int) -> timedelta:
    """Backoff après l'échec de l'attribution `attempt` (1 ou 2)."""
    if type(attempt) is not int or attempt not in BACKOFFS:
        raise ValueError("aucun backoff pour ce rang d'attribution")
    return BACKOFFS[attempt]


def next_epoch(epoch: int) -> int:
    """Epoch suivant ; jamais de débordement ni de recyclage."""
    if type(epoch) is not int or not 0 <= epoch < MAX_EPOCH:
        raise ValueError("lease_epoch : aucun epoch suivant")
    return epoch + 1


def next_attempt(attempts: int) -> int:
    """Compteur suivant ; jamais au-delà de MAX_ATTEMPTS."""
    if type(attempts) is not int or not 0 <= attempts < MAX_ATTEMPTS:
        raise ValueError("attempts_used : aucune attribution supplémentaire")
    return attempts + 1


class LeaseToken:
    """Jeton de bail brut (secret serveur)."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes):
        if type(value) is not bytes or len(value) != TOKEN_BYTES:
            raise ValueError("jeton de bail : 32 octets attendus")
        self._value = value

    @classmethod
    def generate(cls) -> "LeaseToken":
        return cls(secrets.token_bytes(TOKEN_BYTES))

    def reveal(self) -> bytes:
        return self._value

    def digest(self) -> str:
        """Empreinte stockée (lease_token_hash)."""
        return hashlib.sha256(_TOKEN_DOMAIN + self._value).hexdigest()

    def matches(self, recorded) -> bool:
        return type(recorded) is str and len(recorded) == 64 and hmac.compare_digest(
            self.digest().encode("ascii"), recorded.encode("ascii"))

    def __repr__(self) -> str:
        return "LeaseToken(<secret>)"

    __str__ = __repr__

    def __eq__(self, other) -> bool:
        return type(other) is LeaseToken and hmac.compare_digest(self._value, other._value)

    def __hash__(self):
        raise TypeError("LeaseToken n'est pas hachable")

    def __reduce__(self):
        raise TypeError("LeaseToken n'est pas sérialisable")

    def __reduce_ex__(self, protocol):
        raise TypeError("LeaseToken n'est pas sérialisable")

    def __copy__(self):
        raise TypeError("LeaseToken n'est pas copiable")

    def __deepcopy__(self, memo):
        raise TypeError("LeaseToken n'est pas copiable")


def _aware(value) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _uuid(value) -> bool:
    return type(value) is uuid.UUID


def _count(value, upper=MAX_EPOCH) -> bool:
    return type(value) is int and 0 <= value <= upper


class _Unserializable:
    """Vues porteuses d'une capacité : jamais sérialisées implicitement."""

    __slots__ = ()

    def __reduce__(self):
        raise TypeError(f"{type(self).__name__} n'est pas sérialisable")

    def __reduce_ex__(self, protocol):
        raise TypeError(f"{type(self).__name__} n'est pas sérialisable")

    def __copy__(self):
        raise TypeError(f"{type(self).__name__} n'est pas copiable")

    def __deepcopy__(self, memo):
        raise TypeError(f"{type(self).__name__} n'est pas copiable")


@dataclass(frozen=True, slots=True, repr=False)
class DecryptageLease(_Unserializable):
    """Capacité interne d'UN bail : réservation, epoch, jeton secret,
    échéance indicative (la décision fait foi en base) et identité minimale
    du tour."""
    reservation_id: uuid.UUID
    lease_epoch: int
    token: LeaseToken = dataclasses.field(repr=False)
    lease_expires_at: datetime
    conversation_key: str
    source_user_turn_id: uuid.UUID
    delivery_ordinal: int

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _uuid(self.source_user_turn_id):
            raise ValueError("bail : identifiants UUID attendus")
        if not _count(self.lease_epoch) or self.lease_epoch < 1 or type(self.token) is not LeaseToken:
            raise ValueError("bail : epoch ou jeton hors contrat")
        if not _aware(self.lease_expires_at) or type(self.conversation_key) is not str or self.delivery_ordinal != 1:
            raise ValueError("bail : échéance, conversation ou ordinal hors contrat")

    def __repr__(self) -> str:
        return (f"DecryptageLease(reservation_id={self.reservation_id!r}, lease_epoch={self.lease_epoch!r}, "
                f"token=<secret>, lease_expires_at={self.lease_expires_at!r})")

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class LeaseGranted(_Unserializable):
    """Bail attribué ET committé : seule vue qui permettra une génération."""
    lease: DecryptageLease
    attempts_used: int

    def __post_init__(self):
        if type(self.lease) is not DecryptageLease or not _count(self.attempts_used, MAX_ATTEMPTS) \
                or self.attempts_used < 1:
            raise ValueError("bail attribué hors contrat")

    def __repr__(self) -> str:
        return f"LeaseGranted(lease={self.lease!r}, attempts_used={self.attempts_used!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ReservationInProgress:
    """Bail encore actif détenu par un autre exécutant (ou jeton perdu après
    un COMMIT incertain) : aucune nouvelle capacité, attendre l'échéance."""
    reservation_id: uuid.UUID
    lease_epoch: int
    lease_expires_at: datetime

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _count(self.lease_epoch) or self.lease_epoch < 1 \
                or not _aware(self.lease_expires_at):
            raise ValueError("réservation en cours hors contrat")


@dataclass(frozen=True, slots=True)
class ReservationRetryDeferred:
    """Bail libéré après un échec connu : aucune attribution avant
    retry_not_before_at."""
    reservation_id: uuid.UUID
    retry_not_before_at: datetime
    attempts_used: int

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _aware(self.retry_not_before_at) \
                or not _count(self.attempts_used, MAX_ATTEMPTS):
            raise ValueError("report hors contrat")


@dataclass(frozen=True, slots=True)
class ReservationBlocked:
    """Blocage terminal durable (committé)."""
    reservation_id: uuid.UUID
    blocked_reason: str

    def __post_init__(self):
        if not _uuid(self.reservation_id) or type(self.blocked_reason) is not str \
                or self.blocked_reason not in BLOCKED_REASONS:
            raise ValueError("blocage hors contrat")


@dataclass(frozen=True, slots=True)
class ReservationFinalized:
    """Réservation finalisée par PR 9-C (lien de livraison cohérent)."""
    reservation_id: uuid.UUID
    assistant_delivery_id: uuid.UUID

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _uuid(self.assistant_delivery_id):
            raise ValueError("réservation finalisée hors contrat")


@dataclass(frozen=True, slots=True)
class AttemptFailureRecorded:
    """Échec déclaré par le détenteur valide du bail, committé : bail libéré
    avec backoff (reserved) ou blocage terminal (blocked)."""
    reservation_id: uuid.UUID
    state: str
    blocked_reason: object       # None (libéré) ou motif fermé
    retry_not_before_at: object  # datetime avec fuseau (libéré) ou None
    attempts_used: int

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _count(self.attempts_used, MAX_ATTEMPTS):
            raise ValueError("échec déclaré hors contrat")
        if self.state == RESERVED:
            if self.blocked_reason is not None or not _aware(self.retry_not_before_at):
                raise ValueError("échec déclaré : libération hors contrat")
        elif self.state == BLOCKED:
            if self.blocked_reason not in BLOCKED_REASONS or self.retry_not_before_at is not None:
                raise ValueError("échec déclaré : blocage hors contrat")
        else:
            raise ValueError("échec déclaré : état hors contrat")


RESERVE_RESULTS = (LeaseGranted, ReservationInProgress, ReservationRetryDeferred, ReservationBlocked,
                   ReservationFinalized)
FAILURE_RESULTS = (AttemptFailureRecorded,)
