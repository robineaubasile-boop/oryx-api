"""V0-T08 Lot 2B-2 bloc B — registre provisoire et certification des
preuves (core/product_analytics/proofs.py), contre un VRAI PostgreSQL (16 et
18 en CI). La frontière F2 et le vrai runner : tests/test_activity_proof_bridge.py.

1. sans base : vocabulaires identiques à 0024, natures certifiables (jamais
   assistant_delivery), refus avant tout accès à la base ;
2. origine : bonne origine, autre utilisateur, autre campagne, interaction
   d'une autre visite, 'close', inconnue, visite clôturée, origine déplacée,
   écrivain hors service (avant J0, après J+12) ;
3. états : pending exact, pending -> certified exact, idempotence, double
   certification, rejets terminaux (retrait, clôture, annulation), règlement
   indeterminate, aucune résurrection, conservation de 168 heures exactes ;
4. temps : J0, J+12 - 1 µs, J+12, J+12 + 1 µs, horloge PostgreSQL lue après
   une attente de verrou qui franchit l'échéance ;
5. appareils multiples et contraintes SQL de 0024 (dernière défense) ;
6. concurrence : inscription, certification, conflit de visites, clôture et
   retrait concurrents, finalisation de visite, retry après échec.
Jamais d'événement ni d'attribution définitive.
"""
import dataclasses
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from core.authz_f2 import attestations as att
from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import participation as pp
from core.product_analytics import proofs
from core.product_analytics import sessions as ps
from core.product_analytics.common import InvalidProductAnalyticsInput
from tests.activity_proof_support import (
    ACK,
    CREATE,
    DELETE,
    LAPTOP,
    NOTICE,
    PHONE,
    POLICY,
    REGISTRY,
    TWELVE_DAYS,
    UPDATE,
    Background,
    Clock,
    Holder,
    analytics_engine,
    certify,
    close,
    committed,
    definitive_rows,
    observe,
    open_wave,
    operation,
    origin_of,
    proof_row,
    register,
    registry,
    settle,
    touch,
    utc,
    waiting,
)
from tests.test_cognitive_capture import _NoDB
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _reset_schema, _run_alembic
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

USER_A, USER_B = "v0-proof-user-a", "v0-proof-user-b"
MIGRATION_0024 = REPO_ROOT / "alembic" / "versions" / "0024_product_activity_proofs.py"
HOURS_168 = timedelta(hours=168)
MICRO = timedelta(microseconds=1)


def _m0024():
    spec = importlib.util.spec_from_file_location("m0024_block_b", MIGRATION_0024)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_vocabularies_are_those_of_0024_and_only_the_attested_natures_are_certifiable():
    m = _m0024()
    assert proofs.PROOF_STATUSES == m.PROOF_STATUSES
    assert proofs.RESOLUTION_REASONS == m.RESOLUTION_REASONS
    assert set(proofs.REJECTION_REASONS) | set(proofs.INDETERMINATE_REASONS) == set(m.RESOLUTION_REASONS)
    assert not set(proofs.REJECTION_REASONS) & set(proofs.INDETERMINATE_REASONS)
    assert proofs.REGISTRY_RETENTION == HOURS_168 and m.REGISTRY_RETENTION == "168 hours"
    couples = {kind: (event_type, surface) for event_type, surface, kinds in m.OPERATION_RULES for kind in kinds}
    assert all(couples[kind] == rule for kind, rule in proofs.OPERATION_RULES.items())
    # Natures certifiables = exactement celles que F2 atteste aujourd'hui ;
    # ni assistant_delivery (Éducation, Coach : aucune preuve de réussite), ni portfolio_write.
    assert set(proofs.OPERATION_KINDS) == set(att.OPERATIONS)
    assert "assistant_delivery" not in proofs.OPERATION_KINDS and "portfolio_write" not in proofs.OPERATION_KINDS


def _snapshot(**overrides):
    fields = {"campaign_id": uuid.uuid4(), "user_id": USER_A, "visit_id": uuid.uuid4(),
              "interaction_key": uuid.uuid4(), "recorded_at": datetime(2026, 11, 23, tzinfo=timezone.utc)}
    fields.update(overrides)
    return proofs.OriginSnapshot(**fields)


@pytest.mark.parametrize("function", [proofs.register_pending, proofs.certify_pending])
def test_no_new_effect_and_owner_conflict_are_refused_before_any_database_access(function):
    kwargs = {"proof_id": uuid.uuid4()} if function is proofs.certify_pending else {}
    for op, reason in ((operation(USER_A, new_effect=False), proofs.NO_NEW_EFFECT),
                       (operation(USER_B), proofs.OWNER_CONFLICT)):
        decision = function(_NoDB(), origin=_snapshot(), operation=op, **kwargs)
        assert decision == proofs.ProofDecision(proofs.ABSENT, reason)


