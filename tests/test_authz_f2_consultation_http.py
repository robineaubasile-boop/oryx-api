"""Routes de lecture R1 PR 8 sur l'application personnelle isolée.

  GET /api/v2/me/analysis-overview
  GET /api/v2/me/theses
  GET /api/v2/me/theses/{thesis_id}

Vrai AuthorizedTransactionRunner, sessions du vrai moteur F2. Couvre :
vues JSON exactes et canoniques, dates publiques, 404 indiscernables,
parcours paginés complets (dates égales et nulles, filtre, limit 1 / 25 /
50, page de exactement limit entrées, page vide, dernière page), mutations
historiques entre deux pages, bornes 131072 / 512 octets, curseur trop long
à la production, provenance, transport strict (Host, Origin,
Sec-Fetch-Site, Referer, doublons, cookies, sélecteur, query brute
2048, %HH, UTF-8, « + » / %2B, doublons après décodage, paramètres
inconnus, limit, query interdite sur le détail, chemin encodé, framing,
corps, déconnexion), ordre des contrôles instrumenté (aucune transaction
pour un refus de transport), résultats inattendus du runner (503),
en-têtes no-store sans Set-Cookie, méthodes refusées, composition de test.
"""
import json
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa

from core.auth_f2_http.provenance import validate_allowed_origins
from core.authz_f2 import commands as c
from core.authz_f2 import runner as runner_module
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, SESSION_INVALID, UNAVAILABLE, AuthorizationOutcome
from core.authz_f2_http import consultation as consultation_module
from core.authz_f2_http import consultation_codec as codec
from core.authz_f2_http.isolated import make_personal_app
from core.authz_f2_http.routes import PersonalDependencies
from tests.authz_f2_consultation_support import (
    OVERVIEW,
    THESES,
    cursor_of,
    decoded_cursor,
    get,
    insert_session,
    insert_thesis,
    raw_get,
    thesis_path,
    walk,
    write_state,
)
from tests.authz_f2_support import (
    BOB,
    USER_A,
    USER_B,
    AuthzEnv,
    active_cookie,
    compose_test_app,
    me_headers,
    me_request,
    personal_app,
)
from tests.f2_http_support import ORIGIN
from tests.f2_support import prepare_schema, scalar
from tests.test_authz_f2_http import (
    PROVENANCE_REFUSALS,
    assert_error,
    assert_private,
    browser_for,
    http_sign_in,
)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

ROUTES = [OVERVIEW, THESES, thesis_path(1)]
LISTS = [OVERVIEW, THESES]


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = AuthzEnv(pg_url)
    yield environment
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


@pytest.fixture
def app(env):
    return personal_app(env)


def assert_ok(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.content == codec.canonical_json(json.loads(response.content))
    assert_private(response)
    return response.json()


def assert_raw_private(response):
    for name, value in (("cache-control", "no-store"), ("pragma", "no-cache"), ("x-content-type-options", "nosniff"),
                        ("referrer-policy", "no-referrer")):
        assert response.get_list(name) == [value], name
    assert response.get_list("set-cookie") == [] and response.get_list("access-control-allow-origin") == []


def assert_raw_error(response, status, code):
    assert response.status_code == status, response.content
    assert response.json() == {"error": code}
    assert_raw_private(response)


# --------------------------------------------------------------------------
# Routes et vues
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,status", [
    ("POST", THESES, 405), ("PUT", THESES, 405), ("DELETE", THESES, 405), ("PATCH", THESES, 405),
    ("POST", thesis_path(1), 405), ("PUT", thesis_path(1), 405), ("DELETE", thesis_path(1), 405),
    ("PATCH", thesis_path(1), 405), ("POST", OVERVIEW, 405), ("DELETE", OVERVIEW, 405),
    ("GET", "/api/v2/me/analyses", 404), ("GET", thesis_path(1) + "/x", 404), ("GET", "/api/v2/me/analysis", 404),
    ("POST", "/api/v2/me/analysis-overview/abandon", 404),
])
def test_only_the_three_reads_exist(env, alice, app, method, path, status):
    response = me_request(app, method=method, path=path, headers=me_headers(alice.generation_id, active_cookie(alice)))
    assert response.status_code == status
    assert_private(response)
    assert env.hook_calls == []


def test_empty_collections(env, alice, app):
    assert get(app, alice, OVERVIEW).content == b'{"entries":[],"next_cursor":null}'
    assert get(app, alice, THESES).content == b'{"next_cursor":null,"theses":[]}'
    assert_ok(get(app, alice, OVERVIEW))


def test_overview_json_view(env, alice, app):
    session = insert_session(env, USER_A, "AIR.PA", current_step="moat", started_at="2026-10-28 14:00:00+02",
                             updated_at="2026-10-28 14:00:00.000001+02")
    thesis = insert_thesis(env, USER_A, "MC.PA", created_at="2026-10-28 12:00:00")
    insert_session(env, USER_A, "TTE.PA", current_step=None)
    insert_session(env, USER_A, "TTE.PA", current_step="moat", started_at="2020-01-01 00:00:00+00")
    body = assert_ok(get(app, alice, OVERVIEW))
    assert body["next_cursor"] is None
    assert body["entries"][0] == {"ticker": "AIR.PA", "state": "in_progress", "analysis_session_id": str(session),
                                  "current_step": "moat", "updated_at": "2026-10-28T12:00:00.000001Z",
                                  "latest_thesis_id": None, "diagnostics": []}
    assert body["entries"][1] == {"ticker": "MC.PA", "state": "thesis_available", "analysis_session_id": None,
                                  "current_step": None, "updated_at": "2026-10-28T12:00:00.000000Z",
                                  "latest_thesis_id": thesis, "diagnostics": []}
    assert body["entries"][2]["current_step"] is None
    assert body["entries"][2]["diagnostics"] == ["multiple_active_sessions", "invalid_current_step"]
    assert all(set(e) == {"ticker", "state", "analysis_session_id", "current_step", "updated_at",
                          "latest_thesis_id", "diagnostics"} for e in body["entries"])


