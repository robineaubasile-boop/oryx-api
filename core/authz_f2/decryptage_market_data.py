"""Politique FERMÉE des données financières d'un tour Décrypter F2 (R1 PR 9-B2) — module PUR.

Fournisseurs autorisés : EODHD (principal) puis FMP (repli ou complément
contrôlé). yfinance est INTERDIT : ce module n'en connaît ni le nom de
paquet ni les fonctions ; le point d'entrée historique
core.data_fetcher.fetch_financial_data (qui l'atteint) n'est jamais utilisé
par B2 (graphe d'appels vérifié par les tests). Les transports sont injectés
(core.decryptage_providers en production, doubles dans les tests) ; ce
module ne fait que valider et convertir leurs réponses JSON déjà décodées.

Règles :

- identité de l'instrument conservée et vérifiée (EODHD General.Code = code
  résolu, ISIN identique s'il est connu des deux côtés ; FMP symbol = ticker
  résolu) ; sinon la réponse est inexploitable ;
- devise de cotation obligatoire (^[A-Z]{3}$) ; seule conversion : GBX ->
  GBP (division exacte par 100 du prix, tracée dans unit_conversion) ; aucune
  autre conversion de devise ;
- une valeur monétaire issue des états financiers n'est retenue que si la
  devise des états (currency_symbol EODHD, reportedCurrency FMP) est
  EXACTEMENT la devise de cotation ; sinon statut currency_mismatch ;
- chaque champ porte sa source, sa période (FY:<date>, TTM, CAGR:<FY>..<FY>,
  REALTIME, PROFILE), son unité et sa devise ; un champ absent reste absent
  (jamais zéro, jamais une moyenne, jamais une estimation : ni
  EPSEstimateCurrentYear, ni taux d'impôt par défaut) ; NaN, infini, booléen
  ou chaîne non numérique : absent ;
- aucune fusion de périodes ou de définitions incompatibles : la croissance
  FMP (annuelle, YoY) n'est jamais présentée comme le CAGR EODHD ; un
  complément FMP n'est accepté que pour un champ de MÊME définition, MÊME
  exercice (date de clôture identique) et, s'il est monétaire, même devise ;
- données suffisantes : au moins 3 des 5 champs clés (revenue_growth,
  operating_margin, fcf_per_share, roe, eps), seuil historique.

Les textes fournisseurs (nom, secteur) sont des CONTENUS non fiables :
bornés, sans caractère de contrôle, jamais des instructions.
Ne lit aucune variable d'environnement, n'ouvre aucune connexion.
"""
import dataclasses
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_instrument as di

SNAPSHOT_VERSION = "oryx-decryptage-market-data-v1"
EODHD = "eodhd"
FMP = "fmp"
SOURCES = (EODHD, FMP)
PRESENT = "present"
ABSENT = "absent"
CURRENCY_MISMATCH = "currency_mismatch"
FIELD_STATUSES = (PRESENT, ABSENT, CURRENCY_MISMATCH)
# Fraîcheur FERMÉE du cours (audit P2-01). Seul PRICE_RECENT peut être
# présenté comme le cours actuel ; il exige un seuil EXPLICITE fourni par la
# composition (aucun seuil par défaut : à arbitrer par le fondateur) et une
# date de cotation fiable, comparée à l'horloge PostgreSQL de la lecture.
PRICE_RECENT = "recent"            # cours daté, âge vérifié <= seuil explicite
PRICE_DATED = "dated"              # dernier cours connu, daté, fraîcheur non établie
PRICE_UNDATED = "undated"          # cours sans date de cotation fiable (FMP profile, timestamp absent)
PRICE_UNAVAILABLE = "unavailable"  # aucun cours
PRICE_FRESHNESS = (PRICE_RECENT, PRICE_DATED, PRICE_UNDATED, PRICE_UNAVAILABLE)

