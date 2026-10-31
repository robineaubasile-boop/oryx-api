"""R1 PR 9-B1 — concurrence des réservations Décrypter F2, contre un VRAI
PostgreSQL : vraies connexions, points de synchronisation déterministes
(points d'interruption du runner, transactions tenues par des connexions
tierces), attentes de verrou OBSERVÉES dans pg_stat_activity, compteur
pg_stat_database.deadlocks inchangé (fixture) et aucun 503 imprévu.

Deux familles, distinguées explicitement :

- BF2 RÉELS : deux commandes du VRAI runner (registre de production) ;
- INTERNES SQL : une connexion de TEST écrit directement une ligne
  (réservation ou conversation) sans passer par la frontière, pour forcer
  un entrelacement que deux transactions F2 du même compte ne peuvent pas
  produire (elles se sérialisent au rang users FOR NO KEY UPDATE). Ces
  écritures de test ne sont pas un chemin applicatif : la commande observée
  passe toujours par le vrai runner.

Un délai de verrou n'est jamais une preuve d'absence d'interblocage :
chaque scénario exige des résultats métier exacts.
"""
import threading
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import interaction_identity as ii
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.runner import AUTHORIZED, IDEMPOTENCY_CONFLICT, NOT_FOUND, REVISION_CONFLICT, SESSION_INVALID, \
    UNAVAILABLE
from tests.authz_f2_pr9a_support import LEGACY_APP, Background, Pause, wait_until
from tests.authz_f2_pr9b1_support import CONV, ReservationEnv, legacy_decryptage, legacy_route, reserve_command  # noqa: F401
from tests.authz_f2_support import BOB, USER_A, USER_B, create_command
from tests.f2_support import APP_NAME, prepare_schema
from tests.test_authz_f2_pr9b1 import expected_fingerprint
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
    assert environment.deadlocks() == before  # aucun interblocage détecté par PostgreSQL
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def started(fn) -> Background:
    actor = Background(fn)
    actor.start()
    return actor


def paused(env, issued, command, point="executed", **kwargs):
    pause = Pause(point)
    actor = started(lambda: env.run(issued, command, hook=pause, **kwargs))
    assert pause.reached.wait(15), "point d'interruption non atteint"
    return pause, actor


class PauseThenFail(Pause):
    """Pause, puis échec injecté au même point (ROLLBACK du gagnant)."""

    def __call__(self, point):
        super().__call__(point)
        if point == self.point:
            raise RuntimeError("gagnant annulé (injecté)")


def own_conversation(env, key=CONV, user_id=USER_A):
    with env.observer.begin() as conn:
        conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                             "VALUES (:k, :u, now())"), {"k": key, "u": user_id})


def kind(outcome):
    assert outcome.status == AUTHORIZED, outcome
    return type(outcome.value)


# --------------------------------------------------------------------------
# BF2 réels : deux commandes de réservation du même tour
# --------------------------------------------------------------------------

def test_two_f2_reservations_of_the_same_turn_yield_exactly_one_lease(env, alice):
    command = reserve_command()
    pause, first = paused(env, alice, command)
    second = started(lambda: env.run(alice, command))
    wait_until(lambda: env.waiting(APP_NAME) >= 1, message="le second doit attendre au rang users")
    assert env.all_reservations() == []  # gagnant non committé : invisible
    pause.release.set()
    assert kind(first.outcome()) is dr.LeaseGranted
    assert kind(second.outcome()) is dr.ReservationInProgress  # aucun second détenteur
    [row] = env.reservations()
    assert (row["lease_epoch"], row["attempts_used"]) == (1, 1)


def test_a_rolled_back_winner_leaves_the_turn_to_the_next_reservation(env, alice):
    command = reserve_command()
    pause = PauseThenFail("executed")
    first = started(lambda: env.run(alice, command, hook=pause))
    assert pause.reached.wait(15)
    second = started(lambda: env.run(alice, command))
    wait_until(lambda: env.waiting(APP_NAME) >= 1)
    pause.release.set()
    assert first.outcome().status == UNAVAILABLE
    granted = second.outcome()
    assert kind(granted) is dr.LeaseGranted and granted.value.lease.lease_epoch == 1


