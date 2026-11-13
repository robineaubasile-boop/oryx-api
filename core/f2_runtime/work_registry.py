"""Double registre F04-C : admissions HTTP F2 et travaux physiques (ACT-L1 / F04-C V0.3) — DORMANT.

Contrat : docs/act_l1_f04c_freeze_v03.md, §§ 4 et 5. Aucun effet à l'import
(aucun fil, aucune lecture d'environnement, aucun réseau). Seul le point
d'entrée F04-C (core.f2_runtime.entrypoint) construit un WorkRegistry.

Deux suivis complémentaires, protégés par UNE serrure commune
(threading.Lock) :

1. admissions : un jeton (Admission) par appel ASGI F2 admis, créé
   atomiquement avant tout await (admit), libéré une seule fois (release) ;
2. travaux physiques (Job) : inscrits par le soumetteur RegisteredSubmitter
   AVANT toute soumission à AnyIO, avec le protocole fermé

       REGISTERED -> STARTED -> FINISHED
       REGISTERED -> ABANDONED

   - start()   : première instruction du wrapper dans le fil physique ;
                 False si le travail est ABANDONED (le callable n'est
                 alors JAMAIS exécuté : aucun effet métier, SQL ou
                 fournisseur) ;
   - finish()  : clause finally du fil physique, après le retour ou
                 l'échec réel du callable ; seule voie vers FINISHED ;
   - abandon() : côté requête (finally de l'attente HTTP, y compris sous
                 asyncio.CancelledError) ; effectif SEULEMENT avant STARTED
                 (preuve atomique de non-démarrage) ; sans effet ensuite.

Invariants (sous la serrure) :

- une inscription exige une admission VIVANTE de CE registre ; une
  admission libérée ne peut plus rien inscrire ;
- pendant DRAINING (close_admissions), aucune nouvelle admission ; les
  admissions déjà acceptées restent vivantes jusqu'à leur libération et
  peuvent encore inscrire leurs travaux ;
- idle := admissions_actives == 0 ET travaux_non_terminaux == 0, évalué
  d'un seul tenant sous la serrure. Après close_admissions, idle est STABLE :
  plus aucune admission ne peut naître, donc plus aucune inscription ;
  seal_if_idle() le constate et scelle le registre (toute inscription
  ultérieure est refusée) avant que l'enveloppe ne ferme les ressources ;
- états terminaux exclusifs, comptés une fois, compteurs jamais négatifs ;
  toute transition hors protocole est refusée et comptée dans
  `violations` (jamais silencieuse).

Les attentes de drainage (wait_idle) sont notifiées depuis n'importe quel fil
par loop.call_soon_threadsafe : la boucle événementielle n'est jamais
bloquée.
"""
import asyncio
import contextvars
import functools
import itertools
import threading
from dataclasses import dataclass

import anyio.to_thread

REGISTERED = "registered"
STARTED = "started"
FINISHED = "finished"
ABANDONED = "abandoned"
TERMINAL_STATES = frozenset({FINISHED, ABANDONED})

# Admission de l'appel ASGI F2 courant : posée par l'enveloppe F04-C dans la
# tâche de la requête, lue par le soumetteur (même tâche, avant soumission).
CURRENT_ADMISSION = contextvars.ContextVar("oryx_f2_admission", default=None)


class SubmissionRefused(RuntimeError):
    """Soumission sans admission vivante de ce registre, ou registre scellé.
    Aucun fil n'est lancé ; les sept sites la traitent comme toute autre
    exception d'exécution (refus fermé existant, 503)."""


class JobAbandoned(RuntimeError):
    """Levée DANS le fil si le travail avait été abandonné avant son
    démarrage : le callable n'est pas invoqué."""


@dataclass(frozen=True)
class RegistrySnapshot:
    accepting: bool
    sealed: bool
    admissions_active: int
    jobs_registered: int
    jobs_started: int
    admitted_total: int
    released_total: int
    registered_total: int
    started_total: int
    finished_total: int
    abandoned_total: int
    refused_submissions: int
    violations: int

    @property
    def jobs_non_terminal(self) -> int:
        return self.jobs_registered + self.jobs_started

    @property
    def idle(self) -> bool:
        return self.admissions_active == 0 and self.jobs_non_terminal == 0