def test_theses_json_view_and_detail(env, alice, app):
    session = insert_session(env, USER_A, "AIR.PA", status="completed", current_step="swot_final")
    verified = insert_thesis(env, USER_A, "AIR.PA", text="Texte « original »\n\t\"exact\" 😀",
                             created_at="2026-10-28 12:00:00", session_id=session)
    historical = insert_thesis(env, USER_A, "AIR.PA", text="ancienne", created_at=None)
    body = assert_ok(get(app, alice, THESES))
    assert body == {"next_cursor": None, "theses": [
        {"thesis_id": verified, "ticker": "AIR.PA", "created_at": "2026-10-28T12:00:00.000000Z",
         "provenance_status": "verified"},
        {"thesis_id": historical, "ticker": "AIR.PA", "created_at": None, "provenance_status": "unverified"}]}
    assert b"thesis_text" not in get(app, alice, THESES).content and b"original" not in get(app, alice, THESES).content
    detail = assert_ok(get(app, alice, thesis_path(verified)))
    assert detail == {"thesis_id": verified, "ticker": "AIR.PA", "thesis_text": "Texte « original »\n\t\"exact\" 😀",
                      "created_at": "2026-10-28T12:00:00.000000Z",
                      "provenance": {"status": "verified", "analysis_session_id": str(session)}}
    assert assert_ok(get(app, alice, thesis_path(historical)))["provenance"] == {
        "status": "unverified", "analysis_session_id": None}


def test_foreign_and_missing_theses_share_one_404(env, alice, app):
    env.confirmed(BOB, USER_B)
    foreign = insert_thesis(env, USER_B, "AIR.PA", text="secret de B")
    own = insert_thesis(env, USER_A, "AIR.PA")
    responses = [get(app, alice, thesis_path(t)) for t in (foreign, own + 1000, 2147483647)]
    for response in responses:
        assert_error(response, 404, "thesis_not_found")
        assert b"secret" not in response.content
    assert len({(r.content, tuple(sorted(r.headers.items()))) for r in responses}) == 1
    assert get(app, alice, thesis_path(own)).status_code == 200


def test_provenance_of_another_user_is_never_rendered(env, alice, app):
    env.confirmed(BOB, USER_B)
    foreign = insert_session(env, USER_B, "AIR.PA")
    other = insert_session(env, USER_A, "MC.PA")
    first = insert_thesis(env, USER_A, "AIR.PA", session_id=foreign)
    second = insert_thesis(env, USER_A, "AIR.PA", session_id=other)
    for thesis in (first, second):
        response = get(app, alice, thesis_path(thesis))
        assert assert_ok(response)["provenance"] == {"status": "unverified", "analysis_session_id": None}
        assert str(foreign).encode() not in response.content and str(other).encode() not in response.content
    listed = get(app, alice, THESES)
    assert str(foreign).encode() not in listed.content and str(other).encode() not in listed.content


def test_reads_write_nothing_and_set_no_cookie(env, alice, app):
    insert_session(env, USER_A, "AIR.PA", current_step="inconnue")
    insert_thesis(env, USER_A, "MC.PA")
    before = write_state(env)
    for path in ROUTES + [OVERVIEW + "?limit=1"]:
        response = get(app, alice, path)
        assert response.headers.get_list("set-cookie") == []
    assert write_state(env) == before


def test_logs_carry_no_ticker_text_cursor_or_identifier(env, alice, app, caplog):
    caplog.set_level("DEBUG")
    env.confirmed(BOB, USER_B)
    secret_ticker, secret_text = "TICKER-SECRET-9f3", "TEXTE-PATRIMONIAL-SECRET"
    session = insert_session(env, USER_A, secret_ticker)
    own = insert_thesis(env, USER_A, secret_ticker, text=secret_text, session_id=session)
    foreign = insert_thesis(env, USER_B, secret_ticker, text=secret_text)
    insert_thesis(env, USER_A, "Z" * 600, created_at="2000-01-01 00:00:00")  # 503 : alerte sans détail
    first = get(app, alice, THESES, query="limit=1").json()
    for path, query in ((OVERVIEW, "limit=1"), (THESES, f"limit=1&cursor={first['next_cursor']}"),
                        (THESES, f"ticker={secret_ticker}"), (thesis_path(own), None), (thesis_path(foreign), None),
                        (THESES, "cursor=" + "A" * 50), (THESES, None), (OVERVIEW, None)):
        get(app, alice, path, query=query)
    # Journaux applicatifs (le client de test httpx journalise ses propres URL).
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("oryx"))
    assert "status=" in logged and "alert=consultation_value_unrepresentable" in logged
    for forbidden in (secret_ticker, secret_text, first["next_cursor"], str(session), f"/{own}", f"/{foreign}",
                      alice.active_secret.reveal(), str(alice.generation_id), USER_A, USER_B, "Z" * 50, "SELECT"):
        assert forbidden not in logged, forbidden


# --------------------------------------------------------------------------
# Pagination (données immobiles)
# --------------------------------------------------------------------------

def _seed_theses(env, user_id=USER_A):
    """37 thèses : dates égales, nulles, anciennes ; deux tickers."""
    created = (["2026-01-01 00:00:00"] * 9 + [None] * 8 + ["1900-01-01 00:00:00", "0001-01-01 00:00:00"]
               + [f"2026-0{m}-15 10:00:00.00000{m}" for m in range(1, 10)] + ["2026-01-01 00:00:00"] * 5
               + [None] * 4)
    ids = []
    for index, date in enumerate(created):
        ids.append((insert_thesis(env, user_id, "AIR.PA" if index % 3 else "MC.PA", created_at=date), date, index))
    return ids


