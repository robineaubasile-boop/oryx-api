"""R1 PR 9-C3 — orchestration HTTP F2 Décrypter ISOLÉE, contre un VRAI
PostgreSQL (16 et 18 en CI), sous le VRAI runner, le registre de PRODUCTION
(vingt commandes) et la VRAIE application personnelle isolée (transports
EODHD / FMP / Claude simulés, aucun réseau).

Chaque état durable est observé par une connexion indépendante ; « aucun
appel B1 » = aucun point d'interruption du runner atteint ET aucune
réservation écrite.

1. parcours nominal et restitution canonique (octets JSONB exacts) ;
2. admission HTTP stricte (aucun appel B1 sur refus) ;
3. matrice B1 ; 4. matrice B2 ; 5. matrice C1-B ; 6. replay et ACK C2 ;
7. budget coopératif de 270 s ; 8. continuité du principal ;
9. rotation HMAC B1 ; 10. récupération après incident.
"""
import hashlib
import json
import logging
import time
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import (AUTHORIZED, IDEMPOTENCY_CONFLICT, NOT_FOUND, REVISION_CONFLICT, SESSION_INVALID,
                                  UNAVAILABLE, AuthorizationOutcome, AuthorizedTransactionRunner)
from core.authz_f2_http import decryptage as c3
from tests.authz_f2_pr9a_support import BOB_EMAIL, USER_A, USER_B
from tests.authz_f2_pr9b1_support import D1, D2, reserve_command
from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock, FakeEodhd
from tests.authz_f2_pr9c1b_support import schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c3_support import (ACK, CONV, REPLAY, TURNS, HttpEnv, body_of, c3_headers, identity_fields,
                                          post, raw_post, turn_fields)
from tests.authz_f2_support import active_cookie, csrf_pair, me_request, raw_request
from tests.test_authz_f2_http import NO_STORE
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = HttpEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


@pytest.fixture
def bob(env):
    return env.confirmed(BOB_EMAIL, USER_B)


def assert_private(response):
    headers = getattr(response, "headers", response)  # httpx.Response ou RawResponse (appel ASGI brut)
    for name, value in NO_STORE.items():
        assert headers.get_list(name) == [value], name
    assert headers.get_list("set-cookie") == []
    assert headers.get_list("access-control-allow-origin") == []


def assert_error(response, status, code):
    assert response.status_code == status, (response.status_code, response.content[:300])
    assert response.json() == {"error": code}
    assert_private(response)


def turn(env, app, issued, fields, **kwargs):
    return post(app, TURNS, body_of(fields), c3_headers(issued, **kwargs))


def delivered_ok(response, env):
    """200 de restitution : enveloppe fermée, payload = texte JSONB EXACT."""
    assert response.status_code == 200, response.content[:300]
    assert_private(response)
    data = json.loads(response.content, parse_float=Decimal, parse_int=Decimal)
    assert set(data) == {"assistant_turn_id", "delivery_status", "payload", "reservation_id"}
    payload_text = env.payload_text(uuid.UUID(data["assistant_turn_id"]))
    expected = (b'{"assistant_turn_id":"' + data["assistant_turn_id"].encode() + b'","delivery_status":"'
                + data["delivery_status"].encode() + b'","payload":' + payload_text.encode("utf-8")
                + b',"reservation_id":"' + data["reservation_id"].encode() + b'"}')
    assert response.content == expected  # octets JSONB insérés tels quels, jamais resérialisés
    row = env.reservation(uuid.UUID(data["reservation_id"]))
    assert row["state"] == "finalized" and str(row["assistant_delivery_id"]) == data["assistant_turn_id"]
    return data


def ids_of(data):
    return identity_fields(data["reservation_id"], data["assistant_turn_id"])


def runner_untouched(env):
    assert env.hook_calls == [] and env.count("decryptage_turn_reservations") == 0


# --------------------------------------------------------------------------
# 1. Parcours nominal et restitution canonique
# --------------------------------------------------------------------------

