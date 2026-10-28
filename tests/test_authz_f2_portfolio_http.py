"""Routes /api/v2/me/portfolio (R1 PR 7) sur l'application personnelle isolée.

Vrai AuthorizedTransactionRunner, sessions du vrai moteur F2, trousseau
portefeuille de test (clés fictives). Couvre : cinq routes, vues JSON
exactes, 201 + Location, 200, DELETE sans corps, rejeu octet pour octet,
conflits fermés, identifiants canoniques (position_id, X-Oryx-Operation-Id,
X-Oryx-Expected-Revision), provenance, CSRF, cookies, query,
Content-Type, framing par appels ASGI bruts, bornes 4096 / 4097, JSON
strict, ordre exact des contrôles par instrumentation (aucune transaction
pour un refus de transport), résultats inattendus du runner, composition,
sécurité inter-comptes, absence de fuite, parcours de bout en bout sur la
composition de test.
"""
import json
import time
import uuid

import pytest
import sqlalchemy as sa

from core.authz_f2 import runner as runner_module
from core.authz_f2.portfolio_results import canonical_json
from core.authz_f2_http import portfolio as portfolio_module
from core.authz_f2_http.isolated import PersonalHttpConfigError, build_isolated_personal_app, make_personal_app
from core.authz_f2_http.routes import PersonalDependencies
from core.auth_f2_http.provenance import validate_allowed_origins
from tests.authz_f2_support import (
    BOB,
    K1,
    ME,
    ME_LEVEL,
    PORTFOLIO,
    USER_A,
    USER_B,
    AuthzEnv,
    PersonalBrowser,
    active_cookie,
    compose_test_app,
    me_headers,
    personal_app,
    portfolio_headers,
    portfolio_keyring,
    portfolio_request,
    position_body,
    raw_request,
)
from tests.f2_http_support import ORIGIN, csrf_keyring
from tests.f2_support import prepare_schema
from tests.test_authz_f2_http import assert_error, assert_private, http_sign_in
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

MAX_REVISION = 2**63 - 1


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


def item(position_id):
    return f"{PORTFOLIO}/{position_id}"


def get(app, issued, path=PORTFOLIO, **kwargs):
    headers = kwargs.pop("headers", None) or me_headers(issued.generation_id, active_cookie(issued))
    return portfolio_request(app, "GET", path, headers=headers, **kwargs)


def post(app, issued, body=None, op=None, **kwargs):
    headers = kwargs.pop("headers", None) or portfolio_headers(issued, operation_id=uuid.uuid4() if op is None else op, **kwargs)
    return portfolio_request(app, "POST", PORTFOLIO, headers=headers,
                             content=position_body() if body is None else body)


def put(app, issued, position_id, revision="0", body=None, op=None, **kwargs):
    headers = kwargs.pop("headers", None) or portfolio_headers(issued, operation_id=uuid.uuid4() if op is None else op,
                                                               expected_revision=revision, **kwargs)
    return portfolio_request(app, "PUT", item(position_id), headers=headers,
                             content=position_body(quantity="5") if body is None else body)


def delete(app, issued, position_id, revision="0", op=None, content=None, **kwargs):
    headers = kwargs.pop("headers", None) or portfolio_headers(issued, operation_id=uuid.uuid4() if op is None else op,
                                                               expected_revision=revision, **kwargs)
    return portfolio_request(app, "DELETE", item(position_id), headers=headers, content=content)


def assert_canonical(response, status):
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.content == canonical_json(json.loads(response.content))
    assert_private(response)


# --------------------------------------------------------------------------
# Routes et succès
# --------------------------------------------------------------------------

def test_personal_application_contains_exactly_its_routes(app):
    assert sorted((sorted(r.methods), r.path) for r in app.routes if hasattr(r, "methods")) == sorted([
        (["GET"], ME), (["PUT"], ME_LEVEL), (["GET"], PORTFOLIO), (["POST"], PORTFOLIO),
        (["GET"], PORTFOLIO + "/{position_id}"), (["PUT"], PORTFOLIO + "/{position_id}"),
        (["DELETE"], PORTFOLIO + "/{position_id}"),
        # R1 PR 8 : trois lectures (tests/test_authz_f2_consultation_http.py).
        (["GET"], "/api/v2/me/analysis-overview"), (["GET"], "/api/v2/me/theses"),
        (["GET"], "/api/v2/me/theses/{thesis_id}")])


@pytest.mark.parametrize("method,path,status", [
    ("PATCH", PORTFOLIO, 405), ("DELETE", PORTFOLIO, 405), ("PUT", PORTFOLIO, 405), ("POST", item(1), 405),
    ("PATCH", item(1), 405), ("GET", PORTFOLIO + "/1/x", 404), ("GET", "/api/v2/portfolio", 404),
])
def test_routing_refusals_keep_the_existing_404_405(env, alice, app, method, path, status):
    response = portfolio_request(app, method, path, headers=portfolio_headers(alice, operation_id=uuid.uuid4()))
    assert response.status_code == status
    assert_private(response)
    assert env.hook_calls == []


