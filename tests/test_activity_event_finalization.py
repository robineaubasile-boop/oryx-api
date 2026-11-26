"""V0-T08 Lot 2B-2 bloc C — finalisation atomique des preuves certifiées
(core/product_analytics/finalization.py, chemin certifié de events.py),
contre un VRAI PostgreSQL (16 et 18 en CI). Chaîne réelle F2 -> B -> C et
frontière de transaction : tests/test_activity_event_finalization_chain.py.

1. sans base : vocabulaires (= 0024, = attestations F2, = bloc B), point
   d'entrée fermé (identifiant de preuve seulement), record_event refuse
   toujours les natures canoniques, projection, échéances ;
2. états : absente, pending jamais enregistrable, pending expirée résolue
   sans certification, certified -> recorded exact, recorded idempotente,
   rejected / indeterminate terminales, nature non prise en charge, preuve
   certifiée incohérente ;
3. temps : J0, J+12 - 1 µs, J+12, après J+12, borne - 1 µs, borne, au-delà,
   close_by_at plus restrictif, visite close ou expirée avant J+12 (sans
   effet sur l'échéance), horloge régressive, clôture, annulation, retrait,
   horloge PostgreSQL réelle après une attente de verrou ; ultime horloge
   après les écritures : attente sur l'index unique d'un écrivain hors
   service qui annule ou committe avant / après la borne ;
4. atomicité : pannes avant / après chaque écriture et pendant le flush,
   violations SQL réelles, transaction de l'appelant utilisable ;
5. idempotence et concurrence : finalisations concurrentes, preuves
   distinctes, certification et résolution concurrentes, retrait / clôture /
   annulation concurrents, collisions (clé, opération canonique, événement
   2A, état partiel, écrivain hors service), attribution incorrecte ;
6. conservation : 90 jours depuis occurred_at, 168 heures absolues,
   certified_at identiques, indépendance vis-à-vis du registre, relecture
   hors conservation refusée ;
7. non-contournement et dormance (inventaires).
"""
import ast
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.authz_f2 import attestations as att
from core.models import (
    ProductActivityEvent,
    ProductActivityEventAttribution,
    ProductActivityProof,
    ProductCampaignParticipant,
)
from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import events as pe
from core.product_analytics import finalization as fz
from core.product_analytics import participation as pp
from core.product_analytics import proofs
from core.product_analytics import retention
from core.product_analytics import sessions as ps
from core.product_analytics.common import InvalidProductAnalyticsInput
from tests.activity_proof_support import (
    ACK,
    ATTRIBUTIONS,
    CREATE,
    DELETE,
    EVENTS,
    LAPTOP,
    NOTICE,
    POLICY,
    PROOF_APP,
    REGISTRY,
    UPDATE,
    Background,
    Clock,
    Holder,
    analytics_engine,
    certify,
    close,
    committed,
    open_origin,
    open_wave,
    opened,
    operation,
    proof_row,
    register,
    rows,
    settle,
    touch,
    utc,
    waiting,
)
from tests.test_cognitive_capture import _NoDB
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _reset_schema, _run_alembic
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

USER_A, USER_B = "v0-final-user-a", "v0-final-user-b"
HOURS_168 = timedelta(hours=168)
DAYS_90 = timedelta(days=90)
MICRO = timedelta(microseconds=1)
GRACE = timedelta(minutes=30)
KINDS = [(CREATE, "portfolio_used", "portfolio"), (UPDATE, "portfolio_used", "portfolio"),
         (DELETE, "portfolio_used", "portfolio"), (ACK, "decrypter_used", "decrypter")]
PACKAGE = REPO_ROOT / "core" / "product_analytics"
SERVICE = PACKAGE / "finalization.py"
BOUNDARY = REPO_ROOT / "core" / "_activity_event_finalization.py"
# V0-T08 Lot 2C PR 1 (nominatif) : la frontière du cycle de vie reprend les
# preuves par les points d'entrée du bloc C, inchangés ; elle-même sans appelant.
LIFECYCLE_BOUNDARY = REPO_ROOT / "core" / "_activity_proof_lifecycle.py"
# V0-T08 Lot 2C PR 2, correctif post-audit (nominatif) : le cycle de vie des
# événements VÉRIFIE (lecture seule) les champs canoniques d'un événement
# certifié avec la requête canonique du bloc C ; il ne l'écrit jamais.
EVENT_LIFECYCLE_SERVICE = PACKAGE / "event_lifecycle.py"


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_the_certified_vocabulary_is_exactly_the_attested_f2_natures_and_the_0024_couples():
    assert set(pe.CERTIFIED_OPERATIONS) == set(proofs.OPERATION_KINDS) == set(att.OPERATIONS)
    for kind, (event_type, surface, historical) in pe.CERTIFIED_OPERATIONS.items():
        assert proofs.OPERATION_RULES[kind] == (event_type, surface)
        # nature historique 2A du même parcours (jamais une nature de preuve)
        assert common.EVENT_TYPE_RULES[event_type].operation_kind == historical
        assert historical in common.SOURCE_OPERATION_KINDS and historical not in proofs.OPERATION_KINDS
    # Éducation et Coach : aucune attestation de réussite, donc aucun événement certifié.
    assert "assistant_delivery" not in pe.CERTIFIED_OPERATIONS
    assert {r[0] for r in pe.CERTIFIED_OPERATIONS.values()} == {"decrypter_used", "portfolio_used"}
    # Le chemin historique est inchangé : aucune grâce générique, natures 2A.
    assert pe.LATE_EVENT_GRACE == timedelta(0) and common.SOURCE_OPERATION_KINDS == ("assistant_delivery",
                                                                                     "portfolio_write")
    assert fz.FINALIZATION_GRACE == common.SESSION_FINALIZATION_GRACE == GRACE
    assert set(fz.FINALIZATION_REASONS) == set(proofs.RESOLUTION_REASONS) | {fz.RETENTION_EXPIRED,
                                                                              fz.RECORD_INCOHERENT}
    assert fz.FINALIZATION_STATES == proofs.DECISION_STATES
    attribution_columns = [c.name for c in ProductActivityEventAttribution.__table__.columns]
    assert set(attribution_columns) == set(fz.IDENTITY_FIELDS) | {"event_id", "certified_at"}
    assert set(fz.IDENTITY_FIELDS) <= {c.name for c in ProductActivityProof.__table__.columns}


def test_the_entry_points_receive_only_a_proof_identifier():
    for function in (fz.finalize_certified_proof, fz.resolve_expired_pending_proof, fz.observe_proof):
        assert list(inspect.signature(function).parameters) == ["db", "proof_id"]
        for designation in ({"user_id": USER_A}, {"campaign_id": uuid.uuid4()}, {"visit_id": uuid.uuid4()},
                            {"interaction_key": uuid.uuid4()}, {"occurred_at": datetime.now(timezone.utc)},
                            {"outcome": "succeeded"}, {"source_operation_kind": CREATE}):
            with pytest.raises(TypeError):
                function(_NoDB(), proof_id=uuid.uuid4(), **designation)
        for bad in (str(uuid.uuid4()), None, 42):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), proof_id=bad)  # refus avant tout accès à la base


def _record_kwargs(**overrides):
    now = datetime.now(timezone.utc)
    kwargs = {"user_id": USER_A, "visit_id": uuid.uuid4(), "idempotency_key": uuid.uuid4(),
              "event_type": "portfolio_used", "surface": "portfolio", "executed_route": "f2",
              "source": "server_business", "source_operation_kind": "portfolio_write",
              "source_operation_id": uuid.uuid4(), "outcome": "succeeded", "delivery_status": "not_applicable",
              "occurred_at": now, "resolved_at": now, "delivered_at": None}
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("kind, event_type, surface", KINDS)
def test_record_event_still_refuses_every_canonical_nature_before_any_database_access(kind, event_type, surface):
    """Paramètres de réussite inventés : le contrat 2A n'écrit jamais une
    nature canonique, quelle que soit la combinaison."""
    now = datetime.now(timezone.utc)
    for source in ("server_business", "client_signal", "server_visit"):
        for delivery, delivered in (("not_applicable", None), ("confirmed", now)):
            with pytest.raises(InvalidProductAnalyticsInput):
                pe.record_event(_NoDB(), **_record_kwargs(event_type=event_type, surface=surface, source=source,
                                                          source_operation_kind=kind, delivery_status=delivery,
                                                          delivered_at=delivered))
    # Éducation / Coach : jamais server_business.
    for event_type in ("education_used", "coach_used"):
        with pytest.raises(InvalidProductAnalyticsInput):
            pe.record_event(_NoDB(), **_record_kwargs(event_type=event_type, surface=event_type.split("_")[0],
                                                      source_operation_kind="assistant_delivery"))


def test_the_certified_request_is_entirely_determined_by_the_canonical_nature():
    occurred = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
    certified = occurred + timedelta(seconds=2)
    base = {"user_id": USER_A, "visit_id": uuid.uuid4(), "idempotency_key": uuid.uuid4(),
            "source_operation_id": uuid.uuid4(), "occurred_at": occurred, "resolved_at": certified}
    ack = pe._certified_request(source_operation_kind=ACK, **base)
    assert {k: ack[k] for k in ("event_type", "surface", "source", "outcome", "executed_route", "delivery_status",
                                "delivered_at", "occurred_at", "resolved_at")} == {
        "event_type": "decrypter_used", "surface": "decrypter", "source": "server_business",
        "outcome": "succeeded", "executed_route": "f2", "delivery_status": "confirmed", "delivered_at": occurred,
        "occurred_at": occurred, "resolved_at": certified}
    for kind in (CREATE, UPDATE, DELETE):
        portfolio = pe._certified_request(source_operation_kind=kind, **base)
        assert (portfolio["event_type"], portfolio["delivery_status"], portfolio["delivered_at"],
                portfolio["source_operation_kind"]) == ("portfolio_used", "not_applicable", None, kind)
    assert set(ack) == set(pe.REQUEST_FIELDS)
    for kind in ("assistant_delivery", "portfolio_write", "view_opened", None):
        with pytest.raises(InvalidProductAnalyticsInput):
            pe._certified_request(source_operation_kind=kind, **base)
    for bad in ({"resolved_at": None}, {"resolved_at": occurred - MICRO}, {"occurred_at": datetime(2026, 11, 24)},
                {"source_operation_id": "x"}, {"user_id": ""}):
        with pytest.raises(InvalidProductAnalyticsInput):
            pe._certified_request(source_operation_kind=CREATE, **{**base, **bad})


