"""V0-T08 2B-2 PR 0 — attestations métier internes de l'ACK F2 Décrypter,
contre un VRAI PostgreSQL (16 et 18 en CI), sous le VRAI runner et le
registre de PRODUCTION, avec des livraisons produites par la VRAIE chaîne
B1 -> B2 -> C1-B (transports simulés, aucun réseau).

La distinction premier ACK / rejeu provient du statut de la livraison OBSERVÉ
par le pont sous SON verrou FOR UPDATE, avant toute mutation ; jamais du
statut final (toujours delivered), jamais de la preuve C1-B lue avant ce
verrou. Elle n'est exposée qu'après un COMMIT confirmé (_run_attested) ; la
réponse de run() et la réponse HTTP restent celles de l'historique.

Matrice : premier ACK ; ACK identique ; deux ACK concurrents (deux sessions) ;
ACK C2 contre ACK historique (deux ordres, dont celui où la preuve pré-verrou
dit encore pending) ; ACK rejoué depuis un autre contexte autorisé ; livraison
étrangère ; identité, réservation ou provenance incohérente ; échec avant
COMMIT ; COMMIT refusé ; COMMIT incertain ; absence de seconde capture ;
réponse HTTP historique conservée ; aucune nouvelle cause d'échec.
"""
import logging
import uuid

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import attestations as att
from core.authz_f2.runner import (
    AUTHORIZED,
    NOT_FOUND,
    REVISION_CONFLICT,
    SESSION_INVALID,
    UNAVAILABLE,
    AttestedOutcome,
    AuthorizationOutcome,
    AuthorizedTransactionRunner,
)
from core.authz_f2_http import decryptage as c3
from tests.authz_f2_pr9a_support import BOB_EMAIL, LEGACY_APP, USER_A, USER_B, Background, Pause, legacy_ack, \
    wait_until
from tests.authz_f2_pr9b1_support import CONV
from tests.authz_f2_pr9c1b_support import schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c3_support import ACK, HttpEnv, body_of, c3_headers, identity_fields, post
from tests.test_authz_f2_pr9a_concurrency import LegacyPause, waited_relations, waiting_pids
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture
def env(pg_url, schema, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="oryx.authz_f2")
    environment = HttpEnv(pg_url)  # CanonicalEnv + application isolée C3
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before, "interblocage PostgreSQL détecté"
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


@pytest.fixture
def bob(env):
    return env.confirmed(BOB_EMAIL, USER_B)


def attested(env, issued, command, **kwargs) -> AttestedOutcome:
    kwargs.setdefault("decryptage_keys", None)  # C2 n'utilise aucun trousseau
    out = env.b1_runner(**kwargs)._run_attested(issued.credential(), command)
    assert type(out) is AttestedOutcome and type(out.outcome) is AuthorizationOutcome
    return out


def expected(done, qualification, user_id=USER_A):
    return att.BusinessAttestation(user_id, att.DECRYPTAGE_DELIVERY_ACK, done.assistant_turn_id, qualification)


def acked(env, issued, done, qualification, **kwargs):
    out = attested(env, issued, done.ack(), **kwargs)
    assert out.outcome.status == AUTHORIZED and out.outcome.value == bridge.DeliveryAcknowledged(
        done.assistant_turn_id, "delivered", out.outcome.value.response_action), out
    assert type(out.outcome.value) is bridge.DeliveryAcknowledged  # jamais l'enveloppe interne
    assert out.attestation == expected(done, qualification, out.outcome.principal.user_id)
    return out


def refused(env, issued, command, status, **kwargs):
    before = env.full_snapshot()
    out = attested(env, issued, command, **kwargs)
    assert out.outcome == AuthorizationOutcome(status) and out.attestation is None, out
    assert env.full_snapshot() == before
    return out


def single_capture(env, done):
    link = env.link(done.assistant_turn_id)
    assert link["capture_state"] == "captured" and link["response_action"] == "opened_event"
    assert len([e for e in env.events() if e["status"] == "open"
                and e["conversation_key"] == done.conversation_key]) == 1  # un seul event ouvert par la réponse
    assert env.delivery(done.assistant_turn_id)["status"] == "delivered"


def no_unavailable(caplog):
    assert "status=unavailable" not in caplog.text, caplog.text


# --------------------------------------------------------------------------
# 1. Premier ACK, ACK identique, absence de seconde capture
# --------------------------------------------------------------------------

