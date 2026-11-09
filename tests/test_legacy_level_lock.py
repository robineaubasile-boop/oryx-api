"""Correctif de stabilité du run CI #50 : la route historique
PUT /api/user/{user_id}/level verrouille la ligne users (SELECT ... FOR
UPDATE) avant d'écrire le niveau, dans la même transaction.

Diagnostic (PostgreSQL 16 et 18) : un UPDATE nu de users (sans verrou
préalable) est une mise à jour « sans changement de clé ». Un FOR KEY SHARE
qui la rencontre ne l'attend pas : il verrouille d'abord les versions
suivantes de la ligne, puis la sienne ; un FOR UPDATE réveillé fait de même
en sens inverse. Les deux peuvent s'attendre mutuellement (40P01). Un UPDATE
précédé de FOR UPDATE dans sa transaction est marqué « clé modifiée » : le
FOR KEY SHARE l'attend, sans parcourir la chaîne, et le cycle disparaît.

Contre-épreuves (vraies connexions PostgreSQL, vraie route) :
- la route émet SELECT ... FOR UPDATE puis l'UPDATE du seul niveau, dans UNE
  transaction (SQL capturé par la vraie application ASGI) ;
- création du profil absent conservée (SELECT ... FOR UPDATE sans ligne,
  puis INSERT), réponses inchangées ;
- contrat level_revision conservé : la route ne l'avance pas, le CAS F2
  ne voit toujours pas l'écriture historique (test négatif historique
  inchangé dans tests/test_authz_f2_http.py) ;
- sérialisation : la route attend chacun des trois verrous de plan users
  (FOR KEY SHARE, FOR NO KEY UPDATE, FOR UPDATE) ;
- mécanisme : un lecteur F2 (FOR KEY SHARE) attend désormais la transaction
  historique non validée, alors qu'il passait sans attendre un UPDATE nu ;
- charge mixte lectures + portefeuille + route historique : compteur
  pg_stat_database.deadlocks inchangé (fixture env).

Aucun changement du runner, de l'ordre global des verrous, des plans, ni
d'aucune autre route.
"""
import asyncio
import re
import threading

import httpx
import pytest
import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED
from tests.authz_f2_support import USER_A, legacy_set_level, replace_command
from tests.test_authz_f2_concurrency import (CONCURRENT_APP, Background, Blocker, alice,  # noqa: F401 — fixtures
                                             concurrent_f2, env, profile_of, schema, wait_until, waiting)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

LEVEL_ROUTE = "/api/user/{user_id}/level"