@pytest.mark.parametrize("bad", [
    {"operation": proofs.CommittedOperation(USER_A, "assistant_delivery", uuid.uuid4(), True)},
    {"operation": proofs.CommittedOperation(USER_A, "portfolio_write", uuid.uuid4(), True)},
    {"operation": proofs.CommittedOperation(USER_A, CREATE, str(uuid.uuid4()), True)},
    {"operation": proofs.CommittedOperation(USER_A, CREATE, uuid.uuid4(), 1)},
    {"operation": SimpleNamespace(owner_user_id=USER_A, operation_kind=CREATE, operation_id=uuid.uuid4(),
                                  new_effect=True)},
    {"origin": SimpleNamespace(**dataclasses.asdict(_snapshot()))},
    {"origin": _snapshot(recorded_at=datetime(2026, 11, 23))},
])
def test_out_of_contract_inputs_are_refused_before_any_database_access(bad):
    kwargs = {"origin": _snapshot(), "operation": operation(USER_A), **bad}
    for function in (proofs.register_pending, proofs.settle):
        extra = {"proof_id": uuid.uuid4()} if function is proofs.settle else {}
        with pytest.raises(InvalidProductAnalyticsInput):
            function(_NoDB(), **kwargs, **extra)


def test_contracts_carry_no_user_content_and_never_print_identifiers():
    assert [f.name for f in dataclasses.fields(proofs.OriginSnapshot)] == [
        "campaign_id", "user_id", "visit_id", "interaction_key", "recorded_at"]
    assert [f.name for f in dataclasses.fields(proofs.CommittedOperation)] == [
        "owner_user_id", "operation_kind", "operation_id", "new_effect"]
    origin, op = _snapshot(), operation(USER_A)
    for value in (origin, op):
        text = repr(value)
        assert USER_A not in text and str(op.operation_id) not in text and str(origin.visit_id) not in text


def test_eligibility_requires_both_deadlines_strictly():
    end, close_by = datetime(2026, 12, 13, tzinfo=timezone.utc), datetime(2026, 12, 12, tzinfo=timezone.utc)
    participation = SimpleNamespace(participation_status="enrolled", window_end_at=end)
    campaign = SimpleNamespace(status="active", close_by_at=close_by)
    check = proofs._eligibility_problem
    assert check(participation, campaign, close_by - MICRO) is None
    assert check(participation, campaign, close_by) == proofs.WINDOW_EXPIRED
    assert check(SimpleNamespace(participation_status="completed", window_end_at=end), campaign,
                 close_by - MICRO) == proofs.NOT_AUTHORIZED


# --------------------------------------------------------------------------
# 2. PostgreSQL réel : préparation
# --------------------------------------------------------------------------

CLEANUP = (REGISTRY, "product_activity_event_attributions", "product_activity_events",
           "product_activity_session_interactions", "product_activity_sessions", "product_cohort_seats",
           "product_campaign_participants", "product_campaign_cohorts", "product_campaigns")


@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    eng = analytics_engine(pg_url)
    _reset_schema(eng)
    _run_alembic(pg_url, "upgrade", "head")
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:a, 'debutant'), (:b, 'debutant')"),
                     {"a": USER_A, "b": USER_B})
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    before = _deadlocks(engine)
    yield engine
    assert definitive_rows(engine) == (0, 0)  # le bloc B n'écrit jamais d'événement ni d'attribution
    assert _deadlocks(engine) == before, "interblocage PostgreSQL détecté"
    with engine.begin() as conn:
        for table in CLEANUP:
            conn.execute(sa.text(f"DELETE FROM {table}"))


def _deadlocks(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
                            ).scalar_one()


@pytest.fixture
def clock(monkeypatch):
    """Même horloge déterministe pour 2A / 2B-1 et pour le registre."""
    fake = Clock(datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5))
    monkeypatch.setattr(common, "utcnow", fake)
    monkeypatch.setattr(proofs, "server_clock", fake)
    return fake


def _pending(db, user=USER_A, kind=CREATE, **kwargs):
    origin = origin_of(db, user, **kwargs)
    op = operation(user, kind)
    decision = register(db, origin, op)
    assert decision.state == proofs.PENDING and decision.written
    return origin, op, decision


def _resolved_exactly(row, status, reason, *, certified_at=None):
    assert (row["status"], row["resolution_reason"], row["recorded_event_id"]) == (status, reason, None)
    assert row["certified_at"] == certified_at
    assert row["resolved_at"] is not None and row["purge_after_at"] - row["resolved_at"] == HOURS_168


