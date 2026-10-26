"""AuthorizedTransactionRunner (R1 PR 5) contre un vrai PostgreSQL jetable.

Sessions émises par le vrai moteur F2 ; deux utilisateurs A / B avec
portefeuilles, thèses, conversations et livraisons. Couvre :

- C.5.2-A : secret erroné, génération absente, session ou génération
  révoquée, expiration, version historique de K_active inconnue (sans
  substitution, même quand la clé courante reproduirait l'empreinte),
  trousseau invalide, classification 401 / 503 ;
- livraison pending refusée, confirmation par authenticate_session (même
  effet que /session/resolve), aucun effet de bord des lectures, plusieurs
  sessions et générations, justificatifs croisés ;
- appartenance (C.5.2 §6) : filtres id ET user_id, ressource étrangère et
  inexistante indistinguables, mutation étrangère sans écriture, aucun
  profil créé ;
- BF1 : registre fermé, aucune connexion ni méthode transactionnelle dans
  le contexte, rollback intégral après écriture ;
- BF2 : T_authorization lu après les verrous, égalité avec
  absolute_expires_at refusée, expiration postérieure sans rollback,
  délai de verrou PostgreSQL -> UNAVAILABLE.
"""
import dataclasses
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from core.auth_f2.crypto import Redacted, b64url_encode
from core.auth_f2.keys import PURPOSES, Keyring, KeyringError
from core.auth_f2.service import SessionCredential
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import runner as runner_module
from core.authz_f2.principal import AuthorizedContext, AuthorizedPrincipal
from core.authz_f2.runner import (
    AUTHORIZED,
    NOT_FOUND,
    SESSION_INVALID,
    UNAVAILABLE,
    AuthorizationConfigError,
    AuthorizationOutcome,
    AuthorizedTransactionRunner,
    UnknownCommand,
)
from tests.authz_f2_support import BOB, USER_A, USER_B, AuthzEnv
from tests.f2_support import SimulatedCrash, fake_key, prepare_schema, rows, scalar
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

US = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = AuthzEnv(pg_url)
    yield environment
    environment.dispose()


def other_secret(credential):
    raw = bytes(32) if credential.secret.reveal() != b64url_encode(bytes(32)) else bytes([1]) * 32
    return SessionCredential(credential.generation_id, Redacted(b64url_encode(raw)))


def position(env, position_id):
    return rows(env.observer, "SELECT * FROM portfolio_positions WHERE id = :i", i=position_id)[0]


def session_snapshot(env, issued):
    return (rows(env.observer, "SELECT * FROM oryx_sessions WHERE id = :s", s=issued.session_id),
            rows(env.observer, "SELECT * FROM oryx_session_generations WHERE id = :g", g=issued.generation_id))


# --------------------------------------------------------------------------
# Identité et classification (C.5.2 §4, C.5.2-A)
# --------------------------------------------------------------------------

def test_confirmed_session_yields_an_immutable_server_principal(env):
    issued = env.confirmed()
    outcome = env.runner().run(issued.credential(), c.ReadProfile())
    assert outcome.status == AUTHORIZED
    assert outcome.value == c.ProfileView(USER_A, "debutant", 0)
    assert outcome.principal == AuthorizedPrincipal(USER_A, issued.session_id, issued.generation_id,
                                                    "supabase_email_sign_in")
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.principal.user_id = USER_B
    assert "secret" not in repr(outcome) and issued.active_secret.reveal() not in repr(outcome)


@pytest.mark.parametrize("variant", ["wrong_secret", "malformed_secret", "plain_string_secret", "unknown_generation",
                                     "not_a_credential", "none"])
def test_invalid_credentials_are_session_invalid(env, variant):
    issued = env.confirmed()
    good = issued.credential()
    credential = {
        "wrong_secret": other_secret(good),
        "malformed_secret": SessionCredential(good.generation_id, Redacted("x" * 43)),
        "plain_string_secret": SessionCredential(good.generation_id, good.secret.reveal()),
        "unknown_generation": SessionCredential(uuid.uuid4(), good.secret),
        "not_a_credential": (good.generation_id, good.secret),
        "none": None,
    }[variant]
    assert env.runner().run(credential, c.ReadProfile()) == AuthorizationOutcome(SESSION_INVALID)
    assert env.hook_calls == []  # refus dès la phase A : aucune phase B


def test_revoked_session_and_revoked_generation_are_session_invalid(env):
    first = env.confirmed()
    second = env.confirmed("alice2@example.com", USER_A)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE oryx_session_generations SET status = 'revoked', revoked_at = clock_timestamp(), "
                             "revocation_reason = 'security' WHERE id = :g"), {"g": second.generation_id})
    assert env.service.revoke_session(first.session_id, "logout") is True
    runner = env.runner()
    assert runner.run(first.credential(), c.ReadProfile()).status == SESSION_INVALID
    assert runner.run(second.credential(), c.ReadProfile()).status == SESSION_INVALID


