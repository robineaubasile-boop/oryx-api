"""Codec PUR des lectures personnelles R1 PR 8 (aucune base, aucun réseau,
aucune requête HTTP) : query stricte, curseurs versionnés, dates, corps
JSON et bornes de restitution.

Query (request.scope["query_string"], 2048 octets bruts au plus) : octets
ASCII visibles seulement ; segments séparés par « & », chacun « nom=valeur »
(segment vide, sans « = » ou de nom vide : refus) ; un SEUL décodage
formulaire : « + » signifie espace, %HH strict (deux chiffres
hexadécimaux), puis UTF-8 strict (aucun U+FFFD, aucune normalisation
Unicode) ; noms comparés après décodage ; paramètre répété ou inconnu,
valeur vide : refus. Paramètres : limit (« [1-9][0-9]? », 1 à 50, défaut
25), cursor, et pour /theses seulement ticker (filtre exact : non vide, 32
caractères au plus, sans blanc de bord, jamais corrigé).

Curseur : JSON canonique UTF-8 (clés triées, séparateurs compacts,
ensure_ascii=False, ni NaN ni Infinity) encodé en Base64URL sans padding,
1024 caractères ASCII au plus. Le décodage exige qu'un réencodage
canonique reproduise EXACTEMENT la chaîne reçue (alphabet, bits de
remplissage, octets UTF-8, ordre des clés, échappements, blancs), sans
doublon ni champ inconnu, types exacts, version 1 et route attendue :

- analysis-overview : {"last_ticker": <chaîne exacte, éventuellement
  vide>, "route": "analysis-overview", "v": 1} ;
- theses : {"last_created_at": <date publique ou null>, "last_thesis_id":
  <1 à 2147483647>, "route": "theses", "ticker": <filtre courant exact ou
  null>, "v": 1}.

Un curseur ne porte aucun user_id, secret, cookie ni droit : il n'est
jamais utilisé pour déterminer l'appartenance des données.

Dates : format fermé YYYY-MM-DDTHH:MM:SS.ffffffZ (six décimales toujours
présentes) ; TIMESTAMP sans fuseau lu comme UTC historique (aucune
conversion depuis le fuseau du serveur), TIMESTAMPTZ converti en UTC ;
date d'un curseur reconstruite en datetime naïf EXACT (même valeur de tri
SQL, même précision).

Restitution : corps JSON canonique UTF-8 de 131072 octets au plus (tout
compris) ; curseur suivant construit depuis la DERNIÈRE entrée rendue,
jamais depuis la ligne supplémentaire ; curseur suivant de plus de 1024
caractères, valeur non représentable ou page incohérente avec sa
commande : Unrestorable (503), jamais de page partielle ni de troncature.
"""
import base64
import binascii
import json
import re
from datetime import datetime, timezone

from core.authz_f2 import commands as c

MAX_QUERY_BYTES = 2048
MAX_CURSOR_CHARS = 1024
MAX_RESPONSE_BYTES = 131072
DEFAULT_LIMIT = 25
CURSOR_VERSION = 1
OVERVIEW_ROUTE = "analysis-overview"
THESES_ROUTE = "theses"
OVERVIEW_PARAMS = frozenset({"limit", "cursor"})
THESES_PARAMS = frozenset({"limit", "cursor", "ticker"})
OVERVIEW_CURSOR_KEYS = frozenset({"last_ticker", "route", "v"})
THESES_CURSOR_KEYS = frozenset({"last_created_at", "last_thesis_id", "route", "ticker", "v"})

_QUERY_BYTES_RE = re.compile(rb"[\x21-\x7e]*")
_HEX_RE = re.compile(rb"[0-9A-Fa-f]{2}")
_LIMIT_RE = re.compile(r"[1-9][0-9]?", re.ASCII)
_THESIS_ID_RE = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_TIMESTAMP_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{6})Z",
                           re.ASCII)


class InvalidRequest(ValueError):
    """Paramètre, encodage, curseur ou identifiant invalide : 400."""


class Unrestorable(ValueError):
    """Résultat non restituable dans le contrat public : 503."""


# --------------------------------------------------------------------------
# JSON canonique et dates
# --------------------------------------------------------------------------

def canonical_json(obj) -> bytes:
    try:
        return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise Unrestorable("JSON non sérialisable") from None


def render_timestamp(value):
    """None -> None ; datetime naïf (UTC historique) ou avec fuseau (converti
    en UTC) -> YYYY-MM-DDTHH:MM:SS.ffffffZ ; sinon Unrestorable."""
    if value is None:
        return None
    if type(value) is not datetime:
        raise Unrestorable("date hors contrat")
    if value.tzinfo is not None:
        if value.utcoffset() is None:
            raise Unrestorable("fuseau hors contrat")
        try:
            value = value.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            raise Unrestorable("date hors représentation") from None
    if not 1 <= value.year <= 9999:
        raise Unrestorable("date hors représentation")
    return (f"{value.year:04d}-{value.month:02d}-{value.day:02d}T{value.hour:02d}:{value.minute:02d}:"
            f"{value.second:02d}.{value.microsecond:06d}Z")