def test_a_failure_report_and_a_retry_serialize_on_the_reservation(env, alice):
    command = reserve_command()
    lease = env.run(alice, command).value.lease
    pause, report = paused(env, alice, c.RecordDecryptageAttemptFailure.from_lease(lease, FAILURE))
    retry = started(lambda: env.run(alice, command))
    wait_until(lambda: env.waiting(APP_NAME) >= 1)
    pause.release.set()
    assert kind(report.outcome()) is dr.AttemptFailureRecorded
    assert kind(retry.outcome()) is dr.ReservationRetryDeferred  # la libération committée est vue


# --------------------------------------------------------------------------
# Internes SQL : conflit UNIQUE direct, gagnant non committé
# --------------------------------------------------------------------------

def _hold_reservation(env, conn, command, fingerprint, *, epoch=1):
    conn.execute(sa.text("BEGIN"))
    conn.execute(sa.text(
        "INSERT INTO decryptage_turn_reservations (id, user_id, conversation_key, surface, source_user_turn_id, "
        "delivery_ordinal, fingerprint_version, fingerprint_key_version, request_fingerprint, state, lease_epoch, "
        "lease_token_hash, lease_expires_at, attempts_used, created_at, updated_at) VALUES (gen_random_uuid(), "
        ":u, :k, 'decryptage', :t, 1, 'oryx-decryptage-reservation-v1', 'd1', :f, 'reserved', :e, :h, "
        "clock_timestamp() + interval '360 seconds', :e, clock_timestamp(), clock_timestamp())"),
        {"u": USER_A, "k": command.conversation_key, "t": command.client_turn_id, "f": fingerprint, "e": epoch,
         "h": "a" * 64})


@pytest.mark.parametrize("winner", ["commit", "rollback", "divergent"])
def test_direct_unique_conflict_on_two_connections_rereads_the_winner(env, alice, winner):
    own_conversation(env)
    command = reserve_command()
    fingerprint = expected_fingerprint(command) if winner != "divergent" else "b" * 64
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    with env.observer.connect() as holder:
        _hold_reservation(env, holder, command, fingerprint)
        sa.event.listen(env.engine, "before_cursor_execute", capture)
        try:
            actor = started(lambda: env.run(alice, command))
            wait_until(lambda: env.waiting(APP_NAME) >= 1, message="la réservation F2 doit attendre l'index unique")
            holder.execute(sa.text("COMMIT" if winner != "rollback" else "ROLLBACK"))
            outcome = actor.outcome()
        finally:
            sa.event.remove(env.engine, "before_cursor_execute", capture)
    if winner == "rollback":
        assert kind(outcome) is dr.LeaseGranted and outcome.value.lease.lease_epoch == 1
        return
    if winner == "commit":
        assert kind(outcome) is dr.ReservationInProgress
    else:
        assert outcome.status == IDEMPOTENCY_CONFLICT
    locks = [i for i, s in enumerate(statements)
             if "FROM decryptage_turn_reservations WHERE user_id" in s and "FOR UPDATE" in s]
    inserts = [i for i, s in enumerate(statements) if s.startswith("INSERT INTO decryptage_turn_reservations")]
    rollbacks = [i for i, s in enumerate(statements) if s.startswith("ROLLBACK TO SAVEPOINT")]
    # Verrou (absente), INSERT en SAVEPOINT, retour au SAVEPOINT, puis relecture
    # verrouillée par une NOUVELLE instruction.
    assert len(locks) == 2 and len(inserts) == 1 and len(rollbacks) == 1
    assert locks[0] < inserts[0] < rollbacks[0] < locks[1]
    [row] = env.reservations()
    assert row["lease_token_hash"] == "a" * 64  # la ligne du gagnant n'est pas modifiée


