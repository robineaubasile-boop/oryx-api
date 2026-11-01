"""R1 PR 9-B1 — réservation durable, idempotence, bail et fencing des tours
Décrypter F2, contre un VRAI PostgreSQL (16 et 18 en CI).

VRAI runner, registre de PRODUCTION (seize commandes), sessions F2 réelles :
aucun registre de test, aucun chemin contournant BF2. Chaque écriture
durable est observée par une connexion indépendante ; xmin inchangé =
aucune écriture. Couvre :

- première réservation (epoch 1, une tentative, jeton jamais stocké, échéance
  = T_lease_decision + 360 s, capacité rendue après COMMIT seulement) ;
- retry identique, intention divergente, autre tour ;
- bail actif, expiration stricte (instant exact compris), reprise, plafond de
  trois attributions, libération avec backoffs 30 s / 120 s, épuisement ;
- fencing : ancien epoch, ancien jeton, bail expiré, successeur protégé,
  réservation terminale jamais réactivée ;
- collisions avec /decryptage historique (avant création, pendant un bail,
  après libération, à la déclaration d'échec) : blocage durable, aucune
  adoption ;
- autorisation F2 à chaque transaction, propriété (réservation et
  conversation d'autrui), trousseau absent ou clé retirée, COMMIT incertain,
  échec injecté, ordre des instructions, profil de verrous mesuré, secret.
"""
import logging
import re
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_fingerprint as dfp
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.runner import (AUTHORIZED, IDEMPOTENCY_CONFLICT, NOT_FOUND, REVISION_CONFLICT, SESSION_INVALID,
                                  UNAVAILABLE)
from tests.authz_f2_pr9b1_support import (CONV, D2, ReservationEnv, decryptage_keyring, reserve_command)
from tests.authz_f2_support import BOB, USER_A, USER_B
from tests.f2_support import APP_NAME, prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

FAILURE = dr.GENERATION_UNAVAILABLE


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = ReservationEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def granted(env, issued, command, **kwargs) -> dr.LeaseGranted:
    outcome = env.run(issued, command, **kwargs)
    assert outcome.status == AUTHORIZED, outcome
    assert type(outcome.value) is dr.LeaseGranted, outcome.value
    return outcome.value


def value(env, issued, command, kind, **kwargs):
    outcome = env.run(issued, command, **kwargs)
    assert outcome.status == AUTHORIZED, outcome
    assert type(outcome.value) is kind, outcome.value
    return outcome.value


def fail(env, issued, lease, failure=FAILURE, **kwargs):
    return env.run(issued, c.RecordDecryptageAttemptFailure.from_lease(lease, failure), **kwargs)


def expected_fingerprint(command, user_id=USER_A, key_version="d1", keys=None):
    question, context = command.texts
    return dfp.fingerprint(keys or decryptage_keyring(), dfp.FINGERPRINT_VERSION, key_version, user_id=user_id,
                           conversation_key=command.conversation_key, source_user_turn_id=command.client_turn_id,
                           ticker=command.ticker, question=question, context=context,
                           last_method_id=command.last_method_id)


# --------------------------------------------------------------------------
# Première réservation, retry, intention
# --------------------------------------------------------------------------

