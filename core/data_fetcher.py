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


def _fetch_eod_yearly_prices(ticker: str, years: int = 5) -> dict:
    """Fetch end-of-year closing prices from EODHD EOD endpoint.
    Returns dict like {2024: 236.15, 2023: 198.50, ...}
    """
    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=365 * years + 30)).strftime("%Y-%m-%d")

    data = _eod_get(f"eod/{ticker}", params={
        "from": start_date,
        "to": end_date,
        "period": "m",
        "order": "d"
    })

    if not data or not isinstance(data, list):
        logger.warning(f"[EOD PRICES] No historical prices for {ticker}")
        return {}

    # Pour chaque année, prendre le dernier prix mensuel disponible (décembre ou le dernier mois)
    prices_by_year = {}
    for entry in data:
        date_str = entry.get("date", "")
        close = _num(entry.get("adjusted_close") or entry.get("close"))
        if not date_str or close is None:
            continue
        year = int(date_str[:4])
        month = int(date_str[5:7])
        # Garder le mois le plus tardif de chaque année
        if year not in prices_by_year or month > prices_by_year[year]["month"]:
            prices_by_year[year] = {"month": month, "price": close}

    result = {y: v["price"] for y, v in prices_by_year.items()}
    logger.info(f"[EOD PRICES] Yearly prices for {ticker}: {result}")
    return result


def _extract_annual_eps(fundamentals: dict, years: int = 5) -> dict:
    """Extract annual EPS from Earnings.History (sum of quarterly epsActual).
    Returns dict like {2024: 7.98, 2023: 6.50, ...}
    """
    from datetime import datetime

    earnings_history = fundamentals.get("Earnings", {}).get("History", {})
    if not earnings_history:
        logger.warning("[EPS HISTORY] No Earnings.History section found")
        return {}

    # Collecter les EPS trimestriels par année
    quarterly_eps = {}  # {2024: [eps_q1, eps_q2, ...], ...}
    current_year = datetime.now().year
    min_year = current_year - years

    for date_key, entry in earnings_history.items():
        eps_actual = _num(entry.get("epsActual"))
        if eps_actual is None:
            continue
        try:
            year = int(date_key[:4])
        except (ValueError, IndexError):
            continue
        if year < min_year:
            continue
        if year not in quarterly_eps:
            quarterly_eps[year] = []
        quarterly_eps[year].append(eps_actual)

    # Sommer les trimestres pour obtenir l'EPS annuel (garder seulement les années avec 4 trimestres)
    annual_eps = {}
    for year, eps_list in quarterly_eps.items():
        if len(eps_list) >= 4:
            # Prendre les 4 trimestres les plus récents si plus de 4
            annual_eps[year] = round(sum(sorted(eps_list, reverse=True)[:4]), 4)
            logger.info(f"[EPS HISTORY] {year}: {len(eps_list)} quarters, annual EPS = {annual_eps[year]}")
        else:
            logger.info(f"[EPS HISTORY] {year}: only {len(eps_list)} quarters, skipping")

    return annual_eps


def _compute_historical_pe(annual_eps: dict, yearly_prices: dict) -> float | None:
    """Compute average historical P/E from annual EPS and year-end prices.
    Returns the average P/E over available years, or None.
    """
    pe_ratios = {}
    for year in sorted(annual_eps.keys()):
        eps = annual_eps[year]
        price = yearly_prices.get(year)
        if eps is not None and eps > 0 and price is not None and price > 0:
            pe = round(price / eps, 2)
            pe_ratios[year] = pe
            logger.info(f"[PE HISTORY] {year}: price={price}, EPS={eps}, P/E={pe}")

    if not pe_ratios:
        logger.warning("[PE HISTORY] No valid P/E ratios computed")
        return None

    avg_pe = round(sum(pe_ratios.values()) / len(pe_ratios), 2)
    logger.info(f"[PE HISTORY] Average P/E over {len(pe_ratios)} years: {avg_pe} (years: {list(pe_ratios.keys())})")
    return avg_pe


