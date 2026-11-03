"""Identification de l'instrument d'un tour Décrypter F2 (R1 PR 9-B2) — module PUR.

Sépare strictement :

- ticker_input : saisie normalisée localement (strip().upper()), signée dans
  l'intention HMAC B1, JAMAIS modifiée ;
- resolved_ticker : instrument effectivement identifié, au format historique
  de core.ticker_resolver (code nu pour une cotation US, CODE.PLACE sinon),
  pour rester comparable aux AnalysisSession existantes ;
- le résultat de validation (InstrumentResolution) : identified, ambiguous,
  not_found ou unavailable, avec sa provenance.

Ni le fallback brut de normalize_ticker() ni un strip().upper() ne sont une
preuve : hors table des cas vérifiés manuellement, un instrument n'est
identifié que par les résultats EODHD Search (UNE recherche, transport
injecté, jamais appelé ici) :

1. table _KNOWN_TICKER_OVERRIDES (vérifiée manuellement, recopiée) :
   identified / known_override, aucun appel ;
2. symbole qualifié CODE.PLACE (place connue) : une ligne (Code, Exchange)
   exacte, d'un type admis ; plusieurs ISIN distincts : ambiguous ;
3. symbole de forme US (1-5 lettres) : ligne (Code, 'US') exacte, d'un type
   admis, sans le même code sur une autre place pour un autre ISIN (sinon
   ambiguous) ; sinon étape 4 sur les mêmes résultats ;
4. code exact (toutes places) ET/OU groupe de nom normalisé EXACTEMENT égal
   à la requête : l'identité de l'entreprise exige UN seul ISIN non vide
   parmi les cotations retenues ; code et nom désignant deux ISIN distincts,
   zéro ou plusieurs ISIN : ambiguous ; un nom seulement apparenté (préfixe),
   ou un nom apparenté d'une AUTRE entreprise (autre ISIN) à côté du nom
   exact : ambiguous ; rien : not_found. La cotation est ensuite choisie parmi celles
   de CETTE entreprise par le classement historique (_rank, copie fidèle de
   core.ticker_resolver._pick_best_match, équivalence vérifiée par les
   tests). Jamais de choix entre homonymes au nombre de pays, jamais de repli
   sur la saisie brute.

Une réponse mal formée (pas une liste de dictionnaires de chaînes, plus de
MAX_RESULTS lignes) est inexploitable : unavailable (échec fournisseur), comme
une panne ou un délai dépassé. Aucun ticker n'est persisté (PR 9-C).

Constantes recopiées de core.ticker_resolver (la frontière n'importe aucun
module réseau) : égalité vérifiée par les tests. Ne lit aucune variable
d'environnement, n'ouvre aucune connexion.
"""
import re
import unicodedata
from dataclasses import dataclass

from core.authz_f2 import decryptage_context as dc

IDENTIFIED = "identified"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
STATUSES = (IDENTIFIED, AMBIGUOUS, NOT_FOUND, UNAVAILABLE)
KNOWN_OVERRIDE = "known_override"
EXACT_SYMBOL = "exact_symbol"
EXACT_COMPANY_NAME = "exact_company_name"
PROVENANCES = (KNOWN_OVERRIDE, EXACT_SYMBOL, EXACT_COMPANY_NAME)
MAX_RESULTS = 50
RESULT_FIELDS = ("Code", "Exchange", "Name", "Type", "Country", "Isin")
MAX_FIELD_CHARS = 256

# --- Copies de core.ticker_resolver (égalité vérifiée par les tests) -------
PEA_EXCHANGES = ["PA", "AS", "BR", "LS", "MC", "MI", "XETRA", "DE", "IR", "HE", "CO", "ST", "VI"]
OTHER_EXCHANGES = ["LSE", "L", "SW", "OL", "US"]
ALLOWED_TYPES = ("Common Stock", "Preferred Stock", "ETF", "Fund", "Mutual Fund")
KNOWN_TICKER_OVERRIDES = {
    "LVMH": "MC.PA", "ASML": "ASML.AS", "ASTRAZENECA": "AZN.LSE", "BAYER": "BAYN.XETRA",
    "EXXON MOBIL": "XOM.US", "NETFLIX": "NFLX.US", "AMAZON": "AMZN.US", "MICROSOFT": "MSFT.US",
    "GOOGLE": "GOOGL.US", "ALPHABET": "GOOGL.US",
}
US_TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
_LEGAL_SUFFIX_PATTERN = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|SE|PLC|SA|NV|AG|"
    r"LTD|LIMITED|LLC|LP|HOLDING|HOLDINGS|AKTIENGESELLSCHAFT|"
    r"GMBH|KGAA|SPA|BV|OYJ|ASA|CEDEAR|ADR|CDR|CLASS [A-Z]|CL [A-Z])\b"
)
_RATIO_SUFFIX_PATTERN = re.compile(r"\b\d+\s*/\s*\d+\b")
_CAD_HEDGED_PATTERN = re.compile(r"\(CAD HEDGED\)", re.IGNORECASE)
_OTC_ADR_SUFFIX_PATTERN = re.compile(r"^[A-Z]{3,5}[YF]$")


