"""Valeurs de position du contrat F2 versionné (R1 PR 7) — fonctions pures.

Représentation financière : les colonnes historiques restent des Float
SQLAlchemy (binary64 PostgreSQL) ; PR 7 ne les convertit pas.

Entrée (nouvelle écriture) : chaîne JSON décimale ASCII exacte

    (?:0|[1-9][0-9]*)(?:\\.[0-9]{0,5}[1-9])?

21 caractères au plus (borne jugée AVANT toute analyse), 15 chiffres
significatifs au plus, six décimales au plus ; ni signe, ni exposant, ni
blanc, ni zéro initial ou final superflu, ni chiffre Unicode, ni NaN /
Infinity. Analyse par Decimal, conversion en float binary64 fini, puis
exigence Decimal(repr(f)) == valeur saisie : aucun arrondi silencieux.
Cette validation ne prétend pas que le float est un décimal exact.

Domaines : quantity dans ]0, 10^12] ; purchase_price null ou dans
[0, 10^12] ; target_percent null ou dans [0, 100].

Sortie : format versionné NUMERIC_ENCODING = "binary64-decimal-v1". Nombre
fini : représentation round-trip la plus courte de CPython (repr, identique
de 3.11 à 3.13), rendue en notation décimale fixe sans exposant ni zéro
fractionnaire inutile ; +0.0 et -0.0 donnent "0". Valeurs historiques
spéciales : "NaN", "Infinity", "-Infinity" — toujours des chaînes, jamais
des constantes JSON non standard. Les renderers versionnés restent
disponibles (RENDERERS) pour les anciens reçus.

Ticker d'une nouvelle écriture : [A-Z0-9][A-Z0-9.^=-]{0,31} exact (ni
trim, ni passage en majuscules) ; enveloppe : null, "PEA" ou "CTO". Ces
règles sont le domaine technique du premier contrat F2, pas une
classification des instruments financiers ; aucune somme de cibles n'est
imposée, aucune allocation n'est suggérée (« décrire, jamais prescrire »).

Diagnostic de lecture (calculé, jamais enregistré, jamais corrigé) : un
champ historique est valide si sa représentation binary64-decimal-v1 serait
acceptée telle quelle par une nouvelle écriture. Codes fermés, dans l'ordre
ISSUE_CODES, un au plus par champ.
"""
import math
import re
import struct
from decimal import Decimal, InvalidOperation

NUMERIC_ENCODING = "binary64-decimal-v1"
VALID = "valid"
LEGACY_INVALID = "legacy_invalid"
DATA_STATUSES = (VALID, LEGACY_INVALID)
ISSUE_CODES = ("ticker_invalid", "quantity_invalid", "purchase_price_invalid", "target_percent_invalid",
               "envelope_invalid")
ENVELOPES = ("PEA", "CTO")
MAX_DECIMAL_CHARS = 21
MAX_SIGNIFICANT_DIGITS = 15
TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.^=-]{0,31}", re.ASCII)
DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{0,5}[1-9])?", re.ASCII)
MAX_AMOUNT = Decimal(10) ** 12
MAX_PERCENT = Decimal(100)
FLOAT8_BYTES = 8


class ValueRejected(ValueError):
    """Valeur hors du contrat d'écriture."""


def _decimal(text) -> Decimal:
    """Valeur décimale d'une chaîne d'entrée conforme (sans le domaine)."""
    if type(text) is not str or len(text) > MAX_DECIMAL_CHARS or not DECIMAL_RE.fullmatch(text):
        raise ValueRejected("décimal hors grammaire")
    if len(text.replace(".", "").lstrip("0")) > MAX_SIGNIFICANT_DIGITS:
        raise ValueRejected("plus de 15 chiffres significatifs")
    value = Decimal(text)
    number = float(value)
    if not math.isfinite(number) or Decimal(repr(number)) != value:
        raise ValueRejected("décimal non représentable sans arrondi")
    return value


def _check(text, low: Decimal, high: Decimal, *, low_open: bool) -> str:
    value = _decimal(text)
    if value > high or value < low or (low_open and value == low):
        raise ValueRejected("hors domaine")
    return text


def check_quantity(text) -> str:
    return _check(text, Decimal(0), MAX_AMOUNT, low_open=True)


def check_purchase_price(text):
    return None if text is None else _check(text, Decimal(0), MAX_AMOUNT, low_open=False)


def check_target_percent(text):
    return None if text is None else _check(text, Decimal(0), MAX_PERCENT, low_open=False)


def check_ticker(text) -> str:
    if type(text) is not str or not TICKER_RE.fullmatch(text):
        raise ValueRejected("ticker hors contrat")
    return text


def check_envelope(text):
    if text is not None and not (type(text) is str and text in ENVELOPES):
        raise ValueRejected("enveloppe hors contrat")
    return text


CHECKS = (check_ticker, check_quantity, check_purchase_price, check_target_percent, check_envelope)


def to_float(text):
    """Chaîne d'entrée validée -> float binary64 écrit en base (ou None)."""
    return None if text is None else float(Decimal(text))


# --------------------------------------------------------------------------
# Sortie binary64-decimal-v1
# --------------------------------------------------------------------------

def render_binary64_decimal_v1(number) -> str:
    if type(number) is not float:
        raise TypeError("float binary64 attendu")
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0.0:
        return "0"
    text = format(Decimal(repr(number)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


RENDERERS = {NUMERIC_ENCODING: render_binary64_decimal_v1}


def float_from_float8send(raw) -> float:
    """Octets IEEE 754 big-endian de float8send(colonne) -> float exact,
    indépendamment de extra_float_digits et de toute sortie texte."""
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if type(raw) is not bytes or len(raw) != FLOAT8_BYTES:
        raise TypeError("float8send : 8 octets attendus")
    return struct.unpack(">d", raw)[0]


def is_valid_rendered(text, check) -> bool:
    try:
        check(text)
    except ValueRejected:
        return False
    return True


def diagnose(ticker, quantity, purchase_price, target_percent, envelope) -> tuple:
    """Codes d'anomalie, dans l'ordre fermé, pour des valeurs déjà rendues
    (chaînes binary64-decimal-v1, ticker et enveloppe historiques)."""
    values = (ticker, quantity, purchase_price, target_percent, envelope)
    return tuple(code for code, value, check in zip(ISSUE_CODES, values, CHECKS)
                 if not is_valid_rendered(value, check))


def is_rendered_number(text) -> bool:
    """Chaîne produite par render_binary64_decimal_v1 pour un float
    quelconque (y compris hors domaine ou spécial) : ré-analyse exacte."""
    if type(text) is not str:
        return False
    if text in ("NaN", "Infinity", "-Infinity"):
        return True
    if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?", text, re.ASCII) or text == "-0":
        return False
    try:
        number = float(Decimal(text))
    except (InvalidOperation, OverflowError):
        return False
    return math.isfinite(number) and render_binary64_decimal_v1(number) == text