def test_the_certified_writer_refuses_to_run_outside_the_callers_savepoint_or_with_a_foreign_request():
    occurred = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
    key = uuid.uuid4()
    request = pe._certified_request(user_id=USER_A, visit_id=uuid.uuid4(), idempotency_key=key,
                                    source_operation_kind=CREATE, source_operation_id=uuid.uuid4(),
                                    occurred_at=occurred, resolved_at=occurred)
    campaign = SimpleNamespace(id=uuid.uuid4(), retention_policy_version="v0-retention-v1", retention_days=90)
    scope = retention.campaign_scope(campaign, occurred)

    class Outside:
        def in_nested_transaction(self):
            return False

    class Inside:
        def in_nested_transaction(self):
            return True

        def __getattr__(self, name):
            raise AssertionError(f"écriture inattendue : {name}")
    with pytest.raises(InvalidProductAnalyticsInput):
        pe._stage_certified_event(Outside(), user_id=USER_A, idempotency_key=key,
                                  request=request, scope=scope, now=occurred)
    for forged in ({**request, "outcome": "failed"}, {**request, "executed_route": "legacy"},
                   {**request, "source": "client_signal"}, {**request, "delivery_status": "confirmed"}):
        with pytest.raises(InvalidProductAnalyticsInput):
            pe._stage_certified_event(Inside(), user_id=USER_A, idempotency_key=key,
                                      request=forged, scope=scope, now=occurred)
    for bad_scope, now in ((scope, scope.expires_at), (None, occurred), ("scope", occurred)):
        with pytest.raises(InvalidProductAnalyticsInput):
            pe._stage_certified_event(Inside(), user_id=USER_A, idempotency_key=key,
                                      request=request, scope=bad_scope, now=now)
    with pytest.raises(InvalidProductAnalyticsInput):  # occurrence postérieure à l'enregistrement
        pe._stage_certified_event(Inside(), user_id=USER_A, idempotency_key=key,
                                  request=request, scope=scope, now=occurred - MICRO)


def test_the_finalization_deadline_is_bounded_by_the_window_and_the_campaign_never_by_the_visit():
    end = datetime(2026, 12, 5, 9, tzinfo=timezone.utc)
    participation = SimpleNamespace(window_end_at=end)
    campaign = SimpleNamespace(close_by_at=end + timedelta(days=10))
    assert fz.finalization_deadline(participation, campaign) == end + GRACE
    # close_by_at plus restrictif : aucune marge au-delà.
    assert fz.finalization_deadline(participation, SimpleNamespace(close_by_at=end)) == end
    assert fz.finalization_deadline(participation, SimpleNamespace(close_by_at=end + timedelta(minutes=7))) == (
        end + timedelta(minutes=7))
    # Arbitrage R1 : la visite d'origine n'est pas un paramètre de l'échéance.
    assert list(inspect.signature(fz.finalization_deadline).parameters) == ["participation", "campaign"]
    assert not hasattr(fz, "visit_end") and not hasattr(fz, "pending_deadline")
    assert pe.LATE_EVENT_GRACE == timedelta(0)  # chemin historique : aucune grâce


# --------------------------------------------------------------------------
# PostgreSQL réel : préparation
# --------------------------------------------------------------------------

CLEANUP = (REGISTRY, ATTRIBUTIONS, EVENTS, "product_activity_session_interactions", "product_activity_sessions",
           "product_cohort_seats", "product_campaign_participants", "product_campaign_cohorts", "product_campaigns")


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


def _deadlocks(engine):
    return rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]


@pytest.fixture
def db(engine):
    before = _deadlocks(engine)
    yield engine
    assert _deadlocks(engine) == before, "interblocage PostgreSQL détecté"
    assert _orphans(engine) == [], "événement ou attribution orphelin"
    with engine.begin() as conn:
        for table in CLEANUP:
            conn.execute(sa.text(f"DELETE FROM {table}"))


@pytest.fixture
def clock(monkeypatch):
    """Même horloge déterministe pour 2A / 2B-1, le registre et le bloc C."""
    fake = Clock(datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5))
    monkeypatch.setattr(common, "utcnow", fake)
    monkeypatch.setattr(proofs, "server_clock", fake)
    return fake


def _orphans(engine) -> list:
    """Événement certifié sans attribution, attribution sans preuve recorded
    concordante (tant que la preuve est conservée), preuve recorded sans
    événement : jamais."""
    return rows(engine, f"""
        SELECT 'event' AS kind, e.id FROM {EVENTS} e
          JOIN {REGISTRY} p ON p.id = e.idempotency_key AND p.status = 'recorded'
          LEFT JOIN {ATTRIBUTIONS} a ON a.event_id = e.id
         WHERE a.event_id IS NULL OR p.recorded_event_id <> e.id
        UNION ALL
        SELECT 'attribution', a.event_id FROM {ATTRIBUTIONS} a JOIN {REGISTRY} p
            ON p.user_id = a.user_id AND p.source_operation_kind = a.source_operation_kind
           AND p.source_operation_id = a.source_operation_id
         WHERE p.status <> 'recorded' OR p.recorded_event_id <> a.event_id
        UNION ALL
        SELECT 'proof', p.id FROM {REGISTRY} p LEFT JOIN {ATTRIBUTIONS} a ON a.event_id = p.recorded_event_id
         WHERE p.status = 'recorded' AND a.event_id IS NULL""")


def finalize(db, proof_id):
    return committed(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof_id))


def expire(db, proof_id):
    return committed(db, lambda s: fz.resolve_expired_pending_proof(s, proof_id=proof_id))


def _events(db, user_id=None):
    where, params = ("WHERE user_id = :u", {"u": user_id}) if user_id else ("", {})
    return rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS} {where} ORDER BY recorded_at, id", **params)


def _attributions(db):
    return rows(db, f"SELECT *, xmin::text AS xmin FROM {ATTRIBUTIONS} ORDER BY event_id")


def _definitive(db):
    return len(_events(db)), len(_attributions(db))


def _participation(db, campaign_id, user_id=USER_A):
    return committed(db, lambda s: s.get(ProductCampaignParticipant, (campaign_id, user_id)))


def _interaction(db, origin):
    [row] = rows(db, "SELECT * FROM product_activity_session_interactions WHERE interaction_key = :k",
                 k=origin.interaction_key)
    return row


def _certified(db, user=USER_A, kind=CREATE, *, campaign_id=None, environment=LAPTOP):
    origin, op = opened(db, user, kind, campaign_id=campaign_id, environment=environment)
    pending = register(db, origin, op)
    decision = certify(db, origin, op, pending.proof_id)
    assert decision.state == proofs.CERTIFIED, decision
    return SimpleNamespace(origin=origin, op=op, proof_id=pending.proof_id, campaign_id=origin.campaign_id)


def _resolved_exactly(row, status, reason, *, certified=True):
    assert (row["status"], row["resolution_reason"], row["recorded_event_id"]) == (status, reason, None)
    assert (row["certified_at"] is not None) is certified
    assert row["resolved_at"] is not None and row["purge_after_at"] - row["resolved_at"] == HOURS_168