# --------------------------------------------------------------------------
# 3. Origine
# --------------------------------------------------------------------------

def test_the_right_owner_campaign_visit_and_interaction_give_an_exact_snapshot(db):
    campaign = open_wave(db, USER_A)
    visit, key = touch(db, campaign, USER_A)
    origin = observe(db, campaign, USER_A, visit, key)
    [interaction] = [r for r in _interactions(db) if r["interaction_key"] == key]
    assert origin == proofs.OriginSnapshot(campaign, USER_A, visit, key, utc(interaction["recorded_at"]))


def _interactions(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text("SELECT * FROM product_activity_session_interactions"))
                .mappings()]


def _refused(db, reason, **kwargs):
    with pytest.raises(proofs.ProofRefused) as caught:
        observe(db, **kwargs)
    assert caught.value.reason == reason


def test_foreign_or_unknown_origins_are_refused(db):
    campaign_a, campaign_b = open_wave(db, USER_A), open_wave(db, USER_B)
    visit_a, key_a = touch(db, campaign_a, USER_A)
    visit_b, key_b = touch(db, campaign_b, USER_B)
    other_visit, other_key = touch(db, campaign_a, USER_A, environment=PHONE)
    close_key = close(db, campaign_a, USER_A, environment=PHONE)
    base = {"campaign_id": campaign_a, "user_id": USER_A, "visit_id": visit_a, "interaction_key": key_a}
    # Autre utilisateur : B n'a aucune participation à la campagne de A ; A n'a pas l'interaction de B.
    _refused(db, proofs.NOT_AUTHORIZED, **{**base, "user_id": USER_B})
    _refused(db, proofs.NOT_AUTHORIZED, **{**base, "campaign_id": campaign_b})  # A n'est pas inscrit à B
    _refused(db, proofs.VISIT_CONFLICT, **{**base, "visit_id": visit_b})  # visite d'un autre propriétaire
    _refused(db, proofs.ORIGIN_UNVERIFIED, **{**base, "interaction_key": key_b})
    _refused(db, proofs.VISIT_CONFLICT, **{**base, "interaction_key": other_key})  # interaction d'une autre visite
    _refused(db, proofs.ORIGIN_UNVERIFIED, **{**base, "visit_id": other_visit, "interaction_key": close_key})
    _refused(db, proofs.ORIGIN_UNVERIFIED, **{**base, "interaction_key": uuid.uuid4()})  # inconnue
    _refused(db, proofs.VISIT_CONFLICT, **{**base, "visit_id": uuid.uuid4()})  # visite inconnue
    with pytest.raises(InvalidProductAnalyticsInput):
        observe(db, **{**base, "visit_id": str(visit_a)})


def test_a_visit_closed_later_keeps_its_historically_valid_interaction(db):
    campaign = open_wave(db, USER_A)
    visit, key = touch(db, campaign, USER_A)
    origin = observe(db, campaign, USER_A, visit, key)
    close(db, campaign, USER_A)  # déconnexion : visite finalisée ('logout')
    assert committed(db, lambda s: s.get(ps.ProductActivitySession, visit)).finalized_at is not None
    assert observe(db, campaign, USER_A, visit, key) == origin
    op = operation(USER_A)
    pending = register(db, origin, op)
    assert certify(db, origin, op, pending.proof_id).state == proofs.CERTIFIED


def test_a_moved_or_altered_origin_is_never_registered(db):
    origin = origin_of(db, USER_A)
    other_visit, other_key = touch(db, origin.campaign_id, USER_A, environment=PHONE)
    for moved, reason in ((dataclasses.replace(origin, recorded_at=origin.recorded_at + MICRO),
                           proofs.ORIGIN_UNVERIFIED),
                          (dataclasses.replace(origin, visit_id=other_visit), proofs.VISIT_CONFLICT),
                          (dataclasses.replace(origin, interaction_key=other_key), proofs.VISIT_CONFLICT),
                          (dataclasses.replace(origin, user_id=USER_B), proofs.NOT_AUTHORIZED)):
        op = operation(moved.user_id)
        assert register(db, moved, op) == proofs.ProofDecision(proofs.ABSENT, reason)
    assert registry(db) == []


def _forged_interaction(db, origin, recorded_at) -> uuid.UUID:
    """Écrivain HORS service : interaction 'activity' de la même visite à un
    instant choisi (les CHECK de 0023 ne bornent pas l'instant)."""
    key = uuid.uuid4()
    with db.begin() as conn:
        digest = conn.execute(sa.text("SELECT environment_digest FROM product_activity_sessions WHERE id = :v"),
                              {"v": origin.visit_id}).scalar_one()
        conn.execute(sa.text(
            "INSERT INTO product_activity_session_interactions (campaign_id, user_id, interaction_key, "
            "interaction_kind, environment_digest, session_id, recorded_at) VALUES (:c, :u, :k, 'activity', :d, :v, "
            ":r)"), {"c": origin.campaign_id, "u": origin.user_id, "k": key, "d": digest, "v": origin.visit_id,
                     "r": recorded_at})
    return key