# Champ -> (unité, monétaire). Ordre fermé.
FIELDS = {
    "current_price": ("currency", True),
    "revenue_growth": ("percent_cagr", False),
    "operating_margin": ("percent", False),
    "gross_margin_latest": ("percent", False),
    "roe": ("percent", False),
    "roic": ("percent", False),
    "eps": ("currency_per_share", True),
    "fcf_per_share": ("currency_per_share", True),
    "net_cash": ("currency", True),
    "debt_to_equity": ("ratio", False),
}
FIELD_NAMES = tuple(FIELDS)
CORE_FIELDS = ("revenue_growth", "operating_margin", "fcf_per_share", "roe", "eps")
MIN_CORE_FIELDS = 3
# Complément FMP : champ -> exercice EODHD de référence.
COMPLEMENTABLE = {"operating_margin": "income", "gross_margin_latest": "income", "debt_to_equity": "balance",
                  "fcf_per_share": "cash_flow"}
CURRENCY_RE = re.compile(r"[A-Z]{3}", re.ASCII)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
NUMBER_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?", re.ASCII)
NAME_MAX_CHARS = 200
SECTOR_MAX_CHARS = 100
PERIOD_RE = re.compile(r"(?:FY:[0-9]{4}-[0-9]{2}-[0-9]{2}|TTM|REALTIME|PROFILE|"
                       r"CAGR:FY[0-9]{4}-[0-9]{2}-[0-9]{2}\.\.FY[0-9]{4}-[0-9]{2}-[0-9]{2})", re.ASCII)


def number(value):
    """float fini, ou None (absent) : jamais de zéro par défaut. Un entier
    non représentable en binary64 (OverflowError de float()) ou une chaîne
    hors domaine (float() rend ±inf) est absent, jamais une erreur ni un
    infini (audit P2-02) ; zéro réellement fourni reste 0.0."""
    if type(value) is int:
        try:
            result = float(value)
        except OverflowError:
            return None
    elif type(value) is float:
        result = value
    elif type(value) is str and NUMBER_RE.fullmatch(value.strip()):
        result = float(value.strip())
    else:
        return None
    return result + 0.0 if math.isfinite(result) else None


def label(value, max_chars):
    """Texte fournisseur borné, sans caractère de contrôle ; sinon None."""
    if type(value) is not str:
        return None
    value = value.strip()
    if not value or len(value) > max_chars or any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")
                                                   for ch in value):
        return None
    return value


def _currency(value):
    return value if type(value) is str and CURRENCY_RE.fullmatch(value) else None


@dataclass(frozen=True, slots=True)
class FinancialField:
    name: str
    status: str
    value: object = None   # float fini si present
    source: object = None  # eodhd | fmp si present
    period: object = None  # période fermée si present

    def __post_init__(self):
        if self.name not in FIELDS or self.status not in FIELD_STATUSES:
            raise ValueError("champ financier hors contrat")
        if self.status == PRESENT:
            if type(self.value) is not float or not math.isfinite(self.value) or self.source not in SOURCES \
                    or type(self.period) is not str or not PERIOD_RE.fullmatch(self.period):
                raise ValueError("champ financier présent hors contrat")
        elif self.value is not None or self.source is not None or self.period is not None:
            raise ValueError("champ financier absent : aucune valeur")

    @property
    def unit(self) -> str:
        return FIELDS[self.name][0]

    @property
    def present(self) -> bool:
        return self.status == PRESENT


def _present(name, value, source, period) -> FinancialField:
    """Champ présent seulement pour une valeur FINIE : un dérivé qui déborde
    (produit, quotient, arrondi) est absent, jamais un infini (audit P2-02)."""
    if value is None or not math.isfinite(value):
        return FinancialField(name, ABSENT)
    return FinancialField(name, PRESENT, value, source, period)


