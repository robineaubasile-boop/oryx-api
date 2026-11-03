"""R1 PR 9-B2 — concurrence et courses de la préparation / génération
Décrypter F2, contre un VRAI PostgreSQL (16 et 18 en CI).

Les mutations concurrentes sont COMMITTÉES par d'autres transactions
pendant un appel fournisseur simulé (aucune transaction B2 n'est alors
ouverte) : révocation, niveau, ancre, contributions, faits, reprise du bail
par un successeur, AnalysisSession modifiée pendant Claude. Chaque scénario
montre que B2 ne produit aucun effet canonique et transporte les éléments
dont PR 9-C aura besoin ; les garanties finales de PR 9-C (revalidation sous
verrous, fencing à la finalisation) ne sont PAS démontrées ici.
"""
import threading

import pytest
import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.runner import AUTHORIZED, REVISION_CONFLICT
from tests.authz_f2_pr9b1_support import legacy_route  # noqa: F401 — fixture
from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock, FakeEodhd, PreparationEnv
from tests.f2_support import prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = PreparationEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def during_market_data(env, action):
    """Orchestrateur dont l'action concurrente est committée pendant EODHD."""
    clock = FakeClock()
    return env.orchestrator(clock, eodhd=FakeEodhd(clock, actions={"fundamentals": action}))


def during_claude(env, action):
    clock = FakeClock()
    return env.orchestrator(clock, claude=FakeClaude(clock, actions={"create_message": action}))


def test_the_same_lease_is_never_run_twice_concurrently_by_one_orchestrator(env, alice):
    command, reserved = env.reserve(alice)
    nested = []
    clock = FakeClock()
    orchestrator = None

    def again():
        nested.append(orchestrator.generate(alice.credential(), command, reserved))

    orchestrator = env.orchestrator(clock, claude=FakeClaude(clock, actions={"create_message": again}))
    first = orchestrator.generate(alice.credential(), command, reserved)
    assert first.status == dg.GENERATED
    assert [o.reason for o in nested] == [dg.LEASE_ALREADY_USED]
    assert orchestrator.generate(alice.credential(), command, reserved).reason == dg.LEASE_ALREADY_USED
    assert len(env.claude.requests) == 1


def test_two_orchestrators_with_the_same_lease_produce_nothing_canonical(env, alice):
    command, reserved = env.reserve(alice)
    before = env.full_state()
    results, barrier = [], threading.Barrier(2, timeout=20)

    def worker():
        clock = FakeClock()
        orchestrator = env.orchestrator(clock, claude=FakeClaude(clock, actions={"create_message": barrier.wait}))
        results.append(orchestrator.generate(alice.credential(), command, reserved))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert [o.status for o in results] == [dg.GENERATED, dg.GENERATED]
    a, b = (o.candidate for o in results)
    # Même capacité (même epoch, même jeton) : seule la finalisation PR 9-C,
    # sous verrou et fencing, pourra en retenir au plus une.
    assert (a.lease.reservation_id, a.lease.lease_epoch) == (b.lease.reservation_id, b.lease.lease_epoch)
    assert a.preparation.fingerprint == b.preparation.fingerprint
    assert env.full_state() == before  # aucune livraison, aucun lien, réservation intacte


def test_a_revocation_between_the_two_reads_stops_before_claude(env, alice):
    command, reserved = env.reserve(alice)
    orchestrator = during_market_data(env, lambda: env.service.revoke_session(alice.session_id, "logout"))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason) == (dg.REFUSED, dg.SESSION_REFUSED)
    assert env.claude.calls == [] and env.count("assistant_deliveries") == 0


def test_a_level_change_during_the_providers_is_a_context_change(env, alice):
    command, reserved = env.reserve(alice)
    # Écriture historique (révision inchangée, comme PUT /api/user/{id}/level) : détectée par level.
    orchestrator = during_market_data(env, lambda: env.execute("UPDATE users SET level = 'avance' WHERE id = :u",
                                                               u=alice.user_id))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert outcome.reason == dg.CONTEXT_CHANGED and env.claude.calls == []


def test_an_aba_level_change_is_invisible_to_b2_and_documented(env, alice):
    command, reserved = env.reserve(alice)

    def aba():
        env.execute("UPDATE users SET level = 'avance' WHERE id = :u", u=alice.user_id)
        env.execute("UPDATE users SET level = 'debutant' WHERE id = :u", u=alice.user_id)

    orchestrator = during_market_data(env, aba)
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert outcome.status == dg.GENERATED  # limite ABA : (level, level_revision) identiques
    assert (outcome.candidate.preparation.level, outcome.candidate.preparation.level_revision) == ("debutant", 0)


def test_concurrent_contributions_or_facts_between_reads_are_context_changes(env, alice):
    for insert in ("statement", "fact"):
        command, reserved = env.reserve(alice)
        session_id = env.add_session(step="moat") if insert == "statement" else env.sessions()[0]["id"]

        def write():
            if insert == "statement":
                env.add_statement(session_id, "business", "Ajout concurrent.")
            else:
                env.add_fact(session_id, "roe", 12.0)

        outcome = during_market_data(env, write).generate(alice.credential(), command, reserved)
        assert outcome.reason == dg.CONTEXT_CHANGED and env.claude.calls == [], insert