def test_empty_then_filled_collection(env, alice, app):
    empty = get(app, alice)
    assert_canonical(empty, 200)
    assert empty.content == b'{"positions":[]}'
    first = post(app, alice, position_body(quantity="4.5", purchase_price="100.1", target_percent="20",
                                           envelope="CTO")).json()["position_id"]
    legacy = env.insert_position(ticker="vieux", quantity=0.1 + 0.2, envelope="AV")
    listed = get(app, alice)
    assert_canonical(listed, 200)
    assert listed.json() == {"positions": [
        {"position_id": first, "position_revision": "0", "ticker": "AIR.PA", "quantity": "4.5",
         "purchase_price": "100.1", "target_percent": "20", "envelope": "CTO",
         "numeric_encoding": "binary64-decimal-v1", "data_status": "valid", "validation_issues": []},
        {"position_id": legacy, "position_revision": "0", "ticker": "vieux", "quantity": "0.30000000000000004",
         "purchase_price": None, "target_percent": None, "envelope": "AV",
         "numeric_encoding": "binary64-decimal-v1", "data_status": "legacy_invalid",
         "validation_issues": ["ticker_invalid", "quantity_invalid", "envelope_invalid"]}]}
    one = get(app, alice, item(legacy))
    assert_canonical(one, 200)
    assert one.json() == listed.json()["positions"][1]


def test_historical_special_values_are_json_strings(env, alice, app):
    target = env.insert_position(quantity=float("nan"), purchase_price=float("inf"), target_percent=float("-inf"))
    response = get(app, alice, item(target))
    assert b"NaN" in response.content and b'"NaN"' in response.content
    assert (response.json()["quantity"], response.json()["purchase_price"], response.json()["target_percent"]) == (
        "NaN", "Infinity", "-Infinity")
    json.loads(response.content, parse_constant=lambda name: pytest.fail(f"constante JSON {name}"))


def test_missing_and_foreign_positions_are_identical_404(env, alice, app):
    bob = env.confirmed(BOB, USER_B)
    foreign = post(app, bob).json()["position_id"]
    responses = [get(app, alice, item(foreign)), get(app, alice, item(2147483647)),
                 put(app, alice, foreign), put(app, alice, 2147483647),
                 delete(app, alice, foreign), delete(app, alice, 2147483647)]
    for response in responses:
        assert_error(response, 404, "position_not_found")
    assert len({(r.status_code, r.content, tuple(sorted(r.headers.items()))) for r in responses}) == 1
    assert env.position(foreign)["position_revision"] == 0 and env.receipts(USER_A) == []


def test_post_creates_with_location_and_replays_byte_for_byte(env, alice, app):
    op = uuid.uuid4()
    first = post(app, alice, op=op)
    assert_canonical(first, 201)
    body = first.json()
    assert body["operation_id"] == str(op) and body["outcome"] == "created" and body["position"]["position_revision"] \
        == "0"
    assert first.headers.get_list("location") == [f"/api/v2/me/portfolio/{body['position_id']}"]
    (receipt,) = env.receipts()
    assert json.loads(receipt["payload_text"]) == body and receipt["result_status"] == 201
    replay = post(app, alice, op=op)  # réponse perdue : même intention, même UUID
    assert replay.status_code == 201 and replay.content == first.content
    assert replay.headers.get_list("location") == first.headers.get_list("location")
    assert env.count("portfolio_positions") == 1
    assert_error(post(app, alice, position_body(quantity="5"), op=op), 409, "idempotency_conflict")


def test_put_replaces_and_replays(env, alice, app):
    target = post(app, alice).json()["position_id"]
    op = uuid.uuid4()
    body = position_body(ticker="MC.PA", quantity="2", purchase_price=None, target_percent="0", envelope=None)
    first = put(app, alice, target, "0", body, op)
    assert_canonical(first, 200)
    assert first.json()["outcome"] == "replaced" and first.json()["position"]["position_revision"] == "1"
    assert first.headers.get_list("location") == []
    assert put(app, alice, target, "0", body, op).content == first.content
    assert_error(put(app, alice, target, "0", body), 409, "position_revision_conflict")
    noop = put(app, alice, target, "1", body)
    assert noop.status_code == 200 and noop.json()["position"]["position_revision"] == "1"
    assert get(app, alice, item(target)).json() == first.json()["position"]


def test_delete_without_body_and_replays_after_disappearance(env, alice, app):
    target = post(app, alice).json()["position_id"]
    op = uuid.uuid4()
    first = delete(app, alice, target, "0", op, content_type=None)
    assert_canonical(first, 200)
    assert first.json() == {"operation_id": str(op), "operation_type": "portfolio_position_delete",
                            "position_id": target, "outcome": "deleted", "deleted_revision": "0"}
    assert_error(get(app, alice, item(target)), 404, "position_not_found")
    assert delete(app, alice, target, "0", op).content == first.content
    assert_error(delete(app, alice, target, "0"), 404, "position_not_found")


