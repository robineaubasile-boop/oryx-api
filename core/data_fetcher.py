import os
import time
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

REQUEST_TIMEOUT = 8
CACHE_TTL = 3600

# --- In-memory cache ---
_cache = {}
_cache_lock = threading.Lock()


def _get_cached(ticker: str):
    with _cache_lock:
        entry = _cache.get(ticker)
        if entry and (time.time() - entry["timestamp"]) < CACHE_TTL:
            print(f"[CACHE] Hit for {ticker}")
            return entry["result"]
    return None


def _set_cached(ticker: str, result: dict):
    with _cache_lock:
        _cache[ticker] = {"timestamp": time.time(), "result": result}


def _fmp_get(endpoint: str, ticker: str):
    """Single FMP GET — fail fast, never crash."""
    url = f"{FMP_BASE_URL}/{endpoint}/{ticker}"
    params = {"apikey": FMP_API_KEY}

    print(f"[FMP] Calling {endpoint}/{ticker} -> {url}")

    start = time.time()
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        elapsed = round(time.time() - start, 2)
        print(f"[FMP] {endpoint}/{ticker} status={resp.status_code} time={elapsed}s")

        if resp.status_code != 200:
            print(f"[FMP ERROR] {endpoint}/{ticker} body: {resp.text[:300]}")
            return None

        data = resp.json()
        print(f"[FMP] {endpoint}/{ticker} OK — type={type(data).__name__}, len={len(data) if isinstance(data, list) else 'dict'}")
        return data
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        print(f"[FMP ERROR] {endpoint}/{ticker} {type(e).__name__} after {elapsed}s: {e}")
        return None


def _first(response):
    """Safely get first element from FMP list response."""
    if response and isinstance(response, list) and len(response) > 0:
        return response[0]
    return None


def _num(value):
    """Convert value to float, return 0 if None or invalid."""
    if value is None:
        return 0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0


def fetch_financial_data(ticker: str):
    ticker = ticker.upper()

    print(f"[FETCH] === Starting fetch for {ticker} ===")
    print(f"[FETCH] FMP_API_KEY present: {bool(FMP_API_KEY)} (len={len(FMP_API_KEY)})")

    # Check cache first
    cached = _get_cached(ticker)
    if cached:
        return cached

    total_start = time.time()

    # --- Fetch 3 endpoints in parallel ---
    raw = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fmp_get, "profile", ticker): "profile",
            executor.submit(_fmp_get, "ratios", ticker): "ratios",
            executor.submit(_fmp_get, "financial-growth", ticker): "growth",
        }
        for future in as_completed(futures):
            key = futures[future]
            raw[key] = future.result()

    profile = _first(raw.get("profile"))
    ratios = _first(raw.get("ratios"))
    growth = _first(raw.get("growth"))

    print(f"[FETCH] profile: {'OK' if profile else 'NONE'}")
    print(f"[FETCH] ratios: {'OK' if ratios else 'NONE'}")
    print(f"[FETCH] growth: {'OK' if growth else 'NONE'}")

    if profile:
        print(f"[FETCH] profile keys: {list(profile.keys())[:10]}")
        print(f"[FETCH] profile.price={profile.get('price')}, profile.eps={profile.get('eps')}")
    if ratios:
        print(f"[FETCH] ratios keys: {list(ratios.keys())[:10]}")
        print(f"[FETCH] ratios.operatingProfitMargin={ratios.get('operatingProfitMargin')}, ratios.returnOnEquity={ratios.get('returnOnEquity')}")
    if growth:
        print(f"[FETCH] growth keys: {list(growth.keys())[:10]}")
        print(f"[FETCH] growth.revenueGrowth={growth.get('revenueGrowth')}, growth.epsgrowth={growth.get('epsgrowth')}")

    # --- Parse: 0 if missing, ratios * 100 for % ---
    current_price = _num(profile.get("price")) if profile else 0
    eps = _num(profile.get("eps")) if profile else 0

    operating_margin = round(_num(ratios.get("operatingProfitMargin")) * 100, 2) if ratios else 0
    roe = round(_num(ratios.get("returnOnEquity")) * 100, 2) if ratios else 0

    revenue_growth = round(_num(growth.get("revenueGrowth")) * 100, 2) if growth else 0
    eps_growth = round(_num(growth.get("epsgrowth")) * 100, 2) if growth else 0

    # net_cash from profile
    total_cash = _num(profile.get("totalCash")) if profile else 0
    total_debt = _num(profile.get("totalDebt")) if profile else 0
    net_cash = total_cash - total_debt

    # fcf_per_share from ratios
    fcf_per_share = round(_num(ratios.get("freeCashFlowPerShare")) if ratios else 0, 4)

    data = {
        "current_price": current_price,
        "revenue_growth": revenue_growth,
        "operating_margin": operating_margin,
        "roe": roe,
        "net_cash": net_cash,
        "fcf_per_share": fcf_per_share,
        "growth": eps_growth,
        "eps": eps,
    }

    available = sum(1 for v in data.values() if v != 0)
    total_elapsed = round(time.time() - total_start, 2)

    print(f"[FETCH] Final data: {data}")
    print(f"[FETCH] Non-zero fields: {available}/{len(data)}")
    print(f"[FETCH] Total time: {total_elapsed}s")

    # Only fail if absolutely nothing
    if not profile and not ratios and not growth:
        print(f"[FETCH ERROR] All 3 sources returned None for {ticker}")
        return {"success": False, "ticker": ticker, "error": "All data sources failed, no data available", "data": None}

    result = {"success": True, "ticker": ticker, "error": None, "data": data}
    _set_cached(ticker, result)
    return result
