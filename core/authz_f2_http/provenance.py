"""Provenance des requêtes personnelles (R1 PR 5, C.5.2 §7 ; R1 PR 6).

Politique distincte de core.auth_f2_http.provenance (PR 3, inchangée) :

- Host : présent une seule fois, canonique (minuscules, port explicite
  seulement s'il n'est pas 443) ; l'origine attendue https://<Host> doit
  être EXACTEMENT l'une des origines autorisées ;
- Origin présent : une seule fois, identique à l'origine attendue
  (« null » et toute autre valeur refusés) ;
- Origin absent : Sec-Fetch-Site: same-origin obligatoire ;
- Sec-Fetch-Site présent : uniquement same-origin (same-site, cross-site,
  none refusés) ;
- Referer présent : une seule fois, https, sans identifiants, d'origine
  égale à l'origine attendue ;
- tout en-tête de provenance dupliqué est refusé.

Mutations personnelles (R1 PR 6, PUT /api/v2/me/level) : mêmes règles,
mais Origin est OBLIGATOIRE et exactement égal à l'origine dérivée du Host
(Sec-Fetch-Site seul ne suffit jamais) ; Sec-Fetch-Site, s'il est présent,
vaut exclusivement same-origin.

Aucun CORS : la réponse n'est destinée qu'à la même origine.
"""
import re
from urllib.parse import urlsplit

from core.auth_f2_http.provenance import ProvenanceRejected

_HOST_RE = re.compile(r"[a-z0-9.-]+(:[0-9]{1,5})?")


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


def personal_get_origin(headers, allowed: frozenset) -> str:
    """Origine attendue de la requête, ou ProvenanceRejected."""
    return _personal_origin(headers, allowed, origin_required=False)


def personal_mutation_origin(headers, allowed: frozenset) -> str:
    """Origine attendue d'une mutation personnelle (Origin obligatoire), ou
    ProvenanceRejected."""
    return _personal_origin(headers, allowed, origin_required=True)


def _personal_origin(headers, allowed: frozenset, *, origin_required: bool) -> str:
    host = _single(headers, "host")
    origin = _single(headers, "origin")
    fetch_site = _single(headers, "sec-fetch-site")
    referer = _single(headers, "referer")
    if host is None or not _HOST_RE.fullmatch(host) or host.endswith(":443"):
        raise ProvenanceRejected("Host non canonique")
    expected = f"https://{host}"
    if expected not in allowed:
        raise ProvenanceRejected("hôte non autorisé")
    if origin is None and origin_required:
        raise ProvenanceRejected("Origin exigé")
    if origin is not None and origin != expected:
        raise ProvenanceRejected("Origin différente de l'origine attendue")
    if fetch_site is not None and fetch_site != "same-origin":
        raise ProvenanceRejected("Sec-Fetch-Site incompatible")
    if origin is None and fetch_site is None:
        raise ProvenanceRejected("provenance même origine non attestée")
    if referer is not None and _referer_origin(referer) != expected:
        raise ProvenanceRejected("Referer étranger")
    return expected
