"""V0-T08 2B-2 PR 0 — attestations métier internes du portefeuille F2, contre
un VRAI PostgreSQL (16 et 18 en CI), sous le VRAI runner et le registre de
PRODUCTION.

Sessions émises par le vrai moteur F2 ; positions et reçus observés par une
connexion indépendante (xmin : aucune écriture). Chaque cas vérifie que la
qualification correspond à la branche RÉELLEMENT exécutée de _write_portfolio,
qu'elle n'est exposée qu'après un COMMIT confirmé (_run_attested), et que
résultat, reçu et octets canoniques sont EXACTEMENT ceux de run().

1. création, modification effective, valeurs identiques (no-op), suppression ;
2. rejeux : création, modification (effective et no-op), suppression, création
   après suppression de la position ;
3. refus : conflit de révision, conflit d'idempotence, position et reçu
   étrangers, session invalide : aucune attestation ;
4. rollback, COMMIT refusé, COMMIT incertain : aucune attestation (la
   statistique est perdue, jamais l'opération) ;
5. concurrence réelle (deux sessions du même compte, verrou users) ;
6. conservation exacte des résultats et des reçus canoniques.
"""
import logging
import uuid

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import attestations as att
from core.authz_f2 import portfolio_results as results
from core.authz_f2.runner import (
    AUTHORIZED,
    IDEMPOTENCY_CONFLICT,
    NOT_FOUND,
    REVISION_CONFLICT,
    SESSION_INVALID,
    UNAVAILABLE,
    AttestedOutcome,
    AuthorizationOutcome,
)
from tests.authz_f2_support import BOB, USER_A, USER_B, AuthzEnv, create_command, delete_command, replace_command
from tests.f2_support import prepare_schema
from tests.test_authz_f2_concurrency import Background, Pause, deadlocks, wait_until
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

IDLE = lambda point: None  # noqa: E731 — runner sans point d'interruption


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = AuthzEnv(pg_url)
    before = deadlocks(environment)
    yield environment
    assert deadlocks(environment) == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def attested(env, issued, command, **kwargs) -> AttestedOutcome:
    out = env.runner(**kwargs)._run_attested(issued.credential(), command)
    assert type(out) is AttestedOutcome and type(out.outcome) is AuthorizationOutcome
    return out


def ok(env, issued, command, qualification, **kwargs):
    """Succès : résultat canonique exact (type, same_result) ET attestation
    liée au principal, à la nature et à l'operation_id de la commande."""
    out = attested(env, issued, command, **kwargs)
    outcome = out.outcome
    assert outcome.status == AUTHORIZED and type(outcome.value) is results.PortfolioWriteResult, outcome
    operation = _adapters_operation(command)
    assert results.same_result(outcome.value, operation, command.operation_id)
    assert out.attestation == att.BusinessAttestation(outcome.principal.user_id, operation, command.operation_id,
                                                      qualification)
    assert out.attestation.new_effect is (qualification in att.NEW_EFFECTS)
    return outcome


def refused(env, issued, command, status, **kwargs):
    before = (positions_state(env), env.receipts(), env.receipts(USER_B))
    out = attested(env, issued, command, **kwargs)
    assert out.outcome == AuthorizationOutcome(status) and out.attestation is None, out
    assert (positions_state(env), env.receipts(), env.receipts(USER_B)) == before  # rien d'écrit
    return out


def _adapters_operation(command) -> str:
    return _adapters.OPERATION_TYPES[type(command)]


def positions_state(env):
    """Toutes les positions (révision, xmin : inchangés = aucune écriture)."""
    with env.observer.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(
            "SELECT id, user_id, position_revision, xmin::text AS xmin FROM portfolio_positions ORDER BY id"
        )).mappings()]


# --------------------------------------------------------------------------
# 1. Branches effectives et no-op
# --------------------------------------------------------------------------

def test_a_real_creation_is_attested_created(env, alice):
    outcome = ok(env, alice, create_command(), att.PORTFOLIO_CREATED)
    assert outcome.value.status == 201 and env.position(outcome.value.position_id)["position_revision"] == 0
    assert len(env.receipts()) == 1


