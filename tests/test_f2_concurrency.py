"""Concurrence F2 multi-processus sur PostgreSQL réel (R1 PR 2).

Chaque scénario lance de vrais processus (multiprocessing, spawn), chacun
avec ses connexions, alignés sur une barrière : les quotas, verrous,
index uniques et FK composites sont exercés par PostgreSQL lui-même.
Aucune erreur (interblocage, violation, délai) n'est tolérée.
"""
import multiprocessing
import uuid

import pytest
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.policy import VERIFY_IP_LIMIT
from core.auth_f2.service import F2AuthService
from tests.f2_concurrency_worker import CountingSupabase, SilentMailer, keyring, worker
from tests.f2_support import clean_f2, create_profile_with_identity, prepare_schema, rows, scalar
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

IP = "203.0.113.50"
CTX = multiprocessing.get_context("spawn")


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def db(pg_url, schema):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    clean_f2(engine)
    yield engine
    engine.dispose()


def service(db, abuse_versions=("v1",), subject=None, init=True):
    ring = keyring(abuse_versions)
    if init:
        abuse.initialize_abuse_key_state(db, ring, abuse_versions[0])
    return F2AuthService(db, ring, CountingSupabase(subject), SilentMailer())


def run(pg_url, jobs, abuse_versions=("v1",), during=None):  # noqa: F811
    """jobs : [(scénario, args)]. during : action du processus de test
    exécutée juste après la barrière (rotation concurrente)."""
    barrier = CTX.Barrier(len(jobs) + 1)
    queue = CTX.Queue()
    processes = [CTX.Process(target=worker, args=(pg_url, name, args, list(abuse_versions), barrier, queue))
                 for name, args in jobs]
    for p in processes:
        p.start()
    barrier.wait(timeout=120)
    if during is not None:
        during()
    results = [queue.get(timeout=180) for _ in processes]
    for p in processes:
        p.join(timeout=60)
    errors = [r[1] for r in results if r[0] == "error"]
    assert errors == []
    return [r[1] for r in results], sum(r[2] for r in results), sum(r[3] for r in results)


def count(db, table, where="TRUE", **params):
    return scalar(db, f"SELECT count(*) FROM {table} WHERE {where}", **params)


def assert_markers_unique(db):
    duplicates = scalar(db, "SELECT count(*) FROM (SELECT subject_id, event_kind, occurred_at FROM auth_abuse_events "
                            "WHERE event_kind IN ('otp_request_refused', 'otp_verification_refused') "
                            "GROUP BY 1, 2, 3 HAVING count(*) > 1) d")
    assert duplicates == 0


def test_thirtieth_admitted_thirty_first_refused_across_processes(pg_url, db):  # noqa: F811
    service(db)
    statuses, _, verified = run(pg_url, [("verify_unknown", {"operation_id": uuid.uuid4(), "ip": IP})
                                         for _ in range(VERIFY_IP_LIMIT + 10)])
    assert statuses.count("rejected") == VERIFY_IP_LIMIT and statuses.count("refused") == 10
    assert count(db, "auth_abuse_events", "event_kind = 'otp_verification_attempt'") == VERIFY_IP_LIMIT
    assert 1 <= count(db, "auth_abuse_events", "event_kind = 'otp_verification_refused'") <= 2
    assert_markers_unique(db)
    assert verified == 0


def test_concurrent_wrong_codes_stop_exactly_at_five(pg_url, db):  # noqa: F811
    svc = service(db)
    start = svc.start_sign_in(email="alice@example.com", client_ip=IP)
    args = {"operation_id": start.operation_id, "candidate_id": start.candidate.candidate_id,
            "candidate_secret": start.candidate.secret.reveal(), "code": "11111111", "ip": IP}
    reasons, _, verified = run(pg_url, [("verify_code", dict(args)) for _ in range(9)])
    request = rows(db, "SELECT * FROM auth_otp_requests")[0]
    assert (request["status"], request["failure_count"]) == ("exhausted", 5)
    assert count(db, "auth_abuse_events", "event_kind = 'otp_failure'") == 5
    assert count(db, "auth_abuse_events", "event_kind = 'otp_email_block'") == 1
    assert reasons.count("code_incorrect") == 4 and reasons.count("email_blocked") == 5
    assert verified == 0


