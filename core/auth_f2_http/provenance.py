"""Contrôle de provenance des requêtes F2 (R1 PR 3).

Règle unique pour GET /csrf/bootstrap et pour toute mutation POST :

- Origin présent (une seule fois) : il doit être EXACTEMENT l'une des
  origines autorisées (comparaison de chaînes ; « null » refusé). Si
  Sec-Fetch-Site est présent, il doit valoir same-origin ou same-site.
- Origin absent : la requête n'est acceptée que si sa provenance même
  origine est attestée : Sec-Fetch-Site = same-origin (en-tête interdit
  aux scripts, posé par le navigateur), origine « https://<Host> »
  autorisée, et, si Referer est présent, son origine est cette même
  origine. Toute autre combinaison (Sec-Fetch-Site absent, cross-site,
  none, Referer étranger ou illisible, en-têtes dupliqués) est refusée.

L'origine retenue est liée au jeton CSRF (csrf.py) : un jeton émis pour
une origine ne vaut pas pour une autre.
"""
import re
from urllib.parse import urlsplit

_ORIGIN_RE = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?")
_HOST_RE = re.compile(r"[a-z0-9.-]+(:[0-9]{1,5})?")


class ProvenanceRejected(Exception):
    """Provenance inconnue, ambiguë ou hostile."""


def validate_allowed_origins(origins) -> frozenset:
    if isinstance(origins, str):
        raise ValueError("origines autorisées : collection attendue")
    origins = tuple(origins)
    if not origins:
        raise ValueError("origines autorisées : au moins une")
    for origin in origins:
        if not isinstance(origin, str) or not _ORIGIN_RE.fullmatch(origin) or origin.endswith(":443"):
            raise ValueError("origine autorisée : https://<hôte>[:port] canonique attendue")
    if len(set(origins)) != len(origins):
        raise ValueError("origines autorisées : doublon")
    return frozenset(origins)


def _single(headers, name: str):
    values = headers.getlist(name)
    if len(values) > 1:
        raise ProvenanceRejected(f"en-tête {name} dupliqué")
    return values[0] if values else None


def _referer_origin(referer: str):
    try:
        parts = urlsplit(referer)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return f"https://{parts.hostname}" + (f":{port}" if port not in (None, 443) else "")


def request_origin(headers, allowed: frozenset) -> str:
    """Origine autorisée de la requête, ou ProvenanceRejected."""
    origin = _single(headers, "origin")
    fetch_site = _single(headers, "sec-fetch-site")
    referer = _single(headers, "referer")
    host = _single(headers, "host")
    if origin is not None:
        if origin not in allowed:
            raise ProvenanceRejected("origine non autorisée")
        if fetch_site is not None and fetch_site not in ("same-origin", "same-site"):
            raise ProvenanceRejected("Sec-Fetch-Site incompatible")
        return origin
    if fetch_site != "same-origin" or host is None or not _HOST_RE.fullmatch(host):
        raise ProvenanceRejected("provenance même origine non attestée")
    derived = f"https://{host}"
    if derived not in allowed:
        raise ProvenanceRejected("hôte non autorisé")
    if referer is not None and _referer_origin(referer) != derived:
        raise ProvenanceRejected("Referer étranger")
    return derived
