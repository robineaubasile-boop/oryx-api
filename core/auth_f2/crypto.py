"""Protocole cryptographique normatif F2-C2-V1 (C.2.1, parties IV et V).

Fonctions pures, sans état, sans accès base ni réseau. Les clés sont
passées explicitement (32 octets) : la résolution des versions relève de
core.auth_f2.keys, jamais d'une clé « courante » implicite.

Encodage normatif :

  FRAME(tag, f1, ..., fn) = ASCII("ORYX-F2-C2") || 0x00
                            || UINT16_BE(len(ASCII(tag))) || ASCII(tag)
                            || pour chaque champ : UINT32_BE(len(f)) || f

UUID : 16 octets (ordre réseau). Entiers : UINT32_BE. Tags, versions,
finalités et codes OTP : ASCII strict (le code OTP n'est jamais converti
en entier : « 00123456 » garde ses zéros initiaux). Empreintes : HEX
minuscule de HMAC-SHA256. Secrets transportés : base64url sans
remplissage (43 caractères pour 32 octets).

Les vecteurs officiels V1-V4 sont vérifiés par tests/test_f2_crypto.py
contre les valeurs fixées par C.2.1 et contre une implémentation de
référence indépendante (OpenSSL).
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import uuid

FRAME_PREFIX = b"ORYX-F2-C2\x00"
DERIVATION_PROTOCOL = "f2-c2-hkdf-sha256-v1"
KEY_BYTES = 32
SECRET_BYTES = 32
SECRET_B64_LENGTH = 43
OTP_PURPOSES = ("sign_in", "email_linking")

_KEY_VERSION_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_OTP_RE = re.compile(r"[0-9]{8}")
_B64URL_43_RE = re.compile(r"[A-Za-z0-9_-]{43}")


class CryptoContractError(ValueError):
    """Entrée hors contrat F2-C2-V1 (type, longueur, encodage, version)."""


class Redacted:
    """Conteneur d'un secret (secret candidat, secret actif, jeton
    d'invitation) : jamais affiché par repr()/str(), jamais sérialisé
    implicitement. La valeur n'est lisible que par .reveal()."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise CryptoContractError("secret transporté : chaîne attendue")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Redacted(<secret>)"

    __str__ = __repr__

    def __eq__(self, other) -> bool:
        return isinstance(other, Redacted) and hmac.compare_digest(self._value, other._value)

    def __hash__(self):
        raise TypeError("Redacted n'est pas hachable")

    def __reduce__(self):
        raise TypeError("Redacted n'est pas sérialisable")


# --------------------------------------------------------------------------
# Champs et FRAME
# --------------------------------------------------------------------------

def ascii_field(value: str) -> bytes:
    if not isinstance(value, str):
        raise CryptoContractError("champ ASCII : chaîne attendue")
    try:
        return value.encode("ascii")
    except UnicodeEncodeError:
        raise CryptoContractError("champ ASCII : caractère hors ASCII") from None


def uuid_field(value: uuid.UUID) -> bytes:
    if not isinstance(value, uuid.UUID):
        raise CryptoContractError("champ UUID : uuid.UUID attendu")
    return value.bytes


def uint32_field(value: int) -> bytes:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise CryptoContractError("champ UINT32 : entier 0..2^32-1 attendu")
    return struct.pack(">I", value)


def frame(tag: str, *fields: bytes) -> bytes:
    tag_bytes = ascii_field(tag)
    if not tag_bytes or len(tag_bytes) > 0xFFFF:
        raise CryptoContractError("tag FRAME : longueur invalide")
    out = [FRAME_PREFIX, struct.pack(">H", len(tag_bytes)), tag_bytes]
    for field in fields:
        if not isinstance(field, bytes):
            raise CryptoContractError("champ FRAME : octets attendus")
        out.append(uint32_field(len(field)))
        out.append(field)
    return b"".join(out)


def _key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise CryptoContractError("clé serveur : exactement 32 octets attendus")
    return key


def _secret(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) != SECRET_BYTES:
        raise CryptoContractError("secret : exactement 32 octets attendus")
    return value


def _hmac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


# --------------------------------------------------------------------------
# Formules normatives
# --------------------------------------------------------------------------

def candidate_hash(k_candidate: bytes, operation_id: uuid.UUID, candidate_id: uuid.UUID, secret: bytes) -> str:
    """HEX(HMAC(K_candidate, FRAME("candidate", O, G, C)))."""
    message = frame("candidate", uuid_field(operation_id), uuid_field(candidate_id), _secret(secret))
    return _hmac(_key(k_candidate), message).hex()