def _parse_eod_data(fundamentals: dict, realtime: dict, ticker: str, yearly_prices: dict = None) -> dict | None:
    """Parse EOD fundamentals + real-time into our standard data format.

    Returns None if insufficient valid data.
    """
    # --- Price from real-time ---
    current_price = _num_or_zero(realtime.get("close")) if realtime else 0
    currency = _currency_from_ticker(ticker)

    # --- Sector from General ---
    general = fundamentals.get("General", {}) if fundamentals else {}
    sector = general.get("Sector", "Unknown")
    industry = general.get("Industry", "Unknown")
    logger.info(f"[SECTOR] {ticker}: Sector={sector}, Industry={industry}")

    # --- EPS from Highlights ---
    highlights = fundamentals.get("Highlights", {}) if fundamentals else {}
    eps = _num(highlights.get("EarningsShare"))
    shares_outstanding = _num_or_zero(highlights.get("SharesOutstanding"))
    logger.info(f"[DEBUG SHARES] SharesOutstanding for {ticker}: {highlights.get('SharesOutstanding')}")

    # --- Income Statement: revenue, operating income (up to 5 years for CAGR) ---
    financials = fundamentals.get("Financials", {}) if fundamentals else {}
    income_raw = financials.get("Income_Statement", {}).get("yearly", {})

    # Sort years descending (keys are like "2024-12-31")
    sorted_years = sorted(income_raw.keys(), reverse=True)

    # Collect revenues for each available year (most recent first)
    revenues_by_year = []
    latest_income = income_raw[sorted_years[0]] if len(sorted_years) >= 1 else {}
    for date_key in sorted_years[:5]:
        rev = _num(income_raw[date_key].get("totalRevenue"))
        if rev is not None and rev > 0:
            revenues_by_year.append(rev)

    revenue_t = revenues_by_year[0] if len(revenues_by_year) >= 1 else None
    operating_income = _num(latest_income.get("operatingIncome"))

    logger.info(f"[EOD] revenues (recent→old): {revenues_by_year}, operating_income={operating_income}")

    # --- Cash Flow: FCF (latest year) ---
    cashflow_raw = financials.get("Cash_Flow", {}).get("yearly", {})
    cf_sorted = sorted(cashflow_raw.keys(), reverse=True)

    free_cash_flow = None
    if len(cf_sorted) >= 1:
        latest_cf = cashflow_raw[cf_sorted[0]]
        logger.info(f"[CF KEYS] {list(latest_cf.keys())}")
        free_cash_flow = _num(latest_cf.get("freeCashFlow"))
        if free_cash_flow is None:
            operating_cf = _num(latest_cf.get("totalCashFromOperatingActivities"))
            capex = _num(latest_cf.get("capitalExpenditures"))
            if operating_cf is not None and capex is not None:
                if capex > 0:
                    capex = -capex
                free_cash_flow = operating_cf + capex
                logger.info(f"[FCF FALLBACK] Calculated FCF from OCF ({operating_cf}) + CapEx ({capex}) = {free_cash_flow}")
            else:
                logger.warning(f"[FCF] No freeCashFlow and no OCF/CapEx available. OCF={operating_cf}, CapEx={capex}")

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

    # --- Revenue growth as CAGR (3-5 years preferred, 2-year fallback) ---
    # Key is "revenue_growth" for backward compatibility, but value is a CAGR, not YoY
    revenue_growth = None
    if len(revenues_by_year) >= 2:
        revenue_recent = revenues_by_year[0]
        revenue_oldest = revenues_by_year[-1]
        nb_years = len(revenues_by_year) - 1
        if revenue_oldest > 0:
            revenue_growth = round(((revenue_recent / revenue_oldest) ** (1 / nb_years) - 1) * 100, 2)
            logger.info(f"[CAGR] revenue: {revenue_oldest} → {revenue_recent} over {nb_years} years = {revenue_growth}%")

    operating_margin = None
    if operating_income is not None and revenue_t is not None and revenue_t != 0:
        operating_margin = round((operating_income / revenue_t) * 100, 2)

    fcf_per_share = None
    if free_cash_flow is not None and shares_outstanding > 0:
        fcf_per_share = round(free_cash_flow / shares_outstanding, 4)

    # --- ROIC (Return on Invested Capital) ---
    roic = None
    if operating_income is not None and len(bs_sorted) >= 1:
        latest_bs_ref = balance_raw[bs_sorted[0]]
        tax_expense = _num(latest_income.get("incomeTaxExpense"))
        pretax_income = _num(latest_income.get("incomeBeforeTax"))

        if tax_expense is not None and pretax_income is not None and pretax_income != 0:
            tax_rate = tax_expense / pretax_income
        else:
            tax_rate = 0.25

        nopat = operating_income * (1 - tax_rate)

        equity = _num(latest_bs_ref.get("totalStockholderEquity"))
        if equity is not None:
            invested_capital = equity + total_debt - total_cash
            if invested_capital > 0:
                roic = round((nopat / invested_capital) * 100, 2)
                logger.info(f"[ROIC] NOPAT={nopat}, Invested Capital={invested_capital}, ROIC={roic}%")

    # --- Debt to Equity ---
    debt_to_equity = None
    if len(bs_sorted) >= 1:
        latest_bs_ref = balance_raw[bs_sorted[0]]
        equity = _num(latest_bs_ref.get("totalStockholderEquity"))
        if equity is not None and equity > 0:
            debt_to_equity = round(total_debt / equity, 2)
            logger.info(f"[D/E] Debt={total_debt}, Equity={equity}, D/E={debt_to_equity}")

    # --- EPS growth from Highlights ---
    eps_growth = _num(highlights.get("EPSEstimateCurrentYear"))
    if eps_growth is not None and eps is not None and eps != 0:
        eps_growth = round(((eps_growth - eps) / abs(eps)) * 100, 2)
    else:
        eps_growth = None

    # --- Predictability metrics ---

    # revenue_growth_years: consecutive years of revenue growth (most recent first)
    revenue_growth_years = 0
    if len(revenues_by_year) >= 2:
        for i in range(len(revenues_by_year) - 1):
            if revenues_by_year[i] > revenues_by_year[i + 1]:
                revenue_growth_years += 1
            else:
                break
        logger.info(f"[PREDICTABILITY] revenue_growth_years={revenue_growth_years} (from {len(revenues_by_year)} years)")

    # margin_stability: standard deviation of operating margins over available years
    margin_stability = None
    margins_list = []
    for date_key in sorted_years[:5]:
        inc = income_raw[date_key]
        rev = _num(inc.get("totalRevenue"))
        op_inc = _num(inc.get("operatingIncome"))
        if rev is not None and rev > 0 and op_inc is not None:
            margins_list.append((op_inc / rev) * 100)
    if len(margins_list) >= 2:
        import statistics
        margin_stability = round(statistics.stdev(margins_list), 2)
        logger.info(f"[PREDICTABILITY] margin_stability={margin_stability} (stdev of {margins_list})")

    # eps_positive_years: count of years with positive annual EPS
    annual_eps_for_pred = _extract_annual_eps(fundamentals) if fundamentals else {}
    eps_positive_years = sum(1 for v in annual_eps_for_pred.values() if v is not None and v > 0)
    logger.info(f"[PREDICTABILITY] eps_positive_years={eps_positive_years} (from {len(annual_eps_for_pred)} years)")

    # --- P/E historique ---
    pe_history_avg = None
    if fundamentals:
        annual_eps = annual_eps_for_pred if annual_eps_for_pred else _extract_annual_eps(fundamentals)
        if annual_eps and yearly_prices:
            pe_history_avg = _compute_historical_pe(annual_eps, yearly_prices)

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
        "sector": sector,
        "name": general.get("Name", ticker),
        "revenue_growth": revenue_growth,
        "operating_margin": operating_margin,
        "roe": roe,
        "roic": roic,
        "debt_to_equity": debt_to_equity,
        "net_cash": net_cash,
        "fcf_per_share": fcf_per_share,
        "growth": eps_growth,
        "eps": eps,
        "pe_history_avg": pe_history_avg,
        "revenue_growth_years": revenue_growth_years,
        "margin_stability": margin_stability,
        "eps_positive_years": eps_positive_years,
    }

    data["missing_fields"] = [k for k, v in data.items() if v is None]
    return data


