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
import unicodedata
from typing import Optional

EODHD_API_KEY = os.environ.get("EOD_API_KEY", "")
EODHD_SEARCH_URL = "https://eodhd.com/api/search/{query}"

# Cache en mémoire : input_normalisé → ticker_résolu
_RESOLUTION_CACHE: dict = {}

# Watchlist de debug : ajoute une requête ici (en majuscules) pour
# voir tous les candidats EODHD bruts dans les logs lors du prochain
# test. Vide par défaut — à remplir ponctuellement, pas laissé actif
# en continu.
_DEBUG_QUERIES: set = {
    # CAC40
    "AIR LIQUIDE", "AIRBUS", "ALSTOM", "ARCELORMITTAL", "AXA",
    "BNP PARIBAS", "BOUYGUES", "CAPGEMINI", "CARREFOUR", "CREDIT AGRICOLE",
    "DANONE", "DASSAULT SYSTEMES", "EDENRED", "ENGIE", "ESSILORLUXOTTICA",
    "HERMES", "KERING", "LEGRAND", "L'OREAL", "LVMH",
    "MICHELIN", "ORANGE", "PERNOD RICARD", "PUBLICIS", "RENAULT",
    "SAFRAN", "SAINT-GOBAIN", "SANOFI", "SCHNEIDER ELECTRIC",
    "SOCIETE GENERALE", "STELLANTIS", "STMICROELECTRONICS",
    "TELEPERFORMANCE", "THALES", "TOTALENERGIES", "VEOLIA", "VINCI", "VIVENDI",
    # Grandes US (S&P500 / Nasdaq)
    "AMAZON", "APPLE", "MICROSOFT", "NVIDIA", "GOOGLE", "META",
    "TESLA", "NETFLIX", "VISA", "MASTERCARD", "JOHNSON & JOHNSON",
    "PROCTER & GAMBLE", "COCA-COLA", "PEPSICO", "MCDONALDS", "DISNEY",
    "BERKSHIRE HATHAWAY", "JPMORGAN", "GOLDMAN SACHS", "INTEL",
    "AMD", "BROADCOM", "SALESFORCE", "ADOBE", "ORACLE", "CISCO",
    "QUALCOMM", "TEXAS INSTRUMENTS", "COSTCO", "HOME DEPOT",
    "UNITEDHEALTH", "EXXON MOBIL", "CHEVRON", "PFIZER", "MERCK",
    "ABBVIE", "ELI LILLY", "BOEING", "CATERPILLAR", "AMERICAN EXPRESS",
    "STARBUCKS", "UBER", "AIRBNB", "PALANTIR",
    # Autres internationales
    "SAP", "SIEMENS", "ALLIANZ", "BASF", "VOLKSWAGEN", "BAYER",
    "ASML", "NESTLE", "NOVARTIS", "ROCHE", "NOVO NORDISK",
    "UNILEVER", "SHELL", "BP", "ASTRAZENECA", "HSBC",
    "RIO TINTO", "BHP", "TOYOTA", "SAMSUNG",
}

# Exchanges PEA-éligibles (priorité haute si entreprise européenne)
# "F" (Frankfurt Börse) est volontairement exclu : ce suffixe sert
# quasi exclusivement de cotation secondaire peu liquide pour des
# entreprises étrangères (souvent américaines), jamais la cotation
# principale d'une entreprise réellement européenne/PEA-éligible —
# contrairement à XETRA ("DE") qui reste la place principale des
# grandes entreprises allemandes.
PEA_EXCHANGES = ["PA", "AS", "BR", "LS", "MC", "MI", "XETRA", "DE",
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

_KNOWN_NAME_OVERRIDES = {
    "LVMH": "LVMH Moët Hennessy Louis Vuitton",
    "ASML": "ASML Holding N.V.",
}


def get_known_name_override(raw: str) -> str | None:
    """
    Nom correct pour les entrées de _KNOWN_TICKER_OVERRIDES. Utilisé
    par market_lookup.py pour garantir que le nom affiché correspond
    toujours au ticker réellement résolu, sans dépendre d'une
    recherche EODHD séparée qui peut pointer vers un autre instrument.
    """
    cleaned = (raw or "").strip().upper()
    return _KNOWN_NAME_OVERRIDES.get(cleaned)


# Pattern ticker US pur : 1-5 lettres majuscules, optionnellement avec un point
# pour classes d'actions (BRK.B, BF.B), pas de chiffres.
US_TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

_LEGAL_SUFFIX_PATTERN = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|SE|PLC|SA|NV|AG|"
    r"LTD|LIMITED|LLC|LP|HOLDING|HOLDINGS|AKTIENGESELLSCHAFT|"
    r"GMBH|KGAA|SPA|BV|OYJ|ASA|CEDEAR|ADR|CDR|CLASS [A-Z]|CL [A-Z])\b"
)
_RATIO_SUFFIX_PATTERN = re.compile(r"\b\d+\s*/\s*\d+\b")
_CAD_HEDGED_PATTERN = re.compile(r"\(CAD HEDGED\)", re.IGNORECASE)


