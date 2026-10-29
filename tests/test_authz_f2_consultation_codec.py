"""Codec pur et vues de R1 PR 8 (sans base de données).

Couvre : décodage strict de la query (2048 octets, %HH, UTF-8 strict, « + »
et %2B, doublons après décodage, paramètres inconnus, valeurs vides),
limit, filtre ticker, curseurs versionnés (Base64URL sans padding,
canonicité JSON, doublons, champs inconnus, version, route, filtre,
dates, identifiants, 1024 / 1025 caractères, curseur trop long à la
production), dates publiques (microsecondes, UTC historique, TIMESTAMPTZ,
hors représentation), commandes fermées sans user_id, vues fermées
(états, diagnostics, provenance, ordre total), corps et bornes de taille.
"""
import base64
import dataclasses
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.authz_f2 import commands as c
from core.authz_f2_http import consultation_codec as codec
from tests.authz_f2_consultation_support import b64, cursor_of

SID = uuid.UUID("11111111-2222-4333-8444-555555555555")
AWARE = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 10, 28, 12, 0)


def overview_cursor(ticker="AIR.PA", **changes):
    return cursor_of({"last_ticker": ticker, "route": "analysis-overview", "v": 1, **changes})


def theses_cursor(created="2026-10-28T12:00:00.000000Z", thesis_id=42, ticker=None, **changes):
    return cursor_of({"last_created_at": created, "last_thesis_id": thesis_id, "route": "theses", "ticker": ticker,
                      "v": 1, **changes})


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (b"", {}),
    (b"limit=5", {"limit": "5"}),
    (b"ticker=A+B", {"ticker": "A B"}),            # « + » : espace (formulaire)
    (b"ticker=A%2BB", {"ticker": "A+B"}),          # %2B : vrai plus
    (b"ticker=A%2bB", {"ticker": "A+B"}),
    (b"l%69mit=5", {"limit": "5"}),                # noms comparés après décodage
    (b"ticker=%C3%89DF", {"ticker": "ÉDF"}),
    (b"ticker=A=B", {"ticker": "A=B"}),
    (b"ticker=%2525", {"ticker": "%25"}),          # un SEUL décodage
])
def test_query_is_decoded_once_and_strictly(raw, expected):
    assert codec.decode_query(raw, codec.THESES_PARAMS) == expected


@pytest.mark.parametrize("raw", [
    b"limit=5&limit=6", b"limit=5&l%69mit=6", b"foo=1", b"user_id=user-b", b"limit", b"=5", b"limit=",
    b"limit=5&", b"&limit=5", b"limit=5&&cursor=x", b"ticker=%", b"ticker=%4", b"ticker=%G1", b"ticker=%FF",
    b"ticker=%C3", b"ticker=%ED%A0%80", b"ticker=\xc3\x89", b"ticker=a b", b"ticker=a\tb", b"ticker=\x7f",
    b"limit=5#x" + b"\x00",
])
def test_query_refusals(raw):
    with pytest.raises(codec.InvalidRequest):
        codec.decode_query(raw, codec.THESES_PARAMS)


def test_query_raw_bound_is_2048_bytes_before_decoding():
    exact = b"ticker=" + b"A" * (2048 - 7)
    assert len(exact) == 2048
    assert codec.decode_query(exact, codec.THESES_PARAMS)["ticker"] == "A" * 2041
    with pytest.raises(codec.InvalidRequest):
        codec.decode_query(exact + b"A", codec.THESES_PARAMS)


def test_ticker_is_unknown_on_the_overview():
    with pytest.raises(codec.InvalidRequest):
        codec.decode_query(b"ticker=AIR.PA", codec.OVERVIEW_PARAMS)


@pytest.mark.parametrize("text,value", [(None, 25), ("1", 1), ("25", 25), ("50", 50), ("9", 9)])
def test_limit(text, value):
    assert codec.parse_limit(text) == value


@pytest.mark.parametrize("text", ["0", "51", "99", "100", "05", "+5", "-5", " 5", "5 ", "٥", "５", "5.0", "1e1", "",
                                  "x"])
def test_limit_refusals(text):
    with pytest.raises(codec.InvalidRequest):
        codec.parse_limit(text)