class Admission:
    """Jeton d'un appel ASGI F2 admis (créé par WorkRegistry.admit)."""

    __slots__ = ("_registry", "id", "_live")

    def __init__(self, registry, admission_id):
        self._registry = registry
        self.id = admission_id
        self._live = True

    def __repr__(self) -> str:
        return f"Admission({self.id}, live={self._live})"

    @property
    def live(self) -> bool:
        with self._registry._lock:
            return self._live

    def release(self) -> bool:
        """Libère l'admission une seule fois ; True si cet appel l'a libérée."""
        return self._registry._release(self)


class Job:
    """Travail physique inscrit (créé par WorkRegistry.register)."""

    __slots__ = ("_registry", "id", "admission_id", "_state")

    def __init__(self, registry, job_id, admission_id):
        self._registry = registry
        self.id = job_id
        self.admission_id = admission_id
        self._state = REGISTERED

    def __repr__(self) -> str:
        return f"Job({self.id}, {self._state})"

    @property
    def state(self) -> str:
        with self._registry._lock:
            return self._state

    def start(self) -> bool:
        return self._registry._start(self)

    def finish(self) -> None:
        self._registry._finish(self)

    def abandon(self) -> bool:
        return self._registry._abandon(self)


class WorkRegistry:
    """Double registre (voir le module). observer(kind, fields) facultatif :
    appelé SOUS la serrure, dans l'ordre exact des transitions ; il doit être
    rapide, non bloquant, et ses exceptions sont ignorées."""

    def __init__(self, *, observer=None):
        if observer is not None and not callable(observer):
            raise TypeError("observateur appelable attendu")
        self._lock = threading.Lock()
        self._observer = observer
        self._ids = itertools.count(1)
        self._accepting = False
        self._sealed = False
        self._admissions = set()
        self._jobs = {}
        self._waiters = []
        self._admitted = self._released = 0
        self._registered = self._started = self._finished = self._abandoned = 0
        self._refused = self._violations = 0

    def __repr__(self) -> str:
        return "WorkRegistry(<private>)"

    def __reduce__(self):
        raise TypeError("WorkRegistry n'est pas sérialisable")

    # -- observation ------------------------------------------------------

    def _emit(self, kind, **fields):
        if self._observer is not None:
            try:
                self._observer(kind, fields)
            except Exception:  # noqa: BLE001 — l'observation ne modifie jamais le protocole
                pass

    def _violation(self, kind, **fields):
        self._violations += 1
        self._emit("VIOLATION", what=kind, **fields)

    def _idle_locked(self) -> bool:
        return not self._admissions and not self._jobs

    def _notify_locked(self):
        if not self._idle_locked() or not self._waiters:
            return
        waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(_resolve, future)
            except RuntimeError:  # boucle fermée : plus personne n'attend
                pass

    # -- admissions -------------------------------------------------------

    def open_admissions(self) -> None:
        with self._lock:
            if self._sealed:
                raise RuntimeError("registre scellé")
            self._accepting = True
            self._emit("ADMISSIONS_OPEN")

    def close_admissions(self) -> None:
        """Passage à DRAINING : plus aucune NOUVELLE admission (idempotent)."""
        with self._lock:
            self._accepting = False
            self._emit("ADMISSIONS_CLOSED", admissions=len(self._admissions), jobs=len(self._jobs))
            self._notify_locked()

    def admit(self):
        """Admission atomique, ou None si les admissions sont fermées."""
        with self._lock:
            if not self._accepting or self._sealed:
                self._emit("ADMISSION_REFUSED")
                return None
            admission = Admission(self, next(self._ids))
            self._admissions.add(admission.id)
            self._admitted += 1
            self._emit("ADMITTED", admission=admission.id)
            return admission

    def _release(self, admission) -> bool:
        with self._lock:
            if admission._registry is not self or not admission._live:
                return False
            admission._live = False
            self._admissions.discard(admission.id)
            self._released += 1
            self._emit("ADMISSION_RELEASED", admission=admission.id)
            self._notify_locked()
            return True

    # -- travaux ----------------------------------------------------------

    def register(self, admission) -> Job:
        """REGISTERED, sous la serrure, avant toute soumission ; refus fermé
        sans admission vivante de ce registre ou si le registre est scellé."""
        with self._lock:
            if (self._sealed or type(admission) is not Admission or admission._registry is not self
                    or not admission._live):
                self._refused += 1
                self._emit("SUBMISSION_REFUSED")
                raise SubmissionRefused("soumission F2 sans admission vivante")
            job = Job(self, next(self._ids), admission.id)
            self._jobs[job.id] = job
            self._registered += 1
            self._emit("JOB_REGISTERED", job=job.id, admission=admission.id)
            return job

    def _start(self, job) -> bool:
        with self._lock:
            if job._state == REGISTERED:
                job._state = STARTED
                self._started += 1
                self._emit("JOB_STARTED", job=job.id)
                return True
            if job._state != ABANDONED:
                self._violation("start", job=job.id, state=job._state)
            else:
                self._emit("JOB_START_REFUSED", job=job.id)
            return False

    def _finish(self, job) -> None:
        with self._lock:
            if job._state != STARTED:
                self._violation("finish", job=job.id, state=job._state)
                return
            job._state = FINISHED
            del self._jobs[job.id]
            self._finished += 1
            self._emit("JOB_FINISHED", job=job.id)
            self._notify_locked()

    def _abandon(self, job) -> bool:
        with self._lock:
            if job._state != REGISTERED:
                return False  # déjà démarré (le fil terminera) ou déjà terminal
            job._state = ABANDONED
            del self._jobs[job.id]
            self._abandoned += 1
            self._emit("JOB_ABANDONED", job=job.id)
            self._notify_locked()
            return True

    # -- drainage ---------------------------------------------------------

    def snapshot(self) -> RegistrySnapshot:
        with self._lock:
            registered = sum(1 for j in self._jobs.values() if j._state == REGISTERED)
            return RegistrySnapshot(
                accepting=self._accepting, sealed=self._sealed, admissions_active=len(self._admissions),
                jobs_registered=registered, jobs_started=len(self._jobs) - registered,
                admitted_total=self._admitted, released_total=self._released, registered_total=self._registered,
                started_total=self._started, finished_total=self._finished, abandoned_total=self._abandoned,
                refused_submissions=self._refused, violations=self._violations)

    def is_idle(self) -> bool:
        with self._lock:
            return self._idle_locked()

    def seal_if_idle(self) -> bool:
        """Évalue atomiquement admissions == 0 ET travaux non terminaux == 0
        alors que les admissions sont fermées ; si vrai, scelle le registre
        (aucune admission ni inscription possible ensuite) et rend True."""
        with self._lock:
            if self._accepting or not self._idle_locked():
                return False
            self._sealed = True
            self._emit("SEALED")
            return True

    async def wait_idle(self, timeout=None) -> bool:
        """Attend (sans bloquer la boucle) que le registre soit vide ; timeout
        None : sans échéance. Rend l'état idle constaté à la sortie."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._idle_locked():
                return True
            future = loop.create_future()
            self._waiters.append((loop, future))
        try:
            if timeout is None:
                await future
            else:
                await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                self._waiters = [(l, f) for l, f in self._waiters if f is not future]
        return self.is_idle()


def _resolve(future):
    if not future.done():
        future.set_result(True)


class RegisteredSubmitter:
    """Soumetteur explicite F04-C, injecté aux sept sites run_in_threadpool.
    Même interface que starlette.concurrency.run_in_threadpool (callable,
    *args, **kwargs) et même exécuteur (anyio.to_thread.run_sync)."""

    __slots__ = ("registry",)

    def __init__(self, registry):
        if type(registry) is not WorkRegistry:
            raise TypeError("WorkRegistry attendu")
        self.registry = registry

    def __repr__(self) -> str:
        return "RegisteredSubmitter(<private>)"

    async def __call__(self, func, *args, **kwargs):
        if kwargs:
            func = functools.partial(func, **kwargs)
        job = self.registry.register(CURRENT_ADMISSION.get())

        def run_job():
            if not job.start():  # première instruction du fil : abandonné => rien
                raise JobAbandoned("travail abandonné avant démarrage")
            try:
                return func(*args)
            finally:
                job.finish()

        try:
            return await anyio.to_thread.run_sync(run_job)
        finally:
            job.abandon()  # sans effet si le fil a pris STARTED : il terminera lui-même