def _recorded_exactly(db, proof, decision, kind, event_type, surface, *, finalized_at):
    """Événement, attribution et preuve EXACTS, mutuellement concordants."""
    row = proof_row(db, proof.proof_id)
    [event] = [e for e in _events(db) if e["id"] == decision.event_id]
    [attribution] = [a for a in _attributions(db) if a["event_id"] == event["id"]]
    interaction = _interaction(db, proof.origin)
    occurred = utc(interaction["recorded_at"])
    delivered = occurred if event_type == "decrypter_used" else None
    assert decision == fz.FinalizationDecision(fz.RECORDED, None, True, proof.proof_id, event["id"], finalized_at)
    assert {k: event[k] for k in ("user_id", "visit_id", "idempotency_key", "event_type", "surface", "executed_route",
                                  "source", "source_operation_kind", "source_operation_id", "outcome",
                                  "delivery_status", "delivered_at", "schema_version")} == {
        "user_id": proof.op.owner_user_id, "visit_id": proof.origin.visit_id, "idempotency_key": proof.proof_id,
        "event_type": event_type, "surface": surface, "executed_route": "f2", "source": "server_business",
        "source_operation_kind": kind, "source_operation_id": proof.op.operation_id, "outcome": "succeeded",
        "delivery_status": "confirmed" if delivered else "not_applicable", "delivered_at": delivered,
        "schema_version": "product-activity-event-v1"}
    assert (utc(event["occurred_at"]), event["resolved_at"], utc(event["recorded_at"]), utc(event["updated_at"])) == (
        occurred, row["certified_at"], finalized_at, finalized_at)
    expiry = occurred + DAYS_90
    assert event["retention_scopes"] == {"version": "retention-scopes-v1", "scopes": [
        {"kind": "campaign", "campaign_id": str(proof.campaign_id), "policy_version": "v0-retention-v1",
         "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}]}
    assert utc(event["next_scope_expires_at"]) == utc(event["purge_after_at"]) == expiry
    assert {k: attribution[k] for k in fz.IDENTITY_FIELDS} == {k: row[k] for k in fz.IDENTITY_FIELDS}
    assert attribution["certified_at"] == row["certified_at"]  # égalité imposée par le service
    assert (row["status"], row["recorded_event_id"], utc(row["resolved_at"]), row["resolution_reason"],
            row["purge_after_at"] - row["resolved_at"]) == ("recorded", event["id"], finalized_at, None, HOURS_168)
    assert utc(interaction["recorded_at"]) <= utc(row["created_at"]) <= utc(row["certified_at"]) <= finalized_at
    return row, event, attribution


# --------------------------------------------------------------------------
# 2. États
# --------------------------------------------------------------------------

def test_an_absent_proof_records_nothing(db):
    assert finalize(db, uuid.uuid4()) == fz.FinalizationDecision(fz.ABSENT)
    assert expire(db, uuid.uuid4()) == fz.FinalizationDecision(fz.ABSENT)
    assert _definitive(db) == (0, 0)


@pytest.mark.parametrize("kind, event_type, surface", KINDS)
def test_a_certified_proof_becomes_exactly_one_event_one_attribution_and_a_recorded_proof(db, clock, kind,
                                                                                          event_type, surface):
    proof = _certified(db, kind=kind)
    before = proof_row(db, proof.proof_id)
    finalized_at = clock.advance(seconds=3)
    decision = finalize(db, proof.proof_id)
    row, _, _ = _recorded_exactly(db, proof, decision, kind, event_type, surface, finalized_at=finalized_at)
    assert _definitive(db) == (1, 1)
    # Identité et origine de la preuve EXACTEMENT celles du bloc B ; certified_at intact.
    assert {k: row[k] for k in (*fz.IDENTITY_FIELDS, "id", "created_at", "certified_at")} == {
        k: before[k] for k in (*fz.IDENTITY_FIELDS, "id", "created_at", "certified_at")}


def test_a_recorded_proof_is_idempotent_and_never_rewritten(db, clock):
    proof = _certified(db, kind=ACK)
    first = finalize(db, proof.proof_id)
    snapshot = (proof_row(db, proof.proof_id), _events(db), _attributions(db))
    for delta in ({"seconds": 1}, {"minutes": 40}, {"days": 3}):  # retries plus tardifs, même après J+12 + 30 min
        clock.advance(**delta)
        again = finalize(db, proof.proof_id)
        assert again == fz.FinalizationDecision(fz.RECORDED, None, False, proof.proof_id, first.event_id,
                                                first.resolved_at)
        assert (proof_row(db, proof.proof_id), _events(db), _attributions(db)) == snapshot  # xmin compris
        assert expire(db, proof.proof_id) == again and committed(
            db, lambda s: fz.observe_proof(s, proof_id=proof.proof_id)) == again


def test_a_pending_proof_is_never_recordable(db, clock):
    origin, op = opened(db, USER_A)
    pending = register(db, origin, op)
    before = proof_row(db, pending.proof_id)
    for delta in ({"seconds": 1}, {"days": 11}, {"days": 2}):
        clock.advance(**delta)
        decision = finalize(db, pending.proof_id)
        assert decision == fz.FinalizationDecision(fz.PENDING, None, False, pending.proof_id)
    assert proof_row(db, pending.proof_id) == before and _definitive(db) == (0, 0)


def test_an_expired_pending_proof_is_resolved_explicitly_without_certification(db, clock):
    origin, op = opened(db, USER_A)
    pending = register(db, origin, op)
    end = _participation(db, origin.campaign_id).window_end_at
    deadline = end + GRACE  # close_by_at (J0 + 30 j) n'est pas plus restrictif
    for instant in (end - MICRO, end, deadline - MICRO):
        clock.set(instant)
        assert expire(db, pending.proof_id) == fz.FinalizationDecision(fz.PENDING, None, False, pending.proof_id)
        assert proof_row(db, pending.proof_id)["status"] == "pending"
    clock.set(deadline)
    decision = expire(db, pending.proof_id)
    assert decision == fz.FinalizationDecision(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED, True,
                                               pending.proof_id, None, deadline)
    row = proof_row(db, pending.proof_id)
    _resolved_exactly(row, "indeterminate", "finalization_expired", certified=False)
    assert utc(row["resolved_at"]) == deadline and _definitive(db) == (0, 0)
    clock.advance(seconds=1)
    for repeat in (expire(db, pending.proof_id), finalize(db, pending.proof_id),
                   certify(db, origin, op, pending.proof_id)):
        assert (repeat.state, repeat.written) == (proofs.INDETERMINATE, False)  # jamais ressuscitée
    assert proof_row(db, pending.proof_id) == row


def test_the_pending_resolution_never_touches_a_certified_proof(db, clock):
    proof = _certified(db)
    clock.set(_participation(db, proof.campaign_id).window_end_at + timedelta(hours=1))
    before = proof_row(db, proof.proof_id)
    assert expire(db, proof.proof_id) == fz.FinalizationDecision(fz.CERTIFIED, None, False, proof.proof_id)
    assert proof_row(db, proof.proof_id) == before


def test_rejected_and_indeterminate_proofs_are_terminal(db, clock):
    withdrawn = _certified(db, USER_A)
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=withdrawn.campaign_id, user_id=USER_A))
    clock.advance(seconds=1)
    rejected = finalize(db, withdrawn.proof_id)
    assert (rejected.state, rejected.reason, rejected.written) == (fz.REJECTED, proofs.NOT_AUTHORIZED, True)
    origin, op = opened(db, USER_B)
    pending = register(db, origin, op)
    settled = settle(db, origin, op, pending.proof_id)
    assert settled.state == proofs.INDETERMINATE
    snapshot = {p: proof_row(db, p) for p in (withdrawn.proof_id, pending.proof_id)}
    clock.advance(seconds=1)
    for proof_id, state, reason in ((withdrawn.proof_id, fz.REJECTED, proofs.NOT_AUTHORIZED),
                                    (pending.proof_id, fz.INDETERMINATE, proofs.TECHNICAL_UNCERTAINTY)):
        for function in (finalize, expire):
            decision = function(db, proof_id)
            assert (decision.state, decision.reason, decision.written, decision.event_id) == (state, reason, False,
                                                                                              None)
    assert {p: proof_row(db, p) for p in snapshot} == snapshot and _definitive(db) == (0, 0)


def _forge(db, proof_id, **columns):
    """Écrivain hors service (SQL direct) : seule façon d'obtenir une preuve
    certifiée que le bloc B ne produit jamais."""
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    with db.begin() as conn:
        conn.execute(sa.text(f"UPDATE {REGISTRY} SET {assignments} WHERE id = :proof_id"),
                     {**columns, "proof_id": proof_id})


@pytest.mark.parametrize("event_type, surface", [("coach_used", "coach"), ("education_used", "education")])
def test_a_certified_row_of_an_unattested_nature_never_becomes_an_event(db, clock, event_type, surface):
    """assistant_delivery est admise par le CHECK de 0024 mais aucune
    attestation F2 ne la produit : une ligne certified ne suffit pas."""
    proof = _certified(db)
    _forge(db, proof.proof_id, event_type=event_type, surface=surface, source_operation_kind="assistant_delivery")
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason, decision.written) == (fz.REJECTED, proofs.SOURCE_UNVERIFIED, True)
    _resolved_exactly(proof_row(db, proof.proof_id), "rejected", "source_unverified")
    assert _definitive(db) == (0, 0)


def test_an_incoherent_certified_proof_never_becomes_an_event(db, clock):
    # Certification à J+12 ou après (forgée) : jamais admissible.
    late = _certified(db, USER_A)
    end = _participation(db, late.campaign_id).window_end_at
    _forge(db, late.proof_id, certified_at=end)
    clock.set(end + timedelta(minutes=1))
    decision = finalize(db, late.proof_id)
    assert (decision.state, decision.reason) == (fz.REJECTED, proofs.WINDOW_EXPIRED)
    # Chronologie stockée incohérente (preuve antérieure à son interaction).
    early = _certified(db, USER_B)
    interaction = _interaction(db, early.origin)
    before = utc(interaction["recorded_at"]) - timedelta(seconds=1)
    _forge(db, early.proof_id, created_at=before, certified_at=before)
    decision = finalize(db, early.proof_id)
    assert (decision.state, decision.reason) == (fz.INDETERMINATE, proofs.TECHNICAL_UNCERTAINTY)
    _resolved_exactly(proof_row(db, early.proof_id), "indeterminate", "technical_uncertainty")
    assert _definitive(db) == (0, 0)


# --------------------------------------------------------------------------
# 3. Temps
# --------------------------------------------------------------------------

def test_an_activity_at_j0_is_recorded_at_j0(db, clock):
    j0 = clock.now
    proof = _certified(db)
    participation = _participation(db, proof.campaign_id)
    assert utc(participation.window_start_at) == j0 == utc(_interaction(db, proof.origin)["recorded_at"])
    decision = finalize(db, proof.proof_id)
    _, event, _ = _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio", finalized_at=j0)
    assert utc(event["occurred_at"]) == j0


def _last_minute(db, clock, offset=MICRO, *, user=USER_A, kind=CREATE):
    """Interaction, inscription et certification à window_end_at - offset."""
    campaign = open_wave(db, user)
    end = _participation(db, campaign, user).window_end_at
    clock.set(end - offset)
    proof = _certified(db, user, kind, campaign_id=campaign)
    return proof, end


def test_a_certification_at_j_plus_12_minus_one_microsecond_is_recorded(db, clock):
    proof, end = _last_minute(db, clock)
    decision = finalize(db, proof.proof_id)
    _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio", finalized_at=end - MICRO)


def test_a_certification_at_j_plus_12_is_refused_and_never_recorded(db, clock):
    campaign = open_wave(db, USER_A)
    end = _participation(db, campaign).window_end_at
    clock.set(end - timedelta(seconds=1))
    origin, op = opened(db, USER_A, campaign_id=campaign)
    pending = register(db, origin, op)
    clock.set(end)
    assert certify(db, origin, op, pending.proof_id).state == proofs.REJECTED  # bloc B : window_expired
    decision = finalize(db, pending.proof_id)
    assert (decision.state, decision.reason, decision.written) == (fz.REJECTED, proofs.WINDOW_EXPIRED, False)
    assert _definitive(db) == (0, 0)


def test_a_proof_certified_before_j_plus_12_is_recorded_during_the_margin(db, clock):
    proof, end = _last_minute(db, clock, timedelta(minutes=5))
    finalized_at = clock.set(end + timedelta(minutes=20))
    decision = finalize(db, proof.proof_id)
    _, event, _ = _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio",
                                    finalized_at=finalized_at)
    # Occurrence dans la fenêtre ; conservation jamais prolongée par la finalisation tardive.
    assert utc(event["occurred_at"]) == end - timedelta(minutes=5) < end < utc(event["recorded_at"])
    assert utc(event["purge_after_at"]) == end - timedelta(minutes=5) + DAYS_90