def test_an_effective_update_is_attested_updated(env, alice):
    position = ok(env, alice, create_command(quantity="4"), att.PORTFOLIO_CREATED).value.position_id
    outcome = ok(env, alice, replace_command(position, 0, quantity="5"), att.PORTFOLIO_UPDATED)
    assert outcome.value.view.position_revision == 1 and env.position(position)["position_revision"] == 1


def test_an_update_with_identical_values_is_attested_noop(env, alice):
    position = env.insert_position(quantity=4.5, purchase_price=100.0, target_percent=10.0)
    before = env.position(position)
    outcome = ok(env, alice, replace_command(position, 0), att.PORTFOLIO_NOOP)  # mêmes valeurs binary64
    after = env.position(position)
    assert (after["xmin"], after["position_revision"], after["updated_at"]) == (
        before["xmin"], 0, before["updated_at"])  # aucun UPDATE
    assert outcome.value.view.position_revision == 0 and len(env.receipts()) == 1  # reçu écrit, comme avant


def test_a_real_deletion_is_attested_deleted(env, alice):
    position = env.insert_position()
    outcome = ok(env, alice, delete_command(position, 0), att.PORTFOLIO_DELETED)
    assert outcome.value.deleted_revision == 0 and env.position(position) is None


# --------------------------------------------------------------------------
# 2. Rejeux depuis le reçu canonique
# --------------------------------------------------------------------------

def _replayed(env, issued, command, first):
    before = (positions_state(env), env.receipts())
    again = ok(env, issued, command, att.PORTFOLIO_REPLAY)
    assert again.value == first.value and again.value.body == first.value.body  # mêmes octets
    assert (positions_state(env), env.receipts()) == before  # aucune écriture
    return again


def test_a_creation_replay_is_attested_replay(env, alice):
    command = create_command()
    first = ok(env, alice, command, att.PORTFOLIO_CREATED)
    _replayed(env, alice, command, first)


@pytest.mark.parametrize("quantity, first_qualification", [("5", att.PORTFOLIO_UPDATED),
                                                           ("4.5", att.PORTFOLIO_NOOP)])
def test_an_update_replay_is_attested_replay(env, alice, quantity, first_qualification):
    position = env.insert_position(quantity=4.5, purchase_price=100.0, target_percent=10.0)
    command = replace_command(position, 0, quantity=quantity)
    first = ok(env, alice, command, first_qualification)
    _replayed(env, alice, command, first)


def test_a_deletion_replay_after_the_deletion_is_attested_replay(env, alice):
    position = env.insert_position()
    command = delete_command(position, 0)
    first = ok(env, alice, command, att.PORTFOLIO_DELETED)
    _replayed(env, alice, command, first)


def test_a_creation_replay_after_the_position_was_deleted_is_still_a_replay(env, alice):
    command = create_command()
    first = ok(env, alice, command, att.PORTFOLIO_CREATED)
    ok(env, alice, delete_command(first.value.position_id, 0), att.PORTFOLIO_DELETED)
    _replayed(env, alice, command, first)  # reçu de création restitué : jamais une nouvelle création


# --------------------------------------------------------------------------
# 3. Refus : aucune attestation
# --------------------------------------------------------------------------

def test_a_revision_conflict_has_no_attestation(env, alice):
    position = env.insert_position(revision=3)
    refused(env, alice, replace_command(position, 2, quantity="9"), REVISION_CONFLICT)
    refused(env, alice, delete_command(position, 2), REVISION_CONFLICT)


def test_an_idempotency_conflict_has_no_attestation(env, alice):
    command = create_command(quantity="4")
    ok(env, alice, command, att.PORTFOLIO_CREATED)
    refused(env, alice, create_command(command.operation_id, quantity="7"), IDEMPOTENCY_CONFLICT)


def test_a_foreign_position_or_receipt_has_no_attestation_and_never_crosses_users(env, alice):
    bob = env.confirmed(BOB, USER_B)
    theirs = env.insert_position(user_id=USER_B)
    refused(env, alice, replace_command(theirs, 0, quantity="9"), NOT_FOUND)
    refused(env, alice, delete_command(theirs, 0), NOT_FOUND)
    # Même operation_id chez deux comptes : deux triplets, deux attestations
    # distinctes, chacune liée à SON propriétaire serveur.
    command = create_command()
    mine = attested(env, alice, command).attestation
    other = attested(env, bob, command).attestation
    assert (mine.owner_user_id, other.owner_user_id) == (USER_A, USER_B) and mine != other
    assert mine.qualification == other.qualification == att.PORTFOLIO_CREATED


