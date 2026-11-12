"""F04-B / B01 — configuration stricte F2 (chargeur pur, sans base ni réseau).

Configuration absente, partielle ou invalide ; JSON dupliqué, malformé ;
versions historiques et syntaxes propres à chaque classe ; collisions de
clés dans un trousseau et entre trousseaux ; absence de secret dans les
erreurs ; validation fermée limitée à l'espace ORYX_F2_*.
"""
import json
import pickle

import pytest

from core.auth_f2.crypto import Redacted
from core.auth_f2.keys import PURPOSES, Keyring
from core.auth_f2_http.csrf import CsrfKeyring
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.portfolio_fingerprint import PortfolioKeyring
from core.f2_runtime import config as c
from tests import f04b_support as s


def _problems(env) -> set:
    with pytest.raises(c.F2ConfigError) as info:
        c.load_f2_config(env)
    _assert_no_secret(info.value, env)
    return set(info.value.problems)


def _assert_no_secret(error, env):
    text = str(error) + repr(error) + repr(error.problems)
    for value in list(s.SECRET_VALUES) + s.all_key_strings():
        assert value not in text
    for name in c.VARIABLES:
        value = env.get(name)
        if isinstance(value, str) and len(value) >= 8 and name != c.ALLOWED_ORIGIN:
            assert value not in text, name


def _codes(env, variable) -> set:
    return {(code, where) for name, code, where in _problems(env) if name == variable}


# --------------------------------------------------------------------------
# Nominal
# --------------------------------------------------------------------------

def test_complete_configuration_is_loaded_with_existing_classes():
    config = c.load_f2_config(s.environ())
    assert type(config) is c.F2Config
    assert type(config.keyring) is Keyring and type(config.csrf_keyring) is CsrfKeyring
    assert type(config.portfolio_keyring) is PortfolioKeyring
    assert type(config.decryptage_keyring) is DecryptageKeyring
    assert config.keyring.current("otp") == ("v1", s.raw_key(100 + PURPOSES.index("otp")))
    assert config.csrf_keyring.current_version == "c1"
    assert config.portfolio_keyring.key("p1") == s.raw_key(300)
    assert config.decryptage_keyring.key("d1") == s.raw_key(400)
    assert config.database_url.drivername == "postgresql+psycopg2"
    assert config.database_url.database == "oryx_f04b_never_test"
    assert config.allowed_origin == s.ORIGIN and config.allowed_origins == (s.ORIGIN,)
    assert config.supabase_url == s.SUPABASE_URL
    assert config.decryptage_model == s.MODEL
    assert config.resend_sender == "Oryx <no-reply@oryx-f04b.example>"
    for field in ("supabase_service_key", "supabase_verify_key", "resend_api_key", "eodhd_api_key",
                  "fmp_api_key", "anthropic_api_key"):
        assert type(getattr(config, field)) is Redacted
    assert config.anthropic_api_key.reveal() == "sk-ant-synthetic-key"


def test_loading_is_deterministic_and_reads_only_the_given_mapping(monkeypatch):
    env = s.environ()
    monkeypatch.setenv("ORYX_F2_UNEXPECTED_IN_PROCESS", "1")  # jamais lu : seul le mappage compte
    first, second = c.load_f2_config(env), c.load_f2_config(dict(env))
    for field in ("allowed_origin", "supabase_url", "resend_sender", "decryptage_model"):
        assert getattr(first, field) == getattr(second, field)
    assert first.database_url == second.database_url
    for purpose in PURPOSES:
        assert first.keyring.current(purpose) == second.keyring.current(purpose)


def test_configuration_reveals_nothing_and_is_not_serializable():
    config = c.load_f2_config(s.environ())
    assert repr(config) == str(config) == "F2Config(<private>)"
    with pytest.raises(TypeError):
        pickle.dumps(config)
    assert "synthetic-password" not in repr(config.database_url)


