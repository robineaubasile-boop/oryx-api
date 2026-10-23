"""Corrections de l'audit de la PR #222 (R1 PR 2), sur PostgreSQL réel.

P1 rotation K_abuse : entrelacement déterministe exact
  1. T1 lit l'état de rotation v1 (sous verrou FOR SHARE) ;
  2. T1 est suspendue ;
  3. une rotation vers v2 est lancée ;
  4. T2 tente de réserver une émission (qui serait enregistrée sous v2) ;
  5. T1 reprend.
  La rotation et T2 attendent T1 : le quota n'est jamais dépassé. Un
  témoin rétablit l'ancienne lecture sans verrou et montre que ce même
  entrelacement dépassait le quota (le test détecte bien le défaut).
  Rotations concurrentes : une seule réussit, aucun interblocage.

P1 expirations : sur deux connexions indépendantes, une connexion retient
  un verrou avant l'échéance, l'échéance passe, le verrou est libéré :
  l'autorisation est obligatoirement refusée (heure lue APRÈS le verrou).
  Une variante utilise l'horloge réelle de PostgreSQL (clock_timestamp()),
  pour montrer qu'aucun instant de début de transaction n'est utilisé.
"""
import threading
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.abuse import AbuseUnavailable
from core.auth_f2.service import F2AuthService
from tests.f2_support import FakeMailer, FakeSupabase, create_profile_with_identity, make_keyring, rows
from tests.test_f2_service import ALICE, BOB, IP1, Env, schema  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

PURPOSES = ("candidate", "derive", "active", "otp", "invitation", "abuse")
IP = "198.51.100.77"


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = Env(pg_url)
    yield environment
    environment.dispose()


@pytest.fixture
def patient_locks(monkeypatch):
    """Entrelacements déterministes : la suspension de T1 est contrôlée par
    le test ; la rotation et T2 doivent attendre aussi longtemps qu'elle
    dure. En production, lock_timeout (5 s) borne cette attente : voir
    test_rotation_blocked_beyond_lock_timeout_fails_closed."""
    monkeypatch.setattr("core.auth_f2.txn.LOCK_TIMEOUT", "120s")


def both_versions():
    versions = {p: ("t1",) for p in PURPOSES}
    versions["abuse"] = ("v1", "v2")
    return make_keyring(versions, {**{p: "t1" for p in PURPOSES}, "abuse": "v1"})


def wait_for_waiters(engine, count, timeout=20.0):
    """pg_stat_activity est un instantané pris au premier accès DANS une
    transaction : chaque relevé se fait dans sa propre transaction (sinon
    une attente apparue après le premier relevé n'est jamais vue)."""
    deadline = time.monotonic() + timeout
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'")).scalar_one()
            probe.rollback()
            if waiting >= count:
                return
            time.sleep(0.02)
    raise AssertionError(f"{count} transaction(s) en attente de verrou attendue(s)")


class Runner(threading.Thread):
    def __init__(self, target, done_order, name):
        super().__init__(daemon=True)
        self._target_fn, self.done_order, self.label = target, done_order, name
        self.result = self.error = None

    def run(self):
        try:
            self.result = self._target_fn()
        except BaseException as exc:  # noqa: BLE001 — rapporté au test
            self.error = exc
        finally:
            self.done_order.append(self.label)


def suspend_first_state_lock():
    """Point d'interruption : suspend la PREMIÈRE transaction de décision
    juste après la lecture verrouillée de l'état de clé."""
    locked, resume = threading.Event(), threading.Event()
    seen = []

    def hook(point):
        if point == "abuse.state_locked" and not seen:
            seen.append(point)
            locked.set()
            assert resume.wait(30)

    return hook, locked, resume


def _seed_ip_emissions(svc, count):
    for i in range(count):
        assert svc.start_sign_in(email=f"seed{i}@example.com", client_ip=IP).status == "issued"


