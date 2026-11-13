"""ACT-L1 / F04-C V0.3 — double registre et soumetteur explicite (sans base).

Freeze § 4 (admissions), § 5 (protocole REGISTERED -> STARTED -> FINISHED |
REGISTERED -> ABANDONED, atomicité inter-registres), § 8.C (courses start /
abandon / finish / DRAINING, compteurs jamais négatifs, aucun double
terminal, notification thread-safe sans bloquer la boucle).
"""
import asyncio
import random
import threading
import time

import anyio
import pytest

from core.f2_runtime import work_registry as wr


def registry_open(**kwargs):
    registry = wr.WorkRegistry(**kwargs)
    registry.open_admissions()
    return registry


# --------------------------------------------------------------------------
# 1. Admissions
# --------------------------------------------------------------------------

def test_admissions_are_refused_until_opened_and_after_closing():
    registry = wr.WorkRegistry()
    assert registry.admit() is None  # INERT / STARTING : fermé
    registry.open_admissions()
    admission = registry.admit()
    assert admission is not None and admission.live
    registry.close_admissions()
    assert registry.admit() is None  # DRAINING : aucune nouvelle admission
    assert admission.live  # l'ancienne reste vivante (non convertie en refus)
    assert admission.release() is True and admission.release() is False  # une seule libération
    snap = registry.snapshot()
    assert (snap.admitted_total, snap.released_total, snap.admissions_active) == (1, 1, 0)


def test_an_admission_without_thread_prevents_sealing_even_with_zero_jobs():
    registry = registry_open()
    admission = registry.admit()
    registry.close_admissions()
    snap = registry.snapshot()
    assert snap.jobs_non_terminal == 0 and snap.admissions_active == 1
    assert registry.seal_if_idle() is False  # admissions == 0 ET jobs == 0 exigés
    admission.release()
    assert registry.seal_if_idle() is True
    assert registry.admit() is None


def test_sealing_is_refused_while_admissions_are_still_accepted():
    registry = registry_open()
    assert registry.is_idle() and registry.seal_if_idle() is False  # OPEN : jamais scellé
    registry.close_admissions()
    assert registry.seal_if_idle() is True
    with pytest.raises(RuntimeError):
        registry.open_admissions()


# --------------------------------------------------------------------------
# 2. Protocole des travaux
# --------------------------------------------------------------------------

def test_registration_requires_a_live_admission_of_this_registry():
    registry, other = registry_open(), registry_open()
    for bad in (None, object(), other.admit()):
        with pytest.raises(wr.SubmissionRefused):
            registry.register(bad)
    admission = registry.admit()
    admission.release()
    with pytest.raises(wr.SubmissionRefused):
        registry.register(admission)  # libérée : plus aucune soumission
    assert registry.snapshot().refused_submissions == 4


def test_nominal_protocol_registered_started_finished():
    registry = registry_open()
    admission = registry.admit()
    job = registry.register(admission)
    assert job.state == wr.REGISTERED and not registry.is_idle()
    assert job.start() is True and job.state == wr.STARTED
    assert job.abandon() is False  # après STARTED : sans effet, le fil terminera
    job.finish()
    assert job.state == wr.FINISHED
    admission.release()
    snap = registry.snapshot()
    assert snap.idle and (snap.registered_total, snap.started_total, snap.finished_total,
                          snap.abandoned_total, snap.violations) == (1, 1, 1, 0, 0)


def test_abandon_before_start_is_an_atomic_proof_of_non_start():
    registry = registry_open()
    admission = registry.admit()
    job = registry.register(admission)
    assert job.abandon() is True and job.state == wr.ABANDONED
    assert job.start() is False  # le fil constate l'abandon : le callable ne sera pas exécuté
    assert job.abandon() is False
    snap = registry.snapshot()
    assert (snap.abandoned_total, snap.started_total, snap.jobs_non_terminal, snap.violations) == (1, 0, 0, 0)