def _parse_etf_data(fundamentals: dict, realtime: dict, ticker: str) -> dict | None:
    """Parse EODHD fundamentals for an ETF into our standard ETF format."""
    general = fundamentals.get("General", {})
    etf_data = fundamentals.get("ETF_Data", {})
    technicals = fundamentals.get("Technicals", {})

    if not etf_data:
        logger.warning(f"[ETF] No ETF_Data section for {ticker}")
        return None

    current_price = _num_or_zero(realtime.get("close")) if realtime else 0
    currency = _currency_from_ticker(ticker)

    # --- General info ---
    name = general.get("Name", ticker)
    category = general.get("Category", "Unknown")

    # --- ETF info ---
    isin = etf_data.get("ISIN", None)
    index_name = etf_data.get("Index_Name", None)
    inception_date = etf_data.get("Inception_Date", None)
    total_assets = _num(etf_data.get("TotalAssets"))
    holdings_count = _num(etf_data.get("Holdings_Count"))
    yield_pct = _num(etf_data.get("Yield"))

    # --- Frais ---
    net_expense_ratio = _num(etf_data.get("NetExpenseRatio"))
    ongoing_charge = _num(etf_data.get("Ongoing_Charge"))
    # Utiliser ongoing_charge en priorité (standard européen), sinon expense ratio
    ter = ongoing_charge if ongoing_charge is not None else net_expense_ratio
    # Convertir en pourcentage lisible si besoin
    if ter is not None and ter < 1:
        ter = round(ter * 100, 2)

    # --- Performance ---
    performance = etf_data.get("Performance", {})
    returns_ytd = _num(performance.get("Returns_YTD"))
    returns_1y = _num(performance.get("Returns_1Y"))
    returns_3y = _num(performance.get("Returns_3Y"))
    returns_5y = _num(performance.get("Returns_5Y"))
    returns_10y = _num(performance.get("Returns_10Y"))
    volatility_1y = _num(performance.get("1y_Volatility"))
    volatility_3y = _num(performance.get("3y_Volatility"))
    sharpe_3y = _num(performance.get("3y_SharpRatio"))

    # --- Top 10 Holdings ---
    top_10_raw = etf_data.get("Top_10_Holdings", {})
    top_10 = []
    if isinstance(top_10_raw, dict):
        for key, holding in top_10_raw.items():
            if isinstance(holding, dict):
                top_10.append({
                    "code": holding.get("Code", key),
                    "name": holding.get("Name", "Unknown"),
                    "assets_pct": _num(holding.get("Assets_%")),
                    "sector": holding.get("Sector", ""),
                })
        top_10 = sorted(top_10, key=lambda x: x.get("assets_pct") or 0, reverse=True)[:10]

    # --- Sector Weights ---
    sector_weights_raw = etf_data.get("Sector_Weights", {})
    sector_weights = {}
    if isinstance(sector_weights_raw, dict):
        for sector_name, values in sector_weights_raw.items():
            if isinstance(values, dict):
                pct = _num(values.get("Equity_%"))
                if pct is not None and pct > 0:
                    sector_weights[sector_name] = round(pct, 2)

    # --- World Regions ---
    regions_raw = etf_data.get("World_Regions", {})
    world_regions = {}
    if isinstance(regions_raw, dict):
        for region_name, values in regions_raw.items():
            if isinstance(values, dict):
                pct = _num(values.get("Equity_%"))
                if pct is not None and pct > 0:
                    world_regions[region_name] = round(pct, 2)

    # --- Market Cap breakdown ---
    market_cap_raw = etf_data.get("Market_Capitalisation", {})
    market_cap = {}
    if isinstance(market_cap_raw, dict):
        for size, pct in market_cap_raw.items():
            val = _num(pct)
            if val is not None and val > 0:
                market_cap[size] = round(val, 2)

    # --- Valuations vs category ---
    valuations_growth = etf_data.get("Valuations_Growth", {})
    valuations_portfolio = valuations_growth.get("Valuations_Rates_Portfolio", {})
    valuations_category = valuations_growth.get("Valuations_Rates_To_Category", {})

    pe_portfolio = _num(valuations_portfolio.get("Price/Prospective Earnings"))
    pb_portfolio = _num(valuations_portfolio.get("Price/Book"))
    pe_category = _num(valuations_category.get("Price/Prospective Earnings"))
    pb_category = _num(valuations_category.get("Price/Book"))

    # --- Morningstar ---
    morningstar = etf_data.get("MorningStar", {})
    morningstar_rating = _num(morningstar.get("Ratio"))
    morningstar_benchmark = morningstar.get("Category_Benchmark", None)

    logger.info(f"[ETF] Parsed: name={name}, TER={ter}, AUM={total_assets}, holdings={holdings_count}")
    logger.info(f"[ETF] Returns: YTD={returns_ytd}, 1Y={returns_1y}, 3Y={returns_3y}, 5Y={returns_5y}")
    logger.info(f"[ETF] Top sectors: {sector_weights}")
    logger.info(f"[ETF] Top regions: {world_regions}")

    return {
        "type": "ETF",
        "name": name,
        "current_price": current_price,
        "currency": currency,
        "category": category,
        "isin": isin,
        "index_name": index_name,
        "inception_date": inception_date,
        "total_assets": total_assets,
        "holdings_count": int(holdings_count) if holdings_count else None,
        "yield_pct": round(yield_pct, 2) if yield_pct else None,
        "ter": ter,
        "returns_ytd": round(returns_ytd, 2) if returns_ytd else None,
        "returns_1y": round(returns_1y, 2) if returns_1y else None,
        "returns_3y": round(returns_3y, 2) if returns_3y else None,
        "returns_5y": round(returns_5y, 2) if returns_5y else None,
        "returns_10y": round(returns_10y, 2) if returns_10y else None,
        "volatility_1y": round(volatility_1y, 2) if volatility_1y else None,
        "volatility_3y": round(volatility_3y, 2) if volatility_3y else None,
        "sharpe_3y": round(sharpe_3y, 2) if sharpe_3y else None,
        "top_10_holdings": top_10,
        "sector_weights": sector_weights,
        "world_regions": world_regions,
        "market_cap_breakdown": market_cap,
        "pe_portfolio": round(pe_portfolio, 2) if pe_portfolio else None,
        "pb_portfolio": round(pb_portfolio, 2) if pb_portfolio else None,
        "pe_category": round(pe_category, 2) if pe_category else None,
        "pb_category": round(pb_category, 2) if pb_category else None,
        "morningstar_rating": int(morningstar_rating) if morningstar_rating else None,
        "morningstar_benchmark": morningstar_benchmark,
    }


