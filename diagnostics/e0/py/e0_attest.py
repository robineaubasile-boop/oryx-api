"""E0 — attestation v1 : encodage, décodage strict, vérificateur par phase (PA2 à PA15).

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 2.2,
§ 5.1) : bibliothèque standard seulement ; cryptographie déléguée à
e0_ed25519 (OpenSSL).

Valeur de l'en-tête `x-oryx-e0-attestation` :

    "v1." || base64url(P) || "." || base64url(σ)      (≤ 4 096 octets)

base64url SANS remplissage et CANONIQUE (le réencodage doit être identique),
σ de 64 octets (86 caractères). P = onze champs dans l'ordre fixe
`v, aud, kid, ts, nonce, ip, c, m, h, hi, t`, chacun codé
`<longueur décimale sans zéro initial>:<octets ASCII>;`, aucun octet après le
dernier. Message signé : b"oryx-e0-attestation/v1\\n" || P.

Vérificateur gardé (ordre PA8) : comptage de l'en-tête → grammaire → `v` →
`aud` → `kid` → bornes → signature Ed25519 (OpenSSL) → fenêtre temporelle →
liaisons pures (méthode, `h`, `hi`, `t`) → reclassement de l'IP → PUIS
SEULEMENT consommation du nonce (magasin en mémoire par démarrage).

Observations élémentaires produites pour chaque requête : `att_present`,
`att_crypto_valid` (clé de l'ensemble courant OU historique d'audit, avec
`kid`, `aud` et origine `current` / `audit`), `att_policy_accepted`. Ce module
ne produit JAMAIS `att_issued_evidence` (conclusion dérivée de l'analyseur).
"""
import base64
import binascii
import re
import threading
from dataclasses import dataclass, field

import e0_ed25519
from e0_ipclass import parse_strict, signable_address
from e0_version import ATTESTATION_DOMAIN, ATTESTATION_FIELDS, ATTESTATION_MAX_BYTES, ATTESTATION_PREFIX, \
    ATTESTATION_VERSION, AUDIENCE_KID_TAG, AUDIENCES, KID_PATTERN, PHASE_AUDIENCE, SIGNED_CLASS

SIGNATURE_B64_CHARS = 86
NONCE_BYTES = 16
MAX_TARGET_BYTES = 2048
MAX_HOST_CHARS = 253

_B64URL = re.compile(r"[A-Za-z0-9_-]*")
_TS = re.compile(r"[1-9][0-9]{12}")
_METHOD = re.compile(r"[A-Z]{1,16}")
_LDH_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TARGET = re.compile(r"/[\x21-\x7e]*")
_LENGTH = re.compile(rb"(0|[1-9][0-9]{0,4}):")


class AttestationFormatError(ValueError):
    """Valeur hors grammaire v1."""


# --------------------------------------------------------------------------
# base64url canonique
# --------------------------------------------------------------------------

def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode_canonical(text: str) -> bytes:
    """Décodage STRICT : alphabet base64url, aucun remplissage, longueur
    valide, réencodage identique (bits de bourrage nuls)."""
    if not isinstance(text, str) or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise AttestationFormatError("base64url invalide")
    padded = text + "=" * (-len(text) % 4)
    try:
        data = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise AttestationFormatError("base64url invalide") from exc
    if b64url_encode(data) != text:
        raise AttestationFormatError("base64url non canonique")
    return data


# --------------------------------------------------------------------------
# Grammaires des champs
# --------------------------------------------------------------------------

