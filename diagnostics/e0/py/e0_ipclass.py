"""E0 — classification réseau V / I-a / I-b / I-c / I-d / F (miroir Python du Worker).

Code TEMPORAIRE et ISOLÉ du runtime Oryx (protocole E0 V0.1c § 6.4 ;
spécification V0.2-R3 § 2.3, étape 1) : bibliothèque standard seulement.

Étape 1 SEULEMENT : la classification ne dépend d'AUCUNE donnée déclarée par
la sonde. Les en-têtes d'engagement (x-e0-cmp-pre-*) ne sont jamais lus ici ;
le contrôle négatif pré-signature (étape 2, purement soustractif) relève
d'e0_ipcompare. Ordre des critères (premier critère vérifié = classe rendue) :

  I-d  hôte d'entrée différent de l'hôte figé de la phase, ou port explicite (PA14)
  I-b  marqueur de sous-requête Worker (CF-Worker ou CF-EW-Via) présent (C9)
  I-c  request.cf.asn = 13335 (indice de refus seulement ; non signée en E0)
  F    CF-Connecting-IP absente, vide, multiple, illisible (grammaire stricte),
       mappée IPv4, de classe E, de documentation ou non globale (table v1)
  I-a  adresse de substitution Cloudflare (liste versionnée, égalité exacte)
  V    candidat V : signature seulement après l'étape 2 (aucun engagement → no-op)

Toute requête non HTTPS est refusée par le Worker avant classification
(403 local) : le miroir la classe F (`not_https`), jamais V.

La liste I-a est le miroir EXACT de config/i_a_addresses.v1.json (égalité
vérifiée par les tests).
"""
import ipaddress
import re
from dataclasses import dataclass

from e0_redact import canonical_text, category, is_global

CLOUDFLARE_ASN = 13335
I_A_ADDRESSES = ("2a06:98c0:3600::103",)
WORKER_MARKER_HEADERS = ("cf-worker", "cf-ew-via")

CLASS_V = "V"
CLASS_IA = "I-a"
CLASS_IB = "I-b"
CLASS_IC = "I-c"
CLASS_ID = "I-d"
CLASS_F = "F"
CLASSES = (CLASS_V, CLASS_IA, CLASS_IB, CLASS_IC, CLASS_ID, CLASS_F)

_IPV4_STRICT = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")
_IPV6_CHARS = re.compile(r"[0-9A-Fa-f:.]{2,45}")
_I_A = frozenset(ipaddress.ip_address(a) for a in I_A_ADDRESSES)


@dataclass(frozen=True)
class ParsedIP:
    address: object
    canonical: str
    was_canonical: bool


def parse_strict(text):
    """Analyse STRICTE d'une adresse nue : aucun espace, crochet, zone, port,
    préfixe ni zéro non significatif en IPv4. Rend ParsedIP ou None. Une
    adresse valide mais non canonique est acceptée et rendue sous sa forme
    canonique (IPv4 décimale stricte, IPv6 RFC 5952), sa canonicité étant
    consignée (U-CANON)."""
    if not isinstance(text, str) or not text:
        return None
    if ":" not in text:
        if not _IPV4_STRICT.fullmatch(text):
            return None
        try:
            address = ipaddress.IPv4Address(text)
        except ValueError:
            return None
    else:
        if not _IPV6_CHARS.fullmatch(text):
            return None
        try:
            address = ipaddress.IPv6Address(text)
        except ValueError:
            return None
    canonical = canonical_text(address)
    return ParsedIP(address, canonical, canonical == text)


def address_reason(address):
    """Motif F d'une adresse lisible, ou None si elle reste candidate (V ou I-a)."""
    if address.version == 6 and address.ipv4_mapped is not None:
        return "ip_mapped"
    if address.version == 4 and address in ipaddress.ip_network("240.0.0.0/4"):
        return "ip_class_e"
    if category(address) == "documentation":
        return "ip_documentation"
    if not is_global(address):
        return "ip_non_global"
    return None


def is_i_a(address) -> bool:
    return address in _I_A


@dataclass(frozen=True)
class Classification:
    cls: str
    reason: str
    canonical_ip: object = None   # forme canonique, seulement pour V et I-a ; transitoire
    canonical_input: object = None

    def public(self) -> dict:
        """Résultat publiable : jamais l'adresse."""
        return {"class": self.cls, "reason": self.reason, "canonical_input": self.canonical_input}


def classify(*, scheme, entry_host, entry_port_explicit, expected_entry_host, cf_connecting_ip,
             cf_worker_present, cf_ew_via_present, asn) -> Classification:
    """Étape 1. `cf_connecting_ip` est la valeur telle que la rendrait
    `Headers.get` (doublons concaténés par « , »), ou None si absente."""
    if scheme != "https":
        return Classification(CLASS_F, "not_https")
    if not isinstance(expected_entry_host, str) or not expected_entry_host or entry_host != expected_entry_host:
        return Classification(CLASS_ID, "entry_host_mismatch")
    if entry_port_explicit:
        return Classification(CLASS_ID, "entry_port_explicit")
    if cf_worker_present or cf_ew_via_present:
        return Classification(CLASS_IB, "worker_marker")
    if asn == CLOUDFLARE_ASN:
        return Classification(CLASS_IC, "cloudflare_asn")
    if cf_connecting_ip is None:
        return Classification(CLASS_F, "ip_absent")
    if cf_connecting_ip == "":
        return Classification(CLASS_F, "ip_empty")
    if "," in cf_connecting_ip:
        return Classification(CLASS_F, "ip_multiple")
    parsed = parse_strict(cf_connecting_ip)
    if parsed is None:
        return Classification(CLASS_F, "ip_unparsable")
    reason = address_reason(parsed.address)
    if reason is not None:
        return Classification(CLASS_F, reason, canonical_input=parsed.was_canonical)
    if is_i_a(parsed.address):
        return Classification(CLASS_IA, "cloudflare_substitute", canonical_input=parsed.was_canonical)
    return Classification(CLASS_V, "candidate", canonical_ip=parsed.canonical, canonical_input=parsed.was_canonical)


def headers_get(headers, name):
    """Équivalent de `Headers.get` (Fetch) : valeurs du nom (insensible à la
    casse) concaténées par « , », ou None."""
    values = [value for key, value in headers if key.lower() == name]
    return ", ".join(values) if values else None


def classify_request(view: dict) -> Classification:
    """Classification d'une vue de requête : {scheme, entry_host,
    entry_port_explicit, expected_entry_host, headers: [(nom, valeur)], asn}.
    Les en-têtes d'engagement éventuels sont ignorés par construction."""
    headers = view.get("headers") or ()
    return classify(
        scheme=view.get("scheme"), entry_host=view.get("entry_host"),
        entry_port_explicit=bool(view.get("entry_port_explicit")),
        expected_entry_host=view.get("expected_entry_host"),
        cf_connecting_ip=headers_get(headers, "cf-connecting-ip"),
        cf_worker_present=headers_get(headers, "cf-worker") is not None,
        cf_ew_via_present=headers_get(headers, "cf-ew-via") is not None,
        asn=view.get("asn"))


def signable_address(text):
    """Contrôle de reclassement à l'origine (§ 2.2) : une IP signée n'est
    recevable que si elle est canonique, lisible, globale, non mappée, hors
    classe E, hors documentation et hors liste I-a. Rend la forme canonique
    ou None."""
    parsed = parse_strict(text)
    if parsed is None or not parsed.was_canonical:
        return None
    if address_reason(parsed.address) is not None or is_i_a(parsed.address):
        return None
    return parsed.canonical