def _normalize_ticker(ticker: str) -> str:
    """Add .US suffix for EODHD if no exchange suffix present."""
    if "." in ticker:
        return ticker
    normalized = f"{ticker}.US"
    logger.info(f"[TICKER] {ticker} → {normalized}")
    return normalized


def _currency_from_ticker(ticker: str) -> str:
    """Determine currency from ticker exchange suffix."""
    EXCHANGE_CURRENCY = {
        ".PA": "EUR",   # Euronext Paris
        ".AS": "EUR",   # Euronext Amsterdam
        ".BR": "EUR",   # Euronext Brussels
        ".LI": "EUR",   # Euronext Lisbon
        ".DE": "EUR",   # XETRA Frankfurt
        ".MI": "EUR",   # Milan
        ".MC": "EUR",   # Madrid
        ".HE": "EUR",   # Helsinki
        ".VI": "EUR",   # Vienna
        ".L": "GBP",    # London
        ".TO": "CAD",   # Toronto
        ".HK": "HKD",   # Hong Kong
        ".T": "JPY",    # Tokyo
        ".US": "USD",   # US exchanges
    }
    for suffix, currency in EXCHANGE_CURRENCY.items():
        if ticker.upper().endswith(suffix):
            logger.info(f"[CURRENCY] {ticker} → {currency}")
            return currency
    logger.info(f"[CURRENCY] {ticker} → USD (default)")
    return "USD"