def test_foreign_variables_are_ignored_but_unknown_f2_variables_are_refused():
    env = s.environ(RAILWAY_SERVICE_NAME="x", PORT="8080", ORYX_F2X="ignored-prefix-differs")
    c.load_f2_config(env)
    assert _problems(s.environ(ORYX_F2_ACTIVATION="1")) == {("ORYX_F2_ACTIVATION", c.UNKNOWN_VARIABLE, None)}
    assert _problems(s.environ(ORYX_F2_SUPABASE_KEY="x")) == {("ORYX_F2_SUPABASE_KEY", c.UNKNOWN_VARIABLE, None)}


def test_the_contract_lists_exactly_fifteen_variables():
    assert c.VARIABLES == (
        "DATABASE_URL", "ORYX_F2_ALLOWED_ORIGIN", "ORYX_F2_AUTH_KEYS_JSON", "ORYX_F2_CSRF_KEYS_JSON",
        "ORYX_F2_PORTFOLIO_KEYS_JSON", "ORYX_F2_DECRYPTAGE_KEYS_JSON", "ORYX_F2_SUPABASE_URL",
        "ORYX_F2_SUPABASE_SERVICE_KEY", "ORYX_F2_SUPABASE_VERIFY_KEY", "ORYX_F2_RESEND_API_KEY",
        "ORYX_F2_RESEND_SENDER", "ORYX_F2_EODHD_API_KEY", "ORYX_F2_FMP_API_KEY", "ORYX_F2_ANTHROPIC_API_KEY",
        "ORYX_F2_DECRYPTAGE_MODEL")


# --------------------------------------------------------------------------
# Absente, partielle
# --------------------------------------------------------------------------

def test_empty_environment_reports_every_missing_variable():
    assert _problems({}) == {(name, c.MISSING, None) for name in c.VARIABLES}


def test_legacy_only_environment_is_refused_without_fallback():
    legacy = {"DATABASE_URL": s.FAKE_DATABASE_URL, "ANTHROPIC_API_KEY": "x", "EODHD_API_KEY": "x",
              "FMP_API_KEY": "x", "SUPABASE_URL": s.SUPABASE_URL, "RESEND_API_KEY": "x"}
    assert _problems(legacy) == {(name, c.MISSING, None) for name in c.VARIABLES if name != "DATABASE_URL"}


@pytest.mark.parametrize("name", c.VARIABLES)
def test_each_missing_variable_is_refused(name):
    assert _problems(s.environ(**{name: None})) == {(name, c.MISSING, None)}


@pytest.mark.parametrize("name", c.VARIABLES)
@pytest.mark.parametrize("value, code", [("", c.EMPTY), (" padded", c.SURROUNDING_WHITESPACE),
                                         ("padded\n", c.SURROUNDING_WHITESPACE)])
def test_empty_or_padded_values_are_refused_without_trimming(name, value, code):
    assert _problems(s.environ(**{name: value})) == {(name, code, None)}


@pytest.mark.parametrize("name", c.SECRET_VARIABLES)
@pytest.mark.parametrize("value", ["two words", "tab\tinside", "nul\x00inside", "é-non-ascii", "x" * 4097])
def test_provider_secrets_are_single_printable_ascii_tokens(name, value):
    codes = _codes(s.environ(**{name: value}), name)
    assert codes in ({(c.INVALID_CHARACTERS, None)}, {(c.TOO_LONG, None)})


def test_non_mapping_environment_is_refused():
    with pytest.raises(TypeError):
        c.load_f2_config([("DATABASE_URL", s.FAKE_DATABASE_URL)])


# --------------------------------------------------------------------------
# Valeurs simples
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"),
    ("postgresql://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"),
    ("postgresql+psycopg2://u:p@h/d?sslmode=require", "postgresql+psycopg2://u:p@h/d?sslmode=require"),
])
def test_database_url_formats_are_normalized_to_psycopg2(url, expected):
    config = c.load_f2_config(s.environ(DATABASE_URL=url))
    assert config.database_url.render_as_string(hide_password=False) == expected


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://u:p@h/d", "postgresql+psycopg://u:p@h/d", "sqlite:///x.db", "mysql://u:p@h/d",
    "postgresql://u:p@h", "postgresql://h/d", "postgresql://u:p@/d", "not a url", "https://u:p@h/d",
])
def test_unsupported_or_incomplete_database_urls_are_refused(url):
    assert _codes(s.environ(DATABASE_URL=url), "DATABASE_URL") == {(c.INVALID_DATABASE_URL, None)}