@pytest.mark.parametrize("offset, recorded", [(-MICRO, True), (timedelta(0), False), (MICRO, False),
                                              (timedelta(hours=2), False)])
def test_the_finalization_bound_is_strict(db, clock, offset, recorded):
    proof, end = _last_minute(db, clock)
    bound = end + GRACE
    # La visite d'origine a expiré par inactivité à end - 1 µs + 30 min, AVANT
    # bound - 1 µs : sans effet sur l'échéance (arbitrage R1).
    clock.set(bound + offset)
    decision = finalize(db, proof.proof_id)
    if recorded:
        _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio", finalized_at=bound + offset)
    else:
        assert decision == fz.FinalizationDecision(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED, True,
                                                   proof.proof_id, None, bound + offset)
        row = proof_row(db, proof.proof_id)
        _resolved_exactly(row, "indeterminate", "finalization_expired")
        assert _definitive(db) == (0, 0)


def _tight_campaign(db, clock, user=USER_A):
    """close_by_at = window_end_at : aucune marge après J+12."""
    def action(s):
        now = common.utcnow()
        campaign = pc.create_campaign(s, code=f"wave-0-{uuid.uuid4().hex[:10]}", name="Vague 0",
                                      purpose="Mesurer l'usage des surfaces Oryx.",
                                      close_by_at=now + timedelta(days=2), **POLICY)
        pc.activate_campaign(s, campaign_id=campaign.id)
        pp.enroll_participant(s, campaign_id=campaign.id, user_id=user, window_start_at=now,
                              window_end_at=now + timedelta(days=2), authorization_recorded_at=now,
                              notice_version=NOTICE)
        return campaign.id
    return committed(db, action)


@pytest.mark.parametrize("offset, recorded", [(-MICRO, True), (timedelta(0), False)])
def test_a_close_by_at_tighter_than_the_margin_wins(db, clock, offset, recorded):
    campaign = _tight_campaign(db, clock)
    end = _participation(db, campaign).window_end_at
    clock.set(end - timedelta(seconds=10))
    proof = _certified(db, campaign_id=campaign)
    clock.set(end + offset)  # seule close_by_at borne
    decision = finalize(db, proof.proof_id)
    assert decision.state == (fz.RECORDED if recorded else fz.INDETERMINATE)
    assert _definitive(db) == ((1, 1) if recorded else (0, 0))


def test_a_visit_closed_before_j_plus_12_does_not_shorten_the_margin(db, clock):
    """Arbitrage R1 : déconnexion avant J+12, preuve historiquement valide,
    finalisation après J+12 mais avant la borne : admissible."""
    proof, end = _last_minute(db, clock, timedelta(minutes=10))
    clock.set(end - timedelta(minutes=5))
    close(db, proof.campaign_id, USER_A, environment=LAPTOP)  # visite d'origine close (logout)
    finalized_at = clock.set(end + timedelta(minutes=1))
    decision = finalize(db, proof.proof_id)
    _, event, _ = _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio",
                                    finalized_at=finalized_at)
    # Aucune réouverture, aucune prolongation de la visite.
    [visit] = rows(db, "SELECT * FROM product_activity_sessions WHERE id = :v", v=proof.origin.visit_id)
    assert visit["finalized_at"] is not None and utc(visit["finalized_at"]) == end - timedelta(minutes=5)
    assert utc(event["occurred_at"]) == end - timedelta(minutes=10)


@pytest.mark.parametrize("offset, recorded", [(-MICRO, True), (timedelta(0), False)])
def test_a_visit_ended_by_inactivity_long_before_j_plus_12_does_not_shorten_the_margin(db, clock, offset,
                                                                                      recorded):
    proof, end = _last_minute(db, clock, timedelta(hours=2))  # visite expirée à end - 1 h 30
    visit_before = rows(db, "SELECT * FROM product_activity_sessions WHERE id = :v", v=proof.origin.visit_id)
    clock.set(end + GRACE + offset)
    decision = finalize(db, proof.proof_id)
    if recorded:
        _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio", finalized_at=end + GRACE - MICRO)
    else:
        assert (decision.state, decision.reason) == (fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
        assert _definitive(db) == (0, 0)
    assert rows(db, "SELECT * FROM product_activity_sessions WHERE id = :v", v=proof.origin.visit_id) == visit_before


def test_a_regressive_clock_writes_nothing_and_the_retry_is_clean(db, clock):
    proof = _certified(db)
    certified_at = utc(proof_row(db, proof.proof_id)["certified_at"])
    before = proof_row(db, proof.proof_id)
    clock.set(certified_at - MICRO)
    assert finalize(db, proof.proof_id) == fz.FinalizationDecision(fz.CERTIFIED, proofs.TECHNICAL_UNCERTAINTY,
                                                                   False, proof.proof_id)
    assert proof_row(db, proof.proof_id) == before and _definitive(db) == (0, 0)
    clock.set(certified_at)
    assert finalize(db, proof.proof_id).state == fz.RECORDED


@pytest.mark.parametrize("change", ["close", "cancel", "withdraw"])
def test_a_closure_cancellation_or_withdrawal_before_the_decision_refuses_and_never_after(db, clock, change):
    proof = _certified(db)
    recorded = _certified(db, USER_B)
    assert finalize(db, recorded.proof_id).state == fz.RECORDED
    if change == "withdraw":
        for proof_, user in ((proof, USER_A), (recorded, USER_B)):
            committed(db, lambda s, p=proof_, u=user: pp.withdraw_participant(s, campaign_id=p.campaign_id,
                                                                              user_id=u))
    else:
        transition = pc.close_campaign if change == "close" else pc.cancel_campaign
        for proof_ in (proof, recorded):
            committed(db, lambda s, p=proof_: transition(s, campaign_id=p.campaign_id))
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason, decision.written) == (fz.REJECTED, proofs.NOT_AUTHORIZED, True)
    _resolved_exactly(proof_row(db, proof.proof_id), "rejected", "not_authorized")
    # Une preuve déjà recorded n'est jamais réécrite du fait de la clôture ultérieure.
    snapshot = (proof_row(db, recorded.proof_id), _events(db), _attributions(db))
    again = finalize(db, recorded.proof_id)
    assert (again.state, again.reason, again.written) == (fz.RECORDED, None, False) and again.event_id is not None
    assert (proof_row(db, recorded.proof_id), _events(db), _attributions(db)) == snapshot


def _active_campaign_and_window(db, *, window, close_by=None, user=USER_A):
    """Campagne active ; participation dont la fenêtre se termine dans
    `window` (horloge réelle)."""
    def action(s):
        now = common.utcnow()
        campaign = pc.create_campaign(s, code=f"wave-0-{uuid.uuid4().hex[:10]}", name="Vague 0",
                                      purpose="Mesurer l'usage des surfaces Oryx.",
                                      close_by_at=now + (close_by or timedelta(days=30)), **POLICY)
        pc.activate_campaign(s, campaign_id=campaign.id)
        pp.enroll_participant(s, campaign_id=campaign.id, user_id=user,
                              window_start_at=now + timedelta(milliseconds=200), window_end_at=now + window,
                              authorization_recorded_at=now, notice_version=NOTICE)
        return campaign.id, now
    return committed(db, action)


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