def test_first_reservation_grants_epoch_one_after_commit_and_stores_only_a_digest(env, alice):
    before = env.snapshot()
    command = reserve_command(ticker=" mc.pa ", question="  Pourquoi ?  ", last_method_id="construction_these")
    seen_before_commit = []
    lease_grant = granted(env, alice, command,
                          hook=lambda point: seen_before_commit.append(len(env.all_reservations()))
                          if point == "executed" else None)
    assert seen_before_commit == [0]  # rien n'est visible avant le COMMIT du runner
    lease = lease_grant.lease
    assert (lease.lease_epoch, lease_grant.attempts_used, lease.delivery_ordinal) == (1, 1, 1)
    assert (lease.conversation_key, lease.source_user_turn_id) == (CONV, command.client_turn_id)
    [row] = env.reservations()
    assert row["id"] == lease.reservation_id
    assert (row["state"], row["blocked_reason"], row["lease_epoch"], row["attempts_used"]) == ("reserved", None, 1, 1)
    assert (row["surface"], row["delivery_ordinal"], row["resolved_ticker"], row["assistant_delivery_id"],
            row["finalized_at"], row["retry_not_before_at"]) == ("decryptage", 1, None, None, None, None)
    assert row["lease_token_hash"] == lease.token.digest() and lease.token.matches(row["lease_token_hash"])
    assert row["lease_expires_at"] - row["updated_at"] == timedelta(seconds=360)
    assert row["created_at"] == row["updated_at"] and row["lease_expires_at"] == lease.lease_expires_at
    assert (row["fingerprint_version"], row["fingerprint_key_version"]) == ("oryx-decryptage-reservation-v1", "d1")
    assert row["request_fingerprint"] == expected_fingerprint(command)
    text = env.reservation_text(row["id"])
    raw = lease.token.reveal()
    assert raw.hex() not in text and raw.hex().upper() not in text
    # Conversation enregistrée dans la MÊME transaction ; aucun effet cognitif.
    [conversation] = env.conversations()
    assert (conversation["conversation_key"], conversation["user_id"]) == (CONV, USER_A)
    # Chaque INSERT a son SAVEPOINT (xid de sous-transaction distinct) ;
    # l'atomicité est prouvée par test_a_failure_after_execution_rolls_everything_back.
    assert conversation["xmin"] != row["xmin"]
    after = env.snapshot()
    assert {t: v for t, v in after.items() if t != "conversation_identities"} == {
        t: v for t, v in before.items() if t != "conversation_identities"}


def test_identical_retry_during_an_active_lease_is_in_progress_without_any_write(env, alice):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    [before] = env.reservations()
    for again in (command, reserve_command(command.client_turn_id, ticker="mc.pa ", question=" Pourquoi ? ")):
        progress = value(env, alice, again, dr.ReservationInProgress)
        assert (progress.reservation_id, progress.lease_epoch, progress.lease_expires_at) == (
            lease.reservation_id, 1, lease.lease_expires_at)
    assert env.reservations() == [before]


@pytest.mark.parametrize("change", [{"question": "Autre question ?"}, {"ticker": "AIR.PA"}, {"context": "ctx"},
                                    {"last_method_id": ""}, {"last_method_id": "construction_these"}])
def test_divergent_intent_for_the_same_turn_is_refused_without_any_write(env, alice, change):
    command = reserve_command()
    granted(env, alice, command)
    before = env.full_snapshot()
    outcome = env.run(alice, reserve_command(command.client_turn_id, **change))
    assert (outcome.status, outcome.value, outcome.principal) == (IDEMPOTENCY_CONFLICT, None, None)
    assert env.full_snapshot() == before


def test_the_same_question_on_another_turn_is_another_reservation(env, alice):
    first = granted(env, alice, reserve_command()).lease
    second = granted(env, alice, reserve_command()).lease
    assert first.reservation_id != second.reservation_id and first.token != second.token
    assert len(env.reservations()) == 2 and len(env.conversations()) == 1


# --------------------------------------------------------------------------
# Bail, expiration, reprise, plafond
# --------------------------------------------------------------------------

def test_expired_lease_is_recovered_with_a_new_epoch_counter_and_token(env, alice):
    command = reserve_command()
    first = granted(env, alice, command).lease
    env.expire(first.reservation_id)
    second_grant = granted(env, alice, command)
    second = second_grant.lease
    assert (second.reservation_id, second.lease_epoch, second_grant.attempts_used) == (first.reservation_id, 2, 2)
    assert second.token != first.token and second.lease_expires_at > first.lease_expires_at
    row = env.reservation(first.reservation_id)
    assert row["lease_token_hash"] == second.token.digest() != first.token.digest()
    assert row["lease_expires_at"] - row["updated_at"] == timedelta(seconds=360)
    # L'ancien détenteur (même s'il avait encore son jeton) ne peut plus agir.
    before = env.reservation(first.reservation_id)
    assert fail(env, alice, first).status == REVISION_CONFLICT
    assert env.reservation(first.reservation_id) == before