@pytest.mark.parametrize("raw", [b"ticker=%20AIR", b"ticker=AIR%20", b"ticker=" + b"A" * 33,
                                 b"ticker=%00", b"ticker=AIR%0A", b"ticker=%E2%80%83AIR"])
def test_ticker_filter_refusals(raw):
    with pytest.raises(codec.InvalidRequest):
        codec.theses_command(raw)


def test_ticker_filter_is_exact_never_corrected():
    command = codec.theses_command(b"ticker=air.pa")
    assert command.ticker == "air.pa"  # ni upper ni strip
    assert codec.theses_command(b"ticker=" + b"A" * 32).ticker == "A" * 32
    assert codec.theses_command("ticker=𝔸𝔹".encode("utf-8").replace("𝔸𝔹".encode(), b"%F0%9D%94%B8%F0%9D%94%B9")
                                ).ticker == "𝔸𝔹"


@pytest.mark.parametrize("text,value", [("1", 1), ("42", 42), ("2147483647", 2147483647)])
def test_thesis_id(text, value):
    assert codec.parse_thesis_id(text) == value


@pytest.mark.parametrize("text", ["0", "-1", "+1", "01", "1.0", "1e3", "2147483648", "99999999999", "١", "１", "",
                                  " 1", "0x1"])
def test_thesis_id_refusals(text):
    with pytest.raises(codec.InvalidRequest):
        codec.parse_thesis_id(text)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def test_timestamps_keep_six_decimals_and_are_rendered_in_utc():
    assert codec.render_timestamp(None) is None
    assert codec.render_timestamp(NAIVE) == "2026-10-28T12:00:00.000000Z"  # UTC historique, aucune conversion
    assert codec.render_timestamp(datetime(2026, 1, 2, 3, 4, 5, 6)) == "2026-01-02T03:04:05.000006Z"
    paris = timezone(timedelta(hours=2))
    assert codec.render_timestamp(datetime(2026, 10, 28, 14, 0, 0, 123456, tzinfo=paris)) == \
        "2026-10-28T12:00:00.123456Z"
    assert codec.render_timestamp(datetime(1, 1, 1)) == "0001-01-01T00:00:00.000000Z"
    assert codec.render_timestamp(datetime(9999, 12, 31, 23, 59, 59, 999999)) == "9999-12-31T23:59:59.999999Z"


def test_unrepresentable_timestamps_are_unrestorable():
    with pytest.raises(codec.Unrestorable):
        codec.render_timestamp(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))))  # < 0001-01-01 UTC
    with pytest.raises(codec.Unrestorable):
        codec.render_timestamp(datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-2))))
    for bad in ("2026-10-28", 1730000000, NAIVE.date()):
        with pytest.raises(codec.Unrestorable):
            codec.render_timestamp(bad)


def test_cursor_timestamps_are_reconstructed_exactly():
    assert codec.parse_timestamp("2026-10-28T12:00:00.000001Z") == datetime(2026, 10, 28, 12, 0, 0, 1)
    assert codec.parse_timestamp("0001-01-01T00:00:00.000000Z").tzinfo is None
    for bad in ("2026-10-28T12:00:00Z", "2026-10-28T12:00:00.000000", "2026-10-28T12:00:00.000000+00:00",
                "2026-10-28 12:00:00.000000Z", "2026-02-30T12:00:00.000000Z", "0000-01-01T00:00:00.000000Z",
                "2026-10-28T24:00:00.000000Z", "2026-10-28T12:00:00.0000000Z", "２026-10-28T12:00:00.000000Z",
                "2026-10-28t12:00:00.000000z", None, 5):
        with pytest.raises(codec.InvalidRequest):
            codec.parse_timestamp(bad)


# --------------------------------------------------------------------------
# Curseurs
# --------------------------------------------------------------------------

def test_cursor_roundtrip_is_canonical_base64url_without_padding():
    text = overview_cursor("AIR.PA")
    assert "=" not in text and set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert codec.overview_command(f"cursor={text}".encode()).after_ticker == "AIR.PA"
    assert codec.encode_cursor({"last_ticker": "AIR.PA", "route": "analysis-overview", "v": 1}) == text
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    assert raw == b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":1}'


def _overview(cursor):
    return codec.overview_command(b"cursor=" + cursor.encode("latin-1"))


