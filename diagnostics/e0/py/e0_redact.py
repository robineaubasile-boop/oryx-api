"""E0 — table IANA v1, pseudonymisation HMAC par démarrage, rédaction du texte libre.

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 3.6) :
bibliothèque standard seulement, aucun import d'un module Oryx.

Règle de confidentialité (invariant I6) : aucune adresse IP globale brute
n'est jamais émise. Une adresse n'est écrite en clair QUE si l'entrée la plus
spécifique de la table v1 porte `clear = true` (documentation, privée, CGNAT,
boucle locale, lien local, ULA, classe E…) ; toute autre adresse — globale,
sans entrée, ou embarquant une IPv4 (Teredo, 6to4, NAT64) — est remplacée par
un pseudonyme `g4:<16 hex>` / `g6:<16 hex>` = HMAC-SHA256 tronqué sous une clé
aléatoire tirée au démarrage, jamais exportée ni sérialisable. Une IPv4 mappée
est traitée sur son IPv4 interne.

La table ci-dessous est le miroir EXACT de config/ip_special_registry.v1.json
(égalité vérifiée par les tests) : le conteneur de l'origine ne contient que
le répertoire py/.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets

from e0_version import PSEUDONYM_DOMAIN

MAX_VALUE_CHARS = 512

# (préfixe, globally_reachable, catégorie, clear) — miroir de la table v1.
IPV4_TABLE = (
    ("0.0.0.0/8", False, "this_network", True),
    ("0.0.0.0/32", False, "unspecified", True),
    ("10.0.0.0/8", False, "private", True),
    ("100.64.0.0/10", False, "shared_cgnat", True),
    ("127.0.0.0/8", False, "loopback", True),
    ("169.254.0.0/16", False, "link_local", True),
    ("172.16.0.0/12", False, "private", True),
    ("192.0.0.0/24", False, "ietf_protocol", True),
    ("192.0.0.0/29", False, "ietf_protocol", True),
    ("192.0.0.8/32", False, "ietf_protocol", True),
    ("192.0.0.9/32", True, "anycast_service", False),
    ("192.0.0.10/32", True, "anycast_service", False),
    ("192.0.0.170/32", False, "ietf_protocol", True),
    ("192.0.0.171/32", False, "ietf_protocol", True),
    ("192.0.2.0/24", False, "documentation", True),
    ("192.31.196.0/24", True, "anycast_service", False),
    ("192.52.193.0/24", True, "anycast_service", False),
    ("192.88.99.0/24", None, "deprecated", False),
    ("192.168.0.0/16", False, "private", True),
    ("192.175.48.0/24", True, "anycast_service", False),
    ("198.18.0.0/15", False, "benchmarking", True),
    ("198.51.100.0/24", False, "documentation", True),
    ("203.0.113.0/24", False, "documentation", True),
    ("224.0.0.0/4", False, "multicast", True),
    ("240.0.0.0/4", False, "reserved_class_e", True),
    ("255.255.255.255/32", False, "broadcast", True),
)
IPV6_TABLE = (
    ("::/128", False, "unspecified", True),
    ("::1/128", False, "loopback", True),
    ("::ffff:0:0/96", False, "ipv4_mapped", False),
    ("64:ff9b::/96", True, "nat64", False),
    ("64:ff9b:1::/48", False, "nat64", False),
    ("100::/64", False, "discard", True),
    ("100:0:0:1::/64", False, "discard", True),
    ("2001::/23", False, "ietf_protocol", False),
    ("2001::/32", None, "teredo", False),
    ("2001:1::1/128", True, "anycast_service", False),
    ("2001:1::2/128", True, "anycast_service", False),
    ("2001:1::3/128", True, "anycast_service", False),
    ("2001:2::/48", False, "benchmarking", True),
    ("2001:3::/32", True, "anycast_service", False),
    ("2001:4:112::/48", True, "anycast_service", False),
    ("2001:10::/28", None, "deprecated", False),
    ("2001:20::/28", True, "orchid", False),
    ("2001:30::/28", True, "orchid", False),
    ("2001:db8::/32", False, "documentation", True),
    ("2002::/16", None, "6to4", False),
    ("2620:4f:8000::/48", True, "anycast_service", False),
    ("3fff::/20", False, "documentation", True),
    ("5f00::/16", False, "srv6", False),
    ("fc00::/7", False, "unique_local", True),
    ("fe80::/10", False, "link_local", True),
    ("ff00::/8", False, "multicast", True),
)
IPV6_GLOBAL_UNICAST_SPACE = "2000::/3"

_TABLES = {
    4: tuple((ipaddress.ip_network(p), g, c, k) for p, g, c, k in IPV4_TABLE),
    6: tuple((ipaddress.ip_network(p), g, c, k) for p, g, c, k in IPV6_TABLE),
}
_V6_GLOBAL = ipaddress.ip_network(IPV6_GLOBAL_UNICAST_SPACE)

_CANDIDATE = re.compile(r"[0-9A-Fa-f:.]+")
_QUAD = re.compile(r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
_PORT_ONLY = re.compile(r"(?::\d{1,5})?")


def canonical_text(address) -> str:
    """Forme canonique DÉTERMINISTE, indépendante de la version de Python :
    IPv4 décimale ; IPv6 selon RFC 5952 (minuscules, zéros non significatifs
    retirés, plus longue suite d'au moins deux groupes nuls remplacée par
    « :: », la première en cas d'égalité ; notation mixte `::ffff:a.b.c.d`
    pour une IPv4 mappée, RFC 5952 § 5). `ipaddress` a changé la forme des
    adresses mappées entre deux versions correctives de Python 3.11 : elle
    n'est donc jamais utilisée telle quelle."""
    if address.version == 4:
        return ".".join(str(b) for b in address.packed)
    if address.ipv4_mapped is not None:
        return "::ffff:" + canonical_text(address.ipv4_mapped)
    groups = [int.from_bytes(address.packed[i:i + 2], "big") for i in range(0, 16, 2)]
    best_start, best_len, start = -1, 0, None
    for index in range(9):
        if index < 8 and groups[index] == 0:
            if start is None:
                start = index
        elif start is not None:
            if index - start > best_len:
                best_start, best_len = start, index - start
            start = None
    parts = ["%x" % g for g in groups]
    if best_len >= 2:
        return ":".join(parts[:best_start]) + "::" + ":".join(parts[best_start + best_len:])
    return ":".join(parts)