def test_delete_ignores_a_content_type_without_body(env, alice, app):
    target = post(app, alice).json()["position_id"]
    assert delete(app, alice, target, content_type="text/plain").status_code == 200


def test_revision_bounds_in_http(env, alice, app):
    target = env.insert_position(revision=MAX_REVISION, quantity=4.0)
    same = put(app, alice, target, str(MAX_REVISION), position_body(quantity="4"))
    assert same.status_code == 200 and same.json()["position"]["position_revision"] == str(MAX_REVISION)
    assert_error(put(app, alice, target, str(MAX_REVISION), position_body(quantity="5")), 503,
                 "temporarily_unavailable")
    assert delete(app, alice, target, str(MAX_REVISION)).json()["deleted_revision"] == str(MAX_REVISION)


def test_explicit_repair_of_a_legacy_position_through_put(env, alice, app):
    target = env.insert_position(ticker="?", quantity=-1.0, envelope="AV")
    assert get(app, alice, item(target)).json()["data_status"] == "legacy_invalid"
    repaired = put(app, alice, target, "0", position_body(quantity="1"))
    assert repaired.json()["position"]["data_status"] == "valid"


# --------------------------------------------------------------------------
# Identifiants et en-têtes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["0", "01", "-1", "+1", "1.0", "1e3", "2147483648", "99999999999", "%31", "%20",
                                 "١", "１", " 1", "abc"])
def test_non_canonical_position_ids_are_invalid_request(env, alice, app, raw):
    for response in (get(app, alice, item(raw)), put(app, alice, raw), delete(app, alice, raw)):
        assert_error(response, 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("op", ["", "abc", "{G}", "{g}x", "{n}", "{{{g}}}", " {g}", "{g} ", "urn:uuid:{g}"])
def test_non_canonical_operation_ids_are_invalid_request(env, alice, app, op):
    canonical = str(uuid.uuid4())
    value = op.replace("{G}", canonical.upper()).replace("{g}", canonical).replace("{n}", canonical.replace("-", ""))
    target = env.insert_position()
    for response in (post(app, alice, op=value), put(app, alice, target, op=value),
                     delete(app, alice, target, op=value)):
        assert_error(response, 400, "invalid_request")
    assert env.receipts() == [] and env.hook_calls == []


def test_operation_id_header_must_be_present_and_unique(env, alice, app):
    headers = portfolio_headers(alice)
    assert_error(portfolio_request(app, "POST", PORTFOLIO, headers=headers, content=position_body()), 400,
                 "invalid_request")
    raw = [("host", "oryx.test")] + list(portfolio_headers(alice).items()) + [
        ("x-oryx-operation-id", str(uuid.uuid4())), ("x-oryx-operation-id", str(uuid.uuid4()))]
    response = raw_request(app, method="POST", path=PORTFOLIO, headers=raw, chunks=[position_body()])
    assert response.status_code == 400 and response.json() == {"error": "invalid_request"}


@pytest.mark.parametrize("revision", ["", "-1", "+1", "01", "00", "1.0", "1e3", " 1", "1 ", "0x1",
                                      "9223372036854775808", "99999999999999999999", "١", "１", "²"])
def test_non_canonical_expected_revisions_are_invalid_request(env, alice, app, revision):
    target = env.insert_position()
    if not revision.isascii():  # chiffres Unicode : octets UTF-8 transmis tels quels
        responses = [raw(app, alice, method, item(target), revision=revision.encode("utf-8"), chunks=chunks)
                     for method, chunks in (("PUT", [position_body()]), ("DELETE", []))]
        assert [r.status_code for r in responses] == [400, 400]
        assert env.hook_calls == []
        return
    for response in (put(app, alice, target, revision), delete(app, alice, target, revision)):
        assert_error(response, 400, "invalid_request")
    assert env.hook_calls == []


def test_expected_revision_header_must_be_present_and_unique(env, alice, app):
    target = env.insert_position()
    headers = portfolio_headers(alice, operation_id=uuid.uuid4())
    assert_error(portfolio_request(app, "DELETE", item(target), headers=headers), 400, "invalid_request")
    raw = [("host", "oryx.test")] + list(portfolio_headers(alice, operation_id=uuid.uuid4()).items()) + [
        ("x-oryx-expected-revision", "0"), ("x-oryx-expected-revision", "0")]
    assert raw_request(app, method="DELETE", path=item(target), headers=raw).status_code == 400


# --------------------------------------------------------------------------
# Provenance, CSRF, cookies, query
# --------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"origin": None}, {"origin": "https://evil.test"}, {"origin": "null"}, {"fetch_site": "same-site"},
    {"fetch_site": "cross-site"}, {"extra": {"referer": "https://evil.test/x"}},
    {"extra": {"host": "evil.test"}},
])
def test_mutation_provenance_refusals(env, alice, app, change):
    target = env.insert_position()
    for response in (post(app, alice, **change), put(app, alice, target, **change),
                     delete(app, alice, target, **change)):
        assert_error(response, 403, "origin_refused")
    assert env.hook_calls == []