def _fetch_eod(ticker: str) -> dict | None:
    """Fetch from EODHD. Returns parsed data dict or None."""
    eod_ticker = _normalize_ticker(ticker)
    logger.info(f"[EOD] Fetch start for {eod_ticker}")

    if not EOD_API_KEY:
        logger.warning("[EOD] No EOD_API_KEY set, skipping EOD")
        return None

    # Fetch real-time, fundamentals, and historical prices in parallel
    raw = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_eod_get, f"real-time/{eod_ticker}"): "realtime",
            executor.submit(_eod_get, f"fundamentals/{eod_ticker}"): "fundamentals",
            executor.submit(_fetch_eod_yearly_prices, eod_ticker): "yearly_prices",
        }
        for future in as_completed(futures):
            key = futures[future]
            raw[key] = future.result()

    realtime = raw.get("realtime")
    fundamentals = raw.get("fundamentals")
    yearly_prices = raw.get("yearly_prices", {})

    logger.info(f"[EOD] realtime: {'OK' if realtime else 'NONE'}")
    logger.info(f"[EOD] fundamentals: {'OK' if fundamentals else 'NONE'}")
    logger.info(f"[EOD] yearly_prices: {len(yearly_prices)} years")

    if not fundamentals:
        logger.error(f"[EOD ERROR] No fundamentals data for {eod_ticker}")
        return None

    return _parse_eod_data(fundamentals, realtime, eod_ticker, yearly_prices=yearly_prices)


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

    # FMP financial-growth only provides YoY, not CAGR — log it clearly
    rg_raw = _num(growth.get("revenueGrowth")) if growth else None
    revenue_growth = round(rg_raw * 100, 2) if rg_raw is not None else None
    if revenue_growth is not None:
        logger.info(f"[FMP] revenue_growth is YoY (not CAGR): {revenue_growth}%")

    eg_raw = _num(growth.get("epsgrowth")) if growth else None
    eps_growth = round(eg_raw * 100, 2) if eg_raw is not None else None

    total_cash = _num_or_zero(profile.get("totalCash")) if profile else 0
    total_debt = _num_or_zero(profile.get("totalDebt")) if profile else 0
    net_cash = total_cash - total_debt

    # ROIC — FMP has returnOnCapitalEmployed in ratios (close proxy)
    roce_raw = _num(ratios.get("returnOnCapitalEmployed")) if ratios else None
    roic = round(roce_raw * 100, 2) if roce_raw is not None else None

    # Debt to Equity
    de_raw = _num(ratios.get("debtEquityRatio")) if ratios else None
    debt_to_equity = round(de_raw, 2) if de_raw is not None else None

    data = {
        "current_price": current_price,
        "currency": currency,
        "revenue_growth": revenue_growth,
        "operating_margin": operating_margin,
        "roe": roe,
        "roic": roic,
        "debt_to_equity": debt_to_equity,
        "net_cash": net_cash,
        "fcf_per_share": fcf_per_share,
        "growth": eps_growth,
        "eps": eps,
    }

    data["missing_fields"] = [k for k, v in data.items() if v is None]
    return data


