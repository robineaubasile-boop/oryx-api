"""T1 de R1 PR 4 (C.4.5 §10.4) : quota par opération sous concurrence
entre IP DISTINCTES, connexions PostgreSQL indépendantes, PostgreSQL réel.

Exécuté séparément pour /invitation/resume et /email-link/result :

1. une opération éligible du type correspondant et ses justificatifs
   valides ; neuf resume_attempt durables committés dans la fenêtre,
   produits par de vraies requêtes depuis plusieurs IP ;
2. au moins deux processus (spawn), chacun avec son application et son
   moteur NullPool, se présentant avec deux IP dont les valeurs
   NORMALISÉES sont distinctes ; leurs pg_backend_pid() sont relevés ;
3. le processus de test tient l'opération FOR UPDATE sur sa propre
   connexion, libère la barrière, attend que les requêtes concurrentes
   soient TOUTES en attente de ce verrou (pg_locks : la concurrence est
   réelle au niveau du verrou d'opération), puis relâche ;
4. résultat : exactement une admission (200), les autres 429
   rate_limited sans cookie, aucune erreur, aucun interblocage ; lecture
   finale sur une connexion d'observation indépendante : exactement dix
   resume_attempt pour l'opération, toutes IP confondues, jamais onze ;
   une nouvelle demande valide dans la même fenêtre laisse dix.

Les budgets HTTP par IP (30 / 5 min par action, 60 / 5 min au total) ne
sont jamais approchés : seul le quota par opération peut refuser.
Le rapport (IP normalisées, PID, départ à neuf, attentes observées,
arrivée à dix) est attaché au rapport JUnit de la session
(record_testsuite_property, compatible xunit2), une clé par test.
"""
import multiprocessing
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.auth_f2.normalize import normalize_client_ip
from core.auth_f2.policy import OPERATION_RESUME_LIMIT
from core.auth_f2_http.cookies import active_name, candidate_name
from tests.f2_pr4_concurrency_worker import worker
from tests.f2_pr4_support import IP_A, IP_B, Pr4Env, activate, link_result, linked, resume
from tests.f2_support import prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CTX = multiprocessing.get_context("spawn")
PREPARATION_IPS = ("192.0.2.31", "192.0.2.32", "192.0.2.33")


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def report(request, record_testsuite_property):
    def record(key, value):
        record_testsuite_property(f"{request.node.name}.{key}", value)
    return record


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = Pr4Env(pg_url)
    yield environment
    environment.dispose()


def _lock_waiters(observer) -> tuple:
    """(attentes du verrou d'opération, attentes de verrou au total) :
    backends bloqués sur un verrou de ligne de auth_operations (FOR UPDATE
    de l'opération) et, au total, sur un verrou quelconque."""
    with observer.connect() as conn:
        return tuple(conn.execute(sa.text(
            "SELECT count(*) FILTER (WHERE a.query ILIKE '%FROM auth_operations WHERE id%FOR UPDATE%'), count(*) "
            "FROM pg_stat_activity a WHERE a.datname = current_database() AND a.wait_event_type = 'Lock'"
        )).one())


def _run_t1(env, pg_url, operation_id, path, body, cookie_name, cookie_value, ips, record):  # noqa: F811
    normalized = {ip: normalize_client_ip(ip) for ip in ips}
    assert len(set(normalized.values())) >= 2, "T1 exige deux IP normalisées distinctes"
    assert env.resume_attempts(operation_id) == OPERATION_RESUME_LIMIT - 1  # départ à neuf, committés
    holder = env.observer.connect()
    trans = holder.begin()
    holder_pid = holder.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
    holder.execute(sa.text("SELECT id FROM auth_operations WHERE id = :o FOR UPDATE"), {"o": operation_id})
    barrier = CTX.Barrier(len(ips) + 1)
    queue = CTX.Queue()
    processes = [CTX.Process(target=worker, args=(pg_url, {
        "ip": ip, "path": path, "body": body, "cookie_name": cookie_name, "cookie_value": cookie_value},
        barrier, queue)) for ip in ips]
    try:
        for p in processes:
            p.start()
        barrier.wait(timeout=180)
        # Une requête par IP normalisée attend le verrou d'opération ; une
        # seconde requête de la même IP attend d'abord le sujet IP (rang
        # inférieur de l'ordre global). Toutes sont bloquées en même temps.
        expected = (len(set(normalized.values())), len(ips))
        deadline = time.monotonic() + 60
        waiting = (0, 0)
        while time.monotonic() < deadline:
            waiting = _lock_waiters(env.observer)
            if waiting == expected:
                break
            time.sleep(0.05)
        assert waiting == expected, "les requêtes concurrentes doivent toutes attendre en même temps"
        assert env.resume_attempts(operation_id) == OPERATION_RESUME_LIMIT - 1
    finally:
        trans.rollback()
        holder.close()
    results = [queue.get(timeout=180) for _ in processes]
    for p in processes:
        p.join(timeout=60)
    errors = [r[1] for r in results if r[0] == "error"]
    assert errors == []
    statuses = sorted((r[1], tuple(sorted(r[2].items()))) for r in results)
    pid_sets = [set(r[3]) for r in results]
    for i, pids in enumerate(pid_sets):
        assert pids and holder_pid not in pids
        for other in pid_sets[i + 1:]:
            assert not pids & other  # connexions PostgreSQL indépendantes
    reported_ips = {r[4] for r in results}
    assert reported_ips == set(normalized.values())
    final = env.resume_attempts(operation_id)  # connexion d'observation indépendante
    record("t1_normalized_ips", sorted(reported_ips))
    record("t1_backend_pids", [sorted(p) for p in pid_sets])
    record("t1_holder_pid", holder_pid)
    record("t1_lock_waiters_observed", waiting)
    record("t1_resume_attempts_before", OPERATION_RESUME_LIMIT - 1)
    record("t1_resume_attempts_after", final)
    record("t1_statuses", [s[0] for s in statuses])
    return statuses, [r[5] for r in results], final