def _interleave_rotation(env, keyring):
    """Rejoue l'entrelacement 1 à 5 ; rend (T1, rotation, T2, ordre de fin)."""
    probe_engine = env.observer
    hook, t1_locked, t1_resume = suspend_first_state_lock()
    t1_service = F2AuthService(env.engine, keyring, FakeSupabase(), FakeMailer(), fault_hook=hook)
    t2_service = F2AuthService(env.engine, keyring, FakeSupabase(), FakeMailer())
    done = []
    t1 = Runner(lambda: t1_service.start_sign_in(email="t1@example.com", client_ip=IP), done, "T1")
    rotation = Runner(lambda: abuse.rotate_abuse_key(env.engine, keyring, "v2"), done, "rotation")
    t2 = Runner(lambda: t2_service.start_sign_in(email="t2@example.com", client_ip=IP), done, "T2")
    t1.start()
    assert t1_locked.wait(20)                       # 1-2 : T1 a lu v1 et est suspendue
    rotation.start()                                # 3
    return t1, rotation, t2, t1_resume, done, probe_engine


def test_rotation_cannot_change_versions_during_a_decision_and_quota_holds(env, patient_locks):
    keyring = both_versions()
    svc = env.service(keyring=keyring, init_abuse=False)
    abuse.initialize_abuse_key_state(env.engine, keyring, "v1")
    _seed_ip_emissions(svc, 19)                     # quota IP : 20 / h
    t1, rotation, t2, t1_resume, done, probe = _interleave_rotation(env, keyring)
    wait_for_waiters(probe, 1)                      # la rotation attend T1
    t2.start()                                      # 4
    wait_for_waiters(probe, 2)                      # T2 attend aussi
    time.sleep(0.3)
    assert done == [] and rotation.is_alive() and t2.is_alive()
    t1_resume.set()                                 # 5
    for thread in (t1, rotation, t2):
        thread.join(30)
    assert (t1.error, rotation.error, t2.error) == (None, None, None)
    # La décision de T1 est committée avant la rotation (le fil T1 se
    # termine plus tard : appels fournisseur hors transaction) ; T2 décide
    # après la rotation.
    assert done.index("rotation") < done.index("T2")
    t1_reserved = rows(env.observer, "SELECT r.requested_at FROM auth_otp_requests r JOIN auth_operations o "
                                     "ON o.id = r.auth_operation_id WHERE o.email_normalized = 't1@example.com'")
    demoted = rows(env.observer, "SELECT previous_demoted_at FROM auth_abuse_key_state")[0]["previous_demoted_at"]
    assert t1_reserved[0]["requested_at"] < demoted
    assert t1.result.status == "issued"
    assert (t2.result.status, t2.result.reason) == ("refused", "quota_ip_issue")
    per_version = rows(env.observer, "SELECT s.hash_key_version AS v, count(*) AS n FROM auth_abuse_events e JOIN "
                                     "auth_abuse_subjects s ON s.id = e.subject_id WHERE s.scope_kind = 'ip' "
                                     "AND e.event_kind = 'otp_request' GROUP BY 1 ORDER BY 1")
    assert [(r["v"], r["n"]) for r in per_version] == [("v1", 20)]
    state = rows(env.observer, "SELECT * FROM auth_abuse_key_state")[0]
    assert (state["current_version"], state["previous_version"]) == ("v2", "v1")
    # Après la rotation, le quota reste celui des deux versions.
    late = F2AuthService(env.engine, keyring, FakeSupabase(), FakeMailer())
    assert late.start_sign_in(email="t3@example.com", client_ip=IP).reason == "quota_ip_issue"


def test_control_the_old_unlocked_read_let_the_same_interleaving_exceed_the_quota(env, monkeypatch, patient_locks):
    """Témoin : avec une lecture SANS verrou (comportement audité), la
    rotation et T2 passent pendant la suspension de T1, qui ne compte
    ensuite que v1 : 21 émissions pour un quota de 20. Le test ci-dessus
    détecte donc bien le défaut corrigé."""
    keyring = both_versions()
    svc = env.service(keyring=keyring, init_abuse=False)
    abuse.initialize_abuse_key_state(env.engine, keyring, "v1")
    _seed_ip_emissions(svc, 19)
    monkeypatch.setattr(abuse, "lock_key_state", abuse.load_key_state)
    t1, rotation, t2, t1_resume, done, _ = _interleave_rotation(env, keyring)
    rotation.join(20)
    assert done == ["rotation"]
    t2.start()
    t2.join(20)
    assert t2.result.status == "issued"             # 4 : T2 réserve sous v2
    t1_resume.set()                                 # 5
    t1.join(30)
    assert t1.result.status == "issued"             # T1 ne voit pas l'événement v2
    total = rows(env.observer, "SELECT count(*) AS n FROM auth_abuse_events e JOIN auth_abuse_subjects s "
                               "ON s.id = e.subject_id WHERE s.scope_kind = 'ip' AND e.event_kind = 'otp_request'")
    assert total[0]["n"] == 21                      # dépassement reproduit sans le correctif