# ============================================================
# yfinance — FCF-only fallback (covers European stocks)
# ============================================================

def _fetch_fcf_yfinance(ticker: str) -> float | None:
    """Fetch FCF per share from yfinance. Used only when EOD+FMP have no FCF."""
    try:
        import yfinance as yf

        stock = yf.Ticker(ticker)
        cashflow = stock.cashflow  # DataFrame: rows=items, cols=dates (most recent first)

        if cashflow is None or cashflow.empty:
            logger.warning(f"[YFINANCE] No cashflow data for {ticker}")
            return None

        # Try "Free Cash Flow" row directly
        fcf = None
        if "Free Cash Flow" in cashflow.index:
            fcf = cashflow.loc["Free Cash Flow"].iloc[0]
        elif "Operating Cash Flow" in cashflow.index and "Capital Expenditure" in cashflow.index:
            # Manual fallback: OCF - CapEx (CapEx is negative in yfinance)
            ocf = cashflow.loc["Operating Cash Flow"].iloc[0]
            capex = cashflow.loc["Capital Expenditure"].iloc[0]
            if capex > 0:
                capex = -capex
            fcf = ocf + capex
            logger.info(f"[YFINANCE] Calculated FCF from OCF ({ocf}) + CapEx ({capex}) = {fcf}")

        if fcf is None or (hasattr(fcf, '__float__') and float(fcf) == 0):
            logger.warning(f"[YFINANCE] FCF is None or 0 for {ticker}")
            return None

        shares = stock.info.get("sharesOutstanding")
        if not shares or shares == 0:
            logger.warning(f"[YFINANCE] No sharesOutstanding for {ticker}")
            return None

        fcf_per_share = round(float(fcf) / float(shares), 4)
        logger.info(f"[YFINANCE] FCF per share for {ticker}: {fcf_per_share}")
        return fcf_per_share

    except Exception as e:
        logger.warning(f"[YFINANCE] Failed to get FCF for {ticker}: {e}")
        return None