def test_an_interaction_outside_the_observation_is_never_an_origin(db):
    origin = origin_of(db, USER_A)
    participation = committed(db, lambda s: s.get(pp.ProductCampaignParticipant, (origin.campaign_id, USER_A)))
    for recorded_at in (participation.window_start_at - MICRO, participation.window_end_at,
                        participation.window_end_at + MICRO):
        key = _forged_interaction(db, origin, recorded_at)
        _refused(db, proofs.ORIGIN_UNVERIFIED, campaign_id=origin.campaign_id, user_id=USER_A,
                 visit_id=origin.visit_id, interaction_key=key)
        forged = dataclasses.replace(origin, interaction_key=key, recorded_at=recorded_at)
        assert register(db, forged, operation(USER_A)).reason == proofs.ORIGIN_UNVERIFIED
    assert registry(db) == []


# --------------------------------------------------------------------------
# 4. États
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind, event_type, surface", [(CREATE, "portfolio_used", "portfolio"),
                                                       (UPDATE, "portfolio_used", "portfolio"),
                                                       (DELETE, "portfolio_used", "portfolio"),
                                                       (ACK, "decrypter_used", "decrypter")])
def test_pending_then_certified_rows_are_exact(db, clock, kind, event_type, surface):
    origin = origin_of(db, USER_A)
    op = operation(USER_A, kind)
    created = clock.advance(seconds=1)
    pending = register(db, origin, op)
    [row] = registry(db)
    assert pending == proofs.ProofDecision(proofs.PENDING, None, True, row["id"], created, None)
    expected_identity = {"campaign_id": origin.campaign_id, "user_id": USER_A, "visit_id": origin.visit_id,
                         "interaction_key": origin.interaction_key, "interaction_kind": "activity",
                         "event_type": event_type, "surface": surface, "source_operation_kind": kind,
                         "source_operation_id": op.operation_id}
    assert {k: row[k] for k in expected_identity} == expected_identity
    assert (row["status"], utc(row["created_at"]), row["certified_at"], row["resolved_at"], row["recorded_event_id"],
            row["resolution_reason"], row["purge_after_at"]) == ("pending", created, None, None, None, None, None)
    certified_at = clock.advance(seconds=2)
    decision = certify(db, origin, op, pending.proof_id)
    assert decision == proofs.ProofDecision(proofs.CERTIFIED, None, True, row["id"], created, certified_at)
    after = proof_row(db, row["id"])
    assert (after["status"], utc(after["certified_at"]), after["resolved_at"], after["recorded_event_id"],
            after["resolution_reason"], after["purge_after_at"]) == ("certified", certified_at, None, None, None, None)
    assert {k: after[k] for k in expected_identity} == expected_identity  # identité et origine inchangées


def test_registration_and_certification_are_idempotent(db, clock):
    origin, op, pending = _pending(db)
    again = register(db, origin, op)
    assert again == dataclasses.replace(pending, written=False)
    first_row = proof_row(db, pending.proof_id)
    certified = certify(db, origin, op, pending.proof_id)
    row = proof_row(db, pending.proof_id)
    clock.advance(hours=1)
    for repeat in (certify(db, origin, op, pending.proof_id), register(db, origin, op)):
        assert repeat == dataclasses.replace(certified, written=False)  # même identité, même instant
    assert proof_row(db, pending.proof_id) == row and row["xmin"] != first_row["xmin"]  # aucune seconde écriture
    assert len(registry(db)) == 1


def test_a_certification_never_targets_another_proof_identifier(db):
    origin, op, pending = _pending(db)
    assert certify(db, origin, op, uuid.uuid4()) == proofs.ProofDecision(proofs.ABSENT, proofs.OPERATION_CONFLICT)
    assert settle(db, origin, op, uuid.uuid4()) == proofs.ProofDecision(proofs.ABSENT, proofs.OPERATION_CONFLICT)
    assert certify(db, origin, operation(USER_A), uuid.uuid4()).reason == proofs.TECHNICAL_UNCERTAINTY
    assert proof_row(db, pending.proof_id)["status"] == "pending"


def _withdraw(db, origin):
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=origin.campaign_id, user_id=origin.user_id))