def _normalize_company_name(name: str) -> str:
    """
    Réduit un nom d'entreprise à son identité de base, pour regrouper
    les différentes cotations d'UNE MÊME entreprise et les distinguer
    d'une entreprise différente qui porte un nom proche (filiale,
    homonyme, société sans rapport). Ne retire QUE les suffixes
    juridiques génériques (Inc, SE, PLC, Ltd, AG...) — jamais un mot
    porteur de sens (un nom de pays, "Energy", "Healthineers"...),
    car c'est justement ce qui permet de distinguer "Siemens AG"
    (Allemagne) de "Siemens Energy AG" ou de "Siemens Limited" (Inde,
    une filiale cotée séparément qui n'a rien à voir).
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.upper()
    n = re.sub(r"^\s*THE\s+", "", n)
    n = re.sub(r"\b([A-Z])\.([A-Z])\.?", r"\1\2", n)
    n = re.sub(r"[.,'()]", " ", n)
    n = _RATIO_SUFFIX_PATTERN.sub(" ", n)
    n = _CAD_HEDGED_PATTERN.sub(" ", n)
    n = _LEGAL_SUFFIX_PATTERN.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


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
                  f"country={r.get('Country')!r} type={r.get('Type')!r} name={r.get('Name')!r} "
                  f"isin={r.get('Isin')!r}")

    # --- Étape 1 : identifier la bonne ENTREPRISE avant de choisir sa
    # cotation. On regroupe les candidats par nom normalisé (identité
    # d'entreprise) pour ne jamais laisser une filiale ou une entreprise
    # homonyme prendre le pas sur la vraie entreprise demandée juste
    # parce qu'elle est mieux placée géographiquement.
    normalized_query = _normalize_company_name(query)
    groups: dict = {}
    for r in filtered:
        key = _normalize_company_name(r.get("Name", ""))
        groups.setdefault(key, []).append(r)

    if normalized_query and len(groups) > 1:
        def _breadth(items):
            return len({it.get("Country", "") for it in items})

        # Un groupe est "apparenté" à la requête si son nom normalisé
        # correspond exactement, ou commence par la requête suivie d'un
        # mot supplémentaire (ex: requête "AIRBUS" → groupe "AIRBUS
        # GROUP" reste apparenté, car c'est la même entreprise sous un
        # autre nom enregistré — mais requête "FERRARI" → groupe
        # "FERRARI GROUP" est UNE AUTRE entreprise : c'est le nombre de
        # pays qui tranche ensuite, pas la simple présence du mot).
        related = {
            k: v for k, v in groups.items()
            if k == normalized_query
            or k.startswith(normalized_query + " ")
            or normalized_query.startswith(k + " ")
        }
        pool = related if related else groups
        if len(pool) == 1:
            filtered = next(iter(pool.values()))
        else:
            best_key = max(pool, key=lambda k: _breadth(pool[k]))
            filtered = pool[best_key]

    # --- Étape 2 : parmi les cotations de LA bonne entreprise (ou de
    # tous les candidats si l'identité n'a pas pu être départagée),
    # choisir la cotation la plus pertinente pour notre audience.

    def rank(item):
        exchange = item.get("Exchange", "")
        code = item.get("Code", "")
        item_type = item.get("Type", "")

        # Priorité 0 : si prefer_us est demandé et que ce résultat est
        # une action US authentique, elle passe avant même la priorité
        # PEA (cas des entreprises US résolues via une recherche par
        # nom, où on veut le vrai ticker US plutôt qu'une cotation
        # secondaire européenne comme Frankfurt).
        if prefer_us and exchange == "US" and item_type == "Common Stock" and not _is_likely_otc_adr(code):
            base_rank = 0

        # Priorité 1 : cotation sur une place PEA-éligible — c'est le
        # public principal d'Oryx (investisseurs français/européens).
        # On ne se fie plus au champ "Country" d'EODHD pour décider :
        # un certificat OTC/ADR coté aux US peut être étiqueté
        # "Country: USA" alors que l'entreprise est européenne, et un
        # ETF américain sans rapport peut porter le même mot dans son
        # nom (ex: recherche "Hermès" → ETF "Federated Hermes").
        elif exchange in PEA_EXCHANGES:
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

        # Correspondance exacte avec la requête tapée : sert UNIQUEMENT
        # à départager deux candidats déjà dans le même palier (ex:
        # "ASML" coté à la fois à Amsterdam et comme ADR US, tous deux
        # PEA ou tous deux hors PEA) — jamais à dépasser un palier plus
        # prioritaire. Une coïncidence de code sur une filiale sans
        # rapport (ex: "BASF" tapé par nom, qui matche par hasard le
        # code d'une cotation secondaire à Budapest) ne doit jamais
        # devancer la vraie cotation principale de l'entreprise.
        exact_match = 0 if code.upper() == query.upper() else 1
        return (base_rank, exact_match)

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