@dataclass(frozen=True, slots=True)
class FinancialSnapshot:
    """Instantané financier contrôlé et immuable (aucun dict mutable)."""
    version: str
    ticker: str
    provider_symbol: str
    instrument_code: str
    instrument_isin: object
    primary_source: str
    name: object
    sector: object
    currency: str
    unit_conversion: object      # "GBX->GBP" ou None
    price_as_of: object          # ISO 8601 UTC ou None (fraîcheur inconnue)
    fundamentals_updated_at: object
    statement_currency: object
    income_fy: object            # dates de clôture EODHD de référence (complément)
    balance_fy: object
    cash_flow_fy: object
    fields: tuple
    complemented: tuple          # champs complétés par FMP
    price_freshness: str         # PRICE_FRESHNESS (audit P2-01)
    price_reference_at: object = None  # instant PostgreSQL de qualification (ISO 8601) ou None

    def __post_init__(self):
        if self.version != SNAPSHOT_VERSION or not dc.valid_resolved_ticker(self.ticker):
            raise ValueError("instantané financier : version ou ticker hors contrat")
        if self.primary_source not in SOURCES or _currency(self.currency) is None:
            raise ValueError("instantané financier : source ou devise hors contrat")
        if type(self.fields) is not tuple or tuple(f.name for f in self.fields) != FIELD_NAMES \
                or any(type(f) is not FinancialField for f in self.fields):
            raise ValueError("instantané financier : champs hors contrat")
        if type(self.complemented) is not tuple or any(
                n not in COMPLEMENTABLE or self.field(n).source != FMP for n in self.complemented):
            raise ValueError("instantané financier : complément hors contrat")
        for text in (self.provider_symbol, self.instrument_code):
            if type(text) is not str or not text:
                raise ValueError("instantané financier : identité hors contrat")
        for optional in (self.instrument_isin, self.name, self.sector, self.unit_conversion, self.price_as_of,
                         self.fundamentals_updated_at, self.statement_currency, self.income_fy, self.balance_fy,
                         self.cash_flow_fy):
            if optional is not None and type(optional) is not str:
                raise ValueError("instantané financier : métadonnée hors contrat")
        price = self.field("current_price").present
        expected = (PRICE_UNAVAILABLE,) if not price else (PRICE_UNDATED,) if self.price_as_of is None else (
            PRICE_DATED, PRICE_RECENT)
        if self.price_freshness not in expected or (self.price_reference_at is not None
                                                    and type(self.price_reference_at) is not str):
            raise ValueError("instantané financier : fraîcheur du cours incohérente")
        if self.price_freshness == PRICE_RECENT and self.price_reference_at is None:
            raise ValueError("instantané financier : cours récent sans instant de référence")

    def field(self, name: str) -> FinancialField:
        return self.fields[FIELD_NAMES.index(name)]

    def value(self, name: str):
        return self.field(name).value

    @property
    def sufficient(self) -> bool:
        return sum(1 for n in CORE_FIELDS if self.field(n).present) >= MIN_CORE_FIELDS

    def missing_complementable(self) -> tuple:
        return tuple(n for n in COMPLEMENTABLE if self.field(n).status == ABSENT)

    def public_price(self):
        """Cours présentable comme ACTUEL (payload, « Prix actuel ») : seulement
        PRICE_RECENT ; sinon None (le dernier cours connu reste dans le
        snapshot, avec sa date et sa provenance)."""
        return self.value("current_price") if self.price_freshness == PRICE_RECENT else None

    def prompt_data(self) -> dict:
        """Nouveau dict (copie) au format attendu par
        core.decryptage_engine.build_system_prompt : champs absents = None
        (affichés N/D) ; nom et secteur omis s'ils sont inconnus ; le moteur
        affiche current_price comme « Prix actuel » : seul un cours
        PRICE_RECENT y figure (audit P2-01)."""
        data = {n: self.value(n) for n in FIELD_NAMES}
        data["current_price"] = self.public_price()
        data["currency"] = self.currency
        if self.name is not None:
            data["name"] = self.name
        if self.sector is not None:
            data["sector"] = self.sector
        return data

    def canonical(self) -> dict:
        return {"version": self.version, "ticker": self.ticker, "provider_symbol": self.provider_symbol,
                "instrument_code": self.instrument_code, "instrument_isin": self.instrument_isin,
                "primary_source": self.primary_source, "name": self.name, "sector": self.sector,
                "currency": self.currency, "unit_conversion": self.unit_conversion, "price_as_of": self.price_as_of,
                "fundamentals_updated_at": self.fundamentals_updated_at,
                "statement_currency": self.statement_currency, "income_fy": self.income_fy,
                "balance_fy": self.balance_fy, "cash_flow_fy": self.cash_flow_fy,
                "complemented": list(self.complemented), "price_freshness": self.price_freshness,
                "price_reference_at": self.price_reference_at,
                "fields": [{"name": f.name, "status": f.status, "value": f.value, "source": f.source,
                            "period": f.period, "unit": f.unit} for f in self.fields]}


def _freshness(price, as_of) -> str:
    """Fraîcheur constatée par le parseur (jamais « récente » : seule
    qualify_price peut l'établir)."""
    if price is None:
        return PRICE_UNAVAILABLE
    return PRICE_UNDATED if as_of is None else PRICE_DATED