def test_get_keeps_the_pr5_personal_provenance(env, alice, app):
    cookie = active_cookie(alice)
    assert get(app, alice, headers=me_headers(alice.generation_id, cookie)).status_code == 200
    assert get(app, alice, headers=me_headers(alice.generation_id, cookie, origin=ORIGIN,
                                              fetch_site=None)).status_code == 200
    assert_error(get(app, alice, headers=me_headers(alice.generation_id, cookie, fetch_site=None)), 403,
                 "origin_refused")
    assert_error(get(app, alice, headers=me_headers(alice.generation_id, cookie, fetch_site="cross-site")), 403,
                 "origin_refused")


@pytest.mark.parametrize("token", [None, "", "c1.1.AAAAAAAAAAAAAAAAAAAAAA.x", "pas-un-jeton"])
def test_mutations_require_a_valid_csrf_token(env, alice, app, token):
    target = env.insert_position()
    for response in (post(app, alice, csrf_token=token), put(app, alice, target, csrf_token=token),
                     delete(app, alice, target, csrf_token=token)):
        assert_error(response, 403, "csrf_invalid")
    assert env.hook_calls == [] and env.receipts() == []


def test_unavailable_csrf_dependency_is_503(env, alice):
    app = make_personal_app(PersonalDependencies(object(), validate_allowed_origins([ORIGIN])))
    assert_error(post(app, alice), 503, "temporarily_unavailable")


def test_malformed_cookie_header_and_absent_designated_cookie(env, alice, app):
    assert_error(post(app, alice, cookie="__Host-oryx-active-x=y"), 400, "invalid_request")
    other = env.confirmed("alice2@example.com", USER_A)
    # Sélecteur d'une génération dont le cookie est absent, cookie actif
    # d'une autre génération présent : jamais choisi implicitement.
    assert_error(post(app, other, generation_id=uuid.uuid4()), 401, "session_invalid")
    assert_error(post(app, other, generation_id=alice.generation_id), 401, "session_invalid")
    assert_error(get(app, alice, headers=me_headers(uuid.uuid4(), active_cookie(alice))), 401, "session_invalid")


@pytest.mark.parametrize("query", ["?user_id=user-b", "?x", "?position_id=1"])
def test_query_is_invalid_request(env, alice, app, query):
    target = env.insert_position()
    headers = portfolio_headers(alice, operation_id=uuid.uuid4(), expected_revision="0")
    for method, path, content in (("GET", PORTFOLIO, None), ("GET", item(target), None),
                                  ("POST", PORTFOLIO, position_body()), ("PUT", item(target), position_body()),
                                  ("DELETE", item(target), None)):
        sent = me_headers(alice.generation_id, active_cookie(alice)) if method == "GET" else headers
        assert_error(portfolio_request(app, method, path + query, headers=sent, content=content), 400,
                     "invalid_request")


def test_client_user_id_is_never_honoured(env, alice, app):
    bob = env.confirmed(BOB, USER_B)
    payload = json.loads(position_body())
    payload["user_id"] = USER_B
    assert_error(post(app, alice, json.dumps(payload).encode()), 400, "invalid_request")
    created = post(app, alice, extra={"x-user-id": USER_B}).json()
    assert env.position(created["position_id"])["user_id"] == USER_A
    assert get(app, bob).json() == {"positions": []}


# --------------------------------------------------------------------------
# Content-Type, framing, corps
# --------------------------------------------------------------------------

@pytest.mark.parametrize("content_type", [None, "text/plain", "application/jsonx", "application/json;",
                                          "application/json; charset=latin-1", "application/json; charset=utf-8; x=1",
                                          "multipart/form-data"])
def test_post_and_put_content_type_is_strict(env, alice, app, content_type):
    target = env.insert_position()
    assert_error(post(app, alice, content_type=content_type), 415, "invalid_request")
    assert_error(put(app, alice, target, content_type=content_type), 415, "invalid_request")


@pytest.mark.parametrize("content_type", ["application/json; charset=utf-8", "application/json;charset=UTF-8",
                                          "Application/JSON"])
def test_accepted_content_types(env, alice, app, content_type):
    assert post(app, alice, content_type=content_type).status_code == 201