def parse_timestamp(text) -> datetime:
    """Date publique -> datetime naïf exact ; toute autre forme : InvalidRequest."""
    match = _TIMESTAMP_RE.fullmatch(text) if type(text) is str else None
    if match is None:
        raise InvalidRequest("date hors format")
    try:
        value = datetime(*(int(part) for part in match.groups()))
    except ValueError:
        raise InvalidRequest("date impossible") from None
    if render_timestamp(value) != text:
        raise InvalidRequest("date non canonique")
    return value


# --------------------------------------------------------------------------
# Query stricte
# --------------------------------------------------------------------------

def _form_decode(raw: bytes) -> str:
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte == 0x25:  # %HH
            pair = raw[index + 1:index + 3]
            if not _HEX_RE.fullmatch(pair):
                raise InvalidRequest("percent-encoding invalide")
            out.append(int(pair, 16))
            index += 3
        else:
            out.append(0x20 if byte == 0x2B else byte)  # « + » : espace (formulaire)
            index += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidRequest("UTF-8 invalide") from None


def decode_query(raw, allowed: frozenset) -> dict:
    """{nom: valeur} décodés une seule fois ; refus de tout écart."""
    if type(raw) is not bytes or len(raw) > MAX_QUERY_BYTES or not _QUERY_BYTES_RE.fullmatch(raw):
        raise InvalidRequest("query brute hors contrat")
    params = {}
    if not raw:
        return params
    for segment in raw.split(b"&"):
        raw_name, separator, raw_value = segment.partition(b"=")
        if not separator or not raw_name:
            raise InvalidRequest("segment de query invalide")
        name, value = _form_decode(raw_name), _form_decode(raw_value)
        if name in params or name not in allowed:
            raise InvalidRequest("paramètre répété ou inconnu")
        if not value:
            raise InvalidRequest("valeur vide")
        params[name] = value
    return params


def parse_limit(text) -> int:
    if text is None:
        return DEFAULT_LIMIT
    if not _LIMIT_RE.fullmatch(text) or not c.valid_page_limit(int(text)):
        raise InvalidRequest("limit hors contrat")
    return int(text)


def parse_thesis_id(text) -> int:
    """Identifiant de chemin : [1-9][0-9]{0,9}, 2147483647 au plus."""
    if type(text) is not str or not _THESIS_ID_RE.fullmatch(text) or not c.valid_thesis_id(int(text)):
        raise InvalidRequest("thesis_id hors contrat")
    return int(text)


# --------------------------------------------------------------------------
# Curseurs versionnés
# --------------------------------------------------------------------------

def encode_cursor(obj: dict) -> str:
    encoded = base64.urlsafe_b64encode(canonical_json(obj)).rstrip(b"=").decode("ascii")
    if len(encoded) > MAX_CURSOR_CHARS:
        raise Unrestorable("curseur trop long")
    return encoded


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("clé JSON dupliquée")
        out[key] = value
    return out


def _reject_constant(_):
    raise ValueError("constante JSON refusée")


def decode_cursor(text, route: str, keys: frozenset) -> dict:
    """Objet du curseur, ou InvalidRequest (forme, canonicité, schéma,
    version, route)."""
    if type(text) is not str or len(text) > MAX_CURSOR_CHARS or not _BASE64URL_RE.fullmatch(text) \
            or len(text) % 4 == 1:
        raise InvalidRequest("curseur hors alphabet ou hors borne")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise InvalidRequest("Base64URL invalide") from None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text:
        raise InvalidRequest("Base64URL non canonique")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidRequest("JSON de curseur invalide") from None
    if type(obj) is not dict or set(obj) != keys:
        raise InvalidRequest("schéma de curseur invalide")
    try:
        canonical = canonical_json(obj)
    except Unrestorable:
        raise InvalidRequest("curseur non canonique") from None
    if canonical != raw:
        raise InvalidRequest("curseur non canonique")
    if type(obj["v"]) is not int or obj["v"] != CURSOR_VERSION or type(obj["route"]) is not str \
            or obj["route"] != route:
        raise InvalidRequest("version ou route de curseur")
    return obj


# --------------------------------------------------------------------------
# Requêtes -> commandes
# --------------------------------------------------------------------------

def overview_command(raw_query) -> c.ListAnalysisOverviewPage:
    params = decode_query(raw_query, OVERVIEW_PARAMS)
    limit = parse_limit(params.get("limit"))
    after = None
    if "cursor" in params:
        after = decode_cursor(params["cursor"], OVERVIEW_ROUTE, OVERVIEW_CURSOR_KEYS)["last_ticker"]
        if not c.valid_cursor_ticker(after):
            raise InvalidRequest("last_ticker hors contrat")
    try:
        return c.ListAnalysisOverviewPage(limit, after)
    except ValueError:
        raise InvalidRequest("commande hors contrat") from None