def test_concurrent_emissions_for_one_email_call_the_provider_once(pg_url, db):  # noqa: F811
    service(db)
    statuses, generated, _ = run(pg_url, [("issue", {"email": "alice@example.com", "ip": f"192.0.2.{i + 1}"})
                                          for i in range(10)])
    assert statuses.count("issued") == 1 and statuses.count("refused") == 9
    assert generated == 1 and count(db, "auth_otp_requests") == 1
    assert count(db, "auth_abuse_events", "event_kind = 'otp_request'") == 2
    assert_markers_unique(db)


def test_rotation_under_concurrent_load_keeps_exact_quotas_without_deadlock(pg_url, db):  # noqa: F811
    both = ("v1", "v2")
    service(db, both)

    def rotate():
        abuse.rotate_abuse_key(db, keyring(both), "v2")

    statuses, _, _ = run(pg_url, [("verify_unknown", {"operation_id": uuid.uuid4(), "ip": IP})
                                  for _ in range(VERIFY_IP_LIMIT + 10)], abuse_versions=both, during=rotate)
    assert statuses.count("rejected") == VERIFY_IP_LIMIT
    assert count(db, "auth_abuse_events", "event_kind = 'otp_verification_attempt'") == VERIFY_IP_LIMIT
    state = rows(db, "SELECT * FROM auth_abuse_key_state")[0]
    assert (state["current_version"], state["previous_version"]) == ("v2", "v1")
    # Après la rotation, les nouvelles admissions restent refusées (aucune remise à zéro).
    statuses, _, _ = run(pg_url, [("verify_unknown", {"operation_id": uuid.uuid4(), "ip": IP}) for _ in range(5)],
                         abuse_versions=both)
    assert statuses == ["refused"] * 5


def test_rotation_during_concurrent_emissions(pg_url, db):  # noqa: F811
    both = ("v1", "v2")
    service(db, both)
    statuses, generated, _ = run(pg_url, [("issue", {"email": "bob@example.com", "ip": f"192.0.2.{i + 1}"})
                                          for i in range(12)], abuse_versions=both,
                                 during=lambda: abuse.rotate_abuse_key(db, keyring(both), "v2"))
    assert statuses.count("issued") == 1 and generated == 1


def _invited_session(svc):
    created = svc.create_invitation(cohort="beta")
    begin = svc.begin_invitation_activation(token=created.token)
    return svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session


def _linking(svc, session, email, ip):
    linking = svc.start_email_linking(session=session.credential(), email=email, client_ip=ip)
    assert linking.status == "issued", linking
    return {"operation_id": linking.operation_id, "generation_id": session.generation_id,
            "active_secret": session.active_secret.reveal(), "code": "24681357", "ip": ip}


def test_concurrent_linkings_keep_one_identity_per_profile(pg_url, db):  # noqa: F811
    svc = service(db)
    session = _invited_session(svc)
    first = _linking(svc, session, "bob@example.com", "192.0.2.1")
    second = _linking(svc, session, "carol@example.com", "192.0.2.2")
    first["subject"] = "00000000-0000-4000-8000-0000000000b0"
    second["subject"] = "00000000-0000-4000-8000-0000000000c0"
    reasons, _, _ = run(pg_url, [("verify_linking", first), ("verify_linking", second)])
    assert sorted(reasons) == ["new_identity", "profile_already_linked"]
    assert count(db, "auth_identities") == 1


def test_concurrent_linkings_of_one_subject_never_merge_profiles(pg_url, db):  # noqa: F811
    svc = service(db)
    subject = "00000000-0000-4000-8000-0000000000d0"
    jobs = []
    for i, email in enumerate(("dave@example.com", "dave2@example.com")):
        args = _linking(svc, _invited_session(svc), email, f"192.0.2.{i + 10}")
        args["subject"] = subject
        jobs.append(("verify_linking", args))
    reasons, _, _ = run(pg_url, jobs)
    assert sorted(reasons) == ["conflict_other_profile", "new_identity"]
    assert count(db, "auth_identities", "subject = :s", s=subject) == 1