def qualify_price(snapshot: FinancialSnapshot, reference: datetime, max_age) -> FinancialSnapshot:
    """Qualifie la fraîcheur du cours à l'instant PostgreSQL `reference`
    (read_at de la seconde lecture). PRICE_RECENT seulement si max_age est
    un seuil EXPLICITE (timedelta > 0) ET si 0 <= reference - date de
    cotation <= max_age ; une date future ou trop ancienne reste
    PRICE_DATED ; aucun seuil (None, défaut) : jamais récent. Ne crée ni ne
    modifie aucune date."""
    if type(reference) is not datetime or reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("instant de référence PostgreSQL attendu")
    if max_age is not None and (type(max_age) is not timedelta or max_age <= timedelta(0)):
        raise ValueError("seuil de fraîcheur : timedelta strictement positif ou None")
    status = snapshot.price_freshness
    if status in (PRICE_DATED, PRICE_RECENT):
        age = reference - datetime.fromisoformat(snapshot.price_as_of)
        status = PRICE_RECENT if max_age is not None and timedelta(0) <= age <= max_age else PRICE_DATED
    return dataclasses.replace(snapshot, price_freshness=status,
                               price_reference_at=reference.astimezone(timezone.utc).isoformat())


def eodhd_symbol(resolution: di.InstrumentResolution) -> str:
    """Symbole EODHD (règle historique : suffixe .US sans place)."""
    return resolution.ticker if "." in resolution.ticker else f"{resolution.ticker}.US"


def _yearly(fundamentals: dict, section: str) -> list:
    """[(date, entrée)] des exercices annuels valides, du plus récent au plus ancien."""
    financials = fundamentals.get("Financials")
    statement = financials.get(section) if type(financials) is dict else None
    yearly = statement.get("yearly") if type(statement) is dict else None
    if type(yearly) is not dict:
        return []
    entries = [(k, v) for k, v in yearly.items() if type(k) is str and DATE_RE.fullmatch(k) and type(v) is dict]
    return sorted(entries, key=lambda kv: kv[0], reverse=True)


def _cagr(income: list):
    """CAGR sur des exercices CONSÉCUTIFS depuis le plus récent (5 au plus)."""
    run = []
    for date, entry in income[:5]:
        revenue = number(entry.get("totalRevenue"))
        if revenue is None or revenue <= 0 or (run and int(date[:4]) != int(run[-1][0][:4]) - 1):
            break
        run.append((date, revenue))
    if len(run) < 2:
        return None, None
    years = len(run) - 1
    value = round(((run[0][1] / run[-1][1]) ** (1 / years) - 1) * 100, 2)
    return (value, f"CAGR:FY{run[-1][0]}..FY{run[0][0]}") if math.isfinite(value) else (None, None)


def _ratio(numerator, denominator, scale=100.0, digits=2):
    if numerator is None or denominator is None or not math.isfinite(numerator) \
            or not math.isfinite(denominator) or denominator <= 0:
        return None
    value = round(numerator / denominator * scale, digits)
    return value if math.isfinite(value) else None


def _free_cash_flow(entry: dict):
    fcf = number(entry.get("freeCashFlow"))
    if fcf is not None:
        return fcf
    ocf, capex = number(entry.get("totalCashFromOperatingActivities")), number(entry.get("capitalExpenditures"))
    if ocf is None or capex is None:
        return None
    return ocf + (-capex if capex > 0 else capex)