def most_specific(address):
    """(réseau, globally_reachable, catégorie, clear) de l'entrée la plus
    spécifique contenant l'adresse, ou None."""
    best = None
    for entry in _TABLES[address.version]:
        if address in entry[0] and (best is None or entry[0].prefixlen > best[0].prefixlen):
            best = entry
    return best


def category(address) -> str:
    entry = most_specific(address)
    if entry is not None:
        return entry[2]
    if address.version == 6 and address not in _V6_GLOBAL:
        return "ipv6_unallocated"
    return "global"


def is_global(address) -> bool:
    """Globalité selon la table v1 (règle conservatrice : N/A = non global ;
    IPv6 hors 2000::/3 = non globale)."""
    if address.version == 6 and address not in _V6_GLOBAL:
        return False
    entry = most_specific(address)
    return entry is None or entry[1] is True


def clear_allowed(address) -> bool:
    """Vrai seulement si l'adresse peut être écrite en clair."""
    if address.version == 6 and address.ipv4_mapped is not None:
        return clear_allowed(address.ipv4_mapped)
    entry = most_specific(address)
    return entry is not None and entry[3] is True


def parse_plain(text):
    """Adresse IP nue (sans espace, port, crochet ni zone), sinon None.
    Analyse LARGE réservée à la rédaction ; la grammaire stricte de
    classification est celle d'e0_ipclass."""
    if not isinstance(text, str) or not text or text != text.strip() or "%" in text or "/" in text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


class Redactor:
    """Pseudonymisation par démarrage. La clé n'est jamais exposée ni
    sérialisable ; les pseudonymes ne sont comparables qu'au sein d'un même
    démarrage."""

    def __init__(self, key: bytes = None):
        if key is None:
            key = secrets.token_bytes(32)
        if not isinstance(key, bytes) or len(key) < 16:
            raise ValueError("clé de pseudonymisation : au moins 16 octets")
        self.__key = key

    def __repr__(self) -> str:
        return "Redactor(<clé de démarrage non exportée>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Redactor n'est pas sérialisable")

    def __getstate__(self):
        raise TypeError("Redactor n'est pas sérialisable")

    def pseudonym(self, address) -> str:
        digest = hmac.new(self.__key, PSEUDONYM_DOMAIN + address.packed, hashlib.sha256).hexdigest()[:16]
        return f"g{address.version}:{digest}"

    def address_text(self, address) -> str:
        """Texte publiable : clair seulement si la table v1 l'autorise."""
        if address.version == 6 and address.ipv4_mapped is not None:
            inner = address.ipv4_mapped
            return "::ffff:" + (canonical_text(inner) if clear_allowed(inner) else self.pseudonym(inner))
        return canonical_text(address) if clear_allowed(address) else self.pseudonym(address)

    def describe(self, text):
        """Description publiable d'une valeur censée être une IP nue."""
        if text is None:
            return None
        address = parse_plain(text)
        if address is None:
            return {"parsed": False, "text": self.redact_text(text), "length": len(text)}
        out = {"parsed": True, "version": address.version, "category": category(address),
               "global": is_global(address), "text": self.address_text(address),
               "canonical": text == canonical_text(address)}
        if address.version == 6 and address.ipv4_mapped is not None:
            out["mapped_inner_category"] = category(address.ipv4_mapped)
        return out

    def _redact_candidate(self, candidate: str) -> str:
        if not any(c.isdigit() for c in candidate):
            return candidate
        address = parse_plain(candidate)
        if address is not None:
            return candidate if clear_allowed(address) else self.address_text(address)

        def quad(match):
            inner = parse_plain(match.group(0))
            if inner is not None and not clear_allowed(inner):
                return "<" + self.pseudonym(inner) + ">"
            return match.group(0)

        redacted = _QUAD.sub(quad, candidate)
        if ":" not in candidate:
            return redacted
        residual = _QUAD.sub("", candidate)
        if _PORT_ONLY.fullmatch(residual):
            return redacted
        # Groupe IPv6 illisible : il pourrait contenir un préfixe réel.
        return f"<opaque:{len(candidate)}>"

    def redact_text(self, text) -> str:
        """Texte libre dont toute adresse non autorisée en clair est
        pseudonymisée ; tronqué à MAX_VALUE_CHARS ; caractères de contrôle et
        non ASCII échappés."""
        if not isinstance(text, str):
            text = repr(text)
        clipped = text[:MAX_VALUE_CHARS]
        safe = "".join(c if (c.isprintable() and ord(c) < 128) else f"\\x{ord(c):02x}" for c in clipped)
        out = _CANDIDATE.sub(lambda m: self._redact_candidate(m.group(0)), safe)
        if len(text) > MAX_VALUE_CHARS:
            out += f"…<+{len(text) - MAX_VALUE_CHARS}>"
        return out