def normalize_company_name(name: str) -> str:
    """Copie de core.ticker_resolver._normalize_company_name."""
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


def is_likely_otc_adr(code: str) -> bool:
    return bool(_OTC_ADR_SUFFIX_PATTERN.match(code or ""))


def fix_mojibake(text):
    """Copie de core.ticker_resolver._fix_mojibake (réparation défensive)."""
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def looks_like_eu_ticker(s: str) -> bool:
    if "." not in s or " " in s or len(s) > 15:
        return False
    parts = s.split(".")
    if len(parts) != 2:
        return False
    return parts[1] in PEA_EXCHANGES or parts[1] in OTHER_EXCHANGES


def _rank(item: dict, effective_prefer_us: bool, query: str) -> tuple:
    """Classement historique des cotations d'UNE entreprise (copie de rank()
    dans core.ticker_resolver._pick_best_match)."""
    exchange = item.get("Exchange") or ""
    code = item.get("Code") or ""
    item_type = item.get("Type") or ""
    if effective_prefer_us and exchange == "US" and item_type == "Common Stock" and not is_likely_otc_adr(code):
        base_rank = 0
    elif exchange in PEA_EXCHANGES:
        base_rank = 1 + PEA_EXCHANGES.index(exchange)
    elif exchange in OTHER_EXCHANGES and exchange != "US":
        base_rank = 50 + OTHER_EXCHANGES.index(exchange)
    elif exchange == "US" and item_type == "Common Stock" and not is_likely_otc_adr(code):
        base_rank = 100
    else:
        base_rank = 500
    if exchange == "LSE" and code[:1].isdigit():
        base_rank = 900
    exact_match = 0 if code.upper() == query.upper() else 1
    return (base_rank, exact_match)


def format_ticker(code: str, exchange: str) -> str:
    """Format historique : code nu pour US, CODE.PLACE sinon."""
    if exchange == "US":
        return code
    return f"{code}.{exchange}" if exchange else code


@dataclass(frozen=True, slots=True)
class InstrumentResolution:
    """Résultat fermé de l'identification. identified : ticker canonique,
    code, place, provenance ; nom et ISIN EODHD s'ils sont connus (jamais
    inventés). Autres statuts : aucun ticker."""
    status: str
    ticker: object = None
    code: object = None
    exchange: object = None
    provenance: object = None
    name: object = None
    isin: object = None

    def __post_init__(self):
        if type(self.status) is not str or self.status not in STATUSES:
            raise ValueError("résolution : statut hors contrat")
        if self.status == IDENTIFIED:
            if not dc.valid_resolved_ticker(self.ticker) or self.provenance not in PROVENANCES:
                raise ValueError("résolution : instrument identifié hors contrat")
            if type(self.code) is not str or not self.code or type(self.exchange) is not str:
                raise ValueError("résolution : code ou place hors contrat")
            if format_ticker(self.code, self.exchange) != self.ticker and not (
                    self.provenance == KNOWN_OVERRIDE and f"{self.code}.{self.exchange}" == self.ticker):
                raise ValueError("résolution : ticker incohérent avec le code et la place")
            for optional in (self.name, self.isin):
                if optional is not None and (type(optional) is not str or not optional):
                    raise ValueError("résolution : métadonnée hors contrat")
        elif any(v is not None for v in (self.ticker, self.code, self.exchange, self.provenance, self.name,
                                         self.isin)):
            raise ValueError("résolution : aucun instrument sans identification")

    @property
    def identified(self) -> bool:
        return self.status == IDENTIFIED


UNAVAILABLE_RESOLUTION = InstrumentResolution(UNAVAILABLE)
NOT_FOUND_RESOLUTION = InstrumentResolution(NOT_FOUND)
AMBIGUOUS_RESOLUTION = InstrumentResolution(AMBIGUOUS)


def known_override(ticker_input: str):
    """Résolution sans appel (table vérifiée manuellement), ou None."""
    resolved = KNOWN_TICKER_OVERRIDES.get(ticker_input)
    if resolved is None:
        return None
    code, exchange = resolved.rsplit(".", 1)
    return InstrumentResolution(IDENTIFIED, resolved, code, exchange, KNOWN_OVERRIDE)


def _admit_results(raw):
    """Liste fermée de lignes {champ: str | None}, ou None si inexploitable."""
    if raw is None:
        return []
    if type(raw) is not list or len(raw) > MAX_RESULTS:
        return None
    rows = []
    for item in raw:
        if type(item) is not dict:
            return None
        row = {}
        for field in RESULT_FIELDS:
            value = item.get(field)
            if value is not None and (type(value) is not str or len(value) > MAX_FIELD_CHARS or "\x00" in value):
                return None
            row[field] = value
        row["Name"] = fix_mojibake(row["Name"])
        rows.append(row)
    return rows


def _isins(rows) -> set:
    return {r["Isin"] for r in rows if r["Isin"]}