def _raising_trigger(env, table, errcode, constraint):
    with env.observer.begin() as conn:
        conn.execute(sa.text(
            "CREATE FUNCTION pr9b1_raise() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"RAISE EXCEPTION 'injecté' USING ERRCODE = '{errcode}', CONSTRAINT = '{constraint}'; END $$"))
        conn.execute(sa.text(f"CREATE TRIGGER pr9b1_raise BEFORE INSERT ON {table} FOR EACH ROW "
                             "EXECUTE FUNCTION pr9b1_raise()"))


def _drop_trigger(env, table):
    with env.observer.begin() as conn:
        conn.execute(sa.text(f"DROP TRIGGER pr9b1_raise ON {table}"))
        conn.execute(sa.text("DROP FUNCTION pr9b1_raise()"))


@pytest.mark.parametrize("table,errcode,constraint", [
    ("decryptage_turn_reservations", "23505", "uq_other_constraint"),
    ("decryptage_turn_reservations", "23503", "uq_decryptage_turn_reservations_turn"),
    ("decryptage_turn_reservations", "23514", "ck_decryptage_turn_reservations_state"),
    ("conversation_identities", "23505", "uq_other_constraint"),
    ("conversation_identities", "23503", "conversation_identities_pkey"),
    ("conversation_identities", "23503", "uq_conversation_identities_key_owner"),
])
def test_a_non_targeted_constraint_error_is_never_an_idempotent_success(env, alice, table, errcode, constraint):
    before = env.full_snapshot()
    _raising_trigger(env, table, errcode, constraint)
    try:
        assert env.run(alice, reserve_command()).status == UNAVAILABLE
        if table == "conversation_identities":
            with Session(env.legacy_engine) as db:  # service historique : propagée telle quelle
                with pytest.raises(sa.exc.IntegrityError):
                    ii.register_or_resolve_conversation(db, user_id=USER_A, conversation_key="conv-legacy")
                db.rollback()
    finally:
        _drop_trigger(env, table)
    assert env.full_snapshot() == before


def test_a_targeted_name_without_a_visible_winner_is_refused_closed(env, alice):
    """23505 sur la contrainte d'identité mais AUCUNE ligne gagnante relue :
    refus fermé (jamais un succès, jamais une ligne inventée)."""
    before = env.full_snapshot()
    _raising_trigger(env, "decryptage_turn_reservations", "23505", "uq_decryptage_turn_reservations_turn")
    try:
        assert env.run(alice, reserve_command()).status == UNAVAILABLE
    finally:
        _drop_trigger(env, "decryptage_turn_reservations")
    assert env.full_snapshot() == before


# --------------------------------------------------------------------------
# Conversations : writers historiques et F2, deux ordres, nom de contrainte
# --------------------------------------------------------------------------

def _legacy_register(env, user_id, key, *, hold=None):
    """register_or_resolve_conversation historique dans sa propre Session ;
    hold : événement attendu avant le COMMIT (transaction tenue ouverte)."""
    with Session(env.legacy_engine) as db:
        try:
            identity = ii.register_or_resolve_conversation(db, user_id=user_id, conversation_key=key)
            owner = identity.user_id
        except ii.ConversationOwnershipConflict:
            db.rollback()
            return "conflict"
        if hold is not None:
            hold.reached.set()
            assert hold.release.wait(15)
        db.commit()
        return owner


@pytest.mark.parametrize("second_owner", [USER_A, USER_B])
def test_two_historical_writers_resolve_the_winner_without_reassignment(env, second_owner):
    env.confirmed(BOB, USER_B)
    env.confirmed()
    hold = Pause("never")
    first = started(lambda: _legacy_register(env, USER_A, "conv-race", hold=hold))
    assert hold.reached.wait(15)
    second = started(lambda: _legacy_register(env, second_owner, "conv-race"))
    wait_until(lambda: env.waiting(LEGACY_APP) >= 1, message="le second writer doit attendre l'index")
    hold.release.set()
    assert first.outcome() == USER_A
    assert second.outcome() == (USER_A if second_owner == USER_A else "conflict")
    assert [(r["conversation_key"], r["user_id"]) for r in env.conversations()] == [("conv-race", USER_A)]


@pytest.mark.parametrize("legacy_owner", [USER_A, USER_B])
def test_legacy_conversation_writer_first_then_f2_reservation(env, alice, legacy_owner):
    env.confirmed(BOB, USER_B)
    hold = Pause("never")
    legacy = started(lambda: _legacy_register(env, legacy_owner, CONV, hold=hold))
    assert hold.reached.wait(15)
    reservation = started(lambda: env.run(alice, reserve_command()))
    wait_until(lambda: env.waiting(APP_NAME) >= 1, message="F2 doit attendre la conversation non committée")
    hold.release.set()
    assert legacy.outcome() == legacy_owner
    outcome = reservation.outcome()
    if legacy_owner == USER_A:
        assert kind(outcome) is dr.LeaseGranted
    else:
        assert (outcome.status, outcome.value) == (NOT_FOUND, None)
        assert env.all_reservations() == []
    assert [(r["conversation_key"], r["user_id"]) for r in env.conversations()] == [(CONV, legacy_owner)]


@pytest.mark.parametrize("legacy_owner", [USER_A, USER_B])
def test_f2_reservation_first_then_legacy_conversation_writer(env, alice, legacy_owner):
    env.confirmed(BOB, USER_B)
    pause, reservation = paused(env, alice, reserve_command())
    legacy = started(lambda: _legacy_register(env, legacy_owner, CONV))
    wait_until(lambda: env.waiting(LEGACY_APP) >= 1, message="le writer historique doit attendre l'index")
    pause.release.set()
    assert kind(reservation.outcome()) is dr.LeaseGranted
    assert legacy.outcome() == (USER_A if legacy_owner == USER_A else "conflict")
    assert [(r["conversation_key"], r["user_id"]) for r in env.conversations()] == [(CONV, USER_A)]


@pytest.fixture
def owner_unique_first(env, pg_url):  # noqa: F811
    """Recrée la PK APRÈS uq_conversation_identities_key_owner : l'ordre de
    contrôle des index uniques (OID) change, PostgreSQL signale alors une
    collision de même propriétaire sur la contrainte composite. Schéma
    reconstruit ensuite (prepare_schema)."""
    with env.observer.begin() as conn:
        fks = conn.execute(sa.text(
            "SELECT conname, conrelid::regclass::text AS rel, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = 'conversation_identities'::regclass "
            "AND conindid = 'conversation_identities_pkey'::regclass")).mappings().all()
        for fk in fks:
            conn.execute(sa.text(f'ALTER TABLE {fk["rel"]} DROP CONSTRAINT {fk["conname"]}'))
        conn.execute(sa.text("ALTER TABLE conversation_identities DROP CONSTRAINT conversation_identities_pkey"))
        conn.execute(sa.text("ALTER TABLE conversation_identities ADD CONSTRAINT conversation_identities_pkey "
                             "PRIMARY KEY (conversation_key)"))
        for fk in fks:
            conn.execute(sa.text(f'ALTER TABLE {fk["rel"]} ADD CONSTRAINT {fk["conname"]} {fk["def"]}'))
    yield
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


def test_postgresql_may_report_the_composite_constraint_and_both_writers_resolve_it(env, alice, owner_unique_first):
    own_conversation(env, "conv-first")
    with env.observer.begin() as conn:
        conn.execute(sa.text("SAVEPOINT s"))
        with pytest.raises(sa.exc.IntegrityError) as reported:
            conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                                 "VALUES ('conv-first', :u, now())"), {"u": USER_A})
        conn.execute(sa.text("ROLLBACK TO SAVEPOINT s"))
    diag = reported.value.orig.diag
    # Contre-épreuve : l'ancien prédicat (nom de la PK seulement) ne l'aurait pas reconnue.
    assert (diag.sqlstate, diag.constraint_name) == ("23505", "uq_conversation_identities_key_owner")
    # Writers historiques concurrents (même propriétaire) : collision résolue.
    hold = Pause("never")
    first = started(lambda: _legacy_register(env, USER_A, "conv-order", hold=hold))
    assert hold.reached.wait(15)
    second = started(lambda: _legacy_register(env, USER_A, "conv-order"))
    wait_until(lambda: env.waiting(LEGACY_APP) >= 1)
    hold.release.set()
    assert first.outcome() == USER_A and second.outcome() == USER_A
    # Adaptateur F2 contre un writer historique (même propriétaire) : idem.
    hold = Pause("never")
    legacy = started(lambda: _legacy_register(env, USER_A, CONV, hold=hold))
    assert hold.reached.wait(15)
    reservation = started(lambda: env.run(alice, reserve_command()))
    wait_until(lambda: env.waiting(APP_NAME) >= 1)
    hold.release.set()
    assert legacy.outcome() == USER_A and kind(reservation.outcome()) is dr.LeaseGranted