def test_the_first_canonical_ack_is_attested_first_after_commit(env, alice):
    done = env.finalized(alice)
    assert env.delivery(done.assistant_turn_id)["status"] == "pending"
    out = acked(env, alice, done, att.DELIVERY_FIRST_ACK)
    assert out.outcome.value.response_action == "opened_event" and out.attestation.new_effect is True
    single_capture(env, done)  # la transition et la capture ont bien été committées


def test_a_second_identical_ack_is_a_replay_writes_nothing_and_captures_nothing(env, alice):
    done = env.finalized(alice)
    first = acked(env, alice, done, att.DELIVERY_FIRST_ACK)
    before, events = env.full_snapshot(), env.counts()["cognitive_events"]
    second = acked(env, alice, done, att.DELIVERY_REPLAY)
    assert second.outcome.value == first.outcome.value and second.attestation.new_effect is False
    assert env.full_snapshot() == before and env.counts()["cognitive_events"] == events  # aucune seconde capture
    assert first.attestation.operation_id == second.attestation.operation_id  # même opération, autre qualification


def test_each_delivery_has_its_own_first_ack(env, alice):
    one, two = env.finalized(alice), env.finalized(alice, conversation_key="conv-deux")
    a = acked(env, alice, one, att.DELIVERY_FIRST_ACK).attestation
    b = acked(env, alice, two, att.DELIVERY_FIRST_ACK).attestation
    assert a.operation_id != b.operation_id and a != b  # jamais interchangeables


# --------------------------------------------------------------------------
# 2. Concurrence et autres contextes autorisés
# --------------------------------------------------------------------------

def _paused(env, issued, command, point="executed"):
    pause = Pause(point)
    worker = Background(lambda: attested(env, issued, command, hook=pause))
    worker.start()
    assert pause.reached.wait(15), "point d'interruption non atteint"
    return pause, worker


def test_two_concurrent_acks_attest_exactly_one_first_ack(env, alice, caplog):
    done = env.finalized(alice)
    second = env.confirmed("alice2@example.com", USER_A)
    pause, holder = _paused(env, alice, done.ack())
    contender = Background(lambda: attested(env, second, done.ack()))
    contender.start()
    wait_until(lambda: env.waiting() == 1, message="le second ACK n'attend pas")
    assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"users"}  # ordre des verrous inchangé
    pause.release.set()
    first, other = holder.outcome(), contender.outcome()
    assert first.attestation == expected(done, att.DELIVERY_FIRST_ACK)
    assert other.attestation == expected(done, att.DELIVERY_REPLAY)
    assert first.outcome.value == other.outcome.value
    single_capture(env, done)
    no_unavailable(caplog)


def test_c2_ack_holding_the_delivery_makes_a_concurrent_legacy_ack_a_replay(env, alice, caplog):
    done = env.finalized(alice)
    pause, holder = _paused(env, alice, done.ack())
    contender = Background(lambda: legacy_ack(env, CONV, done.assistant_turn_id))
    contender.start()
    wait_until(lambda: env.waiting(LEGACY_APP) == 1, message="l'ACK historique n'attend pas")
    pause.release.set()
    assert holder.outcome().attestation == expected(done, att.DELIVERY_FIRST_ACK)
    assert contender.outcome()["delivery_status"] == "delivered"
    single_capture(env, done)
    no_unavailable(caplog)


def test_the_qualification_comes_from_the_locked_delivery_not_from_the_pre_lock_proof(env, alice, monkeypatch,
                                                                                      caplog):
    """ACK historique en cours (capture faite, COMMIT non encore fait) : la
    preuve C1-B, lue sous le verrou B1 mais AVANT celui de la livraison, voit
    encore pending ; le pont attend le verrou de la livraison, relit delivered :
    l'ACK C2 est un rejeu. Déduire la nouveauté de la preuve serait faux."""
    done = env.finalized(alice)
    seen = []
    original = _adapters._canonical_proof

    def recording(conn, user_id, command):
        proof = original(conn, user_id, command)
        seen.append(getattr(proof, "delivery_status", None))
        return proof

    monkeypatch.setattr(_adapters, "_canonical_proof", recording)
    hold = LegacyPause(monkeypatch, "acknowledge_delivery")
    holder = Background(lambda: legacy_ack(env, CONV, done.assistant_turn_id))
    holder.start()
    assert hold.reached.wait(15)
    contender = Background(lambda: attested(env, alice, done.ack()))
    contender.start()
    wait_until(lambda: env.waiting() == 1, message="l'ACK C2 n'attend pas")
    assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"assistant_deliveries"}
    hold.release.set()
    assert holder.outcome()["delivery_status"] == "delivered"
    out = contender.outcome()
    assert seen == ["pending"]  # la preuve pré-verrou aurait conclu à tort à un premier ACK
    assert out.outcome.status == AUTHORIZED and out.attestation == expected(done, att.DELIVERY_REPLAY)
    single_capture(env, done)
    no_unavailable(caplog)