class Captured:
    """Moteur dédié dont on enregistre les instructions, les BEGIN et les
    COMMIT (événements SQLAlchemy du moteur, aucun mock de la base)."""

    def __init__(self, pg_url):  # noqa: F811
        self.engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool,
                                       connect_args={"application_name": CONCURRENT_APP})
        self.statements, self.begins, self.commits = [], 0, 0
        sa.event.listen(self.engine, "before_cursor_execute", self._execute)
        sa.event.listen(self.engine, "begin", self._begin)
        sa.event.listen(self.engine, "commit", self._commit)

    def _execute(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append((id(conn.connection.dbapi_connection), " ".join(statement.split())))

    def _begin(self, conn):
        self.begins += 1

    def _commit(self, conn):
        self.commits += 1

    def users_statements(self):
        return [(conn, sql) for conn, sql in self.statements if re.search(r"\busers\b", sql)]


def http_put_level(engine, user_id, level):
    """Vraie application api:app (ASGI), get_db remplacé par une Session
    configurée comme core.db.SessionLocal sur `engine`."""
    import api
    import core.db
    from sqlalchemy.orm import sessionmaker

    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    async def put():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.put(LEVEL_ROUTE.format(user_id=user_id), json={"level": level})

    api.app.dependency_overrides[core.db.get_db] = override_get_db
    try:
        return asyncio.run(put())
    finally:
        api.app.dependency_overrides.clear()


def test_route_locks_the_users_row_then_writes_only_the_level_in_one_transaction(env, alice, pg_url):  # noqa: F811
    env.set_profile("debutant", 7)
    captured = Captured(pg_url)
    try:
        response = http_put_level(captured.engine, USER_A, "avance")
    finally:
        captured.engine.dispose()
    assert response.status_code == 200 and response.json() == {"success": True}
    users = captured.users_statements()
    assert len(users) == 2, users
    (lock_conn, lock_sql), (write_conn, write_sql) = users
    assert re.fullmatch(r"SELECT users\.id .* FROM users WHERE users\.id = %\(\w+\)s LIMIT %\(\w+\)s FOR UPDATE",
                        lock_sql), lock_sql
    assert re.fullmatch(r"UPDATE users SET level=%\(\w+\)s WHERE users\.id = %\(\w+\)s", write_sql), write_sql
    assert lock_conn == write_conn and captured.begins == 1 and captured.commits == 1  # une seule transaction
    assert profile_of(env) == ("avance", 7)  # niveau écrit, révision intacte


def test_route_still_creates_a_missing_profile_with_its_historical_response(env, pg_url):  # noqa: F811
    captured = Captured(pg_url)
    try:
        response = http_put_level(captured.engine, "legacy-new", "intermediaire")
    finally:
        captured.engine.dispose()
    assert response.status_code == 200 and response.json() == {"success": True}
    users = [sql for _, sql in captured.users_statements()]
    assert len(users) == 2, users
    assert users[0].startswith("SELECT users.id") and users[0].endswith("FOR UPDATE")
    assert users[1].startswith("INSERT INTO users (id, level, created_at)"), users[1]
    assert captured.begins == 1 and captured.commits == 1
    assert env.profile("legacy-new")[:2] == ("intermediaire", 0)  # révision par défaut du serveur


def test_route_keeps_the_level_revision_contract_and_stays_invisible_to_the_cas(env, alice):
    issued, _ = alice
    env.set_profile("debutant", 7)
    assert legacy_set_level(env.observer, USER_A, "avance") == {"success": True}
    assert profile_of(env) == ("avance", 7)  # jamais de révision avancée par la route historique
    # Le CAS F2 fondé sur la révision 7 réussit toujours : le contournement
    # connu (bloquant pour l'activation) est inchangé, ni masqué ni corrigé.
    result = env.runner().run(issued.credential(), c.UpdateProfileLevel("debutant", 7))
    assert result.status == AUTHORIZED and result.value.level_revision == 8
    assert profile_of(env) == ("debutant", 8)


@pytest.mark.parametrize("plan_lock", ["FOR KEY SHARE", "FOR NO KEY UPDATE", "FOR UPDATE"])
def test_route_waits_behind_every_users_plan_lock(env, alice, pg_url, plan_lock):  # noqa: F811
    env.set_profile("debutant", 3)
    before = env.profile()
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    blocker = Blocker(env, f"SELECT id FROM users WHERE id = :u {plan_lock}", u=USER_A)
    writer = Background(lambda: legacy_set_level(engine, USER_A, "avance"))
    released = False
    try:
        writer.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la route n'attend pas le verrou de plan")
        assert env.profile() == before  # rien d'écrit pendant l'attente
        blocker.release()
        released = True
        assert writer.outcome() == {"success": True}
    finally:
        if not released:
            blocker.release()
        engine.dispose()
    assert profile_of(env) == ("avance", 3)


def test_a_key_share_reader_now_waits_for_the_legacy_transaction_instead_of_passing_it(env, alice, pg_url):  # noqa: F811
    """Précondition du cycle supprimée, de façon déterministe.

    1. UPDATE nu non validé (forme d'avant correctif) : ReadProfile (users
       FOR KEY SHARE) ne l'attend pas et lit l'ancienne valeur. C'est ce
       passage sans attente qui laissait le lecteur parcourir les versions
       suivantes de la ligne.
    2. Route historique suspendue entre son UPDATE et son COMMIT : le même
       ReadProfile attend la transaction, puis lit la valeur validée."""
    issued, _ = alice
    runner = env.runner(hook=lambda point: None)
    with env.observer.connect() as bare:
        tx = bare.begin()
        bare.execute(sa.text("UPDATE users SET level = 'avance' WHERE id = :u"), {"u": USER_A})
        passed = runner.run(issued.credential(), c.ReadProfile())
        tx.rollback()
    assert passed.status == AUTHORIZED and passed.value.level == "debutant"

    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    paused, resume = threading.Event(), threading.Event()

    def hold_commit(conn):
        paused.set()
        assert resume.wait(30), "COMMIT historique jamais relâché"

    sa.event.listen(engine, "commit", hold_commit)
    writer = Background(lambda: legacy_set_level(engine, USER_A, "avance"))
    reader = Background(lambda: runner.run(issued.credential(), c.ReadProfile()))
    try:
        writer.start()
        assert paused.wait(10), "la route n'atteint pas son COMMIT"
        reader.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="le lecteur F2 n'attend pas la route historique")
        resume.set()
        assert writer.outcome() == {"success": True}
        read = reader.outcome()
    finally:
        resume.set()
        engine.dispose()
    assert read.status == AUTHORIZED and read.value.level == "avance"


def test_reads_portfolio_and_legacy_route_under_load_produce_no_deadlock(env, alice, pg_url):  # noqa: F811
    """Mélange le plus exposé du diagnostic (FOR KEY SHARE + FOR UPDATE +
    écriture historique) : la fixture env exige pg_stat_database.deadlocks
    inchangé."""
    issued, target = alice
    second = env.confirmed("alice2@example.com", USER_A)
    engine = concurrent_f2(env, pg_url)[0]
    runner = env.runner(hook=lambda point: None)
    rounds = 40
    barrier = threading.Barrier(6)

    def reads(session, command):
        barrier.wait(10)
        return [runner.run(session.credential(), command).status for _ in range(rounds)]

    def positions():
        barrier.wait(10)
        return [runner.run(issued.credential(), replace_command(target, i, quantity=str(i + 1))).status
                for i in range(rounds)]

    def legacy(levels):
        barrier.wait(10)
        return [legacy_set_level(engine, USER_A, levels[i % 2]) for i in range(rounds)]

    actors = [Background(fn) for fn in (lambda: reads(issued, c.ReadProfile()), lambda: reads(second, c.ReadProfile()),
                                        lambda: reads(issued, c.ListPositions()), positions,
                                        lambda: legacy(("avance", "debutant")),
                                        lambda: legacy(("debutant", "avance")))]
    try:
        for actor in actors:
            actor.start()
        results = [actor.outcome(timeout=120) for actor in actors]
    finally:
        engine.dispose()
    for statuses in results[:4]:
        assert statuses == [AUTHORIZED] * rounds
    assert results[4] == results[5] == [{"success": True}] * rounds
    assert profile_of(env)[1] == 0  # aucune écriture historique n'a avancé la révision