def test_three_attributions_then_an_expired_last_lease_blocks_durably(env, alice):
    command = reserve_command()
    leases = [granted(env, alice, command).lease]
    for epoch in (2, 3):
        env.expire(leases[-1].reservation_id)
        leases.append(granted(env, alice, command).lease)
        assert leases[-1].lease_epoch == epoch
    # Dernier bail ACTIF au plafond : il reste autorisé à terminer.
    assert value(env, alice, command, dr.ReservationInProgress).lease_epoch == 3
    env.expire(leases[-1].reservation_id)
    blocked = value(env, alice, command, dr.ReservationBlocked)
    assert blocked.blocked_reason == "attempts_exhausted"
    row = env.reservation(blocked.reservation_id)  # blocage COMMITTÉ, lu par une autre connexion
    assert (row["state"], row["blocked_reason"], row["lease_epoch"], row["attempts_used"], row["lease_token_hash"],
            row["lease_expires_at"], row["retry_not_before_at"]) == ("blocked", "attempts_exhausted", 3, 3, None,
                                                                     None, None)
    # Terminal : jamais réactivé, ni par un retry, ni par un ancien détenteur.
    assert value(env, alice, command, dr.ReservationBlocked).blocked_reason == "attempts_exhausted"
    for lease in leases:
        assert fail(env, alice, lease).status == REVISION_CONFLICT
    assert env.reservation(blocked.reservation_id) == row


def test_expiry_is_strict_at_the_exact_instant(env, alice, monkeypatch):
    command = reserve_command()
    first = granted(env, alice, command).lease
    real_clock = _adapters._lease_clock
    at_expiry = []

    def exact(conn):
        real_clock(conn)
        return env.reservation(first.reservation_id)["lease_expires_at"] if at_expiry else real_clock(conn)

    monkeypatch.setattr(_adapters, "_lease_clock", exact)
    at_expiry.append(1)
    before = env.reservation(first.reservation_id)
    # T_lease_decision == lease_expires_at : la capacité n'est plus valide.
    assert fail(env, alice, first).status == REVISION_CONFLICT
    assert env.reservation(first.reservation_id) == before
    second = granted(env, alice, command).lease  # et le bail est repris à cet instant
    assert second.lease_epoch == 2


def test_known_failures_release_with_frozen_backoffs_then_exhaust(env, alice):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    for attempt, backoff in ((1, 30), (2, 120)):
        recorded = fail(env, alice, lease)
        assert recorded.status == AUTHORIZED and type(recorded.value) is dr.AttemptFailureRecorded
        assert (recorded.value.state, recorded.value.blocked_reason, recorded.value.attempts_used) == (
            "reserved", None, attempt)
        row = env.reservation(lease.reservation_id)
        assert row["retry_not_before_at"] - row["updated_at"] == timedelta(seconds=backoff)
        assert row["lease_token_hash"] is None and row["lease_expires_at"] is None and row["lease_epoch"] == attempt
        # Avant la fin du backoff : aucun nouveau bail, aucune écriture.
        deferred = value(env, alice, command, dr.ReservationRetryDeferred)
        assert deferred.retry_not_before_at == row["retry_not_before_at"]
        assert env.reservation(lease.reservation_id) == row
        # Un second échec déclaré avec la même capacité est refusé (jeton retiré).
        assert fail(env, alice, lease).status == REVISION_CONFLICT
        env.end_backoff(lease.reservation_id)
        lease = granted(env, alice, command).lease
        assert lease.lease_epoch == attempt + 1
    final = fail(env, alice, lease)
    assert final.status == AUTHORIZED
    assert (final.value.state, final.value.blocked_reason, final.value.retry_not_before_at, final.value.attempts_used) == (
        "blocked", "attempts_exhausted", None, 3)
    assert value(env, alice, command, dr.ReservationBlocked).blocked_reason == "attempts_exhausted"