@pytest.mark.parametrize("cursor", [
    overview_cursor() + "=",                                                      # padding
    overview_cursor() + "==",
    overview_cursor().replace("-", "+").replace("_", "/") if "-" in overview_cursor() or "_" in overview_cursor()
    else overview_cursor() + "+",                                                 # alphabet standard
    overview_cursor()[:-1],                                                       # tronqué
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":1}')[:-1] + "B",  # bits de remplissage
    b64(b'{"route":"analysis-overview","last_ticker":"AIR.PA","v":1}'),            # ordre des clés
    b64(b'{"last_ticker": "AIR.PA","route":"analysis-overview","v":1}'),           # blanc
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":1} '),
    b64(b'{"last_ticker":"\\u0041IR.PA","route":"analysis-overview","v":1}'),      # échappement superflu
    b64(b'{"last_ticker":"AIR.PA","last_ticker":"AIR.PA","route":"analysis-overview","v":1}'),  # doublon
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":1,"x":1}'),      # champ inconnu
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview"}'),                  # champ manquant
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":2}'),            # version
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":1.0}'),
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":true}'),
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":"1"}'),
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":NaN}'),
    b64(b'{"last_ticker":"AIR.PA","route":"analysis-overview","v":1e400}'),
    b64(b'{"last_ticker":"AIR.PA","route":"theses","v":1}'),                       # autre route
    b64(b'{"last_ticker":null,"route":"analysis-overview","v":1}'),
    b64(b'{"last_ticker":5,"route":"analysis-overview","v":1}'),
    b64(b'{"last_ticker":"\\u0000","route":"analysis-overview","v":1}'),          # NUL : jamais un ticker PostgreSQL
    b64(b'{"last_ticker":"\\ud800","route":"analysis-overview","v":1}'),          # substitut isolé
    b64(b'{"last_ticker":"\xff","route":"analysis-overview","v":1}'),             # UTF-8 invalide
    b64(b'["AIR.PA","analysis-overview",1]'),
    b64(b""), "", "!", "A", "AB=C",
    theses_cursor(),                                                             # curseur d'une autre route
])
def test_overview_cursor_refusals(cursor):
    with pytest.raises(codec.InvalidRequest):
        _overview(cursor)


def test_non_zero_padding_bits_are_an_alternative_encoding():
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for ticker in ("A", "AB", "ABC"):
        text = overview_cursor(ticker)
        if len(text) % 4 == 0:
            continue
        unused = 4 if len(text) % 4 == 2 else 2  # bits de remplissage du dernier caractère
        index = alphabet.index(text[-1])
        assert index % (1 << unused) == 0
        alternative = text[:-1] + alphabet[index + 1]
        # Le décodeur Python, laxiste, rend les MÊMES octets : seul le
        # réencodage canonique les distingue.
        assert base64.urlsafe_b64decode(alternative + "=" * (-len(alternative) % 4)) == \
            base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        with pytest.raises(codec.InvalidRequest):
            _overview(alternative)
        assert _overview(text).after_ticker == ticker


def test_overview_cursor_accepts_exact_historical_tickers():
    for ticker in ("", "air.pa", "AIR.PA ", " A", "A&B=C", "Ä", "😀", "á", '"\\', " "):
        assert _overview(overview_cursor(ticker)).after_ticker == ticker


def _theses(query: bytes):
    return codec.theses_command(query)


def test_theses_cursor_carries_date_id_and_filter():
    command = _theses(f"cursor={theses_cursor()}".encode())
    assert command == c.ListThesesPage(25, None, 42, datetime(2026, 10, 28, 12))
    command = _theses(f"cursor={theses_cursor(None, 7)}".encode())
    assert command == c.ListThesesPage(25, None, 7, None)  # dernière date nulle
    command = _theses(f"limit=3&ticker=AIR.PA&cursor={theses_cursor(ticker='AIR.PA')}".encode())
    assert command == c.ListThesesPage(3, "AIR.PA", 42, datetime(2026, 10, 28, 12))
    assert _theses(b"") == c.ListThesesPage(25, None, None, None)