def _distinct_ip_pairs():
    return [IP_A, IP_B]


def test_t1_invitation_resume_across_two_distinct_ips(env, pg_url, report):  # noqa: F811
    app = env.app()
    owner = env.browser(app, PREPARATION_IPS[0])
    started, done = activate(env, owner)
    op = started["operation_id"]
    for i in range(OPERATION_RESUME_LIMIT - 1):  # neuf admissions réelles, plusieurs IP
        b = env.browser(app, PREPARATION_IPS[i % 3])
        b.jar.update(owner.candidates())
        assert resume(b, started).status_code == 200
    cookie = candidate_name(uuid.UUID(started["candidate_id"]))
    statuses, cookies_set, final = _run_t1(
        env, pg_url, op, "/invitation/resume", {"operation_id": op, "candidate_id": started["candidate_id"]},
        cookie, owner.jar[cookie], _distinct_ip_pairs(), report)
    codes = [s[0] for s in statuses]
    assert codes == [200, 429]
    assert dict(statuses[1][1]) == {"error": "rate_limited"}
    assert dict(statuses[0][1])["generation_id"] == done["generation_id"]
    assert cookies_set == [True, False] or cookies_set == [False, True]
    assert final == OPERATION_RESUME_LIMIT
    # Nouvelle demande valide dans la même fenêtre : toujours dix.
    late = env.browser(app, "192.0.2.99")
    late.jar.update(owner.candidates())
    assert resume(late, started).json() == {"error": "rate_limited"}
    assert env.resume_attempts(op) == OPERATION_RESUME_LIMIT
    assert env.count("oryx_sessions") == 1
    window = env.rows("SELECT min(occurred_at) AS first, clock_timestamp() AS now FROM auth_abuse_events "
                      "WHERE auth_operation_id = :o AND event_kind = 'resume_attempt'", o=op)[0]
    assert window["now"] - window["first"] < timedelta(minutes=4)


def test_t1_invitation_resume_with_four_processes_on_two_ips(env, pg_url, report):  # noqa: F811
    app = env.app()
    owner = env.browser(app, PREPARATION_IPS[0])
    started, _ = activate(env, owner)
    op = started["operation_id"]
    for i in range(OPERATION_RESUME_LIMIT - 1):
        b = env.browser(app, PREPARATION_IPS[i % 3])
        b.jar.update(owner.candidates())
        assert resume(b, started).status_code == 200
    cookie = candidate_name(uuid.UUID(started["candidate_id"]))
    statuses, _, final = _run_t1(
        env, pg_url, op, "/invitation/resume", {"operation_id": op, "candidate_id": started["candidate_id"]},
        cookie, owner.jar[cookie], [IP_A, IP_B, IP_A, IP_B], report)
    assert [s[0] for s in statuses] == [200, 429, 429, 429]
    assert final == OPERATION_RESUME_LIMIT


def test_t1_email_link_result_across_two_distinct_ips(env, pg_url, report):  # noqa: F811
    app = env.app()
    owner = env.browser(app, PREPARATION_IPS[0])
    generation, op, _ = linked(env, owner)
    for i in range(OPERATION_RESUME_LIMIT - 1):
        b = env.browser(app, PREPARATION_IPS[i % 3])
        b.jar.update(owner.actives())
        assert link_result(b, generation, op).status_code == 200
    cookie = active_name(uuid.UUID(generation))
    statuses, cookies_set, final = _run_t1(
        env, pg_url, op, "/email-link/result", {"generation_id": generation, "operation_id": op},
        cookie, owner.jar[cookie], _distinct_ip_pairs(), report)
    assert [(s[0], dict(s[1])) for s in statuses] == [(200, {"status": "email_linked", "operation_id": op}),
                                                       (429, {"error": "rate_limited"})]
    assert cookies_set == [False, False]  # aucune réponse de résultat ne pose de cookie
    assert final == OPERATION_RESUME_LIMIT
    late = env.browser(app, "192.0.2.99")
    late.jar.update(owner.actives())
    assert link_result(late, generation, op).json() == {"error": "rate_limited"}
    assert env.resume_attempts(op) == OPERATION_RESUME_LIMIT
    generation_row = env.rows("SELECT delivery_status FROM oryx_session_generations")[0]
    assert generation_row["delivery_status"] == "pending"


def test_t1_email_link_result_with_four_processes_on_two_ips(env, pg_url, report):  # noqa: F811
    app = env.app()
    owner = env.browser(app, PREPARATION_IPS[0])
    generation, op, _ = linked(env, owner)
    for i in range(OPERATION_RESUME_LIMIT - 1):
        b = env.browser(app, PREPARATION_IPS[i % 3])
        b.jar.update(owner.actives())
        assert link_result(b, generation, op).status_code == 200
    cookie = active_name(uuid.UUID(generation))
    statuses, _, final = _run_t1(
        env, pg_url, op, "/email-link/result", {"generation_id": generation, "operation_id": op},
        cookie, owner.jar[cookie], [IP_A, IP_B, IP_B, IP_A], report)
    assert [s[0] for s in statuses] == [200, 429, 429, 429]
    assert final == OPERATION_RESUME_LIMIT