def _expected(ids, ticker=None):
    """Ordre attendu calculé indépendamment : created_at DESC NULLS LAST, id DESC."""
    selected = [(i, d) for i, d, index in ids if ticker is None or ("AIR.PA" if index % 3 else "MC.PA") == ticker]
    dated = sorted(((datetime.fromisoformat(d), i) for i, d in selected if d is not None), reverse=True)
    nulls = sorted((i for i, d in selected if d is None), reverse=True)
    return [i for _, i in dated] + nulls


@pytest.mark.parametrize("limit", [1, 2, 7, 25, 36, 37, 50])
def test_paginated_walk_returns_every_thesis_exactly_once(env, alice, app, limit):
    ids = _seed_theses(env)
    pages = walk(app, alice, THESES, limit=limit)
    seen = [t["thesis_id"] for page in pages for t in page["theses"]]
    assert seen == _expected(ids)  # ordre total, aucune répétition ni omission
    assert all(len(page["theses"]) == limit for page in pages[:-1])
    assert 1 <= len(pages[-1]["theses"]) <= limit and pages[-1]["next_cursor"] is None
    for page in pages[:-1]:  # curseur construit depuis la DERNIÈRE entrée rendue
        last = page["theses"][-1]
        assert decoded_cursor(page["next_cursor"]) == {"last_created_at": last["created_at"],
                                                       "last_thesis_id": last["thesis_id"], "route": "theses",
                                                       "ticker": None, "v": 1}


@pytest.mark.parametrize("limit", [1, 4, 50])
def test_filtered_walk_keeps_its_filter(env, alice, app, limit):
    ids = _seed_theses(env)
    insert_thesis(env, USER_B, "MC.PA")
    pages = walk(app, alice, THESES, limit=limit, extra="&ticker=MC.PA")
    seen = [t["thesis_id"] for page in pages for t in page["theses"]]
    assert seen == _expected(ids, "MC.PA")
    assert all(decoded_cursor(p["next_cursor"])["ticker"] == "MC.PA" for p in pages[:-1])
    if len(pages) > 1:  # changement de filtre avec le curseur : refus
        cursor = pages[0]["next_cursor"]
        assert_error(get(app, alice, THESES, query=f"cursor={cursor}"), 400, "invalid_request")
        assert_error(get(app, alice, THESES, query=f"ticker=AIR.PA&cursor={cursor}"), 400, "invalid_request")


def test_default_limit_and_exact_limit_pages(env, alice, app):
    for _ in range(25):
        insert_thesis(env, USER_A, "AIR.PA")
    body = assert_ok(get(app, alice, THESES))
    assert len(body["theses"]) == 25 and body["next_cursor"] is None  # exactement limit : dernière page
    insert_thesis(env, USER_A, "AIR.PA")
    body = assert_ok(get(app, alice, THESES))
    assert len(body["theses"]) == 25 and body["next_cursor"] is not None
    last = assert_ok(get(app, alice, THESES, query=f"cursor={body['next_cursor']}"))
    assert len(last["theses"]) == 1 and last["next_cursor"] is None
    for limit in (1, 25, 50):
        assert len(assert_ok(get(app, alice, THESES, query=f"limit={limit}"))["theses"]) == min(limit, 26)


def test_cursor_after_the_last_row_yields_an_empty_page(env, alice, app):
    first = insert_thesis(env, USER_A, "AIR.PA", created_at=None)
    cursor = cursor_of({"last_created_at": None, "last_thesis_id": first, "route": "theses", "ticker": None, "v": 1})
    assert get(app, alice, THESES, query=f"cursor={cursor}").content == b'{"next_cursor":null,"theses":[]}'
    cursor = cursor_of({"last_ticker": "\U0010ffff", "route": "analysis-overview", "v": 1})
    insert_session(env, USER_A, "Z")
    assert get(app, alice, OVERVIEW, query=f"cursor={cursor}").content == b'{"entries":[],"next_cursor":null}'


TICKERS = ["", "AIR.PA", "Air.pa", "B", "Z", "_x", "a", "air.pa", "É", "😀", "A&B=C", "A+B", "A B", '"q"']


@pytest.mark.parametrize("limit", [1, 3, 50])
def test_overview_walk_returns_every_ticker_once_in_collation_c(env, alice, app, limit):
    for index, ticker in enumerate(TICKERS):
        if index % 2:
            insert_session(env, USER_A, ticker)
        else:
            insert_thesis(env, USER_A, ticker)
    insert_session(env, USER_A, "AIR.PA")  # session ET thèse : une seule entrée
    pages = walk(app, alice, OVERVIEW, limit=limit)
    seen = [e["ticker"] for page in pages for e in page["entries"]]
    assert seen == sorted(TICKERS)
    for page in pages[:-1]:
        assert decoded_cursor(page["next_cursor"]) == {"last_ticker": page["entries"][-1]["ticker"],
                                                       "route": "analysis-overview", "v": 1}


def test_historical_empty_ticker_cursor(env, alice, app):
    insert_thesis(env, USER_A, "")
    insert_thesis(env, USER_A, "A")
    first = assert_ok(get(app, alice, OVERVIEW, query="limit=1"))
    assert first["entries"][0]["ticker"] == "" and decoded_cursor(first["next_cursor"])["last_ticker"] == ""
    second = assert_ok(get(app, alice, OVERVIEW, query=f"limit=1&cursor={first['next_cursor']}"))
    assert [e["ticker"] for e in second["entries"]] == ["A"] and second["next_cursor"] is None


# --------------------------------------------------------------------------
# Mutations historiques entre deux pages (aucun snapshot inter-pages)
# --------------------------------------------------------------------------