def raw(app, alice, method, path, *, op=True, revision=None, content_length=None, chunks=(), extra=(),
        disconnect=False):
    headers = [("host", "oryx.test")] + list(portfolio_headers(
        alice, operation_id=uuid.uuid4() if op else None,
        expected_revision=revision if not isinstance(revision, bytes) else None).items())
    if isinstance(revision, bytes):
        headers.append(("x-oryx-expected-revision", revision))
    if content_length is not None:
        headers += [("content-length", v) for v in (content_length if isinstance(content_length, list)
                                                   else [content_length])]
    return raw_request(app, method=method, path=path, headers=headers + list(extra), chunks=list(chunks),
                       disconnect=disconnect)


def _padded(size):
    body = position_body()
    return body[:-1] + b" " * (size - len(body)) + b"}"


def test_post_body_bound_is_4096(env, alice, app):
    exact, too_big = _padded(4096), _padded(4097)
    assert raw(app, alice, "POST", PORTFOLIO, content_length="4096", chunks=[exact]).status_code == 201
    assert raw(app, alice, "POST", PORTFOLIO, content_length="4097", chunks=[too_big]).status_code == 413
    assert raw(app, alice, "POST", PORTFOLIO, content_length="4097", chunks=[]).status_code == 413
    assert raw(app, alice, "POST", PORTFOLIO, chunks=[exact[:2048], exact[2048:]]).status_code == 201
    assert raw(app, alice, "POST", PORTFOLIO, chunks=[too_big[:2048], too_big[2048:]]).status_code == 413
    assert env.count("portfolio_positions") == 2


@pytest.mark.parametrize("case,status", [
    ("transfer_encoding", 400), ("transfer_encoding_with_length", 400), ("duplicated_length", 400),
    ("malformed_length", 400), ("unicode_length", 400), ("short_stream", 400), ("long_stream", 400),
    ("disconnect", 400),
])
def test_post_and_put_framing(env, alice, app, case, status):
    target = env.insert_position()
    body = position_body()
    size = str(len(body))
    params = {
        "transfer_encoding": dict(extra=[("transfer-encoding", "chunked")], chunks=[body]),
        "transfer_encoding_with_length": dict(content_length=size, extra=[("transfer-encoding", "identity")],
                                              chunks=[body]),
        "duplicated_length": dict(content_length=[size, size], chunks=[body]),
        "malformed_length": dict(content_length=["+" + size], chunks=[body]),
        "unicode_length": dict(content_length=["٤٠".encode("utf-8")], chunks=[body]),
        "short_stream": dict(content_length=str(len(body) + 10), chunks=[body]),
        "long_stream": dict(content_length=str(len(body) - 1), chunks=[body]),
        "disconnect": dict(content_length=size, chunks=[body[:5]], disconnect=True),
    }[case]
    for method, path, revision in (("POST", PORTFOLIO, None), ("PUT", item(target), "0")):
        response = raw(app, alice, method, path, revision=revision, **params)
        assert response.status_code == status and response.json() == {"error": "invalid_request"}, (method, case)
    assert env.hook_calls == [] and env.receipts() == []


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("content_length,chunks,status", [
    ("1", [b"x"], 400), ("4096", [], 400), ("4097", [], 413), ("99999999999999999999", [], 413), ("0", [], None),
    (None, [b"{}"], 400), (None, [b" " * 2048, b" " * 2049], 413), (None, [], None),
    (["0", "0"], [], 400), ("-1", [], 400), ("0x10", [], 400), ("0", [b"x"], 400),
])
def test_get_and_delete_accept_no_body(env, alice, app, method, content_length, chunks, status):
    target = env.insert_position()
    if method == "GET":
        headers = [("host", "oryx.test")] + list(me_headers(alice.generation_id, active_cookie(alice)).items())
        if content_length is not None:
            headers += [("content-length", v) for v in (content_length if isinstance(content_length, list)
                                                       else [content_length])]
        response = raw_request(app, method="GET", path=item(target), headers=headers, chunks=chunks)
    else:
        response = raw(app, alice, "DELETE", item(target), revision="0", content_length=content_length,
                       chunks=chunks)
    if status is None:
        assert response.status_code == 200
    else:
        assert response.status_code == status
        assert response.json() == {"error": "invalid_request" if status == 400 else "request_too_large"}
        assert env.position(target) is not None


def test_get_and_delete_refuse_transfer_encoding(env, alice, app):
    target = env.insert_position()
    headers = [("host", "oryx.test"), ("transfer-encoding", "chunked")] + list(
        me_headers(alice.generation_id, active_cookie(alice)).items())
    assert raw_request(app, method="GET", path=PORTFOLIO, headers=headers).status_code == 400
    response = raw(app, alice, "DELETE", item(target), revision="0", extra=[("transfer-encoding", "chunked")])
    assert response.status_code == 400 and env.position(target) is not None