def _finalize_while_participation_is_locked(db, proof, release_at):
    holder = Holder(db, sql="SELECT 1 FROM product_campaign_participants WHERE campaign_id = :c FOR UPDATE",
                    c=proof.campaign_id)
    contender = Background(lambda: finalize(db, proof.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la finalisation n'attend pas le verrou de participation")
        _sleep_until(release_at)
    finally:
        holder.rollback()
    return contender.outcome()


def test_the_postgresql_clock_is_read_after_a_lock_wait_that_crosses_j_plus_12(db):
    """Horloge PostgreSQL RÉELLE : certifiée avant J+12, la finalisation
    attend un verrou pendant que la fenêtre se termine ; elle est décidée
    APRÈS l'attente (dans la marge), jamais antidatée."""
    campaign, now = _active_campaign_and_window(db, window=timedelta(seconds=3))
    _sleep_until(now + timedelta(milliseconds=300))
    proof = _certified(db, campaign_id=campaign)
    end = now + timedelta(seconds=3)
    decision = _finalize_while_participation_is_locked(db, proof, end + timedelta(milliseconds=200))
    assert decision.state == fz.RECORDED and decision.resolved_at >= end
    [event] = _events(db)
    assert utc(event["recorded_at"]) >= end > utc(event["occurred_at"])


def test_the_postgresql_clock_is_read_after_a_lock_wait_that_crosses_the_finalization_bound(db):
    campaign, now = _active_campaign_and_window(db, window=timedelta(seconds=3), close_by=timedelta(seconds=3))
    _sleep_until(now + timedelta(milliseconds=300))
    proof = _certified(db, campaign_id=campaign)
    bound = now + timedelta(seconds=3)  # close_by_at = window_end_at
    decision = _finalize_while_participation_is_locked(db, proof, bound + timedelta(milliseconds=200))
    assert (decision.state, decision.reason) == (fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
    assert decision.resolved_at >= bound and _definitive(db) == (0, 0)


class _Clocks:
    """Horloge du bloc C : `first` pour la décision (après les verrous),
    `final` pour l'ultime lecture (après les écritures)."""

    def __init__(self, first, final):
        self.values, self.calls = [first, final], 0

    def __call__(self, *args):
        value = self.values[min(self.calls, 1)]
        self.calls += 1
        return value


@pytest.mark.parametrize("final_offset, expected", [(-MICRO, "recorded"), (timedelta(0), "expired"),
                                                    (timedelta(hours=1), "expired")])
def test_the_final_clock_inside_the_savepoint_decides_whether_the_writes_are_kept(db, clock, monkeypatch,
                                                                                  final_offset, expected):
    proof, end = _last_minute(db, clock)
    bound = end + GRACE
    first, final = bound - timedelta(seconds=1), bound + final_offset
    before = proof_row(db, proof.proof_id)
    clocks = _Clocks(first, final)
    monkeypatch.setattr(proofs, "server_clock", clocks)
    decision = finalize(db, proof.proof_id)
    assert clocks.calls == 2  # décision, puis ultime horloge dans le SAVEPOINT
    if expected == "recorded":
        _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio", finalized_at=first)
        return
    assert decision == fz.FinalizationDecision(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED, True, proof.proof_id,
                                               None, final)
    row = _untouched_certification(db, proof, before)
    _resolved_exactly(row, "indeterminate", "finalization_expired")
    assert utc(row["resolved_at"]) == final and _definitive(db) == (0, 0)  # jamais antidatée


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(hours=1)])
def test_an_expired_decision_is_taken_before_any_write_and_prevails_over_a_conflict(db, clock, monkeypatch, offset):
    """Échéance atteinte dès l'horloge de décision : résolution immédiate,
    AUCUNE écriture tentée (une seule lecture d'horloge), et l'expiration
    l'emporte sur un occupant existant de l'opération (aucun conflit relu)."""
    proof, end = _last_minute(db, clock)
    _historical_event(db, idempotency_key=proof.proof_id)  # occupant : la tentative d'écriture serait un conflit
    snapshot = _events(db)
    bound = end + GRACE
    clocks = _Clocks(bound + offset, bound + offset)
    monkeypatch.setattr(proofs, "server_clock", clocks)
    decision = finalize(db, proof.proof_id)
    assert clocks.calls == 1  # aucune ultime horloge : aucune écriture tentée
    assert decision == fz.FinalizationDecision(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED, True, proof.proof_id,
                                               None, bound + offset)
    assert _events(db) == snapshot and _attributions(db) == []


def test_a_final_clock_behind_the_decision_writes_nothing_and_the_retry_is_clean(db, clock, monkeypatch):
    proof = _certified(db)
    before = proof_row(db, proof.proof_id)
    first = clock.now + timedelta(seconds=1)
    monkeypatch.setattr(proofs, "server_clock", _Clocks(first, first - MICRO))
    assert finalize(db, proof.proof_id) == fz.FinalizationDecision(fz.CERTIFIED, proofs.TECHNICAL_UNCERTAINTY,
                                                                   False, proof.proof_id)
    assert proof_row(db, proof.proof_id) == before and _definitive(db) == (0, 0)
    monkeypatch.setattr(proofs, "server_clock", _Clocks(first, first))
    assert finalize(db, proof.proof_id).state == fz.RECORDED


def test_a_retention_expiring_before_the_final_clock_keeps_nothing(db, clock, monkeypatch):
    proof = _certified(db)
    occurred = utc(_interaction(db, proof.origin)["recorded_at"])
    first = clock.now + timedelta(seconds=1)
    # Instant fictif (défense) : conservation de 90 j échue à l'ultime horloge seulement.
    monkeypatch.setattr(fz, "finalization_deadline", lambda participation, campaign: occurred + DAYS_90 * 2)
    monkeypatch.setattr(proofs, "server_clock", _Clocks(first, occurred + DAYS_90))
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason, decision.resolved_at) == (fz.REJECTED, proofs.NOT_AUTHORIZED,
                                                                       occurred + DAYS_90)
    assert _definitive(db) == (0, 0)


# Attente SQL APRÈS l'horloge initiale : un écrivain hors service (qui
# contourne les verrous consultatifs) tient, sans committer, l'entrée d'index
# unique que l'INSERT de la finalisation doit écrire. Horloge PostgreSQL
# RÉELLE, aucun lock_timeout dans ces sessions de test.

COLLISIONS = ["canonical_identity", "idempotency_key"]


def _outside_collision(proof, collision):
    if collision == "canonical_identity":
        return {}  # même opération canonique, autre clé d'idempotence
    return {"idempotency_key": proof.proof_id, "operation_id": uuid.uuid4()}  # même clé, autre opération


def _unique_index_waits(db) -> int:
    """Finalisations qui attendent la fin d'une transaction tierce
    (transactionid) PENDANT l'INSERT de l'événement : attente sur l'index
    unique, jamais sur un verrou explicite."""
    return rows(db, "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = :a "
                    "AND wait_event_type = 'Lock' AND wait_event = 'transactionid' AND query LIKE :q",
                a=PROOF_APP, q=f"INSERT INTO {EVENTS}%")[0]["n"]


def _behind_an_outside_insert(db, monkeypatch, proof, collision, *, release_at, outcome, action=None):
    """La finalisation lit son horloge, puis attend l'index unique ;
    l'écrivain hors service committe ou annule à `release_at`. Rend
    (décision, horloges lues avant l'attente, toutes les horloges lues)."""
    clocks, real = [], proofs.server_clock

    def spy(session):
        value = real(session)
        clocks.append(utc(value))
        return value
    monkeypatch.setattr(proofs, "server_clock", spy)
    holder = Holder(db, lambda s: _outside_event(s, proof, **_outside_collision(proof, collision)))
    contender = Background(action or (lambda: finalize(db, proof.proof_id)))
    contender.start()
    try:
        _wait(lambda: _unique_index_waits(db) == 1, "la finalisation n'attend pas l'index unique")
        before_wait = list(clocks)
        _sleep_until(release_at)
    finally:
        holder.commit() if outcome == "commit" else holder.rollback()
    return contender.outcome(), before_wait, clocks


def _near_the_bound(db, *, seconds=3):
    """Preuve certifiée (horloge réelle) ; borne de finalisation = close_by_at
    = window_end_at, `seconds` après la création de la campagne."""
    campaign, now = _active_campaign_and_window(db, window=timedelta(seconds=seconds),
                                                close_by=timedelta(seconds=seconds))
    _sleep_until(now + timedelta(milliseconds=300))
    proof = _certified(db, campaign_id=campaign)
    return proof, now + timedelta(seconds=seconds)


def _untouched_certification(db, proof, before):
    """Aucun rejeu, aucune nouvelle certification, aucune interaction."""
    row = proof_row(db, proof.proof_id)
    assert (row["certified_at"], row["created_at"], row["interaction_key"], row["visit_id"]) == (
        before["certified_at"], before["created_at"], before["interaction_key"], before["visit_id"])
    assert len(rows(db, f"SELECT id FROM {REGISTRY}")) == 1
    assert len(rows(db, "SELECT interaction_key FROM product_activity_session_interactions")) == 1
    return row


@pytest.mark.parametrize("collision", COLLISIONS)
def test_an_outside_insert_rolled_back_after_the_bound_never_lets_a_stale_decision_record(db, monkeypatch,
                                                                                         collision):
    """Contre-épreuve R1 : décision initiale admissible, attente sur l'index
    unique qui franchit close_by_at, ROLLBACK de l'écrivain : l'INSERT
    réussirait ; l'ultime horloge (dans le SAVEPOINT) annule les trois
    écritures et la preuve est résolue à l'instant réel, jamais antidatée."""
    proof, bound = _near_the_bound(db)
    before = proof_row(db, proof.proof_id)
    decision, before_wait, clocks = _behind_an_outside_insert(
        db, monkeypatch, proof, collision, release_at=bound + timedelta(milliseconds=200), outcome="rollback")
    assert len(before_wait) == 1 and before_wait[0] < bound  # horloge initiale lue AVANT l'attente
    assert _definitive(db) == (0, 0)
    assert (decision.state, decision.reason, decision.written, decision.event_id) == (
        fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED, True, None)
    assert decision.resolved_at == clocks[-1] >= bound  # instant réel après l'attente
    row = _untouched_certification(db, proof, before)
    _resolved_exactly(row, "indeterminate", "finalization_expired")
    assert utc(row["resolved_at"]) == decision.resolved_at


@pytest.mark.parametrize("collision", COLLISIONS)
def test_an_outside_insert_rolled_back_before_the_bound_lets_the_finalization_record(db, monkeypatch, collision):
    proof, bound = _near_the_bound(db, seconds=4)
    before = proof_row(db, proof.proof_id)
    decision, before_wait, clocks = _behind_an_outside_insert(
        db, monkeypatch, proof, collision, release_at=bound - timedelta(seconds=2), outcome="rollback")
    assert decision.state == fz.RECORDED and decision.written
    # Instant de décision = horloge initiale (après les verrous explicites) ;
    # l'ultime horloge, après l'attente, est encore strictement avant la borne.
    assert decision.resolved_at == before_wait[0] < clocks[-1] < bound
    [event] = _events(db)
    assert utc(event["recorded_at"]) == decision.resolved_at and _definitive(db) == (1, 1)
    _untouched_certification(db, proof, before)


@pytest.mark.parametrize("collision", COLLISIONS)
def test_an_outside_insert_committed_during_the_wait_is_a_conflict_and_nothing_is_written(db, monkeypatch,
                                                                                       collision):
    proof, bound = _near_the_bound(db, seconds=4)
    before = proof_row(db, proof.proof_id)
    decision, _, _ = _behind_an_outside_insert(db, monkeypatch, proof, collision,
                                               release_at=bound - timedelta(seconds=2), outcome="commit")
    assert decision == fz.FinalizationDecision(fz.CERTIFIED, proofs.OPERATION_CONFLICT, False, proof.proof_id)
    assert proof_row(db, proof.proof_id) == before and len(_events(db)) == 1 and _attributions(db) == []


