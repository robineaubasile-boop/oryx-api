"""E0 — contrat versionné de comparaison IP (pré-signature et post-signature).

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 2.3,
§ 2.6, § 3.6) : bibliothèque standard seulement.

Engagement (format v1, vecteurs vectors/commitment_v1.json) :

    MAC = HMAC-SHA256(clé, ASCII(domaine) || 0x0A || ASCII(forme canonique de l'IP))

rendu en 64 caractères hexadécimaux minuscules ; domaines
`oryx-e0-ipcompare/pre/v1` (contrôle négatif du Worker) et
`oryx-e0-ipcompare/post/v1` (engagement post-signature `eng_post`). Les clés
font 32 octets, sont indépendantes des clés de signature et propres à un run.

Pré-signature (étape 2 du Worker, PUREMENT SOUSTRACTIVE) :
  1. détection : engagement présent si l'un au moins des en-têtes
     x-e0-cmp-pre-key / x-e0-cmp-pre-mac est présent ; absent → no-op ;
  2. validation : présent mal formé → refus de signer (`cmp_malformed`) ;
  3. comparaison : discordant → classe F pour l'essai (`cmp_disagreement`) ;
     concordant → aucun effet.
Le résultat ne peut que RETRANCHER : il n'existe aucune issue « promouvoir »
ni « signer ». Un engagement est une donnée contrôlable par le client : il ne
prouve jamais une provenance visiteur.

Post-signature : l'origine calcule eng_post(ip signée), le témoin
eng_post(ip observée) ; la comparaison est faite hors ligne (analyseur). Une
divergence de `cmp_key_post_id` rend la comparaison INCONCLUSIVE.
"""
import hashlib
import hmac
import re
from dataclasses import dataclass

from e0_ipclass import parse_strict
from e0_version import CMP_DISAGREEMENT, CMP_MALFORMED, IPCOMPARE_KEY_ID_DOMAIN, IPCOMPARE_POST_DOMAIN, \
    IPCOMPARE_PRE_DOMAIN

KEY_BYTES = 32
KEY_HEX = re.compile(r"[0-9a-f]{64}")
MAC_LIST = re.compile(r"[0-9a-f]{64}(?:,[0-9a-f]{64})?")
KEY_ID = re.compile(r"cmp-[0-9a-f]{16}")
DOMAINS = (IPCOMPARE_PRE_DOMAIN, IPCOMPARE_POST_DOMAIN)

EFFECT_NONE = "none"
EFFECT_REFUSE = "refuse"


def canonical_ip(text) -> str:
    parsed = parse_strict(text)
    if parsed is None:
        raise ValueError("adresse illisible")
    return parsed.canonical


def mac(key: bytes, domain: str, ip_text: str) -> str:
    if domain not in DOMAINS:
        raise ValueError("domaine de comparaison inconnu")
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError("clé de comparaison : 32 octets")
    message = domain.encode("ascii") + b"\n" + canonical_ip(ip_text).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def key_from_hex(text) -> bytes:
    if not isinstance(text, str) or not KEY_HEX.fullmatch(text):
        raise ValueError("clé de comparaison : 64 hexadécimaux minuscules")
    return bytes.fromhex(text)


def key_id(key: bytes) -> str:
    """Identifiant public d'une clé post-signature : jamais la clé."""
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError("clé de comparaison : 32 octets")
    return "cmp-" + hashlib.sha256(IPCOMPARE_KEY_ID_DOMAIN.encode("ascii") + b"\n" + key).hexdigest()[:16]


# --------------------------------------------------------------------------
# Pré-signature (contrôle négatif)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PreCheck:
    entered: bool
    effect: str            # EFFECT_NONE ou EFFECT_REFUSE ; jamais de promotion
    reason: object = None  # None, CMP_MALFORMED ou CMP_DISAGREEMENT


def pre_signature_check(key_values, mac_values, candidate_ip) -> PreCheck:
    """Étape 2 sur un candidat V. `key_values` et `mac_values` sont les valeurs
    reçues (une par occurrence d'en-tête). Aucun engagement → no-op."""
    key_values = list(key_values or ())
    mac_values = list(mac_values or ())
    if not key_values and not mac_values:
        return PreCheck(False, EFFECT_NONE)
    if len(key_values) != 1 or len(mac_values) != 1:
        return PreCheck(True, EFFECT_REFUSE, CMP_MALFORMED)
    key_text, mac_text = key_values[0], mac_values[0]
    if not isinstance(key_text, str) or not KEY_HEX.fullmatch(key_text) \
            or not isinstance(mac_text, str) or not MAC_LIST.fullmatch(mac_text):
        return PreCheck(True, EFFECT_REFUSE, CMP_MALFORMED)
    committed = mac_text.split(",")
    if len(set(committed)) != len(committed):
        return PreCheck(True, EFFECT_REFUSE, CMP_MALFORMED)
    try:
        observed = mac(bytes.fromhex(key_text), IPCOMPARE_PRE_DOMAIN, candidate_ip)
    except ValueError:
        return PreCheck(True, EFFECT_REFUSE, CMP_MALFORMED)
    agreed = False
    for value in committed:
        agreed = hmac.compare_digest(observed, value) or agreed
    if not agreed:
        return PreCheck(True, EFFECT_REFUSE, CMP_DISAGREEMENT)
    return PreCheck(True, EFFECT_NONE)


# --------------------------------------------------------------------------
# Post-signature
# --------------------------------------------------------------------------

class PostKey:
    """Clé post-signature d'un run, détenue en mémoire. Jamais sérialisable,
    jamais affichée ; seul son identifiant public sort."""

    def __init__(self, key: bytes, expected_id=None):
        if not isinstance(key, bytes) or len(key) != KEY_BYTES:
            raise ValueError("clé de comparaison : 32 octets")
        derived = key_id(key)
        if expected_id is not None and expected_id != derived:
            raise ValueError("cmp_key_post_id incohérent avec la clé")
        self.__key = key
        self.key_id = derived

    def __repr__(self) -> str:
        return f"PostKey({self.key_id})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("PostKey n'est pas sérialisable")

    def __getstate__(self):
        raise TypeError("PostKey n'est pas sérialisable")

    def eng_post(self, ip_text: str) -> str:
        return mac(self.__key, IPCOMPARE_POST_DOMAIN, ip_text)


MATCH = "match"
MISMATCH = "mismatch"
INCONCLUSIVE = "inconclusive"
_ENG = re.compile(r"[0-9a-f]{64}")


def compare_post(origin_eng, origin_key_id, witness_eng, witness_key_id) -> dict:
    """Comparaison hors ligne de deux engagements post-signature. Rend
    {"match": MATCH | MISMATCH | INCONCLUSIVE, "basis": motif}."""
    for value in (origin_eng, witness_eng):
        if not isinstance(value, str) or not _ENG.fullmatch(value):
            return {"match": INCONCLUSIVE, "basis": "engagement_absent_or_malformed"}
    for value in (origin_key_id, witness_key_id):
        if not isinstance(value, str) or not KEY_ID.fullmatch(value):
            return {"match": INCONCLUSIVE, "basis": "key_id_absent_or_malformed"}
    if origin_key_id != witness_key_id:
        return {"match": INCONCLUSIVE, "basis": "cmp_key_post_id_divergent"}
    if hmac.compare_digest(origin_eng, witness_eng):
        return {"match": MATCH, "basis": "eng_post_equal_same_key_id"}
    return {"match": MISMATCH, "basis": "eng_post_different_same_key_id"}