def test_out_of_protocol_transitions_are_refused_and_counted_never_silent():
    events = []
    registry = registry_open(observer=lambda kind, fields: events.append(kind))
    admission = registry.admit()
    job = registry.register(admission)
    job.finish()  # FINISHED sans STARTED
    assert job.state == wr.REGISTERED
    assert job.start() is True
    job.finish()
    job.finish()  # double fin
    assert job.start() is False  # redémarrage d'un terminal
    snap = registry.snapshot()
    assert snap.finished_total == 1 and snap.violations == 3 and events.count("VIOLATION") == 3
    assert snap.jobs_non_terminal == 0


def test_a_released_admission_keeps_its_started_job_tracked():
    registry = registry_open()
    admission = registry.admit()
    job = registry.register(admission)
    job.start()
    admission.release()  # requête annulée / sortie
    registry.close_admissions()
    assert registry.snapshot().jobs_started == 1 and registry.seal_if_idle() is False
    job.finish()
    assert registry.seal_if_idle() is True


def test_submissions_during_draining_are_allowed_only_for_live_admissions():
    registry = registry_open()
    admitted = registry.admit()
    registry.close_admissions()
    job = registry.register(admitted)  # admise avant DRAINING, encore vivante : permis
    assert job.state == wr.REGISTERED
    assert registry.admit() is None
    job.abandon()
    admitted.release()
    assert registry.seal_if_idle() is True
    with pytest.raises(wr.SubmissionRefused):
        registry.register(admitted)