def test_an_invalid_session_has_no_attestation(env, alice):
    command = create_command()
    ok(env, alice, command, att.PORTFOLIO_CREATED)
    env.service.revoke_session(alice.session_id, "security")
    refused(env, alice, command, SESSION_INVALID)  # même un rejeu est refusé avant tout


# --------------------------------------------------------------------------
# 4. Rollback, COMMIT refusé, COMMIT incertain
# --------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["position_written", "receipt_ready", "receipt_written", "executed"])
def test_a_failure_before_commit_leaves_no_attestation_and_the_retry_is_a_first_execution(env, alice, point):
    command = create_command()

    def fail(at):
        if at == point:
            raise RuntimeError("panne avant COMMIT (simulée)")

    refused(env, alice, command, UNAVAILABLE, hook=fail)
    ok(env, alice, command, att.PORTFOLIO_CREATED)  # rien n'avait survécu : vraie création, jamais un rejeu


def test_a_failed_commit_leaves_no_attestation(env, alice):
    def refuse_commit(conn):
        raise OSError("COMMIT refusé")

    command = create_command()
    sa.event.listen(env.engine, "commit", refuse_commit)
    try:
        refused(env, alice, command, UNAVAILABLE)
    finally:
        sa.event.remove(env.engine, "commit", refuse_commit)
    ok(env, alice, command, att.PORTFOLIO_CREATED)


def test_an_uncertain_commit_leaves_no_attestation_and_the_retry_is_a_replay(env, alice):
    """COMMIT effectué, confirmation perdue : 503 sans attestation (statistique
    perdue, préférée à une attestation présumée) ; la même intention retrouve
    son reçu : rejeu, jamais une seconde création."""
    commits = []

    def commit_then_lose_connection(conn):
        if not commits and conn.info.get("receipt"):
            commits.append("commit")
            conn.connection.dbapi_connection.commit()
            raise OSError("connexion perdue après COMMIT")

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO portfolio_write_idempotency"):
            conn.info["receipt"] = True

    command = create_command()
    sa.event.listen(env.engine, "before_cursor_execute", mark)
    sa.event.listen(env.engine, "commit", commit_then_lose_connection)
    try:
        out = attested(env, alice, command)
    finally:
        sa.event.remove(env.engine, "commit", commit_then_lose_connection)
        sa.event.remove(env.engine, "before_cursor_execute", mark)
        env.engine.dispose()
    assert commits == ["commit"] and out.outcome == AuthorizationOutcome(UNAVAILABLE) and out.attestation is None
    assert len(env.receipts()) == 1  # committé malgré l'erreur
    ok(env, alice, command, att.PORTFOLIO_REPLAY)


# --------------------------------------------------------------------------
# 5. Concurrence réelle
# --------------------------------------------------------------------------

def _held_then_contended(env, first, second, first_command, second_command):
    pause = Pause("executed")
    holder = Background(lambda: env.runner(hook=pause)._run_attested(first.credential(), first_command))
    holder.start()
    assert pause.reached.wait(10)
    contender = Background(lambda: env.runner(hook=IDLE)._run_attested(second.credential(), second_command))
    contender.start()
    try:
        wait_until(lambda: env.waiting_on_lock() == 1, message="le second n'attend pas")
    finally:
        pause.release.set()
    return holder.outcome(), contender.outcome()


@pytest.fixture
def sessions(env):
    return env.confirmed(), env.confirmed("alice2@example.com", USER_A)


def test_the_same_creation_in_two_sessions_is_created_once_then_replayed(env, sessions):
    command = create_command()
    won, other = _held_then_contended(env, *sessions, command, command)
    assert (won.attestation.qualification, other.attestation.qualification) == (att.PORTFOLIO_CREATED,
                                                                                att.PORTFOLIO_REPLAY)
    assert won.attestation.operation_id == other.attestation.operation_id == command.operation_id
    assert other.outcome.value.body == won.outcome.value.body and len(env.receipts()) == 1