@pytest.mark.parametrize("origin", [
    "http://oryx.example", "https://oryx.example/", "https://Oryx.example", "https://oryx.example:443",
    "https://a.example,https://b.example", "null", "https://oryx.example/path",
])
def test_allowed_origin_is_a_single_canonical_https_origin(origin):
    assert _codes(s.environ(ORYX_F2_ALLOWED_ORIGIN=origin), c.ALLOWED_ORIGIN) == {(c.INVALID_ORIGIN, None)}


@pytest.mark.parametrize("url", ["http://x.supabase.co", "https://x.supabase.co/", "https://x.supabase.co/auth/v1",
                                 "https://X.supabase.co", "https://u@x.supabase.co"])
def test_supabase_url_has_no_path_and_is_https(url):
    assert _codes(s.environ(ORYX_F2_SUPABASE_URL=url), c.SUPABASE_URL) == {(c.INVALID_URL, None)}


@pytest.mark.parametrize("sender, code", [("no-at-sign", c.INVALID_SENDER), ("a@b\r\nBcc: x@y", c.INVALID_CHARACTERS)])
def test_resend_sender_is_checked(sender, code):
    assert _codes(s.environ(ORYX_F2_RESEND_SENDER=sender), c.RESEND_SENDER) == {(code, None)}


@pytest.mark.parametrize("model", ["-leading-dash", "with space", "x" * 129, "modèle", "a;b"])
def test_invalid_models_are_refused(model):
    codes = _codes(s.environ(ORYX_F2_DECRYPTAGE_MODEL=model), c.DECRYPTAGE_MODEL)
    assert codes in ({(c.INVALID_MODEL, None)}, {(c.TOO_LONG, None)}, {(c.INVALID_CHARACTERS, None)})


def test_model_rule_is_exactly_the_c3_rule():
    """valid_model est recopié (le module C3 charge core.db) : équivalence
    vérifiée sur un corpus, pour que la configuration n'accepte jamais un
    modèle que DecryptageHttpConfig refuserait (et réciproquement)."""
    from core.authz_f2_http.decryptage import valid_model as c3_valid_model

    corpus = ["claude-opus-5-5", "claude-sonnet-5-5@2026", "a", "A.b:c/d-e_f", "x" * 128, "x" * 129, "", " a",
              "-a", ".a", "a b", "modèle", "a;b", "a\n", None, 5, "anthropic/claude:1"]
    for value in corpus:
        assert c.valid_model(value) == c3_valid_model(value), value


# --------------------------------------------------------------------------
# Documents JSON : forme
# --------------------------------------------------------------------------

KEYRINGS = (c.AUTH_KEYS_JSON, c.CSRF_KEYS_JSON, c.PORTFOLIO_KEYS_JSON, c.DECRYPTAGE_KEYS_JSON)


@pytest.mark.parametrize("name", KEYRINGS)
@pytest.mark.parametrize("text, code", [
    ("{", c.INVALID_JSON), ("[]", c.NOT_AN_OBJECT), ('"x"', c.NOT_AN_OBJECT), ("null", c.NOT_AN_OBJECT),
    ('{"schema": NaN}', c.INVALID_JSON), ('{"schema": Infinity}', c.INVALID_JSON),
    ('{"a": 1} trailing', c.INVALID_JSON), ("\ufeff{}", c.INVALID_CHARACTERS), ("{}\x00", c.INVALID_CHARACTERS),
])
def test_malformed_documents_are_refused(name, text, code):
    assert _codes(s.environ(**{name: text}), name) == {(code, None)}


def test_multiline_json_is_accepted():
    c.load_f2_config(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(s.auth_document(), indent=2)))


@pytest.mark.parametrize("name", KEYRINGS)
def test_oversized_documents_are_refused(name):
    assert _codes(s.environ(**{name: '{"x": "' + "a" * (64 * 1024) + '"}'}), name) == {(c.TOO_LONG, None)}