@pytest.mark.parametrize("change", ["withdraw", "close", "cancel"])
def test_a_lost_authorization_rejects_the_pending_proof_terminally(db, clock, change):
    origin, op, pending = _pending(db)
    if change == "withdraw":
        _withdraw(db, origin)
    else:
        transition = pc.close_campaign if change == "close" else pc.cancel_campaign
        committed(db, lambda s: transition(s, campaign_id=origin.campaign_id))
    resolved_at = clock.advance(seconds=3)
    decision = certify(db, origin, op, pending.proof_id)
    assert (decision.state, decision.reason, decision.written) == (proofs.REJECTED, proofs.NOT_AUTHORIZED, True)
    row = proof_row(db, pending.proof_id)
    _resolved_exactly(row, "rejected", "not_authorized")
    assert utc(row["resolved_at"]) == resolved_at and utc(row["purge_after_at"]) == resolved_at + HOURS_168
    # Terminal : ni re-certification, ni ré-inscription, ni règlement.
    clock.advance(seconds=1)
    for repeat in (certify(db, origin, op, pending.proof_id), register(db, origin, op),
                   settle(db, origin, op, pending.proof_id)):
        assert (repeat.state, repeat.written) == (proofs.REJECTED, False)
    assert proof_row(db, pending.proof_id) == row


def test_settle_turns_only_a_pending_proof_into_an_indeterminate_one(db, clock):
    origin, op, pending = _pending(db)
    resolved_at = clock.advance(seconds=4)
    decision = settle(db, origin, op, pending.proof_id)
    assert (decision.state, decision.reason, decision.written) == (proofs.INDETERMINATE,
                                                                   proofs.TECHNICAL_UNCERTAINTY, True)
    row = proof_row(db, pending.proof_id)
    _resolved_exactly(row, "indeterminate", "technical_uncertainty")
    assert utc(row["resolved_at"]) == resolved_at
    clock.advance(seconds=1)
    for repeat in (certify(db, origin, op, pending.proof_id), register(db, origin, op),
                   settle(db, origin, op, pending.proof_id)):
        assert (repeat.state, repeat.written) == (proofs.INDETERMINATE, False)  # jamais ressuscitée
    assert proof_row(db, pending.proof_id) == row
    # Une preuve certifiée est constatée, jamais rendue indéterminée.
    origin2, op2, pending2 = _pending(db)
    certified = certify(db, origin2, op2, pending2.proof_id)
    assert settle(db, origin2, op2, pending2.proof_id) == dataclasses.replace(certified, written=False)


def test_a_resolution_keeps_a_historical_certification_when_the_contract_allows_it(db, clock):
    """La forme rejected / indeterminate avec certified_at conservé reste
    admise par 0024 ; le bloc B ne la produit pas lui-même (bloc C), mais ne la
    modifie jamais."""
    origin, op, pending = _pending(db)
    certified = certify(db, origin, op, pending.proof_id)
    later = clock.advance(seconds=10)
    with db.begin() as conn:  # résolution d'un bloc ultérieur (simulée en SQL)
        conn.execute(sa.text(f"UPDATE {REGISTRY} SET status = 'indeterminate', resolved_at = :r, "
                             "resolution_reason = 'finalization_expired', purge_after_at = :p WHERE id = :i"),
                     {"r": later, "p": later + HOURS_168, "i": pending.proof_id})
    row = proof_row(db, pending.proof_id)
    assert utc(row["certified_at"]) == certified.certified_at
    for repeat in (certify(db, origin, op, pending.proof_id), settle(db, origin, op, pending.proof_id),
                   register(db, origin, op)):
        assert (repeat.state, repeat.certified_at, repeat.written) == (proofs.INDETERMINATE,
                                                                       certified.certified_at, False)
    assert proof_row(db, pending.proof_id) == row


# --------------------------------------------------------------------------
# 5. Temps : J0, J+12 et horloge après les verrous
# --------------------------------------------------------------------------

def _window(db, origin):
    participation = committed(db, lambda s: s.get(pp.ProductCampaignParticipant, (origin.campaign_id,
                                                                                   origin.user_id)))
    return participation.window_start_at, participation.window_end_at


def test_an_interaction_at_j0_is_registered_and_certified_at_j0(db, clock):
    origin = origin_of(db, USER_A)  # J0, interaction et inscription au même instant serveur
    start, end = _window(db, origin)
    assert origin.recorded_at == start and end - start == TWELVE_DAYS
    op = operation(USER_A)
    pending = register(db, origin, op)
    assert certify(db, origin, op, pending.proof_id).certified_at == start