BAD_BODIES = {
    "array": b"[]", "string": b'"x"', "null": b"null", "empty": b"", "trailing": position_body() + b" x",
    "duplicate": b'{"ticker":"AIR.PA","ticker":"MC.PA","quantity":"4","purchase_price":null,'
                 b'"target_percent":null,"envelope":null}',
    "nan": b'{"ticker":"AIR.PA","quantity":NaN,"purchase_price":null,"target_percent":null,"envelope":null}',
    "number_quantity": b'{"ticker":"AIR.PA","quantity":4,"purchase_price":null,"target_percent":null,'
                       b'"envelope":null}',
    "float_price": b'{"ticker":"AIR.PA","quantity":"4","purchase_price":1.5,"target_percent":null,'
                   b'"envelope":null}',
    "missing_envelope": b'{"ticker":"AIR.PA","quantity":"4","purchase_price":null,"target_percent":null}',
    "extra_key": b'{"ticker":"AIR.PA","quantity":"4","purchase_price":null,"target_percent":null,'
                 b'"envelope":null,"user_id":"user-b"}',
    "bom": b"\xef\xbb\xbf" + position_body(),
    "latin1": position_body(ticker="AIR.PA").replace(b"AIR.PA", b"A\xe9"),
    "utf16": position_body().decode().encode("utf-16"),
    "bool_ticker": b'{"ticker":true,"quantity":"4","purchase_price":null,"target_percent":null,"envelope":null}',
    "deep": b"[" * 5000 + b"]" * 5000,
}


@pytest.mark.parametrize("case", sorted(BAD_BODIES))
def test_malformed_json_is_invalid_request(env, alice, app, case):
    target = env.insert_position()
    body = BAD_BODIES[case]
    for response in (post(app, alice, body), put(app, alice, target, body=body)):
        expected = 413 if len(body) > 4096 else 400
        assert response.status_code == expected, case
    assert env.receipts() == [] and env.hook_calls == []


@pytest.mark.parametrize("field,value", [
    ("ticker", "air.pa"), ("ticker", " AIR.PA"), ("ticker", ""), ("ticker", None), ("ticker", "A" * 33),
    ("quantity", "0"), ("quantity", "-1"), ("quantity", "1e3"), ("quantity", "4.0"), ("quantity", None),
    ("quantity", "1000000000000.5"), ("quantity", "0.0000001"), ("quantity", "1234567890123456"),
    ("purchase_price", "-1"), ("purchase_price", "1000000000001"), ("purchase_price", ""),
    ("target_percent", "100.5"), ("target_percent", "1E2"), ("envelope", "pea"), ("envelope", "AV"),
    ("envelope", ""),
])
def test_invalid_field_values_are_invalid_request(env, alice, app, field, value):
    values = {"ticker": "AIR.PA", "quantity": "4", "purchase_price": None, "target_percent": None, "envelope": None}
    values[field] = value
    body = position_body(**values)
    target = env.insert_position()
    assert_error(post(app, alice, body), 400, "invalid_request")
    assert_error(put(app, alice, target, body=body), 400, "invalid_request")


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

    for name, attr in (("provenance", "personal_mutation_origin"), ("provenance", "personal_get_origin"),
                       ("cookies", "parse_cookie_headers"), ("csrf", "_check_csrf"), ("selector", "_generation_id"),
                       ("position", "_position_id"), ("operation", "_operation_id"), ("revision", "_expected_revision"),
                       ("content_type", "_check_content_type"), ("framing", "_announced_length"),
                       ("json", "_position_request")):
        monkeypatch.setattr(portfolio_module, attr, record(name, getattr(portfolio_module, attr)))
    monkeypatch.setattr(portfolio_module, "_read_body", record_async("body", portfolio_module._read_body))
    monkeypatch.setattr(runner_module, "transaction", record("transaction", runner_module.transaction))
    return calls


BIG = b"x" * 5000