def test_an_ack_replayed_from_another_authorized_context_is_a_replay(env, alice):
    done = env.finalized(alice)
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)
    other_session = env.confirmed("alice2@example.com", USER_A)
    acked(env, other_session, done, att.DELIVERY_REPLAY)
    with_keyring = attested(env, alice, done.ack(), decryptage_keys=env.decryptage_keys)
    assert with_keyring.attestation == expected(done, att.DELIVERY_REPLAY)


def test_an_ack_after_a_committed_legacy_ack_is_a_replay(env, alice):
    done = env.finalized(alice)
    assert legacy_ack(env, CONV, done.assistant_turn_id)["delivery_status"] == "delivered"
    before = env.full_snapshot()
    acked(env, alice, done, att.DELIVERY_REPLAY)
    assert env.full_snapshot() == before


# --------------------------------------------------------------------------
# 3. Refus : aucune attestation de réussite
# --------------------------------------------------------------------------

def test_a_foreign_delivery_has_no_attestation(env, alice, bob):
    done = env.finalized(alice)
    refused(env, bob, done.ack(), NOT_FOUND)
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)  # le propriétaire garde son premier ACK
    refused(env, bob, done.ack(), NOT_FOUND)


@pytest.mark.parametrize("change", [{"conversation_key": "conv-autre"}, {"assistant_turn_id": "other"},
                                    {"reservation_id": "other"}])
def test_an_inconsistent_reservation_or_identity_has_no_attestation(env, alice, change):
    done = env.finalized(alice)
    fields = {k: (uuid.uuid4() if v == "other" else v) for k, v in change.items()}
    status = NOT_FOUND if "reservation_id" in change else REVISION_CONFLICT
    refused(env, alice, done.ack(**fields), status)
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)


def test_a_non_finalized_reservation_has_no_attestation(env, alice):
    from tests.authz_f2_pr9b1_support import reserve_command
    lease = env.run(alice, reserve_command()).value.lease
    refused(env, alice, identity_ack(lease.reservation_id), REVISION_CONFLICT)


def identity_ack(reservation_id):
    from core.authz_f2 import commands as c
    return c.AcknowledgeDecryptageDelivery(reservation_id, CONV, uuid.uuid4())


@pytest.mark.parametrize("fault", ["provenance", "attestation_port"])
def test_an_incoherent_provenance_or_attestation_has_no_attestation(env, alice, monkeypatch, fault):
    done = env.finalized(alice)
    with monkeypatch.context() as patch:
        if fault == "provenance":
            def incoherent(*a, **k):
                raise bridge.ProvenanceIncoherent("provenance incohérente (test)")
            patch.setattr(bridge, "_require_owned_provenance", incoherent)
        else:
            def refuse(self, **kwargs):
                raise contracts.CapabilityRefused("attestation invalide (test)")
            patch.setattr(_adapters._AckAdmissionPort, "require_delivery", refuse)
        refused(env, alice, done.ack(), UNAVAILABLE)
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)  # rien n'avait survécu


def test_an_invalid_session_has_no_attestation(env, alice):
    done = env.finalized(alice)
    env.service.revoke_session(alice.session_id, "security")
    refused(env, alice, done.ack(), SESSION_INVALID)


# --------------------------------------------------------------------------
# 4. Échec avant COMMIT, COMMIT refusé, COMMIT incertain
# --------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["authorized", "executed"])
def test_a_failure_before_commit_leaves_no_first_ack_attestation(env, alice, point):
    done = env.finalized(alice)

    def hook(at):
        if at == point:
            raise RuntimeError("panne avant COMMIT (simulée)")

    refused(env, alice, done.ack(), UNAVAILABLE, hook=hook)
    assert env.delivery(done.assistant_turn_id)["status"] == "pending"
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)  # le premier ACK committé est le suivant


def test_a_commit_rejected_by_postgresql_leaves_no_attestation(env, alice):
    done = env.finalized(alice)
    with env.observer.begin() as conn:
        conn.execute(sa.text("CREATE FUNCTION pr0_reject() RETURNS trigger LANGUAGE plpgsql AS "
                             "$$ BEGIN RAISE EXCEPTION 'pr0 commit refusé'; END $$"))
        conn.execute(sa.text("CREATE CONSTRAINT TRIGGER pr0_reject AFTER UPDATE ON assistant_deliveries "
                             "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pr0_reject()"))
    try:
        refused(env, alice, done.ack(), UNAVAILABLE)
    finally:
        with env.observer.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER pr0_reject ON assistant_deliveries"))
            conn.execute(sa.text("DROP FUNCTION pr0_reject()"))
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)


