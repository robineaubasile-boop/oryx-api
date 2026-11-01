"""Empreinte HMAC des intentions de tour Décrypter F2 (R1 PR 9-B1) — module PUR.

Version de canonicalisation : FINGERPRINT_VERSION = "oryx-decryptage-reservation-v1".

Trame (aucun JSON, indépendante des espaces et de l'ordre des clés) :

    ASCII("ORYX-DECRYPTAGE-RESERVATION") || 0x00
    || pour chacun des ONZE champs, dans cet ordre, même null :
       u32be(longueur du payload) || payload
       payload = 0x00 (null) | 0x01 || UTF-8(valeur)

    1 fingerprint_version        2 fingerprint_key_version   3 user_id serveur
    4 conversation_key           5 surface                   6 source_user_turn_id
    7 delivery_ordinal           8 ticker saisi normalisé    9 question
    10 contexte textuel          11 last_method_id

UUID : minuscules avec tirets (str(uuid.UUID)) ; entiers : décimal
canonique ; ticker : strip().upper() local (jamais la résolution EODHD) ;
question et contexte : strip() Python, caractères intérieurs inchangés ;
last_method_id : null distinct de toute chaîne (y compris ""), jamais
normalisé ; aucune normalisation Unicode supplémentaire. Ne sont JAMAIS
signés : niveau pédagogique, données de marché, état cognitif, epoch,
échéance, résultat Claude. HMAC-SHA256(K_decryptage[version], trame),
64 caractères hexadécimaux minuscules, comparé en temps constant. Toute
modification de cette trame exige une nouvelle version.

Ce HMAC n'est PAS équivalent au SHA-256 historique
assistant_deliveries.request_fingerprint (core.assistant_delivery) : les
deux ne sont jamais comparés.

Trousseau Décrypter (DecryptageKeyring) DÉDIÉ et indépendant des trousseaux
F2, CSRF et portefeuille (aucun repli entre trousseaux ; distinction des
valeurs vérifiée par le runner) : versions [a-z0-9][a-z0-9_-]{0,31}, clés
de 32 octets distinctes. Une réservation existante est TOUJOURS revérifiée
avec la version de canonicalisation et la version de clé ENREGISTRÉES ;
introuvables : UnknownDecryptageKey (refus fermé, jamais de remplacement
silencieux de l'empreinte).

Admission structurelle (admit_turn), avant tout accès SQL : conversation_key
1 à 200 caractères sans blanc de bord ; ticker normalisé 1 à 32
caractères ; question au plus 16 Kio UTF-8 ; contexte au plus 32 Kio ;
question + contexte au plus 48 Kio ; last_method_id None ou au plus 64
caractères ; chaînes Python exactes, encodables en UTF-8, sans NUL. Jamais
de troncature.

Ne lit aucune variable d'environnement, n'ouvre aucune connexion.
"""
import hashlib
import hmac
import re
import struct
import uuid
from types import MappingProxyType

FINGERPRINT_VERSION = "oryx-decryptage-reservation-v1"
PREFIX = b"ORYX-DECRYPTAGE-RESERVATION\x00"
KEY_BYTES = 32
KEY_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
SURFACE = "decryptage"
DELIVERY_ORDINAL = 1
FIELDS = ("fingerprint_version", "fingerprint_key_version", "user_id", "conversation_key", "surface",
          "source_user_turn_id", "delivery_ordinal", "ticker", "question", "context", "last_method_id")

CONVERSATION_KEY_MAX_CHARS = 200
TICKER_MAX_CHARS = 32
QUESTION_MAX_BYTES = 16 * 1024
CONTEXT_MAX_BYTES = 32 * 1024
TEXTS_MAX_BYTES = 48 * 1024
LAST_METHOD_ID_MAX_CHARS = 64


class DecryptageKeyringError(ValueError):
    """Trousseau Décrypter invalide (refus fermé à la construction)."""


class UnknownDecryptageKey(LookupError):
    """Version de canonicalisation ou de clé introuvable : refus fermé, sans repli."""


class TurnRejected(ValueError):
    """Entrée hors contrat (admission structurelle) : aucune requête SQL."""