def test_expired_session_is_session_invalid(env):
    issued = env.confirmed()
    env.expire_at(issued.session_id, "clock_timestamp() - interval '1 second'")
    assert env.runner().run(issued.credential(), c.ReadProfile()).status == SESSION_INVALID


def _keyring_with_active(active: dict, current: str) -> Keyring:
    keys = {p: {"t1": fake_key(p, "t1")} for p in PURPOSES}
    keys["active"] = active
    return Keyring(keys, {**{p: "t1" for p in PURPOSES}, "active": current})


def test_unknown_historical_active_key_version_is_refused_without_substitution(env):
    issued = env.confirmed()
    # La version courante « t9 » porte la valeur de clé qui a produit
    # l'empreinte (t1) : une substitution vers la clé courante
    # reproduirait l'empreinte et accepterait le secret. Le runner résout
    # la version EXACTE enregistrée (t1), absente : refus 401.
    substituting = _keyring_with_active({"t9": fake_key("active", "t1")}, "t9")
    assert env.runner(keyring=substituting).run(issued.credential(), c.ReadProfile()).status == SESSION_INVALID
    # Contrôle : la version historique connue est résolue exactement.
    historical = _keyring_with_active({"t1": fake_key("active", "t1"), "t9": fake_key("active", "t9")}, "t9")
    assert env.runner(keyring=historical).run(issued.credential(), c.ReadProfile()).status == AUTHORIZED


def test_invalid_keyring_or_engine_cannot_build_an_authorizer(env):
    with pytest.raises(KeyringError):
        Keyring({p: {"t1": b"short"} for p in PURPOSES}, {p: "t1" for p in PURPOSES})
    for engine, keyring in ((env.engine, "keyring"), (env.engine, None), ("engine", env.keyring),
                            (sa.create_engine("sqlite://"), env.keyring)):
        with pytest.raises(AuthorizationConfigError):
            AuthorizedTransactionRunner(engine, keyring)
    with pytest.raises(AuthorizationConfigError):
        AuthorizedTransactionRunner(env.engine, env.keyring, clock="now")


def test_postgresql_outage_is_unavailable_not_session_invalid(env):
    issued = env.confirmed()
    down = sa.create_engine("postgresql+psycopg2://postgres@127.0.0.1:1/oryx_down_test", poolclass=sa.pool.NullPool)
    assert env.runner(engine=down).run(issued.credential(), c.ReadProfile()) == AuthorizationOutcome(UNAVAILABLE)


def test_unexpected_failure_is_unavailable(env):
    issued = env.confirmed()

    def broken_clock(conn):
        raise RuntimeError("panne imprévue")

    assert env.runner(clock=broken_clock).run(issued.credential(), c.ReadProfile()).status == UNAVAILABLE


# --------------------------------------------------------------------------
# Sessions : livraison confirmée obligatoire, aucun effet de bord
# --------------------------------------------------------------------------

def test_pending_delivery_is_refused_and_never_confirmed_by_the_runner(env):
    issued = env.sign_in()
    runner = env.runner()
    before = session_snapshot(env, issued)
    for command in (c.ReadProfile(), c.ListPositions()):
        assert runner.run(issued.credential(), command).status == SESSION_INVALID
    assert session_snapshot(env, issued) == before
    generation = env.generation(issued.generation_id)
    assert (generation["delivery_status"], generation["delivery_confirmed_at"], generation["delivery_proof_version"]) \
        == ("pending", None, None)
    # Seule la résolution F2 (PR 3 : /session/resolve -> authenticate_session) confirme.
    assert env.service.authenticate_session(issued.credential()) is not None
    assert runner.run(issued.credential(), c.ReadProfile()).status == AUTHORIZED


def test_reads_have_no_side_effect_on_sessions(env):
    issued = env.confirmed()
    before = session_snapshot(env, issued)
    runner = env.runner()
    for _ in range(3):
        assert runner.run(issued.credential(), c.ReadProfile()).status == AUTHORIZED
    assert session_snapshot(env, issued) == before


def test_several_sessions_and_generations_never_cross(env):
    a1 = env.confirmed()
    a2 = env.confirmed("alice2@example.com", USER_A)
    b = env.confirmed(BOB, USER_B)
    runner = env.runner()
    for issued, user in ((a1, USER_A), (a2, USER_A), (b, USER_B)):
        outcome = runner.run(issued.credential(), c.ReadProfile())
        assert (outcome.principal.user_id, outcome.principal.generation_id) == (user, issued.generation_id)
    # Sélecteur d'une génération, secret d'une autre : jamais de choix implicite.
    for selector, secret in ((a1, a2), (a2, a1), (a1, b), (b, a1)):
        crossed = SessionCredential(selector.generation_id, secret.active_secret)
        assert runner.run(crossed, c.ReadProfile()).status == SESSION_INVALID


