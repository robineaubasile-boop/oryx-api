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

# Watchlist de debug : ajoute une requête ici (en majuscules) pour
# voir tous les candidats EODHD bruts dans les logs lors du prochain
# test. Vide par défaut — à remplir ponctuellement, pas laissé actif
# en continu.
_DEBUG_QUERIES: set = {"LVMH"}

# Exchanges PEA-éligibles (priorité haute si entreprise européenne)
PEA_EXCHANGES = ["PA", "AS", "BR", "LS", "MC", "MI", "XETRA", "DE", "F",
                  "IR", "HE", "CO", "ST", "VI"]

# Exchanges acceptables hors PEA
OTHER_EXCHANGES = ["LSE", "L", "SW", "OL", "US"]

ALLOWED_TYPES = ("Common Stock", "Preferred Stock", "ETF", "Fund", "Mutual Fund")

# Filet de sécurité : entreprises connues pour lesquelles la recherche
# EODHD ne renvoie pas fiablement la bonne cotation principale (CDR
# étranger, ADR homonyme, etc.). Vérifié manuellement. Étends cette
# table au cas par cas si un nouveau cas est découvert — ne devine pas
# un ticker sans l'avoir confirmé.
_KNOWN_TICKER_OVERRIDES = {
    "LVMH": "MC.PA",
    "ASML": "ASML.AS",
}

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


_OTC_ADR_SUFFIX_PATTERN = re.compile(r"^[A-Z]{3,5}[YF]$")


def _is_likely_otc_adr(code: str) -> bool:
    """
    Détecte un ticker suivant la convention OTC/ADR US (se termine par
    Y ou F, ex: LVMHF, RNLSY). Ces certificats ne sont presque jamais
    la cotation principale d'une entreprise étrangère.
    """
    return bool(_OTC_ADR_SUFFIX_PATTERN.match(code or ""))


def _fix_mojibake(text: str) -> str:
    """
    Répare le cas classique de double-encodage : du texte UTF-8 source
    mal interprété comme Latin-1 puis ré-encodé (ex: "HermÃ¨s" au lieu
    de "Hermès"). Défensif : si la réparation échoue ou ne change
    rien, retourne le texte original tel quel.
    """
    if not text:
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
        return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


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
        data = r.json()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "Name" in item:
                    item["Name"] = _fix_mojibake(item["Name"])
        return data
    except Exception as e:
        print(f"[RESOLVER] EODHD search failed for '{query}': {type(e).__name__}: {e}")
        return None


def _pick_best_match(results: list, prefer_us: bool = False, query: str = "") -> Optional[str]:
    """
    Sélectionne le meilleur résultat selon la priorité.

    Si prefer_us=True : on cherche d'abord un listing US.
    Sinon : priorité PEA d'abord, puis US authentique, indépendamment
    du champ Country d'EODHD.
    """
    if not results:
        return None

    # Filtrer par type
    filtered = [r for r in results if r.get("Type") in ALLOWED_TYPES]
    if not filtered:
        filtered = results

    if query.upper() in _DEBUG_QUERIES:
        print(f"[RESOLVER-DEBUG] query={query!r} — {len(results)} résultats bruts EODHD :")
        for r in results:
            print(f"[RESOLVER-DEBUG]   code={r.get('Code')!r} exchange={r.get('Exchange')!r} "
                  f"country={r.get('Country')!r} type={r.get('Type')!r} name={r.get('Name')!r}")

    def rank(item):
        exchange = item.get("Exchange", "")
        code = item.get("Code", "")
        item_type = item.get("Type", "")

        # Priorité 1 : cotation sur une place PEA-éligible — c'est le
        # public principal d'Oryx (investisseurs français/européens).
        # On ne se fie plus au champ "Country" d'EODHD pour décider :
        # un certificat OTC/ADR coté aux US peut être étiqueté
        # "Country: USA" alors que l'entreprise est européenne, et un
        # ETF américain sans rapport peut porter le même mot dans son
        # nom (ex: recherche "Hermès" → ETF "Federated Hermes").
        if exchange in PEA_EXCHANGES:
            base_rank = 1 + PEA_EXCHANGES.index(exchange)

        # Priorité 2 : autres places boursières reconnues hors PEA
        # (hors US, traité séparément juste après).
        elif exchange in OTHER_EXCHANGES and exchange != "US":
            base_rank = 50 + OTHER_EXCHANGES.index(exchange)

        # Priorité 3 : action ordinaire cotée US authentique — pas un
        # certificat OTC/ADR (suffixe Y/F), pas un fonds sans rapport.
        elif exchange == "US" and item_type == "Common Stock" and not _is_likely_otc_adr(code):
            base_rank = 100

        # Priorité 4 : tout le reste (ADR/OTC, ETF homonymes, cotations
        # exotiques hors PEA/US) — dernier recours seulement.
        else:
            base_rank = 500

        # Correspondance exacte avec la requête tapée : bonus fort,
        # mais appliqué EN PLUS du rang de base — pas un remplacement.
        # Ça évite qu'une égalité de Code entre deux places (ex: "ASML"
        # coté à la fois à Amsterdam et comme ADR US) ne se départage
        # au hasard de l'ordre brut renvoyé par EODHD : la priorité
        # PEA reste décisive même en cas d'égalité de ticker.
        if code.upper() == query.upper():
            return base_rank - 1000

        return base_rank

    # prefer_us influence désormais uniquement le classement via rank(),
    # plus de pré-filtrage strict qui excluait les bonnes cotations
    # européennes en cas de faux positif (voir _is_likely_otc_adr).

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

    # 1.5 Override codé en dur pour les cas connus non fiables via
    # la recherche EODHD (voir _KNOWN_TICKER_OVERRIDES).
    if cleaned in _KNOWN_TICKER_OVERRIDES:
        resolved = _KNOWN_TICKER_OVERRIDES[cleaned]
        _RESOLUTION_CACHE[cleaned] = resolved
        print(f"[RESOLVER] '{cleaned}' → override codé en dur → '{resolved}'")
        return resolved

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
            us_match = _pick_best_match(results, prefer_us=True, query=cleaned)
            if us_match:
                _RESOLUTION_CACHE[cleaned] = us_match
                return us_match
        # Si pas trouvé en US, fallback recherche normale
        if results:
            generic_match = _pick_best_match(results, prefer_us=False, query=cleaned)
            if generic_match:
                _RESOLUTION_CACHE[cleaned] = generic_match
                return generic_match

    # 4. Recherche EODHD générique (priorité pays d'origine)
    results = _eodhd_search(cleaned)
    if results:
        match = _pick_best_match(results, prefer_us=False, query=cleaned)
        if match:
            _RESOLUTION_CACHE[cleaned] = match
            return match

    # 5. Fallback : retour brut
    _RESOLUTION_CACHE[cleaned] = cleaned
    print(f"[RESOLVER] '{cleaned}' no match, fallback raw")
    return cleaned
