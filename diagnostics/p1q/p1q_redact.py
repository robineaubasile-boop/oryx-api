"""P1-Q — classification et pseudonymisation des adresses IP (banc de diagnostic).

ACT-L1 / F04-D / P1-Q. Code TEMPORAIRE et ISOLÉ du runtime Oryx : bibliothèque
standard seulement, aucun import d'un module Oryx.

Règle de confidentialité (protocole P1-Q § 3.3) : aucune adresse IP *globale*
(au sens de `ipaddress.is_global`) n'est jamais émise en clair par le banc. Elle
est remplacée par un pseudonyme `g4:<16 hex>` / `g6:<16 hex>` = HMAC-SHA256
tronqué sous une clé aléatoire tirée au démarrage du processus, jamais écrite
ni exportée. Le pseudonyme est stable pendant la vie du processus (ce qui
permet de comparer deux observations d'un même démarrage) et inutilisable
au-delà (aucune réidentification hors du processus).

Les adresses non globales (documentation RFC 5737 / RFC 3849, privées
RFC 1918, ULA, CGNAT, boucle locale, lien local...) sont des adresses
d'infrastructure ou des valeurs d'essai contrôlées : elles restent en clair,
car elles sont précisément l'objet de la qualification.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets

MAX_VALUE_CHARS = 512

_DOCUMENTATION = tuple(ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32", "3fff::/20"))
_SHARED_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_ULA = ipaddress.ip_network("fc00::/7")

# Candidats d'adresse dans un texte libre : suites de chiffres hexadécimaux,
# de « : » et de « . ». Les quadruplets IPv4 sont recherchés à part, y compris
# à l'intérieur d'un candidat illisible.
_CANDIDATE = re.compile(r"[0-9A-Fa-f:.]+")
_QUAD = re.compile(r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
_PORT_ONLY = re.compile(r"(?::\d{1,5})?")


def parse_plain(text):
    """Adresse IP nue (sans espace, port, crochet ni zone), sinon None."""
    if not isinstance(text, str) or not text or text != text.strip() or "%" in text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def category(address) -> str:
    """Catégorie IANA générique ; aucune plage propre à Railway."""
    if address.version == 6 and address.ipv4_mapped is not None:
        return "ipv4_mapped"
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if any(address.version == n.version and address in n for n in _DOCUMENTATION):
        return "documentation"
    if address.version == 4 and address in _SHARED_CGNAT:
        return "shared_cgnat"
    if address.version == 4 and any(address in n for n in _RFC1918):
        return "private_rfc1918"
    if address.version == 6 and address in _ULA:
        return "unique_local"
    if address.is_global:
        return "global"
    return "non_global_other"


def _is_sensitive(address) -> bool:
    if address.version == 6 and address.ipv4_mapped is not None:
        return _is_sensitive(address.ipv4_mapped)
    return category(address) == "global"


class Redactor:
    """Pseudonymisation par processus. La clé n'est jamais exposée."""

    def __init__(self, key: bytes = None):
        if key is None:
            key = secrets.token_bytes(32)
        if not isinstance(key, bytes) or len(key) < 16:
            raise ValueError("clé de pseudonymisation : au moins 16 octets")
        self.__key = key

    def __repr__(self) -> str:
        return "Redactor(<clé de processus non exportée>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Redactor n'est pas sérialisable")

    def pseudonym(self, address) -> str:
        digest = hmac.new(self.__key, address.packed, hashlib.sha256).hexdigest()[:16]
        return f"g{address.version}:{digest}"

    def address_text(self, address) -> str:
        """Texte publiable d'une adresse : clair si non globale, sinon pseudonyme.
        Une IPv4 mappée globale est pseudonymisée sur son IPv4 interne."""
        if address.version == 6 and address.ipv4_mapped is not None:
            if _is_sensitive(address.ipv4_mapped):
                return "::ffff:" + self.pseudonym(address.ipv4_mapped)
            return address.compressed
        return self.pseudonym(address) if _is_sensitive(address) else address.compressed

    def describe(self, text) -> dict:
        """Description publiable d'une valeur qui devrait être une IP nue."""
        if text is None:
            return None
        address = parse_plain(text)
        if address is None:
            return {"parsed": False, "text": self.redact_text(text), "length": len(text)}
        out = {"parsed": True, "version": address.version, "category": category(address),
               "text": self.address_text(address), "canonical": text == address.compressed,
               "sensitive": _is_sensitive(address)}
        if address.version == 6 and address.ipv4_mapped is not None:
            inner = address.ipv4_mapped
            out["mapped_inner"] = {"category": category(inner), "text": self.address_text(inner)}
        return out

    def describe_tuple(self, value) -> dict:
        """scope["client"] / scope["server"] : (hôte, port) ou None."""
        if value is None:
            return None
        host, port = value[0], value[1] if len(value) > 1 else None
        described = self.describe(host if isinstance(host, str) else repr(host))
        described["port"] = port if isinstance(port, int) else None
        return described

    def _redact_candidate(self, candidate: str) -> str:
        if not any(c.isdigit() for c in candidate):
            # Sans chiffre : ni IPv4, ni IPv6 unicast globale (2000::/3).
            return candidate
        address = parse_plain(candidate)
        if address is not None:
            # Adresse non sensible : forme d'origine conservée (la canonicité est observée).
            return self.address_text(address) if _is_sensitive(address) else candidate

        def quad(match):
            inner = parse_plain(match.group(0))
            if inner is not None and _is_sensitive(inner):
                return "<" + self.pseudonym(inner) + ">"
            return match.group(0)

        redacted = _QUAD.sub(quad, candidate)
        if ":" not in candidate:
            return redacted
        residual = _QUAD.sub("", candidate)
        lowered = candidate.lower()
        if _PORT_ONLY.fullmatch(residual) or lowered.startswith(("2001:db8:", "2001:0db8:", "::ffff:")):
            return redacted
        # Groupe IPv6 illisible : il pourrait contenir un préfixe réel.
        return f"<opaque:{len(candidate)}>"

    def redact_text(self, text) -> str:
        """Texte libre (valeur d'en-tête) dont toute adresse globale est
        pseudonymisée ; tronqué à MAX_VALUE_CHARS ; caractères de contrôle
        remplacés."""
        if not isinstance(text, str):
            text = repr(text)
        clipped = text[:MAX_VALUE_CHARS]
        safe = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in clipped)
        out = _CANDIDATE.sub(lambda m: self._redact_candidate(m.group(0)), safe)
        if len(text) > MAX_VALUE_CHARS:
            out += f"…<+{len(text) - MAX_VALUE_CHARS}>"
        return out

    def matches_commitment(self, address, key_hex: str, macs) -> bool:
        """Vrai si `address` est l'une des adresses engagées par le client sous
        la clé `key_hex` (HMAC-SHA256 de address.packed). Aucune adresse du
        client ne transite en clair."""
        try:
            key = bytes.fromhex(key_hex)
        except (TypeError, ValueError):
            return False
        if len(key) < 16:
            return False
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        mac = hmac.new(key, address.packed, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(mac, m) for m in macs)


def commitment(key: bytes, address_text: str) -> str:
    """Engagement HMAC-SHA256 d'une adresse, calculé côté client."""
    address = ipaddress.ip_address(address_text)
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return hmac.new(key, address.packed, hashlib.sha256).hexdigest()