# --------------------------------------------------------------------------
# Appartenance des ressources (C.5.2 §6)
# --------------------------------------------------------------------------

@pytest.fixture
def two_users(env):
    a, b = env.confirmed(), env.confirmed(BOB, USER_B)
    return a, b, env.seed_resources()


def test_each_user_lists_only_own_resources(env, two_users):
    a, b, ids = two_users
    runner = env.runner()
    for issued, user in ((a, USER_A), (b, USER_B)):
        positions = runner.run(issued.credential(), c.ListPositions())
        assert [p.position_id for p in positions.value] == ids[(user, "positions")]
        theses = runner.run(issued.credential(), c.ListTheses())
        assert [t.thesis_id for t in theses.value] == ids[(user, "theses")]
        # Même ticker chez A et B : seul le sien.
        by_ticker = runner.run(issued.credential(), c.ListThesesForTicker("AIR.PA"))
        assert [t.thesis_text for t in by_ticker.value] == [f"thèse de {user}"]


def test_foreign_and_missing_individual_resources_are_indistinguishable(env, two_users):
    a, _, ids = two_users
    runner = env.runner()
    own = runner.run(a.credential(), c.ReadPosition(ids[(USER_A, "positions")][0]))
    assert own.status == AUTHORIZED and own.value.ticker == "AIR.PA" and own.value.quantity == 3.0
    foreign = runner.run(a.credential(), c.ReadPosition(ids[(USER_B, "positions")][0]))
    missing = runner.run(a.credential(), c.ReadPosition(2**31 - 1))
    assert foreign == missing == AuthorizationOutcome(NOT_FOUND)

    delivery = runner.run(a.credential(), c.ReadDelivery(ids[(USER_A, "delivery")]))
    assert delivery.status == AUTHORIZED and delivery.value.assistant_turn_id == ids[(USER_A, "delivery")]
    assert {f.name for f in dataclasses.fields(delivery.value)} == {
        "assistant_turn_id", "conversation_key", "surface", "status", "generated_at"}
    assert "privé" not in repr(delivery.value)
    foreign = runner.run(a.credential(), c.ReadDelivery(ids[(USER_B, "delivery")]))
    missing = runner.run(a.credential(), c.ReadDelivery(uuid.uuid4()))
    assert foreign == missing == AuthorizationOutcome(NOT_FOUND)

    foreign_conversation = runner.run(a.credential(), c.ListConversationDeliveries(ids[(USER_B, "conversation")]))
    missing_conversation = runner.run(a.credential(), c.ListConversationDeliveries("conv-nobody"))
    assert foreign_conversation.value == missing_conversation.value == ()
    own_conversation = runner.run(a.credential(), c.ListConversationDeliveries(ids[(USER_A, "conversation")]))
    assert [d.assistant_turn_id for d in own_conversation.value] == [ids[(USER_A, "delivery")]]


def test_foreign_mutation_writes_nothing_and_locks_nothing(env, two_users):
    a, _, ids = two_users
    target = ids[(USER_B, "positions")][0]
    before = position(env, target)
    # La ligne de B est verrouillée par un tiers : la tentative de A ne
    # l'attend pas (filtre id ET user_id : la ligne d'autrui n'est jamais
    # candidate au verrou) et rend l'absence.
    with env.observer.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(sa.text("SELECT id FROM portfolio_positions WHERE id = :i FOR UPDATE"), {"i": target})
        started = time.monotonic()
        outcome = env.runner().run(a.credential(), c.UpdatePositionQuantity(target, 99.0))
        assert time.monotonic() - started < 3
        tx.rollback()
    assert outcome == AuthorizationOutcome(NOT_FOUND)
    assert position(env, target) == before
    assert env.hook_calls == ["prevalidated", "locked"]  # aucune exécution


def test_own_mutation_is_authorized_at_t_authorization(env, two_users):
    a, _, ids = two_users
    target = ids[(USER_A, "positions")][0]
    instants = []

    def clock(conn):
        instants.append(conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one())
        return instants[-1]

    outcome = env.runner(clock=clock).run(a.credential(), c.UpdatePositionQuantity(target, 4.5))
    assert outcome.status == AUTHORIZED and outcome.value.quantity == 4.5
    row = position(env, target)
    assert row["quantity"] == 4.5
    # updated_at (colonne historique sans fuseau, UTC) = T_authorization.
    assert row["updated_at"] == instants[-1].astimezone(timezone.utc).replace(tzinfo=None)


