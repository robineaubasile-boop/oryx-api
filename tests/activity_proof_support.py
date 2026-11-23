"""Outils des tests V0-T08 Lot 2B-2 bloc B (certification après COMMIT des
preuves d'utilisation significative).

Campagnes, participations, visites et interactions sont produites par les
VRAIS services 2A / 2B-1 (aucune ligne fabriquée, sauf scénarios explicites
d'écrivain hors service) ; chaque étape est committée dans sa propre
transaction, comme le ferait le futur raccordement.
"""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import participation as pp
from core.product_analytics import proofs
from core.product_analytics import sessions as ps

POLICY = {"retention_days": 90, "retention_policy_version": "v0-retention-v1",
          "measurement_policy_version": "v0-measurement-v1"}
NOTICE = "v0-notice-1"
TWELVE_DAYS = timedelta(days=12)
LAPTOP = uuid.UUID("0b8f0e8e-0000-4000-8000-0000000000a1")
PHONE = uuid.UUID("0b8f0e8e-0000-4000-8000-0000000000a2")
PROOF_APP = "v0-proof-tests"
REGISTRY = "product_activity_proof_registry"
ATTRIBUTIONS = "product_activity_event_attributions"
EVENTS = "product_activity_events"
CREATE, UPDATE, DELETE, ACK = ("portfolio_position_create", "portfolio_position_update", "portfolio_position_delete",
                               "decryptage_delivery_ack")


class Clock:
    """Horloge déterministe partagée par les services 2A / 2B-1
    (common.utcnow) ET le registre (proofs.server_clock)."""

    def __init__(self, now):
        self.now = now

    def __call__(self, *args):
        return self.now

    def set(self, value):
        self.now = value
        return value

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


def analytics_engine(pg_url):
    return sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": PROOF_APP})


def committed(engine, action):
    with Session(bind=engine, autoflush=False, expire_on_commit=False) as db:
        result = action(db)
        db.commit()
        return result


def open_wave(engine, user_id, *, close_days=30) -> uuid.UUID:
    """Campagne active sans cohorte et J0 de `user_id` maintenant (vrais
    services)."""
    def action(db):
        now = common.utcnow()
        campaign = pc.create_campaign(db, code=f"wave-0-{uuid.uuid4().hex[:10]}", name="Vague 0",
                                      purpose="Mesurer l'usage des surfaces Oryx.",
                                      close_by_at=now + timedelta(days=close_days), **POLICY)
        pc.activate_campaign(db, campaign_id=campaign.id)
        pp.enroll_at_first_eligible_login(db, campaign_id=campaign.id, user_id=user_id,
                                          authorization_recorded_at=now, notice_version=NOTICE)
        return campaign.id
    return committed(engine, action)


def touch(engine, campaign_id, user_id, *, environment=LAPTOP, key=None) -> tuple:
    """Interaction 'activity' acceptée et committée : (visit_id, interaction_key)."""
    key = key or uuid.uuid4()

    def action(db):
        return ps.touch_activity_session(db, campaign_id=campaign_id, user_id=user_id, environment_key=environment,
                                         interaction_key=key).session.id
    return committed(engine, action), key


def close(engine, campaign_id, user_id, *, environment=LAPTOP, key=None) -> uuid.UUID:
    key = key or uuid.uuid4()
    committed(engine, lambda db: ps.close_activity_session(db, campaign_id=campaign_id, user_id=user_id,
                                                           environment_key=environment, interaction_key=key))
    return key


def observe(engine, campaign_id, user_id, visit_id, interaction_key) -> proofs.OriginSnapshot:
    return committed(engine, lambda db: proofs.read_origin(db, campaign_id=campaign_id, user_id=user_id,
                                                           visit_id=visit_id, interaction_key=interaction_key))


def origin_of(engine, user_id, *, environment=LAPTOP, campaign_id=None) -> proofs.OriginSnapshot:
    campaign_id = campaign_id or open_wave(engine, user_id)
    visit_id, key = touch(engine, campaign_id, user_id, environment=environment)
    return observe(engine, campaign_id, user_id, visit_id, key)


def operation(user_id, kind=CREATE, *, operation_id=None, new_effect=True) -> proofs.CommittedOperation:
    return proofs.CommittedOperation(user_id, kind, operation_id or uuid.uuid4(), new_effect)


def register(engine, origin, op):
    return committed(engine, lambda db: proofs.register_pending(db, origin=origin, operation=op))


def certify(engine, origin, op, proof_id):
    return committed(engine, lambda db: proofs.certify_pending(db, origin=origin, operation=op, proof_id=proof_id))


def settle(engine, origin, op, proof_id):
    return committed(engine, lambda db: proofs.settle(db, origin=origin, operation=op, proof_id=proof_id))


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]


def registry(engine, user_id=None):
    where, params = ("WHERE user_id = :u", {"u": user_id}) if user_id else ("", {})
    return rows(engine, f"SELECT *, xmin::text AS xmin FROM {REGISTRY} {where} ORDER BY created_at, id", **params)


def proof_row(engine, proof_id):
    found = rows(engine, f"SELECT *, xmin::text AS xmin FROM {REGISTRY} WHERE id = :i", i=proof_id)
    return found[0] if found else None


def definitive_rows(engine) -> tuple:
    """(événements, attributions) : le bloc B n'en écrit jamais."""
    return tuple(rows(engine, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in (EVENTS, ATTRIBUTIONS))


def waiting(engine, application_name=PROOF_APP) -> int:
    return rows(engine, "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = :a "
                        "AND wait_event_type = 'Lock'", a=application_name)[0]["n"]


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


class Holder:
    """Transaction tierce qui garde ses verrous jusqu'à commit() / rollback()."""

    def __init__(self, engine, action=None, sql=None, **params):
        self.db = Session(bind=engine, autoflush=False, expire_on_commit=False)
        self.value = action(self.db) if action is not None else None
        if sql is not None:
            self.db.execute(sa.text(sql), params)

    def commit(self):
        self.db.commit()
        self.db.close()

    def rollback(self):
        self.db.rollback()
        self.db.close()


class Background(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn = fn
        self.result = self.error = None

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 — rapporté au test
            self.error = exc

    def outcome(self, timeout=30):
        self.join(timeout)
        assert not self.is_alive(), "acteur bloqué"
        if self.error is not None:
            raise self.error
        return self.result