def test_duplicate_properties_are_refused_at_every_level():
    doc = json.dumps(s.single_document("csrf", 200))
    top = doc[:-1] + ', "current": "c1"}'
    assert _codes(s.environ(ORYX_F2_CSRF_KEYS_JSON=top), c.CSRF_KEYS_JSON) == {(c.DUPLICATE_PROPERTY, None)}
    nested = '{"schema": "oryx-f2-csrf-keys-v1", "current": "c1", "keys": {"c1": "%s", "c1": "%s"}}' % (
        s.key(200), s.key(201))
    assert _codes(s.environ(ORYX_F2_CSRF_KEYS_JSON=nested), c.CSRF_KEYS_JSON) == {(c.DUPLICATE_PROPERTY, None)}
    auth = json.dumps(s.auth_document())
    auth_dup = auth.replace('"keys": {', '"keys": {"otp": {"v9": "%s"}, ' % s.key(950), 1)
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=auth_dup), c.AUTH_KEYS_JSON) == {(c.DUPLICATE_PROPERTY, None)}
    current_dup = auth.replace('"current": {', '"current": {"abuse": "v1", ', 1)
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=current_dup), c.AUTH_KEYS_JSON) == {(c.DUPLICATE_PROPERTY, None)}


@pytest.mark.parametrize("family, name, base", [("csrf", c.CSRF_KEYS_JSON, 200),
                                                ("portfolio", c.PORTFOLIO_KEYS_JSON, 300),
                                                ("decryptage", c.DECRYPTAGE_KEYS_JSON, 400)])
def test_single_keyring_documents_have_exact_properties(family, name, base):
    good = s.single_document(family, base)
    cases = [
        ({**good, "extra": 1}, c.UNKNOWN_PROPERTY),
        ({k: v for k, v in good.items() if k != "current"}, c.MISSING_PROPERTY),
        ({k: v for k, v in good.items() if k != "keys"}, c.MISSING_PROPERTY),
        ({k: v for k, v in good.items() if k != "schema"}, c.MISSING_PROPERTY),
        ({**good, "schema": f"oryx-f2-{family}-keys-v2"}, c.UNKNOWN_SCHEMA),
        ({**good, "schema": "oryx-f2-auth-keys-v1"}, c.UNKNOWN_SCHEMA),
        ({**good, "keys": {}}, c.NO_KEY_VERSION),
        ({**good, "keys": []}, c.NOT_AN_OBJECT),
        ({**good, "current": "absent"}, c.CURRENT_VERSION_ABSENT),
        ({**good, "current": ""}, c.INVALID_VERSION_FIELD),
        ({**good, "current": 1}, c.INVALID_VERSION_FIELD),
        ({**good, "current": None}, c.INVALID_VERSION_FIELD),
    ]
    for document, code in cases:
        assert _codes(s.environ(**{name: json.dumps(document)}), name) == {(code, None)}, (document, code)


FREEZE_AUTH_EXAMPLE = """{
  "schema": "oryx-f2-auth-keys-v1",
  "keys": {
    "candidate": {"v1": "%s"},
    "derive": {"v1": "%s"},
    "active": {"v1": "%s"},
    "otp": {"v1": "%s"},
    "invitation": {"v1": "%s"},
    "abuse": {"v1": "%s"}
  },
  "current": {
    "candidate": "v1",
    "derive": "v1",
    "active": "v1",
    "otp": "v1",
    "invitation": "v1",
    "abuse": "v1"
  }
}"""


def test_contract_auth_document_has_exactly_the_frozen_structure():
    """Freeze F04-B V0.3 § 2.2 (docs/act_l1_f04b_freeze_v03.md) : document
    Auth F2 recopié sur l'exemple contractuel, clés synthétiques."""
    text = FREEZE_AUTH_EXAMPLE % tuple(s.key(800 + i) for i in range(6))
    config = c.load_f2_config(s.environ(ORYX_F2_AUTH_KEYS_JSON=text))
    for i, purpose in enumerate(PURPOSES):
        assert config.keyring.current(purpose) == ("v1", s.raw_key(800 + i))
        assert config.keyring.versions(purpose) == ("v1",)
    assert list(json.loads(text)) == ["schema", "keys", "current"]