def test_missing_profile_is_unavailable_and_never_created(env):
    issued = env.confirmed()
    # État incohérent forcé (FK désactivées pour cette seule transaction
    # d'observateur superutilisateur) : session valide, profil absent.
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": USER_A})
    for command in (c.ReadProfile(), c.ListPositions(), c.UpdatePositionQuantity(1, 1.0)):
        assert env.runner().run(issued.credential(), command) == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("users", "id = :u", u=USER_A) == 0


def test_commands_carry_no_owner_and_validate_their_fields():
    for command_type in _adapters.REGISTRY:
        assert "user_id" not in {f.name for f in dataclasses.fields(command_type)}
        with pytest.raises(TypeError):
            command_type(user_id=USER_B)
    for bad in (True, 0, -1, 2**31, "1", 1.0, None):
        with pytest.raises(ValueError):
            c.ReadPosition(bad)
    for bad in (0, -1.0, float("nan"), float("inf"), True, "1"):
        with pytest.raises(ValueError):
            c.UpdatePositionQuantity(1, bad)
    for bad in ("", " AIR.PA", "x" * 33, None):
        with pytest.raises(ValueError):
            c.ListThesesForTicker(bad)
    with pytest.raises(ValueError):
        c.ReadDelivery(str(uuid.uuid4()))


# --------------------------------------------------------------------------
# BF1 : registre fermé, contexte restreint, rollback intégral
# --------------------------------------------------------------------------

def test_registry_is_closed_and_unknown_commands_are_refused_before_any_transaction(env):
    issued = env.confirmed()
    assert set(_adapters.REGISTRY) == {c.ReadProfile, c.ListPositions, c.ReadPosition, c.ListTheses,
                                       c.ListThesesForTicker, c.ReadDelivery, c.ListConversationDeliveries,
                                       c.UpdatePositionQuantity, c.UpdateProfileLevel}
    assert len(_adapters.REGISTRY) == 9
    with pytest.raises(TypeError):
        _adapters.REGISTRY[object] = None

    @dataclasses.dataclass(frozen=True, slots=True)
    class Smuggled(c.ReadProfile):
        pass

    def operation(conn, principal):
        conn.execute(sa.text("DELETE FROM portfolio_positions"))

    runner = env.runner()
    for command in (operation, lambda conn: None, Smuggled(), "ReadProfile", c.ReadProfile, None,
                    _adapters.REGISTRY[c.ReadProfile]):
        with pytest.raises(UnknownCommand):
            runner.run(issued.credential(), command)
    assert env.hook_calls == []


def test_commands_see_only_principal_and_t_authorization(env, monkeypatch):
    issued = env.confirmed()
    seen = []

    def recording(principal, at):
        context = AuthorizedContext(principal, at)
        seen.append(context)
        return context

    monkeypatch.setattr(runner_module, "AuthorizedContext", recording)
    assert env.runner().run(issued.credential(), c.ReadProfile()).status == AUTHORIZED
    (context,) = seen
    assert {f.name for f in dataclasses.fields(context)} == {"principal", "authorized_at"}
    public = {name for name in dir(context) if not name.startswith("_")}
    assert public == {"principal", "authorized_at"}
    for forbidden in ("conn", "connection", "session", "engine", "commit", "rollback", "begin", "execute"):
        assert not hasattr(context, forbidden) and not hasattr(context.principal, forbidden)
    assert {name for name in dir(env.runner()) if not name.startswith("_")} == {"run"}


@pytest.mark.parametrize("point", ["locked", "authorized", "executed"])
def test_failure_inside_phase_b_rolls_everything_back(env, two_users, point):
    a, _, ids = two_users
    target = ids[(USER_A, "positions")][0]
    before = position(env, target)

    def fail():
        raise RuntimeError(f"panne au point {point}")

    env.hooks[point] = fail
    outcome = env.runner().run(a.credential(), c.UpdatePositionQuantity(target, 42.0))
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert position(env, target) == before


def test_crash_after_write_rolls_back(env, two_users):
    a, _, ids = two_users
    target = ids[(USER_A, "positions")][0]
    before = position(env, target)

    def crash():
        # L'écriture a eu lieu dans la transaction : visible pour elle seule.
        assert env.count("portfolio_positions", "id = :i AND quantity = 42.0", i=target) == 0
        raise SimulatedCrash()

    env.hooks["executed"] = crash
    with pytest.raises(SimulatedCrash):
        env.runner().run(a.credential(), c.UpdatePositionQuantity(target, 42.0))
    assert position(env, target) == before
    assert env.engine.pool.checkedout() == 0


# --------------------------------------------------------------------------
# BF2 : T_authorization
# --------------------------------------------------------------------------

def test_t_authorization_is_read_after_every_decision_lock(env, two_users):
    a, _, ids = two_users

    def clock(conn):
        env.hook_calls.append("clock")
        return conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()

    env.runner(clock=clock).run(a.credential(), c.UpdatePositionQuantity(ids[(USER_A, "positions")][0], 2.0))
    assert env.hook_calls == ["clock", "prevalidated", "locked", "clock", "authorized", "executed"]