def test_a_complete_intention_is_reserved_generated_finalized_and_restored(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    response = turn(env, app, alice, fields)
    data = delivered_ok(response, env)
    assert data["delivery_status"] == "pending"  # aucun ACK automatique
    [row] = env.reservations()
    assert (row["state"], row["lease_epoch"], row["attempts_used"], row["conversation_key"],
            str(row["source_user_turn_id"])) == ("finalized", 1, 1, CONV, fields["client_turn_id"])
    assert env.provider_calls() == ["search", "fundamentals", "realtime", "create_message"]
    [delivery] = env.delivery_rows()
    assert delivery["status"] == "pending" and str(delivery["id"]) == data["assistant_turn_id"]
    assert env.link(delivery["id"])["capture_state"] == "awaiting_delivery"
    # Contenu = livraison canonique C2 (aucun champ du candidat, aucune métadonnée privée).
    assert set(data["payload"]) == {"success", "ticker", "name", "method_used", "analysis", "price", "currency",
                                    "disclaimer"}
    for secret in ("private_metadata", "lease", "fingerprint", "step_marker", "ORYX_STEP", "cognitive"):
        assert secret.encode() not in response.content, secret


def test_the_replayed_and_acknowledged_content_stays_byte_identical(env, alice):
    app = env.c3_app()
    first = turn(env, app, alice, fields := turn_fields())
    data = delivered_ok(first, env)
    replayed = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    assert replayed.status_code == 200 and replayed.content == first.content
    again = turn(env, app, alice, fields)  # même intention : ReservationFinalized -> C2
    assert again.status_code == 200 and again.content == first.content
    assert env.claude.methods() == ["create_message"]  # aucun nouvel appel fournisseur
    acked = post(app, ACK, body_of(ids_of(data)), c3_headers(alice))
    assert acked.status_code == 200 and acked.json() == {"assistant_turn_id": data["assistant_turn_id"],
                                                         "delivery_status": "delivered"}
    assert_private(acked)
    after = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    assert after.json()["delivery_status"] == "delivered"
    assert after.json()["payload"] == first.json()["payload"]
    assert json.loads(after.content)["payload"] == json.loads(first.content)["payload"]


@pytest.mark.parametrize("price", [None, 600.5, 0.0, 1e-07, 1e22, 123456789.123])
def test_jsonb_numbers_unicode_and_escapes_are_restored_as_persisted(env, alice, price):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    delivery_id = uuid.UUID(data["assistant_turn_id"])
    analysis = 'Analyse « éducative » — ligne\n\t"citée" \\   💶 fin'
    payload = {**data["payload"], "price": price, "analysis": analysis, "name": "Société Générale ✓"}
    env.set_payload(delivery_id, "CAST(:p AS jsonb)", p=json.dumps(payload, ensure_ascii=False))
    env.update("assistant_deliveries", "visible_content_fingerprint = :f", "id = :d",
               f=hashlib.sha256(analysis.encode("utf-8")).hexdigest(), d=delivery_id)
    persisted = env.payload_text(delivery_id)
    response = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    delivered = delivered_ok(response, env)
    assert persisted.encode("utf-8") in response.content  # texte JSONB inséré tel quel
    assert delivered["payload"]["analysis"] == analysis and delivered["payload"]["name"] == "Société Générale ✓"
    if price is None:
        assert delivered["payload"]["price"] is None
    else:
        assert float(delivered["payload"]["price"]) == price  # même binary64, aucun arrondi
    if price == 1e-07:
        assert b"0.0000001" in response.content and b"1e-07" not in response.content  # notation JSONB


def test_an_incoherent_persisted_payload_is_never_repaired(env, alice):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    env.set_payload(uuid.UUID(data["assistant_turn_id"]), "CAST(:p AS jsonb)",
                    p=json.dumps({**data["payload"], "price": "600.5"}))
    response = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    assert_error(response, 503, "temporarily_unavailable")
    assert b"600.5" not in response.content


# --------------------------------------------------------------------------
# 2. Admission HTTP stricte
# --------------------------------------------------------------------------

def _csrf_expired():
    _, token = csrf_pair(now=time.time() - 7200)
    return token


ADMISSION = {
    "origin_absent": dict(origin=None, expect=(403, "origin_refused")),
    "origin_foreign": dict(origin="https://evil.test", expect=(403, "origin_refused")),
    "origin_http": dict(origin="http://oryx.test", expect=(403, "origin_refused")),
    "cross_site": dict(fetch_site="cross-site", expect=(403, "origin_refused")),
    "csrf_absent": dict(csrf=None, expect=(403, "csrf_invalid")),
    "csrf_invalid": dict(csrf="c1.1.AAAAAAAAAAAAAAAAAAAAAA.x", expect=(403, "csrf_invalid")),
    "csrf_expired": dict(csrf="expired", expect=(403, "csrf_invalid")),
    "content_type_text": dict(content_type="text/plain", expect=(415, "invalid_request")),
    "content_type_latin1": dict(content_type="application/json; charset=latin-1", expect=(415, "invalid_request")),
    "content_type_absent": dict(content_type=None, expect=(415, "invalid_request")),
}


@pytest.mark.parametrize("path", [TURNS, REPLAY, ACK])
@pytest.mark.parametrize("case", sorted(ADMISSION))
def test_transport_refusals_never_reach_the_runner(env, alice, path, case):
    app = env.c3_app()
    spec = dict(ADMISSION[case])
    status, code = spec.pop("expect")
    if spec.get("csrf") == "expired":
        spec["csrf"] = _csrf_expired()
    body = turn_fields() if path == TURNS else identity_fields(uuid.uuid4(), uuid.uuid4())
    assert_error(post(app, path, body_of(body), c3_headers(alice, **spec)), status, code)
    runner_untouched(env)
    assert env.provider_calls() == []


@pytest.mark.parametrize("path", [TURNS, REPLAY, ACK])
def test_selector_cookie_and_query_refusals(env, alice, path):
    app = env.c3_app()
    body = body_of(turn_fields() if path == TURNS else identity_fields(uuid.uuid4(), uuid.uuid4()))
    other = env.confirmed(BOB_EMAIL, USER_B)
    env.hook_calls.clear()
    upper = c3_headers(alice)
    upper["x-oryx-generation-id"] = str(alice.generation_id).upper()
    missing = {k: v for k, v in c3_headers(alice).items() if k != "x-oryx-generation-id"}
    cases = [
        (upper, 400, "invalid_request"),
        (missing, 400, "invalid_request"),
        (c3_headers(alice, cookie=lambda b: f"{b}; {active_cookie(alice)}; {active_cookie(alice)}"), 400,
         "invalid_request"),
        # Cookie actif d'une AUTRE génération : jamais choisi implicitement.
        (c3_headers(generation_id=alice.generation_id, cookie=lambda b: f"{b}; {active_cookie(other)}"), 401,
         "session_invalid"),
        (c3_headers(generation_id=alice.generation_id), 401, "session_invalid"),
    ]
    for headers, status, code in cases:
        assert_error(post(app, path, body, headers), status, code)
    assert_error(post(app, path + "?user_id=user-b", body, c3_headers(alice)), 400, "invalid_request")
    duplicated = list(c3_headers(alice).items()) + [("x-oryx-generation-id", str(alice.generation_id))]
    response = raw_request(app, method="POST", path=path, headers=[("host", "oryx.test")] + duplicated, chunks=[body])
    assert response.status_code == 400 and response.json() == {"error": "invalid_request"}
    two_cookie_headers = list(c3_headers(alice).items()) + [("cookie", "a=b")]
    response = raw_request(app, method="POST", path=path, headers=[("host", "oryx.test")] + two_cookie_headers,
                           chunks=[body])
    assert response.status_code == 400
    runner_untouched(env)


BODY_REFUSALS = {
    "user_id_added": lambda f: body_of({**f, "user_id": USER_B}),
    "extra_field": lambda f: body_of({**f, "state": "finalized"}),
    "missing_field": lambda f: body_of({k: v for k, v in f.items() if k != "context"}),
    "lease_token_added": lambda f: body_of({**f, "lease_token": "x"}),
    "duplicate_key": lambda f: body_of(f)[:-1] + b', "question": "autre"}',
    "malformed": lambda f: body_of(f)[:-1],
    "invalid_utf8": lambda f: body_of({**f, "question": "QQ"}).replace(b"QQ", b"\xff\xfe"),
    "bom": lambda f: b"\xef\xbb\xbf" + body_of(f),
    "nan_constant": lambda f: body_of(f)[:-1] + b', "x": NaN}',
    "number_value": lambda f: body_of({**f, "last_method_id": 1}),
    "array_root": lambda f: body_of([f]),
    "uuid_uppercase": lambda f: body_of({**f, "client_turn_id": f["client_turn_id"].upper()}),
    "uuid_braces": lambda f: body_of({**f, "client_turn_id": "{" + f["client_turn_id"] + "}"}),
    "uuid_number": lambda f: body_of({**f, "client_turn_id": None}),
    "conversation_padded": lambda f: body_of({**f, "conversation_key": " conv"}),
    "ticker_empty": lambda f: body_of({**f, "ticker_input": "   "}),
    "question_type": lambda f: body_of({**f, "question": ["x"]}),
    "question_too_long": lambda f: body_of({**f, "question": "é" * 8193}),
    "context_too_long": lambda f: body_of({**f, "context": "x" * (32 * 1024 + 1)}),
    "texts_too_long": lambda f: body_of({**f, "question": "x" * (16 * 1024), "context": "y" * (32 * 1024 + 1)}),
    "nul_character": lambda f: body_of({**f, "question": "a\u0000b"}),
    "lone_surrogate": lambda f: body_of(f).replace(b'"Pourquoi ?"', b'"\\ud800"'),
    "last_method_too_long": lambda f: body_of({**f, "last_method_id": "m" * 65}),
}


@pytest.mark.parametrize("case", sorted(BODY_REFUSALS))
def test_turn_bodies_outside_the_contract_are_invalid_requests(env, alice, case):
    app = env.c3_app()
    content = BODY_REFUSALS[case](turn_fields())
    assert_error(post(app, TURNS, content, c3_headers(alice)), 400, "invalid_request")
    runner_untouched(env)


@pytest.mark.parametrize("path", [REPLAY, ACK])
@pytest.mark.parametrize("case", ["user_id_added", "extra", "missing", "uuid_upper", "uuid_int", "dup", "conv_type",
                                  "conv_padded", "nan"])
def test_identity_bodies_outside_the_contract_are_invalid_requests(env, alice, path, case):
    app = env.c3_app()
    fields = identity_fields(uuid.uuid4(), uuid.uuid4())
    content = {
        "user_id_added": lambda: body_of({**fields, "user_id": USER_A}),
        "extra": lambda: body_of({**fields, "delivery_status": "delivered"}),
        "missing": lambda: body_of({k: v for k, v in fields.items() if k != "conversation_key"}),
        "uuid_upper": lambda: body_of({**fields, "reservation_id": fields["reservation_id"].upper()}),
        "uuid_int": lambda: body_of({**fields, "assistant_turn_id": 12}),
        "dup": lambda: body_of(fields)[:-1] + b', "reservation_id": "' + fields["reservation_id"].encode() + b'"}',
        "conv_type": lambda: body_of({**fields, "conversation_key": None}),
        "conv_padded": lambda: body_of({**fields, "conversation_key": "conv "}),
        "nan": lambda: body_of(fields)[:-1] + b', "x": Infinity}',
    }[case]()
    assert_error(post(app, path, content, c3_headers(alice)), 400, "invalid_request")
    runner_untouched(env)


def test_escaped_json_expansion_fits_the_transport_bound_without_raising_b1_bounds(env, alice):
    app = env.c3_app()
    # 16 Kio + 32 Kio UTF-8 EXACTS, écrits en échappements \\uXXXX (6 octets par caractère).
    fields = turn_fields(question="\u0001" * (16 * 1024), context="\u0002" * (32 * 1024))
    content = json.dumps(fields).encode("ascii")
    assert 4096 * 70 < len(content) <= c3.TURN_MAX_BODY_BYTES
    response = turn(env, app, alice, fields)
    assert response.status_code == 200, response.content[:200]  # admis : bornes métier B1 exactes
    over = turn_fields(question="\u0001" * (16 * 1024 + 1), context="")
    assert_error(turn(env, app, alice, over), 400, "invalid_request")  # borne métier B1 inchangée


def _padded_to(body: bytes, size: int) -> bytes:
    """Corps JSON valide de `size` octets EXACTS (clé supplémentaire de bourrage)."""
    head, tail = body[:-1] + b',"pad":"', b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


def test_body_size_is_judged_on_the_received_stream(env, alice):
    app = env.c3_app()
    headers = c3_headers(alice)
    limit = c3.TURN_MAX_BODY_BYTES
    padded = body_of(turn_fields())
    exact = _padded_to(padded, limit)
    assert len(exact) == limit
    assert_error(raw_post(app, TURNS, headers, [exact]), 400, "invalid_request")  # admis au transport, clé refusée
    too_big = _padded_to(padded, limit + 1)
    assert_error(raw_post(app, TURNS, headers, [too_big[:200000], too_big[200000:]]), 413, "request_too_large")
    assert_error(raw_post(app, TURNS, {**headers, "content-length": str(limit + 1)}, [b"{}"]), 413,
                 "request_too_large")
    assert_error(raw_post(app, TURNS, {**headers, "content-length": "10"}, [padded]), 400, "invalid_request")
    assert_error(raw_post(app, TURNS, {**headers, "content-length": str(len(padded) + 5)}, [padded]), 400,
                 "invalid_request")  # flux tronqué
    assert_error(raw_post(app, TURNS, {**headers, "content-length": "-1"}, [padded]), 400, "invalid_request")
    assert_error(raw_post(app, TURNS, {**headers, "transfer-encoding": "chunked"}, [padded]), 400, "invalid_request")
    assert_error(raw_post(app, TURNS, headers, [padded[:20]], disconnect=True), 400, "invalid_request")
    for path in (REPLAY, ACK):
        identity = body_of(identity_fields(uuid.uuid4(), uuid.uuid4()))
        assert_error(raw_post(app, path, headers, [_padded_to(identity, 4096)]), 400, "invalid_request")
        assert_error(raw_post(app, path, headers, [_padded_to(identity, 4097)]), 413, "request_too_large")
        assert_error(raw_post(app, path, {**headers, "content-length": "4097"}, [b"{}"]), 413, "request_too_large")
        assert_error(raw_post(app, path, headers, [_padded_to(identity, 5000)[:3000],
                                                   _padded_to(identity, 5000)[3000:]]), 413, "request_too_large")
    runner_untouched(env)


def test_methods_other_than_post_are_refused_by_the_router(env, alice):
    app = env.c3_app()
    for path in (TURNS, REPLAY, ACK):
        for method in ("GET", "PUT", "DELETE"):
            response = me_request(app, method=method, path=path, headers=c3_headers(alice))
            assert response.status_code == 405, (method, path)
            assert_private(response)
    preflight = me_request(app, method="OPTIONS", path=TURNS, headers={
        "origin": "https://evil.test", "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in preflight.headers
    runner_untouched(env)


def test_a_revoked_session_is_session_invalid_and_reserves_nothing(env, alice):
    app = env.c3_app()
    env.service.revoke_session(alice.session_id, "logout")
    assert_error(turn(env, app, alice, turn_fields()), 401, "session_invalid")
    assert env.count("decryptage_turn_reservations") == 0 and env.provider_calls() == []


def test_logs_never_carry_the_request_contents(env, alice, caplog):
    app = env.c3_app()
    caplog.set_level(logging.DEBUG)
    marker = "QUESTION-SECRETE-7f3a"
    data = delivered_ok(turn(env, app, alice, turn_fields(question=marker, context="CONTEXTE-SECRET")), env)
    post(app, ACK, body_of(ids_of(data)), c3_headers(alice))
    text = caplog.text
    for secret in (marker, "CONTEXTE-SECRET", alice.active_secret.reveal(), "Commençons"):
        assert secret not in text, secret  # aucun contenu, aucun secret, dans AUCUN journal
    c3_lines = "\n".join(r.getMessage() for r in caplog.records if r.name == "oryx.authz_f2.http")
    for identifier in (CONV, data["assistant_turn_id"], data["reservation_id"], str(alice.generation_id), USER_A):
        assert identifier not in c3_lines, identifier  # C3 : route, statut, code seulement
    assert "authz_f2 http decryptage.turns status=200" in c3_lines and "authz_f2 http decryptage.ack status=200" in c3_lines


# --------------------------------------------------------------------------
# 3. Matrice B1
# --------------------------------------------------------------------------

def _reserve_directly(env, issued, fields):
    """Réservation B1 committée par un AUTRE exécutant (réponse perdue)."""
    command = reserve_command(uuid.UUID(fields["client_turn_id"]), fields["conversation_key"], fields["ticker_input"],
                              fields["question"], fields["context"], fields["last_method_id"])
    outcome = env.run(issued, command)
    assert outcome.status == AUTHORIZED and type(outcome.value) is dr.LeaseGranted
    return outcome.value.lease


def test_an_active_lease_is_202_in_progress_without_generation(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    lease = _reserve_directly(env, alice, fields)
    before = env.reservation(lease.reservation_id)
    response = turn(env, app, alice, fields)
    assert response.status_code == 202 and set(response.json()) == {"status", "reservation_id", "lease_expires_at"}
    assert_private(response)
    body = response.json()
    assert body["status"] == "in_progress" and body["reservation_id"] == str(lease.reservation_id)
    expected = before["lease_expires_at"].astimezone(__import__("datetime").timezone.utc)
    assert body["lease_expires_at"] == expected.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert len(body["lease_expires_at"]) == 27 and body["lease_expires_at"].endswith("Z")
    assert "retry-after" not in response.headers
    assert env.provider_calls() == [] and env.reservation(lease.reservation_id)["xmin"] == before["xmin"]


def test_a_deferred_retry_is_202_retry_deferred(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    lease = _reserve_directly(env, alice, fields)
    failed = env.run(alice, c.RecordDecryptageAttemptFailure.from_lease(lease, dr.GENERATION_UNAVAILABLE))
    assert failed.status == AUTHORIZED
    row = env.reservation(lease.reservation_id)
    response = turn(env, app, alice, fields)
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"status", "reservation_id", "retry_not_before_at"} and body["status"] == "retry_deferred"
    at = row["retry_not_before_at"].astimezone(__import__("datetime").timezone.utc)
    assert body["retry_not_before_at"] == at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert env.provider_calls() == []


def test_a_new_attribution_after_expiry_is_an_active_resumption(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    lease = _reserve_directly(env, alice, fields)
    env.expire(lease.reservation_id)
    data = delivered_ok(turn(env, app, alice, fields), env)
    row = env.reservation(lease.reservation_id)
    assert (row["lease_epoch"], row["attempts_used"]) == (2, 2) and data["reservation_id"] == str(lease.reservation_id)


def test_the_same_turn_with_another_intention_is_an_idempotency_conflict(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    delivered_ok(turn(env, app, alice, fields), env)
    before = env.full_snapshot()
    for change in ({"question": "Autre ?"}, {"ticker_input": "AIR.PA"}, {"context": "x"}, {"last_method_id": "m"}):
        assert_error(turn(env, app, alice, {**fields, **change}), 409, "idempotency_conflict")
    assert env.full_snapshot() == before and env.claude.methods() == ["create_message"]


def test_three_failed_attributions_block_the_turn_terminally(env, alice):
    clock = env.transports(claude=None)
    env.claude = FakeClaude(clock, responses={"create_message": ConnectionError("Claude indisponible")})
    app = env.c3_app()
    fields = turn_fields()
    for attempt in (1, 2, 3):
        assert_error(turn(env, app, alice, fields), 503, "provider_unavailable")  # échec déclaré
        [row] = env.reservations()
        if attempt < 3:
            assert (row["state"], row["attempts_used"]) == ("reserved", attempt)
            assert turn(env, app, alice, fields).json()["status"] == "retry_deferred"
            env.end_backoff(row["id"])
    [row] = env.reservations()
    assert (row["state"], row["blocked_reason"]) == ("blocked", "attempts_exhausted")
    assert_error(turn(env, app, alice, fields), 409, "reservation_blocked")
    assert env.claude.methods() == ["create_message"] * 3  # jamais un nouveau tour implicite


def test_a_legacy_collision_blocks_without_any_provider(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    env.claim_legacy(USER_A, CONV, uuid.UUID(fields["client_turn_id"]))
    assert_error(turn(env, app, alice, fields), 409, "reservation_blocked")
    [row] = env.reservations()
    assert (row["state"], row["blocked_reason"]) == ("blocked", "legacy_collision") and env.provider_calls() == []
    assert_error(turn(env, app, alice, fields), 409, "reservation_blocked")  # aucun nouveau tour automatique


def test_another_owners_conversation_is_not_found(env, alice, bob):
    app = env.c3_app()
    fields = turn_fields()
    data = delivered_ok(turn(env, app, alice, fields), env)
    before = env.full_snapshot()
    assert_error(turn(env, app, bob, fields), 404, "not_found")
    assert_error(turn(env, app, bob, {**fields, "client_turn_id": str(uuid.uuid4())}), 404, "not_found")
    for path in (REPLAY, ACK):
        assert_error(post(app, path, body_of(ids_of(data)), c3_headers(bob)), 404, "not_found")
    assert env.full_snapshot() == before


def test_an_unavailable_reservation_is_503_and_writes_nothing(env, alice):
    def fail(point):
        if point == "prevalidated":
            raise RuntimeError("panne simulée")

    app = env.c3_app(hook=fail)
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    assert env.count("decryptage_turn_reservations") == 0 and env.provider_calls() == []


def _stub_outcome(monkeypatch, command_type, make):
    original = AuthorizedTransactionRunner.run

    def run(self, credential, command):
        outcome = original(self, credential, command)
        return make(outcome) if type(command) is command_type else outcome

    monkeypatch.setattr(AuthorizedTransactionRunner, "run", run)


@pytest.mark.parametrize("status, expected", [
    (SESSION_INVALID, (401, "session_invalid")), (NOT_FOUND, (404, "not_found")),
    (IDEMPOTENCY_CONFLICT, (409, "idempotency_conflict")), (REVISION_CONFLICT, (409, "revision_conflict")),
    (UNAVAILABLE, (503, "temporarily_unavailable")), ("forged", (503, "temporarily_unavailable"))])
def test_the_closed_b1_status_matrix(env, alice, monkeypatch, status, expected):
    app = env.c3_app()
    _stub_outcome(monkeypatch, c.ReserveDecryptageTurn, lambda _: AuthorizationOutcome(status))
    assert_error(turn(env, app, alice, turn_fields()), *expected)
    assert env.provider_calls() == []


@pytest.mark.parametrize("forge", ["not_an_outcome", "unknown_view", "foreign_lease"])
def test_an_out_of_contract_b1_result_is_refused_closed(env, alice, monkeypatch, forge):
    app = env.c3_app()

    def make(outcome):
        if forge == "not_an_outcome":
            return {"status": AUTHORIZED}
        if forge == "unknown_view":
            return AuthorizationOutcome(AUTHORIZED, object(), outcome.principal)
        lease = outcome.value.lease
        other = dr.DecryptageLease(lease.reservation_id, lease.lease_epoch, lease.token, lease.lease_expires_at,
                                   "conv-other", lease.source_user_turn_id, 1)
        return AuthorizationOutcome(AUTHORIZED, dr.LeaseGranted(other, 1), outcome.principal)

    _stub_outcome(monkeypatch, c.ReserveDecryptageTurn, make)
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    assert env.provider_calls() == [] and env.count("assistant_deliveries") == 0


# --------------------------------------------------------------------------
# 4. Matrice B2
# --------------------------------------------------------------------------

def _lease_row(env):
    [row] = env.reservations()
    return row


def _no_declaration(env):
    row = _lease_row(env)
    assert (row["state"], row["attempts_used"], row["retry_not_before_at"]) == ("reserved", 1, None)
    assert row["lease_token_hash"] is not None  # bail jamais libéré artificiellement
    assert env.count("assistant_deliveries") == 0


@pytest.mark.parametrize("results", [[], [
    {"Code": "MC", "Exchange": "PA", "Name": "LVMH", "Type": "Common Stock", "Isin": "FR1"},
    {"Code": "MC", "Exchange": "PA", "Name": "LVMH", "Type": "Common Stock", "Isin": "FR2"}]],
    ids=["not_found", "ambiguous"])
def test_an_unidentified_instrument_is_422_without_declaration(env, alice, results):
    clock = env.transports()
    env.eodhd = FakeEodhd(clock, responses={"search": results})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields(ticker_input="ZZZZ")), 422, "instrument_unavailable")
    _no_declaration(env)
    assert env.provider_calls() == ["search"]


def test_a_context_change_between_the_reads_is_a_context_conflict(env, alice):
    clock = env.transports()
    env.eodhd = FakeEodhd(clock, actions={"fundamentals": lambda: env.execute(
        "UPDATE users SET level = 'avance' WHERE id = :u", u=USER_A)})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 409, "context_conflict")
    _no_declaration(env)
    assert env.claude.calls == []


def test_a_closed_preparation_refusal_is_a_context_conflict(env, alice):
    env.add_session()
    env.add_session(offset=60)
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 409, "context_conflict")
    _no_declaration(env)


def test_a_revocation_during_the_providers_is_session_invalid(env, alice):
    clock = env.transports()
    env.eodhd = FakeEodhd(clock, actions={"fundamentals": lambda: env.service.revoke_session(alice.session_id,
                                                                                             "logout")})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 401, "session_invalid")
    _no_declaration(env)


def test_an_exhausted_b2_budget_is_503_without_declaration(env, alice):
    env.transports()
    calls = []

    def slow(point):
        calls.append(point)
        if point == "executed" and calls.count("executed") == 2:  # ReadProfile de B2 (après B1)
            env.clock.advance(25.0)

    app = env.c3_app(hook=slow)
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    _no_declaration(env)
    assert env.provider_calls() == []


def test_an_expired_indicative_lease_is_503_without_declaration(env, alice):
    clock = env.transports()

    def shorten():
        env.force(_lease_row(env)["id"], "lease_expires_at = clock_timestamp() + interval '60 seconds'")

    env.eodhd = FakeEodhd(clock, actions={"search": shorten})
    env.claude = FakeClaude(clock, delays={"create_message": lambda allowance: allowance})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    row = _lease_row(env)
    assert row["retry_not_before_at"] is None and row["lease_token_hash"] is not None
    assert env.count("assistant_deliveries") == 0


def test_a_declared_provider_failure_is_503_provider_unavailable(env, alice):
    clock = env.transports()
    env.claude = FakeClaude(clock, responses={"create_message": TimeoutError("lent")})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 503, "provider_unavailable")
    row = _lease_row(env)
    assert (row["state"], row["lease_token_hash"], row["attempts_used"]) == ("reserved", None, 1)
    assert row["retry_not_before_at"] is not None


def test_a_refused_declaration_is_503_and_never_presumes_a_release(env, alice):
    clock = env.transports()

    def fail_and_expire():
        env.expire(_lease_row(env)["id"])
        raise TimeoutError("Claude lent")

    env.claude = FakeClaude(clock, actions={"create_message": fail_and_expire})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    row = _lease_row(env)
    assert row["lease_token_hash"] is not None and row["retry_not_before_at"] is None


def test_an_uncertain_declaration_is_503_temporarily_unavailable(env, alice, monkeypatch):
    clock = env.transports()
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)

    def fail():
        env.hooks["executed"] = lambda: armed.append(1)  # prochaine transaction exécutée : la déclaration
        raise ConnectionError("Claude indisponible")

    env.claude = FakeClaude(clock, actions={"create_message": fail})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    env.hooks.clear()
    row = _lease_row(env)
    assert row["lease_token_hash"] is None and row["retry_not_before_at"] is not None  # committée malgré l'erreur