def test_insertion_and_deletion_between_two_pages(env, alice, app):
    a = insert_thesis(env, USER_A, "AIR.PA", created_at="2026-01-03 00:00:00")
    b = insert_thesis(env, USER_A, "AIR.PA", created_at="2026-01-02 00:00:00")
    d = insert_thesis(env, USER_A, "AIR.PA", created_at="2026-01-01 00:00:00")
    e = insert_thesis(env, USER_A, "AIR.PA", created_at=None)
    first = assert_ok(get(app, alice, THESES, query="limit=2"))
    assert [t["thesis_id"] for t in first["theses"]] == [a, b]
    # Insertion AVANT la position du curseur (plus récente) : jamais vue par
    # les pages suivantes ; APRÈS : vue. Suppression d'une ligne non encore
    # rendue : omise sans erreur ; suppression de la ligne du curseur : le
    # keyset continue (il ne relit pas la ligne).
    newer = insert_thesis(env, USER_A, "AIR.PA", created_at="2027-01-01 00:00:00")
    older = insert_thesis(env, USER_A, "AIR.PA", created_at="2025-01-01 00:00:00")
    with env.observer.begin() as conn:
        conn.execute(sa.text("DELETE FROM investment_theses WHERE id IN (:b, :d)"), {"b": b, "d": d})
    second = assert_ok(get(app, alice, THESES, query=f"limit=2&cursor={first['next_cursor']}"))
    assert [t["thesis_id"] for t in second["theses"]] == [older, e] and second["next_cursor"] is None
    assert newer not in [t["thesis_id"] for t in second["theses"]]


def test_analysis_state_change_between_two_pages(env, alice, app):
    insert_thesis(env, USER_A, "A")
    active = insert_session(env, USER_A, "B", current_step="risques")
    insert_session(env, USER_A, "C")
    first = assert_ok(get(app, alice, OVERVIEW, query="limit=1"))
    assert [e["ticker"] for e in first["entries"]] == ["A"]
    with env.observer.begin() as conn:  # B termine (thèse), C abandonnée sans thèse
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'completed', current_step = 'swot_final' "
                             "WHERE id = :s"), {"s": active})
        conn.execute(sa.text("INSERT INTO investment_theses (user_id, ticker, thesis_text, analysis_session_id) "
                             "VALUES (:u, 'B', 'fin', :s)"), {"u": USER_A, "s": active})
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE ticker = 'C'"))
    second = assert_ok(get(app, alice, OVERVIEW, query=f"limit=5&cursor={first['next_cursor']}"))
    assert [(e["ticker"], e["state"]) for e in second["entries"]] == [("B", "thesis_available")]


# --------------------------------------------------------------------------
# Bornes de restitution
# --------------------------------------------------------------------------

def _detail_overhead(env, alice, app):
    thesis = insert_thesis(env, USER_A, "A", text="", created_at=None)
    size = len(get(app, alice, thesis_path(thesis)).content)
    return thesis, size


def test_detail_response_of_exactly_131072_bytes(env, alice, app):
    thesis, overhead = _detail_overhead(env, alice, app)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE investment_theses SET thesis_text = :x WHERE id = :i"),
                     {"x": "x" * (131072 - overhead), "i": thesis})
    response = get(app, alice, thesis_path(thesis))
    assert len(response.content) == 131072
    assert len(assert_ok(response)["thesis_text"]) == 131072 - overhead  # intégral, jamais tronqué
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE investment_theses SET thesis_text = :x WHERE id = :i"),
                     {"x": "x" * (131072 - overhead + 1), "i": thesis})
    assert_error(get(app, alice, thesis_path(thesis)), 503, "temporarily_unavailable")
    assert scalar(env.observer, "SELECT octet_length(thesis_text) FROM investment_theses WHERE id = :i",
                  i=thesis) == 131072 - overhead + 1  # donnée intacte


def test_escaping_counts_toward_the_bound(env, alice, app):
    thesis = insert_thesis(env, USER_A, "A", text='"\\' * 40000)  # 80 000 octets bruts, 160 000 échappés
    assert_error(get(app, alice, thesis_path(thesis)), 503, "temporarily_unavailable")
    small = insert_thesis(env, USER_A, "A", text='"\\' * 30000)
    assert assert_ok(get(app, alice, thesis_path(small)))["thesis_text"] == '"\\' * 30000


def test_list_response_over_131072_bytes_is_unavailable(env, alice, app):
    for index in range(50):  # 50 tickers de 512 octets de contrôle : 3 072 octets JSON chacun
        insert_thesis(env, USER_A, chr(1) * 511 + chr(65 + index % 26) if index < 26 else chr(1) * 510 + "Z" +
                      chr(65 + index % 26))
    assert_error(get(app, alice, THESES, query="limit=50"), 503, "temporarily_unavailable")
    assert_error(get(app, alice, OVERVIEW, query="limit=50"), 503, "temporarily_unavailable")
    assert len(assert_ok(get(app, alice, THESES, query="limit=10"))["theses"]) == 10


@pytest.mark.parametrize("ticker,status", [("A" * 512, 200), ("A" * 513, 503), ("é" * 256, 200),
                                           ("é" * 256 + "x", 503)])
def test_ticker_bound_over_http(env, alice, app, ticker, status):
    thesis = insert_thesis(env, USER_A, ticker)
    for path in (OVERVIEW, THESES, thesis_path(thesis)):
        response = get(app, alice, path)
        assert response.status_code == status, path
        if status == 200:
            assert ticker in response.content.decode("utf-8")
        else:
            assert_error(response, 503, "temporarily_unavailable")


def test_next_cursor_too_long_after_serialization_fails_the_page(env, alice, app):
    insert_thesis(env, USER_A, '"' * 512)  # 512 octets, 1 024 échappés : curseur > 1024 caractères
    insert_thesis(env, USER_A, "~")
    assert_error(get(app, alice, OVERVIEW, query="limit=1"), 503, "temporarily_unavailable")
    final = assert_ok(get(app, alice, OVERVIEW, query="limit=2"))  # dernière page : aucun curseur à produire
    assert [e["ticker"] for e in final["entries"]] == ['"' * 512, "~"] and final["next_cursor"] is None