def _scripted_clock(*instants):
    remaining = list(instants)
    return lambda conn: remaining.pop(0)


def test_exact_equality_with_absolute_expiry_is_refused(env, two_users):
    a, _, ids = two_users
    target = ids[(USER_A, "positions")][0]
    expires = scalar(env.observer, "SELECT absolute_expires_at FROM oryx_sessions WHERE id = :s", s=a.session_id)
    before = position(env, target)
    refused = env.runner(clock=_scripted_clock(expires - timedelta(seconds=1), expires))
    assert refused.run(a.credential(), c.UpdatePositionQuantity(target, 9.0)) == AuthorizationOutcome(SESSION_INVALID)
    assert position(env, target) == before
    accepted = env.runner(clock=_scripted_clock(expires - timedelta(seconds=1), expires - US))
    assert accepted.run(a.credential(), c.UpdatePositionQuantity(target, 9.0)).status == AUTHORIZED
    assert position(env, target)["quantity"] == 9.0


def test_expiry_after_t_authorization_does_not_roll_back(env, two_users):
    a, _, ids = two_users
    target = ids[(USER_A, "positions")][0]
    env.expire_at(a.session_id, "clock_timestamp() + interval '1500 milliseconds'")
    env.hooks["authorized"] = lambda: time.sleep(2.0)  # l'échéance passe APRÈS la décision
    outcome = env.runner().run(a.credential(), c.UpdatePositionQuantity(target, 11.0))
    assert outcome.status == AUTHORIZED
    assert position(env, target)["quantity"] == 11.0
    expired = scalar(env.observer, "SELECT absolute_expires_at < clock_timestamp() FROM oryx_sessions WHERE id = :s",
                     s=a.session_id)
    assert expired is True
    assert env.runner().run(a.credential(), c.ReadProfile()).status == SESSION_INVALID


def test_lock_timeout_is_unavailable(env, two_users):
    a, _, ids = two_users
    target = ids[(USER_A, "positions")][0]
    before = position(env, target)
    with env.observer.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": USER_A})
        started = time.monotonic()
        outcome = env.runner().run(a.credential(), c.UpdatePositionQuantity(target, 5.0))
        elapsed = time.monotonic() - started
        tx.rollback()
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert 4.5 <= elapsed < 9  # lock_timeout = 5 s (core.auth_f2.policy)
    assert position(env, target) == before


def test_no_network_call_happens_inside_runner_transactions(env, two_users, monkeypatch):
    a, _, ids = two_users
    attempts = []

    def guard(self, address):
        attempts.append(address)
        raise OSError("réseau interdit pendant une transaction verrouillée")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: guard(None, args))
    outcome = env.runner().run(a.credential(), c.UpdatePositionQuantity(ids[(USER_A, "positions")][0], 6.0))
    assert outcome.status == AUTHORIZED and attempts == []


def test_runner_is_thread_safe_for_independent_users(env, two_users):
    a, b, _ = two_users
    runner = env.runner()
    results = []

    def read(issued):
        results.append(runner.run(issued.credential(), c.ReadProfile()).value.user_id)

    threads = [threading.Thread(target=read, args=(issued,)) for issued in (a, b, a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == [USER_A, USER_A, USER_B, USER_B]


# --------------------------------------------------------------------------
# R1 PR 6 : profil versionné et CAS sur users.level_revision
# --------------------------------------------------------------------------

MAX_REVISION = 2**63 - 1


def test_profile_view_carries_level_and_revision_read_together(env):
    issued = env.confirmed()
    env.set_profile("avance", 7)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        outcome = env.runner().run(issued.credential(), c.ReadProfile())
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    assert outcome.status == AUTHORIZED and outcome.value == c.ProfileView(USER_A, "avance", 7)
    # Une seule requête lit le couple, après T_authorization ; aucune écriture.
    profile_reads = [s for s in statements if "level" in s]
    assert profile_reads == ["SELECT id, level, level_revision FROM users WHERE id = %(u)s"]
    assert statements.index(profile_reads[0]) > statements.index("SELECT clock_timestamp()")
    assert not any(s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in statements)
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.value.level_revision = 8


@pytest.mark.parametrize("current,requested,expected,status,after", [
    (("debutant", 7), "avance", 7, AUTHORIZED, ("avance", 8)),       # exacte, différent : +1
    (("debutant", 7), "debutant", 7, AUTHORIZED, ("debutant", 7)),   # exacte, identique : inchangé
    (("debutant", 7), "avance", 6, "revision_conflict", ("debutant", 7)),    # périmée, différent
    (("debutant", 7), "debutant", 6, "revision_conflict", ("debutant", 7)),  # périmée, identique
    (("debutant", 7), "debutant", 8, "revision_conflict", ("debutant", 7)),  # future : périmée aussi
    (("avance", 0), "debutant", 0, AUTHORIZED, ("debutant", 1)),
])
def test_cas_matrix(env, current, requested, expected, status, after):
    issued = env.confirmed()
    env.set_profile(*current)
    before = env.profile()
    outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel(requested, expected))
    assert outcome.status == status
    level, revision, xmin = env.profile()
    assert (level, revision) == after
    if status == AUTHORIZED:
        assert outcome.value == c.ProfileView(USER_A, *after)
        assert outcome.principal.user_id == USER_A
    else:
        # Conflit : ni profil courant ni principal transportés.
        assert outcome == AuthorizationOutcome(runner_module.REVISION_CONFLICT)
    if after == current:
        assert xmin == before[2]  # aucune écriture (ni UPDATE ni réécriture de ligne)
    else:
        assert xmin != before[2]


def test_conflict_is_decided_after_authorization_and_rolls_back(env):
    issued = env.confirmed()
    env.set_profile("debutant", 7)
    outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 3))
    assert outcome.status == runner_module.REVISION_CONFLICT
    assert env.hook_calls == ["prevalidated", "locked", "authorized"]  # jamais « executed »
    # Une session invalide prime sur le conflit : jugée avant la révision.
    env.service.revoke_session(issued.session_id, "logout")
    assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 3)).status == SESSION_INVALID
    assert env.profile()[:2] == ("debutant", 7)


