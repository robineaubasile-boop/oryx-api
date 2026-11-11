"""Configuration stricte F2 (ACT-L1 / F04-B V0.3, B01) — chargeur PUR, DORMANT.

load_f2_config(variables) lit EXCLUSIVEMENT le mappage qu'on lui passe (la
future composition F04-C lui transmettra os.environ) : aucune lecture
implicite de l'environnement, aucun effet à l'import, aucun accès réseau ni
base, aucun secret généré, aucune valeur par défaut, aucun repli legacy. Le
résultat est déterministe : mêmes entrées, même configuration.

Variables contractuelles (toutes requises) :

    DATABASE_URL                    URL PostgreSQL (core.f2_runtime.database)
    ORYX_F2_ALLOWED_ORIGIN          origine https canonique UNIQUE
    ORYX_F2_AUTH_KEYS_JSON          trousseau F2 (Keyring, six usages)
    ORYX_F2_CSRF_KEYS_JSON          trousseau CSRF (CsrfKeyring)
    ORYX_F2_PORTFOLIO_KEYS_JSON     trousseau portefeuille (PortfolioKeyring)
    ORYX_F2_DECRYPTAGE_KEYS_JSON    trousseau Décrypter (DecryptageKeyring)
    ORYX_F2_SUPABASE_URL            https://<hôte>[:port], sans chemin
    ORYX_F2_SUPABASE_SERVICE_KEY    clé d'administration (generate_link)
    ORYX_F2_SUPABASE_VERIFY_KEY     clé de vérification OTP (verify)
    ORYX_F2_RESEND_API_KEY          clé Resend
    ORYX_F2_RESEND_SENDER           expéditeur Resend
    ORYX_F2_EODHD_API_KEY           clé EODHD
    ORYX_F2_FMP_API_KEY             clé FMP
    ORYX_F2_ANTHROPIC_API_KEY       clé Anthropic
    ORYX_F2_DECRYPTAGE_MODEL        modèle Claude explicite

Validation fermée de l'espace F2 seulement : toute variable ORYX_F2_* hors
de cette liste est refusée (faute de frappe, variable retirée) ; les autres
variables (Railway, legacy) sont ignorées sans être lues.

Aucune valeur n'est rognée ni normalisée en silence : un blanc de bord, un
caractère de contrôle ou une valeur vide est refusé.

Trousseaux : JSON UTF-8 strict (propriétés dupliquées, constantes NaN /
Infinity, BOM refusés ; 64 Kio au plus), propriétés EXACTES (aucun champ
inconnu, aucun champ manquant), schéma versionné :

    ORYX_F2_AUTH_KEYS_JSON :
      {"schema": "oryx-f2-auth-keys-v1",
       "purposes": {"candidate":  {"current": "<v>", "keys": {"<v>": "<clé>", ...}},
                    "derive": {...}, "active": {...}, "otp": {...},
                    "invitation": {...}, "abuse": {...}}}

    ORYX_F2_CSRF_KEYS_JSON / _PORTFOLIO_ / _DECRYPTAGE_ :
      {"schema": "oryx-f2-csrf-keys-v1" | "oryx-f2-portfolio-keys-v1"
                 | "oryx-f2-decryptage-keys-v1",
       "current": "<v>", "keys": {"<v>": "<clé>", ...}}

Chaque clé : base64url CANONIQUE sans remplissage, exactement 32 octets (43
caractères, core.auth_f2.crypto.b64url_decode_secret). Les versions
historiques (anciennes clés conservées pour la vérification) sont admises à
côté de la version courante, qui doit figurer dans « keys ». La syntaxe des
versions est celle de chaque classe existante (réutilisée telle quelle) :

    Keyring           [A-Za-z0-9._-]{1,64}
    CsrfKeyring       [A-Za-z0-9_-]{1,32}
    PortfolioKeyring  [a-z0-9][a-z0-9_-]{0,31}
    DecryptageKeyring [a-z0-9][a-z0-9_-]{0,31}

Aucune valeur de clé ne peut servir deux fois : ni entre deux versions ou
deux usages d'un trousseau, ni entre deux trousseaux (F2, CSRF,
portefeuille, Décrypter).

Erreurs : F2ConfigError porte la liste des problèmes (variable, code[, usage])
; aucune valeur, aucun fragment de valeur, aucun message d'origine d'un
décodeur n'y figure. Rien n'est journalisé.

Le même objet Keyring et le même objet CsrfKeyring sont destinés aux deux
compositions (authentification et personnelle).
"""
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.auth_f2.crypto import CryptoContractError, Redacted, b64url_decode_secret
from core.auth_f2.keys import PURPOSES, Keyring, KeyringError
from core.auth_f2.providers import is_valid_issuer
from core.auth_f2_http.csrf import CsrfConfigError, CsrfKeyring
from core.auth_f2_http.provenance import validate_allowed_origins
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring, DecryptageKeyringError
from core.authz_f2.portfolio_fingerprint import PortfolioKeyring, PortfolioKeyringError
from core.f2_runtime.database import F2DatabaseUrlError, normalize_f2_database_url

