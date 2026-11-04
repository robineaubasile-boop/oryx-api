"""R1 PR 9-C1-B — capacités privées TransactionProbe et FinalizationReadPort,
contre un VRAI PostgreSQL (16 et 18 en CI).

Chaque test ouvre une vraie transaction sur une connexion du moteur F2,
construit les capacités comme le font les adaptateurs SQL (réservation
réelle verrouillée, candidat réel) et vérifie qu'elles refusent tout usage
hors contrat AVANT toute requête métier : autre connexion, autre
transaction, autre principal, autre réservation, opération close,
transaction avortée, lecture hors du SAVEPOINT C1 lié, après RELEASE,
après annulation locale, seconde lecture ; scellement des implémentations ;
aucune capacité SQL arbitraire.
"""
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import decryptage_context as dc
from core.authz_f2.principal import AuthorizedContext, AuthorizedPrincipal
from tests.authz_f2_pr9a_support import USER_A, USER_B
from tests.authz_f2_pr9c1b_support import FinalizationEnv, next_turn, schema  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = FinalizationEnv(pg_url)
    yield environment
    environment.dispose()


@pytest.fixture
def generated(env):
    alice = env.confirmed()
    _, lease, candidate = env.generate(alice, next_turn())
    return lease, candidate


def ctx_of(user_id=USER_A):
    return AuthorizedContext(AuthorizedPrincipal(user_id, uuid.uuid4(), uuid.uuid4(), "test"),
                             datetime.now(timezone.utc))


def port_for(env, conn, candidate, user_id=USER_A, intent=None):
    row = _adapters._first(conn, _adapters._LOCK_RESERVATION, id=candidate.lease.reservation_id, u=USER_A)
    res = _adapters._reservation(row, USER_A)
    return _adapters._FinalizationReadPort(_adapters._CAPABILITY, conn, user_id, env.decryptage_keys, res,
                                           intent or _adapters._TurnIntent.of(candidate))


def statements(conn):
    log = []
    sa.event.listen(conn, "before_cursor_execute", lambda c, cur, st, *a: log.append(" ".join(st.split())))
    return log


def business_reads(log):
    return [s for s in log if not s.startswith(("SELECT current_setting", "SAVEPOINT", "RELEASE", "ROLLBACK"))]


# --------------------------------------------------------------------------
# Scellement et forme des capacités
# --------------------------------------------------------------------------

def test_implementations_are_sealed_to_the_sql_adapters():
    assert contracts.implementation(contracts.TransactionProbe) is _adapters._TransactionProbe
    assert contracts.implementation(contracts.FinalizationReadPort) is _adapters._FinalizationReadPort
    with pytest.raises(TypeError):
        class Rogue(contracts.TransactionProbe):  # noqa: F841 — module de test : refusé
            pass
    with pytest.raises(TypeError):
        class RoguePort(_adapters._FinalizationReadPort):  # noqa: F841
            pass
    with pytest.raises(contracts.CapabilityRefused):
        _adapters._TransactionProbe(object(), None, USER_A)


def test_capabilities_expose_no_arbitrary_sql_nor_callback():
    public = {name for cls in (_adapters._FinalizationReadPort,) for name in dir(cls) if not name.startswith("_")}
    assert public == {"user_id", "bound_to", "require_live", "read_preparation", "bind_scope"}
    probe_public = {name for name in dir(_adapters._TransactionProbe) if not name.startswith("_")}
    assert probe_public == {"user_id", "bound_to", "require_live"}
    import inspect
    assert list(inspect.signature(_adapters._FinalizationReadPort.read_preparation).parameters) == [
        "self", "db", "scope"]


# --------------------------------------------------------------------------
# TransactionProbe
# --------------------------------------------------------------------------

def test_probe_is_bound_to_its_connection_transaction_and_principal(env, generated):
    with env.engine.connect() as conn, env.engine.connect() as other:
        tx = conn.begin()
        probe = _adapters._TransactionProbe(_adapters._CAPABILITY, conn, USER_A)
        probe.require_live()
        assert probe.bound_to(conn, USER_A) and not probe.bound_to(other, USER_A)
        assert not probe.bound_to(conn, USER_B)
        tx.rollback()
        conn.begin()
        with pytest.raises(contracts.TransactionLost):  # autre transaction sur la même connexion
            probe.require_live()
        probe._close()
        with pytest.raises(contracts.CapabilityRefused):
            probe.require_live()
        for attempt in (lambda: setattr(probe, "_user_id", USER_B), lambda: __import__("pickle").dumps(probe),
                        lambda: __import__("copy").copy(probe)):
            with pytest.raises((AttributeError, TypeError)):
                attempt()


def test_probe_detects_an_aborted_postgresql_transaction(env):
    with env.engine.begin() as conn:
        probe = _adapters._TransactionProbe(_adapters._CAPABILITY, conn, USER_A)
        try:
            conn.execute(sa.text("SELECT 1 / 0"))
        except sa.exc.DataError:
            pass  # erreur avalée : PostgreSQL a avorté la transaction, SQLAlchemy la croit active
        assert conn.get_transaction().is_active
        with pytest.raises(sa.exc.DBAPIError) as aborted:
            probe.require_live()
        assert aborted.value.orig.pgcode == "25P02"
        conn.get_transaction().rollback()