@pytest.mark.parametrize("query", [
    f"cursor={theses_cursor(ticker='AIR.PA')}",                    # filtre retiré
    f"ticker=AIR.PA&cursor={theses_cursor()}",                     # filtre ajouté
    f"ticker=MC.PA&cursor={theses_cursor(ticker='AIR.PA')}",       # filtre changé
    f"ticker=air.pa&cursor={theses_cursor(ticker='AIR.PA')}",
    f"cursor={theses_cursor(thesis_id=0)}", f"cursor={theses_cursor(thesis_id=-1)}",
    f"cursor={theses_cursor(thesis_id=2147483648)}", f"cursor={theses_cursor(thesis_id='42')}",
    f"cursor={theses_cursor(thesis_id=True)}", f"cursor={theses_cursor(thesis_id=None)}",
    f"cursor={theses_cursor(thesis_id=42.0)}",
    f"cursor={theses_cursor(created='2026-10-28T12:00:00Z')}", f"cursor={theses_cursor(created=0)}",
    f"cursor={theses_cursor(created='2026-02-30T00:00:00.000000Z')}",
    f"cursor={theses_cursor(route='analysis-overview')}", f"cursor={theses_cursor(v=2)}",
    f"cursor={theses_cursor(ticker=5)}",
    f"cursor={overview_cursor()}",
    f"cursor={cursor_of({'last_created_at': None, 'last_thesis_id': 1, 'route': 'theses', 'v': 1})}",
])
def test_theses_cursor_refusals(query):
    with pytest.raises(codec.InvalidRequest):
        _theses(query.encode())


def _cursor_with_length(target):
    """Curseur de synthèse valide d'exactement `target` caractères."""
    for size in range(0, 900):
        text = overview_cursor("A" * size)
        if len(text) == target:
            return text
    raise AssertionError(target)


def test_cursor_length_bound_1024_and_1025():
    exact = _cursor_with_length(1024)
    assert _overview(exact).after_ticker.startswith("A")
    over = _cursor_with_length(1026)  # 1025 n'est jamais une longueur Base64URL de ce JSON
    with pytest.raises(codec.InvalidRequest):
        _overview(over)
    with pytest.raises(codec.InvalidRequest):
        _overview(exact + "A")  # 1025 caractères
    with pytest.raises(codec.InvalidRequest):
        _overview("A" * 1025)


def test_produced_cursor_too_long_is_unrestorable():
    with pytest.raises(codec.Unrestorable):
        codec.encode_cursor({"last_ticker": '"' * 512, "route": "analysis-overview", "v": 1})
    assert len(codec.encode_cursor({"last_ticker": "A" * 512, "route": "analysis-overview", "v": 1})) <= 1024


# --------------------------------------------------------------------------
# Commandes et vues fermées
# --------------------------------------------------------------------------

def test_new_commands_carry_no_owner_and_validate_their_fields():
    for command_type in (c.ListAnalysisOverviewPage, c.ListThesesPage, c.ReadThesis):
        assert "user_id" not in {f.name for f in dataclasses.fields(command_type)}
        with pytest.raises(TypeError):
            command_type(user_id="user-b")
    for bad in (0, 51, True, "5", 5.0, None):
        with pytest.raises(ValueError):
            c.ListAnalysisOverviewPage(bad, None)
        with pytest.raises(ValueError):
            c.ListThesesPage(bad, None, None, None)
    for bad in (5, b"A", "\x00", "\ud800"):
        with pytest.raises(ValueError):
            c.ListAnalysisOverviewPage(1, bad)
    assert c.ListAnalysisOverviewPage(1, "").after_ticker == ""
    for bad in ("", " A", "A ", "A" * 33, 5, "\x00"):
        with pytest.raises(ValueError):
            c.ListThesesPage(1, bad, None, None)
    for thesis_id, date in ((None, NAIVE), (0, None), (2**31, None), (True, None), (1, AWARE), (1, "2026-10-28")):
        with pytest.raises(ValueError):
            c.ListThesesPage(1, None, thesis_id, date)
    for bad in (0, -1, 2**31, True, "1", 1.0, None):
        with pytest.raises(ValueError):
            c.ReadThesis(bad)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.ReadThesis(1).thesis_id = 2