# --------------------------------------------------------------------------
# 3. Courses (fils réels)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_start_abandon_finish_races_never_double_terminal_nor_lose_a_job(seed):
    rng = random.Random(seed)
    registry = registry_open()
    admission = registry.admit()
    jobs = [registry.register(admission) for _ in range(400)]
    results = {}
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        for job in jobs:
            if rng.random() < 0.5:
                time.sleep(0)
            if job.start():
                results[job.id] = "started"
                job.finish()

    def canceller():
        barrier.wait()
        for job in reversed(jobs):
            if job.abandon():
                assert results.get(job.id) is None
                results.setdefault(job.id, "abandoned")

    threads = [threading.Thread(target=worker), threading.Thread(target=canceller)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    snap = registry.snapshot()
    assert snap.started_total + snap.abandoned_total == 400
    assert snap.finished_total == snap.started_total and snap.jobs_non_terminal == 0 and snap.violations == 0
    assert {j.state for j in jobs} <= {wr.FINISHED, wr.ABANDONED}
    assert sum(1 for j in jobs if j.state == wr.ABANDONED) == snap.abandoned_total


def test_draining_and_late_registrations_race_never_seal_with_a_live_submitter():
    """Barrière commune : tant qu'une admission vivante peut inscrire, le
    scellement est impossible ; une fois scellé, aucune inscription."""
    for _ in range(200):
        registry = registry_open()
        admission = registry.admit()
        outcome = {}

        def submit_then_release():
            try:
                job = registry.register(admission)
                job.start()
                job.finish()
                outcome["job"] = True
            except wr.SubmissionRefused:
                outcome["job"] = False
            admission.release()

        t = threading.Thread(target=submit_then_release)
        t.start()
        registry.close_admissions()
        sealed = False
        while not sealed:
            sealed = registry.seal_if_idle()
        t.join(10)
        snap = registry.snapshot()
        assert outcome["job"] is True  # l'admission vivante a toujours pu soumettre
        assert snap.idle and snap.finished_total == 1 and snap.violations == 0


def test_wait_idle_is_notified_from_a_worker_thread_without_blocking_the_loop():
    registry = registry_open()
    admission = registry.admit()
    job = registry.register(admission)
    job.start()
    admission.release()
    registry.close_admissions()
    ticks = []

    async def main():
        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        tick = asyncio.ensure_future(ticker())
        threading.Timer(0.3, job.finish).start()
        started = time.monotonic()
        assert await registry.wait_idle() is True
        elapsed = time.monotonic() - started
        tick.cancel()
        return elapsed

    elapsed = asyncio.run(main())
    assert 0.25 < elapsed < 5 and len(ticks) >= 10  # la boucle a continué de tourner
    assert registry.seal_if_idle() is True


def test_wait_idle_timeout_reports_not_idle():
    registry = registry_open()
    registry.admit()
    registry.close_admissions()
    assert asyncio.run(registry.wait_idle(0.05)) is False
    assert asyncio.run(registry.wait_idle(0)) is False


# --------------------------------------------------------------------------
# 4. Soumetteur explicite (anyio réel)
# --------------------------------------------------------------------------

def run_with_admission(registry, coro_factory):
    async def main():
        admission = registry.admit()
        token = wr.CURRENT_ADMISSION.set(admission)
        try:
            return await coro_factory()
        finally:
            wr.CURRENT_ADMISSION.reset(token)
            admission.release()

    return asyncio.run(main())


def test_the_submitter_runs_in_a_worker_thread_and_finishes_the_job():
    registry = registry_open()
    submit = wr.RegisteredSubmitter(registry)
    main_thread = threading.get_ident()
    seen = {}

    def work(a, *, b):
        seen["thread"] = threading.get_ident()
        seen["state_during"] = registry.snapshot().jobs_started
        return a + b

    assert run_with_admission(registry, lambda: submit(work, 2, b=3)) == 5
    assert seen["thread"] != main_thread and seen["state_during"] == 1
    snap = registry.snapshot()
    assert snap.idle and (snap.finished_total, snap.abandoned_total) == (1, 0)


def test_the_submitter_refuses_without_admission_and_launches_no_thread():
    registry = registry_open()
    submit = wr.RegisteredSubmitter(registry)
    called = []
    with pytest.raises(wr.SubmissionRefused):
        asyncio.run(submit(lambda: called.append(1)))
    assert called == [] and registry.snapshot().registered_total == 0


def test_a_callable_exception_still_finishes_the_job():
    registry = registry_open()
    submit = wr.RegisteredSubmitter(registry)

    def boom():
        raise RuntimeError("panne")

    with pytest.raises(RuntimeError):
        run_with_admission(registry, lambda: submit(boom))
    snap = registry.snapshot()
    assert snap.idle and snap.finished_total == 1 and snap.violations == 0


def test_native_cancellation_while_waiting_for_a_thread_token_abandons_without_running():
    """Limiteur AnyIO saturé : le travail est REGISTERED, jamais STARTED ;
    l'annulation native l'abandonne ; libérer le jeton n'exécute rien."""
    registry = registry_open()
    submit = wr.RegisteredSubmitter(registry)
    blocker, release = threading.Event(), threading.Event()
    called = []

    async def main():
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        a1 = registry.admit()
        t1 = wr.CURRENT_ADMISSION.set(a1)
        occupy = asyncio.ensure_future(submit(lambda: (blocker.set(), release.wait(10))))
        wr.CURRENT_ADMISSION.reset(t1)
        await asyncio.to_thread(blocker.wait, 10)
        a2 = registry.admit()
        t2 = wr.CURRENT_ADMISSION.set(a2)
        victim = asyncio.ensure_future(submit(lambda: called.append("ran")))
        wr.CURRENT_ADMISSION.reset(t2)
        await asyncio.sleep(0.1)
        assert registry.snapshot().jobs_registered == 1
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        a2.release()
        release.set()
        await occupy
        a1.release()
        await asyncio.sleep(0.1)

    asyncio.run(main())
    snap = registry.snapshot()
    assert called == [] and snap.idle and (snap.finished_total, snap.abandoned_total) == (1, 1)


def test_native_cancellation_while_the_thread_runs_keeps_the_job_until_it_ends():
    registry = registry_open()
    submit = wr.RegisteredSubmitter(registry)
    inside, release = threading.Event(), threading.Event()

    async def main():
        admission = registry.admit()
        token = wr.CURRENT_ADMISSION.set(admission)
        task = asyncio.ensure_future(submit(lambda: (inside.set(), release.wait(10))))
        wr.CURRENT_ADMISSION.reset(token)
        await asyncio.to_thread(inside.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        admission.release()  # la requête est sortie
        registry.close_admissions()
        snap = registry.snapshot()
        assert snap.admissions_active == 0 and snap.jobs_started == 1
        assert registry.seal_if_idle() is False  # jamais de fermeture : le fil travaille
        release.set()
        assert await registry.wait_idle(10) is True

    asyncio.run(main())
    assert registry.seal_if_idle() is True and registry.snapshot().finished_total == 1