@pytest.mark.parametrize("stale", ["epoch", "token", "expired", "released", "blocked"])
def test_a_stale_capability_never_mutates(env, alice, stale):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    capability = c.RecordDecryptageAttemptFailure.from_lease(lease, FAILURE)
    if stale == "epoch":
        capability = c.RecordDecryptageAttemptFailure(lease.reservation_id, 2, lease.token, FAILURE)
    elif stale == "token":
        capability = c.RecordDecryptageAttemptFailure(lease.reservation_id, 1, dr.LeaseToken.generate(), FAILURE)
    elif stale == "expired":
        env.expire(lease.reservation_id)
    elif stale == "released":
        assert fail(env, alice, lease).status == AUTHORIZED
    else:
        env.claim_legacy(USER_A, CONV, command.client_turn_id)
        assert value(env, alice, command, dr.ReservationBlocked).blocked_reason == "legacy_collision"
    before = env.reservation(lease.reservation_id)
    outcome = env.run(alice, capability)
    assert (outcome.status, outcome.value, outcome.principal) == (REVISION_CONFLICT, None, None)
    assert env.reservation(lease.reservation_id) == before


def test_an_old_holder_cannot_release_or_reset_its_successor(env, alice):
    command = reserve_command()
    old = granted(env, alice, command).lease
    env.expire(old.reservation_id)
    successor = granted(env, alice, command).lease
    before = env.reservation(old.reservation_id)
    for capability in (c.RecordDecryptageAttemptFailure.from_lease(old, FAILURE),
                       c.RecordDecryptageAttemptFailure(old.reservation_id, successor.lease_epoch, old.token, FAILURE),
                       c.RecordDecryptageAttemptFailure(old.reservation_id, old.lease_epoch, successor.token,
                                                        FAILURE)):
        assert env.run(alice, capability).status == REVISION_CONFLICT
    assert env.reservation(old.reservation_id) == before  # compteur, epoch et bail du successeur intacts
    assert fail(env, alice, successor).status == AUTHORIZED


def test_epoch_overflow_is_refused_without_wrapping(env, alice, caplog):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    env.force(lease.reservation_id, f"lease_epoch = {dr.MAX_EPOCH}, lease_expires_at = clock_timestamp() - "
                                    "interval '1 second'")
    before = env.reservation(lease.reservation_id)
    with caplog.at_level(logging.ERROR, logger="oryx.authz_f2"):
        assert env.run(alice, command).status == UNAVAILABLE
    assert "alert=decryptage_epoch_exhausted" in caplog.text
    assert env.reservation(lease.reservation_id) == before


# --------------------------------------------------------------------------
# Collisions avec /decryptage historique
# --------------------------------------------------------------------------

def test_a_preexisting_legacy_delivery_blocks_the_turn_without_any_lease(env, alice):
    command = reserve_command()
    with env.observer.begin() as conn:  # conversation historique du même propriétaire
        conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                             "VALUES (:k, :u, now())"), {"k": CONV, "u": USER_A})
    delivery_id = env.claim_legacy(USER_A, CONV, command.client_turn_id)
    deliveries = env.deliveries()
    conversations = env.conversations()
    blocked = value(env, alice, command, dr.ReservationBlocked)
    assert blocked.blocked_reason == "legacy_collision"
    [row] = env.reservations()
    assert (row["state"], row["blocked_reason"], row["lease_epoch"], row["attempts_used"], row["lease_token_hash"],
            row["lease_expires_at"], row["assistant_delivery_id"]) == ("blocked", "legacy_collision", 0, 0, None,
                                                                       None, None)
    assert row["request_fingerprint"] == expected_fingerprint(command)  # intention conservée
    assert env.deliveries() == deliveries and env.conversations() == conversations  # rien d'adopté ni de réécrit
    assert delivery_id not in {r["assistant_delivery_id"] for r in env.all_reservations()}
    # Retry : même blocage, aucune écriture ; autre intention : conflit.
    assert value(env, alice, command, dr.ReservationBlocked).reservation_id == blocked.reservation_id
    assert env.reservations() == [row]
    assert env.run(alice, reserve_command(command.client_turn_id, question="autre")).status == IDEMPOTENCY_CONFLICT


