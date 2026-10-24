"""Cookies F2 : noms, lecture stricte, écriture (R1 PR 3).

Formats (PR 2, docs/f2_r1_pr2.md §9.6) :

  __Host-oryx-candidate-<candidate_id>  -> secret du CandidateCredential
  __Host-oryx-active-<generation_id>    -> secret du SessionCredential
  __Host-oryx-csrf-binding              -> secret de liaison CSRF (csrf.py)

Attributs : Secure, HttpOnly, Path=/, sans Domain, SameSite=Lax (tests
même origine). Les identifiants des noms sont des SÉLECTEURS non secrets :
un cookie n'est jamais choisi implicitement ; la route reçoit l'identifiant
exact dans le corps JSON, lit le cookie de ce nom et le moteur F2 juge le
justificatif.

Lecture (refus fermé de toute ambiguïté) :

- au plus un en-tête Cookie, 8 Ko au plus ;
- tout nom commençant par « __host-oryx- » (casse ignorée, comme les
  navigateurs pour le préfixe __Host-) doit être EXACTEMENT l'un des trois
  formats ci-dessus, UUID canonique (minuscules) ;
- 16 cookies __Host-oryx-* au plus ; un nom en double est refusé (jamais
  « le premier » ni « le dernier ») ;
- chaque valeur est un secret de 43 caractères base64url canonique
  (32 octets) ;
- les autres cookies sont ignorés.
"""
import math
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType

from core.auth_f2.crypto import CryptoContractError, b64url_decode_secret

ORYX_PREFIX = "__Host-oryx-"
CANDIDATE_PREFIX = ORYX_PREFIX + "candidate-"
ACTIVE_PREFIX = ORYX_PREFIX + "active-"
CSRF_BINDING = ORYX_PREFIX + "csrf-binding"

MAX_COOKIE_HEADER_BYTES = 8192
MAX_ORYX_COOKIES = 16

# Conservation du cookie candidat : 15 minutes au plus depuis la création
# du candidat (10 minutes d'authentification + 5 minutes de reprise après
# un commit au plus tard). Jamais renouvelée par un renvoi ; n'étend
# jamais les échéances PostgreSQL (le moteur les juge seul).
CANDIDATE_COOKIE_TTL = timedelta(minutes=15)
# Liaison CSRF : 30 minutes (valeur proposée par l'architecture), durée
# DISTINCTE de celle du jeton (csrf.TOKEN_TTL_SECONDS, 30 minutes). Chaque
# bootstrap repose le MÊME secret avec une nouvelle durée de 30 minutes :
# la liaison survit toujours au dernier jeton émis (et donc à tous les
# jetons encore valides des autres onglets), puis disparaît 30 minutes au
# plus après le dernier bootstrap.
CSRF_BINDING_TTL = timedelta(minutes=30)

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class CookieHeaderRejected(Exception):
    """En-tête Cookie hors contrat : la requête est refusée."""


def canonical_uuid(value):
    """UUID canonique (36 caractères minuscules) ou None."""
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        return None
    parsed = uuid.UUID(value)
    return parsed if str(parsed) == value else None


def is_canonical_secret(value) -> bool:
    try:
        b64url_decode_secret(value)
    except CryptoContractError:
        return False
    return True


@dataclass(frozen=True)
class OryxCookies:
    candidates: MappingProxyType
    actives: MappingProxyType
    csrf_binding: object
    count: int


EMPTY = OryxCookies(MappingProxyType({}), MappingProxyType({}), None, 0)


def parse_cookie_headers(values) -> OryxCookies:
    """values : liste des en-têtes Cookie reçus (Starlette getlist)."""
    if not values:
        return EMPTY
    if len(values) != 1:
        raise CookieHeaderRejected("plusieurs en-têtes Cookie")
    raw = values[0]
    if len(raw.encode("latin-1", errors="replace")) > MAX_COOKIE_HEADER_BYTES:
        raise CookieHeaderRejected("en-tête Cookie trop long")
    candidates, actives, binding, seen = {}, {}, None, set()
    for piece in raw.split(";"):
        piece = piece.strip(" \t")
        if not piece:
            continue
        name, sep, value = piece.partition("=")
        name, value = name.strip(" \t"), value.strip(" \t")
        if not name.lower().startswith(ORYX_PREFIX.lower()):
            continue
        if not sep or name in seen:
            raise CookieHeaderRejected("cookie Oryx ambigu")
        seen.add(name)
        if len(seen) > MAX_ORYX_COOKIES:
            raise CookieHeaderRejected("trop de cookies Oryx")
        if not is_canonical_secret(value):
            raise CookieHeaderRejected("valeur de cookie Oryx hors contrat")
        if name == CSRF_BINDING:
            binding = value
        elif name.startswith(CANDIDATE_PREFIX) and canonical_uuid(name[len(CANDIDATE_PREFIX):]):
            candidates[canonical_uuid(name[len(CANDIDATE_PREFIX):])] = value
        elif name.startswith(ACTIVE_PREFIX) and canonical_uuid(name[len(ACTIVE_PREFIX):]):
            actives[canonical_uuid(name[len(ACTIVE_PREFIX):])] = value
        else:
            raise CookieHeaderRejected("nom de cookie Oryx inconnu")
    return OryxCookies(MappingProxyType(candidates), MappingProxyType(actives), binding, len(seen))


def candidate_name(candidate_id: uuid.UUID) -> str:
    return CANDIDATE_PREFIX + str(candidate_id)


def active_name(generation_id: uuid.UUID) -> str:
    return ACTIVE_PREFIX + str(generation_id)


def set_cookie(name: str, value: str, max_age_seconds: float) -> str:
    """Valeur d'un en-tête Set-Cookie ; Max-Age arrondi vers le bas (jamais
    au-delà de la borne calculée par l'appelant)."""
    if not is_canonical_secret(value):
        raise ValueError("cookie Oryx : secret canonique attendu")
    max_age = math.floor(max_age_seconds)
    if max_age <= 0:
        raise ValueError("cookie Oryx : durée positive attendue")
    return f"{name}={value}; Max-Age={max_age}; Path=/; Secure; HttpOnly; SameSite=Lax"


def clear_cookie(name: str) -> str:
    return f"{name}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"