def test_an_anchor_modification_between_reads_is_a_context_change(env, alice, legacy_route):
    command, reserved = env.reserve(alice)
    env.anchor_conversation(legacy_route)
    [event] = env.events()
    orchestrator = during_market_data(env, lambda: env.execute(
        "UPDATE cognitive_events SET status = 'abandoned', closed_at = clock_timestamp() WHERE id = :i", i=event["id"]))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    # La seconde lecture constate l'ancre fermée : refus stale, jamais un rattachement.
    assert outcome.reason == dc.STALE_CONVERSATION_CONTEXT and env.claude.calls == []


def test_a_change_of_the_active_analysis_during_claude_is_carried_for_pr9c(env, alice):
    command, reserved = env.reserve(alice)
    session_id = env.add_session(step="business")
    lease = reserved.value.lease
    orchestrator = during_claude(env, lambda: env.execute(
        "UPDATE analysis_sessions SET current_step = 'moat' WHERE id = :i", i=session_id))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert outcome.status == dg.GENERATED
    snapshot = outcome.candidate.preparation
    assert snapshot.active_session.current_step == "business"  # contexte RÉELLEMENT utilisé
    # Une lecture postérieure diffère : PR 9-C doit revalider ; B2 ne prétend rien.
    later = env.read(alice, lease, command).value
    assert later.active_session.current_step == "moat"
    rebuilt = dcand.build_snapshot(later, snapshot.ticker_input, snapshot.method_id, snapshot.method_basis)
    assert rebuilt.fingerprint != snapshot.fingerprint
    assert env.count("assistant_deliveries") == 0


def _take_over(env, alice, command, reservation_id):
    env.expire(reservation_id)
    successor = env.run(alice, command)
    assert successor.status == AUTHORIZED and successor.value.lease.lease_epoch == 2
    return successor


def test_a_lease_taken_over_between_reads_stops_before_claude(env, alice):
    command, reserved = env.reserve(alice)
    taken = []
    orchestrator = during_market_data(env, lambda: taken.append(
        _take_over(env, alice, command, reserved.value.lease.reservation_id)))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason) == (dg.REFUSED, dg.LEASE_STALE) and env.claude.calls == []
    [row] = env.reservations()
    assert (row["lease_epoch"], row["state"]) == (2, "reserved") and taken[0].value.lease.token.matches(
        row["lease_token_hash"])  # successeur intact


def test_a_candidate_of_an_old_epoch_is_fenced_out_by_b1(env, alice):
    command, reserved = env.reserve(alice)
    orchestrator = during_claude(env, lambda: _take_over(env, alice, command, reserved.value.lease.reservation_id))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert outcome.status == dg.GENERATED  # B2 ne peut pas le savoir : C refera la décision sous verrou
    candidate = outcome.candidate
    assert candidate.lease.lease_epoch == 1
    [row] = env.reservations()
    assert row["lease_epoch"] == 2 and not candidate.lease.token.matches(row["lease_token_hash"])
    old = env.run(alice, c.RecordDecryptageAttemptFailure.from_lease(candidate.lease, dr.GENERATION_UNUSABLE))
    assert old.status == REVISION_CONFLICT  # l'ancien détenteur ne touche jamais son successeur
    assert env.read(alice, candidate.lease, command).status == REVISION_CONFLICT
    assert env.reservations() == [row]


def test_a_crash_after_generation_leaves_no_trace_and_no_repair(env, alice, monkeypatch):
    command, reserved = env.reserve(alice)
    orchestrator = env.orchestrator()
    before = env.full_state()

    def crash(raw):
        raise RuntimeError("arrêt brutal après la réponse Claude (simulé)")

    monkeypatch.setattr(dcand, "validate_response", crash)
    with pytest.raises(RuntimeError):
        orchestrator.generate(alice.credential(), command, reserved)
    assert env.full_state() == before  # aucune livraison, aucune déclaration, aucune réparation
    assert len(env.claude.requests) == 1
    assert orchestrator.generate(alice.credential(), command, reserved).reason == dg.LEASE_ALREADY_USED


def test_a_refused_declaration_never_presumes_the_lease_released(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    lease_id = reserved.value.lease.reservation_id

    def fail_and_expire():
        env.expire(lease_id)
        raise TimeoutError("Claude lent")

    orchestrator = env.orchestrator(clock, claude=FakeClaude(clock, actions={"create_message": fail_and_expire}))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.declaration) == (
        dg.PROVIDER_FAILED, dr.GENERATION_UNAVAILABLE, dg.DECLARATION_REFUSED)
    [row] = env.reservations()
    assert row["lease_token_hash"] is not None and row["retry_not_before_at"] is None  # rien n'a été libéré


def test_an_uncertain_declaration_commit_is_reported_as_uncertain(env, alice, monkeypatch):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)

    def fail():
        env.hooks["executed"] = lambda: armed.append(1)  # la prochaine transaction exécutée : la déclaration
        raise ConnectionError("Claude indisponible")

    orchestrator = env.orchestrator(clock, claude=FakeClaude(clock, actions={"create_message": fail}))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    env.hooks.clear()
    assert (outcome.status, outcome.declaration) == (dg.PROVIDER_FAILED, dg.DECLARATION_UNCERTAIN)
    [row] = env.reservations()  # committée malgré l'erreur : seule une lecture constate l'état réel
    assert row["lease_token_hash"] is None and row["retry_not_before_at"] is not None
    assert env.count("assistant_deliveries") == 0
    assert row["id"] == reserved.value.lease.reservation_id