def test_key_state_lock_key_is_the_documented_constant():
    import hashlib
    expected = int.from_bytes(hashlib.sha256(b"oryx:f2:auth_abuse_key_state").digest()[:8], "big", signed=True)
    assert abuse.KEY_STATE_LOCK_KEY == expected == -8112447256766139957


def test_concurrent_rotations_one_wins_without_deadlock(env, patient_locks):
    versions = {p: ("t1",) for p in PURPOSES}
    versions["abuse"] = ("v1", "v2", "v3")
    keyring = make_keyring(versions, {**{p: "t1" for p in PURPOSES}, "abuse": "v1"})
    env.service(keyring=keyring, init_abuse=False)
    abuse.initialize_abuse_key_state(env.engine, keyring, "v1")
    assert abuse.initialize_abuse_key_state(env.engine, keyring, "v1") is False  # idempotent
    hook, locked, resume = suspend_first_state_lock()
    holder = F2AuthService(env.engine, keyring, FakeSupabase(), FakeMailer(), fault_hook=hook)
    done = []
    decision = Runner(lambda: holder.start_sign_in(email=ALICE, client_ip=IP1), done, "decision")
    decision.start()
    assert locked.wait(20)
    rotations = [Runner(lambda v=v: abuse.rotate_abuse_key(env.engine, keyring, v), done, f"rotate-{v}")
                 for v in ("v2", "v3")]
    for r in rotations:
        r.start()
    wait_for_waiters(env.observer, 2)
    resume.set()
    for thread in [decision, *rotations]:
        thread.join(30)
    assert decision.error is None and decision.result.status == "issued"
    errors = [r.error for r in rotations]
    assert sum(e is None for e in errors) == 1
    assert all(isinstance(e, AbuseUnavailable) and "précédente" in str(e) for e in errors if e is not None)
    state = rows(env.observer, "SELECT * FROM auth_abuse_key_state")[0]
    assert state["previous_version"] == "v1" and state["current_version"] in ("v2", "v3")


def test_rotation_blocked_beyond_lock_timeout_fails_closed(env, monkeypatch):
    """Une rotation qui attend une décision plus longtemps que lock_timeout
    échoue (à relancer) sans rien modifier ; la décision n'est pas gênée."""
    monkeypatch.setattr("core.auth_f2.txn.LOCK_TIMEOUT", "300ms")
    keyring = both_versions()
    env.service(keyring=keyring, init_abuse=False)
    abuse.initialize_abuse_key_state(env.engine, keyring, "v1")
    hook, locked, resume = suspend_first_state_lock()
    holder = F2AuthService(env.engine, keyring, FakeSupabase(), FakeMailer(), fault_hook=hook)
    decision = Runner(lambda: holder.start_sign_in(email=ALICE, client_ip=IP1), [], "decision")
    decision.start()
    assert locked.wait(20)
    with pytest.raises(sa.exc.OperationalError, match="lock timeout"):
        abuse.rotate_abuse_key(env.engine, keyring, "v2")
    resume.set()
    decision.join(30)
    assert decision.error is None and decision.result.status == "issued"
    state = rows(env.observer, "SELECT * FROM auth_abuse_key_state")[0]
    assert (state["current_version"], state["previous_version"]) == ("v1", None)
    abuse.rotate_abuse_key(env.engine, keyring, "v2")  # relancée hors décision : aboutit
    assert rows(env.observer, "SELECT current_version FROM auth_abuse_key_state")[0]["current_version"] == "v2"