def _single(rows, provenance: str, ticker: str) -> InstrumentResolution:
    """Symbole exact sur sa place : unique par construction ; plusieurs ISIN
    distincts pour la même ligne de cotation : ambiguous."""
    isins = _isins(rows)
    if len(isins) > 1:
        return AMBIGUOUS_RESOLUTION
    first = rows[0]
    names = {normalize_company_name(r["Name"] or "") for r in rows}
    name = first["Name"] if len(names) == 1 and first["Name"] else None
    return InstrumentResolution(IDENTIFIED, ticker, first["Code"], first["Exchange"], provenance, name,
                                next(iter(isins)) if isins else None)


def resolve(ticker_input: str, raw_results) -> InstrumentResolution:
    """Décision pure sur les résultats d'UNE recherche EODHD de ticker_input
    (déjà normalisé B1). Le cas known_override est traité avant tout appel."""
    if type(ticker_input) is not str or not ticker_input:
        raise ValueError("ticker_input normalisé attendu")
    override = known_override(ticker_input)
    if override is not None:
        return override
    rows = _admit_results(raw_results)
    if rows is None:
        return UNAVAILABLE_RESOLUTION  # réponse fournisseur inexploitable
    eligible = [r for r in rows if r["Type"] in ALLOWED_TYPES and r["Code"]]
    if looks_like_eu_ticker(ticker_input):
        code, exchange = ticker_input.split(".")
        exact = [r for r in eligible if r["Code"] == code and r["Exchange"] == exchange]
        if not exact:
            return NOT_FOUND_RESOLUTION
        return _single(exact, EXACT_SYMBOL, ticker_input) if dc.valid_resolved_ticker(ticker_input) \
            else AMBIGUOUS_RESOLUTION
    if US_TICKER_PATTERN.match(ticker_input):
        exact_us = [r for r in eligible if r["Code"] == ticker_input and r["Exchange"] == "US"]
        if exact_us:
            elsewhere = _isins([r for r in eligible if r["Code"] == ticker_input and r["Exchange"] != "US"])
            if elsewhere - _isins(exact_us):
                return AMBIGUOUS_RESOLUTION  # même code ailleurs pour une autre entreprise : homonymie
            return _single(exact_us, EXACT_SYMBOL, ticker_input)
    return _by_company(ticker_input, eligible)


def _by_company(query: str, eligible) -> InstrumentResolution:
    code_hits = [r for r in eligible if r["Code"] == query]
    normalized = normalize_company_name(query)
    groups = {}
    for r in eligible:
        groups.setdefault(normalize_company_name(r["Name"] or ""), []).append(r)
    name_hits = groups.get(normalized, []) if normalized else []
    candidates = [(EXACT_SYMBOL, code_hits), (EXACT_COMPANY_NAME, name_hits)]
    candidates = [(p, hits) for p, hits in candidates if hits]
    if not candidates:
        related = normalized and any(k.startswith(normalized + " ") or normalized.startswith(k + " ")
                                     for k in groups if k)
        return AMBIGUOUS_RESOLUTION if related else NOT_FOUND_RESOLUTION
    identities = []
    for _, hits in candidates:
        isins = _isins([r for r in hits if not (r["Exchange"] == "US" and is_likely_otc_adr(r["Code"]))])
        if len(isins) != 1:
            return AMBIGUOUS_RESOLUTION  # identité de l'entreprise non vérifiable ou homonymes
        identities.append(next(iter(isins)))
    if len(set(identities)) != 1:
        return AMBIGUOUS_RESOLUTION  # le code et le nom désignent deux entreprises
    provenance, hits = candidates[0]
    isin = identities[0]
    # Nom APPARENTÉ (préfixe) d'une autre entreprise (autre ISIN) : homonymie
    # possible (Merck & Co / Merck KGaA, Ferrari NV / Ferrari Group) : refus.
    related = [r for k, rows in groups.items() if k and k != normalized
               and (k.startswith(normalized + " ") or normalized.startswith(k + " ")) for r in rows]
    if normalized and _isins([r for r in related if not (r["Exchange"] == "US" and is_likely_otc_adr(r["Code"]))]) \
            - {isin}:
        return AMBIGUOUS_RESOLUTION
    listings = [r for r in hits if r["Isin"] in (isin, None, "")]
    effective_prefer_us = any(r["Exchange"] == "US" and r["Type"] == "Common Stock" and not is_likely_otc_adr(r["Code"])
                              for r in listings)
    best = sorted(listings, key=lambda r: _rank(r, effective_prefer_us, query))[0]
    ticker = format_ticker(best["Code"], best["Exchange"] or "")
    if not dc.valid_resolved_ticker(ticker):
        return AMBIGUOUS_RESOLUTION
    names = {normalize_company_name(r["Name"] or "") for r in listings}
    name = best["Name"] if len(names) == 1 and best["Name"] else None
    return InstrumentResolution(IDENTIFIED, ticker, best["Code"], best["Exchange"] or "", provenance, name, isin)