def test_execute_refuses_any_capability_outside_its_contract(env, generated):
    lease, candidate = generated
    ack = bridge.DeliveryAcknowledgement(uuid.uuid4(), candidate.turn.conversation_key)
    finalization = bridge.CandidateFinalization(candidate)
    with env.engine.connect() as conn, env.engine.connect() as other:
        with conn.begin():
            probe = _adapters._TransactionProbe(_adapters._CAPABILITY, conn, USER_A)
            port = port_for(env, conn, candidate)
            log = statements(conn)
            refusals = [
                (ack, port, conn, ctx_of()),                 # relecture B2 refusée à une opération historique
                (finalization, probe, conn, ctx_of()),       # finalisation sans le port
                (ack, probe, other, ctx_of()),               # autre connexion
                (ack, probe, conn, ctx_of(USER_B)),          # autre principal
                (finalization, port, conn, ctx_of(USER_B)),
                (ack, object(), conn, ctx_of()),             # objet imitant l'interface
            ]
            for operation, capability, connection, ctx in refusals:
                with pytest.raises(contracts.CapabilityRefused):
                    bridge._execute(connection, ctx, operation, capability)
            assert log == []  # aucune requête, aucune Session
            probe._close()
            with pytest.raises(contracts.CapabilityRefused):
                bridge._execute(conn, ctx_of(), ack, probe)  # opération close


def test_a_port_never_mixes_two_reservations(env):
    alice = env.confirmed()
    _, _, first = env.generate(alice, next_turn())
    _, _, second = env.generate(alice, next_turn())
    with env.engine.begin() as conn:
        with pytest.raises(contracts.CapabilityRefused):
            port_for(env, conn, first, intent=_adapters._TurnIntent.of(second))


# --------------------------------------------------------------------------
# FinalizationReadPort : relecture B2 liée au SAVEPOINT C1
# --------------------------------------------------------------------------

def test_the_preparation_read_runs_once_inside_the_bound_savepoint(env, generated):
    lease, candidate = generated
    with env.engine.begin() as conn:
        port = port_for(env, conn, candidate)
        db = bridge._BridgeSession(conn)
        log = statements(conn)
        with pytest.raises(contracts.CapabilityRefused):
            port.read_preparation(db, None)  # avant le SAVEPOINT : refus SANS requête
        assert business_reads(log) == []
    log = statements(env.engine)  # SAVEPOINT de la Session : visibles au niveau du moteur
    with env.engine.begin() as conn:
        port = port_for(env, conn, candidate)
        db = bridge._BridgeSession(conn)
        with bridge._business_savepoint(db, port) as scope:
            view = port.read_preparation(db, scope)
            assert type(view) is dc.DecryptagePreparationView and view.reservation_id == lease.reservation_id
            with pytest.raises(contracts.CapabilityRefused):
                port.read_preparation(db, scope)  # seconde lecture
            with pytest.raises(contracts.CapabilityRefused):
                port.bind_scope(scope)  # association unique
        kinds = [s.split()[0] for s in log if s.split()[0] in ("SAVEPOINT", "WITH", "RELEASE")]
        assert kinds == ["SAVEPOINT", "WITH", "RELEASE"]  # projection DANS le SAVEPOINT
        assert sum(1 for s in log if s.startswith("WITH o AS")) == 1
        db._release()


@pytest.mark.parametrize("moment", ["after_release", "after_local_rollback", "after_close", "other_scope",
                                    "other_session", "nested_scope"])
def test_the_preparation_read_is_refused_outside_its_exact_scope(env, generated, moment):
    lease, candidate = generated
    with env.engine.begin() as conn:
        port = port_for(env, conn, candidate)
        db = bridge._BridgeSession(conn)
        log = statements(conn)
        refused = []

        def attempt(session, scope):
            before = len(business_reads(log))
            with pytest.raises((contracts.CapabilityRefused, bridge.PreparedTurnUnusable)):
                port.read_preparation(session, scope)
            assert len(business_reads(log)) == before  # aucune requête métier
            refused.append(moment)

        if moment == "after_release":
            with bridge._business_savepoint(db, port) as scope:
                pass
            attempt(db, scope)
        elif moment == "after_local_rollback":
            with bridge._business_savepoint(db, port) as scope:
                raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)
            attempt(db, scope)
        elif moment == "after_close":
            with pytest.raises(contracts.CapabilityRefused):  # opération close : sortie du SAVEPOINT refusée aussi
                with bridge._business_savepoint(db, port) as scope:
                    port._close()
                    attempt(db, scope)
        elif moment == "other_scope":
            with bridge._business_savepoint(db, port):
                probe = _adapters._TransactionProbe(_adapters._CAPABILITY, conn, USER_A)
                foreign = bridge._SavepointScope(db)
                attempt(db, foreign)
                del probe
        elif moment == "other_session":
            with bridge._business_savepoint(db, port) as scope:
                other = bridge._BridgeSession(conn)
                attempt(other, scope)
                other._release()
        else:  # SAVEPOINT de service ouvert au-dessus : plus le SAVEPOINT courant
            with bridge._business_savepoint(db, port) as scope:
                with db.begin_nested():
                    attempt(db, scope)
        assert refused == [moment]
        db._release()
        conn.get_transaction().rollback()


def test_a_lease_expired_inside_the_savepoint_is_read_as_stale(env, generated):
    lease, candidate = generated
    env.expire(lease.reservation_id)
    with env.engine.begin() as conn:
        port = port_for(env, conn, candidate)
        db = bridge._BridgeSession(conn)
        with bridge._business_savepoint(db, port) as scope:
            assert port.read_preparation(db, scope) is contracts.CONFLICT  # lease_stale pour le pont
        db._release()