def _iso_timestamp(value):
    if type(value) is not int or isinstance(value, bool) or not 0 < value < 32503680000:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def parse_eodhd(resolution: di.InstrumentResolution, fundamentals, realtime):
    """FinancialSnapshot EODHD, ou None si la réponse est inexploitable
    (structure, identité, devise). La suffisance est jugée par l'appelant."""
    if type(fundamentals) is not dict:
        return None
    general = fundamentals.get("General")
    if type(general) is not dict or general.get("Code") != resolution.code:
        return None
    isin = general.get("ISIN") if type(general.get("ISIN")) is str and general.get("ISIN") else None
    if resolution.isin is not None and isin is not None and isin != resolution.isin:
        return None
    raw_currency = _currency(general.get("CurrencyCode"))
    if raw_currency is None:
        return None
    currency, conversion = ("GBP", "GBX->GBP") if raw_currency == "GBX" else (raw_currency, None)
    symbol = eodhd_symbol(resolution)
    highlights = fundamentals.get("Highlights") if type(fundamentals.get("Highlights")) is dict else {}
    shares_stats = fundamentals.get("SharesStats") if type(fundamentals.get("SharesStats")) is dict else {}
    income, balance, cash_flow = (_yearly(fundamentals, s) for s in ("Income_Statement", "Balance_Sheet",
                                                                      "Cash_Flow"))
    statement_currency = None
    if income:
        statement_currency = _currency(income[0][1].get("currency_symbol"))
    monetary_ok = statement_currency is not None and statement_currency == currency

    price, as_of = None, None
    if type(realtime) is dict and realtime.get("code") == symbol:
        close = number(realtime.get("close"))
        if close is not None and close > 0:
            price = close / 100 if conversion else close
            as_of = _iso_timestamp(realtime.get("timestamp"))
    values = {"current_price": (price, "REALTIME")}
    revenue_growth = _cagr(income)
    values["revenue_growth"] = revenue_growth
    if income:
        date, entry = income[0]
        revenue = number(entry.get("totalRevenue"))
        operating_income = number(entry.get("operatingIncome"))
        values["operating_margin"] = (_ratio(operating_income, revenue), f"FY:{date}")
        values["gross_margin_latest"] = (_ratio(number(entry.get("grossProfit")), revenue), f"FY:{date}")
    roe = number(highlights.get("ReturnOnEquityTTM"))
    values["roe"] = (round(roe * 100, 2) if roe is not None else None, "TTM")
    values["eps"] = (number(highlights.get("EarningsShare")), "TTM")
    shares = number(shares_stats.get("SharesOutstanding"))
    if shares is None:
        shares = number(highlights.get("SharesOutstanding"))
    if cash_flow:
        date, entry = cash_flow[0]
        values["fcf_per_share"] = (_ratio(_free_cash_flow(entry), shares, 1.0, 4), f"FY:{date}")
    if balance:
        date, entry = balance[0]
        cash, debt = number(entry.get("cashAndShortTermInvestments")), number(entry.get("shortLongTermDebtTotal"))
        equity = number(entry.get("totalStockholderEquity"))
        net = cash - debt if cash is not None and debt is not None else None
        values["net_cash"] = (net if net is None or math.isfinite(net) else None, f"FY:{date}")
        values["debt_to_equity"] = (_ratio(debt, equity, 1.0, 2), f"FY:{date}")
        if income and income[0][0] == date and cash is not None and debt is not None and equity is not None:
            entry_i = income[0][1]
            tax, pretax = number(entry_i.get("incomeTaxExpense")), number(entry_i.get("incomeBeforeTax"))
            operating_income = number(entry_i.get("operatingIncome"))
            if tax is not None and pretax not in (None, 0.0) and operating_income is not None:
                nopat = operating_income * (1 - tax / pretax)
                values["roic"] = (_ratio(nopat, equity + debt - cash), f"FY:{date}")
    fields = []
    for name in FIELD_NAMES:
        value, period = values.get(name, (None, None))
        if value is not None and FIELDS[name][1] and name != "current_price" and not monetary_ok:
            fields.append(FinancialField(name, CURRENCY_MISMATCH))
        else:
            fields.append(_present(name, value, EODHD, period))
    updated = general.get("UpdatedAt") if type(general.get("UpdatedAt")) is str and DATE_RE.fullmatch(
        general.get("UpdatedAt")) else None
    return FinancialSnapshot(SNAPSHOT_VERSION, resolution.ticker, symbol, resolution.code, isin or resolution.isin,
                             EODHD, label(general.get("Name"), NAME_MAX_CHARS),
                             label(general.get("Sector"), SECTOR_MAX_CHARS), currency, conversion, as_of, updated,
                             statement_currency, income[0][0] if income else None,
                             balance[0][0] if balance else None, cash_flow[0][0] if cash_flow else None,
                             tuple(fields), (), _freshness(fields[0].value, as_of))


def _first(raw):
    if type(raw) is list:
        return raw[0] if raw and type(raw[0]) is dict else None
    return raw if type(raw) is dict and raw else None