def theses_command(raw_query) -> c.ListThesesPage:
    params = decode_query(raw_query, THESES_PARAMS)
    limit = parse_limit(params.get("limit"))
    ticker = params.get("ticker")
    if ticker is not None and not c.valid_ticker_filter(ticker):
        raise InvalidRequest("ticker hors contrat")
    after_id = after_date = None
    if "cursor" in params:
        cursor = decode_cursor(params["cursor"], THESES_ROUTE, THESES_CURSOR_KEYS)
        # Le filtre fait partie du curseur : tout changement est refusé.
        if (cursor["ticker"] is not None and type(cursor["ticker"]) is not str) or cursor["ticker"] != ticker:
            raise InvalidRequest("filtre incompatible avec le curseur")
        if not c.valid_thesis_id(cursor["last_thesis_id"]):
            raise InvalidRequest("last_thesis_id hors contrat")
        after_id = cursor["last_thesis_id"]
        if cursor["last_created_at"] is not None:
            after_date = parse_timestamp(cursor["last_created_at"])
    try:
        return c.ListThesesPage(limit, ticker, after_id, after_date)
    except ValueError:
        raise InvalidRequest("commande hors contrat") from None


# --------------------------------------------------------------------------
# Corps des réponses 200 (validés, sérialisés, bornés)
# --------------------------------------------------------------------------

def _bounded(body: bytes) -> bytes:
    if len(body) > MAX_RESPONSE_BYTES:
        raise Unrestorable("réponse trop volumineuse")
    return body


def _next_page(page, command) -> bool:
    """Une page suivante n'existe que si limit entrées sont rendues ; une
    page plus longue que limit est incohérente."""
    if len(page.entries) > command.limit or (page.has_more and len(page.entries) != command.limit):
        raise Unrestorable("pagination incohérente")
    return page.has_more


def overview_body(page, command) -> bytes:
    if type(page) is not c.AnalysisOverviewPage or type(command) is not c.ListAnalysisOverviewPage:
        raise Unrestorable("synthèse hors contrat")
    if command.after_ticker is not None and page.entries and not page.entries[0].ticker > command.after_ticker:
        raise Unrestorable("page antérieure au curseur")
    entries = [{"ticker": e.ticker, "state": e.state,
                "analysis_session_id": None if e.analysis_session_id is None else str(e.analysis_session_id),
                "current_step": e.current_step, "updated_at": render_timestamp(e.updated_at),
                "latest_thesis_id": e.latest_thesis_id, "diagnostics": list(e.diagnostics)}
               for e in page.entries]
    cursor = None
    if _next_page(page, command):
        cursor = encode_cursor({"last_ticker": page.entries[-1].ticker, "route": OVERVIEW_ROUTE,
                                "v": CURSOR_VERSION})
    return _bounded(canonical_json({"entries": entries, "next_cursor": cursor}))


def theses_body(page, command) -> bytes:
    if type(page) is not c.ThesesPage or type(command) is not c.ListThesesPage:
        raise Unrestorable("thèses hors contrat")
    if command.ticker is not None and any(e.ticker != command.ticker for e in page.entries):
        raise Unrestorable("thèse hors filtre")
    if command.after_thesis_id is not None and page.entries and not c.thesis_precedes(
            command.after_created_at, command.after_thesis_id, page.entries[0].created_at, page.entries[0].thesis_id):
        raise Unrestorable("page antérieure au curseur")
    theses = [{"thesis_id": e.thesis_id, "ticker": e.ticker, "created_at": render_timestamp(e.created_at),
               "provenance_status": e.provenance_status} for e in page.entries]
    cursor = None
    if _next_page(page, command):
        last = page.entries[-1]
        cursor = encode_cursor({"last_created_at": render_timestamp(last.created_at), "last_thesis_id": last.thesis_id,
                                "route": THESES_ROUTE, "ticker": command.ticker, "v": CURSOR_VERSION})
    return _bounded(canonical_json({"next_cursor": cursor, "theses": theses}))


def thesis_body(view, thesis_id: int) -> bytes:
    if type(view) is not c.ThesisDetailView or view.thesis_id != thesis_id:
        raise Unrestorable("thèse hors contrat")
    session_id = None if view.analysis_session_id is None else str(view.analysis_session_id)
    return _bounded(canonical_json({
        "thesis_id": view.thesis_id, "ticker": view.ticker, "thesis_text": view.thesis_text,
        "created_at": render_timestamp(view.created_at),
        "provenance": {"status": view.provenance_status, "analysis_session_id": session_id}}))
