"""Protocole CSRF de test oryx-f2-csrf-v1 (R1 PR 3).

Indépendant des six clés F2 (candidate, derive, active, otp, invitation,
abuse) : trousseau propre, versionné, dont aucune clé ne peut être une clé
F2 (vérifié à la composition, core.auth_f2_http.isolated). Un jeton CSRF
n'est JAMAIS une preuve d'authentification : il atteste seulement qu'une
requête vient d'une page de l'origine autorisée qui a lu la réponse de
GET /csrf/bootstrap.

Liaison : cookie __Host-oryx-csrf-binding = 32 octets aléatoires
(base64url canonique, 43 caractères), HttpOnly.

Jeton : « version.exp.nonce.mac »

  version : version de la clé CSRF, ^[A-Za-z0-9_-]{1,32}$ (sans point) ;
  exp     : instant d'expiration, secondes Unix en décimal canonique
            (sans zéro initial), émis à t + 30 minutes ;
  nonce   : 16 octets aléatoires, base64url canonique sans remplissage ;
  mac     : HMAC-SHA256(K_csrf[version], M), base64url canonique (43).

  M = ASCII("ORYX-F2-CSRF") || 0x00 || UINT16_BE(len(tag)) || tag
      || pour chaque champ : UINT32_BE(len(f)) || f
  tag = "oryx-f2-csrf-v1" ; champs : version, exp (ASCII), nonce (16
  octets), secret de liaison (32 octets), origine de la requête (ASCII).

Vérification (refus fermé) : quatre parties exactement, chaque partie
canonique ; version présente dans le trousseau (aucune substitution par
la version courante) ; now < exp et exp - now <= 30 minutes (un jeton
au-delà de la durée d'émission est refusé) ; MAC comparé en temps
constant.

Rotation explicite : déployer un trousseau dont la version courante est
la nouvelle et qui contient encore l'ancienne (jetons en cours acceptés
au plus 30 minutes), puis retirer l'ancienne : ses jetons sont refusés.
"""
import base64
import hmac
import re
import secrets
import struct
from hashlib import sha256
from types import MappingProxyType

PROTOCOL = "oryx-f2-csrf-v1"
TOKEN_TTL_SECONDS = 1800
NONCE_BYTES = 16
BINDING_BYTES = 32
KEY_BYTES = 32
MAX_TOKEN_CHARS = 160

_PREFIX = b"ORYX-F2-CSRF\x00"
_VERSION_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_EXP_RE = re.compile(r"[1-9][0-9]{0,11}")
_B64_RE = re.compile(r"[A-Za-z0-9_-]+")


class CsrfConfigError(ValueError):
    """Trousseau CSRF invalide."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_canonical(value, nbytes: int):
    """Octets d'une valeur base64url canonique de nbytes octets, ou None."""
    length = (4 * nbytes + 2) // 3
    if not isinstance(value, str) or len(value) != length or not _B64_RE.fullmatch(value):
        return None
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return raw if len(raw) == nbytes and _b64(raw) == value else None


class CsrfKeyring:
    """keys = {version: clé de 32 octets} ; current = version d'émission."""

    __slots__ = ("_keys", "_current")

    def __init__(self, keys: dict, current: str):
        if not isinstance(keys, dict) or not keys:
            raise CsrfConfigError("trousseau CSRF : au moins une clé")
        seen = set()
        for version, key in keys.items():
            if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
                raise CsrfConfigError("trousseau CSRF : version invalide")
            if not isinstance(key, bytes) or len(key) != KEY_BYTES:
                raise CsrfConfigError("trousseau CSRF : clé de 32 octets attendue")
            if key in seen:
                raise CsrfConfigError("trousseau CSRF : une clé par version")
            seen.add(key)
        if current not in keys:
            raise CsrfConfigError("trousseau CSRF : version courante absente")
        self._keys = MappingProxyType(dict(keys))
        self._current = current

    def __repr__(self) -> str:
        return f"CsrfKeyring(<{len(self._keys)}>)"

    def __reduce__(self):
        raise TypeError("CsrfKeyring n'est pas sérialisable")

    @property
    def current_version(self) -> str:
        return self._current

    def versions(self) -> tuple:
        return tuple(sorted(self._keys))

    def has_key_value(self, key: bytes) -> bool:
        return any(hmac.compare_digest(key, k) for k in self._keys.values())

    def _key(self, version):
        return self._keys.get(version) if isinstance(version, str) else None


def new_binding() -> str:
    return _b64(secrets.token_bytes(BINDING_BYTES))


def _message(version: str, exp: str, nonce: bytes, binding: bytes, origin: str) -> bytes:
    tag = PROTOCOL.encode("ascii")
    out = _PREFIX + struct.pack(">H", len(tag)) + tag
    for field in (version.encode("ascii"), exp.encode("ascii"), nonce, binding, origin.encode("ascii")):
        out += struct.pack(">I", len(field)) + field
    return out


def issue_token(keyring: CsrfKeyring, binding: str, origin: str, now: int) -> str:
    raw_binding = _b64_canonical(binding, BINDING_BYTES)
    if raw_binding is None or not isinstance(origin, str) or not origin.isascii():
        raise ValueError("jeton CSRF : liaison ou origine invalide")
    version = keyring.current_version
    exp = str(int(now) + TOKEN_TTL_SECONDS)
    nonce = secrets.token_bytes(NONCE_BYTES)
    mac = hmac.new(keyring._key(version), _message(version, exp, nonce, raw_binding, origin), sha256).digest()
    return ".".join((version, exp, _b64(nonce), _b64(mac)))


def verify_token(keyring: CsrfKeyring, token, binding, origin: str, now: int) -> bool:
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or not token.isascii():
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    version, exp, nonce_b64, mac_b64 = parts
    if not _VERSION_RE.fullmatch(version) or not _EXP_RE.fullmatch(exp):
        return False
    key = keyring._key(version)
    if key is None:
        return False  # version inconnue : aucune substitution
    expires = int(exp)
    if not (int(now) < expires and expires - int(now) <= TOKEN_TTL_SECONDS):
        return False
    nonce = _b64_canonical(nonce_b64, NONCE_BYTES)
    mac = _b64_canonical(mac_b64, 32)
    raw_binding = _b64_canonical(binding, BINDING_BYTES)
    if nonce is None or mac is None or raw_binding is None or not isinstance(origin, str) or not origin.isascii():
        return False
    expected = hmac.new(key, _message(version, exp, nonce, raw_binding, origin), sha256).digest()
    return hmac.compare_digest(expected, mac)