# --------------------------------------------------------------------------
# P1 — échéances jugées après les verrous (deux connexions indépendantes)
# --------------------------------------------------------------------------

def _hold(engine, sql, **params):
    """Connexion indépendante qui retient un verrou jusqu'à release()."""
    conn = engine.connect()
    trans = conn.begin()
    conn.execute(sa.text(sql), params)

    def release():
        trans.rollback()
        conn.close()
    return release


def _authenticated(env):
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    done = env.svc.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                  code=env.mailer.last_code(ALICE), client_ip=IP1)
    assert done.status == "authenticated"
    return start, done


def test_authenticate_session_rechecks_expiry_after_waiting_for_the_lock(env):
    env.service(fixed_clock=True)
    _, done = _authenticated(env)
    expires = done.session.absolute_expires_at
    env.clock.now = expires - timedelta(seconds=1)
    release = _hold(env.observer, "SELECT 1 FROM oryx_sessions WHERE id = :s FOR UPDATE", s=done.session.session_id)
    waiter = Runner(lambda: env.svc.authenticate_session(done.session.credential()), [], "auth")
    waiter.start()
    wait_for_waiters(env.observer, 1)
    env.clock.now = expires                         # l'échéance passe pendant l'attente
    release()
    waiter.join(20)
    assert waiter.error is None and waiter.result is None
    # Témoin : libéré avant l'échéance, le même parcours autorise.
    env.clock.now = expires - timedelta(seconds=1)
    assert env.svc.authenticate_session(done.session.credential()) is not None


def test_authenticate_session_uses_the_real_postgresql_clock_after_the_wait(env):
    """Horloge réelle : la transaction commence AVANT l'échéance (son
    now() serait encore valide), attend le verrou au-delà de l'échéance,
    et doit refuser : l'heure de décision est clock_timestamp() lu après
    le verrou."""
    env.service()
    _, done = _authenticated(env)
    with env.observer.begin() as c:
        c.execute(sa.text(
            "UPDATE oryx_sessions SET created_at = t.e - interval '15 days', absolute_expires_at = t.e "
            "FROM (SELECT clock_timestamp() + interval '2 seconds' AS e) AS t WHERE id = :s"),
            {"s": done.session.session_id})
        c.execute(sa.text("UPDATE oryx_session_generations SET issued_at = issued_at - interval '15 days' "
                          "WHERE id = :g"), {"g": done.session.generation_id})
    assert env.svc.authenticate_session(done.session.credential()) is not None
    release = _hold(env.observer, "SELECT 1 FROM oryx_sessions WHERE id = :s FOR UPDATE", s=done.session.session_id)
    waiter = Runner(lambda: env.svc.authenticate_session(done.session.credential()), [], "auth")
    waiter.start()
    wait_for_waiters(env.observer, 1)
    time.sleep(2.5)
    release()
    waiter.join(20)
    assert waiter.error is None and waiter.result is None


def test_resume_rechecks_the_resume_window_after_waiting_for_the_lock(env):
    env.service(fixed_clock=True)
    start, done = _authenticated(env)
    resume_until = rows(env.observer, "SELECT resume_until FROM auth_operation_results")[0]["resume_until"]
    env.clock.now = resume_until - timedelta(seconds=1)
    release = _hold(env.observer, "SELECT 1 FROM oryx_sessions WHERE id = :s FOR UPDATE", s=done.session.session_id)
    waiter = Runner(lambda: env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate), [],
                    "resume")
    waiter.start()
    wait_for_waiters(env.observer, 1)
    env.clock.now = resume_until
    release()
    waiter.join(20)
    assert waiter.error is None and (waiter.result.status, waiter.result.reason) == ("rejected", "resume_unavailable")
    env.clock.now = resume_until - timedelta(seconds=1)
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate).status == "resumed"