@pytest.mark.parametrize("offset, certified", [(-MICRO, True), (timedelta(0), False), (MICRO, False)])
def test_certification_is_admitted_strictly_before_j_plus_12(db, clock, offset, certified):
    origin = origin_of(db, USER_A)
    _, end = _window(db, origin)
    op = operation(USER_A)
    pending = register(db, origin, op)
    clock.set(end + offset)
    decision = certify(db, origin, op, pending.proof_id)
    row = proof_row(db, pending.proof_id)
    if certified:
        assert (decision.state, utc(row["certified_at"])) == (proofs.CERTIFIED, end - MICRO)
    else:  # jamais antidatée : refus terminal à l'instant réel
        assert (decision.state, decision.reason) == (proofs.REJECTED, proofs.WINDOW_EXPIRED)
        _resolved_exactly(row, "rejected", "window_expired")
        assert utc(row["resolved_at"]) == end + offset


@pytest.mark.parametrize("offset, registered", [(-MICRO, True), (timedelta(0), False), (MICRO, False)])
def test_registration_is_admitted_strictly_before_j_plus_12(db, clock, offset, registered):
    origin = origin_of(db, USER_A)
    _, end = _window(db, origin)
    clock.set(end + offset)
    decision = register(db, origin, operation(USER_A))
    if registered:
        assert decision.state == proofs.PENDING and utc(proof_row(db, decision.proof_id)["created_at"]) == end - MICRO
    else:
        assert decision == proofs.ProofDecision(proofs.ABSENT, proofs.WINDOW_EXPIRED) and registry(db) == []


def test_the_postgresql_clock_is_read_after_a_lock_wait_that_crosses_j_plus_12(db):
    """Horloge PostgreSQL RÉELLE : la décision attend le retrait (non
    committé) qui verrouille la participation ; pendant l'attente, la fenêtre
    se termine ; la certification n'est pas antidatée."""
    campaign = committed(db, lambda s: _active_campaign(s))
    now = common.utcnow()
    committed(db, lambda s: pp.enroll_participant(s, campaign_id=campaign, user_id=USER_A,
                                                  window_start_at=now + timedelta(milliseconds=200),
                                                  window_end_at=now + timedelta(seconds=3),
                                                  authorization_recorded_at=now, notice_version=NOTICE))
    _sleep_until(now + timedelta(milliseconds=300))
    visit, key = touch(db, campaign, USER_A)
    origin = observe(db, campaign, USER_A, visit, key)
    op = operation(USER_A)
    pending = register(db, origin, op)
    holder = Holder(db, sql="SELECT 1 FROM product_campaign_participants WHERE campaign_id = :c FOR UPDATE",
                    c=campaign)
    contender = Background(lambda: certify(db, origin, op, pending.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la certification n'attend pas le verrou de participation")
        _sleep_until(now + timedelta(seconds=3, milliseconds=200))  # J+12 franchi pendant l'attente
    finally:
        holder.rollback()
    decision = contender.outcome()
    assert (decision.state, decision.reason) == (proofs.REJECTED, proofs.WINDOW_EXPIRED)
    assert utc(proof_row(db, pending.proof_id)["resolved_at"]) >= now + timedelta(seconds=3)


def _active_campaign(session):
    now = common.utcnow()
    campaign = pc.create_campaign(session, code=f"wave-0-{uuid.uuid4().hex[:10]}", name="Vague 0",
                                  purpose="Mesurer l'usage des surfaces Oryx.", close_by_at=now + timedelta(days=30),
                                  **POLICY)
    pc.activate_campaign(session, campaign_id=campaign.id)
    return campaign.id


def _sleep_until(instant):
    import time
    delay = (instant - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        time.sleep(delay)


def _wait(predicate, message, timeout=10.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


# --------------------------------------------------------------------------
# 6. Appareils multiples et contraintes SQL (dernière défense)
# --------------------------------------------------------------------------

def test_the_same_operation_from_a_second_device_is_never_reattributed(db):
    campaign = open_wave(db, USER_A)
    laptop = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=LAPTOP))
    phone = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=PHONE))
    assert laptop.visit_id != phone.visit_id
    op = operation(USER_A)
    pending = register(db, laptop, op)
    certified = certify(db, laptop, op, pending.proof_id)
    row = proof_row(db, pending.proof_id)
    assert register(db, phone, op) == proofs.ProofDecision(proofs.ABSENT, proofs.VISIT_CONFLICT)
    assert certify(db, phone, op, pending.proof_id) == proofs.ProofDecision(proofs.ABSENT, proofs.VISIT_CONFLICT)
    assert settle(db, phone, op, pending.proof_id) == proofs.ProofDecision(proofs.ABSENT, proofs.VISIT_CONFLICT)
    other_campaign = origin_of(db, USER_A)
    assert register(db, other_campaign, op).reason == proofs.OPERATION_CONFLICT
    assert proof_row(db, pending.proof_id) == row and utc(row["certified_at"]) == certified.certified_at
    # Deux utilisateurs : même identifiant d'opération, deux identités canoniques distinctes.
    bob = origin_of(db, USER_B)
    theirs = register(db, bob, operation(USER_B, operation_id=op.operation_id))
    assert theirs.state == proofs.PENDING and len(registry(db)) == 2


