"""Empreinte HMAC des intentions d'écriture portefeuille (R1 PR 7).

Version de canonicalisation : FINGERPRINT_VERSION = "oryx-portfolio-request-v1".

Trame (aucun JSON brut, indépendante des espaces et de l'ordre des clés) :

    ASCII("ORYX-PORTFOLIO-WRITE") || 0x00
    || pour chacun des DOUZE champs, dans cet ordre, même null :
       u32be(longueur du payload) || payload
       payload = 0x00 (null) | 0x01 || UTF-8(valeur)

    1 fingerprint_version   2 fingerprint_key_version   3 user_id serveur
    4 operation_type        5 operation_id              6 position_id
    7 expected_revision     8 ticker                    9 quantity
    10 purchase_price       11 target_percent           12 envelope

Entiers : chaîne décimale canonique ; UUID : minuscules avec tirets.
CREATE : position_id et expected_revision null ; DELETE : cinq champs
métier null. HMAC-SHA256(K_portfolio[version], trame), hexadécimal
minuscule, comparé en temps constant (hmac.compare_digest).

Trousseau portefeuille (PortfolioKeyring) DÉDIÉ : versions
[a-z0-9][a-z0-9_-]{0,31}, clés de 32 octets distinctes entre versions,
distinctes des clés F2 et CSRF (vérifié à la composition,
core.authz_f2_http.isolated). Toute version est immuable et jamais
réattribuée à un autre secret ; cette permanence relève de la
configuration (une version réattribuée n'est pas détectable : les rejeux
de ses reçus rendraient 409). Un rejeu utilise TOUJOURS la version de
canonicalisation et la version de clé enregistrées dans le reçu, jamais la
version courante : version ou clé introuvable -> UnknownPortfolioKey (503,
aucune mutation, aucun repli). Rotation : déployer un trousseau qui lit
l'ancienne et la nouvelle clé, vérifier toutes les instances, puis basculer
la version courante ; conserver les anciennes clés tant que des reçus les
désignent (aucun TTL en R1).

Modèle de menace : le HMAC protège l'intention de requête ; il ne signe pas
le résultat JSONB. La frontière de confiance inclut le serveur et son rôle
SQL.
"""
import hashlib
import hmac
import re
import struct
import uuid
from types import MappingProxyType

FINGERPRINT_VERSION = "oryx-portfolio-request-v1"
PREFIX = b"ORYX-PORTFOLIO-WRITE\x00"
KEY_BYTES = 32
KEY_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
CREATE = "portfolio_position_create"
UPDATE = "portfolio_position_update"
DELETE = "portfolio_position_delete"
OPERATION_TYPES = (CREATE, UPDATE, DELETE)
FIELDS = ("fingerprint_version", "fingerprint_key_version", "user_id", "operation_type", "operation_id",
          "position_id", "expected_revision", "ticker", "quantity", "purchase_price", "target_percent", "envelope")


class PortfolioKeyringError(ValueError):
    """Trousseau portefeuille invalide (refus fermé à la construction)."""


class UnknownPortfolioKey(LookupError):
    """Version de canonicalisation ou de clé introuvable : 503, sans repli."""


class PortfolioKeyring:
    """keys = {version: clé de 32 octets} ; current = version des nouvelles
    écritures. Ne lit aucune variable d'environnement ; repr() ne révèle
    aucune clé."""

    __slots__ = ("_keys", "_current")

    def __init__(self, keys: dict, current: str):
        if type(keys) is not dict or not keys:
            raise PortfolioKeyringError("trousseau portefeuille : au moins une clé")
        seen = set()
        for version, key in keys.items():
            if type(version) is not str or not KEY_VERSION_RE.fullmatch(version):
                raise PortfolioKeyringError("trousseau portefeuille : version invalide")
            if type(key) is not bytes or len(key) != KEY_BYTES:
                raise PortfolioKeyringError("trousseau portefeuille : clé de 32 octets attendue")
            if key in seen:
                raise PortfolioKeyringError("trousseau portefeuille : une clé distincte par version")
            seen.add(key)
        if type(current) is not str or current not in keys:
            raise PortfolioKeyringError("trousseau portefeuille : version courante absente")
        self._keys = MappingProxyType(dict(keys))
        self._current = current

    def __repr__(self) -> str:
        return f"PortfolioKeyring(<{len(self._keys)}>)"

    def __reduce__(self):
        raise TypeError("PortfolioKeyring n'est pas sérialisable")

    @property
    def current_version(self) -> str:
        return self._current

    def versions(self) -> tuple:
        return tuple(sorted(self._keys))

    def key(self, version) -> bytes:
        key = self._keys.get(version) if type(version) is str else None
        if key is None:
            raise UnknownPortfolioKey("clé portefeuille : version inconnue")
        return key

    def has_key_value(self, key: bytes) -> bool:
        return any(hmac.compare_digest(key, k) for k in self._keys.values())


def _field(value) -> bytes:
    if value is None:
        payload = b"\x00"
    elif type(value) is str:
        payload = b"\x01" + value.encode("utf-8")
    else:
        raise TypeError("champ de trame : chaîne ou None")
    return struct.pack(">I", len(payload)) + payload


def _text(value):
    """Entier canonique ou UUID canonique -> texte ; chaîne ou None inchangée."""
    if value is None or type(value) is str:
        return value
    if type(value) is int:
        return str(value)
    if type(value) is uuid.UUID:
        return str(value)
    raise TypeError("champ de trame : type inattendu")


def frame_v1(*, key_version, user_id, operation_type, operation_id, position_id, expected_revision, ticker,
             quantity, purchase_price, target_percent, envelope) -> bytes:
    if operation_type not in OPERATION_TYPES or type(key_version) is not str \
            or not KEY_VERSION_RE.fullmatch(key_version):
        raise ValueError("trame : type d'opération ou version de clé invalide")
    values = (FINGERPRINT_VERSION, key_version, user_id, operation_type, operation_id, position_id,
              expected_revision, ticker, quantity, purchase_price, target_percent, envelope)
    return PREFIX + b"".join(_field(_text(v)) for v in values)


FRAMES = MappingProxyType({FINGERPRINT_VERSION: frame_v1})


def fingerprint(keyring: PortfolioKeyring, fingerprint_version, key_version, **fields) -> str:
    """HMAC hexadécimal minuscule de la trame de la version donnée ;
    UnknownPortfolioKey si la version ou la clé est introuvable."""
    frame = FRAMES.get(fingerprint_version) if type(fingerprint_version) is str else None
    if frame is None:
        raise UnknownPortfolioKey("canonicalisation portefeuille : version inconnue")
    key = keyring.key(key_version)
    return hmac.new(key, frame(key_version=key_version, **fields), hashlib.sha256).hexdigest()


def same_fingerprint(computed: str, recorded) -> bool:
    return type(recorded) is str and FINGERPRINT_RE.fullmatch(recorded) is not None and hmac.compare_digest(
        computed.encode("ascii"), recorded.encode("ascii"))