B2_EXPECTED = {
    dg.INSTRUMENT_NOT_FOUND: (422, "instrument_unavailable"), dg.INSTRUMENT_AMBIGUOUS: (422, "instrument_unavailable"),
    dg.CONTEXT_CHANGED: (409, "context_conflict"), dg.SESSION_REFUSED: (401, "session_invalid"),
    dg.RESERVATION_NOT_FOUND: (404, "not_found"), dg.INTENT_MISMATCH: (409, "revision_conflict"),
    dg.LEASE_STALE: (409, "revision_conflict"), dg.BUDGET_EXHAUSTED: (503, "temporarily_unavailable"),
    dg.LEASE_EXPIRED: (503, "temporarily_unavailable"), dg.AUTHORIZATION_UNAVAILABLE: (503, "temporarily_unavailable"),
    dg.OWNER_CHANGED: (503, "temporarily_unavailable"), dg.LEASE_NOT_GRANTED: (503, "temporarily_unavailable"),
    dg.LEASE_MISMATCH: (503, "temporarily_unavailable"), dg.LEASE_ALREADY_USED: (503, "temporarily_unavailable"),
    **{reason: (409, "context_conflict") for reason in dc.REFUSAL_REASONS},
}


@pytest.mark.parametrize("reason", sorted(B2_EXPECTED))
def test_the_closed_b2_refusal_matrix_never_declares(env, alice, monkeypatch, caplog, reason):
    assert set(B2_EXPECTED) == set(dg.REFUSAL_REASONS)  # vocabulaire B2 entièrement inventorié
    monkeypatch.setattr(dg.DecryptageGenerationOrchestrator, "generate",
                        lambda self, *a: dg.GenerationOutcome(dg.REFUSED, reason))
    app = env.c3_app()
    caplog.set_level(logging.INFO)
    assert_error(turn(env, app, alice, turn_fields()), *B2_EXPECTED[reason])
    _no_declaration(env)
    alert = f"alert=b2_{reason}" in caplog.text
    assert alert == (reason in c3.B2_ALERT_REASONS)