DATABASE_URL = "DATABASE_URL"
ALLOWED_ORIGIN = "ORYX_F2_ALLOWED_ORIGIN"
AUTH_KEYS_JSON = "ORYX_F2_AUTH_KEYS_JSON"
CSRF_KEYS_JSON = "ORYX_F2_CSRF_KEYS_JSON"
PORTFOLIO_KEYS_JSON = "ORYX_F2_PORTFOLIO_KEYS_JSON"
DECRYPTAGE_KEYS_JSON = "ORYX_F2_DECRYPTAGE_KEYS_JSON"
SUPABASE_URL = "ORYX_F2_SUPABASE_URL"
SUPABASE_SERVICE_KEY = "ORYX_F2_SUPABASE_SERVICE_KEY"
SUPABASE_VERIFY_KEY = "ORYX_F2_SUPABASE_VERIFY_KEY"
RESEND_API_KEY = "ORYX_F2_RESEND_API_KEY"
RESEND_SENDER = "ORYX_F2_RESEND_SENDER"
EODHD_API_KEY = "ORYX_F2_EODHD_API_KEY"
FMP_API_KEY = "ORYX_F2_FMP_API_KEY"
ANTHROPIC_API_KEY = "ORYX_F2_ANTHROPIC_API_KEY"
DECRYPTAGE_MODEL = "ORYX_F2_DECRYPTAGE_MODEL"

VARIABLES = (DATABASE_URL, ALLOWED_ORIGIN, AUTH_KEYS_JSON, CSRF_KEYS_JSON, PORTFOLIO_KEYS_JSON,
             DECRYPTAGE_KEYS_JSON, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_VERIFY_KEY, RESEND_API_KEY,
             RESEND_SENDER, EODHD_API_KEY, FMP_API_KEY, ANTHROPIC_API_KEY, DECRYPTAGE_MODEL)
F2_PREFIX = "ORYX_F2_"
SECRET_VARIABLES = (SUPABASE_SERVICE_KEY, SUPABASE_VERIFY_KEY, RESEND_API_KEY, EODHD_API_KEY, FMP_API_KEY,
                    ANTHROPIC_API_KEY)

AUTH_SCHEMA = "oryx-f2-auth-keys-v1"
CSRF_SCHEMA = "oryx-f2-csrf-keys-v1"
PORTFOLIO_SCHEMA = "oryx-f2-portfolio-keys-v1"
DECRYPTAGE_SCHEMA = "oryx-f2-decryptage-keys-v1"

KEYRING_JSON_MAX_BYTES = 64 * 1024
SECRET_MAX_CHARS = 4096
SENDER_MAX_CHARS = 320
# Même règle que core.authz_f2_http.valid_model (non importé : ce module
# charge core.db) ; équivalence vérifiée par tests/test_f04b_config.py.
MODEL_MAX_CHARS = 128
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*", re.ASCII)
_SUPABASE_URL_RE = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?", re.ASCII)
_SECRET_RE = re.compile(r"[\x21-\x7e]+", re.ASCII)