# --------------------------------------------------------------------------
# /decryptage historique contre réservation F2, deux ordres
# --------------------------------------------------------------------------

def test_legacy_route_completes_during_a_paused_reservation_and_the_retry_blocks(env, alice, legacy_route):
    own_conversation(env)
    command = reserve_command()
    pause, reservation = paused(env, alice, command)
    legacy = started(lambda: legacy_decryptage(env, legacy_route, USER_A, CONV, command.client_turn_id))
    result = legacy.outcome()  # termine SANS attendre la transaction F2 suspendue
    assert result["delivery_status"] == "pending"
    pause.release.set()
    lease = reservation.outcome()
    assert kind(lease) is dr.LeaseGranted  # sa lecture précédait la livraison historique
    blocked = env.run(alice, command)
    assert kind(blocked) is dr.ReservationBlocked and blocked.value.blocked_reason == "legacy_collision"
    assert env.run(alice, c.RecordDecryptageAttemptFailure.from_lease(lease.value.lease, FAILURE)).status == \
        REVISION_CONFLICT
    [delivery] = env.deliveries()
    assert all(r["assistant_delivery_id"] is None for r in env.all_reservations())  # jamais adoptée
    assert delivery["status"] == "pending"


def test_uncommitted_legacy_claim_first_then_the_reservation_is_blocked(env, alice):
    command = reserve_command()
    hold = Pause("never")

    def legacy():
        with Session(env.legacy_engine) as db:
            from core import assistant_delivery as ad
            ad.claim_delivery(db, user_id=USER_A, conversation_key=CONV, surface="decryptage",
                              source_user_turn_id=command.client_turn_id, delivery_ordinal=1,
                              request_fingerprint="c" * 64, visible_content_fingerprint="d" * 64,
                              response_payload={"analysis": "x"}, private_metadata={},
                              delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
            hold.reached.set()
            assert hold.release.wait(15)
            db.commit()

    writer = started(legacy)
    assert hold.reached.wait(15)
    reservation = started(lambda: env.run(alice, command))
    wait_until(lambda: env.waiting(APP_NAME) >= 1, message="F2 attend la conversation historique non committée")
    hold.release.set()
    writer.outcome()
    outcome = reservation.outcome()
    assert kind(outcome) is dr.ReservationBlocked and outcome.value.blocked_reason == "legacy_collision"
    [row] = env.reservations()
    assert (row["state"], row["lease_epoch"], row["attempts_used"], row["lease_token_hash"]) == (
        "blocked", 0, 0, None)


# --------------------------------------------------------------------------
# Mutations F2 de profil et de portefeuille, révocation : deux ordres
# --------------------------------------------------------------------------

def _profile_command(env):
    return c.UpdateProfileLevel("avance", env.profile()[1])


@pytest.mark.parametrize("other", ["profile", "portfolio"])
@pytest.mark.parametrize("first", ["reservation", "other"])
def test_reservation_and_f2_mutations_wait_coherently_at_the_users_rank(env, alice, other, first):
    command = reserve_command()
    other_command = _profile_command(env) if other == "profile" else create_command()

    def run_other(hook=None):
        return env.runner(hook=hook).run(alice.credential(), other_command)

    if first == "reservation":
        pause, head = paused(env, alice, command)
        tail = started(run_other)
    else:
        pause = Pause("executed")
        head = started(lambda: run_other(pause))
        assert pause.reached.wait(15)
        tail = started(lambda: env.run(alice, command))
    wait_until(lambda: env.waiting(APP_NAME) >= 1, message="attente au rang users attendue")
    pause.release.set()
    outcomes = [head.outcome(), tail.outcome()]
    assert all(o.status == AUTHORIZED for o in outcomes), outcomes
    assert len(env.reservations()) == 1


def test_revocation_after_authorization_waits_for_the_reservation_commit(env, alice):
    pause, reservation = paused(env, alice, reserve_command())
    revocation = started(lambda: env.service.revoke_session(alice.session_id, "security"))
    wait_until(lambda: env.waiting(APP_NAME) >= 1, message="la révocation attend le FOR SHARE de session")
    pause.release.set()
    assert kind(reservation.outcome()) is dr.LeaseGranted
    assert revocation.outcome() is True
    lease = env.reservations()[0]
    assert env.run(alice, reserve_command()).status == SESSION_INVALID
    assert env.reservations() == [lease]


def test_a_revocation_committed_while_waiting_at_the_users_rank_is_seen(env, alice):
    second_session = env.confirmed("alice.bis@example.com", USER_A)  # seconde identité, même compte
    pause, head = paused(env, alice, reserve_command())
    tail = started(lambda: env.run(second_session, reserve_command()))
    wait_until(lambda: env.waiting(APP_NAME) >= 1)
    env.service.revoke_session(second_session.session_id, "security")
    pause.release.set()
    assert kind(head.outcome()) is dr.LeaseGranted
    assert tail.outcome().status == SESSION_INVALID
    assert len(env.reservations()) == 1  # rien n'est écrit par la session révoquée


# --------------------------------------------------------------------------
# T_lease_decision lu APRÈS l'attente du verrou de réservation
# --------------------------------------------------------------------------

def _hold_row(env, reservation_id, seconds):
    """Connexion tierce qui tient la réservation FOR UPDATE `seconds`."""
    holding = threading.Event()

    def hold():
        with env.observer.connect() as conn:
            conn.execute(sa.text("BEGIN"))
            conn.execute(sa.text("SELECT 1 FROM decryptage_turn_reservations WHERE id = :i FOR UPDATE"),
                         {"i": reservation_id})
            holding.set()
            time.sleep(seconds)
            conn.execute(sa.text("ROLLBACK"))

    holder = started(hold)
    assert holding.wait(10)
    return holder


def test_a_lease_that_expires_while_waiting_for_the_row_lock_is_decided_after_the_wait(env, alice):
    command = reserve_command()
    first = env.run(alice, command).value.lease
    env.force(first.reservation_id, "lease_expires_at = clock_timestamp() + interval '1 second'")
    forced_expiry = env.reservation(first.reservation_id)["lease_expires_at"]
    holder = _hold_row(env, first.reservation_id, 2.5)
    retry = started(lambda: env.run(alice, command))
    wait_until(lambda: env.waiting(APP_NAME) >= 1, message="la reprise doit attendre le verrou de la réservation")
    holder.outcome()
    outcome = retry.outcome()
    # Avant l'attente, le bail était actif ; décidé APRÈS : expiré, repris.
    assert kind(outcome) is dr.LeaseGranted and outcome.value.lease.lease_epoch == 2
    row = env.reservation(first.reservation_id)
    assert row["updated_at"] >= forced_expiry  # T_lease_decision postérieur à l'échéance franchie pendant l'attente
    assert row["lease_expires_at"] - row["updated_at"] == timedelta(seconds=360)


def test_a_failure_report_that_waits_past_the_expiry_is_refused(env, alice):
    command = reserve_command()
    lease = env.run(alice, command).value.lease
    env.force(lease.reservation_id, "lease_expires_at = clock_timestamp() + interval '1 second'")
    before_wait = env.reservation(lease.reservation_id)
    holder = _hold_row(env, lease.reservation_id, 2.5)
    report = started(lambda: env.run(alice, c.RecordDecryptageAttemptFailure.from_lease(lease, FAILURE)))
    wait_until(lambda: env.waiting(APP_NAME) >= 1)
    holder.outcome()
    assert report.outcome().status == REVISION_CONFLICT  # expiré pendant l'attente : aucune mutation
    assert env.reservation(lease.reservation_id) == before_wait