def test_an_uncertain_commit_leaves_no_attestation_and_the_next_ack_is_a_replay(env, alice, monkeypatch):
    """COMMIT effectué, confirmation perdue : UNAVAILABLE sans attestation (la
    statistique est perdue, jamais présumée) ; l'ACK suivant observe delivered
    sous verrou : rejeu, aucune seconde capture."""
    done = env.finalized(alice)
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    out = attested(env, alice, done.ack(), hook=lambda point: armed.append(1) if point == "executed" else None)
    assert out.outcome == AuthorizationOutcome(UNAVAILABLE) and out.attestation is None
    assert env.delivery(done.assistant_turn_id)["status"] == "delivered"  # committé malgré l'erreur
    events = env.counts()["cognitive_events"]
    acked(env, alice, done, att.DELIVERY_REPLAY)
    assert env.counts()["cognitive_events"] == events


# --------------------------------------------------------------------------
# 5. Réponses publiques et causes d'échec inchangées
# --------------------------------------------------------------------------

def test_run_returns_exactly_the_historical_outcome(env, alice):
    done = env.finalized(alice)
    first = env.c2(alice, done.ack())
    second = env.c2(alice, done.ack())
    for outcome in (first, second):
        assert type(outcome) is AuthorizationOutcome and type(outcome.value) is bridge.DeliveryAcknowledged
        assert outcome.value == bridge.DeliveryAcknowledged(done.assistant_turn_id, "delivered", "opened_event")
        assert c3.acknowledged(outcome.value, done.assistant_turn_id)  # contrôle HTTP historique (trois champs)


def test_the_http_ack_response_is_byte_identical_and_never_uses_the_attested_path(env, alice, monkeypatch):
    done = env.finalized(alice)

    def forbidden(*a, **k):
        raise AssertionError("la route ne doit jamais appeler _run_attested")

    monkeypatch.setattr(AuthorizedTransactionRunner, "_run_attested", forbidden)
    app = env.c3_app()
    payload = body_of(identity_fields(done.reservation_id, done.assistant_turn_id))
    first = post(app, ACK, payload, c3_headers(alice))
    second = post(app, ACK, payload, c3_headers(alice))
    expected_body = ('{"assistant_turn_id":"' + str(done.assistant_turn_id) + '","delivery_status":"delivered"}')
    assert first.status_code == second.status_code == 200
    assert first.content == second.content and first.json() == {"assistant_turn_id": str(done.assistant_turn_id),
                                                                  "delivery_status": "delivered"}
    assert first.content.replace(b" ", b"") == expected_body.encode()
    single_capture(env, done)


def test_an_attestation_that_cannot_be_built_never_fails_the_ack(env, alice, monkeypatch):
    def broken(*args, **kwargs):
        raise att.AttestationInvalid("panne simulée du contrat")

    monkeypatch.setattr(att, "BusinessAttestation", broken)
    done = env.finalized(alice)
    out = attested(env, alice, done.ack())
    assert out.outcome.status == AUTHORIZED and out.attestation is None  # statistique perdue, ACK intact
    assert type(out.outcome.value) is bridge.DeliveryAcknowledged
    single_capture(env, done)


def test_a_misbound_attestation_is_dropped_never_the_ack(env, alice, monkeypatch, caplog):
    original = att.delivery_ack
    monkeypatch.setattr(att, "delivery_ack", lambda owner, turn, *, first: original(USER_B, turn, first=first))
    done = env.finalized(alice)
    out = attested(env, alice, done.ack())
    assert out.outcome.status == AUTHORIZED and out.attestation is None
    assert "attestation=dropped" in caplog.text and USER_B not in caplog.text
    single_capture(env, done)


def test_the_adapter_still_checks_the_bare_bridge_view(env, alice, monkeypatch):
    """L'enveloppe ne contourne jamais le contrôle historique : une vue
    substituée (autre livraison) est refusée, capture et statut annulés."""
    done = env.finalized(alice)
    original = bridge._execute

    def substituted(conn, ctx, operation, probe):
        view, attestation = att.unwrap(original(conn, ctx, operation, probe))
        forged = bridge.DeliveryAcknowledged(uuid.uuid4(), view.delivery_status, view.response_action)
        return att.attach(forged, attestation)

    monkeypatch.setattr(bridge, "_execute", substituted)
    refused(env, alice, done.ack(), UNAVAILABLE)


