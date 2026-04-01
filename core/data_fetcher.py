import os
import time
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# --- API keys ---
EOD_API_KEY = os.getenv("EOD_API_KEY", "")
FMP_API_KEY = os.getenv("FMP_API_KEY", "")

# --- Base URLs ---
EOD_BASE_URL = "https://eodhd.com/api"
FMP_STABLE_URL = "https://financialmodelingprep.com/stable"

REQUEST_TIMEOUT = 8
CACHE_TTL = 3600

# --- In-memory cache ---
_cache = {}
_cache_lock = threading.Lock()


def _get_cached(ticker: str):
    with _cache_lock:
        entry = _cache.get(ticker)
        if entry and (time.time() - entry["timestamp"]) < CACHE_TTL:
            logger.info(f"[CACHE] Hit for {ticker}")
            return entry["result"]
    return None


def _set_cached(ticker: str, result: dict):
    with _cache_lock:
        _cache[ticker] = {"timestamp": time.time(), "result": result}


def _num(value):
    """Convert value to float. Returns None only if value is None or non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _num_or_zero(value):
    """Convert value to float, return 0 if None or invalid."""
    if value is None:
        return 0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0


# ============================================================
# EOD Historical Data — Primary source
# ============================================================

def _eod_get(endpoint: str, params: dict = None):
    """Single EODHD GET — fail fast, never crash."""
    url = f"{EOD_BASE_URL}/{endpoint}"
    base_params = {"api_token": EOD_API_KEY, "fmt": "json"}
    if params:
        base_params.update(params)

    logger.info(f"[EOD CALL] URL: {url}")

    start = time.time()
    try:
        resp = requests.get(url, params=base_params, timeout=REQUEST_TIMEOUT)
        elapsed = round(time.time() - start, 2)

        logger.info(f"[EOD STATUS] {resp.status_code} in {elapsed}s")
        logger.info(f"[EOD RESPONSE] Body: {resp.text}")

        if resp.status_code != 200:
            logger.error(f"[EOD ERROR] HTTP {resp.status_code} — {resp.text}")
            return None

        data = resp.json()
        return data
    except requests.exceptions.ConnectionError as e:
        elapsed = round(time.time() - start, 2)
        logger.error(f"[EOD ERROR] ConnectionError after {elapsed}s — {e}")
        return None
    except requests.exceptions.Timeout as e:
        elapsed = round(time.time() - start, 2)
        logger.error(f"[EOD ERROR] Timeout after {elapsed}s — {e}")
        return None
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        logger.error(f"[EOD ERROR] {type(e).__name__} after {elapsed}s — {e}", exc_info=True)
        return None


def _parse_eod_data(fundamentals: dict, realtime: dict, ticker: str) -> dict | None:
    """Parse EOD fundamentals + real-time into our standard data format.

    Returns None if insufficient valid data.
    """
    # --- Price from real-time ---
    current_price = _num_or_zero(realtime.get("close")) if realtime else 0
    currency = realtime.get("currency", "USD") if realtime else "USD"

    # --- EPS from Highlights ---
    highlights = fundamentals.get("Highlights", {}) if fundamentals else {}
    eps = _num(highlights.get("EarningsShare"))
    shares_outstanding = _num_or_zero(highlights.get("SharesOutstanding"))

    # --- Income Statement: revenue, operating income (latest 2 years) ---
    income_yearly = {}
    financials = fundamentals.get("Financials", {}) if fundamentals else {}
    income_raw = financials.get("Income_Statement", {}).get("yearly", {})

    # Sort years descending (keys are like "2024-12-31")
    sorted_years = sorted(income_raw.keys(), reverse=True)
    for date_key in sorted_years:
        income_yearly[date_key] = income_raw[date_key]

    revenue_t = None
    revenue_t1 = None
    operating_income = None

    if len(sorted_years) >= 1:
        latest = income_raw[sorted_years[0]]
        revenue_t = _num(latest.get("totalRevenue"))
        operating_income = _num(latest.get("operatingIncome"))
    if len(sorted_years) >= 2:
        previous = income_raw[sorted_years[1]]
        revenue_t1 = _num(previous.get("totalRevenue"))

    logger.info(f"[EOD] revenue_t={revenue_t}, revenue_t1={revenue_t1}, operating_income={operating_income}")

    # --- Cash Flow: FCF (latest year) ---
    cashflow_raw = financials.get("Cash_Flow", {}).get("yearly", {})
    cf_sorted = sorted(cashflow_raw.keys(), reverse=True)

    free_cash_flow = None
    if len(cf_sorted) >= 1:
        latest_cf = cashflow_raw[cf_sorted[0]]
        free_cash_flow = _num(latest_cf.get("freeCashFlow"))

    # --- Balance Sheet: net cash ---
    balance_raw = financials.get("Balance_Sheet", {}).get("yearly", {})
    bs_sorted = sorted(balance_raw.keys(), reverse=True)

    net_cash = None
    if len(bs_sorted) >= 1:
        latest_bs = balance_raw[bs_sorted[0]]
        total_cash = _num_or_zero(latest_bs.get("cashAndShortTermInvestments"))
        total_debt = _num_or_zero(latest_bs.get("shortLongTermDebtTotal"))
        net_cash = total_cash - total_debt

    # --- ROE from Highlights ---
    roe = _num(highlights.get("ReturnOnEquityTTM"))
    if roe is not None:
        roe = round(roe * 100, 2)

    # --- Computed metrics ---
    revenue_growth = None
    if revenue_t is not None and revenue_t1 is not None and revenue_t1 != 0:
        revenue_growth = round(((revenue_t - revenue_t1) / abs(revenue_t1)) * 100, 2)

    operating_margin = None
    if operating_income is not None and revenue_t is not None and revenue_t != 0:
        operating_margin = round((operating_income / revenue_t) * 100, 2)

    fcf_per_share = None
    if free_cash_flow is not None and shares_outstanding > 0:
        fcf_per_share = round(free_cash_flow / shares_outstanding, 4)

    # --- EPS growth from Highlights ---
    eps_growth = _num(highlights.get("EPSEstimateCurrentYear"))
    if eps_growth is not None and eps is not None and eps != 0:
        eps_growth = round(((eps_growth - eps) / abs(eps)) * 100, 2)
    else:
        eps_growth = None

    # --- Data quality check: at least 3 valid fields ---
    fields = [revenue_growth, operating_margin, fcf_per_share, roe, eps]
    valid_count = sum(1 for f in fields if f is not None)

    logger.info(f"[EOD] Parsed: price={current_price}, rev_growth={revenue_growth}, "
                f"op_margin={operating_margin}, roe={roe}, fcf/share={fcf_per_share}, "
                f"eps={eps}, eps_growth={eps_growth}, net_cash={net_cash}, currency={currency}")
    logger.info(f"[EOD] Valid fields: {valid_count}/{len(fields)}")

    if valid_count < 3:
        logger.warning(f"[DATA QUALITY] Insufficient data for {ticker} — only {valid_count} valid fields")
        return None

    data = {
        "current_price": current_price,
        "currency": currency,
        "revenue_growth": revenue_growth,
        "operating_margin": operating_margin,
        "roe": roe,
        "net_cash": net_cash,
        "fcf_per_share": fcf_per_share,
        "growth": eps_growth,
        "eps": eps,
    }

    data["missing_fields"] = [k for k, v in data.items() if v is None]
    return data


def _fetch_eod(ticker: str) -> dict | None:
    """Fetch from EODHD. Returns parsed data dict or None."""
    logger.info(f"[EOD] Fetch start for {ticker}")

    if not EOD_API_KEY:
        logger.warning("[EOD] No EOD_API_KEY set, skipping EOD")
        return None

    # Fetch real-time and fundamentals in parallel
    raw = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_eod_get, f"real-time/{ticker}"): "realtime",
            executor.submit(_eod_get, f"fundamentals/{ticker}"): "fundamentals",
        }
        for future in as_completed(futures):
            key = futures[future]
            raw[key] = future.result()

    realtime = raw.get("realtime")
    fundamentals = raw.get("fundamentals")

    logger.info(f"[EOD] realtime: {'OK' if realtime else 'NONE'}")
    logger.info(f"[EOD] fundamentals: {'OK' if fundamentals else 'NONE'}")

    if not fundamentals:
        logger.error(f"[EOD ERROR] No fundamentals data for {ticker}")
        return None

    return _parse_eod_data(fundamentals, realtime, ticker)


# ============================================================
# FMP — Fallback source
# ============================================================

def _fmp_get(endpoint: str, ticker: str):
    """Single FMP GET via /stable/ endpoints — fail fast, never crash."""
    url = f"{FMP_STABLE_URL}/{endpoint}"
    params = {"symbol": ticker, "apikey": FMP_API_KEY}

    logger.info(f"[FMP CALL] URL: {url}?symbol={ticker}")

    start = time.time()
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        elapsed = round(time.time() - start, 2)

        logger.info(f"[FMP RESPONSE] Status: {resp.status_code}")
        logger.info(f"[FMP RESPONSE] Body: {resp.text}")

        if resp.status_code != 200:
            logger.error(f"[FMP ERROR] {endpoint}/{ticker}: HTTP {resp.status_code} — {resp.text}")
            return None

        data = resp.json()

        if isinstance(data, dict) and "Error Message" in data:
            logger.error(f"[FMP ERROR] {resp.text}")
            return None

        if isinstance(data, list) and len(data) == 0:
            logger.warning(f"[FMP WARNING] {endpoint}/{ticker} returned EMPTY list []")

        logger.info(f"[FMP OK] {endpoint}/{ticker} — time={elapsed}s")
        return data
    except requests.exceptions.ConnectionError as e:
        elapsed = round(time.time() - start, 2)
        logger.error(f"[FMP ERROR] {endpoint}/{ticker}: ConnectionError after {elapsed}s — {e}")
        return None
    except requests.exceptions.Timeout as e:
        elapsed = round(time.time() - start, 2)
        logger.error(f"[FMP ERROR] {endpoint}/{ticker}: Timeout after {elapsed}s — {e}")
        return None
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        logger.error(f"[FMP ERROR] {endpoint}/{ticker}: {type(e).__name__} after {elapsed}s — {e}", exc_info=True)
        return None


def _first(response):
    """Safely get first element from FMP response (list or dict)."""
    if response is None:
        return None
    if isinstance(response, dict):
        return response if response else None
    if isinstance(response, list) and len(response) > 0:
        return response[0]
    return None


def _fetch_fmp(ticker: str) -> dict | None:
    """Fetch from FMP. Returns parsed data dict or None."""
    logger.info(f"[FALLBACK] Using FMP for {ticker}")

    if not FMP_API_KEY:
        logger.warning("[FMP] No FMP_API_KEY set, skipping FMP")
        return None

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

    logger.info(f"[FMP] profile: {'OK' if profile else 'NONE'}")
    logger.info(f"[FMP] ratios: {'OK' if ratios else 'NONE'}")
    logger.info(f"[FMP] growth: {'OK' if growth else 'NONE'}")

    if not profile and not ratios and not growth:
        logger.error(f"[FMP ERROR] All 3 sources returned None for {ticker}")
        return None

    current_price = _num_or_zero(profile.get("price")) if profile else 0
    currency = profile.get("currency", "USD") if profile else "USD"

    eps = _num(profile.get("eps")) if profile else None

    om_raw = _num(ratios.get("operatingProfitMargin")) if ratios else None
    operating_margin = round(om_raw * 100, 2) if om_raw is not None else None

    roe_raw = _num(ratios.get("returnOnEquity")) if ratios else None
    roe = round(roe_raw * 100, 2) if roe_raw is not None else None

    fcf_raw = _num(ratios.get("freeCashFlowPerShare")) if ratios else None
    fcf_per_share = round(fcf_raw, 4) if fcf_raw is not None else None

    rg_raw = _num(growth.get("revenueGrowth")) if growth else None
    revenue_growth = round(rg_raw * 100, 2) if rg_raw is not None else None

    eg_raw = _num(growth.get("epsgrowth")) if growth else None
    eps_growth = round(eg_raw * 100, 2) if eg_raw is not None else None

    total_cash = _num_or_zero(profile.get("totalCash")) if profile else 0
    total_debt = _num_or_zero(profile.get("totalDebt")) if profile else 0
    net_cash = total_cash - total_debt

    data = {
        "current_price": current_price,
        "currency": currency,
        "revenue_growth": revenue_growth,
        "operating_margin": operating_margin,
        "roe": roe,
        "net_cash": net_cash,
        "fcf_per_share": fcf_per_share,
        "growth": eps_growth,
        "eps": eps,
    }

    data["missing_fields"] = [k for k, v in data.items() if v is None]
    return data


# ============================================================
# Main entry point — EOD first, FMP fallback
# ============================================================

def fetch_financial_data(ticker: str):
    ticker = ticker.upper()
    logger.info(f"[FETCH] === Starting fetch for {ticker} ===")

    # Check cache first
    cached = _get_cached(ticker)
    if cached:
        return cached

    total_start = time.time()

    # --- 1. Try EOD first ---
    data = _fetch_eod(ticker)

    # --- 2. Fallback to FMP if EOD failed ---
    if data is None:
        logger.info(f"[FALLBACK] EOD failed for {ticker}, trying FMP...")
        data = _fetch_fmp(ticker)

    total_elapsed = round(time.time() - total_start, 2)

    # --- 3. Both failed ---
    if data is None:
        logger.error(f"[ERROR] All sources failed for {ticker}")
        return {
            "success": False,
            "ticker": ticker,
            "error": "All data sources failed, no data available",
            "data": None,
        }

    logger.info(f"[FETCH] Final data for {ticker}: {data}")
    logger.info(f"[FETCH] Total time: {total_elapsed}s")

    result = {"success": True, "ticker": ticker, "error": None, "data": data}
    _set_cached(ticker, result)
    return result