def test_revision_conflict_is_a_distinct_closed_status():
    assert runner_module.REVISION_CONFLICT == "revision_conflict"
    assert runner_module.STATUSES == (AUTHORIZED, SESSION_INVALID, NOT_FOUND, UNAVAILABLE,
                                      runner_module.REVISION_CONFLICT)
    assert len(set(runner_module.STATUSES)) == 5


def test_cycle_produces_two_increments_and_stale_retry_conflicts(env):
    issued = env.confirmed()
    runner = env.runner()
    first = runner.run(issued.credential(), c.UpdateProfileLevel("avance", 0))
    second = runner.run(issued.credential(), c.UpdateProfileLevel("debutant", first.value.level_revision))
    assert (first.value.level_revision, second.value.level_revision) == (1, 2)
    assert env.profile()[:2] == ("debutant", 2)
    # Rejeu de la première demande (réponse perdue) : révision périmée,
    # aucune restitution du résultat précédent, aucune écriture.
    assert runner.run(issued.credential(), c.UpdateProfileLevel("avance", 0)).status == runner_module.REVISION_CONFLICT
    assert env.profile()[:2] == ("debutant", 2)


@pytest.mark.parametrize("start", [2**53 - 1, 2**53, 2**53 + 1, MAX_REVISION - 1])
def test_large_revisions_increment_exactly(env, start):
    issued = env.confirmed()
    env.set_profile("debutant", start)
    outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", start))
    assert outcome.status == AUTHORIZED and outcome.value.level_revision == start + 1
    assert type(outcome.value.level_revision) is int
    assert env.profile()[:2] == ("avance", start + 1)


def test_maximum_revision_is_never_overflowed(env, caplog):
    issued = env.confirmed()
    env.set_profile("debutant", MAX_REVISION)
    before = env.profile()
    runner = env.runner()
    same = runner.run(issued.credential(), c.UpdateProfileLevel("debutant", MAX_REVISION))
    assert same.status == AUTHORIZED and same.value == c.ProfileView(USER_A, "debutant", MAX_REVISION)
    with caplog.at_level("DEBUG", logger="oryx.authz_f2"):
        change = runner.run(issued.credential(), c.UpdateProfileLevel("avance", MAX_REVISION))
    assert change == AuthorizationOutcome(UNAVAILABLE)
    assert env.profile() == before  # ni débordement, ni recyclage, ni remise à zéro
    alerts = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage() for r in alerts] == ["authz_f2 UpdateProfileLevel status=unavailable "
                                                "alert=level_revision_exhausted"]
    assert str(MAX_REVISION) not in caplog.text and USER_A not in caplog.text


@pytest.mark.parametrize("level", ["intermediaire", "", "Avance", "avance ", "expert"])
def test_unknown_level_in_postgresql_is_unavailable_never_corrected(env, level):
    issued = env.confirmed()
    env.set_profile(level, 4)
    before = env.profile()
    runner = env.runner()
    assert runner.run(issued.credential(), c.ReadProfile()) == AuthorizationOutcome(UNAVAILABLE)
    for requested in ("debutant", "avance"):
        for revision in (4, 3):  # cohérence jugée AVANT la révision
            assert runner.run(issued.credential(), c.UpdateProfileLevel(requested, revision)) == \
                AuthorizationOutcome(UNAVAILABLE)
    assert env.profile() == before


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _Conn:
    """Connexion simulée (frontière seulement) : rend les lignes données."""

    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements = []

    def execute(self, statement, params):
        self.statements.append(str(statement))
        return _Rows(self.rows.pop(0))