class DecryptageKeyring:
    """keys = {version: clé de 32 octets} ; current = version des nouvelles
    réservations. repr() ne révèle aucune clé ; non sérialisable."""

    __slots__ = ("_keys", "_current")

    def __init__(self, keys: dict, current: str):
        if type(keys) is not dict or not keys:
            raise DecryptageKeyringError("trousseau Décrypter : au moins une clé")
        seen = set()
        for version, key in keys.items():
            if type(version) is not str or not KEY_VERSION_RE.fullmatch(version):
                raise DecryptageKeyringError("trousseau Décrypter : version invalide")
            if type(key) is not bytes or len(key) != KEY_BYTES:
                raise DecryptageKeyringError("trousseau Décrypter : clé de 32 octets attendue")
            if key in seen:
                raise DecryptageKeyringError("trousseau Décrypter : une clé distincte par version")
            seen.add(key)
        if type(current) is not str or current not in keys:
            raise DecryptageKeyringError("trousseau Décrypter : version courante absente")
        self._keys = MappingProxyType(dict(keys))
        self._current = current

    def __repr__(self) -> str:
        return f"DecryptageKeyring(<{len(self._keys)}>)"

    def __reduce__(self):
        raise TypeError("DecryptageKeyring n'est pas sérialisable")

    @property
    def current_version(self) -> str:
        return self._current

    def versions(self) -> tuple:
        return tuple(sorted(self._keys))

    def key(self, version) -> bytes:
        key = self._keys.get(version) if type(version) is str else None
        if key is None:
            raise UnknownDecryptageKey("clé Décrypter : version inconnue")
        return key

    def has_key_value(self, key: bytes) -> bool:
        return any(hmac.compare_digest(key, k) for k in self._keys.values())


# --------------------------------------------------------------------------
# Admission structurelle (pure, avant tout accès SQL)
# --------------------------------------------------------------------------

def _utf8(value) -> bytes:
    if type(value) is not str or "\x00" in value:
        raise TurnRejected("chaîne sans NUL attendue")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise TurnRejected("chaîne non encodable en UTF-8") from None


def admit_conversation_key(value) -> str:
    _utf8(value)
    if not value or len(value) > CONVERSATION_KEY_MAX_CHARS or value != value.strip():
        raise TurnRejected("conversation_key : 1 à 200 caractères, sans blanc de bord")
    return value


def normalize_ticker(value) -> str:
    """strip().upper() local, puis 1 à 32 caractères (jamais tronqué)."""
    _utf8(value)
    normalized = value.strip().upper()
    _utf8(normalized)
    if not normalized or len(normalized) > TICKER_MAX_CHARS:
        raise TurnRejected("ticker : 1 à 32 caractères après normalisation")
    return normalized


def normalize_texts(question, context) -> tuple:
    """(question.strip(), context.strip()) sous les bornes UTF-8 figées."""
    _utf8(question)
    _utf8(context)
    q, ctx = question.strip(), context.strip()
    q_bytes, ctx_bytes = len(_utf8(q)), len(_utf8(ctx))
    if q_bytes > QUESTION_MAX_BYTES or ctx_bytes > CONTEXT_MAX_BYTES or q_bytes + ctx_bytes > TEXTS_MAX_BYTES:
        raise TurnRejected("question / contexte : bornes UTF-8 dépassées")
    return q, ctx


def admit_last_method_id(value):
    if value is None:
        return None
    _utf8(value)
    if len(value) > LAST_METHOD_ID_MAX_CHARS:
        raise TurnRejected("last_method_id : 64 caractères au plus")
    return value


# --------------------------------------------------------------------------
# Trame et HMAC
# --------------------------------------------------------------------------

def _field(value) -> bytes:
    if value is None:
        payload = b"\x00"
    elif type(value) is str:
        payload = b"\x01" + value.encode("utf-8")
    else:
        raise TypeError("champ de trame : chaîne ou None")
    return struct.pack(">I", len(payload)) + payload


def frame_v1(*, key_version, user_id, conversation_key, source_user_turn_id, ticker, question, context,
             last_method_id) -> bytes:
    """Trame v1 de valeurs DÉJÀ admises et normalisées (aucune normalisation
    ici : la trame signe exactement ce qu'elle reçoit)."""
    if type(key_version) is not str or not KEY_VERSION_RE.fullmatch(key_version):
        raise ValueError("trame : version de clé invalide")
    if type(user_id) is not str or not user_id or type(source_user_turn_id) is not uuid.UUID:
        raise ValueError("trame : user_id serveur ou identifiant de tour invalide")
    values = (FINGERPRINT_VERSION, key_version, user_id, conversation_key, SURFACE, str(source_user_turn_id),
              str(DELIVERY_ORDINAL), ticker, question, context, last_method_id)
    return PREFIX + b"".join(_field(v) for v in values)


FRAMES = MappingProxyType({FINGERPRINT_VERSION: frame_v1})


def fingerprint(keyring: DecryptageKeyring, fingerprint_version, key_version, **fields) -> str:
    """HMAC hexadécimal minuscule de la trame de la version donnée ;
    UnknownDecryptageKey si la version ou la clé est introuvable."""
    if type(keyring) is not DecryptageKeyring:
        raise TypeError("trousseau Décrypter attendu")
    frame = FRAMES.get(fingerprint_version) if type(fingerprint_version) is str else None
    if frame is None:
        raise UnknownDecryptageKey("canonicalisation Décrypter : version inconnue")
    key = keyring.key(key_version)
    return hmac.new(key, frame(key_version=key_version, **fields), hashlib.sha256).hexdigest()


def same_fingerprint(computed: str, recorded) -> bool:
    return type(recorded) is str and FINGERPRINT_RE.fullmatch(recorded) is not None and hmac.compare_digest(
        computed.encode("ascii"), recorded.encode("ascii"))