@pytest.mark.parametrize("declaration, expected", [
    (dg.DECLARED, (503, "provider_unavailable")), (dg.DECLARATION_REFUSED, (503, "temporarily_unavailable")),
    (dg.DECLARATION_UNCERTAIN, (503, "temporarily_unavailable"))])
def test_the_closed_provider_failure_matrix(env, alice, monkeypatch, declaration, expected):
    monkeypatch.setattr(dg.DecryptageGenerationOrchestrator, "generate", lambda self, *a: dg.GenerationOutcome(
        dg.PROVIDER_FAILED, dr.GENERATION_UNAVAILABLE, declaration=declaration))
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), *expected)
    _no_declaration(env)  # C3 ne déclare jamais à la place de B2


def test_one_attribution_never_calls_claude_twice(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    delivered_ok(turn(env, app, alice, fields), env)
    for _ in range(3):
        assert turn(env, app, alice, fields).status_code == 200
    assert env.claude.methods() == ["create_message"]


# --------------------------------------------------------------------------
# 5. Matrice C1-B
# --------------------------------------------------------------------------

def test_a_context_change_during_claude_is_a_finalization_context_conflict(env, alice):
    clock = env.transports()
    env.claude = FakeClaude(clock, actions={"create_message": lambda: env.execute(
        "UPDATE users SET level_revision = level_revision + 1 WHERE id = :u", u=USER_A)})
    app = env.c3_app()
    response = turn(env, app, alice, turn_fields())
    assert_error(response, 409, "context_conflict")
    _no_declaration(env)
    assert b"business" not in response.content  # le candidat ne quitte jamais le serveur


def test_a_preparation_refusal_at_finalization_is_a_context_conflict(env, alice):
    env.add_session()
    clock = env.transports()
    env.claude = FakeClaude(clock, actions={"create_message": lambda: env.add_session(offset=60)})
    app = env.c3_app()
    assert_error(turn(env, app, alice, turn_fields()), 409, "context_conflict")
    _no_declaration(env)


def test_a_legacy_collision_during_claude_blocks_at_finalization(env, alice):
    clock = env.transports()
    fields = turn_fields()
    env.claude = FakeClaude(clock, actions={"create_message": lambda: env.claim_legacy(
        USER_A, CONV, uuid.UUID(fields["client_turn_id"]))})
    app = env.c3_app()
    assert_error(turn(env, app, alice, fields), 409, "reservation_blocked")
    row = _lease_row(env)
    assert (row["state"], row["blocked_reason"]) == ("blocked", "legacy_collision")
    assert_error(turn(env, app, alice, fields), 409, "reservation_blocked")


def test_a_revocation_during_b2_refuses_the_finalization(env, alice):
    clock = env.transports()
    env.claude = FakeClaude(clock, actions={"create_message": lambda: env.service.revoke_session(
        alice.session_id, "logout")})
    app = env.c3_app()
    response = turn(env, app, alice, turn_fields())
    assert_error(response, 401, "session_invalid")
    _no_declaration(env)
    assert b"LVMH" not in response.content


def test_a_commit_rejected_at_finalization_is_503_and_nothing_is_finalized(env, alice):
    clock = env.transports()

    def install():
        with env.observer.begin() as conn:
            conn.execute(sa.text("CREATE FUNCTION c3_reject() RETURNS trigger LANGUAGE plpgsql AS "
                                 "$$ BEGIN RAISE EXCEPTION 'c3 commit refusé'; END $$"))
            conn.execute(sa.text("CREATE CONSTRAINT TRIGGER c3_reject AFTER UPDATE ON decryptage_turn_reservations "
                                 "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION c3_reject()"))

    env.claude = FakeClaude(clock, actions={"create_message": install})
    app = env.c3_app()
    try:
        assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
        _no_declaration(env)
        assert env.count("decryptage_cognitive_links") == 0
    finally:
        with env.observer.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER IF EXISTS c3_reject ON decryptage_turn_reservations"))
            conn.execute(sa.text("DROP FUNCTION IF EXISTS c3_reject()"))


def _uncertain_finalization(env, monkeypatch):
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    clock = env.transports()
    # Après Claude, la prochaine transaction exécutée est la finalisation C1-B.
    env.claude = FakeClaude(clock, actions={"create_message": lambda: env.hooks.__setitem__(
        "executed", lambda: armed.append(1))})


def test_an_uncertain_finalization_commit_is_503_then_recovered_by_the_same_intention(env, alice, monkeypatch):
    _uncertain_finalization(env, monkeypatch)
    app = env.c3_app()
    fields = turn_fields()
    assert_error(turn(env, app, alice, fields), 503, "temporarily_unavailable")
    env.hooks.clear()
    assert _lease_row(env)["state"] == "finalized"  # committée malgré l'erreur : jamais un rollback prétendu
    data = delivered_ok(turn(env, app, alice, fields), env)  # même intention : ReservationFinalized -> C2
    assert data["delivery_status"] == "pending" and env.claude.methods() == ["create_message"]


def test_already_finalized_is_restored_by_c2(env, alice, monkeypatch):
    original = dg.DecryptageGenerationOrchestrator.generate

    def generate_then_finalize_elsewhere(self, credential, command, reserved):
        outcome = original(self, credential, command, reserved)
        done = env.run(alice, c.FinalizeDecryptageCandidate(outcome.candidate))  # autre exécutant, même candidat
        assert done.status == AUTHORIZED
        return outcome

    monkeypatch.setattr(dg.DecryptageGenerationOrchestrator, "generate", generate_then_finalize_elsewhere)
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    assert len(env.delivery_rows()) == 1 and data["delivery_status"] == "pending"


@pytest.mark.parametrize("status, expected", [
    (SESSION_INVALID, (401, "session_invalid")), (NOT_FOUND, (404, "not_found")),
    (REVISION_CONFLICT, (409, "revision_conflict")), (IDEMPOTENCY_CONFLICT, (409, "idempotency_conflict")),
    (UNAVAILABLE, (503, "temporarily_unavailable")), ("forged", (503, "temporarily_unavailable"))])
def test_the_closed_c1b_status_matrix(env, alice, monkeypatch, status, expected):
    app = env.c3_app()
    _stub_outcome(monkeypatch, c.FinalizeDecryptageCandidate, lambda _: AuthorizationOutcome(status))
    response = turn(env, app, alice, turn_fields())
    assert_error(response, *expected)
    assert b"LVMH" not in response.content  # aucun candidat restitué


# --------------------------------------------------------------------------
# 6. Replay et ACK C2
# --------------------------------------------------------------------------

def test_ack_is_explicit_idempotent_and_never_captures_twice(env, alice):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    delivery_id = uuid.UUID(data["assistant_turn_id"])
    assert env.delivery(delivery_id)["status"] == "pending"  # ni génération ni replay n'acquittent
    post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    assert env.delivery(delivery_id)["status"] == "pending"
    first = post(app, ACK, body_of(ids_of(data)), c3_headers(alice))
    assert first.json() == {"assistant_turn_id": data["assistant_turn_id"], "delivery_status": "delivered"}
    assert env.delivery(delivery_id)["status"] == "delivered"
    assert env.link(delivery_id)["capture_state"] == "captured"
    before, events = env.full_snapshot(), env.counts()["cognitive_events"]
    second = post(app, ACK, body_of(ids_of(data)), c3_headers(alice))
    assert second.status_code == 200 and second.content == first.content
    assert env.full_snapshot() == before and env.counts()["cognitive_events"] == events


def test_replay_writes_nothing_and_reads_the_current_status(env, alice):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    before = env.full_snapshot()
    for _ in range(3):
        response = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
        assert response.status_code == 200
    assert env.full_snapshot() == before and env.claude.methods() == ["create_message"]


def test_inconsistent_identities_are_revision_conflicts(env, alice):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    for change in ({"assistant_turn_id": str(uuid.uuid4())}, {"conversation_key": "conv-autre"}):
        for path in (REPLAY, ACK):
            assert_error(post(app, path, body_of({**ids_of(data), **change}), c3_headers(alice)), 409,
                         "revision_conflict")
    for path in (REPLAY, ACK):
        assert_error(post(app, path, body_of(identity_fields(uuid.uuid4(), uuid.uuid4())), c3_headers(alice)), 404,
                     "not_found")
    assert env.delivery(uuid.UUID(data["assistant_turn_id"]))["status"] == "pending"


def test_a_non_finalized_reservation_is_a_revision_conflict(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    lease = _reserve_directly(env, alice, fields)
    for path in (REPLAY, ACK):
        assert_error(post(app, path, body_of(identity_fields(lease.reservation_id, uuid.uuid4())),
                          c3_headers(alice)), 409, "revision_conflict")


def test_an_uncertain_ack_commit_is_503_and_a_new_explicit_ack_is_idempotent(env, alice, monkeypatch):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    env.hooks["executed"] = lambda: armed.append(1)
    assert_error(post(app, ACK, body_of(ids_of(data)), c3_headers(alice)), 503, "temporarily_unavailable")
    env.hooks.clear()
    delivery_id = uuid.UUID(data["assistant_turn_id"])
    assert env.delivery(delivery_id)["status"] == "delivered"  # committé malgré l'erreur
    before, events = env.full_snapshot(), env.counts()["cognitive_events"]
    again = post(app, ACK, body_of(ids_of(data)), c3_headers(alice))
    assert again.json() == {"assistant_turn_id": data["assistant_turn_id"], "delivery_status": "delivered"}
    assert env.full_snapshot() == before and env.counts()["cognitive_events"] == events


@pytest.mark.parametrize("path, command_type", [(REPLAY, c.ReadCanonicalDecryptageDelivery),
                                                (ACK, c.AcknowledgeDecryptageDelivery)])
@pytest.mark.parametrize("status, expected", [
    (SESSION_INVALID, (401, "session_invalid")), (NOT_FOUND, (404, "not_found")),
    (REVISION_CONFLICT, (409, "revision_conflict")), (UNAVAILABLE, (503, "temporarily_unavailable")),
    (IDEMPOTENCY_CONFLICT, (503, "temporarily_unavailable")), ("forged", (503, "temporarily_unavailable"))])
def test_the_closed_c2_status_matrix(env, alice, monkeypatch, path, command_type, status, expected):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    _stub_outcome(monkeypatch, command_type, lambda _: AuthorizationOutcome(status))
    assert_error(post(app, path, body_of(ids_of(data)), c3_headers(alice)), *expected)


def _replay_forgery(forge, value):
    if forge == "view_type":
        return object()
    if forge == "other_delivery":
        return c.CanonicalDecryptageDelivery(value.reservation_id, uuid.uuid4(), value.delivery_status,
                                             value.payload_json)
    if forge == "other_reservation":
        return c.CanonicalDecryptageDelivery(uuid.uuid4(), value.assistant_turn_id, value.delivery_status,
                                             value.payload_json)
    if forge == "payload_not_an_object":
        return c.CanonicalDecryptageDelivery(value.reservation_id, value.assistant_turn_id, value.delivery_status,
                                             b'["LVMH"]')
    return c.CanonicalDecryptageDelivery(value.reservation_id, value.assistant_turn_id, value.delivery_status,
                                         value.payload_json[:-1])  # JSON tronqué : jamais réparé


def _ack_forgery(forge, value):
    if forge == "view_type":
        return object()
    if forge == "other_delivery":
        return type(value)(uuid.uuid4(), "delivered", value.response_action)
    if forge == "other_shape":
        import dataclasses
        Shape = dataclasses.make_dataclass("DeliveryAcknowledged", [("assistant_turn_id", uuid.UUID),
                                                                     ("delivery_status", str)], frozen=True)
        return Shape(value.assistant_turn_id, "delivered")
    return value


@pytest.mark.parametrize("forge", ["view_type", "other_delivery", "other_reservation", "payload_not_an_object",
                                   "payload_truncated", "principal_generation"])
def test_an_out_of_contract_replay_result_is_503_without_content(env, alice, monkeypatch, forge):
    app = env.c3_app()
    fields = turn_fields()
    data = delivered_ok(turn(env, app, alice, fields), env)

    def make(outcome):
        principal = outcome.principal
        if forge == "principal_generation":
            principal = AuthorizedPrincipal(principal.user_id, principal.session_id, uuid.uuid4(),
                                            principal.auth_method)
            return AuthorizationOutcome(AUTHORIZED, outcome.value, principal)
        return AuthorizationOutcome(AUTHORIZED, _replay_forgery(forge, outcome.value), principal)

    _stub_outcome(monkeypatch, c.ReadCanonicalDecryptageDelivery, make)
    replayed = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    resumed = turn(env, app, alice, fields)  # même intention : ReservationFinalized -> le même contrôle C2
    for response in (replayed, resumed):
        assert_error(response, 503, "temporarily_unavailable")
        assert b"LVMH" not in response.content


@pytest.mark.parametrize("forge", ["view_type", "other_delivery", "other_shape", "principal_generation"])
def test_an_out_of_contract_ack_result_is_503(env, alice, monkeypatch, forge):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)

    def make(outcome):
        principal = outcome.principal
        if forge == "principal_generation":
            principal = AuthorizedPrincipal(principal.user_id, principal.session_id, uuid.uuid4(),
                                            principal.auth_method)
        return AuthorizationOutcome(AUTHORIZED, _ack_forgery(forge, outcome.value), principal)

    _stub_outcome(monkeypatch, c.AcknowledgeDecryptageDelivery, make)
    assert_error(post(app, ACK, body_of(ids_of(data)), c3_headers(alice)), 503, "temporarily_unavailable")


# --------------------------------------------------------------------------
# 7. Budget coopératif de 270 s
# --------------------------------------------------------------------------

def test_an_exhausted_budget_before_b1_reserves_nothing(env, alice, monkeypatch):
    app = env.c3_app()
    original = c3._submit

    async def late(work):
        env.clock.advance(c3.C3_BUDGET_SECONDS + 1.0)  # file d'attente de l'exécuteur
        return await original(work)

    monkeypatch.setattr(c3, "_submit", late)
    fields = turn_fields()
    assert_error(turn(env, app, alice, fields), 503, "temporarily_unavailable")
    runner_untouched(env)
    monkeypatch.setattr(c3, "_submit", original)
    delivered_ok(turn(env, app, alice, fields), env)  # perte avant B1 : la même intention crée la réservation


def test_less_than_255_seconds_before_b2_keeps_the_committed_lease(env, alice):
    executed = []

    def hook(point):
        if point == "executed" and not executed:
            executed.append(1)
            env.clock.advance(c3.C3_BUDGET_SECONDS - c3.MIN_BEFORE_GENERATION_SECONDS + 0.5)  # pendant B1

    env.transports()
    app = env.c3_app(hook=hook)
    fields = turn_fields()
    assert_error(turn(env, app, alice, fields), 503, "temporarily_unavailable")
    assert env.provider_calls() == []
    _no_declaration(env)  # bail committé, ni déclaration ni libération
    assert turn(env, app, alice, fields).json()["status"] == "in_progress"  # B1 gouverne la reprise
    env.expire(_lease_row(env)["id"])
    delivered_ok(turn(env, app, alice, fields), env)
    assert _lease_row(env)["lease_epoch"] == 2


def test_less_than_15_seconds_before_c1b_finalizes_nothing(env, alice, monkeypatch):
    original = dg.DecryptageGenerationOrchestrator.generate

    def slow(self, *args):
        outcome = original(self, *args)
        env.clock.advance(c3.C3_BUDGET_SECONDS)  # au-delà de toute marge
        return outcome

    monkeypatch.setattr(dg.DecryptageGenerationOrchestrator, "generate", slow)
    app = env.c3_app()
    response = turn(env, app, alice, turn_fields())
    assert_error(response, 503, "temporarily_unavailable")
    _no_declaration(env)
    assert b"LVMH" not in response.content


def test_less_than_5_seconds_after_c1b_is_a_prudent_503_and_the_commit_is_never_interrupted(env, alice):
    clock = env.transports()

    def arm():  # la prochaine transaction exécutée est C1-B : son COMMIT « dure » au-delà du budget
        env.hooks["executed"] = lambda: clock.advance(c3.C3_BUDGET_SECONDS)

    env.claude = FakeClaude(clock, actions={"create_message": arm})
    app = env.c3_app()
    fields = turn_fields()
    assert_error(turn(env, app, alice, fields), 503, "temporarily_unavailable")
    env.hooks.clear()
    assert _lease_row(env)["state"] == "finalized" and len(env.delivery_rows()) == 1  # COMMIT mené à terme
    data = delivered_ok(turn(env, app, alice, fields), env)
    assert data["delivery_status"] == "pending" and env.claude.methods() == ["create_message"]


def test_a_broken_clock_refuses_closed_before_any_reservation(env, alice):
    app = env.c3_app()
    env.clock.now = float("nan")
    assert_error(turn(env, app, alice, turn_fields()), 503, "temporarily_unavailable")
    runner_untouched(env)


# --------------------------------------------------------------------------
# 8. Continuité du principal
# --------------------------------------------------------------------------

def test_every_authorization_consumed_by_c3_is_the_same_principal(env, alice, monkeypatch):
    seen = []
    original = AuthorizedTransactionRunner.run

    def spy(self, credential, command):
        outcome = original(self, credential, command)
        seen.append((type(command).__name__, outcome.principal))
        return outcome

    monkeypatch.setattr(AuthorizedTransactionRunner, "run", spy)
    app = env.c3_app()
    delivered_ok(turn(env, app, alice, turn_fields()), env)
    names = [name for name, _ in seen]
    assert names == ["ReserveDecryptageTurn", "ReadProfile", "ReadDecryptagePreparationContext",
                     "ReadDecryptagePreparationContext", "FinalizeDecryptageCandidate",
                     "ReadCanonicalDecryptageDelivery"]  # une autorisation F2 complète par commande
    identities = {(p.user_id, p.session_id, p.generation_id) for _, p in seen}
    assert identities == {(USER_A, alice.session_id, alice.generation_id)}


@pytest.mark.parametrize("command_type", [c.ReserveDecryptageTurn, c.FinalizeDecryptageCandidate,
                                          c.ReadCanonicalDecryptageDelivery])
@pytest.mark.parametrize("field", ["session_id", "generation_id", "user_id"])
def test_a_divergent_principal_in_one_execution_is_503_without_content(env, alice, monkeypatch, command_type, field):
    def make(outcome):
        p = outcome.principal
        values = {"user_id": p.user_id, "session_id": p.session_id, "generation_id": p.generation_id}
        values[field] = USER_B if field == "user_id" else uuid.uuid4()
        return AuthorizationOutcome(outcome.status, outcome.value,
                                    AuthorizedPrincipal(values["user_id"], values["session_id"],
                                                        values["generation_id"], p.auth_method))

    _stub_outcome(monkeypatch, command_type, make)
    app = env.c3_app()
    response = turn(env, app, alice, turn_fields())
    # B1 : la génération est comparée au sélecteur et l'identité B1 devient la
    # RÉFÉRENCE (B2 revérifie le propriétaire : owner_changed) ; C1-B et C2
    # sont comparés à cette référence.
    assert_error(response, 503, "temporarily_unavailable")
    assert b"LVMH" not in response.content
    if command_type is c.ReserveDecryptageTurn and field == "generation_id":
        assert env.provider_calls() == []  # aucune génération


def test_resumption_under_a_new_session_of_the_same_owner(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    first = delivered_ok(turn(env, app, alice, fields), env)
    env.service.revoke_session(alice.session_id, "logout")
    assert_error(turn(env, app, alice, fields), 401, "session_invalid")
    again = env.confirmed("alice2@example.com", USER_A)  # nouvelle session F2, même propriétaire
    assert again.session_id != alice.session_id
    response = turn(env, app, again, fields)
    assert delivered_ok(response, env) == first
    assert env.claude.methods() == ["create_message"]


def test_resumption_under_another_owner_is_refused(env, alice, bob):
    app = env.c3_app()
    fields = turn_fields()
    lease = _reserve_directly(env, alice, fields)
    before = env.reservation(lease.reservation_id)
    assert_error(turn(env, app, bob, fields), 404, "not_found")
    assert env.reservation(lease.reservation_id)["xmin"] == before["xmin"] and env.provider_calls() == []


# --------------------------------------------------------------------------
# 9. Rotation HMAC B1
# --------------------------------------------------------------------------

def test_hmac_rotation_keeps_the_recorded_key_version(env, alice):
    fields = turn_fields()
    env.decryptage_keys = DecryptageKeyring({"d1": D1}, "d1")
    first = delivered_ok(turn(env, env.c3_app(), alice, fields), env)
    assert _lease_row(env)["fingerprint_key_version"] == "d1"
    # K1 -> K2, K1 conservée : la réservation K1 est revérifiée avec K1.
    env.decryptage_keys = DecryptageKeyring({"d1": D1, "d2": D2}, "d2")
    rotated = env.c3_app()
    assert delivered_ok(turn(env, rotated, alice, fields), env) == first
    assert env.claude.methods() == ["create_message"]  # aucun nouvel appel Claude
    fresh = turn_fields()
    delivered_ok(turn(env, rotated, alice, fresh), env)
    versions = {str(r["source_user_turn_id"]): r["fingerprint_key_version"] for r in env.reservations()}
    assert versions == {fields["client_turn_id"]: "d1", fresh["client_turn_id"]: "d2"}  # nouvelle : clé courante


def test_a_missing_historical_key_refuses_without_substitution(env, alice):
    fields = turn_fields()
    env.decryptage_keys = DecryptageKeyring({"d1": D1}, "d1")
    first = delivered_ok(turn(env, env.c3_app(), alice, fields), env)
    pending = turn_fields()
    _reserve_directly(env, alice, pending)  # réservation K1 encore active
    env.decryptage_keys = DecryptageKeyring({"d2": D2}, "d2")  # K1 supprimée
    without_k1 = env.c3_app()
    before = env.full_snapshot()
    for f in (fields, pending):
        assert_error(turn(env, without_k1, alice, f), 503, "temporarily_unavailable")
    assert env.full_snapshot() == before  # aucun nouveau bail, aucune substitution K2, aucune livraison
    assert {r["fingerprint_key_version"] for r in env.reservations()} == {"d1"}
    # Identifiants connus : replay C2 SANS recalcul du HMAC B1, sous nouvelle autorisation F2.
    replayed = post(without_k1, REPLAY, body_of(ids_of(first)), c3_headers(alice))
    assert replayed.status_code == 200 and replayed.json() == first
    assert env.claude.methods() == ["create_message"]


# --------------------------------------------------------------------------
# 10. Récupération après incident
# --------------------------------------------------------------------------

def test_loss_after_b1_commit_finds_the_reservation_and_grants_no_second_lease(env, alice):
    app = env.c3_app()
    fields = turn_fields()
    lease = _reserve_directly(env, alice, fields)  # réponse B1 perdue
    for _ in range(3):
        assert turn(env, app, alice, fields).json() == {
            "status": "in_progress", "reservation_id": str(lease.reservation_id),
            "lease_expires_at": c3.rfc3339_utc(env.reservation(lease.reservation_id)["lease_expires_at"])}
    row = env.reservation(lease.reservation_id)
    assert (row["lease_epoch"], row["attempts_used"]) == (1, 1) and env.provider_calls() == []


def test_loss_during_replay_is_a_new_read_and_loss_after_ack_a_new_explicit_ack(env, alice):
    app = env.c3_app()
    data = delivered_ok(turn(env, app, alice, turn_fields()), env)
    assert post(app, ACK, body_of(ids_of(data)), c3_headers(alice)).status_code == 200  # réponse « perdue »
    before, events = env.full_snapshot(), env.counts()["cognitive_events"]
    replay = post(app, REPLAY, body_of(ids_of(data)), c3_headers(alice))
    assert replay.json()["delivery_status"] == "delivered"
    assert post(app, ACK, body_of(ids_of(data)), c3_headers(alice)).status_code == 200
    assert env.full_snapshot() == before and env.counts()["cognitive_events"] == events