def test_the_margin_crossed_during_a_unique_index_wait_never_records(db, monkeypatch):
    """Même contre-épreuve, borne = window_end_at + marge (close_by_at
    lointain) : la marge est réduite à 1 s pour l'horloge réelle."""
    monkeypatch.setattr(fz, "FINALIZATION_GRACE", timedelta(seconds=1))
    campaign, now = _active_campaign_and_window(db, window=timedelta(seconds=2))
    _sleep_until(now + timedelta(milliseconds=300))
    proof = _certified(db, campaign_id=campaign)
    end = now + timedelta(seconds=2)
    _sleep_until(end + timedelta(milliseconds=100))  # après J+12, dans la marge : décision initiale admissible
    decision, before_wait, clocks = _behind_an_outside_insert(
        db, monkeypatch, proof, "canonical_identity", release_at=end + timedelta(seconds=1, milliseconds=200),
        outcome="rollback")
    assert end <= before_wait[0] < end + timedelta(seconds=1) <= clocks[-1]
    assert (decision.state, decision.reason) == (fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
    assert _definitive(db) == (0, 0)


def test_the_callers_transaction_stays_usable_after_a_stale_decision_is_rolled_back(db, monkeypatch):
    proof, bound = _near_the_bound(db)

    def action():
        with Session(bind=db, autoflush=False, expire_on_commit=False) as s:
            decision = fz.finalize_certified_proof(s, proof_id=proof.proof_id)
            assert s.in_transaction() and not s.in_nested_transaction()
            seen = s.execute(sa.text(f"SELECT status FROM {REGISTRY} WHERE id = :i"), {"i": proof.proof_id}).scalar()
            later = fz.finalize_certified_proof(s, proof_id=proof.proof_id)  # même transaction : relecture stable
            s.commit()
            return decision, seen, later
    (decision, seen, later), _, _ = _behind_an_outside_insert(
        db, monkeypatch, proof, "idempotency_key", release_at=bound + timedelta(milliseconds=200),
        outcome="rollback", action=action)
    assert decision.state == seen == later.state == fz.INDETERMINATE and not later.written
    assert proof_row(db, proof.proof_id)["status"] == "indeterminate" and _definitive(db) == (0, 0)


# --------------------------------------------------------------------------
# 4. Atomicité
# --------------------------------------------------------------------------

class _FailAfterFlush:
    """Panne APRÈS l'exécution SQL d'une écriture (after_flush), dans le
    SAVEPOINT : l'INSERT / UPDATE a réellement eu lieu côté serveur."""

    def __init__(self, predicate):
        self.predicate, self.fired = predicate, False

    def __call__(self, session, context):
        if not self.fired and any(self.predicate(o) for o in list(session.new) + list(session.dirty)):
            self.fired = True
            raise RuntimeError("panne injectée après l'écriture")


def _with_listener(listener, action):
    sa.event.listen(Session, "after_flush", listener)
    try:
        return action()
    finally:
        sa.event.remove(Session, "after_flush", listener)


def _intact(db, proof, before):
    assert proof_row(db, proof.proof_id) == before  # même xmin : aucune écriture committée
    assert _definitive(db) == (0, 0)


def _boom(*args, **kwargs):
    raise RuntimeError("panne injectée avant l'événement")


FAULTS = ["before_event", "after_event", "after_attribution", "after_proof", "final_flush_check",
          "attribution_fk", "proof_fk"]


@pytest.mark.parametrize("fault", FAULTS)
def test_a_failure_at_any_point_of_the_three_writes_leaves_nothing_and_the_retry_is_clean(db, clock, monkeypatch,
                                                                                         fault):
    proof = _certified(db, kind=ACK)
    before = proof_row(db, proof.proof_id)
    real_attribution, real_mark = fz._attribution, fz._mark_recorded
    expected = RuntimeError
    listener = None
    if fault == "before_event":
        monkeypatch.setattr(pe, "_stage_certified_event", _boom)
    elif fault == "after_event":
        listener = _FailAfterFlush(lambda o: isinstance(o, ProductActivityEvent))
    elif fault == "after_attribution":
        listener = _FailAfterFlush(lambda o: isinstance(o, ProductActivityEventAttribution))
    elif fault == "after_proof":
        listener = _FailAfterFlush(lambda o: isinstance(o, ProductActivityProof) and o.status == "recorded")
    elif fault == "final_flush_check":  # violation RÉELLE du CHECK 168 h pendant le flush final
        def bad_mark(p, event_id, now):
            real_mark(p, event_id, now)
            p.purge_after_at = now + timedelta(hours=167)
        monkeypatch.setattr(fz, "_mark_recorded", bad_mark)
        expected = IntegrityError
    elif fault == "attribution_fk":  # violation RÉELLE de la FK d'interaction après l'INSERT événement
        def bad_attribution(p, event):
            row = real_attribution(p, event)
            row.interaction_key = uuid.uuid4()
            return row
        monkeypatch.setattr(fz, "_attribution", bad_attribution)
        expected = IntegrityError
    elif fault == "proof_fk":  # violation RÉELLE de la FK preuve -> attribution
        def bad_mark(p, event_id, now):
            real_mark(p, uuid.uuid4(), now)
        monkeypatch.setattr(fz, "_mark_recorded", bad_mark)
        expected = IntegrityError
    clock.advance(seconds=1)
    with pytest.raises(expected):
        if listener is None:
            finalize(db, proof.proof_id)
        else:
            _with_listener(listener, lambda: finalize(db, proof.proof_id))
    _intact(db, proof, before)
    monkeypatch.undo()
    monkeypatch.setattr(common, "utcnow", clock)
    monkeypatch.setattr(proofs, "server_clock", clock)
    retried = finalize(db, proof.proof_id)
    _recorded_exactly(db, proof, retried, ACK, "decrypter_used", "decrypter", finalized_at=clock.now)


def test_the_callers_transaction_stays_usable_after_a_rolled_back_finalization(db, clock, monkeypatch):
    proof = _certified(db)
    real_attribution = fz._attribution

    def bad_attribution(p, event):
        row = real_attribution(p, event)
        row.visit_id = uuid.uuid4()
        return row
    monkeypatch.setattr(fz, "_attribution", bad_attribution)

    def action(session):
        with pytest.raises(IntegrityError):
            fz.finalize_certified_proof(session, proof_id=proof.proof_id)
        # SAVEPOINT annulé : la transaction de l'appelant continue, sans aucune des trois écritures.
        assert session.execute(sa.text(f"SELECT count(*) FROM {EVENTS}")).scalar_one() == 0
        assert session.execute(sa.text(f"SELECT status FROM {REGISTRY} WHERE id = :i"),
                               {"i": proof.proof_id}).scalar_one() == "certified"
        session.execute(sa.text("SELECT 1"))
        return "usable"
    assert committed(db, action) == "usable"
    assert proof_row(db, proof.proof_id)["status"] == "certified" and _definitive(db) == (0, 0)


def test_the_service_never_commits_a_successful_finalization_by_itself(db, clock):
    proof = _certified(db)
    with Session(bind=db, autoflush=False, expire_on_commit=False) as session:
        decision = fz.finalize_certified_proof(session, proof_id=proof.proof_id)
        assert decision.state == fz.RECORDED and decision.written
        session.rollback()  # l'appelant renonce : rien n'est durable
    assert proof_row(db, proof.proof_id)["status"] == "certified" and _definitive(db) == (0, 0)


# --------------------------------------------------------------------------
# 5. Idempotence, concurrence et conflits
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


def test_two_concurrent_finalizations_write_once_and_the_loser_reads_the_same_event(db):
    proof = _certified(db)
    won, lost = _contended(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id),
                           lambda: finalize(db, proof.proof_id))
    assert won.state == fz.RECORDED and won.written
    assert lost == fz.FinalizationDecision(fz.RECORDED, None, False, proof.proof_id, won.event_id, won.resolved_at)
    assert _definitive(db) == (1, 1)


def test_two_distinct_proofs_are_finalized_without_waiting_for_each_other(db):
    first, second = _certified(db, USER_A), _certified(db, USER_B, ACK)
    holder = Holder(db, lambda s: fz.finalize_certified_proof(s, proof_id=first.proof_id))
    try:
        other = finalize(db, second.proof_id)  # aucune attente
    finally:
        holder.commit()
    assert holder.value.state == other.state == fz.RECORDED and _definitive(db) == (2, 2)


def test_a_finalization_waits_for_a_concurrent_certification_and_records_it(db):
    origin, op = opened(db, USER_A)
    pending = register(db, origin, op)
    certified, decision = _contended(db, lambda s: proofs.certify_pending(s, origin=origin, operation=op,
                                                                          proof_id=pending.proof_id),
                                     lambda: finalize(db, pending.proof_id))
    assert certified.state == proofs.CERTIFIED and decision.state == fz.RECORDED and decision.written


def test_a_pending_resolution_and_a_certification_are_serialized_both_ways(db, clock):
    # Certification en cours, puis résolution : elle voit certified et ne la touche pas.
    origin, op = opened(db, USER_A)
    pending = register(db, origin, op)
    end = _participation(db, origin.campaign_id).window_end_at
    holder = Holder(db, lambda s: proofs.certify_pending(s, origin=origin, operation=op, proof_id=pending.proof_id))
    clock.set(end + GRACE)  # la résolution serait admise si la preuve restait pending
    contender = Background(lambda: expire(db, pending.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la résolution n'attend pas la certification")
    finally:
        holder.commit()
    assert contender.outcome() == fz.FinalizationDecision(fz.CERTIFIED, None, False, pending.proof_id)
    assert finalize(db, pending.proof_id).reason == proofs.FINALIZATION_EXPIRED  # marge close entre-temps
    # Résolution en cours, puis certification : elle voit indeterminate (terminal).
    origin2, op2 = opened(db, USER_B)
    clock.set(_participation(db, origin2.campaign_id, USER_B).window_start_at)
    pending2 = register(db, origin2, op2)
    clock.set(_participation(db, origin2.campaign_id, USER_B).window_end_at + GRACE)
    resolved, late = _contended(db, lambda s: fz.resolve_expired_pending_proof(s, proof_id=pending2.proof_id),
                                lambda: certify(db, origin2, op2, pending2.proof_id))
    assert resolved.state == fz.INDETERMINATE and (late.state, late.written) == (proofs.INDETERMINATE, False)
    assert _definitive(db) == (0, 0)


@pytest.mark.parametrize("change", ["close", "cancel", "withdraw"])
def test_a_finalization_waits_for_a_concurrent_closure_or_withdrawal_and_sees_it(db, change):
    proof = _certified(db)
    if change == "withdraw":
        def action(s):
            return pp.withdraw_participant(s, campaign_id=proof.campaign_id, user_id=USER_A)
    else:
        transition = pc.close_campaign if change == "close" else pc.cancel_campaign

        def action(s):
            return transition(s, campaign_id=proof.campaign_id)
    _, decision = _contended(db, action, lambda: finalize(db, proof.proof_id))
    assert (decision.state, decision.reason) == (fz.REJECTED, proofs.NOT_AUTHORIZED) and _definitive(db) == (0, 0)


@pytest.mark.parametrize("change", ["close", "withdraw"])
def test_a_closure_or_withdrawal_waits_for_a_finalization_in_progress(db, change):
    proof = _certified(db)
    if change == "withdraw":
        def action():
            return committed(db, lambda s: pp.withdraw_participant(s, campaign_id=proof.campaign_id,
                                                                   user_id=USER_A).participation_status)
    else:
        def action():
            return committed(db, lambda s: pc.close_campaign(s, campaign_id=proof.campaign_id).status)
    recorded, changed = _contended(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id), action)
    assert recorded.state == fz.RECORDED and changed in ("closed", "withdrawn")
    assert proof_row(db, proof.proof_id)["status"] == "recorded" and _definitive(db) == (1, 1)


def test_a_logout_waits_for_a_finalization_in_progress(db, clock):
    proof, end = _last_minute(db, clock, timedelta(minutes=5))
    clock.set(end + timedelta(minutes=2))
    recorded, _ = _contended(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id),
                             lambda: close(db, proof.campaign_id, USER_A, environment=LAPTOP))
    assert recorded.state == fz.RECORDED  # décidée avant la fin de visite, qui attendait son COMMIT