def test_unrepresentable_dates_over_http(env, alice, app):
    thesis = insert_thesis(env, USER_A, "A", created_at="infinity")
    for path in (OVERVIEW, THESES, thesis_path(thesis)):
        assert_error(get(app, alice, path), 503, "temporarily_unavailable")


# --------------------------------------------------------------------------
# Transport : provenance, cookies, sélecteur
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("case", sorted(PROVENANCE_REFUSALS))
def test_provenance_refusals(env, alice, app, path, case):
    headers = me_headers(alice.generation_id, active_cookie(alice), **PROVENANCE_REFUSALS[case])
    assert_error(get(app, alice, path, headers=headers), 403, "origin_refused")
    assert env.hook_calls == []


@pytest.mark.parametrize("path", LISTS + ["detail"])
def test_accepted_provenances(env, alice, app, path):
    thesis = insert_thesis(env, USER_A, "A")
    path = thesis_path(thesis) if path == "detail" else path
    for headers in (me_headers(alice.generation_id, active_cookie(alice)),
                    me_headers(alice.generation_id, active_cookie(alice), origin=ORIGIN, fetch_site=None),
                    me_headers(alice.generation_id, active_cookie(alice), extra={"referer": ORIGIN + "/web-v2"})):
        assert get(app, alice, path, headers=headers).status_code == 200


@pytest.mark.parametrize("name,value", [("origin", ORIGIN), ("sec-fetch-site", "same-origin"),
                                        ("referer", ORIGIN + "/"), ("host", "oryx.test")])
def test_duplicated_provenance_headers_are_refused(env, alice, app, name, value):
    headers = list(me_headers(alice.generation_id, active_cookie(alice), origin=ORIGIN).items()) + [(name, value)] * 2
    for path in ROUTES:
        assert_error(me_request(app, path=path, headers=headers), 403, "origin_refused")


@pytest.mark.parametrize("cookie", [
    "__Host-oryx-active-{g}={s}; __Host-oryx-active-{g}={s}",  # nom dupliqué
    "__Host-oryx-active-{g}=pas-un-secret",
    "__Host-oryx-active-{G}={s}",  # UUID non canonique
    "__Host-oryx-inconnu={s}",
    "__Host-oryx-active-x=y",
])
def test_malformed_cookie_header_is_invalid_request(env, alice, app, cookie):
    value = cookie.format(g=alice.generation_id, G=str(alice.generation_id).upper(), s=alice.active_secret.reveal())
    for path in ROUTES:
        assert_error(get(app, alice, path, headers=me_headers(alice.generation_id, value)), 400, "invalid_request")
    headers = list(me_headers(alice.generation_id).items()) + [("cookie", active_cookie(alice)), ("cookie", "a=b")]
    for path in ROUTES:
        assert_error(me_request(app, path=path, headers=headers), 400, "invalid_request")  # deux en-têtes Cookie
    assert env.hook_calls == []


@pytest.mark.parametrize("selector", [None, "abc", str(uuid.uuid4()).upper(), "{" + str(uuid.uuid4()) + "}"])
def test_selector_must_be_one_canonical_uuid(env, alice, app, selector):
    for path in ROUTES:
        assert_error(get(app, alice, path, headers=me_headers(selector, active_cookie(alice))), 400, "invalid_request")
    headers = list(me_headers(alice.generation_id, active_cookie(alice)).items()) + [
        ("x-oryx-generation-id", str(alice.generation_id))]
    assert_error(me_request(app, path=THESES, headers=headers), 400, "invalid_request")


def test_designated_cookie_only_never_another_active_one(env, alice, app):
    other = env.confirmed("alice2@example.com", USER_A)
    thesis = insert_thesis(env, USER_A, "A")
    cookie = active_cookie(other)  # seul le cookie d'UNE AUTRE génération active
    for path in LISTS + [thesis_path(thesis)]:
        assert_error(get(app, alice, path, headers=me_headers(alice.generation_id, cookie)), 401, "session_invalid")
        both = me_headers(alice.generation_id, f"{active_cookie(other)}; {active_cookie(alice)}")
        assert get(app, alice, path, headers=both).status_code == 200
    assert env.hook_calls.count("prevalidated") == 3


def test_inadmissible_sessions_share_one_401(env, app):
    pending = env.sign_in()
    revoked = env.confirmed("alice2@example.com", USER_A)
    env.service.revoke_session(revoked.session_id, "logout")
    expired = env.confirmed("alice3@example.com", USER_A)
    env.expire_at(expired.session_id, "clock_timestamp()")
    responses = [get(app, issued, path) for issued in (pending, revoked, expired) for path in ROUTES]
    for response in responses:
        assert_error(response, 401, "session_invalid")
    assert len({(r.content, tuple(sorted(r.headers.items()))) for r in responses}) == 1
    assert env.generation(pending.generation_id)["delivery_status"] == "pending"


# --------------------------------------------------------------------------
# Transport : query stricte
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", LISTS)
@pytest.mark.parametrize("query", [
    b"limit=0", b"limit=51", b"limit=05", b"limit=+5", b"limit=%2B5", b"limit=5%20", b"limit=%D9%A5", b"limit=",
    b"limit=1&limit=1", b"limit=1&l%69mit=1", b"user_id=user-b", b"foo", b"=1", b"limit=1&", b"limit=%",
    b"limit=%zz", b"cursor=%FF", b"cursor=", b"cursor=abc", b"x=%C3%28", b"limit=\xd9\xa5",
])
def test_invalid_queries_are_400_without_transaction(env, alice, app, path, query):
    assert_raw_error(raw_get(app, alice, path, query), 400, "invalid_request")
    assert env.hook_calls == []


