"""
Résolveur de ticker dynamique via EODHD Search API.

Pipeline :
1. Cache en mémoire (process-level)
2. Passthrough si l'input contient un point (ex: MC.PA, ALO.PA)
3. Passthrough US si l'input ressemble à un ticker US pur (1-5 lettres)
4. Sinon : recherche EODHD avec priorité basée sur le pays de l'entreprise
5. Fallback : retourne l'input brut si la recherche échoue
"""

import os
import re
import requests
from typing import Optional

EODHD_API_KEY = os.environ.get("EOD_API_KEY", "")
EODHD_SEARCH_URL = "https://eodhd.com/api/search/{query}"

# Cache en mémoire : input_normalisé → ticker_résolu
_RESOLUTION_CACHE: dict = {}

# Exchanges PEA-éligibles (priorité haute si entreprise européenne)
PEA_EXCHANGES = ["PA", "AS", "BR", "LS", "MC", "MI", "XETRA", "DE", "F",
                  "IR", "HE", "CO", "ST", "VI"]

# Exchanges acceptables hors PEA
OTHER_EXCHANGES = ["LSE", "L", "SW", "OL", "US"]

ALLOWED_TYPES = ("Common Stock", "Preferred Stock", "ETF", "Fund", "Mutual Fund")

# Pattern ticker US pur : 1-5 lettres majuscules, optionnellement avec un point
# pour classes d'actions (BRK.B, BF.B), pas de chiffres.
US_TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _looks_like_eu_ticker(s: str) -> bool:
    """Détecte un ticker européen avec suffixe d'exchange (MC.PA, ALO.PA, ASML.AS)."""
    if "." not in s or " " in s or len(s) > 15:
        return False
    parts = s.split(".")
    if len(parts) != 2:
        return False
    suffix = parts[1]
    return suffix in PEA_EXCHANGES or suffix in OTHER_EXCHANGES


def _looks_like_us_ticker(s: str) -> bool:
    """
    Détecte un ticker US pur (AAPL, NVDA, BRK.B).
    On vérifie ensuite via EODHD que c'est bien un vrai ticker US,
    pour éviter de confondre avec un nom court (LVMH a 4 lettres aussi).
    """
    return bool(US_TICKER_PATTERN.match(s))


def _eodhd_search(query: str) -> Optional[list]:
    """Appelle EODHD Search et retourne la liste brute des résultats."""
    if not EODHD_API_KEY:
        print("[RESOLVER] EODHD_API_KEY missing, skip search")
        return None

    try:
        url = EODHD_SEARCH_URL.format(query=query)
        params = {"api_token": EODHD_API_KEY, "limit": 15}
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[RESOLVER] EODHD search failed for '{query}': {type(e).__name__}: {e}")
        return None


def _pick_best_match(results: list, prefer_us: bool = False) -> Optional[str]:
    """
    Sélectionne le meilleur résultat selon la priorité.

    Si prefer_us=True : on cherche d'abord un listing US.
    Sinon : on priorise par pays de l'entreprise (USA → US, sinon PEA → autres).
    """
    if not results:
        return None

    # Filtrer par type
    filtered = [r for r in results if r.get("Type") in ALLOWED_TYPES]
    if not filtered:
        filtered = results

    def rank(item):
        exchange = item.get("Exchange", "")
        country = item.get("Country", "")
        # Si l'entreprise est américaine, on veut le listing US
        if country == "USA":
            if exchange == "US":
                return 0
            return 100  # autres listings US-companies (ADR etc) en dernier
        # Sinon priorité PEA
        if exchange in PEA_EXCHANGES:
            return 1 + PEA_EXCHANGES.index(exchange)
        if exchange in OTHER_EXCHANGES:
            return 50 + OTHER_EXCHANGES.index(exchange)
        return 999

    # Si l'utilisateur a tapé un ticker US pur, on force la préférence US
    if prefer_us:
        us_matches = [r for r in filtered if r.get("Exchange") == "US"]
        if us_matches:
            filtered = us_matches

    filtered.sort(key=rank)
    best = filtered[0]
    code = best.get("Code", "")
    exchange = best.get("Exchange", "")
    name = best.get("Name", "")

    if not code:
        return None

    # Format US : ticker nu (AAPL, pas AAPL.US)
    # Format autre : CODE.EXCHANGE (MC.PA, ASML.AS)
    if exchange == "US":
        ticker = code
    elif exchange:
        ticker = f"{code}.{exchange}"
    else:
        ticker = code

    print(f"[RESOLVER] resolved '{name}' → '{ticker}' (exchange={exchange}, country={best.get('Country')})")
    return ticker


def normalize_ticker(raw: str) -> str:
    """
    Résout un input utilisateur en ticker EODHD canonique.

    Args:
        raw: input utilisateur (ex: "LVMH", "alstom", "alo.pa", "MC.PA", "AAPL", "Apple")

    Returns:
        Ticker normalisé pour EODHD/FMP/yfinance.
    """
    if not raw:
        return ""

    cleaned = raw.strip().upper()

    # 1. Cache
    if cleaned in _RESOLUTION_CACHE:
        return _RESOLUTION_CACHE[cleaned]

    # 2. Ticker EU déjà formé (contient un suffixe d'exchange connu) → passthrough
    if _looks_like_eu_ticker(cleaned):
        _RESOLUTION_CACHE[cleaned] = cleaned
        print(f"[RESOLVER] '{cleaned}' looks like EU ticker, passthrough")
        return cleaned

    # 3. Ticker US pur (AAPL, NVDA, BRK.B) → vérification via EODHD
    #    On force la préférence US pour éviter qu'un nom européen court
    #    (ex: ALO seul) soit traité comme un ticker US par erreur.
    if _looks_like_us_ticker(cleaned):
        results = _eodhd_search(cleaned)
        if results:
            us_match = _pick_best_match(results, prefer_us=True)
            if us_match:
                _RESOLUTION_CACHE[cleaned] = us_match
                return us_match
        # Si pas trouvé en US, fallback recherche normale
        if results:
            generic_match = _pick_best_match(results, prefer_us=False)
            if generic_match:
                _RESOLUTION_CACHE[cleaned] = generic_match
                return generic_match

    # 4. Recherche EODHD générique (priorité pays d'origine)
    results = _eodhd_search(cleaned)
    if results:
        match = _pick_best_match(results, prefer_us=False)
        if match:
            _RESOLUTION_CACHE[cleaned] = match
            return match

    # 5. Fallback : retour brut
    _RESOLUTION_CACHE[cleaned] = cleaned
    print(f"[RESOLVER] '{cleaned}' no match, fallback raw")
    return cleaned