def _context(user_id=USER_A):
    principal = AuthorizedPrincipal(user_id, uuid.uuid4(), uuid.uuid4(), "supabase_email_sign_in")
    return AuthorizedContext(principal, datetime.now(timezone.utc))


@pytest.mark.parametrize("row", [
    None,
    {"id": USER_B, "level": "debutant", "level_revision": 1},  # autre profil : jamais rendu
    {"id": USER_A, "level": None, "level_revision": 1},
    {"id": USER_A, "level": "debutant", "level_revision": -1},
    {"id": USER_A, "level": "debutant", "level_revision": None},
    {"id": USER_A, "level": "debutant", "level_revision": True},
    {"id": USER_A, "level": "debutant", "level_revision": 1.0},
    {"id": USER_A, "level": "debutant", "level_revision": "1"},
    {"id": USER_A, "level": "debutant", "level_revision": MAX_REVISION + 1},
])
def test_incoherent_profile_rows_break_the_invariant(row):
    for adapter, command in ((_adapters._read_profile, c.ReadProfile()),
                             (_adapters._update_profile_level, c.UpdateProfileLevel("avance", 1))):
        conn = _Conn(row)
        with pytest.raises(_adapters.AdapterInvariantBroken):
            adapter(conn, _context(), command, None)
        assert len(conn.statements) == 1 and not conn.statements[0].startswith("UPDATE")


def test_conditional_write_returning_no_row_is_an_invariant_violation_not_a_conflict():
    conn = _Conn({"id": USER_A, "level": "debutant", "level_revision": 7}, None)
    with pytest.raises(_adapters.AdapterInvariantBroken) as raised:
        _adapters._update_profile_level(conn, _context(), c.UpdateProfileLevel("avance", 7), None)
    assert not isinstance(raised.value, _adapters.RevisionExhausted)
    assert conn.statements[1].startswith("UPDATE users SET level = :l, level_revision = level_revision + 1 "
                                         "WHERE id = :u AND level_revision = :r RETURNING")
    for unexpected in ({"id": USER_A, "level": "debutant", "level_revision": 8},
                       {"id": USER_A, "level": "avance", "level_revision": 9},
                       {"id": USER_A, "level": "avance", "level_revision": 7}):
        conn = _Conn({"id": USER_A, "level": "debutant", "level_revision": 7}, unexpected)
        with pytest.raises(_adapters.AdapterInvariantBroken):
            _adapters._update_profile_level(conn, _context(), c.UpdateProfileLevel("avance", 7), None)


def test_update_profile_level_validates_its_fields():
    assert c.UpdateProfileLevel("avance", 0) == c.UpdateProfileLevel("avance", 0)
    assert c.UpdateProfileLevel("debutant", MAX_REVISION).expected_revision == MAX_REVISION
    for bad in ("intermediaire", "", "Avance", " avance", None, 1, b"avance"):
        with pytest.raises(ValueError):
            c.UpdateProfileLevel(bad, 0)

    class Text(str):
        pass

    class Number(int):
        pass

    with pytest.raises(ValueError):
        c.UpdateProfileLevel(Text("avance"), 0)
    for bad in (True, False, -1, MAX_REVISION + 1, 1.0, "7", None, Number(7)):
        with pytest.raises(ValueError):
            c.UpdateProfileLevel("avance", bad)
    command = c.UpdateProfileLevel("avance", 3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.expected_revision = 4
    assert {f.name for f in dataclasses.fields(command)} == {"level", "expected_revision"}
    with pytest.raises(ValueError):
        c.ProfileView(USER_A, "intermediaire", 0)
    with pytest.raises(ValueError):
        c.ProfileView(USER_A, "avance", -1)
    with pytest.raises(ValueError):
        c.next_revision(MAX_REVISION)


def test_profile_mutation_never_creates_a_missing_profile(env):
    issued = env.confirmed()
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": USER_A})
    assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)) == \
        AuthorizationOutcome(UNAVAILABLE)
    assert env.count("users", "id = :u", u=USER_A) == 0


def test_profile_mutation_of_another_user_is_impossible(env):
    a, b = env.confirmed(), env.confirmed(BOB, USER_B)
    before_b = env.profile(USER_B)
    outcome = env.runner().run(a.credential(), c.UpdateProfileLevel("avance", 0))
    assert outcome.status == AUTHORIZED and outcome.value.user_id == USER_A
    assert env.profile(USER_B) == before_b  # B intact : le profil est celui du principal
    assert env.runner().run(b.credential(), c.ReadProfile()).value == c.ProfileView(USER_B, "debutant", 0)


