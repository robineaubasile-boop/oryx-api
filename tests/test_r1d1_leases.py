"""Tests de R1-D1 : lease persistée des ObservationEvaluationRun dans le
service T3-B (core/observation_service.py, migration 0014).

1. Sans base : validation structurelle des primitives de lease.

2. Contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL vers une base DÉDIÉE
   dont le nom contient "test" ; sinon SKIPPÉS, rien n'est simulé) :

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1d1_test \\
           python -m pytest tests/test_r1d1_leases.py

   claim / renew / verify, propriété exigée par toute mutation munie d'un
   jeton (add_observation, complete, fail), lease expirée reprenable, ancien
   détenteur qui revient sans jamais muter le run repris, lease effacée à la
   complétion et à l'échec, horloge PostgreSQL (jamais celle du worker),
   CHECK de 0014 en défense finale, verrou du run détenu par la transaction
   résultat (un claim concurrent attend puis échoue).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from core import observation_service as svc
from core.observation_service import (
    AlreadyLeased,
    InvalidEvaluationState,
    InvalidObservationPayload,
    LostEvaluationLease,
)
from tests.test_observation_service import (  # noqa: F401 — fixtures
    Sessions,
    _add,
    _blocked,
    _committed_run,
    _event,
    _obs_rows,
    _run_row,
    db,
    engine,
)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

LEASE = 300


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

class _NoDB:
    def __getattr__(self, name):
        raise AssertionError(f"accès base inattendu : {name}")


@pytest.mark.parametrize("seconds", [0, -1, 86401, True, 1.5, "60", None])
def test_lease_seconds_are_validated_before_any_database_access(seconds):
    with pytest.raises(InvalidObservationPayload, match="lease_seconds"):
        svc.claim_evaluation_lease(_NoDB(), run_id=uuid.uuid4(), lease_seconds=seconds)
    with pytest.raises(InvalidObservationPayload, match="lease_seconds"):
        svc.renew_evaluation_lease(_NoDB(), run_id=uuid.uuid4(), lease_token=uuid.uuid4(), lease_seconds=seconds)


@pytest.mark.parametrize("token", ["not-a-uuid", str(uuid.uuid4()), 1])
def test_lease_token_type_is_validated_before_any_database_access(token):
    with pytest.raises(InvalidObservationPayload, match="lease_token"):
        svc.verify_evaluation_lease(_NoDB(), run_id=uuid.uuid4(), lease_token=token)
    with pytest.raises(InvalidObservationPayload, match="lease_token"):
        svc.fail_evaluation_run(_NoDB(), run_id=uuid.uuid4(), failure_code="x", lease_token=token)
    with pytest.raises(InvalidObservationPayload, match="lease_token"):
        svc.complete_evaluation_run(_NoDB(), run_id=uuid.uuid4(), output_fingerprint="x", lease_token=token)


# --------------------------------------------------------------------------
# 2. PostgreSQL
# --------------------------------------------------------------------------

def _run(Sessions):
    return _committed_run(Sessions, _event(Sessions))


def _claim(Sessions, run_id, seconds=LEASE) -> uuid.UUID:
    with Sessions() as session:
        token = svc.claim_evaluation_lease(session, run_id=run_id, lease_seconds=seconds).lease_token
        session.commit()
        return token


def _expire(engine, run_id):
    """Simule l'écoulement de la durée de lease (aucun sleep)."""
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE observation_evaluation_runs SET lease_expires_at = clock_timestamp() "
                             "- interval '1 second' WHERE id = :id"), {"id": run_id})


