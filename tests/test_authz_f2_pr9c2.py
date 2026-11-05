"""R1 PR 9-C2 — replay canonique et ACK F2 Décrypter, contre un VRAI
PostgreSQL (16 et 18 en CI), sous le VRAI runner et le registre de PRODUCTION
(vingt commandes), SANS trousseau Décrypter, avec des livraisons produites par
la VRAIE chaîne B1 -> B2 -> C1-B (transports simulés, aucun réseau).

Chaque état durable est observé par une connexion indépendante ; « état
inchangé » = empreinte complète des tables Décrypter, réservations et xmin
compris (aucune écriture durable).

1. replay : contenu exact, statut pending / delivered, octets stables, une
   seule instruction, aucune écriture, aucun verrou, aucun fournisseur ;
2. propriété et provenance : matrice fermée des refus (NOT_FOUND,
   REVISION_CONFLICT, UNAVAILABLE), pour le replay ET l'ACK ; aucun verrou ni
   accès sur une ressource étrangère ;
3. JSONB : valeurs persistées (jamais les octets d'origine), nombres en
   Decimal, Unicode, échappements, bornes, champs, empreinte visible ;
4. ACK et attestation : capture V2 et pending -> delivered atomiques, ACK
   rejoué no-op, stale_delivery conservé, attestation scellée refusée hors
   contrat AVANT toute capture, chemin historique intact ;
5. atomicité et récupération : échec à chaque phase, COMMIT refusé ou
   incertain, session invalide, isolation, transaction perdue.
"""
import copy
import dataclasses
import hashlib
import json
import logging
import pickle
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from core import assistant_delivery as ad
from core import decryptage_cognitive_runtime as dcr
from core.authz_f2 import _adapters
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2.principal import AuthorizedContext, AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, REVISION_CONFLICT, SESSION_INVALID, UNAVAILABLE
from tests.authz_f2_pr9a_support import BOB_EMAIL, USER_A, USER_B
from tests.authz_f2_pr9b1_support import CONV
from tests.authz_f2_pr9c1b_support import next_turn, schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c2_support import CanonicalEnv
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

V2 = dcr.COGNITIVE_RUNTIME_VERSION_V2


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = CanonicalEnv(pg_url)
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


def reply_for(step):
    return f"Étape {step}.\n<!--ORYX_STEP:{step}-->"


def replayed(env, issued, done, **changes):
    outcome = env.c2(issued, done.replay(**changes))
    assert outcome.status == AUTHORIZED and type(outcome.value) is c.CanonicalDecryptageDelivery, outcome
    return outcome.value


def acked(env, issued, done, **changes):
    outcome = env.c2(issued, done.ack(**changes))
    assert outcome.status == AUTHORIZED and type(outcome.value) is bridge.DeliveryAcknowledged, outcome
    return outcome.value


def command_of(kind, done, **changes):
    return done.replay(**changes) if kind == "replay" else done.ack(**changes)


def statements_log(engine):
    log = []
    sa.event.listen(engine, "before_cursor_execute", lambda conn, cur, st, *a: log.append(" ".join(st.split())))
    return log


def refused(env, issued, command, expected, **kwargs):
    """Refus fermé : statut attendu, aucune valeur, aucun principal, rien d'écrit."""
    before = env.full_snapshot()
    outcome = env.c2(issued, command, **kwargs)
    assert (outcome.status, outcome.value, outcome.principal) == (expected, None, None), outcome
    assert env.full_snapshot() == before
    return outcome


# --------------------------------------------------------------------------
# 1. Replay canonique
# --------------------------------------------------------------------------

def test_replay_of_a_pending_delivery_is_the_exact_persisted_content(env, alice):
    done = env.finalized(alice)
    before = env.full_snapshot()
    view = replayed(env, alice, done)
    assert (view.reservation_id, view.assistant_turn_id, view.delivery_status) == (
        done.reservation_id, done.assistant_turn_id, "pending")
    assert view.payload_json == env.payload_text(done.assistant_turn_id).encode("utf-8")  # texte JSONB exact
    assert json.loads(view.payload_json) == done.candidate.payload.as_dict()  # valeurs du candidat finalisé
    assert env.full_snapshot() == before  # aucune écriture, aucune progression, aucun ACK implicite


def test_replay_after_the_ack_keeps_the_content_and_reports_delivered(env, alice):
    done = env.finalized(alice)
    pending = replayed(env, alice, done)
    acked(env, alice, done)
    delivered = replayed(env, alice, done)
    assert delivered.delivery_status == "delivered" and delivered.payload_json == pending.payload_json


def test_two_replays_return_the_same_bytes_and_write_nothing(env, alice):
    done = env.finalized(alice)
    before = env.full_snapshot()
    first, second = replayed(env, alice, done), replayed(env, alice, done)
    assert first == second and first.payload_json == second.payload_json
    assert env.full_snapshot() == before