@pytest.mark.parametrize("point", ["locked", "authorized", "executed"])
def test_profile_failure_inside_phase_b_rolls_back(env, point):
    issued = env.confirmed()
    env.set_profile("debutant", 7)
    before = env.profile()

    def fail():
        raise RuntimeError(f"panne au point {point}")

    env.hooks[point] = fail
    assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 7)) == \
        AuthorizationOutcome(UNAVAILABLE)
    assert env.profile() == before


def test_rollback_after_execution_before_commit(env):
    issued = env.confirmed()
    env.set_profile("debutant", 7)
    before = env.profile()

    def crash():
        # Écriture faite dans la transaction, invisible au-dehors.
        assert env.profile()[:2] == ("debutant", 7)
        raise SimulatedCrash()

    env.hooks["executed"] = crash
    with pytest.raises(SimulatedCrash):
        env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 7))
    assert env.profile() == before
    assert env.engine.pool.checkedout() == 0


def test_failed_commit_is_unavailable_and_writes_nothing(env):
    issued = env.confirmed()
    env.set_profile("debutant", 7)
    before = env.profile()

    def refuse_commit(conn):
        raise OSError("COMMIT refusé")

    sa.event.listen(env.engine, "commit", refuse_commit)
    try:
        outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 7))
    finally:
        sa.event.remove(env.engine, "commit", refuse_commit)
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert env.profile() == before


def test_uncertain_commit_is_unavailable_and_retry_meets_a_conflict(env):
    """COMMIT effectué par le serveur, puis connexion perdue avant la
    confirmation : 503, jamais « annulé » ; aucune relance automatique. Le
    rejeu avec la même révision rencontre un conflit (pas de restitution)."""
    issued = env.confirmed()
    env.set_profile("debutant", 7)
    commits = []

    def commit_then_lose_connection(conn):
        if not commits and conn.in_transaction() and conn.info.get("profile_update"):
            commits.append("commit")
            conn.connection.dbapi_connection.commit()
            raise OSError("connexion perdue après COMMIT")

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE users SET level"):
            conn.info["profile_update"] = True

    sa.event.listen(env.engine, "before_cursor_execute", mark)
    sa.event.listen(env.engine, "commit", commit_then_lose_connection)
    try:
        outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 7))
    finally:
        sa.event.remove(env.engine, "commit", commit_then_lose_connection)
        sa.event.remove(env.engine, "before_cursor_execute", mark)
        env.engine.dispose()
    assert commits == ["commit"]  # une seule tentative, aucune relance
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert env.profile()[:2] == ("avance", 8)  # la transaction a bien été committée
    retry = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 7))
    assert retry == AuthorizationOutcome(runner_module.REVISION_CONFLICT)
    assert env.profile()[:2] == ("avance", 8)


def test_profile_lock_timeout_is_unavailable(env):
    issued = env.confirmed()
    env.set_profile("debutant", 7)
    before = env.profile()
    with env.observer.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": USER_A})
        started = time.monotonic()
        outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 7))
        elapsed = time.monotonic() - started
        tx.rollback()
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert 4.5 <= elapsed < 9  # lock_timeout = 5 s (core.auth_f2.policy, inchangé)
    assert env.profile() == before


def test_profile_mutation_lock_order_and_t_authorization(env):
    """PROFILE_MUTATION : users FOR UPDATE -> oryx_sessions FOR SHARE ->
    oryx_session_generations FOR SHARE -> T_authorization -> lecture du
    couple -> UPDATE conditionnel ; aucun verrou métier (ni portefeuille)."""
    issued = env.confirmed()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        outcome = env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0))
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    assert outcome.status == AUTHORIZED
    phase_b = statements[statements.index("SELECT id FROM users WHERE id = %(u)s FOR UPDATE"):]
    markers = ["SELECT id FROM users WHERE id = %(u)s FOR UPDATE",
               "SELECT * FROM oryx_sessions WHERE id = %(s)s FOR SHARE",
               "SELECT * FROM oryx_session_generations WHERE id = %(g)s FOR SHARE",
               "SELECT clock_timestamp()",
               "SELECT id, level, level_revision FROM users WHERE id = %(u)s"]
    positions = [phase_b.index(m) for m in markers]
    assert positions == sorted(positions)
    (update,) = [s for s in phase_b if s.startswith("UPDATE")]
    assert phase_b.index(update) > positions[-1]
    assert update.startswith("UPDATE users SET level = %(l)s, level_revision = level_revision + 1 "
                             "WHERE id = %(u)s AND level_revision = %(r)s RETURNING")
    assert not any("portfolio_positions" in s or "KEY SHARE" in s for s in phase_b)
    locks = [s for s in statements if " FOR " in s]
    assert len(locks) == 3  # phase A sans verrou