def test_the_former_purposes_format_is_refused():
    """Ancien format de la PR (racine « purposes », keys/current par usage) :
    refusé, sans conversion ni double format."""
    former = {"schema": "oryx-f2-auth-keys-v1",
              "purposes": {p: {"current": "v1", "keys": {"v1": s.key(100 + i)}} for i, p in enumerate(PURPOSES)}}
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(former)), c.AUTH_KEYS_JSON) == {
        (c.UNKNOWN_PROPERTY, None)}
    mixed = {**s.auth_document(), "purposes": former["purposes"]}
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(mixed)), c.AUTH_KEYS_JSON) == {
        (c.UNKNOWN_PROPERTY, None)}


def test_auth_document_has_exact_properties_and_six_purposes():
    good = s.auth_document()
    keys, current = good["keys"], good["current"]
    cases = [
        ({**good, "extra": 1}, (c.UNKNOWN_PROPERTY, None)),
        ({"schema": good["schema"], "keys": keys}, (c.MISSING_PROPERTY, None)),
        ({"schema": good["schema"], "current": current}, (c.MISSING_PROPERTY, None)),
        ({"keys": keys, "current": current}, (c.MISSING_PROPERTY, None)),
        ({**good, "schema": "oryx-f2-auth-keys-v0"}, (c.UNKNOWN_SCHEMA, None)),
        ({**good, "keys": {k: v for k, v in keys.items() if k != "abuse"}}, (c.MISSING_PROPERTY, "keys")),
        ({**good, "keys": {**keys, "csrf": {"v1": s.key(960)}}}, (c.UNKNOWN_PROPERTY, "keys")),
        ({**good, "keys": []}, (c.NOT_AN_OBJECT, "keys")),
        ({**good, "current": {k: v for k, v in current.items() if k != "otp"}}, (c.MISSING_PROPERTY, "current")),
        ({**good, "current": {**current, "csrf": "v1"}}, (c.UNKNOWN_PROPERTY, "current")),
        ({**good, "current": "v1"}, (c.NOT_AN_OBJECT, "current")),
        ({**good, "keys": {**keys, "otp": []}}, (c.NOT_AN_OBJECT, "otp")),
        ({**good, "keys": {**keys, "derive": {}}}, (c.NO_KEY_VERSION, "derive")),
        ({**good, "current": {**current, "active": "v9"}}, (c.CURRENT_VERSION_ABSENT, "active")),
        ({**good, "current": {**current, "invitation": 1}}, (c.INVALID_VERSION_FIELD, "invitation")),
        ({**good, "current": {**current, "abuse": ""}}, (c.INVALID_VERSION_FIELD, "abuse")),
        ({**good, "current": {**current, "candidate": None}}, (c.INVALID_VERSION_FIELD, "candidate")),
    ]
    for document, expected in cases:
        assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(document)), c.AUTH_KEYS_JSON) == {expected}, document


# --------------------------------------------------------------------------
# Encodage des clés
# --------------------------------------------------------------------------

NON_CANONICAL = [
    s.key(200) + "=",                     # remplissage
    s.key(200)[:-1],                      # 42 caractères
    s.key(200) + "A",                     # 44 caractères
    s.b64(b"\x01" * 31),                  # 31 octets
    s.b64(b"\x01" * 33),                  # 33 octets
    s.key(200)[:-1] + "B",                # bits de remplissage non nuls (réencodage différent)
    "+" + s.key(200)[1:],                 # alphabet standard ('+')
    "AAAA" * 10 + "AA/",                  # '/'
    "",
    None,
    32,
]


@pytest.mark.parametrize("encoded", NON_CANONICAL)
def test_non_canonical_or_wrong_length_keys_are_refused(encoded):
    document = s.single_document("csrf", 200, keys={"c1": encoded})
    assert _codes(s.environ(ORYX_F2_CSRF_KEYS_JSON=json.dumps(document)), c.CSRF_KEYS_JSON) == {
        (c.INVALID_KEY_ENCODING, None)}


def test_non_canonical_auth_key_is_refused_with_its_purpose():
    document = s.auth_document()
    document["keys"]["invitation"]["v1"] += "="
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(document)), c.AUTH_KEYS_JSON) == {
        (c.INVALID_KEY_ENCODING, "invitation")}