def test_check_priorities_for_put(env, alice, app, trace):
    target = env.insert_position()
    good = dict(operation_id=uuid.uuid4(), expected_revision="0")
    prefix = ["provenance", "cookies", "csrf", "selector"]
    cases = [
        ("provenance", dict(origin=None, csrf_token=None, generation_id="x", **good), item(target), BIG,
         (403, "origin_refused"), ["provenance"]),
        ("cookies", dict(cookie="__Host-oryx-active-x=y", csrf_token=None, **good), item(target), BIG,
         (400, "invalid_request"), ["provenance", "cookies"]),
        ("csrf", dict(csrf_token=None, generation_id="x", **good), item(target), BIG,
         (403, "csrf_invalid"), ["provenance", "cookies", "csrf"]),
        ("selector", dict(generation_id="x", **good), item("01") + "?q", BIG,
         (400, "invalid_request"), prefix),
        ("query", dict(**good), item("01") + "?q", BIG, (400, "invalid_request"), prefix),
        ("position", dict(operation_id="x", expected_revision="x"), item("01"), BIG,
         (400, "invalid_request"), prefix + ["position"]),
        ("operation", dict(operation_id="x", expected_revision="x", content_type="text/plain"), item(target), BIG,
         (400, "invalid_request"), prefix + ["position", "operation"]),
        ("revision", dict(operation_id=uuid.uuid4(), expected_revision="01", content_type="text/plain"),
         item(target), BIG, (400, "invalid_request"), prefix + ["position", "operation", "revision"]),
        ("content_type", dict(content_type="text/plain", **good), item(target), BIG,
         (415, "invalid_request"), prefix + ["position", "operation", "revision", "content_type"]),
        ("size", dict(**good), item(target), BIG,  # longueur annoncée > 4096 : 413 sans lecture
         (413, "request_too_large"), prefix + ["position", "operation", "revision", "content_type", "framing"]),
        ("json", dict(**good), item(target), b"{}",
         (400, "invalid_request"),
         prefix + ["position", "operation", "revision", "content_type", "framing", "body", "json"]),
        ("cookie_designated", dict(generation_id=uuid.uuid4(), **good), item(target), position_body(),
         (401, "session_invalid"),
         prefix + ["position", "operation", "revision", "content_type", "framing", "body", "json"]),
    ]
    for name, kwargs, path, content, (status, code), executed in cases:
        trace.clear()
        headers = portfolio_headers(alice, **kwargs)
        assert_error(portfolio_request(app, "PUT", path, headers=headers, content=content), status, code)
        assert trace == executed, name  # aucune transaction pour un refus de transport
    assert env.hook_calls == [] and env.receipts() == []
    trace.clear()
    assert put(app, alice, target).status_code == 200
    assert trace[-2:] == ["transaction", "transaction"]


def test_check_priorities_for_delete_and_get(env, alice, app, trace):
    target = env.insert_position()
    trace.clear()
    response = delete(app, alice, target, "01", content=BIG)
    assert_error(response, 400, "invalid_request")
    assert trace == ["provenance", "cookies", "csrf", "selector", "position", "operation", "revision"]
    trace.clear()
    assert_error(delete(app, alice, target, "0", content=BIG), 413, "request_too_large")
    assert trace == ["provenance", "cookies", "csrf", "selector", "position", "operation", "revision", "framing"]
    trace.clear()
    assert_error(get(app, alice, item("x"), content=BIG), 400, "invalid_request")
    assert trace == ["provenance", "cookies", "selector", "position"]
    assert env.hook_calls == []


# --------------------------------------------------------------------------
# Runner, composition, résultats inattendus
# --------------------------------------------------------------------------

def deps(runner):
    return PersonalDependencies(runner, validate_allowed_origins([ORIGIN]), csrf_keyring(), time.time)


def test_unconfigured_runner_failing_runner_and_missing_portfolio_keyring(env, alice):
    assert_error(post(make_personal_app(deps(None)), alice), 503, "temporarily_unavailable")

    class Failing:
        def run(self, credential, command):
            raise RuntimeError("panne")

    assert_error(post(make_personal_app(deps(Failing())), alice), 503, "temporarily_unavailable")
    without_keys = personal_app(env, portfolio_keys=None)
    assert_error(post(without_keys, alice), 503, "temporarily_unavailable")
    assert get(without_keys, alice).status_code == 200
    assert env.count("portfolio_positions") == 0


def test_unexpected_runner_results_are_never_rendered(env, alice, app):
    good = post(app, alice)
    real = env.runner()
    outcome_holder = {}

    class Recording:
        def run(self, credential, command):
            outcome = real.run(credential, command)
            outcome_holder["outcome"] = outcome
            return outcome

    op = uuid.uuid4()
    post(make_personal_app(deps(Recording())), alice, op=op)
    result, principal = outcome_holder["outcome"].value, outcome_holder["outcome"].principal
    from dataclasses import replace
    from core.authz_f2.principal import AuthorizedPrincipal
    other_generation = AuthorizedPrincipal(principal.user_id, principal.session_id, uuid.uuid4(),
                                           principal.auth_method)
    cases = [
        runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, result, other_generation),
        runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, replace(result, body=good.content), principal),
        runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, replace(result, status=200), principal),
        runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, replace(result, operation_id=uuid.uuid4()),
                                           principal),
        runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, None, principal),
        runner_module.AuthorizationOutcome(runner_module.UNAVAILABLE),
        runner_module.AuthorizationOutcome("autre"),
        "pas un résultat",
    ]
    for outcome in cases:
        class Fixed:
            def run(self, credential, command, outcome=outcome):
                return outcome

        assert_error(post(make_personal_app(deps(Fixed())), alice, op=op), 503, "temporarily_unavailable")
    assert post(make_personal_app(deps(Recording())), alice, op=op).status_code == 201  # contrôle