def test_raw_query_bound_2048(env, alice, app):
    insert_thesis(env, USER_A, "A" * 32)
    exact = b"ticker=" + b"A" * 32 + b"&limit=5&" + b"cursor=" + b"A" * (2048 - 7 - 32 - 9 - 7)
    assert len(exact) == 2048
    assert_raw_error(raw_get(app, alice, THESES, exact), 400, "invalid_request")  # curseur invalide, borne tenue
    assert_raw_error(raw_get(app, alice, THESES, exact + b"A"), 400, "invalid_request")
    padded = b"ticker=" + b"%41" * 32 + b"&limit=%35"
    assert len(assert_ok_raw(raw_get(app, alice, THESES, padded))["theses"]) == 1


def assert_ok_raw(response):
    assert response.status_code == 200, response.content
    assert_raw_private(response)
    return response.json()


def test_plus_and_percent_2b_filters(env, alice, app):
    plus = insert_thesis(env, USER_A, "A+B")
    space = insert_thesis(env, USER_A, "A B")
    assert [t["thesis_id"] for t in assert_ok_raw(raw_get(app, alice, THESES, b"ticker=A%2BB"))["theses"]] == [plus]
    assert [t["thesis_id"] for t in assert_ok_raw(raw_get(app, alice, THESES, b"ticker=A+B"))["theses"]] == [space]
    assert [t["thesis_id"] for t in assert_ok_raw(raw_get(app, alice, THESES, b"ticker=A%20B"))["theses"]] == [space]


def test_ticker_filter_is_unknown_on_the_overview_and_exact_on_theses(env, alice, app):
    insert_thesis(env, USER_A, "AIR.PA")
    assert_raw_error(raw_get(app, alice, OVERVIEW, b"ticker=AIR.PA"), 400, "invalid_request")
    for query in (b"ticker=", b"ticker=%20AIR.PA", b"ticker=AIR.PA%20", b"ticker=" + b"A" * 33, b"ticker=%00"):
        assert_raw_error(raw_get(app, alice, THESES, query), 400, "invalid_request")
    assert assert_ok_raw(raw_get(app, alice, THESES, b"ticker=air.pa"))["theses"] == []  # aucune correction


def test_cursor_of_another_route_or_version_is_refused(env, alice, app):
    overview_cursor = cursor_of({"last_ticker": "A", "route": "analysis-overview", "v": 1})
    theses_cursor = cursor_of({"last_created_at": None, "last_thesis_id": 1, "route": "theses", "ticker": None, "v": 1})
    for path, cursor in ((THESES, overview_cursor), (OVERVIEW, theses_cursor),
                         (OVERVIEW, cursor_of({"last_ticker": "A", "route": "analysis-overview", "v": 2})),
                         (OVERVIEW, overview_cursor + "=")):
        assert_raw_error(raw_get(app, alice, path, b"cursor=" + cursor.encode()), 400, "invalid_request")
    assert env.hook_calls == []


def test_cursor_carries_no_right(env, alice, app):
    """Un curseur forgé portant un user_id ou un identifiant de thèse
    d'autrui ne donne accès à rien : champ inconnu refusé, keyset filtré
    par le principal."""
    env.confirmed(BOB, USER_B)
    bob = insert_thesis(env, USER_B, "AIR.PA", created_at=None)
    forged = cursor_of({"last_created_at": None, "last_thesis_id": 1, "route": "theses", "ticker": None, "v": 1,
                        "user_id": USER_B})
    assert_raw_error(raw_get(app, alice, THESES, b"cursor=" + forged.encode()), 400, "invalid_request")
    cursor = cursor_of({"last_created_at": None, "last_thesis_id": bob + 1, "route": "theses", "ticker": None, "v": 1})
    assert assert_ok_raw(raw_get(app, alice, THESES, b"cursor=" + cursor.encode()))["theses"] == []