# --------------------------------------------------------------------------
# Versions : historiques, syntaxe propre à chaque classe
# --------------------------------------------------------------------------

def test_historical_versions_are_kept_next_to_the_current_one():
    auth = s.auth_document()
    s.set_purpose(auth, "abuse", {"v1": s.key(500), "v2": s.key(501)}, "v2")
    csrf = s.single_document("csrf", 200, current="c2", keys={"c1": s.key(510), "c2": s.key(511)})
    portfolio = s.single_document("portfolio", 300, current="p1", keys={"p0": s.key(520), "p1": s.key(521)})
    decryptage = s.single_document("decryptage", 400, keys={"d1": s.key(530), "d0": s.key(531)})
    config = c.load_f2_config(s.environ(
        ORYX_F2_AUTH_KEYS_JSON=json.dumps(auth), ORYX_F2_CSRF_KEYS_JSON=json.dumps(csrf),
        ORYX_F2_PORTFOLIO_KEYS_JSON=json.dumps(portfolio), ORYX_F2_DECRYPTAGE_KEYS_JSON=json.dumps(decryptage)))
    assert config.keyring.versions("abuse") == ("v1", "v2") and config.keyring.current_version("abuse") == "v2"
    assert config.keyring.key("abuse", "v1") == s.raw_key(500)
    assert config.csrf_keyring.versions() == ("c1", "c2") and config.csrf_keyring.current_version == "c2"
    assert config.portfolio_keyring.versions() == ("p0", "p1") and config.portfolio_keyring.current_version == "p1"
    assert config.decryptage_keyring.versions() == ("d0", "d1")


@pytest.mark.parametrize("version, auth_ok, csrf_ok, scoped_ok", [
    ("v1", True, True, True),
    ("V1", True, True, False),            # majuscules : Keyring, CSRF seulement
    ("v.1", True, False, False),          # point : Keyring seulement
    ("_v1", True, True, False),           # initiale non alphanumérique
    ("a" * 32, True, True, True),
    ("a" * 33, True, False, False),       # > 32 : Keyring seulement (64 au plus)
    ("a" * 65, False, False, False),
    ("v 1", False, False, False),
    ("vé", False, False, False),
])
def test_version_syntax_is_the_one_of_each_existing_class(version, auth_ok, csrf_ok, scoped_ok):
    auth = s.auth_document()
    s.set_purpose(auth, "otp", {version: s.key(600)}, version)
    results = {}
    env = s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(auth),
                    ORYX_F2_CSRF_KEYS_JSON=json.dumps(s.single_document("csrf", 200, current=version,
                                                                        keys={version: s.key(601)})),
                    ORYX_F2_PORTFOLIO_KEYS_JSON=json.dumps(s.single_document("portfolio", 300, current=version,
                                                                             keys={version: s.key(602)})),
                    ORYX_F2_DECRYPTAGE_KEYS_JSON=json.dumps(s.single_document("decryptage", 400, current=version,
                                                                              keys={version: s.key(603)})))
    try:
        c.load_f2_config(env)
        problems = set()
    except c.F2ConfigError as exc:
        problems = {(name, code) for name, code, _ in exc.problems}
    for name in KEYRINGS:
        results[name] = (name, c.KEYRING_REJECTED) not in problems
    assert results == {c.AUTH_KEYS_JSON: auth_ok, c.CSRF_KEYS_JSON: csrf_ok, c.PORTFOLIO_KEYS_JSON: scoped_ok,
                       c.DECRYPTAGE_KEYS_JSON: scoped_ok}
    assert problems <= {(name, c.KEYRING_REJECTED) for name in KEYRINGS}


# --------------------------------------------------------------------------
# Collisions de clés
# --------------------------------------------------------------------------

def test_a_key_reused_across_versions_of_one_keyring_is_refused():
    document = s.single_document("portfolio", 300, keys={"p1": s.key(300), "p2": s.key(300)})
    assert _codes(s.environ(ORYX_F2_PORTFOLIO_KEYS_JSON=json.dumps(document)), c.PORTFOLIO_KEYS_JSON) == {
        (c.KEY_REUSED, None)}