def test_a_legacy_delivery_during_an_active_lease_blocks_and_fences_the_holder(env, alice):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    env.claim_legacy(USER_A, CONV, command.client_turn_id)
    blocked = value(env, alice, command, dr.ReservationBlocked)
    assert blocked.blocked_reason == "legacy_collision"
    row = env.reservation(lease.reservation_id)
    assert (row["state"], row["lease_epoch"], row["attempts_used"], row["lease_token_hash"], row["lease_expires_at"],
            row["assistant_delivery_id"]) == ("blocked", 1, 1, None, None, None)
    assert fail(env, alice, lease).status == REVISION_CONFLICT  # l'ancien détenteur ne réactive rien
    assert env.reservation(lease.reservation_id) == row


def test_a_proven_collision_prevails_over_the_backoff(env, alice):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    assert fail(env, alice, lease).status == AUTHORIZED
    env.claim_legacy(USER_A, CONV, command.client_turn_id)
    assert value(env, alice, command, dr.ReservationBlocked).blocked_reason == "legacy_collision"
    assert env.reservation(lease.reservation_id)["retry_not_before_at"] is None


def test_a_collision_found_at_failure_recording_blocks_instead_of_backing_off(env, alice):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    env.claim_legacy(USER_A, CONV, command.client_turn_id)
    recorded = fail(env, alice, lease)
    assert recorded.status == AUTHORIZED
    assert (recorded.value.state, recorded.value.blocked_reason, recorded.value.retry_not_before_at) == (
        "blocked", "legacy_collision", None)


def test_a_legacy_delivery_of_another_turn_or_owner_is_no_collision(env, alice):
    env.confirmed(BOB, USER_B)
    command = reserve_command()
    with env.observer.begin() as conn:
        conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                             "VALUES ('conv-b', :u, now())"), {"u": USER_B})
    env.claim_legacy(USER_B, "conv-b", command.client_turn_id)  # même tour, autre propriétaire / conversation
    granted(env, alice, command)
    env.claim_legacy(USER_A, CONV, uuid.uuid4())  # même conversation, autre tour
    assert type(env.run(alice, command).value) is dr.ReservationInProgress


def test_a_coherent_finalized_reservation_is_presented_and_an_incoherent_one_refused(env, alice, caplog):
    command = reserve_command()
    lease = granted(env, alice, command).lease
    other = env.claim_legacy(USER_A, CONV, uuid.uuid4())
    # Ligne finalisée INCOHÉRENTE (livraison d'un autre tour) : jamais présentée.
    env.force(lease.reservation_id, f"state = 'finalized', assistant_delivery_id = '{other}', "
                                    "finalized_at = updated_at, lease_token_hash = NULL, lease_expires_at = NULL")
    before = env.reservation(lease.reservation_id)
    with caplog.at_level(logging.ERROR, logger="oryx.authz_f2"):
        assert env.run(alice, command).status == UNAVAILABLE
    assert "alert=decryptage_reservation_incoherent" in caplog.text
    assert env.reservation(lease.reservation_id) == before
    own = env.claim_legacy(USER_A, CONV, command.client_turn_id)
    env.force(lease.reservation_id, f"assistant_delivery_id = '{own}'")
    finalized = value(env, alice, command, dr.ReservationFinalized)
    assert (finalized.reservation_id, finalized.assistant_delivery_id) == (lease.reservation_id, own)
    assert fail(env, alice, lease).status == REVISION_CONFLICT


