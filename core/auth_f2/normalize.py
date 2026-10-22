"""Normalisation canonique des portées F2 (C.2.1-A, D1 et D2).

Une seule valeur canonique par adresse e-mail (Supabase, PostgreSQL,
opérations, quotas) et par origine IP (quotas). Toute entrée hors
contrat est refusée (refus fermé), jamais « réparée » au-delà des règles
ci-dessous.
"""
import ipaddress

EMAIL_MAX_BYTES = 254
_ASCII_BLANKS = " \t\n\r\x0b\x0c"


class InvalidEmail(ValueError):
    """Adresse e-mail hors contrat D1."""


class InvalidClientIp(ValueError):
    """Origine IP absente, non fiable ou hors contrat D2 : refus fermé."""


def normalize_email(raw) -> str:
    """D1 : blancs ASCII retirés aux extrémités, majuscules ASCII en
    minuscules ; refus des caractères non ASCII, des contrôles, des blancs
    internes, d'un nombre de '@' différent de un, d'une partie vide, et
    au-delà de 254 octets. Aucun traitement d'alias ('+', points conservés).

    Le résultat satisfait les CHECK de 0017 :
    v = lower(btrim(v)) AND v ~ '^[^@[:space:]]+@[^@[:space:]]+$'."""
    if not isinstance(raw, str):
        raise InvalidEmail("e-mail : chaîne attendue")
    value = raw.strip(_ASCII_BLANKS)
    if not value.isascii():
        raise InvalidEmail("e-mail : caractère non ASCII")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        raise InvalidEmail("e-mail : blanc ou caractère de contrôle")
    value = value.lower()
    if value.count("@") != 1:
        raise InvalidEmail("e-mail : exactement un '@' attendu")
    local, domain = value.split("@")
    if not local or not domain:
        raise InvalidEmail("e-mail : partie locale ou domaine vide")
    if len(value.encode("ascii")) > EMAIL_MAX_BYTES:
        raise InvalidEmail("e-mail : plus de 254 octets")
    return value


def normalize_client_ip(raw) -> str:
    """D2 : 'ipv4:<a.b.c.d>' ou 'ipv6:<préfixe>/64'. IPv4 mappée en IPv6
    ramenée à IPv4 ; identifiant de zone refusé ; valeur absente refusée.
    La provenance (chaîne de proxys approuvée) relève de l'adaptateur HTTP
    futur : ce service ne lit jamais X-Forwarded-For."""
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "%" in raw:
        raise InvalidClientIp("IP client absente ou non fiable")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        raise InvalidClientIp("IP client invalide") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address):
        return f"ipv4:{address.compressed}"
    network = ipaddress.IPv6Network((address, 64), strict=False)
    return f"ipv6:{network.network_address.compressed}/64"
