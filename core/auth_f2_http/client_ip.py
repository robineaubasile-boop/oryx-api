"""Résolution de l'IP client des routes F2 (R1 PR 3, C.2.1-A D2).

Un X-Forwarded-For librement fourni n'est JAMAIS une preuve. Le résolveur
est injecté par la composition ; il rend une adresse IP textuelle (que le
moteur normalise selon D2) ou lève ClientIpUnavailable : la route refuse
alors sans appeler le moteur (refus fermé).

- PeerAddressResolver : adresse du pair TCP (scope ASGI « client »).
  Valable seulement si l'application est exposée sans proxy.
- TrustedProxyResolver : le pair TCP doit appartenir à un réseau de
  proxys APPROUVÉS ; X-Forwarded-For (un seul en-tête) est alors lu de
  droite à gauche en sautant les proxys approuvés, et la première
  adresse non approuvée est l'IP client. Pair non approuvé, en-tête
  absent, dupliqué, trop long ou contenant une valeur qui n'est pas une
  adresse IP nue : refus.

Railway : la chaîne de proxys de Railway (adresses des pairs, nombre de
sauts, en-tête fiable) n'est PAS établie dans PR 3. Aucune configuration
de confiance n'est fournie ni supposée : c'est un prérequis bloquant
avant toute activation réelle (docs/f2_r1_pr3.md).
"""
import ipaddress

MAX_FORWARDED_CHARS = 1024
MAX_FORWARDED_ENTRIES = 16


class ClientIpUnavailable(Exception):
    """IP client absente ou non fiable : refus fermé."""


def _plain_ip(value):
    if not isinstance(value, str) or not value or value != value.strip() or "%" in value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _peer(request):
    client = request.client
    address = _plain_ip(client.host) if client is not None else None
    if address is None:
        raise ClientIpUnavailable("pair TCP inconnu")
    return address


class PeerAddressResolver:
    def __call__(self, request) -> str:
        return _peer(request).compressed

    def __repr__(self) -> str:
        return "PeerAddressResolver()"


class TrustedProxyResolver:
    def __init__(self, trusted_networks, *, header: str = "x-forwarded-for"):
        if isinstance(trusted_networks, str):
            raise ValueError("réseaux de proxys : collection attendue")
        networks = tuple(ipaddress.ip_network(n, strict=True) for n in trusted_networks)
        if not networks:
            raise ValueError("réseaux de proxys : au moins un")
        if not isinstance(header, str) or header.lower() != header or not header:
            raise ValueError("en-tête de proxy : nom en minuscules attendu")
        self._networks = networks
        self._header = header

    def __repr__(self) -> str:
        return f"TrustedProxyResolver(<{len(self._networks)} réseaux>)"

    def _trusted(self, address) -> bool:
        return any(address.version == n.version and address in n for n in self._networks)

    def __call__(self, request) -> str:
        if not self._trusted(_peer(request)):
            raise ClientIpUnavailable("pair non approuvé")
        values = request.headers.getlist(self._header)
        if len(values) != 1 or len(values[0]) > MAX_FORWARDED_CHARS:
            raise ClientIpUnavailable("en-tête de proxy absent ou ambigu")
        entries = [e.strip(" \t") for e in values[0].split(",")]
        if len(entries) > MAX_FORWARDED_ENTRIES:
            raise ClientIpUnavailable("chaîne de proxys trop longue")
        addresses = [_plain_ip(e) for e in entries]
        if any(a is None for a in addresses):
            raise ClientIpUnavailable("valeur de proxy illisible")
        for address in reversed(addresses):
            if not self._trusted(address):
                return address.compressed
        raise ClientIpUnavailable("aucune adresse client")