def test_invalid_portfolio_composition_is_refused(env, pg_url):  # noqa: F811
    base = dict(engine=env.engine, keyring=env.keyring, allowed_origins=[ORIGIN], csrf_keyring=csrf_keyring())
    f2_key = env.keyring.key("active", env.keyring.current_version("active"))
    for keys in ({"k1": f2_key}, {"k1": bytes([0xC0]) * 32}):
        with pytest.raises(PersonalHttpConfigError):
            build_isolated_personal_app(**base, portfolio_keyring=portfolio_keyring(keys))
    with pytest.raises(PersonalHttpConfigError):
        build_isolated_personal_app(**base, portfolio_keyring={"k1": K1})
    engine = sa.create_engine(pg_url, isolation_level="REPEATABLE READ")
    try:
        with pytest.raises(PersonalHttpConfigError):
            build_isolated_personal_app(**{**base, "engine": engine}, portfolio_keyring=portfolio_keyring())
    finally:
        engine.dispose()
    assert build_isolated_personal_app(**base, portfolio_keyring=portfolio_keyring()) is not None


def test_session_refusals_are_401_everywhere(env, alice, app):
    target = post(app, alice).json()["position_id"]
    env.service.revoke_session(alice.session_id, "logout")
    for response in (get(app, alice), get(app, alice, item(target)), post(app, alice), put(app, alice, target),
                     delete(app, alice, target)):
        assert_error(response, 401, "session_invalid")


def test_cross_account_isolation(env, alice, app):
    bob = env.confirmed(BOB, USER_B)
    op = uuid.uuid4()
    b_created = post(app, bob, op=op)
    target = b_created.json()["position_id"]
    # A ne lit, ne modifie, ne supprime rien de B ; l'UUID de B ne lui donne rien.
    assert_error(get(app, alice, item(target)), 404, "position_not_found")
    assert_error(put(app, alice, target, op=op), 404, "position_not_found")
    assert_error(delete(app, alice, target, op=op), 404, "position_not_found")
    mine = post(app, alice, op=op)  # même UUID : son propre triplet, nouvelle création
    assert mine.status_code == 201 and mine.json()["position_id"] != target
    assert get(app, alice).json()["positions"] == [mine.json()["position"]]
    assert get(app, bob).json()["positions"] == [b_created.json()["position"]]


def test_no_secret_or_financial_data_in_logs_or_responses(env, alice, app, caplog):
    op = uuid.uuid4()
    with caplog.at_level("DEBUG"):
        created = post(app, alice, position_body(ticker="ZZTOP.PA", quantity="777.25"), op=op)
        put(app, alice, created.json()["position_id"], "0", position_body(ticker="ZZTOP.PA", quantity="888.5"))
        post(app, alice, position_body(ticker="ZZTOP.PA", quantity="999"), op=op)  # conflit
        get(app, alice)
    text = caplog.text
    for secret in (str(op), "ZZTOP", "777.25", "888.5", alice.active_secret.reveal(), USER_A, "k1"):
        assert secret not in text, secret
    for receipt in env.receipts():
        assert receipt["request_fingerprint"] not in text
        assert receipt["request_fingerprint"] not in created.text
    assert USER_A not in created.text and "fingerprint" not in created.text


# --------------------------------------------------------------------------
# Bout en bout : bootstrap CSRF (PR 3) -> connexion -> résolution -> CRUD
# --------------------------------------------------------------------------

def test_end_to_end_on_the_test_composition(env):
    composed = compose_test_app(env)
    browser = PersonalBrowser(composed)
    browser.bootstrap()
    generation_id = http_sign_in(env, browser)
    assert browser.resolve(generation_id).status_code == 200

    def call(method, path, *, op=None, revision=None, body=None):
        headers = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-oryx-generation-id": generation_id,
                   "cookie": browser.cookie_header(), "x-oryx-csrf": browser.csrf}
        if body is not None:
            headers["content-type"] = "application/json"
        if op is not None:
            headers["x-oryx-operation-id"] = str(op)
        if revision is not None:
            headers["x-oryx-expected-revision"] = revision
        if method == "GET":
            headers = me_headers(generation_id, browser.cookie_header())
        response = portfolio_request(composed, method, path, headers=headers, content=body)
        assert response.headers.get_list("set-cookie") == []
        return response

    created = call("POST", PORTFOLIO, op=uuid.uuid4(), body=position_body())
    assert created.status_code == 201
    target = created.json()["position_id"]
    assert call("GET", PORTFOLIO).json()["positions"][0]["position_id"] == target
    replaced = call("PUT", item(target), op=uuid.uuid4(), revision="0", body=position_body(quantity="6"))
    assert replaced.json()["position"]["position_revision"] == "1"
    assert call("PUT", item(target), op=uuid.uuid4(), revision="0", body=position_body()).status_code == 409
    assert call("DELETE", item(target), op=uuid.uuid4(), revision="1").status_code == 200
    assert call("GET", item(target)).status_code == 404
    assert browser.logout(generation_id).status_code == 200
    assert call("GET", PORTFOLIO).status_code == 401