def test_a_logout_in_progress_after_j_plus_12_is_waited_for_and_does_not_shorten_the_margin(db, clock):
    """La visite d'origine est lue SOUS verrou (FOR SHARE) : une déconnexion
    en cours (visite FOR UPDATE, non committée) est attendue AVANT l'horloge
    (le contrôle de FK de visite de l'attribution n'attendra pas après) ; sa
    fin ne raccourcit pas l'échéance (arbitrage R1)."""
    proof, end = _last_minute(db, clock, timedelta(minutes=5))
    finalized_at = clock.set(end + timedelta(minutes=2))
    closing, decision = _contended(db, lambda s: ps.close_activity_session(
        s, campaign_id=proof.campaign_id, user_id=USER_A, environment_key=LAPTOP, interaction_key=uuid.uuid4()),
        lambda: finalize(db, proof.proof_id))
    assert closing.outcome == ps.CLOSED
    _recorded_exactly(db, proof, decision, CREATE, "portfolio_used", "portfolio", finalized_at=finalized_at)


def _historical_event(db, **overrides):
    """Événement du contrat 2A (vrai record_event)."""
    kwargs = {"user_id": USER_A, "visit_id": uuid.uuid4(), "idempotency_key": uuid.uuid4(),
              "event_type": "portfolio_used", "surface": "portfolio", "executed_route": None,
              "source": "client_signal", "source_operation_kind": None, "source_operation_id": None,
              "outcome": "succeeded", "delivery_status": "not_applicable", "occurred_at": common.utcnow(),
              "resolved_at": common.utcnow(), "delivered_at": None}
    kwargs.update(overrides)
    return committed(db, lambda s: pe.record_event(s, **kwargs)[0].id)


def test_an_occupied_idempotency_key_is_a_conflict_never_a_new_key(db, clock):
    proof = _certified(db)
    historical = _historical_event(db, idempotency_key=proof.proof_id)
    snapshot = _events(db)
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason, decision.written) == (fz.REJECTED, proofs.OPERATION_CONFLICT, True)
    assert _events(db) == snapshot and [e["id"] for e in snapshot] == [historical] and _attributions(db) == []


@pytest.mark.parametrize("kind, historical", [(CREATE, "portfolio_write"), (ACK, "assistant_delivery")])
def test_a_historical_2a_event_of_the_same_operation_is_never_doubled_nor_adopted(db, clock, kind, historical):
    proof = _certified(db, kind=kind)
    event_type = "decrypter_used" if kind == ACK else "portfolio_used"
    _historical_event(db, event_type=event_type, surface=event_type.split("_")[0], source="server_business",
                      executed_route="f2", source_operation_kind=historical,
                      source_operation_id=proof.op.operation_id)
    snapshot = _events(db)
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason) == (fz.REJECTED, proofs.OPERATION_CONFLICT)
    assert _events(db) == snapshot and _attributions(db) == []  # jamais rattaché ni réécrit


def test_the_2a_contract_never_doubles_an_operation_recorded_by_the_certified_path(db, clock):
    proof = _certified(db)
    assert finalize(db, proof.proof_id).state == fz.RECORDED
    snapshot = _events(db)
    with pytest.raises(common.EventUniquenessConflict) as caught:
        _historical_event(db, source="server_business", executed_route="f2", source_operation_kind="portfolio_write",
                          source_operation_id=proof.op.operation_id)
    assert caught.value.reason == "source_operation" and _events(db) == snapshot
    # Une autre opération reste enregistrable par le contrat 2A (aucun autre durcissement).
    _historical_event(db, source="server_business", executed_route="f2", source_operation_kind="portfolio_write",
                      source_operation_id=uuid.uuid4())
    assert len(_events(db)) == 2


def test_a_concurrent_2a_write_of_the_same_operation_waits_for_the_finalization_then_refuses(db, clock):
    proof = _certified(db)
    kwargs = {"source": "server_business", "executed_route": "f2", "source_operation_kind": "portfolio_write",
              "source_operation_id": proof.op.operation_id}

    def historical():
        try:
            return _historical_event(db, **kwargs)
        except common.EventUniquenessConflict as conflict:
            return conflict.reason
    recorded, late = _contended(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id), historical)
    assert recorded.state == fz.RECORDED and late == "source_operation" and _definitive(db) == (1, 1)