def test_the_0024_constraints_remain_the_last_line_of_defense(db):
    campaign = open_wave(db, USER_A)
    laptop = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=LAPTOP))
    phone = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=PHONE))
    op = operation(USER_A)
    register(db, laptop, op)
    close_key = close(db, campaign, USER_A, environment=PHONE)
    insert = (f"INSERT INTO {REGISTRY} (id, campaign_id, user_id, visit_id, interaction_key, interaction_kind, "
              "event_type, surface, source_operation_kind, source_operation_id, status, created_at) VALUES "
              "(gen_random_uuid(), :c, :u, :v, :k, :kind, 'portfolio_used', 'portfolio', :o, :i, 'pending', now())")
    base = {"c": campaign, "u": USER_A, "v": phone.visit_id, "k": phone.interaction_key, "kind": "activity",
            "o": CREATE, "i": op.operation_id}
    cases = (({}, "uq_product_activity_proof_registry_source_operation"),  # seconde preuve de la même opération
             ({"i": uuid.uuid4(), "k": laptop.interaction_key}, "product_activity_proof_registry_interaction_fkey"),
             ({"i": uuid.uuid4(), "k": close_key, "kind": "close"},
              "ck_product_activity_proof_registry_interaction_kind"),
             ({"i": uuid.uuid4(), "o": "portfolio_write"}, "ck_product_activity_proof_registry_operation"))
    for overrides, constraint in cases:
        with pytest.raises(IntegrityError) as caught:
            with db.begin() as conn:
                conn.execute(sa.text(insert), {**base, **overrides})
        assert caught.value.orig.diag.constraint_name == constraint
    assert len(registry(db)) == 1


# --------------------------------------------------------------------------
# 7. Concurrence
# --------------------------------------------------------------------------

def _contended(db, holder_action, contender_action, *, expected_waiting=1):
    holder = Holder(db, holder_action)
    contender = Background(contender_action)
    contender.start()
    try:
        _wait(lambda: waiting(db) == expected_waiting, "le concurrent n'attend pas")
    finally:
        holder.commit()
    return holder.value, contender.outcome()


def test_concurrent_registrations_of_one_identity_create_one_pending_proof(db):
    origin, op = origin_of(db, USER_A), operation(USER_A)
    won, lost = _contended(db, lambda s: proofs.register_pending(s, origin=origin, operation=op),
                           lambda: register(db, origin, op))
    assert won.state == proofs.PENDING and won.written
    assert lost == dataclasses.replace(won, written=False) and len(registry(db)) == 1


def test_concurrent_certifications_certify_once(db):
    origin, op, pending = _pending(db)
    won, lost = _contended(db, lambda s: proofs.certify_pending(s, origin=origin, operation=op,
                                                                proof_id=pending.proof_id),
                           lambda: certify(db, origin, op, pending.proof_id))
    assert won.state == proofs.CERTIFIED and won.written
    assert lost == dataclasses.replace(won, written=False)


def test_concurrent_registrations_from_two_visits_keep_the_first_origin(db):
    campaign = open_wave(db, USER_A)
    laptop = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=LAPTOP))
    phone = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=PHONE))
    op = operation(USER_A)
    won, lost = _contended(db, lambda s: proofs.register_pending(s, origin=laptop, operation=op),
                           lambda: register(db, phone, op))
    assert won.state == proofs.PENDING and lost == proofs.ProofDecision(proofs.ABSENT, proofs.VISIT_CONFLICT)
    [row] = registry(db)
    assert (row["visit_id"], row["interaction_key"]) == (laptop.visit_id, laptop.interaction_key)


@pytest.mark.parametrize("change", ["close", "cancel", "withdraw"])
def test_a_certification_waits_for_a_concurrent_closure_or_withdrawal_and_sees_it(db, change):
    origin, op, pending = _pending(db)
    if change == "withdraw":
        def action(s):
            return pp.withdraw_participant(s, campaign_id=origin.campaign_id, user_id=USER_A)
    else:
        transition = pc.close_campaign if change == "close" else pc.cancel_campaign

        def action(s):
            return transition(s, campaign_id=origin.campaign_id)
    _, decision = _contended(db, action, lambda: certify(db, origin, op, pending.proof_id))
    assert (decision.state, decision.reason) == (proofs.REJECTED, proofs.NOT_AUTHORIZED)