def test_a_key_reused_across_auth_purposes_or_versions_is_refused():
    shared = s.auth_document()
    shared["keys"]["active"]["v1"] = shared["keys"]["candidate"]["v1"]
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(shared)), c.AUTH_KEYS_JSON) == {
        (c.KEY_REUSED, "active")}
    versions = s.auth_document()
    versions["keys"]["otp"]["v0"] = versions["keys"]["otp"]["v1"]
    assert _codes(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(versions)), c.AUTH_KEYS_JSON) == {
        (c.KEY_REUSED, "otp")}


FAMILY_DOCS = {
    c.CSRF_KEYS_JSON: ("csrf", 200), c.PORTFOLIO_KEYS_JSON: ("portfolio", 300),
    c.DECRYPTAGE_KEYS_JSON: ("decryptage", 400),
}


@pytest.mark.parametrize("purpose", PURPOSES)
@pytest.mark.parametrize("target", list(FAMILY_DOCS))
def test_an_auth_key_reused_by_another_family_is_refused(purpose, target):
    family, _ = FAMILY_DOCS[target]
    stolen = s.key(100 + PURPOSES.index(purpose))
    document = s.single_document(family, 0, keys={f"{family[0]}1": s.key(700), f"{family[0]}0": stolen})
    assert _problems(s.environ(**{target: json.dumps(document)})) == {(target, c.KEY_REUSED, None)}


@pytest.mark.parametrize("source, target", [(c.CSRF_KEYS_JSON, c.PORTFOLIO_KEYS_JSON),
                                            (c.CSRF_KEYS_JSON, c.DECRYPTAGE_KEYS_JSON),
                                            (c.PORTFOLIO_KEYS_JSON, c.DECRYPTAGE_KEYS_JSON)])
def test_a_key_shared_between_two_dedicated_families_is_refused(source, target):
    family, _ = FAMILY_DOCS[target]
    stolen = s.key(FAMILY_DOCS[source][1])
    document = s.single_document(family, 0, keys={f"{family[0]}1": stolen})
    assert _problems(s.environ(**{target: json.dumps(document)})) == {(target, c.KEY_REUSED, None)}


def test_several_problems_are_reported_together_without_values():
    env = s.environ(DATABASE_URL="sqlite:///x", ORYX_F2_CSRF_KEYS_JSON="{", ORYX_F2_FMP_API_KEY=None,
                    ORYX_F2_TYPO="synthetic-supabase-service-key")
    assert _problems(env) == {("DATABASE_URL", c.INVALID_DATABASE_URL, None), (c.CSRF_KEYS_JSON, c.INVALID_JSON, None),
                              (c.FMP_API_KEY, c.MISSING, None), ("ORYX_F2_TYPO", c.UNKNOWN_VARIABLE, None)}


def test_errors_never_chain_decoder_messages():
    with pytest.raises(c.F2ConfigError) as info:
        c.load_f2_config(s.environ(ORYX_F2_CSRF_KEYS_JSON='{"schema": "' + "synthetic-password" + '",'))
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert "synthetic-password" not in str(info.value)
    assert pickle.loads(pickle.dumps(info.value)).problems == info.value.problems


def test_no_secret_is_generated_and_nothing_defaults():
    """Deux chargements partiels identiques échouent de la même façon :
    aucune clé générée, aucune valeur par défaut substituée."""
    env = s.environ(ORYX_F2_AUTH_KEYS_JSON=None, ORYX_F2_CSRF_KEYS_JSON=None)
    assert _problems(env) == _problems(dict(env)) == {(c.AUTH_KEYS_JSON, c.MISSING, None),
                                                      (c.CSRF_KEYS_JSON, c.MISSING, None)}


def test_loaded_configuration_is_immutable():
    """Freeze F04-B V0.3 § 2.3 : objets typés et immuables."""
    import dataclasses

    config = c.load_f2_config(s.environ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.decryptage_model = "autre"
    with pytest.raises((AttributeError, TypeError)):
        config.extra = 1
    with pytest.raises(TypeError):
        config.keyring._keys["otp"] = {}  # MappingProxyType : lecture seule