# Codes de refus (stables, sans donnée).
MISSING = "missing"
EMPTY = "empty"
SURROUNDING_WHITESPACE = "surrounding_whitespace"
INVALID_CHARACTERS = "invalid_characters"
TOO_LONG = "too_long"
UNKNOWN_VARIABLE = "unknown_variable"
INVALID_DATABASE_URL = "invalid_database_url"
INVALID_ORIGIN = "invalid_origin"
INVALID_URL = "invalid_url"
INVALID_SENDER = "invalid_sender"
INVALID_MODEL = "invalid_model"
INVALID_JSON = "invalid_json"
DUPLICATE_PROPERTY = "duplicate_property"
NOT_AN_OBJECT = "not_an_object"
UNKNOWN_PROPERTY = "unknown_property"
MISSING_PROPERTY = "missing_property"
UNKNOWN_SCHEMA = "unknown_schema"
NO_KEY_VERSION = "no_key_version"
INVALID_VERSION_FIELD = "invalid_version_field"
CURRENT_VERSION_ABSENT = "current_version_absent"
INVALID_KEY_ENCODING = "invalid_key_encoding"
KEY_REUSED = "key_reused"
KEYRING_REJECTED = "keyring_rejected"


class F2ConfigError(ValueError):
    """Configuration F2 refusée. problems : tuple de (variable, code, usage
    ou None), sans aucune valeur."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("configuration F2 refusée : " + ", ".join(
            f"{variable}:{code}" + (f"[{where}]" if where else "") for variable, code, where in self.problems))

    def __reduce__(self):
        return (type(self), (self.problems,))


class _Problem(Exception):
    def __init__(self, code: str, where=None):
        super().__init__(code)
        self.code = code
        self.where = where


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class F2Config:
    """Configuration F2 validée. repr() ne révèle rien ; non sérialisable."""
    database_url: object            # sqlalchemy.engine.URL (postgresql+psycopg2)
    allowed_origin: str
    keyring: Keyring
    csrf_keyring: CsrfKeyring
    portfolio_keyring: PortfolioKeyring
    decryptage_keyring: DecryptageKeyring
    supabase_url: str
    supabase_service_key: Redacted
    supabase_verify_key: Redacted
    resend_api_key: Redacted
    resend_sender: str
    eodhd_api_key: Redacted
    fmp_api_key: Redacted
    anthropic_api_key: Redacted
    decryptage_model: str

    @property
    def allowed_origins(self) -> tuple:
        """Valeur à transmettre aux deux compositions (une seule origine)."""
        return (self.allowed_origin,)

    def __repr__(self) -> str:
        return "F2Config(<private>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("F2Config n'est pas sérialisable")


# --------------------------------------------------------------------------
# Valeurs simples
# --------------------------------------------------------------------------

def _plain(value, max_chars: int) -> str:
    if type(value) is not str:
        raise _Problem(INVALID_CHARACTERS)
    if not value:
        raise _Problem(EMPTY)
    if value != value.strip():
        raise _Problem(SURROUNDING_WHITESPACE)
    if len(value) > max_chars:
        raise _Problem(TOO_LONG)
    if not value.isprintable():
        raise _Problem(INVALID_CHARACTERS)
    return value


def _secret(value) -> Redacted:
    value = _plain(value, SECRET_MAX_CHARS)
    if not _SECRET_RE.fullmatch(value):
        raise _Problem(INVALID_CHARACTERS)
    return Redacted(value)


def _database_url(value):
    value = _plain(value, SECRET_MAX_CHARS)
    try:
        return normalize_f2_database_url(value)
    except F2DatabaseUrlError:
        raise _Problem(INVALID_DATABASE_URL) from None


def _origin(value) -> str:
    value = _plain(value, 2048)
    try:
        validate_allowed_origins((value,))
    except ValueError:
        raise _Problem(INVALID_ORIGIN) from None
    return value


def _supabase_url(value) -> str:
    value = _plain(value, 2048)
    if not _SUPABASE_URL_RE.fullmatch(value) or not is_valid_issuer(value + "/auth/v1"):
        raise _Problem(INVALID_URL)
    return value


def _sender(value) -> str:
    value = _plain(value, SENDER_MAX_CHARS)
    if "@" not in value:
        raise _Problem(INVALID_SENDER)
    return value


def valid_model(value) -> bool:
    return type(value) is str and 0 < len(value) <= MODEL_MAX_CHARS and _MODEL_RE.fullmatch(value) is not None


def _model(value) -> str:
    value = _plain(value, MODEL_MAX_CHARS)
    if not valid_model(value):
        raise _Problem(INVALID_MODEL)
    return value


# --------------------------------------------------------------------------
# Documents JSON de trousseaux
# --------------------------------------------------------------------------

class _Duplicate(Exception):
    pass


def _pairs(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise _Duplicate()
        obj[key] = value
    return obj


def _reject_constant(_name):
    raise ValueError("constante JSON refusée")


def _document(value):
    if type(value) is not str:
        raise _Problem(INVALID_CHARACTERS)
    if not value:
        raise _Problem(EMPTY)
    if value != value.strip():
        raise _Problem(SURROUNDING_WHITESPACE)
    if len(value.encode("utf-8", "surrogatepass")) > KEYRING_JSON_MAX_BYTES:
        raise _Problem(TOO_LONG)
    if not value.replace("\n", "").replace("\r", "").replace("\t", "").isprintable():
        raise _Problem(INVALID_CHARACTERS)  # seuls les blancs JSON sont admis hors caractères imprimables
    try:
        document = json.loads(value, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except _Duplicate:
        raise _Problem(DUPLICATE_PROPERTY) from None
    except (ValueError, RecursionError):
        raise _Problem(INVALID_JSON) from None
    return document


def _exact(obj, fields: tuple, where=None) -> None:
    if type(obj) is not dict:
        raise _Problem(NOT_AN_OBJECT, where)
    if set(obj) - set(fields):
        raise _Problem(UNKNOWN_PROPERTY, where)
    if set(fields) - set(obj):
        raise _Problem(MISSING_PROPERTY, where)


def _versions(entry, where=None) -> tuple:
    """(clés {version: 32 octets}, version courante) d'une entrée
    {"current", "keys"} ; la syntaxe des versions reste celle de la classe."""
    _exact(entry, ("current", "keys"), where)
    keys, current = entry["keys"], entry["current"]
    if type(keys) is not dict:
        raise _Problem(NOT_AN_OBJECT, where)
    if not keys:
        raise _Problem(NO_KEY_VERSION, where)
    if type(current) is not str or not current:
        raise _Problem(INVALID_VERSION_FIELD, where)
    if current not in keys:
        raise _Problem(CURRENT_VERSION_ABSENT, where)
    decoded = {}
    for version, encoded in keys.items():
        try:
            decoded[version] = b64url_decode_secret(encoded)
        except CryptoContractError:
            raise _Problem(INVALID_KEY_ENCODING, where) from None
    return decoded, current


def _no_reuse(groups) -> None:
    """Aucune valeur de clé deux fois (groups : [(usage, {version: clé})])."""
    seen = set()
    for where, keys in groups:
        for key in keys.values():
            if key in seen:
                raise _Problem(KEY_REUSED, where)
            seen.add(key)


def _auth_keyring(value):
    document = _document(value)
    _exact(document, ("schema", "purposes"))
    if document["schema"] != AUTH_SCHEMA:
        raise _Problem(UNKNOWN_SCHEMA)
    _exact(document["purposes"], PURPOSES)
    keys, current = {}, {}
    for purpose in PURPOSES:
        keys[purpose], current[purpose] = _versions(document["purposes"][purpose], purpose)
    _no_reuse([(p, keys[p]) for p in PURPOSES])
    try:
        keyring = Keyring(keys, current)
    except KeyringError:
        raise _Problem(KEYRING_REJECTED) from None
    return keyring, [key for p in PURPOSES for key in keys[p].values()]


def _single_keyring(value, schema: str, cls, error):
    document = _document(value)
    _exact(document, ("schema", "current", "keys"))
    if document["schema"] != schema:
        raise _Problem(UNKNOWN_SCHEMA)
    keys, current = _versions({"current": document["current"], "keys": document["keys"]})
    _no_reuse([(None, keys)])
    try:
        keyring = cls(keys, current)
    except error:
        raise _Problem(KEYRING_REJECTED) from None
    return keyring, list(keys.values())


_PARSERS = (
    (DATABASE_URL, _database_url),
    (ALLOWED_ORIGIN, _origin),
    (AUTH_KEYS_JSON, _auth_keyring),
    (CSRF_KEYS_JSON, lambda v: _single_keyring(v, CSRF_SCHEMA, CsrfKeyring, CsrfConfigError)),
    (PORTFOLIO_KEYS_JSON, lambda v: _single_keyring(v, PORTFOLIO_SCHEMA, PortfolioKeyring, PortfolioKeyringError)),
    (DECRYPTAGE_KEYS_JSON, lambda v: _single_keyring(v, DECRYPTAGE_SCHEMA, DecryptageKeyring,
                                                     DecryptageKeyringError)),
    (SUPABASE_URL, _supabase_url),
    (SUPABASE_SERVICE_KEY, _secret),
    (SUPABASE_VERIFY_KEY, _secret),
    (RESEND_API_KEY, _secret),
    (RESEND_SENDER, _sender),
    (EODHD_API_KEY, _secret),
    (FMP_API_KEY, _secret),
    (ANTHROPIC_API_KEY, _secret),
    (DECRYPTAGE_MODEL, _model),
)
if tuple(name for name, _ in _PARSERS) != VARIABLES:  # garde de cohérence interne
    raise RuntimeError("table des variables F2 incohérente")
_FIELDS = {
    DATABASE_URL: "database_url", ALLOWED_ORIGIN: "allowed_origin", AUTH_KEYS_JSON: "keyring",
    CSRF_KEYS_JSON: "csrf_keyring", PORTFOLIO_KEYS_JSON: "portfolio_keyring",
    DECRYPTAGE_KEYS_JSON: "decryptage_keyring", SUPABASE_URL: "supabase_url",
    SUPABASE_SERVICE_KEY: "supabase_service_key", SUPABASE_VERIFY_KEY: "supabase_verify_key",
    RESEND_API_KEY: "resend_api_key", RESEND_SENDER: "resend_sender", EODHD_API_KEY: "eodhd_api_key",
    FMP_API_KEY: "fmp_api_key", ANTHROPIC_API_KEY: "anthropic_api_key", DECRYPTAGE_MODEL: "decryptage_model",
}
_KEYRING_VARIABLES = (AUTH_KEYS_JSON, CSRF_KEYS_JSON, PORTFOLIO_KEYS_JSON, DECRYPTAGE_KEYS_JSON)


def load_f2_config(variables) -> F2Config:
    """Configuration F2 complète et validée, ou F2ConfigError (tous les
    problèmes détectés, sans aucune valeur)."""
    if not isinstance(variables, Mapping):
        raise TypeError("mappage de variables attendu")
    problems = []
    for name in sorted(k for k in variables if isinstance(k, str) and k.startswith(F2_PREFIX)):
        if name not in VARIABLES:
            problems.append((name, UNKNOWN_VARIABLE, None))
    values, key_values = {}, {}
    for name, parse in _PARSERS:
        if name not in variables:
            problems.append((name, MISSING, None))
            continue
        try:
            parsed = parse(variables[name])
        except _Problem as problem:
            problems.append((name, problem.code, problem.where))
            continue
        if name in _KEYRING_VARIABLES:
            parsed, key_values[name] = parsed
        values[_FIELDS[name]] = parsed
    # Indépendance entre trousseaux (chaque trousseau a déjà refusé ses
    # propres réutilisations) : une clé partagée est signalée sur le second.
    seen = {}
    for name in _KEYRING_VARIABLES:
        for key in key_values.get(name, ()):
            if seen.get(key, name) != name:
                problems.append((name, KEY_REUSED, None))
                break
            seen[key] = name
    if problems:
        raise F2ConfigError(problems)
    return F2Config(**values)