def test_a_closure_waits_for_a_certification_in_progress(db):
    origin, op, pending = _pending(db)
    certified, closed = _contended(db, lambda s: proofs.certify_pending(s, origin=origin, operation=op,
                                                                        proof_id=pending.proof_id),
                                   lambda: committed(db, lambda s: pc.close_campaign(s, campaign_id=origin.campaign_id
                                                                                     ).status))
    assert certified.state == proofs.CERTIFIED and closed == "closed"
    assert proof_row(db, pending.proof_id)["status"] == "certified"  # décision prise avant la clôture : conservée


def test_a_visit_finalized_concurrently_never_invalidates_the_proof(db, clock):
    origin, op, pending = _pending(db)
    clock.advance(minutes=31)  # visite expirée : finalisable
    holder = Holder(db, lambda s: ps.finalize_idle_sessions(s, campaign_id=origin.campaign_id))
    try:
        # La certification ne verrouille pas la visite : elle n'attend pas sa finalisation.
        decision = certify(db, origin, op, pending.proof_id)
    finally:
        holder.commit()
    assert decision.state == proofs.CERTIFIED
    assert len(holder.value) == 1 and committed(db, lambda s: s.get(ps.ProductActivitySession,
                                                                    origin.visit_id)).finalized_at is not None
    # Une inscription pendant la finalisation attend la clé de la visite (FK), puis réussit.
    origin2 = observe(db, origin.campaign_id, USER_A, *touch(db, origin.campaign_id, USER_A))
    clock.advance(minutes=31)
    op2 = operation(USER_A)
    _, late = _contended(db, lambda s: ps.finalize_idle_sessions(s, campaign_id=origin.campaign_id),
                         lambda: register(db, origin2, op2))
    assert late.state == proofs.PENDING


def test_a_failed_registration_or_certification_leaves_nothing_and_the_retry_is_clean(db, monkeypatch):
    origin, op = origin_of(db, USER_A), operation(USER_A)
    real = proofs.server_clock

    def failing(session):
        raise RuntimeError("panne injectée après les verrous")
    monkeypatch.setattr(proofs, "server_clock", failing)
    with pytest.raises(RuntimeError):
        register(db, origin, op)
    assert registry(db) == []
    monkeypatch.setattr(proofs, "server_clock", real)
    pending = register(db, origin, op)
    monkeypatch.setattr(proofs, "server_clock", failing)
    with pytest.raises(RuntimeError):
        certify(db, origin, op, pending.proof_id)
    assert proof_row(db, pending.proof_id)["status"] == "pending"
    monkeypatch.setattr(proofs, "server_clock", real)
    assert certify(db, origin, op, pending.proof_id).state == proofs.CERTIFIED
    assert len(registry(db)) == 1


def _outside_writer(db, origin, op):
    with db.begin() as conn:
        conn.execute(sa.text(
            f"INSERT INTO {REGISTRY} (id, campaign_id, user_id, visit_id, interaction_key, interaction_kind, "
            "event_type, surface, source_operation_kind, source_operation_id, status, created_at) VALUES "
            "(gen_random_uuid(), :c, :u, :v, :k, 'activity', 'portfolio_used', 'portfolio', :o, :i, 'pending', "
            "clock_timestamp())"), {"c": origin.campaign_id, "u": op.owner_user_id, "v": origin.visit_id,
                                    "k": origin.interaction_key, "o": op.operation_kind, "i": op.operation_id})


def test_a_savepoint_keeps_the_callers_transaction_usable_after_a_lost_race(db, monkeypatch):
    """Écrivain hors service committé entre la lecture et l'INSERT : la
    UNIQUE de 0024 refuse, la preuve gagnante est relue et jamais écrasée."""
    campaign = open_wave(db, USER_A)
    laptop = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=LAPTOP))
    phone = observe(db, campaign, USER_A, *touch(db, campaign, USER_A, environment=PHONE))
    op = operation(USER_A)
    real = proofs._proof_for_update
    calls = []

    def racing(session, operation_):
        found = real(session, operation_)
        if not calls:
            calls.append("raced")
            _outside_writer(db, phone, op)  # autre transaction, committée, sans le verrou d'identité
        return found
    monkeypatch.setattr(proofs, "_proof_for_update", racing)

    def action(session):
        decision = proofs.register_pending(session, origin=laptop, operation=op)
        session.execute(sa.text("SELECT 1"))  # transaction de l'appelant toujours utilisable
        return decision
    assert committed(db, action) == proofs.ProofDecision(proofs.ABSENT, proofs.VISIT_CONFLICT)
    [row] = registry(db)
    assert row["visit_id"] == phone.visit_id