def test_two_effective_updates_from_one_revision_attest_one_update_and_no_conflict_attestation(env, sessions):
    position = env.insert_position(quantity=4.0)
    won, lost = _held_then_contended(env, *sessions, replace_command(position, 0, quantity="5"),
                                     replace_command(position, 0, quantity="6"))
    assert won.attestation.qualification == att.PORTFOLIO_UPDATED
    assert lost.outcome == AuthorizationOutcome(REVISION_CONFLICT) and lost.attestation is None


def test_two_noops_on_one_revision_are_both_noops(env, sessions):
    position = env.insert_position(quantity=4.5, purchase_price=100.0, target_percent=10.0)
    first, second = _held_then_contended(env, *sessions, replace_command(position, 0), replace_command(position, 0))
    assert first.attestation.qualification == second.attestation.qualification == att.PORTFOLIO_NOOP
    assert first.attestation.operation_id != second.attestation.operation_id  # deux opérations distinctes
    assert env.position(position)["position_revision"] == 0


def test_update_against_delete_attests_only_what_really_happened(env, sessions):
    position = env.insert_position(quantity=4.0)
    deleted, late = _held_then_contended(env, *sessions, delete_command(position, 0),
                                         replace_command(position, 0, quantity="5"))
    assert deleted.attestation.qualification == att.PORTFOLIO_DELETED
    assert late.outcome == AuthorizationOutcome(NOT_FOUND) and late.attestation is None


# --------------------------------------------------------------------------
# 6. Conservation exacte des résultats et reçus ; aucune nouvelle cause d'échec
# --------------------------------------------------------------------------

def test_run_and_run_attested_return_the_same_outcome_and_receipt_bytes(env, alice):
    a, b = create_command(), create_command()
    historical = env.runner().run(alice.credential(), a)
    out = attested(env, alice, b)
    assert type(historical) is type(out.outcome) is AuthorizationOutcome
    assert [f for f in AuthorizationOutcome.__slots__] == ["status", "value", "principal"]
    for outcome, command in ((historical, a), (out.outcome, b)):
        assert type(outcome.value) is results.PortfolioWriteResult  # jamais l'enveloppe interne
        assert results.same_result(outcome.value, "portfolio_position_create", command.operation_id)
    receipts = {r["operation_id"]: r for r in env.receipts()}
    for outcome, command in ((historical, a), (out.outcome, b)):
        receipt = receipts[command.operation_id]
        # Reçu canonique : payload JSONB rendu par le renderer versionné = octets du résultat.
        assert results.canonical_json(results.parse_json(receipt["payload_text"])) == outcome.value.body
        assert (receipt["result_version"], receipt["result_status"]) == (results.RESULT_VERSION, 201)
    with env.observer.connect() as conn:  # aucune colonne, aucune table nouvelle
        columns = conn.execute(sa.text("SELECT column_name FROM information_schema.columns "
                                       "WHERE table_name = 'portfolio_write_idempotency' ORDER BY 1")).scalars().all()
    assert set(columns) == set(receipts[a.operation_id]) - {"payload_text"}


def test_an_attestation_that_cannot_be_built_never_fails_the_operation(env, alice, monkeypatch):
    def broken(*args, **kwargs):
        raise att.AttestationInvalid("panne simulée du contrat")

    monkeypatch.setattr(att, "BusinessAttestation", broken)
    command = create_command()
    out = attested(env, alice, command)
    assert out.outcome.status == AUTHORIZED and out.attestation is None  # statistique perdue, opération intacte
    assert type(out.outcome.value) is results.PortfolioWriteResult and len(env.receipts()) == 1


def test_an_attestation_bound_to_another_owner_is_dropped_never_the_operation(env, alice, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oryx.authz_f2")
    original = att.portfolio
    monkeypatch.setattr(att, "portfolio", lambda owner, *rest: original(USER_B, *rest))
    out = attested(env, alice, create_command())
    assert out.outcome.status == AUTHORIZED and out.attestation is None
    assert "attestation=dropped" in caplog.text and USER_B not in caplog.text and USER_A not in caplog.text