# ============================================================
# Merge — fill missing EOD fields from FMP
# ============================================================

def _merge_data(primary: dict, secondary: dict) -> dict:
    """Fill None fields in primary (EOD) from secondary (FMP).

    Only overwrites fields listed in primary["missing_fields"].
    Returns the updated primary dict.
    """
    missing = primary.get("missing_fields", [])
    if not missing or not secondary:
        return primary

    filled = []
    for field in list(missing):
        value = secondary.get(field)
        if value is not None:
            primary[field] = value
            filled.append(field)
            logger.info(f"[MERGE] {field} filled from FMP: {value}")

    # Update missing_fields to reflect what's still missing
    primary["missing_fields"] = [f for f in missing if f not in filled]

    if filled:
        logger.info(f"[MERGE] Filled {len(filled)} fields from FMP: {filled}")
    else:
        logger.info("[MERGE] FMP had no additional data to fill")

    return primary


# ============================================================
# Main entry point — EOD first, FMP fallback/complement
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

    # --- 2. If EOD succeeded but has missing fields, complement with FMP ---
    if data is not None and data.get("missing_fields"):
        logger.info(f"[MERGE] EOD missing fields: {data['missing_fields']} — calling FMP to fill gaps")
        fmp_data = _fetch_fmp(ticker)
        if fmp_data:
            data = _merge_data(data, fmp_data)

    # --- 2b. If FCF still missing after merge, try yfinance ---
    if data is not None and "fcf_per_share" in data.get("missing_fields", []):
        logger.info(f"[YFINANCE] FCF still missing for {ticker}, trying yfinance...")
        yf_fcf = _fetch_fcf_yfinance(ticker)
        if yf_fcf is not None:
            data["fcf_per_share"] = yf_fcf
            data["missing_fields"] = [f for f in data["missing_fields"] if f != "fcf_per_share"]
            logger.info(f"[YFINANCE] FCF filled: {yf_fcf}")

    # --- 3. Full fallback to FMP if EOD failed entirely ---
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


def fetch_etf_data(ticker: str):
    """Fetch and parse ETF data from EODHD."""
    ticker = ticker.upper()
    logger.info(f"[ETF FETCH] === Starting ETF fetch for {ticker} ===")

    cached = _get_cached(f"ETF_{ticker}")
    if cached:
        return cached

    total_start = time.time()
    eod_ticker = _normalize_ticker(ticker)

    if not EOD_API_KEY:
        return {"success": False, "ticker": ticker, "error": "No EOD API key", "data": None}

    raw = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_eod_get, f"real-time/{eod_ticker}"): "realtime",
            executor.submit(_eod_get, f"fundamentals/{eod_ticker}"): "fundamentals",
        }
        for future in as_completed(futures):
            key = futures[future]
            raw[key] = future.result()

    realtime = raw.get("realtime")
    fundamentals = raw.get("fundamentals")

    if not fundamentals:
        return {"success": False, "ticker": ticker, "error": "No fundamentals data", "data": None}

    # Vérifier que c'est bien un ETF
    asset_type = fundamentals.get("General", {}).get("Type", "")
    if asset_type != "ETF":
        return {"success": False, "ticker": ticker, "error": f"Ce ticker est un {asset_type}, pas un ETF. Utilisez la commande Analyse classique.", "data": None}

    data = _parse_etf_data(fundamentals, realtime, eod_ticker)

    if data is None:
        return {"success": False, "ticker": ticker, "error": "ETF data parsing failed", "data": None}

    total_elapsed = round(time.time() - total_start, 2)
    logger.info(f"[ETF FETCH] Total time: {total_elapsed}s")

    result = {"success": True, "ticker": ticker, "error": None, "data": data}
    _set_cached(f"ETF_{ticker}", result)
    return result