def _fmp_ratios(raw, symbol: str):
    """Dernier exercice annuel FMP (symbole exact, date, période FY), ou None."""
    ratios = _first(raw)
    if ratios is None or ratios.get("symbol") != symbol:
        return None
    date = ratios.get("date")
    if type(date) is not str or not DATE_RE.fullmatch(date) or ratios.get("period") not in (None, "FY", "annual"):
        return None
    return ratios


def _fmp_values(ratios: dict) -> dict:
    date = ratios["date"]
    period = f"FY:{date}"

    def percent(key):
        value = number(ratios.get(key))
        return (round(value * 100, 2) if value is not None else None, period)

    debt_to_equity = number(ratios.get("debtEquityRatio"))
    if debt_to_equity is None:
        debt_to_equity = number(ratios.get("debtToEquityRatio"))
    return {"operating_margin": percent("operatingProfitMargin"), "gross_margin_latest": percent("grossProfitMargin"),
            "roe": percent("returnOnEquity"), "debt_to_equity": (debt_to_equity, period),
            "fcf_per_share": (number(ratios.get("freeCashFlowPerShare")), period)}


def complement(snapshot: FinancialSnapshot, ratios_raw) -> FinancialSnapshot:
    """Complète, sans rien écraser, les champs ABSENTS complémentables par
    un exercice FMP de même date de clôture (et même devise s'il est
    monétaire). Toute autre différence : aucun complément."""
    ratios = _fmp_ratios(ratios_raw, snapshot.ticker)
    if ratios is None:
        return snapshot
    reported = _currency(ratios.get("reportedCurrency"))
    values = _fmp_values(ratios)
    references = {"income": snapshot.income_fy, "balance": snapshot.balance_fy, "cash_flow": snapshot.cash_flow_fy}
    fields, filled = list(snapshot.fields), []
    for name in snapshot.missing_complementable():
        value, period = values[name]
        if value is None or references[COMPLEMENTABLE[name]] != ratios["date"]:
            continue
        if FIELDS[name][1] and reported != snapshot.currency:
            continue
        fields[FIELD_NAMES.index(name)] = FinancialField(name, PRESENT, value, FMP, period)
        filled.append(name)
    if not filled:
        return snapshot
    return dataclasses.replace(snapshot, fields=tuple(fields), complemented=tuple(filled))


def parse_fmp(resolution: di.InstrumentResolution, profile_raw, ratios_raw):
    """Repli FMP complet (EODHD inexploitable), ou None. La croissance FMP
    (YoY) n'est jamais un CAGR : revenue_growth reste absent ; aucun EPS ni
    trésorerie nette inventés."""
    profile = _first(profile_raw)
    symbol = resolution.ticker
    if profile is None or profile.get("symbol") != symbol:
        return None
    isin = profile.get("isin") if type(profile.get("isin")) is str and profile.get("isin") else None
    if resolution.isin is not None and isin is not None and isin != resolution.isin:
        return None
    currency = _currency(profile.get("currency"))
    if currency is None or currency == "GBX":
        return None  # aucune conversion implicite sur le repli
    price = number(profile.get("price"))
    values = {"current_price": (price if price is not None and price > 0 else None, "PROFILE")}
    ratios = _fmp_ratios(ratios_raw, symbol)
    date = None
    if ratios is not None:
        date = ratios["date"]
        values.update(_fmp_values(ratios))
        if _currency(ratios.get("reportedCurrency")) != currency:
            values["fcf_per_share"] = ("mismatch", None) if values["fcf_per_share"][0] is not None else (None, None)
    fields = []
    for name in FIELD_NAMES:
        value, period = values.get(name, (None, None))
        if value == "mismatch":
            fields.append(FinancialField(name, CURRENCY_MISMATCH))
        else:
            fields.append(_present(name, value, FMP, period))
    return FinancialSnapshot(SNAPSHOT_VERSION, resolution.ticker, symbol, resolution.code, isin or resolution.isin,
                             FMP, label(profile.get("companyName"), NAME_MAX_CHARS),
                             label(profile.get("sector"), SECTOR_MAX_CHARS), currency, None, None, None,
                             _currency(ratios.get("reportedCurrency")) if ratios is not None else None, date, date,
                             date, tuple(fields), (), _freshness(fields[0].value, None))