def test_steps_are_those_of_construction_these():
    from core.decryptage_progress import DECRYPTAGE_STEP_MARKERS
    assert c.ANALYSIS_STEPS == DECRYPTAGE_STEP_MARKERS


def test_overview_entries_are_closed():
    ok = c.AnalysisOverviewEntry("AIR.PA", c.IN_PROGRESS, SID, "moat", AWARE, None, ())
    assert ok.current_step == "moat"
    c.AnalysisOverviewEntry("AIR.PA", c.IN_PROGRESS, SID, None, AWARE, None, (c.INVALID_CURRENT_STEP,))
    c.AnalysisOverviewEntry("AIR.PA", c.IN_PROGRESS, SID, None, AWARE, None,
                            (c.MULTIPLE_ACTIVE_SESSIONS, c.INVALID_CURRENT_STEP))
    c.AnalysisOverviewEntry("AIR.PA", c.THESIS_AVAILABLE, None, None, NAIVE, 4, ())
    c.AnalysisOverviewEntry("", c.THESIS_AVAILABLE, None, None, None, 4, ())
    bad = [
        ("AIR.PA", "completed", SID, "moat", AWARE, None, ()),
        ("AIR.PA", c.IN_PROGRESS, SID, "moat", AWARE, 4, ()),           # thèse ET session
        ("AIR.PA", c.IN_PROGRESS, str(SID), "moat", AWARE, None, ()),
        ("AIR.PA", c.IN_PROGRESS, SID, "inconnue", AWARE, None, ()),     # étape de substitution
        ("AIR.PA", c.IN_PROGRESS, SID, None, AWARE, None, ()),           # diagnostic manquant
        ("AIR.PA", c.IN_PROGRESS, SID, "moat", AWARE, None, (c.INVALID_CURRENT_STEP,)),
        ("AIR.PA", c.IN_PROGRESS, SID, None, AWARE, None, (c.INVALID_CURRENT_STEP, c.MULTIPLE_ACTIVE_SESSIONS)),
        ("AIR.PA", c.IN_PROGRESS, SID, "moat", AWARE, None, ("autre",)),
        ("AIR.PA", c.IN_PROGRESS, SID, "moat", NAIVE, None, ()),         # TIMESTAMPTZ attendu
        ("AIR.PA", c.IN_PROGRESS, SID, "moat", None, None, ()),
        ("AIR.PA", c.THESIS_AVAILABLE, None, None, AWARE, 4, ()),        # TIMESTAMP naïf attendu
        ("AIR.PA", c.THESIS_AVAILABLE, None, "swot_final", NAIVE, 4, ()),  # aucune étape inventée
        ("AIR.PA", c.THESIS_AVAILABLE, SID, None, NAIVE, 4, ()),
        ("AIR.PA", c.THESIS_AVAILABLE, None, None, NAIVE, None, ()),
        ("AIR.PA", c.THESIS_AVAILABLE, None, None, NAIVE, 4, (c.MULTIPLE_ACTIVE_SESSIONS,)),
        ("A" * 513, c.THESIS_AVAILABLE, None, None, NAIVE, 4, ()),
        ("é" * 257, c.THESIS_AVAILABLE, None, None, NAIVE, 4, ()),       # 514 octets
        (5, c.THESIS_AVAILABLE, None, None, NAIVE, 4, ()),
    ]
    for args in bad:
        with pytest.raises(ValueError):
            c.AnalysisOverviewEntry(*args)
    c.AnalysisOverviewEntry("é" * 256, c.THESIS_AVAILABLE, None, None, NAIVE, 4, ())  # 512 octets


def test_overview_page_requires_strict_code_point_order():
    entry = lambda t: c.AnalysisOverviewEntry(t, c.THESIS_AVAILABLE, None, None, None, 1, ())  # noqa: E731
    c.AnalysisOverviewPage((entry("AIR.PA"), entry("a"), entry("é")), False)  # ordre "C"
    for tickers in (("a", "AIR.PA"), ("A", "A")):
        with pytest.raises(ValueError):
            c.AnalysisOverviewPage(tuple(entry(t) for t in tickers), False)
    with pytest.raises(ValueError):
        c.AnalysisOverviewPage([entry("A")], False)
    with pytest.raises(ValueError):
        c.AnalysisOverviewPage((entry("A"),), 1)