# --------------------------------------------------------------------------
# Autorisation F2 et propriété
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["garbage", "revoked", "expired", "pending_delivery"])
def test_an_invalid_f2_session_writes_nothing(env, case):
    issued = env.sign_in() if case == "pending_delivery" else env.confirmed()
    credential = issued.credential()
    if case == "garbage":
        from core.auth_f2.service import SessionCredential
        credential = SessionCredential(uuid.uuid4(), credential.secret)
    elif case == "revoked":
        env.service.revoke_session(issued.session_id, "security")
    elif case == "expired":
        env.expire_at(issued.session_id, "clock_timestamp() - interval '1 second'")
    before = env.full_snapshot()
    outcome = env.b1_runner().run(credential, reserve_command())
    assert (outcome.status, outcome.value, outcome.principal) == (SESSION_INVALID, None, None)
    assert env.full_snapshot() == before


def test_a_revoked_session_cannot_record_a_failure_even_with_the_token(env, alice):
    lease = granted(env, alice, reserve_command()).lease
    before = env.reservation(lease.reservation_id)
    env.service.revoke_session(alice.session_id, "security")
    assert fail(env, alice, lease).status == SESSION_INVALID
    assert env.reservation(lease.reservation_id) == before


def test_a_foreign_reservation_is_not_found_without_lock_or_leak(env, alice):
    bob = env.confirmed(BOB, USER_B)
    lease = granted(env, bob, reserve_command(conversation_key="conv-bob")).lease
    before = env.reservation(lease.reservation_id)
    with env.observer.connect() as holder:  # un tiers tient la ligne d'autrui
        holder.execute(sa.text("BEGIN"))
        holder.execute(sa.text("SELECT 1 FROM decryptage_turn_reservations WHERE id = :i FOR UPDATE"),
                       {"i": lease.reservation_id})
        outcome = fail(env, alice, lease)
        holder.execute(sa.text("ROLLBACK"))
    assert (outcome.status, outcome.value, outcome.principal) == (NOT_FOUND, None, None)
    absent = env.run(alice, c.RecordDecryptageAttemptFailure(uuid.uuid4(), 1, lease.token, FAILURE))
    assert absent.status == NOT_FOUND  # inexistante : même résultat
    assert env.reservation(lease.reservation_id) == before


def test_a_foreign_conversation_is_not_found_and_nothing_is_written(env, alice):
    bob = env.confirmed(BOB, USER_B)
    granted(env, bob, reserve_command(conversation_key="conv-bob"))
    before = env.full_snapshot()
    outcome = env.run(alice, reserve_command(conversation_key="conv-bob"))
    assert (outcome.status, outcome.value, outcome.principal) == (NOT_FOUND, None, None)
    assert env.full_snapshot() == before


def test_an_existing_own_conversation_is_reused_without_reassignment(env, alice):
    with env.observer.begin() as conn:
        conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                             "VALUES (:k, :u, now())"), {"k": CONV, "u": USER_A})
    [conversation] = env.conversations()
    granted(env, alice, reserve_command())
    assert env.conversations() == [conversation]  # même ligne, même xmin


# --------------------------------------------------------------------------
# Trousseau, panne, COMMIT incertain
# --------------------------------------------------------------------------

def test_without_the_decryptage_keyring_the_commands_are_unavailable_and_others_work(env, alice, caplog):
    lease = granted(env, alice, reserve_command()).lease
    before = env.full_snapshot()
    calls = []
    runner = env.b1_runner(decryptage_keys=None, hook=calls.append)
    with caplog.at_level(logging.WARNING, logger="oryx.authz_f2"):
        for command in (reserve_command(), c.RecordDecryptageAttemptFailure.from_lease(lease, FAILURE)):
            assert runner.run(alice.credential(), command).status == UNAVAILABLE
    assert calls == [] and "error=decryptage_keyring_missing" in caplog.text  # avant toute transaction
    assert runner.run(alice.credential(), c.ReadProfile()).status == AUTHORIZED
    assert env.full_snapshot() == before