# --------------------------------------------------------------------------
# 6. Correctif d'audit PR 0 : pannes de construction et de transport injectées
# --------------------------------------------------------------------------

from tests.test_authz_f2_attestations import DROPPING_FAULTS, inject_fault  # noqa: E402


def _statements(env, action):
    """(résultat, texte SQL exécuté dans l'ordre) : même texte = mêmes
    instructions, mêmes verrous."""
    log = []

    def record(conn, cursor, statement, parameters, context, executemany):
        log.append(" ".join(statement.split()))

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        return action(), log
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)


@pytest.mark.parametrize("fault", DROPPING_FAULTS)
def test_an_attestation_fault_never_fails_the_ack_nor_changes_anything_else(env, alice, bob, monkeypatch, fault):
    # Référence SANS panne : même parcours chez un utilisateur indépendant
    # (l'état cognitif est propre à chaque utilisateur).
    reference = env.finalized(bob, conversation_key="conv-bob")
    done = env.finalized(alice)
    ref_out, ref_sql = _statements(env, lambda: attested(env, bob, reference.ack()))
    assert ref_out.attestation == expected(reference, att.DELIVERY_FIRST_ACK, USER_B)
    inject_fault(monkeypatch, fault)
    out, sql = _statements(env, lambda: attested(env, alice, done.ack()))
    # 1. ACK réussi ; 2. résultat public identique ; 3. aucune attestation exposée.
    assert out.outcome.status == AUTHORIZED and out.attestation is None, out
    assert type(out.outcome.value) is bridge.DeliveryAcknowledged
    assert out.outcome.value == bridge.DeliveryAcknowledged(done.assistant_turn_id, "delivered",
                                                            ref_out.outcome.value.response_action)
    assert out.outcome.principal.user_id == USER_A
    single_capture(env, done)
    # 5. Mêmes instructions SQL (donc mêmes verrous) que sans panne.
    assert sql == ref_sql
    # Rejeu sous la même panne : réussi, sans écriture, sans attestation.
    before = env.full_snapshot()
    replay = attested(env, alice, done.ack())
    assert replay.outcome.status == AUTHORIZED and replay.attestation is None
    assert replay.outcome.value == out.outcome.value and env.full_snapshot() == before
    historical = env.c2(alice, done.ack())  # run() : contrat historique exact
    assert type(historical) is AuthorizationOutcome and historical.value == out.outcome.value


def test_overridable_envelope_methods_are_never_used_by_the_transport(env, alice, monkeypatch):
    done = env.finalized(alice)
    inject_fault(monkeypatch, "envelope_accessors")
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)  # transport natif : attestation intacte


@pytest.mark.parametrize("fault", DROPPING_FAULTS)
def test_refusals_and_rollbacks_are_unchanged_under_an_attestation_fault(env, alice, bob, monkeypatch, fault):
    done = env.finalized(alice)
    with monkeypatch.context() as patch:
        inject_fault(patch, fault)
        refused(env, bob, done.ack(), NOT_FOUND)
        refused(env, alice, done.ack(conversation_key="conv-autre"), REVISION_CONFLICT)

        def crash(point):
            if point == "executed":
                raise RuntimeError("panne métier avant COMMIT (simulée)")

        refused(env, alice, done.ack(), UNAVAILABLE, hook=crash)  # ROLLBACK métier légitime conservé
        assert env.delivery(done.assistant_turn_id)["status"] == "pending"
    acked(env, alice, done, att.DELIVERY_FIRST_ACK)  # panne retirée : rien n'avait survécu


@pytest.mark.parametrize("fault", ["attestation_init", "envelope_type", "binding"])
def test_the_http_ack_is_byte_identical_under_an_attestation_fault(env, alice, monkeypatch, fault):
    done = env.finalized(alice)
    inject_fault(monkeypatch, fault)
    app = env.c3_app()
    payload = body_of(identity_fields(done.reservation_id, done.assistant_turn_id))
    first, second = post(app, ACK, payload, c3_headers(alice)), post(app, ACK, payload, c3_headers(alice))
    expected_body = ('{"assistant_turn_id":"' + str(done.assistant_turn_id) + '","delivery_status":"delivered"}')
    assert first.status_code == second.status_code == 200 and first.content == second.content
    assert first.content.replace(b" ", b"") == expected_body.encode()
    single_capture(env, done)
