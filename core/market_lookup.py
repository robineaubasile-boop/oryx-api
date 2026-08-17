"""Lookup de marché léger pour l'écran Recherche de /web-v2.

Zéro appel Claude, zéro score, zéro fair value : juste ticker, nom, prix,
variation. Réutilise la plomberie EODHD déjà présente dans le projet
(recherche + prix temps réel) au lieu de dupliquer la logique HTTP.

NOTE CRYPTO : le mapping ticker -> "<SYMBOLE>-USD.CC" ci-dessous n'a PAS
été validé par un appel réel à EODHD (pas de clé API disponible au moment
de l'écriture). Le champ "verified" de la réponse crypto reflète cet état
tant que ce n'est pas confirmé manuellement.
"""

import re

from core.data_fetcher import _eod_get, _currency_from_ticker, _num
from core.ticker_resolver import _eodhd_search, normalize_ticker

# Alias courants -> ticker crypto EODHD (format non confirmé, cf. note ci-dessus)
_CRYPTO_ALIASES = {
    "BTC": "BTC", "BITCOIN": "BTC",
    "ETH": "ETH", "ETHEREUM": "ETH",
    "SOL": "SOL", "SOLANA": "SOL",
    "XRP": "XRP", "RIPPLE": "XRP",
    "DOGE": "DOGE", "DOGECOIN": "DOGE",
    "ADA": "ADA", "CARDANO": "ADA",
    "BNB": "BNB",
    "LTC": "LTC", "LITECOIN": "LTC",
}

_STOCK_ETF_TYPES = ("Common Stock", "Preferred Stock", "ETF", "Fund", "Mutual Fund")


def _guess_crypto_ticker(cleaned: str) -> str | None:
    """Devine un ticker crypto EODHD à partir de la requête. Non vérifié (voir note module)."""
    symbol = _CRYPTO_ALIASES.get(cleaned)
    if symbol:
        return f"{symbol}-USD.CC"
    if re.match(r"^[A-Z]{2,10}-USD(\.CC)?$", cleaned):
        base = cleaned.split(".")[0].split("-")[0]
        return f"{base}-USD.CC"
    return None


def _quote_from_realtime(quote: dict | None) -> dict | None:
    if not quote or not isinstance(quote, dict):
        return None
    close = _num(quote.get("close"))
    if close is None:
        return None
    return {
        "price": close,
        "change": _num(quote.get("change")),
        "change_percent": _num(quote.get("change_p")),
        "previous_close": _num(quote.get("previousClose")),
        "as_of": quote.get("timestamp"),
    }


def _quote_with_eod_fallback(ticker: str) -> dict | None:
    """Prix temps réel, avec repli sur le dernier close EOD si le temps réel échoue (403 EU, etc.)."""
    result = _quote_from_realtime(_eod_get(f"real-time/{ticker}"))
    if result:
        return result

    eod_daily = _eod_get(f"eod/{ticker}", params={"order": "d", "period": "d", "limit": "1"})
    if isinstance(eod_daily, list) and eod_daily:
        close = _num(eod_daily[0].get("close"))
        if close is not None:
            return {
                "price": close,
                "change": None,
                "change_percent": None,
                "previous_close": None,
                "as_of": eod_daily[0].get("date"),
            }
    return None


def search_market(raw_query: str) -> dict:
    """Recherche un ticker (action, ETF ou crypto) et retourne prix + variation bruts."""
    cleaned = (raw_query or "").strip().upper()
    if not cleaned:
        return {"success": False, "query": raw_query, "error": "Requête vide."}

    # --- Tentative crypto (non vérifiée, cf. note en tête de module) ---
    crypto_ticker = _guess_crypto_ticker(cleaned)
    if crypto_ticker:
        quote = _quote_with_eod_fallback(crypto_ticker)
        if quote:
            return {
                "success": True,
                "query": raw_query,
                "ticker": crypto_ticker,
                "name": cleaned.split("-")[0].capitalize(),
                "type": "crypto",
                "currency": "USD",
                "verified": False,
                **quote,
            }
        # Pas de résultat crypto : on retente comme action/ETF ci-dessous.

    # --- Action / ETF : réutilise la recherche + résolution EODHD existantes ---
    search_results = _eodhd_search(cleaned)
    match_item = None
    if search_results:
        stock_matches = [r for r in search_results if r.get("Type") in _STOCK_ETF_TYPES]
        match_item = stock_matches[0] if stock_matches else None

    ticker = normalize_ticker(cleaned)
    quote = _quote_with_eod_fallback(ticker)
    if not quote:
        return {
            "success": False,
            "query": raw_query,
            "error": f"Aucune donnée de marché trouvée pour « {raw_query} ».",
        }

    asset_type = "etf" if (match_item and match_item.get("Type") in ("ETF", "Fund", "Mutual Fund")) else "stock"

    return {
        "success": True,
        "query": raw_query,
        "ticker": ticker,
        "name": (match_item.get("Name") if match_item else None) or ticker,
        "type": asset_type,
        "currency": _currency_from_ticker(ticker),
        "verified": True,
        **quote,
    }