def test_a_retired_recorded_key_is_refused_closed_without_replacing_the_fingerprint(env, alice, caplog):
    command = reserve_command()
    lease = granted(env, alice, command).lease  # enregistrée avec d1
    env.expire(lease.reservation_id)
    before = env.reservation(lease.reservation_id)
    rotated = decryptage_keyring({"d2": D2}, "d2")
    with caplog.at_level(logging.ERROR, logger="oryx.authz_f2"):
        assert env.run(alice, command, decryptage_keys=rotated).status == UNAVAILABLE
    assert "alert=decryptage_key_unavailable" in caplog.text
    assert env.reservation(lease.reservation_id) == before
    # Rotation : nouvelle version courante, ancienne encore lisible -> retry vérifié avec d1.
    both = decryptage_keyring(current="d2")
    again = granted(env, alice, command, decryptage_keys=both)
    assert again.lease.lease_epoch == 2
    assert env.reservation(lease.reservation_id)["fingerprint_key_version"] == "d1"
    fresh = reserve_command()
    granted(env, alice, fresh, decryptage_keys=both)
    [row] = [r for r in env.reservations() if r["source_user_turn_id"] == fresh.client_turn_id]
    assert row["fingerprint_key_version"] == "d2"
    assert row["request_fingerprint"] == expected_fingerprint(fresh, key_version="d2", keys=both)


def test_a_failure_after_execution_rolls_everything_back(env, alice):
    before = env.full_snapshot()

    def crash(point):
        if point == "executed":
            raise RuntimeError("panne injectée avant le COMMIT")

    assert env.run(alice, reserve_command(), hook=crash).status == UNAVAILABLE
    assert env.full_snapshot() == before  # ni réservation, ni conversation


def test_an_uncertain_commit_never_yields_a_capability_and_a_retry_waits(env, alice, monkeypatch, caplog):
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    command = reserve_command()
    outcome = env.run(alice, command, hook=lambda point: armed.append(1) if point == "executed" else None)
    assert (outcome.status, outcome.value, outcome.principal) == (UNAVAILABLE, None, None)
    [row] = env.reservations()  # commitée malgré l'erreur : état incertain pour l'appelant
    assert (row["state"], row["lease_epoch"]) == ("reserved", 1)
    progress = value(env, alice, command, dr.ReservationInProgress)  # jeton perdu : attendre l'échéance
    assert progress.lease_epoch == 1 and env.reservations() == [row]
    env.expire(row["id"])
    assert granted(env, alice, command).lease.lease_epoch == 2  # jamais une capacité sur le même epoch


# --------------------------------------------------------------------------
# Ordre des instructions, verrous mesurés, secret
# --------------------------------------------------------------------------

def test_business_statements_run_after_t_authorization_and_the_lease_clock_after_the_lock(env, alice):
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    sa.event.listen(env.engine, "before_cursor_execute", capture)
    try:
        granted(env, alice, reserve_command())
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", capture)
    phase_b = statements[statements.index("SELECT current_setting('transaction_isolation')"):]
    t_authorization = phase_b.index("SELECT clock_timestamp()")
    business = [i for i, s in enumerate(phase_b) if re.search(r"conversation_identities|decryptage_turn_reservations|"
                                                               r"assistant_deliveries", s)]
    assert business and min(business) > t_authorization
    assert phase_b[:t_authorization][1].startswith("SELECT id FROM users WHERE id = ") and phase_b[1].endswith(
        "FOR NO KEY UPDATE")
    lock = next(i for i, s in enumerate(phase_b) if "FROM decryptage_turn_reservations" in s and "FOR UPDATE" in s)
    lease_clock = phase_b.index("SELECT clock_timestamp()", t_authorization + 1)
    assert lock < lease_clock
    for forbidden in ("analysis_sessions", "cognitive_events", "support_traces", "decryptage_cognitive_links",
                      "user_statements", "analysis_facts", "investment_theses", "portfolio_"):
        assert not any(forbidden in s for s in phase_b), forbidden


