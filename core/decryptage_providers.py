"""Transports fournisseurs du futur Décrypter versionné (R1 PR 9-B2) — DORMANTS.

Aucun module applicatif (api.py, workers, scripts) ne les importe ; aucun
appel réseau, aucune lecture de variable d'environnement à l'import ni à la
construction : les clés et le modèle sont fournis explicitement par la
future composition (PR 9-C). Fournisseurs fermés : EODHD (recherche,
fondamentaux, temps réel), FMP (profil, ratios), Anthropic (Messages).
yfinance n'est ni importé ni atteignable ; core.data_fetcher (qui l'atteint)
n'est pas utilisé.

Contrat commun (utilisé par l'orchestrateur, qui ne fait que valider) :

- timeout_s : temps maximal de l'appel, déjà borné par le budget B2 ;
  timeout_s <= 0 : TimeoutError SANS appel ;
- connexion bornée à min(5 s, timeout_s), chaque lecture à min(5 s,
  timeout_s), corps lu par morceaux avec contrôle de l'échéance monotone
  après chaque morceau (dépassement maximal : une lecture, couvert par la
  marge de 5 s du budget) et taille maximale ;
- aucune redirection, aucun retry (HTTPAdapter max_retries=0 ; client
  Anthropic max_retries=0, revérifié par with_options à chaque appel) ;
- 404 : None (introuvable) ; autre statut, corps trop gros, JSON invalide
  (NaN / Infinity refusés) : ConnectionError / ValueError ; délai :
  TimeoutError ;
- RIEN n'est journalisé : ni URL (porteuse de la clé), ni corps, ni clé.
"""
import json
import time
from urllib.parse import quote

import anthropic
import httpx2
import requests
from requests.adapters import HTTPAdapter

EODHD_BASE_URL = "https://eodhd.com/api"
FMP_BASE_URL = "https://financialmodelingprep.com/stable"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 5.0
CHUNK_BYTES = 64 * 1024
SEARCH_MAX_BYTES = 256 * 1024
FUNDAMENTALS_MAX_BYTES = 12 * 1024 * 1024
SMALL_MAX_BYTES = 1024 * 1024
SEARCH_LIMIT = 15  # valeur historique de core.ticker_resolver


def _reject_constant(name):
    raise ValueError("constante JSON non finie refusée")


def _session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_json(session, url: str, params: dict, timeout_s: float, max_bytes: int, *, clock=time.monotonic):
    """GET JSON borné (voir le contrat du module)."""
    if type(timeout_s) not in (int, float) or not timeout_s > 0:
        raise TimeoutError("aucun temps disponible")
    deadline = clock() + timeout_s
    timeouts = (min(CONNECT_TIMEOUT_SECONDS, timeout_s), min(READ_TIMEOUT_SECONDS, timeout_s))
    try:
        response = session.get(url, params=params, timeout=timeouts, stream=True, allow_redirects=False)
    except requests.exceptions.Timeout:
        raise TimeoutError("délai fournisseur dépassé") from None
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(type(exc).__name__) from None
    try:
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ConnectionError(f"statut fournisseur {response.status_code}")
        body = bytearray()
        try:
            for chunk in response.iter_content(CHUNK_BYTES):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError("réponse fournisseur trop volumineuse")
                if clock() > deadline:
                    raise TimeoutError("délai fournisseur dépassé")
        except requests.exceptions.Timeout:
            raise TimeoutError("délai fournisseur dépassé") from None
        except requests.exceptions.RequestException as exc:
            raise ConnectionError(type(exc).__name__) from None
        if clock() > deadline:
            raise TimeoutError("délai fournisseur dépassé")
        return json.loads(bytes(body).decode("utf-8"), parse_constant=_reject_constant)
    finally:
        response.close()


class EodhdTransport:
    __slots__ = ("_key", "_session", "_base", "_clock")

    def __init__(self, api_key: str, *, session=None, base_url: str = EODHD_BASE_URL, clock=time.monotonic):
        if type(api_key) is not str or not api_key:
            raise ValueError("clé EODHD attendue")
        self._key, self._session, self._base, self._clock = api_key, session or _session(), base_url, clock

    def __repr__(self) -> str:
        return "EodhdTransport(<key>)"

    def __reduce__(self):
        raise TypeError("transport non sérialisable")

    def _get(self, path, params, timeout_s, max_bytes):
        return get_json(self._session, f"{self._base}/{path}", {"api_token": self._key, "fmt": "json", **params},
                        timeout_s, max_bytes, clock=self._clock)

    def search(self, query: str, *, timeout_s: float):
        return self._get(f"search/{quote(query, safe='')}", {"limit": SEARCH_LIMIT}, timeout_s, SEARCH_MAX_BYTES)

    def fundamentals(self, symbol: str, *, timeout_s: float):
        return self._get(f"fundamentals/{quote(symbol, safe='')}", {}, timeout_s, FUNDAMENTALS_MAX_BYTES)

    def realtime(self, symbol: str, *, timeout_s: float):
        return self._get(f"real-time/{quote(symbol, safe='')}", {}, timeout_s, SMALL_MAX_BYTES)


class FmpTransport:
    __slots__ = ("_key", "_session", "_base", "_clock")

    def __init__(self, api_key: str, *, session=None, base_url: str = FMP_BASE_URL, clock=time.monotonic):
        if type(api_key) is not str or not api_key:
            raise ValueError("clé FMP attendue")
        self._key, self._session, self._base, self._clock = api_key, session or _session(), base_url, clock

    def __repr__(self) -> str:
        return "FmpTransport(<key>)"

    def __reduce__(self):
        raise TypeError("transport non sérialisable")

    def _get(self, endpoint, symbol, timeout_s):
        return get_json(self._session, f"{self._base}/{endpoint}", {"symbol": symbol, "apikey": self._key},
                        timeout_s, SMALL_MAX_BYTES, clock=self._clock)

    def profile(self, symbol: str, *, timeout_s: float):
        return self._get("profile", symbol, timeout_s)

    def ratios(self, symbol: str, *, timeout_s: float):
        return self._get("ratios", symbol, timeout_s)


class ClaudeTransport:
    """Messages API, UN appel par create_message, aucun retry SDK."""

    __slots__ = ("_client",)

    def __init__(self, api_key: str, *, http_client=None):
        if type(api_key) is not str or not api_key:
            raise ValueError("clé Anthropic attendue")
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0,
                                           timeout=httpx2.Timeout(105.0, connect=CONNECT_TIMEOUT_SECONDS),
                                           http_client=http_client)

    def __repr__(self) -> str:
        return "ClaudeTransport(<key>)"

    def __reduce__(self):
        raise TypeError("transport non sérialisable")

    @property
    def max_retries(self) -> int:
        return self._client.max_retries

    def create_message(self, *, model: str, system: str, user: str, max_tokens: int, timeout_s: float) -> dict:
        if type(timeout_s) not in (int, float) or not timeout_s > 0:
            raise TimeoutError("aucun temps disponible")
        timeout = httpx2.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_s))
        try:
            response = self._client.with_options(max_retries=0).messages.create(
                model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
                timeout=timeout)
        except anthropic.APITimeoutError:
            raise TimeoutError("délai Claude dépassé") from None
        except anthropic.APIError as exc:
            raise ConnectionError(type(exc).__name__) from None
        blocks = []
        for block in response.content:
            kind = getattr(block, "type", None)
            blocks.append({"type": kind, "text": block.text} if kind == "text" else {"type": kind})
        return {"stop_reason": response.stop_reason, "content": blocks}