def _is_ldh_host(text: str) -> bool:
    if not 1 <= len(text) <= MAX_HOST_CHARS:
        return False
    labels = text.split(".")
    return len(labels) >= 2 and all(_LDH_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit()


def check_field(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.isascii():
        raise AttestationFormatError(f"{name} : ASCII attendu")
    if name == "v":
        ok = value == ATTESTATION_VERSION
    elif name == "aud":
        ok = value in AUDIENCES
    elif name == "kid":
        ok = KID_PATTERN.fullmatch(value) is not None
    elif name == "ts":
        ok = _TS.fullmatch(value) is not None
    elif name == "nonce":
        ok = len(value) == 22 and _is_b64url_of(value, NONCE_BYTES)
    elif name == "ip":
        parsed = parse_strict(value)
        ok = parsed is not None and parsed.was_canonical
    elif name == "c":
        ok = value == SIGNED_CLASS
    elif name == "m":
        ok = _METHOD.fullmatch(value) is not None
    elif name in ("h", "hi"):
        ok = _is_ldh_host(value)
    elif name == "t":
        ok = len(value) <= MAX_TARGET_BYTES and _TARGET.fullmatch(value) is not None
    else:
        raise AttestationFormatError("champ inconnu")
    if not ok:
        raise AttestationFormatError(f"{name} hors grammaire")


def _is_b64url_of(text: str, size: int) -> bool:
    try:
        return len(b64url_decode_canonical(text)) == size
    except AttestationFormatError:
        return False


def check_fields(fields: dict) -> None:
    if not isinstance(fields, dict) or tuple(sorted(fields)) != tuple(sorted(ATTESTATION_FIELDS)):
        raise AttestationFormatError("ensemble de champs v1 attendu")
    for name in ATTESTATION_FIELDS:
        check_field(name, fields[name])
    if AUDIENCE_KID_TAG[fields["aud"]] != KID_PATTERN.fullmatch(fields["kid"]).group(1):
        raise AttestationFormatError("kid incohérent avec aud")


# --------------------------------------------------------------------------
# Encodage et décodage de P
# --------------------------------------------------------------------------

def encode_payload(fields: dict) -> bytes:
    check_fields(fields)
    out = bytearray()
    for name in ATTESTATION_FIELDS:
        raw = fields[name].encode("ascii")
        out += str(len(raw)).encode("ascii") + b":" + raw + b";"
    return bytes(out)


def decode_payload(payload: bytes) -> dict:
    if not isinstance(payload, bytes):
        raise AttestationFormatError("P binaire attendu")
    fields, position = {}, 0
    for name in ATTESTATION_FIELDS:
        match = _LENGTH.match(payload, position)
        if match is None:
            raise AttestationFormatError("longueur de champ invalide")
        length = int(match.group(1))
        start = match.end()
        end = start + length
        if end >= len(payload):
            raise AttestationFormatError("champ tronqué")
        if payload[end:end + 1] != b";":
            raise AttestationFormatError("séparateur de champ absent")
        try:
            fields[name] = payload[start:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AttestationFormatError("champ non ASCII") from exc
        position = end + 1
    if position != len(payload):
        raise AttestationFormatError("octets après le dernier champ")
    check_fields(fields)
    return fields


def signed_message(payload: bytes) -> bytes:
    return ATTESTATION_DOMAIN + payload


def encode_header(payload: bytes, signature: bytes) -> str:
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise AttestationFormatError("signature de 64 octets attendue")
    value = ATTESTATION_PREFIX + b64url_encode(payload) + "." + b64url_encode(signature)
    if len(value) > ATTESTATION_MAX_BYTES:
        raise AttestationFormatError("attestation > 4 096 octets")
    return value


def parse_header(value: str) -> tuple:
    """(payload, champs, signature) ; AttestationFormatError sinon. Aucune
    vérification cryptographique ici (grammaire d'abord, § 3.4)."""
    if not isinstance(value, str) or not value.isascii() or len(value) > ATTESTATION_MAX_BYTES:
        raise AttestationFormatError("valeur d'en-tête illisible ou trop longue")
    if not value.startswith(ATTESTATION_PREFIX):
        raise AttestationFormatError("préfixe v1. absent")
    parts = value[len(ATTESTATION_PREFIX):].split(".")
    if len(parts) != 2 or not parts[0] or len(parts[1]) != SIGNATURE_B64_CHARS:
        raise AttestationFormatError("structure v1.P.σ attendue")
    payload = b64url_decode_canonical(parts[0])
    signature = b64url_decode_canonical(parts[1])
    if len(signature) != 64:
        raise AttestationFormatError("signature de 64 octets attendue")
    return payload, decode_payload(payload), signature


def sign_fields(fields: dict, private_der: bytes) -> str:
    """Attestation complète (outil de test et de vecteurs ; l'origine ne signe jamais)."""
    payload = encode_payload(fields)
    return encode_header(payload, e0_ed25519.sign(private_der, signed_message(payload)))


# --------------------------------------------------------------------------
# Magasin de nonces en mémoire (par démarrage ; distinct du futur banc N2)
# --------------------------------------------------------------------------

class InMemoryNonceStore:
    """Unicité des nonces dans UN processus et UN démarrage. Ce n'est PAS N2
    (aucune persistance, aucune unicité inter-processus) : il ne qualifie
    jamais l'anti-rejeu de F04-D (AC-05)."""

    def __init__(self, max_entries: int = 100000):
        self._lock = threading.Lock()
        self._seen = {}
        self._max = max_entries

    def consume(self, aud: str, kid: str, nonce: str, expires_at_ms: int, now_ms: int) -> bool:
        key = (aud, kid, nonce)
        with self._lock:
            if key in self._seen:
                return False
            if len(self._seen) >= self._max:
                self._seen = {k: e for k, e in self._seen.items() if e >= now_ms}
                if len(self._seen) >= self._max:
                    return False  # capacité épuisée : refus fermé
            self._seen[key] = expires_at_ms
            return True

    def __len__(self):
        with self._lock:
            return len(self._seen)


# --------------------------------------------------------------------------
# Vérificateur par phase
# --------------------------------------------------------------------------

@dataclass
class Evaluation:
    """Observations élémentaires d'une requête (jamais `att_issued_evidence`)."""
    att_present: bool = False
    att_header_count: int = 0
    att_crypto_valid: bool = False
    key_origin: object = None        # "current", "audit" ou None
    att_policy_accepted: bool = False
    refusal: object = None           # code de refus, ou None si acceptée
    aud: object = None
    kid: object = None
    ts: object = None
    nonce: object = None
    h: object = None
    hi: object = None
    t: object = None
    m: object = None
    _signed_ip: object = field(default=None, repr=False)

    def take_signed_ip(self):
        """IP signée, rendue UNE fois puis effacée (calcul transitoire de
        eng_post) ; disponible seulement si la signature est valide."""
        value, self._signed_ip = self._signed_ip, None
        return value

    def record(self) -> dict:
        return {"att_present": self.att_present, "att_header_count": self.att_header_count,
                "att_crypto_valid": self.att_crypto_valid, "key_origin": self.key_origin,
                "att_policy_accepted": self.att_policy_accepted, "refusal": self.refusal,
                "aud": self.aud, "kid": self.kid, "ts": self.ts, "nonce": self.nonce, "h": self.h,
                "hi": self.hi, "t": self.t, "m": self.m}


class PhaseVerifier:
    """Vérificateur de diagnostic de la phase `phase` (PA15) : seules les clés
    courantes de l'audience de la phase peuvent conduire à une acceptation ;
    les clés d'audit ne servent qu'à reconnaître une signature."""

    def __init__(self, *, phase: str, keysets, public_host: str, w_ms: int, s_ms: int, nonce_store, clock_ms):
        if phase not in PHASE_AUDIENCE:
            raise ValueError("phase inconnue")
        if keysets.phase != phase:
            raise ValueError("ensembles de clés d'une autre phase")
        if not _is_ldh_host(public_host):
            raise ValueError("hôte public non conforme")
        if not (isinstance(w_ms, int) and isinstance(s_ms, int) and w_ms > 0 and s_ms >= 0):
            raise ValueError("W et S entiers positifs attendus")
        self.phase = phase
        self.audience = PHASE_AUDIENCE[phase]
        self.keysets = keysets
        self.public_host = public_host
        self.w_ms = w_ms
        self.s_ms = s_ms
        self.nonce_store = nonce_store
        self.clock_ms = clock_ms

    def evaluate(self, header_values, *, method: str, host_values, raw_target: str) -> Evaluation:
        result = Evaluation()
        values = list(header_values or ())
        result.att_header_count = len(values)
        result.att_present = bool(values)
        if not values:
            result.refusal = "att_absent"
            return result
        if len(values) > 1:
            result.refusal = "att_multiple"
            return result
        try:
            payload, fields, signature = parse_header(values[0])
        except AttestationFormatError:
            result.refusal = "grammar"
            return result
        for name in ("aud", "kid", "ts", "nonce", "h", "hi", "t", "m"):
            setattr(result, name, fields[name])

        # Observation cryptographique : ensemble courant puis historique d'audit.
        reference = self.keysets.lookup(fields["kid"])
        if reference is not None and reference.aud == fields["aud"]:
            if e0_ed25519.verify(reference.public_key, signed_message(payload), signature):
                result.att_crypto_valid = True
                result.key_origin = reference.origin
                result._signed_ip = fields["ip"]

        # Politique de la phase courante (ordre PA8).
        now = self.clock_ms()
        if fields["v"] != ATTESTATION_VERSION:
            result.refusal = "grammar"
        elif fields["aud"] != self.audience:
            result.refusal = "aud_not_current_phase"
        elif self.keysets.is_retired(fields["kid"]):
            result.refusal = "kid_retired"
        elif self.keysets.accepting(fields["kid"]) is None:
            result.refusal = "kid_unknown"
        elif not (result.att_crypto_valid and result.key_origin == "current"):
            result.refusal = "sig_invalid"
        elif int(fields["ts"]) - now > self.s_ms:
            result.refusal = "ts_future"
        elif now - int(fields["ts"]) > self.w_ms:
            result.refusal = "ts_expired"
        elif fields["m"] != method:
            result.refusal = "bind_method"
        elif fields["h"] != self.public_host:
            result.refusal = "bind_h"
        elif len(host_values) != 1 or host_values[0] != fields["hi"]:
            result.refusal = "bind_hi"
        elif fields["t"] != raw_target:
            result.refusal = "bind_target"
        elif signable_address(fields["ip"]) != fields["ip"]:
            result.refusal = "ip_not_v"
        elif not self.nonce_store.consume(fields["aud"], fields["kid"], fields["nonce"],
                                          int(fields["ts"]) + self.w_ms + self.s_ms, now):
            result.refusal = "replay_in_boot"
        else:
            result.att_policy_accepted = True
        return result