def test_the_user_lock_is_taken_before_the_clock(db):
    """Horloge PostgreSQL RÉELLE : une transaction tenant users FOR UPDATE
    (écriture Portfolio F2) retarde la finalisation AVANT sa décision ; le
    contrôle de FK de l'INSERT n'attend jamais après l'horloge."""
    proof = _certified(db)
    holder = Holder(db, sql="SELECT id FROM users WHERE id = :u FOR UPDATE", u=USER_A)
    contender = Background(lambda: finalize(db, proof.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la finalisation n'attend pas le verrou utilisateur")
        released = datetime.now(timezone.utc)
    finally:
        holder.commit()
    decision = contender.outcome()
    assert decision.state == fz.RECORDED and decision.resolved_at >= released


def test_the_visit_lock_is_taken_before_the_clock(db):
    """Horloge PostgreSQL RÉELLE : une transition 2B-1 en cours (visite FOR
    UPDATE) retarde la finalisation AVANT sa décision ; le contrôle de FK de
    visite de l'attribution n'attend jamais après l'horloge."""
    proof = _certified(db)
    holder = Holder(db, sql="SELECT id FROM product_activity_sessions WHERE id = :v FOR UPDATE",
                    v=proof.origin.visit_id)
    contender = Background(lambda: finalize(db, proof.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la finalisation n'attend pas le verrou de visite")
        [query] = [r["query"] for r in rows(db, "SELECT query FROM pg_stat_activity WHERE application_name = :a "
                                                "AND wait_event_type = 'Lock'", a=PROOF_APP)]
        assert "product_activity_sessions" in query and "FOR SHARE" in query  # attente du rang 4
        released = datetime.now(timezone.utc)
    finally:
        holder.commit()
    decision = contender.outcome()
    assert decision.state == fz.RECORDED and decision.resolved_at >= released


def _outside_event(conn, proof, *, visit_id=None, idempotency_key=None, operation_id=None):
    """Événement canonique écrit HORS service (SQL direct, sans verrou
    consultatif) : provenance non établie."""
    now = common.utcnow()
    conn.execute(sa.text(
        f"INSERT INTO {EVENTS} (id, user_id, visit_id, idempotency_key, event_type, surface, executed_route, source, "
        "source_operation_kind, source_operation_id, outcome, delivery_status, occurred_at, recorded_at, "
        "updated_at, resolved_at, delivered_at, retention_scopes, next_scope_expires_at, purge_after_at, "
        "schema_version) VALUES (:id, :u, :v, :k, 'portfolio_used', 'portfolio', 'f2', 'server_business', :o, :i, "
        "'succeeded', 'not_applicable', :t, :t, :t, :t, NULL, CAST(:doc AS jsonb), :x, :x, "
        "'product-activity-event-v1')"),
        {"id": uuid.uuid4(), "u": proof.op.owner_user_id, "v": visit_id or proof.origin.visit_id,
         "k": idempotency_key or uuid.uuid4(), "o": proof.op.operation_kind, "i": operation_id or proof.op.operation_id,
         "t": now,
         "x": now + DAYS_90, "doc": '{"version": "retention-scopes-v1", "scopes": [{"kind": "campaign", '
                                    f'"campaign_id": "{proof.campaign_id}", "policy_version": "v0-retention-v1", '
                                    f'"expires_at": "{(now + DAYS_90).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}"}}]}}'})


@pytest.mark.parametrize("case, reason", [("other_visit", proofs.VISIT_CONFLICT),
                                          ("same_visit", proofs.OPERATION_CONFLICT),
                                          ("partial_state", proofs.OPERATION_CONFLICT)])
def test_an_existing_canonical_event_of_unknown_provenance_is_refused_never_adopted(db, clock, case, reason):
    proof = _certified(db)
    with db.begin() as conn:
        _outside_event(conn, proof, visit_id=uuid.uuid4() if case == "other_visit" else None,
                       idempotency_key=proof.proof_id if case == "partial_state" else None)
    snapshot = _events(db)
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason, decision.written) == (fz.REJECTED, reason, True)
    assert _events(db) == snapshot and _attributions(db) == []  # aucune attribution, aucune réparation


def test_a_writer_bypassing_the_advisory_locks_loses_nothing_and_writes_nothing(db, clock):
    """Course perdue sur la UNIQUE canonique : SAVEPOINT annulé, aucune
    écriture, aucune décision sur l'état antérieur à l'attente ; la relecture
    suivante refuse."""
    proof = _certified(db)
    before = proof_row(db, proof.proof_id)
    holder = Holder(db, lambda s: _outside_event(s, proof))  # non committé, sans verrou consultatif
    contender = Background(lambda: finalize(db, proof.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "l'INSERT n'attend pas l'index unique")
    finally:
        holder.commit()
    assert contender.outcome() == fz.FinalizationDecision(fz.CERTIFIED, proofs.OPERATION_CONFLICT, False,
                                                          proof.proof_id)
    assert proof_row(db, proof.proof_id) == before and len(_events(db)) == 1 and _attributions(db) == []
    decision = finalize(db, proof.proof_id)
    assert (decision.state, decision.reason) == (fz.REJECTED, proofs.OPERATION_CONFLICT)


@pytest.mark.parametrize("corruption", [
    "UPDATE product_activity_event_attributions SET certified_at = certified_at + interval '1 microsecond'",
    "UPDATE product_activity_events SET occurred_at = occurred_at - interval '1 second'",
    "UPDATE product_activity_events SET delivery_status = 'confirmed', delivered_at = occurred_at",
    "UPDATE product_activity_events SET recorded_at = recorded_at + interval '1 second', "
    "updated_at = updated_at + interval '1 second'",
    "UPDATE product_activity_events SET outcome = 'indeterminate'",
])
def test_a_recorded_proof_with_a_discordant_event_or_attribution_is_never_confirmed_nor_repaired(db, clock,
                                                                                                corruption):
    proof = _certified(db)
    assert finalize(db, proof.proof_id).state == fz.RECORDED
    with db.begin() as conn:
        conn.execute(sa.text(corruption))
    snapshot = (proof_row(db, proof.proof_id), _events(db), _attributions(db))
    decision = finalize(db, proof.proof_id)
    assert decision == fz.FinalizationDecision(fz.RECORDED, fz.RECORD_INCOHERENT, False, proof.proof_id, None,
                                               snapshot[0]["resolved_at"])
    assert (proof_row(db, proof.proof_id), _events(db), _attributions(db)) == snapshot


# --------------------------------------------------------------------------
# 6. Conservation
# --------------------------------------------------------------------------

def test_the_three_retentions_are_exact(db, clock):
    proof, end = _last_minute(db, clock, timedelta(minutes=1), kind=UPDATE)
    finalized_at = clock.set(end + timedelta(minutes=10))
    row, event, attribution = _recorded_exactly(db, proof, finalize(db, proof.proof_id), UPDATE, "portfolio_used",
                                                "portfolio", finalized_at=finalized_at)
    occurred = utc(event["occurred_at"])
    # Événement : 90 jours depuis occurred_at (jamais depuis la finalisation, la certification ou la clôture).
    assert utc(event["purge_after_at"]) == occurred + DAYS_90 != finalized_at + DAYS_90
    # Preuve : 168 heures ABSOLUES depuis sa résolution ; attribution : aucune durée propre.
    assert row["purge_after_at"] - row["resolved_at"] == HOURS_168
    assert attribution["certified_at"] == row["certified_at"] and "purge_after_at" not in attribution


@pytest.mark.parametrize("zone", ["UTC", "Europe/Paris", "America/New_York"])
def test_the_registry_retention_is_absolute_across_daylight_saving_changes(db, clock, zone):
    """Résolution juste avant le passage à l'heure d'hiver (Europe : 25/10/2026,
    Amériques : 01/11/2026), sous le fuseau de session `zone` : 168 heures
    absolues acceptées par le CHECK de 0024, jamais 7 jours civils."""
    clock.set(datetime(2026, 10, 24, 23, tzinfo=timezone.utc) if zone != "America/New_York"
              else datetime(2026, 10, 31, 23, tzinfo=timezone.utc))
    proof = _certified(db)
    engine = sa.create_engine(db.url, poolclass=sa.pool.NullPool, connect_args={"options": f"-c TimeZone={zone}"})
    try:
        with Session(bind=engine, autoflush=False, expire_on_commit=False) as session:
            decision = fz.finalize_certified_proof(session, proof_id=proof.proof_id)
            session.commit()
    finally:
        engine.dispose()
    row = proof_row(db, proof.proof_id)
    assert decision.state == fz.RECORDED and utc(row["purge_after_at"]) == clock.now + HOURS_168


def test_the_event_and_its_attribution_survive_the_purge_of_the_resolved_proof(db, clock):
    proof = _certified(db)
    decision = finalize(db, proof.proof_id)
    event_before, attribution_before = _events(db), _attributions(db)
    clock.advance(hours=168)
    # Purge autorisée future (lot 2C, simulée en SQL) : la preuve d'abord (FK sans cascade).
    with db.begin() as conn:
        conn.execute(sa.text(f"DELETE FROM {REGISTRY} WHERE id = :i AND purge_after_at <= :n"),
                     {"i": proof.proof_id, "n": clock.now})
    assert proof_row(db, proof.proof_id) is None
    assert _events(db) == event_before and _attributions(db) == attribution_before  # intacts
    assert finalize(db, proof.proof_id) == fz.FinalizationDecision(fz.ABSENT)  # rien n'est recréé
    with pytest.raises(IntegrityError):  # l'événement ne part jamais avant son attribution
        with db.begin() as conn:
            conn.execute(sa.text(f"DELETE FROM {EVENTS} WHERE id = :i"), {"i": decision.event_id})
    with db.begin() as conn:
        conn.execute(sa.text(f"DELETE FROM {ATTRIBUTIONS} WHERE event_id = :i"), {"i": decision.event_id})
        conn.execute(sa.text(f"DELETE FROM {EVENTS} WHERE id = :i"), {"i": decision.event_id})
    assert _definitive(db) == (0, 0)


def test_a_reread_beyond_retention_returns_nothing(db, clock):
    proof = _certified(db)
    decision = finalize(db, proof.proof_id)
    row = proof_row(db, proof.proof_id)
    # Registre hors conservation (168 h) : aucune relecture.
    clock.set(utc(row["purge_after_at"]))
    assert finalize(db, proof.proof_id) == fz.FinalizationDecision(fz.RECORDED, fz.RETENTION_EXPIRED, False,
                                                                   proof.proof_id, None, decision.resolved_at)
    # Événement hors conservation (contrat 2A) : refus AVANT toute comparaison.
    clock.set(utc(row["purge_after_at"]) - timedelta(hours=1))
    expired = clock.now - timedelta(minutes=1)
    with db.begin() as conn:
        conn.execute(sa.text(f"UPDATE {EVENTS} SET next_scope_expires_at = :x, purge_after_at = :x, "
                             "retention_scopes = jsonb_set(retention_scopes, '{scopes,0,expires_at}', to_jsonb("
                             "CAST(:s AS text))) WHERE id = :i"),
                     {"x": expired, "s": expired.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "i": decision.event_id})
    snapshot = (proof_row(db, proof.proof_id), _events(db))
    assert finalize(db, proof.proof_id).reason == fz.RETENTION_EXPIRED
    assert (proof_row(db, proof.proof_id), _events(db)) == snapshot


# --------------------------------------------------------------------------
# 7. Non-contournement et dormance (inventaires)
# --------------------------------------------------------------------------

def _calls(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {(n.func.value.id if isinstance(n.func.value, ast.Name) else None, n.func.attr) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _constructed(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_only_the_service_writes_certified_events_and_attributions():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", ".git") or "node_modules" in relative.parts:
            continue
        source = path.read_text(encoding="utf-8")
        calls = _calls(path)
        # Le chemin certifié n'est appelé que par le service (jamais par la frontière B, F2 ou une route).
        for private in ("_stage_certified_event", "_certified_request", "_certified_lock_keys", "_certified_conflict"):
            if path == EVENT_LIFECYCLE_SERVICE and private == "_certified_request":
                continue  # exception nominative ci-dessus : comparaison, jamais écriture
            if path not in (SERVICE, PACKAGE / "events.py"):
                assert private not in source, (relative, private)
        constructed = _constructed(path)
        if "ProductActivityEvent" in constructed:
            assert path == PACKAGE / "events.py", relative  # seul constructeur de lignes d'événement
        if "ProductActivityEventAttribution" in constructed:
            assert path == SERVICE, relative  # seul constructeur d'attributions
        if path not in (SERVICE, BOUNDARY, LIFECYCLE_BOUNDARY):
            assert "finalize_certified_proof" not in source and "resolve_expired_pending_proof" not in source, relative
            assert ("finalization", "finalize_certified_proof") not in calls
    service_calls = _calls(SERVICE)
    assert ("events", "_stage_certified_event") in service_calls and ("events", "record_event") not in service_calls
    lifecycle_calls = _calls(EVENT_LIFECYCLE_SERVICE)
    assert ("events", "_certified_request") in lifecycle_calls
    assert not {call for call in lifecycle_calls if call[0] == "events"} - {("events", "_certified_request"),
                                                                           ("events", "_identity_locks")}


def test_the_service_reads_no_f2_table_and_owns_no_transaction():
    source = SERVICE.read_text(encoding="utf-8")
    for marker in ("oryx_sessions", "OryxSession", "authz_f2", "auth_f2", "Invitation", "AuthOperation",
                   "portfolio_write_idempotency", "assistant_deliveries", "_run_attested", "sa.text", "Thread",
                   "os.environ", "getenv", "import logging", "print("):
        assert marker not in source, marker
    assert not {attr for _, attr in _calls(SERVICE)} & {"commit", "rollback", "close", "delete", "begin"}
    tree = ast.parse(source)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "core.models"
                for a in n.names}
    assert imported == {"ProductActivityEvent", "ProductActivityEventAttribution", "ProductActivityProof",
                        "ProductActivitySession", "ProductActivitySessionInteraction", "ProductCampaignParticipant",
                        "User"}
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "begin_nested"].__len__() == 2  # trois écritures : UN SAVEPOINT ; résolution : un