def _db_now(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()


def _lease(engine, run_id):
    row = _run_row(engine, run_id)
    return row["lease_token"], row["lease_expires_at"]


def _status(engine, run_id):
    row = _run_row(engine, run_id)
    return row["execution_status"], row["interpretation_status"]


def _mutate(Sessions, action):
    """Exécute action(session) dans sa propre transaction (commit si OK)."""
    with Sessions() as session:
        try:
            result = action(session)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise


def test_pg_claim_sets_a_lease_on_the_database_clock_without_changing_statuses(engine, Sessions):
    run_id = _run(Sessions)
    assert _lease(engine, run_id) == (None, None)
    before = _db_now(engine)
    token = _claim(Sessions, run_id)
    after = _db_now(engine)
    stored_token, expires = _lease(engine, run_id)
    assert isinstance(token, uuid.UUID) and stored_token == token
    assert before + timedelta(seconds=LEASE) <= expires <= after + timedelta(seconds=LEASE)
    assert _status(engine, run_id) == ("running", "candidate")


def test_pg_lease_uses_the_postgresql_clock_not_the_worker_clock(engine, Sessions, monkeypatch):
    monkeypatch.setattr(svc, "_utcnow", lambda: datetime(2099, 1, 1, tzinfo=timezone.utc))
    run_id = _run(Sessions)
    _claim(Sessions, run_id)
    assert _lease(engine, run_id)[1] < datetime(2098, 1, 1, tzinfo=timezone.utc)


def test_pg_second_claim_on_a_valid_lease_is_refused_without_mutation(engine, Sessions):
    run_id = _run(Sessions)
    _claim(Sessions, run_id)
    before = _lease(engine, run_id)
    with pytest.raises(AlreadyLeased):
        _claim(Sessions, run_id)
    assert _lease(engine, run_id) == before


def test_pg_expired_lease_is_reclaimable_with_a_new_token(engine, Sessions):
    run_id = _run(Sessions)
    first = _claim(Sessions, run_id)
    _expire(engine, run_id)
    second = _claim(Sessions, run_id)
    assert second != first and _lease(engine, run_id)[0] == second
    assert _status(engine, run_id) == ("running", "candidate")


def test_pg_renew_with_the_right_token_extends_the_lease(engine, Sessions):
    run_id = _run(Sessions)
    token = _claim(Sessions, run_id, seconds=60)
    _, expires = _lease(engine, run_id)
    _mutate(Sessions, lambda s: svc.renew_evaluation_lease(s, run_id=run_id, lease_token=token, lease_seconds=600))
    assert _lease(engine, run_id) == (token, _lease(engine, run_id)[1])
    assert _lease(engine, run_id)[1] > expires + timedelta(seconds=500)


def test_pg_renew_with_a_wrong_or_expired_token_is_lost_without_mutation(engine, Sessions):
    run_id = _run(Sessions)
    token = _claim(Sessions, run_id)
    before = _lease(engine, run_id)
    with pytest.raises(LostEvaluationLease):
        _mutate(Sessions, lambda s: svc.renew_evaluation_lease(s, run_id=run_id, lease_token=uuid.uuid4(),
                                                               lease_seconds=LEASE))
    assert _lease(engine, run_id) == before
    _expire(engine, run_id)
    expired = _lease(engine, run_id)
    with pytest.raises(LostEvaluationLease):
        _mutate(Sessions, lambda s: svc.renew_evaluation_lease(s, run_id=run_id, lease_token=token,
                                                               lease_seconds=LEASE))
    assert _lease(engine, run_id) == expired  # jamais « ressuscitée »


@pytest.mark.parametrize("operation", ["complete", "fail", "add", "verify"])
def test_pg_mutations_with_a_wrong_token_are_refused(engine, Sessions, operation):
    run_id = _run(Sessions)
    _claim(Sessions, run_id)
    before = _run_row(engine, run_id)
    wrong = uuid.uuid4()
    actions = {
        "complete": lambda s: svc.complete_evaluation_run(s, run_id=run_id, output_fingerprint="o", lease_token=wrong),
        "fail": lambda s: svc.fail_evaluation_run(s, run_id=run_id, failure_code="provider_error", lease_token=wrong),
        "add": lambda s: _add(s, run_id, lease_token=wrong),
        "verify": lambda s: svc.verify_evaluation_lease(s, run_id=run_id, lease_token=wrong),
    }
    with pytest.raises(LostEvaluationLease):
        _mutate(Sessions, actions[operation])
    assert _run_row(engine, run_id) == before
    assert _obs_rows(engine, run_id) == []


@pytest.mark.parametrize("operation", ["complete", "fail", "add"])
def test_pg_mutations_without_a_token_never_bypass_a_valid_worker_lease(engine, Sessions, operation):
    run_id = _run(Sessions)
    _claim(Sessions, run_id)
    actions = {
        "complete": lambda s: svc.complete_evaluation_run(s, run_id=run_id, output_fingerprint="o"),
        "fail": lambda s: svc.fail_evaluation_run(s, run_id=run_id, failure_code="x"),
        "add": lambda s: _add(s, run_id),
    }
    with pytest.raises(AlreadyLeased):
        _mutate(Sessions, actions[operation])
    assert _status(engine, run_id) == ("running", "candidate")


def test_pg_a_run_without_lease_keeps_the_t3b_behavior(engine, Sessions):
    run_id = _run(Sessions)
    _mutate(Sessions, lambda s: _add(s, run_id))
    _mutate(Sessions, lambda s: svc.complete_evaluation_run(s, run_id=run_id, output_fingerprint="o"))
    assert _status(engine, run_id) == ("completed", "active") and _lease(engine, run_id) == (None, None)


def test_pg_complete_and_fail_with_the_token_clear_the_lease(engine, Sessions):
    completed, failed = _run(Sessions), _run(Sessions)
    token_c, token_f = _claim(Sessions, completed), _claim(Sessions, failed)
    _mutate(Sessions, lambda s: _add(s, completed, lease_token=token_c))
    _mutate(Sessions, lambda s: svc.complete_evaluation_run(s, run_id=completed, output_fingerprint="o",
                                                            lease_token=token_c))
    _mutate(Sessions, lambda s: svc.fail_evaluation_run(s, run_id=failed, failure_code="provider_error",
                                                        lease_token=token_f))
    assert _status(engine, completed) == ("completed", "active") and _lease(engine, completed) == (None, None)
    assert _status(engine, failed) == ("failed", "obsolete") and _lease(engine, failed) == (None, None)
    assert len(_obs_rows(engine, completed)) == 1


def test_pg_expired_lease_without_token_may_be_failed_by_an_operator(engine, Sessions):
    run_id = _run(Sessions)
    _claim(Sessions, run_id)
    _expire(engine, run_id)
    _mutate(Sessions, lambda s: svc.fail_evaluation_run(s, run_id=run_id, failure_code="manual"))
    assert _status(engine, run_id) == ("failed", "obsolete") and _lease(engine, run_id) == (None, None)


def test_pg_old_holder_returning_after_takeover_never_mutates_the_run(engine, Sessions):
    """A détient L1, L1 expire, B prend L2. A revient : renew, add, fail,
    complete sont tous LostEvaluationLease ; B termine normalement."""
    run_id = _run(Sessions)
    token_a = _claim(Sessions, run_id)
    _expire(engine, run_id)
    token_b = _claim(Sessions, run_id)
    snapshot = _run_row(engine, run_id)
    for action in (
        lambda s: svc.renew_evaluation_lease(s, run_id=run_id, lease_token=token_a, lease_seconds=LEASE),
        lambda s: _add(s, run_id, lease_token=token_a),
        lambda s: svc.fail_evaluation_run(s, run_id=run_id, failure_code="provider_error", lease_token=token_a),
        lambda s: svc.complete_evaluation_run(s, run_id=run_id, output_fingerprint="a", lease_token=token_a),
    ):
        with pytest.raises(LostEvaluationLease):
            _mutate(Sessions, action)
        assert _run_row(engine, run_id) == snapshot
    _mutate(Sessions, lambda s: svc.complete_evaluation_run(s, run_id=run_id, output_fingerprint="b",
                                                            lease_token=token_b))
    row = _run_row(engine, run_id)
    assert (row["execution_status"], row["output_fingerprint"], row["lease_token"]) == ("completed", "b", None)
    # Après la complétion par B, A ne peut toujours rien faire.
    with pytest.raises(LostEvaluationLease):
        _mutate(Sessions, lambda s: svc.fail_evaluation_run(s, run_id=run_id, failure_code="x", lease_token=token_a))


def test_pg_terminal_runs_cannot_be_claimed(engine, Sessions):
    completed = _committed_run(Sessions, _event(Sessions), end="complete")
    failed = _committed_run(Sessions, _event(Sessions), end="fail")
    for run_id in (completed, failed):
        with pytest.raises(InvalidEvaluationState):
            _claim(Sessions, run_id)
        assert _lease(engine, run_id) == (None, None)


def test_pg_crash_leaves_a_recoverable_running_candidate(engine, Sessions):
    """Crash brutal (aucun fail) : le run reste running / candidate, sa lease
    expire, il est reprenable par claim."""
    run_id = _run(Sessions)
    token = _claim(Sessions, run_id)
    with Sessions() as session:  # le worker meurt au milieu de sa transaction
        svc.verify_evaluation_lease(session, run_id=run_id, lease_token=token)
        _add(session, run_id, lease_token=token)
        session.rollback()
    assert _status(engine, run_id) == ("running", "candidate") and _obs_rows(engine, run_id) == []
    with pytest.raises(AlreadyLeased):
        _claim(Sessions, run_id)
    _expire(engine, run_id)
    assert _claim(Sessions, run_id) != token


def test_pg_verify_holds_the_run_lock_until_the_result_transaction_ends(engine, Sessions):
    """La transaction résultat détient le verrou du run après verify : même
    une lease qui expire pendant cette transaction ne peut pas être reprise
    avant son commit (le claim concurrent attend puis voit le run terminal)."""
    run_id = _run(Sessions)
    token = _claim(Sessions, run_id)
    holder, waiter = Sessions(), Sessions()
    try:
        svc.verify_evaluation_lease(holder, run_id=run_id, lease_token=token)
        _add(holder, run_id, lease_token=token)
        svc.complete_evaluation_run(holder, run_id=run_id, output_fingerprint="o", lease_token=token)
        result, error = _blocked(engine, holder, waiter,
                                 lambda s: svc.claim_evaluation_lease(s, run_id=run_id, lease_seconds=LEASE),
                                 waiting_on="SELECT observation_evaluation_runs")
    finally:
        holder.close()
        waiter.close()
    assert result is None and isinstance(error, InvalidEvaluationState)
    assert _status(engine, run_id) == ("completed", "active") and len(_obs_rows(engine, run_id)) == 1


def test_pg_lease_checks_are_the_final_defense(engine, Sessions):
    run_id = _run(Sessions)
    completed = _committed_run(Sessions, _event(Sessions), end="complete")
    with engine.connect() as conn:
        for statement, params in (
            ("UPDATE observation_evaluation_runs SET lease_token = :t WHERE id = :id", {"t": uuid.uuid4(), "id": run_id}),
            ("UPDATE observation_evaluation_runs SET lease_expires_at = now() WHERE id = :id", {"id": run_id}),
            ("UPDATE observation_evaluation_runs SET lease_token = :t, lease_expires_at = now() WHERE id = :id",
             {"t": uuid.uuid4(), "id": completed}),
        ):
            trans = conn.begin()
            with pytest.raises(sa.exc.IntegrityError, match="ck_observation_evaluation_runs_lease"):
                conn.execute(sa.text(statement), params)
            trans.rollback()