def test_verification_admission_rechecks_expiry_after_waiting_for_the_lock(env):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    code = env.mailer.last_code(ALICE)
    op = rows(env.observer, "SELECT * FROM auth_operations")[0]
    env.clock.now = op["expires_at"] - timedelta(seconds=1)
    release = _hold(env.observer, "SELECT 1 FROM auth_operations WHERE id = :o FOR UPDATE", o=start.operation_id)
    waiter = Runner(lambda: env.svc.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                   code=code, client_ip=IP1), [], "verify")
    waiter.start()
    wait_for_waiters(env.observer, 1)
    env.clock.now = op["expires_at"]
    release()
    waiter.join(20)
    assert waiter.error is None and waiter.result.reason == "operation_expired"
    assert env.supabase.verify_calls == [] and env.count("oryx_sessions") == 0


def test_linking_finalization_rechecks_expiry_after_waiting_for_the_session_lock(env):
    env.service(fixed_clock=True)
    created = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=created.token)
    session = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session
    linking = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP1)
    request = rows(env.observer, "SELECT * FROM auth_otp_requests")[0]
    ready, locked = threading.Event(), threading.Event()
    held = {}

    def after_provider():
        ready.set()
        assert locked.wait(20)

    env.hooks["verify.provider_returned"] = after_provider
    waiter = Runner(lambda: env.svc.verify_email_linking(operation_id=linking.operation_id,
                                                         session=session.credential(),
                                                         code=env.mailer.last_code(BOB), client_ip=IP1), [], "link")
    env.clock.now = request["expires_at"] - timedelta(seconds=1)
    waiter.start()
    assert ready.wait(20)
    held["release"] = _hold(env.observer, "SELECT 1 FROM oryx_sessions WHERE id = :s FOR UPDATE",
                            s=session.session_id)
    locked.set()
    wait_for_waiters(env.observer, 1)
    env.clock.now = request["expires_at"]           # code expiré pendant l'attente
    held["release"]()
    waiter.join(20)
    assert waiter.error is None
    assert (waiter.result.status, waiter.result.reason) == ("rejected", "proof_not_applicable")
    assert env.count("auth_identities") == 0        # aucune identité créée sans décision valide
    assert rows(env.observer, "SELECT status FROM auth_otp_requests")[0]["status"] == "expired"
    assert rows(env.observer, "SELECT state FROM auth_otp_verification_attempts")[0]["state"] == "abandoned"


def test_concurrent_identity_insert_wait_is_followed_by_a_fresh_expiry_check(env):
    """La liaison peut attendre l'insertion concurrente d'une identité
    (ON CONFLICT) : cette attente précède la lecture de l'heure, et
    l'identité créée dans le point de sauvegarde est annulée si la
    décision n'est plus valide."""
    env.service(fixed_clock=True)
    created = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=created.token)
    session = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session
    linking = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP1)
    request = rows(env.observer, "SELECT * FROM auth_otp_requests")[0]
    subject = env.supabase.subject_for(BOB)
    other_user = "competing-profile"
    with env.observer.begin() as c:
        c.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": other_user})
    competitor = env.observer.connect()
    competing = competitor.begin()
    ready, go = threading.Event(), threading.Event()

    def after_provider():
        competitor.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, created_at) "
            "VALUES (:id, :u, 'supabase', :i, :s, now())"),
            {"id": uuid.uuid4(), "u": other_user, "i": env.supabase.issuer, "s": subject})
        ready.set()
        assert go.wait(20)

    env.hooks["verify.provider_returned"] = after_provider
    env.clock.now = request["expires_at"] - timedelta(seconds=1)
    waiter = Runner(lambda: env.svc.verify_email_linking(operation_id=linking.operation_id,
                                                         session=session.credential(),
                                                         code=env.mailer.last_code(BOB), client_ip=IP1), [], "link")
    waiter.start()
    assert ready.wait(20)
    go.set()
    wait_for_waiters(env.observer, 1)               # la liaison attend l'insertion concurrente
    env.clock.now = request["expires_at"]
    competing.rollback()                            # l'insertion concurrente est annulée...
    competitor.close()
    waiter.join(20)
    assert waiter.error is None
    # ...la liaison aurait pu créer l'identité, mais l'échéance est passée.
    assert (waiter.result.status, waiter.result.reason) == ("rejected", "proof_not_applicable")
    assert env.count("auth_identities") == 0