def test_replay_is_one_read_only_statement_without_any_provider(env, alice):
    done = env.finalized(alice)
    calls = env.provider_calls()
    log = statements_log(env.engine)
    view = replayed(env, alice, done)
    business = [s for s in log if s.startswith("WITH o AS (SELECT CAST(")]
    assert len(business) == 1 and "d.response_payload::text AS d_payload" in business[0]
    joined = "\n".join(log)
    for forbidden in ("FOR UPDATE", "FOR NO KEY UPDATE", "SAVEPOINT", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in joined, forbidden  # plan READ : FOR KEY SHARE / FOR SHARE du runner seulement
    assert env.provider_calls() == calls  # aucun fournisseur (Claude, EODHD, FMP)
    assert view.delivery_status == "pending"


def test_replay_needs_no_decryptage_keyring_and_ignores_one(env, alice):
    done = env.finalized(alice)
    without = env.c2(alice, done.replay(), decryptage_keys=None)
    with_keys = env.run(alice, done.replay())
    assert without.status == with_keys.status == AUTHORIZED and without.value == with_keys.value


def test_the_view_is_immutable_and_never_shows_its_content(env, alice):
    done = env.finalized(alice)
    view = replayed(env, alice, done)
    text = json.loads(view.payload_json)["analysis"]
    for shown in (repr(view), str(view), f"{view}"):
        assert text not in shown and "payload_json=<private>" in shown and "disclaimer" not in shown
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.payload_json = b"{}"
    assert type(view.payload_json) is bytes and [f.name for f in dataclasses.fields(view)] == [
        "reservation_id", "assistant_turn_id", "delivery_status", "payload_json"]
    for bad in (dict(payload_json="{}"), dict(delivery_status="read"), dict(reservation_id=str(done.reservation_id)),
                dict(payload_json=b"")):
        with pytest.raises(ValueError):
            dataclasses.replace(view, **bad)


# --------------------------------------------------------------------------
# 2. Propriété, état B1, identité demandée et provenance (replay ET ACK)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["replay", "ack"])
def test_an_absent_reservation_is_not_found(env, alice, kind):
    done = env.finalized(alice)
    refused(env, alice, command_of(kind, done, reservation_id=uuid.uuid4()), NOT_FOUND)


@pytest.mark.parametrize("kind", ["replay", "ack"])
def test_a_foreign_reservation_is_not_found_without_lock_nor_access(env, alice, bob, kind):
    theirs = env.finalized(bob, conversation_key="conv-bob")
    log = statements_log(env.engine)
    with env.observer.connect() as holder:  # un tiers tient la réservation et la livraison d'autrui
        holder.execute(sa.text("BEGIN"))
        holder.execute(sa.text("SELECT 1 FROM decryptage_turn_reservations WHERE id = :i FOR UPDATE"),
                       {"i": theirs.reservation_id})
        holder.execute(sa.text("SELECT 1 FROM assistant_deliveries WHERE id = :i FOR UPDATE"),
                       {"i": theirs.assistant_turn_id})
        try:
            outcome = env.c2(alice, command_of(kind, theirs))  # aucune attente : rien d'autrui n'est verrouillé
        finally:
            holder.execute(sa.text("ROLLBACK"))
    assert (outcome.status, outcome.value) == (NOT_FOUND, None)
    assert not [s for s in log if "FOR UPDATE" in s and "decryptage_turn_reservations" not in s]
    # ACK : la réservation d'autrui n'est même pas relue ; replay : seule sa
    # propriété (booléen) est projetée, jamais ses colonnes ni sa livraison.
    assert len([s for s in log if s.startswith("WITH o AS")]) == (0 if kind == "ack" else 1)
    assert env.delivery(theirs.assistant_turn_id)["status"] == "pending"


@pytest.mark.parametrize("kind", ["replay", "ack"])
@pytest.mark.parametrize("case", ["foreign_delivery", "other_conversation", "other_uuid", "unlinked_legacy"])
def test_an_identity_other_than_the_b1_link_is_a_revision_conflict(env, alice, bob, kind, case):
    done = env.finalized(alice)
    if case == "foreign_delivery":
        changes = {"assistant_turn_id": env.finalized(bob, conversation_key="conv-bob").assistant_turn_id}
    elif case == "other_conversation":
        changes = {"conversation_key": "conv-autre"}
    elif case == "other_uuid":
        changes = {"assistant_turn_id": uuid.uuid4()}
    else:  # livraison historique de la même conversation, jamais liée à B1 : jamais adoptée
        changes = {"assistant_turn_id": env.claim_legacy(USER_A, CONV, uuid.uuid4())}
    refused(env, alice, command_of(kind, done, **changes), REVISION_CONFLICT)
    assert env.link(done.assistant_turn_id)["capture_state"] == "awaiting_delivery"


@pytest.mark.parametrize("kind", ["replay", "ack"])
@pytest.mark.parametrize("state", ["reserved", "blocked"])
def test_a_reservation_that_is_not_finalized_is_a_revision_conflict(env, alice, kind, state):
    _, reserved = env.reserve(alice, next_turn())
    reservation_id = reserved.value.lease.reservation_id
    if state == "blocked":
        env.force(reservation_id, "state = 'blocked', blocked_reason = 'attempts_exhausted', "
                                  "lease_token_hash = NULL, lease_expires_at = NULL")
    command = (c.ReadCanonicalDecryptageDelivery if kind == "replay" else c.AcknowledgeDecryptageDelivery)(
        reservation_id, CONV, uuid.uuid4())
    refused(env, alice, command, REVISION_CONFLICT)


def _damage_for(env, done, bob_done, case):
    d, r = done.assistant_turn_id, done.reservation_id
    if case == "missing_delivery":
        env.damage("DELETE FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d", d=d)
        env.damage("DELETE FROM assistant_deliveries WHERE id = :d", d=d)
    elif case == "linked_foreign_delivery":  # lien B1 vers une livraison d'autrui : jamais lue
        theirs = env.claim_legacy(USER_B, bob_done.conversation_key, uuid.uuid4())
        env.force(r, f"assistant_delivery_id = '{theirs}'")
        return {"assistant_turn_id": theirs}
    elif case == "artificially_linked_legacy":  # identité logique identique, runtime absent
        turn = uuid.uuid4()
        legacy = env.claim_legacy(USER_A, CONV, turn)
        env.force(r, f"source_user_turn_id = '{turn}', assistant_delivery_id = '{legacy}'")
        return {"assistant_turn_id": legacy}
    elif case == "runtime_absent":
        env.damage("UPDATE assistant_deliveries SET private_metadata = private_metadata - 'cognitive_runtime_version' "
                   "WHERE id = :d", d=d)
    elif case == "runtime_v1":
        env.damage("UPDATE assistant_deliveries SET private_metadata = jsonb_set(private_metadata, "
                   "'{cognitive_runtime_version}', to_jsonb(CAST(:v AS text))) WHERE id = :d", d=d,
                   v=dcr.COGNITIVE_RUNTIME_VERSION_V1)
    elif case == "runtime_not_a_string":
        env.damage("UPDATE assistant_deliveries SET private_metadata = jsonb_set(private_metadata, "
                   "'{cognitive_runtime_version}', '2') WHERE id = :d", d=d)
    elif case == "link_absent":
        env.damage("DELETE FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d", d=d)
    elif case == "link_version_v1":
        env.damage("UPDATE decryptage_cognitive_links SET capture_version = :v, product_progress_action = NULL "
                   "WHERE assistant_delivery_id = :d", d=d, v=dcr.COGNITIVE_RUNTIME_VERSION_V1)
        env.damage("UPDATE assistant_deliveries SET private_metadata = jsonb_set(private_metadata, "
                   "'{cognitive_runtime_version}', to_jsonb(CAST(:v AS text))) WHERE id = :d", d=d, v=V2)
    elif case == "pending_captured":
        env.damage("UPDATE decryptage_cognitive_links SET capture_state = 'captured', "
                   "response_action = 'no_cognitive_action', captured_at = clock_timestamp() "
                   "WHERE assistant_delivery_id = :d", d=d)
    elif case == "delivered_awaiting":
        env.damage("UPDATE assistant_deliveries SET status = 'delivered', delivered_at = clock_timestamp() "
                   "WHERE id = :d", d=d)
    elif case == "foreign_analysis_session":
        [theirs] = [s for s in env.sessions() if s["user_id"] == USER_B]
        env.damage("UPDATE assistant_deliveries SET analysis_session_id = :s WHERE id = :d", d=d, s=theirs["id"])
    elif case == "missing_analysis_session":
        env.damage("UPDATE assistant_deliveries SET analysis_session_id = :s WHERE id = :d", d=d, s=uuid.uuid4())
    elif case == "missing_ticker":
        env.force(r, "resolved_ticker = NULL")
    elif case == "other_turn_identity":
        env.damage("UPDATE assistant_deliveries SET source_user_turn_id = :t WHERE id = :d", d=d, t=uuid.uuid4())
    elif case == "unknown_schema":
        env.damage("UPDATE assistant_deliveries SET delivery_schema_version = 'assistant-delivery-v9' "
                   "WHERE id = :d", d=d)
    elif case == "foreign_delivery_owner":
        env.damage("UPDATE assistant_deliveries SET user_id = :b WHERE id = :d", d=d, b=USER_B)
    else:  # pragma: no cover
        raise AssertionError(case)
    return {}


PERSISTED_INCOHERENCES = ["missing_delivery", "linked_foreign_delivery", "artificially_linked_legacy",
                          "runtime_absent", "runtime_v1", "runtime_not_a_string", "link_absent", "link_version_v1",
                          "pending_captured", "delivered_awaiting", "foreign_analysis_session",
                          "missing_analysis_session", "missing_ticker", "other_turn_identity", "unknown_schema",
                          "foreign_delivery_owner"]


@pytest.mark.parametrize("kind", ["replay", "ack"])
@pytest.mark.parametrize("case", PERSISTED_INCOHERENCES)
def test_a_persisted_incoherence_is_unavailable_and_never_repaired(env, alice, bob, kind, case, monkeypatch):
    done = env.finalized(alice)
    bob_done = env.finalized(bob, conversation_key="conv-bob")
    changes = _damage_for(env, done, bob_done, case)
    captures = []
    monkeypatch.setattr(dcr, "capture_delivered_response", lambda *a, **k: captures.append(1))
    refused(env, alice, command_of(kind, done, **changes), UNAVAILABLE)
    assert captures == []  # jamais une capture sur une livraison non prouvée
    assert env.delivery(bob_done.assistant_turn_id)["status"] == "pending"


@pytest.mark.parametrize("kind", ["replay", "ack"])
def test_a_broken_transitive_provenance_is_unavailable(env, alice, bob, kind):
    """Livraison delivered + captured dont l'event de réponse est devenu
    celui d'un autre propriétaire : jamais restituée ni ré-acquittée."""
    done = env.finalized(alice)
    acked(env, alice, done)
    event = env.link(done.assistant_turn_id)["response_event_id"]
    env.damage("UPDATE cognitive_events SET user_id = :b WHERE id = :e", e=event, b=USER_B)
    refused(env, alice, command_of(kind, done), UNAVAILABLE)


def test_a_support_trace_with_a_foreign_parent_is_unavailable(env, alice):
    """Tour de continuation (SupportTrace) : l'event PARENT de l'aide fait
    partie de la provenance transitive contrôlée par le replay."""
    first = env.finalized(alice, text=reply_for("business"))
    acked(env, alice, first)
    second = env.finalized(alice, next_turn(CONV, question="Ils vendent du luxe.", context="h"),
                           text=reply_for("business"))
    assert acked(env, alice, second).response_action == "continued_event"
    trace = env.link(second.assistant_turn_id)["support_trace_id"]
    assert trace is not None and replayed(env, alice, second).delivery_status == "delivered"
    [parent] = env.rows("SELECT cognitive_event_id FROM support_traces WHERE id = :t", t=trace)
    env.damage("UPDATE cognitive_events SET user_id = :b WHERE id = :e", e=parent["cognitive_event_id"], b=USER_B)
    refused(env, alice, second.replay(), UNAVAILABLE)
    refused(env, alice, second.ack(), UNAVAILABLE)


# --------------------------------------------------------------------------
# 3. JSONB : valeurs persistées, jamais resérialisées
# --------------------------------------------------------------------------

def _persist(env, done, payload, *, fingerprint=None, raw=None):
    """Remplace le JSONB persisté (texte JSON donné) et, par défaut, l'empreinte
    visible cohérente avec son texte."""
    text = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    env.set_payload(done.assistant_turn_id, "CAST(:p AS jsonb)", p=text)
    analysis = payload.get("analysis") if isinstance(payload, dict) else None
    if fingerprint is None and isinstance(analysis, str):
        fingerprint = hashlib.sha256(analysis.encode("utf-8")).hexdigest()
    if fingerprint is not None:
        env.update("assistant_deliveries", "visible_content_fingerprint = :f", "id = :d", f=fingerprint,
                   d=done.assistant_turn_id)
    return env.payload_text(done.assistant_turn_id)


def _base(done):
    return done.candidate.payload.as_dict()


@pytest.mark.parametrize("price, expected", [
    (None, None), (600.5, Decimal("600.5")), (0.0, Decimal("0")), (-12.25, Decimal("-12.25")),
    (5e-324, Decimal(repr(5e-324))), (1.7976931348623157e308, Decimal(repr(1.7976931348623157e308))),
    (1e22, Decimal("1e22")), (1e-07, Decimal("1e-07")), (123456789.123, Decimal("123456789.123")),
], ids=["null", "decimal", "zero", "negative", "smallest_subnormal", "largest_finite", "big_integer_notation",
        "small_exponent_notation", "many_digits"])
def test_admissible_prices_are_restored_as_persisted_by_jsonb(env, alice, price, expected):
    done = env.finalized(alice)
    persisted = _persist(env, done, {**_base(done), "price": price})
    view = replayed(env, alice, done)
    assert view.payload_json == persisted.encode("utf-8")  # le texte JSONB, jamais les octets d'origine
    parsed = json.loads(view.payload_json, parse_float=Decimal, parse_int=Decimal)
    assert parsed["price"] == expected  # valeur persistée exacte, sans arrondi
    if price is not None:
        assert float(parsed["price"]) == price  # même binary64 que celui écrit par C1-B


def test_jsonb_may_change_the_notation_but_never_the_value(env, alice):
    done = env.finalized(alice)
    original = json.dumps({**_base(done), "price": 1e-07}, ensure_ascii=False)
    persisted = _persist(env, done, {**_base(done), "price": 1e-07})
    assert "1e-07" in original and "1e-07" not in persisted and "0.0000001" in persisted
    view = replayed(env, alice, done)
    assert view.payload_json == persisted.encode("utf-8") != original.encode("utf-8")
    assert json.loads(view.payload_json)["price"] == 1e-07


@pytest.mark.parametrize("raw_price", ["0.1000000000000000000001", "1e400", "-1e400", "600.50000000000000000001"],
                         ids=["not_a_binary64_decimal", "overflow", "negative_overflow", "rounded_away"])
def test_a_number_outside_the_c1b_domain_is_unavailable(env, alice, raw_price):
    done = env.finalized(alice)
    text = json.dumps({**_base(done), "price": "__PRICE__"}, ensure_ascii=False).replace('"__PRICE__"', raw_price)
    _persist(env, done, _base(done), raw=text)
    refused(env, alice, done.replay(), UNAVAILABLE)


@pytest.mark.parametrize("field, value", [
    ("price", True), ("price", False), ("price", "600.5"), ("price", [600.5]), ("price", {"v": 1}),
    ("success", False), ("success", "true"), ("success", 1), ("success", None),
    ("ticker", "AI.PA"), ("ticker", "mc.pa"), ("ticker", 1), ("name", ""), ("name", None),
    ("method_used", 1), ("method_used", ["construction_these"]), ("currency", "eur"), ("currency", "EURO"),
    ("currency", None), ("disclaimer", "Analyse."), ("disclaimer", None),
    ("analysis", ""), ("analysis", "   \n"), ("analysis", "Texte <!--ORYX_STEP:moat-->"),
    ("analysis", "Texte ORYX_STEP"), ("analysis", None), ("analysis", 12),
], ids=lambda v: repr(v)[:30])
def test_an_incoherent_public_field_is_unavailable(env, alice, field, value):
    done = env.finalized(alice)
    payload = {**_base(done), field: value}
    _persist(env, done, payload, fingerprint=None if field == "analysis" and isinstance(value, str) else
             hashlib.sha256(_base(done)["analysis"].encode()).hexdigest())
    refused(env, alice, done.replay(), UNAVAILABLE)


@pytest.mark.parametrize("change", ["missing", "extra", "reserved_key", "not_an_object", "nested_value"])
def test_exactly_the_eight_public_fields_are_admitted(env, alice, change):
    done = env.finalized(alice)
    base = _base(done)
    if change == "missing":
        payload = {k: v for k, v in base.items() if k != "method_used"}
    elif change == "extra":
        payload = {**base, "extra": 1}
    elif change == "reserved_key":
        payload = {**base, "private_metadata": {}}
    elif change == "not_an_object":
        payload = [base]
    else:
        payload = {**base, "analysis": [base["analysis"]]}
    _persist(env, done, payload, fingerprint=hashlib.sha256(base["analysis"].encode()).hexdigest())
    refused(env, alice, done.replay(), UNAVAILABLE)


def test_unicode_accents_and_escapes_are_restored_exactly(env, alice):
    done = env.finalized(alice)
    analysis = ("Élan « luxe » — façade, cœur, 日本語, emoji 🚀, guillemets \"x\", barre \\ oblique,\n"
                "tab\tet contrôle \u0001, séparateur  .")
    persisted = _persist(env, done, {**_base(done), "analysis": analysis, "name": "LVMH Moët Hennessy"})
    view = replayed(env, alice, done)
    assert view.payload_json == persisted.encode("utf-8")
    assert json.loads(view.payload_json)["analysis"] == analysis
    # Aucune seconde sérialisation : json.dumps changerait les octets (ASCII,
    # ordre des clés, séparateurs), jamais la vue.
    reserialized = json.dumps(json.loads(view.payload_json)).encode("utf-8")
    assert reserialized != view.payload_json and json.loads(reserialized) == json.loads(view.payload_json)
    assert "🚀".encode("utf-8") in view.payload_json and b"\\u0001" in view.payload_json


def test_a_wrong_visible_fingerprint_is_unavailable(env, alice):
    done = env.finalized(alice)
    _persist(env, done, _base(done), fingerprint=hashlib.sha256(b"autre texte").hexdigest())
    refused(env, alice, done.replay(), UNAVAILABLE)
    refused(env, alice, done.ack(), UNAVAILABLE)  # même preuve pour l'ACK : aucune capture


def test_the_c1b_text_bound_is_kept_and_no_new_bound_is_added(env, alice):
    done = env.finalized(alice)
    at_bound = "é" * (dcand.RESPONSE_MAX_BYTES // 2)  # 65 536 octets UTF-8 exactement
    assert len(at_bound.encode("utf-8")) == dcand.RESPONSE_MAX_BYTES
    _persist(env, done, {**_base(done), "analysis": at_bound})
    assert json.loads(replayed(env, alice, done).payload_json)["analysis"] == at_bound
    _persist(env, done, {**_base(done), "analysis": at_bound + "x"})
    refused(env, alice, done.replay(), UNAVAILABLE)


# --------------------------------------------------------------------------
# 4. ACK et attestation privée
# --------------------------------------------------------------------------

def test_the_first_ack_captures_and_delivers_atomically(env, alice, monkeypatch):
    done = env.finalized(alice)
    progress = []
    for name in ("apply_construction_these_progress", "apply_prepared_construction_these_progress"):
        monkeypatch.setattr(bridge.dp, name, lambda *a, **k: progress.append(1))
    before_counts = env.counts()
    sessions = env.sessions()
    result = acked(env, alice, done)
    assert result == bridge.DeliveryAcknowledged(done.assistant_turn_id, "delivered", "opened_event")
    delivery, link = env.delivery(done.assistant_turn_id), env.link(done.assistant_turn_id)
    assert delivery["status"] == "delivered" and delivery["delivered_at"] is not None
    assert (link["capture_state"], link["capture_version"], link["response_action"]) == ("captured", V2,
                                                                                         "opened_event")
    after = env.counts()
    assert after["cognitive_events"] == before_counts["cognitive_events"] + 1  # l'event ouvert par la réponse
    assert {k: v for k, v in after.items() if k != "cognitive_events"} == {
        k: v for k, v in before_counts.items() if k != "cognitive_events"}
    assert env.sessions() == sessions and progress == []  # aucune progression d'entrée
    assert env.reservation(done.reservation_id)["state"] == "finalized"


def test_a_replayed_ack_is_a_no_op(env, alice):
    done = env.finalized(alice)
    first = acked(env, alice, done)
    before = env.full_snapshot()
    assert acked(env, alice, done) == first
    assert env.full_snapshot() == before  # aucune capture, aucun event, aucune trace, xmin inchangés


def test_ack_needs_no_decryptage_keyring(env, alice):
    done = env.finalized(alice)
    assert env.c2(alice, done.ack(), decryptage_keys=None).status == AUTHORIZED
    assert env.run(alice, done.ack()).status == AUTHORIZED  # avec trousseau : même résultat (no-op)


def test_a_late_ack_keeps_the_stale_delivery_rule(env, alice):
    """R1-C4 conservé : la réponse d'un tour dont l'event de contexte a été
    fermé entre-temps devient delivered, mais stale_delivery."""
    first = env.finalized(alice, text=reply_for("business"))
    acked(env, alice, first)
    moat = env.finalized(alice, next_turn(CONV, question="Ils vendent du luxe.", context="h"),
                         text=reply_for("moat"))
    late = env.finalized(alice, next_turn(CONV, question="Et la marque ?", context="h"), text=reply_for("business"))
    assert acked(env, alice, moat).response_action == "transitioned_event"
    assert acked(env, alice, late) == bridge.DeliveryAcknowledged(late.assistant_turn_id, "delivered",
                                                                  "stale_delivery")


def test_the_historical_ack_path_with_the_probe_is_unchanged(env, alice):
    """PR 9-A : l'opération avec TransactionProbe garde ses comportements
    (ici : livraison legacy R1-C1, aucun lien, response_action None)."""
    legacy = env.claim_legacy(USER_A, CONV, uuid.uuid4())
    ctx = _ctx()
    with env.engine.connect() as conn:
        with conn.begin():
            probe = _adapters._transaction_probe(conn, ctx)
            try:
                result = bridge._execute(conn, ctx, bridge.DeliveryAcknowledgement(legacy, CONV), probe)
            finally:
                probe._close()
    assert result == bridge.DeliveryAcknowledged(legacy, "delivered", None)
    assert env.delivery(legacy)["status"] == "delivered"


def test_an_attested_ack_is_refused_for_a_legacy_delivery(env, alice):
    """Aucune preuve C1-B pour une livraison legacy : l'attestation ne peut
    pas être construite, et une attestation d'une autre livraison est refusée."""
    done = env.finalized(alice)
    legacy = env.claim_legacy(USER_A, CONV, uuid.uuid4())
    ctx = _ctx()
    with env.engine.connect() as conn:
        tx = conn.begin()
        port = _port(conn, done)
        with pytest.raises(contracts.CapabilityRefused):
            bridge._execute(conn, ctx, bridge.DeliveryAcknowledgement(legacy, CONV), port)
        tx.rollback()
    assert env.delivery(legacy)["status"] == "pending"


def _ctx(user_id=USER_A):
    return AuthorizedContext(AuthorizedPrincipal(user_id, uuid.uuid4(), uuid.uuid4(), "test"),
                             datetime.now(timezone.utc))


def _port(conn, done, user_id=USER_A):
    proof = _adapters._canonical_proof(conn, USER_A, done.ack())
    assert type(proof) is _adapters._CanonicalProof
    return _adapters._AckAdmissionPort(_adapters._CAPABILITY, conn, user_id, proof)


class _Fake:
    user_id = USER_A

    def bound_to(self, conn, user_id):
        return True

    def require_live(self):
        pass

    def require_delivery(self, **kwargs):
        pass


@pytest.mark.parametrize("case", ["absent", "fake", "other_connection", "other_transaction", "other_principal",
                                  "other_reservation", "other_conversation", "other_delivery", "closed", "consumed"])
def test_an_attestation_outside_its_contract_is_refused_before_any_capture(env, alice, monkeypatch, case):
    done = env.finalized(alice)
    other_done = env.finalized(alice, conversation_key="conv-2")
    captures, provenance = [], []
    monkeypatch.setattr(dcr, "capture_delivered_response", lambda *a, **k: captures.append(1))
    original = bridge._require_owned_provenance
    monkeypatch.setattr(bridge, "_require_owned_provenance", lambda *a: provenance.append(1) or original(*a))
    before = env.full_snapshot()
    operation = bridge.DeliveryAcknowledgement(done.assistant_turn_id, CONV)
    ctx = _ctx()
    with env.engine.connect() as conn, env.engine.connect() as other:
        tx = conn.begin()
        expected = contracts.CapabilityRefused
        if case == "absent":
            probe = None
        elif case == "fake":
            probe = _Fake()
        elif case == "other_connection":
            with other.begin():
                probe = _port(other, done)
        elif case == "other_transaction":
            probe = _port(conn, done)
            tx.rollback()
            tx = conn.begin()
            expected = contracts.TransactionLost
        elif case == "other_principal":
            probe = _port(conn, done)
            ctx = _ctx(USER_B)
        elif case == "other_reservation":
            probe = _port(conn, other_done)
        elif case == "other_conversation":
            probe = _port(conn, done)
            object.__setattr__(probe, "_conversation_key", "conv-2")
        elif case == "other_delivery":
            probe = _port(conn, done)
            object.__setattr__(probe, "_assistant_turn_id", other_done.assistant_turn_id)
        elif case == "closed":
            probe = _port(conn, done)
            probe._close()
        elif case == "consumed":
            probe = _port(conn, done)
            probe.require_delivery(assistant_turn_id=done.assistant_turn_id, user_id=USER_A, conversation_key=CONV,
                                   surface="decryptage", source_user_turn_id=probe._source_user_turn_id,
                                   delivery_ordinal=1, delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
        with pytest.raises(expected):
            bridge._execute(conn, ctx, operation, probe)
        tx.rollback()
    assert captures == [] and provenance == []  # refus AVANT la provenance transitive et la capture
    assert env.full_snapshot() == before


@pytest.mark.parametrize("damage", [
    "UPDATE assistant_deliveries SET private_metadata = private_metadata - 'cognitive_runtime_version' WHERE id = :d",
    "DELETE FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d",
    "UPDATE decryptage_cognitive_links SET capture_version = 'decryptage-cognitive-runtime-v1', "
    "product_progress_action = NULL WHERE assistant_delivery_id = :d",
    "UPDATE assistant_deliveries SET status = 'delivered', delivered_at = clock_timestamp() WHERE id = :d",
], ids=["runtime_absent", "link_absent", "link_v1", "delivered_awaiting"])
def test_the_bridge_reverifies_v2_on_the_locked_objects(env, alice, monkeypatch, damage):
    """Incohérence apparue APRÈS la preuve de l'adaptateur (même transaction) :
    le pont la constate sur la livraison et le lien verrouillés, AVANT la
    provenance transitive et la capture (refus fermé, rien n'est écrit)."""
    done = env.finalized(alice)
    captures = []
    monkeypatch.setattr(dcr, "capture_delivered_response", lambda *a, **k: captures.append(1))
    before = env.full_snapshot()
    with env.engine.connect() as conn:
        tx = conn.begin()
        port = _port(conn, done)
        conn.execute(sa.text(damage), {"d": done.assistant_turn_id})
        with pytest.raises(bridge.AckProvenanceIncoherent):
            bridge._execute(conn, _ctx(), bridge.DeliveryAcknowledgement(done.assistant_turn_id, CONV), port)
        port._close()
        tx.rollback()
    assert captures == [] and env.full_snapshot() == before


def test_attestation_implementation_is_sealed_and_exposes_no_sql(env, alice):
    assert contracts.implementation(contracts.AckAdmissionPort) is _adapters._AckAdmissionPort
    for bases in ((contracts.AckAdmissionPort,), (_adapters._AckAdmissionPort,), (contracts.TransactionProbe,)):
        with pytest.raises(TypeError):
            type("Rogue", bases, {"__slots__": ()})
    with pytest.raises(TypeError):  # deux contrats dérivés à la fois : refusé même dans le module autorisé
        type("Both", (contracts.FinalizationReadPort, contracts.AckAdmissionPort),
             {"__slots__": (), "__module__": contracts.IMPLEMENTING_MODULE})
    with pytest.raises(TypeError):  # une seconde implémentation du contrat : refusée
        type("Second", (contracts.AckAdmissionPort,), {"__slots__": (), "__module__": contracts.IMPLEMENTING_MODULE})
    assert contracts.implementation(contracts.AckAdmissionPort) is _adapters._AckAdmissionPort
    public = {n for n in dir(_adapters._AckAdmissionPort) if not n.startswith("_")}
    assert public == {"user_id", "bound_to", "require_live", "require_delivery"}
    done = env.finalized(alice)
    with env.engine.connect() as conn:
        tx = conn.begin()
        proof = _adapters._canonical_proof(conn, USER_A, done.ack())
        with pytest.raises(contracts.CapabilityRefused):
            _adapters._AckAdmissionPort(object(), conn, USER_A, proof)
        with pytest.raises(contracts.CapabilityRefused):
            _adapters._AckAdmissionPort(_adapters._CAPABILITY, conn, USER_A, object())
        port = _adapters._AckAdmissionPort(_adapters._CAPABILITY, conn, USER_A, proof)
        assert repr(port) == "AckAdmissionPort(<private>)" and str(done.assistant_turn_id) not in repr(port)
        assert repr(proof) == "_CanonicalProof(<private>)"
        for attempt in (lambda: setattr(port, "_open", True), lambda: pickle.dumps(port),
                        lambda: copy.copy(port), lambda: copy.deepcopy(port), lambda: pickle.dumps(proof)):
            with pytest.raises((AttributeError, TypeError)):
                attempt()
        port._close()
        tx.rollback()


def test_a_c2_ack_never_falls_back_to_the_historical_path(env, alice, monkeypatch, caplog):
    done = env.finalized(alice)
    historical = []
    monkeypatch.setattr(_adapters, "_transaction_probe", lambda *a: historical.append(1))

    def refuse(self, **kwargs):
        raise contracts.CapabilityRefused("attestation invalide (test)")

    monkeypatch.setattr(_adapters._AckAdmissionPort, "require_delivery", refuse)
    caplog.set_level(logging.INFO, logger="oryx.authz_f2")
    refused(env, alice, done.ack(), UNAVAILABLE)
    assert historical == [] and "alert=decryptage_bridge_capability_refused" in caplog.text


def test_the_adapter_checks_the_bridge_result_against_the_admitted_delivery(env, alice, monkeypatch):
    done = env.finalized(alice)
    original = bridge._execute

    def substituted(conn, ctx, operation, probe):
        result = original(conn, ctx, operation, probe)
        return bridge.DeliveryAcknowledged(uuid.uuid4(), result.delivery_status, result.response_action)

    monkeypatch.setattr(bridge, "_execute", substituted)
    refused(env, alice, done.ack(), UNAVAILABLE)  # capture ET statut annulés


# --------------------------------------------------------------------------
# 5. Atomicité et récupération
# --------------------------------------------------------------------------

def _raises(*a, **k):
    raise RuntimeError("panne simulée")


def test_a_failure_before_the_capture_writes_nothing(env, alice, monkeypatch):
    done = env.finalized(alice)
    monkeypatch.setattr(bridge, "_require_owned_provenance", _raises)
    refused(env, alice, done.ack(), UNAVAILABLE)


def test_a_failure_after_the_capture_and_before_delivered_writes_nothing(env, alice, monkeypatch):
    done = env.finalized(alice)
    seen = []

    def after_capture(db, *, delivery, user_id):
        seen.append(db.execute(sa.select(bridge.DecryptageCognitiveLink.capture_state).where(
            bridge.DecryptageCognitiveLink.assistant_delivery_id == delivery.id)).scalar_one())
        raise RuntimeError("panne après capture")

    monkeypatch.setattr(ad, "acknowledge_locked_delivery", after_capture)
    refused(env, alice, done.ack(), UNAVAILABLE)
    assert seen == ["captured"]  # la capture avait bien eu lieu, puis tout est annulé


def test_a_failure_after_delivered_and_before_commit_writes_nothing(env, alice):
    done = env.finalized(alice)

    def hook(point):
        if point == "executed":
            raise RuntimeError("panne avant COMMIT")

    refused(env, alice, done.ack(), UNAVAILABLE, hook=hook)


def test_a_commit_rejected_by_postgresql_is_unavailable_and_never_retried(env, alice, monkeypatch):
    done = env.finalized(alice)
    calls = []
    entry = bridge.OPERATIONS[bridge.DeliveryAcknowledgement]
    monkeypatch.setattr(bridge, "OPERATIONS", {**bridge.OPERATIONS,
                                               bridge.DeliveryAcknowledgement: lambda *a: calls.append(1) or entry(*a)})
    with env.observer.begin() as conn:
        conn.execute(sa.text("CREATE FUNCTION c2_reject() RETURNS trigger LANGUAGE plpgsql AS "
                             "$$ BEGIN RAISE EXCEPTION 'c2 commit refusé'; END $$"))
        conn.execute(sa.text("CREATE CONSTRAINT TRIGGER c2_reject AFTER UPDATE ON assistant_deliveries "
                             "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION c2_reject()"))
    try:
        refused(env, alice, done.ack(), UNAVAILABLE)
        assert calls == [1]  # aucune réexécution interne
    finally:
        with env.observer.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER c2_reject ON assistant_deliveries"))
            conn.execute(sa.text("DROP FUNCTION c2_reject()"))
    assert acked(env, alice, done).response_action == "opened_event"  # nouvelle invocation : première capture


def test_an_uncertain_commit_is_unavailable_and_a_new_ack_finds_the_persisted_state(env, alice, monkeypatch):
    done = env.finalized(alice)
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    outcome = env.c2(alice, done.ack(), hook=lambda point: armed.append(1) if point == "executed" else None)
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)  # jamais une réussite présumée
    assert env.delivery(done.assistant_turn_id)["status"] == "delivered"  # committé malgré l'erreur
    before = env.full_snapshot()
    events = env.counts()["cognitive_events"]
    assert acked(env, alice, done).response_action == "opened_event"
    assert env.full_snapshot() == before and env.counts()["cognitive_events"] == events  # aucune seconde capture


@pytest.mark.parametrize("kind", ["replay", "ack"])
@pytest.mark.parametrize("case", ["revoked", "expired"])
def test_an_invalid_f2_session_is_session_invalid(env, alice, kind, case):
    done = env.finalized(alice)
    if case == "revoked":
        env.service.revoke_session(alice.session_id, "security")
    else:
        env.expire_at(alice.session_id, "clock_timestamp() - interval '1 second'")
    refused(env, alice, command_of(kind, done), SESSION_INVALID)


def test_ack_refuses_a_non_read_committed_isolation(env, alice, pg_url):  # noqa: F811
    done = env.finalized(alice)
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, isolation_level="REPEATABLE READ")
    try:
        refused(env, alice, done.ack(), UNAVAILABLE, engine=engine)
    finally:
        engine.dispose()


def test_a_lost_runner_transaction_is_never_a_success(env, alice, monkeypatch):
    done = env.finalized(alice)
    original = ad.acknowledge_locked_delivery

    def swallowing(db, *, delivery, user_id):
        result = original(db, delivery=delivery, user_id=user_id)
        try:
            db.execute(sa.text("SELECT 1 / 0"))  # erreur avalée : transaction PostgreSQL avortée
        except sa.exc.DataError:
            pass
        return result

    monkeypatch.setattr(ad, "acknowledge_locked_delivery", swallowing)
    refused(env, alice, done.ack(), UNAVAILABLE)


def test_refusals_never_leak_content_fingerprints_nor_identifiers(env, alice, bob, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    done = env.finalized(alice)
    theirs = env.finalized(bob, conversation_key="conv-bob")
    delivery = env.delivery(done.assistant_turn_id)
    _persist(env, done, _base(done), fingerprint=hashlib.sha256(b"x").hexdigest())
    caplog.clear()  # seuls les journaux des appels C2
    outcomes = [env.c2(alice, done.replay()), env.c2(alice, done.ack()), env.c2(alice, command_of("replay", theirs))]
    assert [o.status for o in outcomes] == [UNAVAILABLE, UNAVAILABLE, NOT_FOUND]
    assert all(o.value is None and o.principal is None for o in outcomes)
    text = caplog.text
    for secret in (_base(done)["analysis"], delivery["request_fingerprint"], delivery["visible_content_fingerprint"],
                   str(done.assistant_turn_id), str(theirs.reservation_id), "SELECT", "assistant_deliveries"):
        assert secret not in text, secret
    assert "alert=decryptage_payload_incoherent" in text


# --------------------------------------------------------------------------
# 6. Unités pures : preuve terminale (états impossibles sous les CHECK)
# --------------------------------------------------------------------------

def _terminal_reservation(**changes):
    base = dict(id=uuid.uuid4(), conversation_key=CONV, source_user_turn_id=uuid.uuid4(),
                fingerprint_version="oryx-decryptage-reservation-v1", fingerprint_key_version="d1",
                request_fingerprint="a" * 64, state="finalized", blocked_reason=None, lease_epoch=1,
                lease_token_hash=None, lease_expires_at=None, attempts_used=1, retry_not_before_at=None,
                assistant_delivery_id=uuid.uuid4(), finalized_at=datetime.now(timezone.utc), resolved_ticker="MC.PA")
    base.update(changes)
    return _adapters._Reservation(**base)


@pytest.mark.parametrize("changes", [
    {"lease_token_hash": "b" * 64}, {"lease_expires_at": datetime.now(timezone.utc)},
    {"retry_not_before_at": datetime.now(timezone.utc)}, {"blocked_reason": "legacy_collision"},
    {"finalized_at": None}, {"resolved_ticker": None}, {"lease_epoch": 0}, {"attempts_used": 0},
    {"fingerprint_version": "oryx-decryptage-reservation-v0"}, {"fingerprint_key_version": "D1"},
    {"request_fingerprint": "A" * 64}, {"state": "reserved"},
], ids=lambda v: next(iter(v)))
def test_the_terminal_proof_refuses_every_incomplete_finalization(changes):
    assert _adapters._terminal(_terminal_reservation())
    assert not _adapters._terminal(_terminal_reservation(**changes))