def test_diagnostics_are_computed_in_closed_order():
    assert c.overview_diagnostics(1, "moat") == ()
    assert c.overview_diagnostics(2, "moat") == (c.MULTIPLE_ACTIVE_SESSIONS,)
    assert c.overview_diagnostics(1, None) == (c.INVALID_CURRENT_STEP,)
    assert c.overview_diagnostics(3, None) == (c.MULTIPLE_ACTIVE_SESSIONS, c.INVALID_CURRENT_STEP)
    for bad in ((0, "moat"), (True, "moat"), (1, "inconnue")):
        with pytest.raises(ValueError):
            c.overview_diagnostics(*bad)


def test_thesis_total_order_is_created_at_desc_nulls_last_then_id_desc():
    early, late = datetime(2020, 1, 1), datetime(2026, 1, 1)
    ordered = [(late, 9), (late, 3), (early, 8), (None, 7), (None, 2)]
    for (a, ai), (b, bi) in zip(ordered, ordered[1:]):
        assert c.thesis_precedes(a, ai, b, bi) and not c.thesis_precedes(b, bi, a, ai)
    assert not c.thesis_precedes(late, 3, late, 3)
    entries = tuple(c.ThesisListEntry(i, "A", d, c.UNVERIFIED) for d, i in ordered)
    c.ThesesPage(entries, False)
    with pytest.raises(ValueError):
        c.ThesesPage(tuple(reversed(entries)), False)


def test_thesis_views_are_closed():
    c.ThesisListEntry(1, "A", None, c.VERIFIED)
    for args in ((0, "A", None, c.VERIFIED), (1, "A" * 513, None, c.VERIFIED), (1, "A", AWARE, c.VERIFIED),
                 (1, "A", None, "inconnue"), (1, "A", None, True)):
        with pytest.raises(ValueError):
            c.ThesisListEntry(*args)
    view = c.ThesisDetailView(1, "A", "x" * c.THESIS_TEXT_MAX_BYTES, NAIVE, SID)
    assert view.provenance_status == c.VERIFIED
    assert c.ThesisDetailView(1, "A", "", None, None).provenance_status == c.UNVERIFIED
    for args in ((1, "A", "x" * (c.THESIS_TEXT_MAX_BYTES + 1), None, None), (1, "A", None, None, None),
                 (1, "A", "x", None, str(SID)), (1, "A", "\ud800", None, None)):
        with pytest.raises(ValueError):
            c.ThesisDetailView(*args)


# --------------------------------------------------------------------------
# Corps
# --------------------------------------------------------------------------

def test_overview_body_shape_and_cursor_from_last_rendered_entry():
    entries = (c.AnalysisOverviewEntry("AIR.PA", c.IN_PROGRESS, SID, "moat", AWARE, None, ()),
               c.AnalysisOverviewEntry("MC.PA", c.THESIS_AVAILABLE, None, None, NAIVE, 7, ()))
    body = codec.overview_body(c.AnalysisOverviewPage(entries, True), c.ListAnalysisOverviewPage(2, None))
    data = json.loads(body)
    assert set(data) == {"entries", "next_cursor"}
    assert data["entries"][0] == {"ticker": "AIR.PA", "state": "in_progress", "analysis_session_id": str(SID),
                                  "current_step": "moat", "updated_at": "2026-10-28T12:00:00.000000Z",
                                  "latest_thesis_id": None, "diagnostics": []}
    assert data["entries"][1] == {"ticker": "MC.PA", "state": "thesis_available", "analysis_session_id": None,
                                  "current_step": None, "updated_at": "2026-10-28T12:00:00.000000Z",
                                  "latest_thesis_id": 7, "diagnostics": []}
    assert json.loads(base64.urlsafe_b64decode(data["next_cursor"] + "==")) == {
        "last_ticker": "MC.PA", "route": "analysis-overview", "v": 1}
    assert body == codec.canonical_json(data)
    last = codec.overview_body(c.AnalysisOverviewPage(entries, False), c.ListAnalysisOverviewPage(2, None))
    assert json.loads(last)["next_cursor"] is None
    empty = codec.overview_body(c.AnalysisOverviewPage((), False), c.ListAnalysisOverviewPage(2, "Z"))
    assert empty == b'{"entries":[],"next_cursor":null}'