@pytest.mark.parametrize("query", [b"a", b"limit=1", b"x=1", b"=", b"&"])
def test_detail_refuses_any_query(env, alice, app, query):
    thesis = insert_thesis(env, USER_A, "A")
    assert_raw_error(raw_get(app, alice, thesis_path(thesis), query), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("raw", ["0", "00", "01", "-1", "+1", "1.0", "1e3", "2147483648", "99999999999", "0x1"])
def test_invalid_thesis_ids_are_400(env, alice, app, raw):
    assert_raw_error(raw_get(app, alice, f"{THESES}/{raw}"), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("encoded", ["%31", "1%30", "%D9%A1", "%EF%BC%91"])
def test_percent_encoded_thesis_id_in_the_raw_path_is_refused(env, alice, app, encoded):
    thesis = insert_thesis(env, USER_A, "A")
    response = me_request(app, path=f"{THESES}/{encoded}", headers=me_headers(alice.generation_id, active_cookie(alice)))
    assert_error(response, 400, "invalid_request")
    assert env.hook_calls == [] and thesis


def test_maximum_thesis_id_is_accepted(env, alice, app):
    assert_error(get(app, alice, thesis_path(2147483647)), 404, "thesis_not_found")


# --------------------------------------------------------------------------
# Transport : framing et corps (règles de PR 7)
# --------------------------------------------------------------------------

def _raw_headers(alice, *extra):
    return [("host", "oryx.test")] + list(me_headers(alice.generation_id, active_cookie(alice)).items()) + list(extra)


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("extra,chunks,status", [
    ([("transfer-encoding", "chunked")], (), 400),
    ([("transfer-encoding", "identity")], (), 400),
    ([("content-length", "0"), ("content-length", "0")], (), 400),
    ([("content-length", "abc")], (), 400),
    ([("content-length", "-1")], (), 400),
    ([("content-length", " 1")], (), 400),
    ([("content-length", "4097")], (), 413),
    ([("content-length", "99999999999999999999")], (), 413),
    ([("content-length", "1")], (), 400),
    ([("content-length", "4096")], (), 400),
    ([], (b"x",), 400),
    ([], (b"x" * 4096,), 400),
    ([], (b"x" * 4097,), 413),
    ([], (b"x" * 3000, b"y" * 2000), 413),
    ([("content-type", "application/json")], (b"{}",), 400),
])
def test_get_bodies_and_framing_are_refused(env, alice, app, path, extra, chunks, status):
    response = raw_get(app, alice, path, headers=_raw_headers(alice, *extra), chunks=chunks)
    assert_raw_error(response, status, "invalid_request" if status == 400 else "request_too_large")
    assert env.hook_calls == []


@pytest.mark.parametrize("path", ROUTES)
def test_disconnect_is_invalid_request(env, alice, app, path):
    response = raw_get(app, alice, path, headers=_raw_headers(alice), chunks=(b"x",), disconnect=True)
    assert_raw_error(response, 400, "invalid_request")
    response = raw_get(app, alice, path, headers=_raw_headers(alice), disconnect=True)
    assert_raw_error(response, 400, "invalid_request")
    assert env.hook_calls == []


def test_content_length_zero_without_body_is_accepted(env, alice, app):
    insert_thesis(env, USER_A, "A")
    assert assert_ok_raw(raw_get(app, alice, THESES, headers=_raw_headers(alice, ("content-length", "0"))))


# --------------------------------------------------------------------------
# Ordre des contrôles (instrumenté)
# --------------------------------------------------------------------------

@pytest.fixture
def trace(monkeypatch):
    calls = []

    def record(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapper

    def record_async(name, fn):
        async def wrapper(*args, **kwargs):
            calls.append(name)
            return await fn(*args, **kwargs)
        return wrapper

    m = consultation_module
    monkeypatch.setattr(m, "personal_get_origin", record("provenance", m.personal_get_origin))
    monkeypatch.setattr(m, "parse_cookie_headers", record("cookies", m.parse_cookie_headers))
    monkeypatch.setattr(m, "_generation_id", record("selector", m._generation_id))
    monkeypatch.setattr(m, "_raw_query", record("raw_query", m._raw_query))
    for name in ("_overview_command", "_theses_command", "_thesis_command"):
        monkeypatch.setattr(m, name, record("parameters", getattr(m, name)))
    monkeypatch.setattr(codec, "parse_thesis_id", record("thesis_id", codec.parse_thesis_id))
    monkeypatch.setattr(m, "_refuse_body", record_async("body", m._refuse_body))
    monkeypatch.setattr(runner_module, "transaction", record("transaction", runner_module.transaction))
    return calls


BIG = [b"x" * 5000]


@pytest.mark.parametrize("path", ROUTES)
def test_check_priorities(env, alice, app, trace, path):
    good = active_cookie(alice)
    gen = alice.generation_id
    detail = path != OVERVIEW and path != THESES
    bad_param = b"" if detail else b"limit=0"
    cases = [
        ("provenance", _hdr(None, good, fetch_site="cross-site"), b"limit=0", BIG, 403, "origin_refused",
         ["provenance"]),
        ("cookie", _hdr(None, "__Host-oryx-active-x=y"), b"limit=0", BIG, 400, "invalid_request",
         ["provenance", "cookies"]),
        ("selecteur", _hdr(None, good), b"x" * 3000, BIG, 400, "invalid_request", ["provenance", "cookies", "selector"]),
        ("query_brute", _hdr(gen, good), b"x" * 2049, BIG, 400, "invalid_request",
         ["provenance", "cookies", "selector", "raw_query"]),
        ("parametres", _hdr(gen, good), b"limit=0" if not detail else b"q=1", BIG, 400, "invalid_request",
         ["provenance", "cookies", "selector", "raw_query", "parameters"]),
        ("corps", _hdr(gen, good), bad_param if detail else b"limit=5", BIG, 413, "request_too_large",
         ["provenance", "cookies", "selector", "raw_query", "parameters"] + (["thesis_id"] if detail else []) + ["body"]),
        ("cookie_absent", _hdr(uuid.uuid4(), good), b"", (), 401, "session_invalid",
         ["provenance", "cookies", "selector", "raw_query", "parameters"] + (["thesis_id"] if detail else []) + ["body"]),
    ]
    for name, headers, query, chunks, status, code, executed in cases:
        trace.clear()
        assert_raw_error(raw_get(app, alice, path, query, headers=headers, chunks=chunks), status, code)
        assert trace == executed, name  # aucune transaction PostgreSQL pour un refus HTTP
    assert env.hook_calls == []
    trace.clear()
    response = raw_get(app, alice, path, headers=_hdr(gen, good))
    assert response.status_code in (200, 404)
    assert trace == ["provenance", "cookies", "selector", "raw_query", "parameters"] + (
        ["thesis_id"] if detail else []) + ["body", "transaction", "transaction"]  # phases A et B


def test_detail_query_is_refused_before_the_identifier(env, alice, app, trace):
    trace.clear()
    assert_raw_error(raw_get(app, alice, f"{THESES}/0", b"x=1"), 400, "invalid_request")
    assert "thesis_id" not in trace
    trace.clear()
    assert_raw_error(raw_get(app, alice, f"{THESES}/0"), 400, "invalid_request")
    assert trace[-1] == "thesis_id"


def _hdr(generation_id, cookie, **kwargs):
    return [("host", "oryx.test")] + list(me_headers(generation_id, cookie, **kwargs).items())


def test_unconfigured_runner_is_503_after_transport_checks(env, alice):
    app = make_personal_app(PersonalDependencies(None, validate_allowed_origins([ORIGIN])))
    for path in ROUTES:
        assert_error(get(app, alice, path), 503, "temporarily_unavailable")
    assert_raw_error(raw_get(app, alice, OVERVIEW, b"limit=0"), 400, "invalid_request")


# --------------------------------------------------------------------------
# Résultats inattendus du runner : jamais de 200 non validé
# --------------------------------------------------------------------------

class FakeRunner:
    def __init__(self, outcome=None, error=None):
        self.outcome, self.error, self.commands = outcome, error, []

    def run(self, credential, command):
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.outcome(command) if callable(self.outcome) else self.outcome


def _principal(generation_id):
    return AuthorizedPrincipal(USER_A, uuid.uuid4(), generation_id, "supabase_email_sign_in")


def _fake_app(runner):
    return make_personal_app(PersonalDependencies(runner, validate_allowed_origins([ORIGIN])))


ENTRY = c.AnalysisOverviewEntry("A", c.THESIS_AVAILABLE, None, None, None, 1, ())
LISTED = c.ThesisListEntry(1, "A", None, c.UNVERIFIED)
DETAIL = c.ThesisDetailView(1, "A", "t", None, None)


def test_unexpected_runner_results_are_503(env, alice):
    gen = alice.generation_id
    ok = {OVERVIEW: c.AnalysisOverviewPage((ENTRY,), False), THESES: c.ThesesPage((LISTED,), False),
          thesis_path(1): DETAIL}
    for path, good in ok.items():
        response = get(_fake_app(FakeRunner(AuthorizationOutcome(AUTHORIZED, good, _principal(gen)))), alice, path)
        assert response.status_code == 200, path
        bad_outcomes = [
            AuthorizationOutcome(AUTHORIZED, good, None),
            AuthorizationOutcome(AUTHORIZED, good, _principal(uuid.uuid4())),         # autre génération
            AuthorizationOutcome(AUTHORIZED, None, _principal(gen)),
            AuthorizationOutcome(AUTHORIZED, "page", _principal(gen)),
            AuthorizationOutcome(AUTHORIZED, ok[THESES] if path != THESES else ok[OVERVIEW], _principal(gen)),
            AuthorizationOutcome(UNAVAILABLE),
            AuthorizationOutcome("autre"),
            ("authorized", good),
            None,
        ]
        if path != thesis_path(1):
            bad_outcomes.append(AuthorizationOutcome(NOT_FOUND))  # aucune ressource individuelle sur une liste
        for outcome in bad_outcomes:
            assert_error(get(_fake_app(FakeRunner(outcome)), alice, path), 503, "temporarily_unavailable")
        assert_error(get(_fake_app(FakeRunner(error=RuntimeError("panne"))), alice, path), 503,
                     "temporarily_unavailable")
        assert_error(get(_fake_app(FakeRunner(AuthorizationOutcome(SESSION_INVALID))), alice, path), 401,
                     "session_invalid")


def test_incoherent_pages_from_the_runner_are_503(env, alice):
    gen = alice.generation_id

    def page_app(value):
        return _fake_app(FakeRunner(AuthorizationOutcome(AUTHORIZED, value, _principal(gen))))

    two = c.AnalysisOverviewPage((ENTRY, c.AnalysisOverviewEntry("B", c.THESIS_AVAILABLE, None, None, None, 2, ())),
                                 False)
    assert_error(get(page_app(two), alice, OVERVIEW, query="limit=1"), 503, "temporarily_unavailable")  # > limit
    assert_error(get(page_app(c.AnalysisOverviewPage((ENTRY,), True)), alice, OVERVIEW, query="limit=2"), 503,
                 "temporarily_unavailable")  # has_more sans page pleine
    cursor = cursor_of({"last_ticker": "B", "route": "analysis-overview", "v": 1})
    assert_error(get(page_app(c.AnalysisOverviewPage((ENTRY,), False)), alice, OVERVIEW, query=f"cursor={cursor}"),
                 503, "temporarily_unavailable")  # antérieure au curseur
    assert_error(get(page_app(c.ThesesPage((LISTED,), False)), alice, THESES, query="ticker=B"), 503,
                 "temporarily_unavailable")  # hors filtre
    assert_error(get(page_app(c.ThesisDetailView(2, "A", "t", None, None)), alice, thesis_path(1)), 503,
                 "temporarily_unavailable")  # autre thèse
    assert_error(get(_fake_app(FakeRunner(AuthorizationOutcome(NOT_FOUND))), alice, thesis_path(1)), 404,
                 "thesis_not_found")


def test_runner_receives_only_typed_commands_without_owner(env, alice):
    runner = FakeRunner(AuthorizationOutcome(SESSION_INVALID))
    app = _fake_app(runner)
    get(app, alice, OVERVIEW, query="limit=3")
    get(app, alice, THESES, query="limit=4&ticker=AIR.PA")
    get(app, alice, thesis_path(42))
    assert runner.commands == [c.ListAnalysisOverviewPage(3, None), c.ListThesesPage(4, "AIR.PA", None, None),
                               c.ReadThesis(42)]


# --------------------------------------------------------------------------
# Composition de test : connexion F2 réelle puis lectures
# --------------------------------------------------------------------------

def test_sign_in_resolve_then_read_in_the_composition(env):
    app = compose_test_app(env)
    browser = browser_for(app)
    generation_id = http_sign_in(env, browser)
    assert browser.resolve(generation_id).status_code == 200
    thesis = insert_thesis(env, USER_A, "AIR.PA", text="composée")
    headers = me_headers(generation_id, browser.cookie_header())
    for path in ROUTES[:2] + [thesis_path(thesis)]:
        response = me_request(app, path=path, headers=headers)
        assert_ok(response)
    assert me_request(app, path=thesis_path(thesis), headers=headers).json()["thesis_text"] == "composée"
    assert browser.logout(generation_id).status_code == 200
    for path in ROUTES:
        assert_error(me_request(app, path=path, headers=me_headers(generation_id, browser.cookie_header())), 401,
                     "session_invalid")