def test_concurrent_invitation_completions_consume_it_once(pg_url, db):  # noqa: F811
    svc = service(db)
    created = svc.create_invitation(cohort="beta")
    jobs = []
    for _ in range(3):
        begin = svc.begin_invitation_activation(token=created.token)
        jobs.append(("complete_invitation", {"operation_id": begin.operation_id,
                                             "candidate_id": begin.candidate.candidate_id,
                                             "candidate_secret": begin.candidate.secret.reveal()}))
    jobs.append(jobs[0])  # doublon exact de la même opération
    statuses, _, _ = run(pg_url, jobs)
    assert statuses.count("invitation_activated") == 1
    assert count(db, "users") == 1 and count(db, "oryx_sessions") == 1
    assert count(db, "auth_operations", "status = 'committed'") == 1


def test_duplicate_verification_creates_one_session(pg_url, db):  # noqa: F811
    subject = "00000000-0000-4000-8000-0000000000e0"
    svc = service(db, subject=subject)
    create_profile_with_identity(db, type("S", (), {"subject_for": lambda self, e: subject})(), "erin@example.com")
    start = svc.start_sign_in(email="erin@example.com", client_ip=IP)
    args = {"operation_id": start.operation_id, "candidate_id": start.candidate.candidate_id,
            "candidate_secret": start.candidate.secret.reveal(), "code": "24681357", "ip": IP, "subject": subject}
    reasons, _, verified = run(pg_url, [("verify_code", dict(args)) for _ in range(6)])
    assert reasons.count("authenticated") == 1 and verified == 1
    assert count(db, "oryx_sessions") == 1 and count(db, "oryx_session_generations") == 1


def test_resume_revoke_and_authenticate_race_without_deadlock(pg_url, db):  # noqa: F811
    svc = service(db)
    created = svc.create_invitation(cohort="beta")
    begin = svc.begin_invitation_activation(token=created.token)
    done = svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session
    resume = {"operation_id": begin.operation_id, "candidate_id": begin.candidate.candidate_id,
              "candidate_secret": begin.candidate.secret.reveal()}
    auth = {"generation_id": done.generation_id, "active_secret": done.active_secret.reveal()}
    jobs = [("resume", resume)] * 6 + [("authenticate", auth)] * 6 + [("revoke", {"session_id": done.session_id})] * 2
    statuses, _, _ = run(pg_url, jobs)
    assert statuses.count("revoked") == 1 and statuses.count("not_revoked") == 1
    assert svc.resume_result(operation_id=begin.operation_id, candidate=begin.candidate).status == "rejected"
    assert svc.authenticate_session(done.credential()) is None


def test_linking_verification_races_revocation_and_authentication_without_deadlock(pg_url, db):  # noqa: F811
    svc = service(db)
    session = _invited_session(svc)
    first = _linking(svc, session, "fay@example.com", "192.0.2.30")
    second = _linking(svc, session, "gus@example.com", "192.0.2.31")
    first["subject"] = "00000000-0000-4000-8000-0000000000f0"
    second["subject"] = "00000000-0000-4000-8000-0000000000f1"
    auth = {"generation_id": session.generation_id, "active_secret": session.active_secret.reveal()}
    jobs = ([("verify_linking", first), ("verify_linking", second)] + [("authenticate", auth)] * 6
            + [("revoke", {"session_id": session.session_id})])
    statuses, _, _ = run(pg_url, jobs)
    assert statuses.count("revoked") == 1
    linked = [s for s in statuses if s in ("new_identity", "profile_already_linked", "proof_not_applicable",
                                            "credential_invalid")]
    assert len(linked) == 2 and statuses.count("new_identity") <= 1
    assert count(db, "auth_identities") == statuses.count("new_identity")
    assert svc.authenticate_session(session.credential()) is None