@pytest.mark.parametrize("page,command", [
    ((("A", 1),), (1, "B")),          # page antérieure au curseur
    ((("A", 1),), (1, "A")),
    ((("A", 1), ("B", 2)), (1, None)),  # plus d'entrées que limit
])
def test_overview_body_refuses_incoherent_pages(page, command):
    entries = tuple(c.AnalysisOverviewEntry(t, c.THESIS_AVAILABLE, None, None, None, i, ()) for t, i in page)
    with pytest.raises(codec.Unrestorable):
        codec.overview_body(c.AnalysisOverviewPage(entries, False), c.ListAnalysisOverviewPage(*command))
    with pytest.raises(codec.Unrestorable):
        codec.overview_body(c.AnalysisOverviewPage(entries[:1], True), c.ListAnalysisOverviewPage(2, None))
    with pytest.raises(codec.Unrestorable):
        codec.overview_body(object(), c.ListAnalysisOverviewPage(2, None))


def test_theses_body_shape_filter_and_cursor():
    entries = (c.ThesisListEntry(9, "AIR.PA", NAIVE, c.VERIFIED), c.ThesisListEntry(3, "AIR.PA", None, c.UNVERIFIED))
    body = codec.theses_body(c.ThesesPage(entries, True), c.ListThesesPage(2, "AIR.PA", None, None))
    data = json.loads(body)
    assert data["theses"] == [
        {"thesis_id": 9, "ticker": "AIR.PA", "created_at": "2026-10-28T12:00:00.000000Z", "provenance_status": "verified"},
        {"thesis_id": 3, "ticker": "AIR.PA", "created_at": None, "provenance_status": "unverified"}]
    assert json.loads(base64.urlsafe_b64decode(data["next_cursor"] + "==")) == {
        "last_created_at": None, "last_thesis_id": 3, "route": "theses", "ticker": "AIR.PA", "v": 1}
    assert "thesis_text" not in body.decode()
    with pytest.raises(codec.Unrestorable):  # thèse hors filtre
        codec.theses_body(c.ThesesPage(entries, False), c.ListThesesPage(2, "MC.PA", None, None))
    with pytest.raises(codec.Unrestorable):  # page antérieure au curseur
        codec.theses_body(c.ThesesPage(entries, False), c.ListThesesPage(2, None, 9, NAIVE))
    assert codec.theses_body(c.ThesesPage((), False), c.ListThesesPage(2, None, 9, NAIVE)) == \
        b'{"next_cursor":null,"theses":[]}'


def test_thesis_body_and_exact_size_bound():
    view = c.ThesisDetailView(42, "AIR.PA", "Texte original.", NAIVE, SID)
    data = json.loads(codec.thesis_body(view, 42))
    assert data == {"thesis_id": 42, "ticker": "AIR.PA", "thesis_text": "Texte original.",
                    "created_at": "2026-10-28T12:00:00.000000Z",
                    "provenance": {"status": "verified", "analysis_session_id": str(SID)}}
    unverified = json.loads(codec.thesis_body(c.ThesisDetailView(42, "A", "t", None, None), 42))
    assert unverified["provenance"] == {"status": "unverified", "analysis_session_id": None}
    with pytest.raises(codec.Unrestorable):
        codec.thesis_body(view, 41)
    overhead = len(codec.thesis_body(c.ThesisDetailView(42, "A", "", None, None), 42))
    exact = c.ThesisDetailView(42, "A", "x" * (codec.MAX_RESPONSE_BYTES - overhead), None, None)
    assert len(codec.thesis_body(exact, 42)) == 131072
    over = c.ThesisDetailView(42, "A", "x" * (codec.MAX_RESPONSE_BYTES - overhead + 1), None, None)
    with pytest.raises(codec.Unrestorable):
        codec.thesis_body(over, 42)
    escaped = c.ThesisDetailView(42, "A", '"' * 70000, None, None)  # 70 000 octets bruts, 140 000 échappés
    with pytest.raises(codec.Unrestorable):
        codec.thesis_body(escaped, 42)


def test_codec_module_is_pure():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(codec))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules == {"base64", "binascii", "json", "re", "datetime", "core.authz_f2"}