def derive_active_secret(k_derive: bytes, k_derive_version: str, operation_id: uuid.UUID,
                         candidate_id: uuid.UUID, session_id: uuid.UUID, generation_id: uuid.UUID,
                         generation_no: int, secret: bytes) -> bytes:
    """A = HMAC(PRK, INFO || 0x01) avec PRK = HMAC(K_derive, C) : premier
    bloc HKDF-SHA256 (sel K_derive), 32 octets."""
    prk = _hmac(_key(k_derive), _secret(secret))
    info = frame(
        "active-derive",
        ascii_field(DERIVATION_PROTOCOL),
        ascii_field(require_key_version(k_derive_version)),
        uuid_field(operation_id),
        uuid_field(candidate_id),
        uuid_field(session_id),
        uuid_field(generation_id),
        uint32_field(generation_no),
    )
    return _hmac(prk, info + b"\x01")


def active_secret_hash(k_active: bytes, session_id: uuid.UUID, generation_id: uuid.UUID, active: bytes) -> str:
    """HEX(HMAC(K_active, FRAME("active", S, R, A)))."""
    message = frame("active", uuid_field(session_id), uuid_field(generation_id), _secret(active))
    return _hmac(_key(k_active), message).hex()


def otp_hmac(k_otp: bytes, request_id: uuid.UUID, code_generation: int, purpose: str, code: str) -> str:
    """HEX(HMAC(K_otp, FRAME("otp", Q, UINT32_BE(n), ASCII(purpose), ASCII(code))))."""
    if purpose not in OTP_PURPOSES:
        raise CryptoContractError("finalité OTP inconnue")
    if not is_valid_otp_code(code):
        raise CryptoContractError("code OTP : huit chiffres ASCII attendus")
    message = frame("otp", uuid_field(request_id), uint32_field(code_generation), ascii_field(purpose),
                    ascii_field(code))
    return _hmac(_key(k_otp), message).hex()


def invitation_hash(k_invitation: bytes, token: bytes) -> str:
    """HEX(HMAC(K_invitation, FRAME("invitation", token))) ; token = 32 octets."""
    return _hmac(_key(k_invitation), frame("invitation", _secret(token))).hex()


def abuse_scope_hash(k_abuse: bytes, scope_kind: str, normalized_scope: str) -> str:
    """HEX(HMAC(K_abuse, FRAME("abuse", ASCII(scope_kind), ASCII(scope))))."""
    if scope_kind not in ("email", "ip"):
        raise CryptoContractError("portée anti-abus inconnue")
    message = frame("abuse", ascii_field(scope_kind), ascii_field(normalized_scope))
    return _hmac(_key(k_abuse), message).hex()


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------

def require_key_version(version: str) -> str:
    if not isinstance(version, str) or not _KEY_VERSION_RE.fullmatch(version):
        raise CryptoContractError("version de clé invalide")
    return version


def is_valid_otp_code(code) -> bool:
    """Exactement huit chiffres ASCII ('0'..'9'), sans blanc ni fin de
    ligne ; les chiffres Unicode (« ٣ », « ３ ») sont refusés."""
    return isinstance(code, str) and len(code) == 8 and _OTP_RE.fullmatch(code) is not None


def is_hex64(value) -> bool:
    return isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None


def hashes_equal(left, right) -> bool:
    """Comparaison en temps constant de deux empreintes bien formées ; une
    empreinte mal formée n'est jamais égale à rien."""
    if not (is_hex64(left) and is_hex64(right)):
        return False
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(_secret(value)).rstrip(b"=").decode("ascii")


def b64url_decode_secret(value) -> bytes:
    """Décodage strict d'un secret transporté : 43 caractères base64url,
    32 octets, réencodage identique (aucun bit de remplissage parasite)."""
    if not isinstance(value, str) or not _B64URL_43_RE.fullmatch(value):
        raise CryptoContractError("secret transporté : 43 caractères base64url attendus")
    raw = base64.urlsafe_b64decode(value + "=")
    if len(raw) != SECRET_BYTES or b64url_encode(raw) != value:
        raise CryptoContractError("secret transporté : encodage non canonique")
    return raw


def derivation_version(k_derive_version: str) -> str:
    """Valeur de oryx_session_generations.derivation_version (D6)."""
    return f"{DERIVATION_PROTOCOL}:{require_key_version(k_derive_version)}"


def parse_derivation_version(value) -> str:
    """Version de K_derive enregistrée ; tout autre protocole est refusé."""
    prefix = DERIVATION_PROTOCOL + ":"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise CryptoContractError("derivation_version : protocole inconnu")
    return require_key_version(value[len(prefix):])


def new_secret() -> bytes:
    """32 octets du CSPRNG du système (secrets.token_bytes)."""
    return secrets.token_bytes(SECRET_BYTES)