def _probe(conn, sql, **params):
    conn.execute(sa.text("SAVEPOINT probe"))
    try:
        conn.execute(sa.text(sql), params)
        return "granted"
    except sa.exc.OperationalError as exc:
        assert exc.orig.pgcode == "55P03", exc
        return "55P03"
    finally:
        conn.execute(sa.text("ROLLBACK TO SAVEPOINT probe"))


def test_measured_lock_profile_of_a_reservation(env, alice):
    """Sondes NOWAIT d'une connexion tierce pendant que la transaction de
    réservation est suspendue avant son COMMIT (point « executed »)."""
    command = reserve_command()
    delivery_turn = uuid.uuid4()
    env.claim_legacy(USER_A, "conv-other", delivery_turn)
    observed = {}

    def probe(point):
        if point != "executed":
            return
        with env.observer.connect() as conn:
            conn.execute(sa.text("BEGIN"))
            row_id = conn.execute(sa.text("SELECT id FROM decryptage_turn_reservations")).scalar_one_or_none()
            observed["visible"] = row_id is not None
            observed["users_key_share"] = _probe(conn, "SELECT 1 FROM users WHERE id = :u FOR KEY SHARE NOWAIT",
                                                 u=USER_A)
            observed["users_update"] = _probe(conn, "SELECT 1 FROM users WHERE id = :u FOR UPDATE NOWAIT", u=USER_A)
            observed["delivery_update"] = _probe(conn, "SELECT 1 FROM assistant_deliveries WHERE user_id = :u "
                                                       "FOR UPDATE NOWAIT", u=USER_A)
            conn.execute(sa.text("ROLLBACK"))

    granted(env, alice, command, hook=probe)
    assert observed == {"visible": False, "users_key_share": "granted", "users_update": "55P03",
                        "delivery_update": "granted"}
    lease = env.reservations()[0]
    held = {}

    def hold(point):  # bail repris : la réservation propre est tenue FOR UPDATE jusqu'au COMMIT
        if point != "executed":
            return
        with env.observer.connect() as conn:
            conn.execute(sa.text("BEGIN"))
            held["reservation"] = _probe(conn, "SELECT 1 FROM decryptage_turn_reservations WHERE id = :i "
                                               "FOR UPDATE NOWAIT", i=lease["id"])
            held["conversation_no_key_update"] = _probe(
                conn, "SELECT 1 FROM conversation_identities WHERE conversation_key = :k FOR NO KEY UPDATE NOWAIT",
                k=CONV)
            conn.execute(sa.text("ROLLBACK"))

    env.expire(lease["id"])
    granted(env, alice, command, hook=hold)
    assert held == {"reservation": "55P03", "conversation_no_key_update": "granted"}
    assert env.waiting(APP_NAME) == 0


def test_the_real_outcome_and_logs_never_expose_the_token(env, alice, caplog):
    with caplog.at_level(logging.DEBUG):
        outcome = env.run(alice, reserve_command())
        lease = outcome.value.lease
        failed = fail(env, alice, lease)
    raw = lease.token.reveal()
    for text in (repr(outcome), str(outcome), repr(failed), caplog.text,
                 repr(c.RecordDecryptageAttemptFailure.from_lease(lease, FAILURE))):
        assert raw.hex() not in text and raw.hex().upper() not in text and repr(raw) not in text
        assert lease.token.digest() not in text
    assert "<secret>" in repr(outcome)
