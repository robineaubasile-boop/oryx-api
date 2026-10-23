"""Service F2 dormant (R1 PR 2) contre un vrai PostgreSQL jetable.

Couvre C.2.1 et C.2.1-A : parcours de connexion OTP, invitation, liaison
e-mail ; frontières exactes des durées et des quotas (horloge contrôlée) ;
cinq échecs, blocage global, générations et opérations ; incertitudes et
pannes avant/après chaque appel fournisseur et chaque commit sensible ;
reprise exacte ; sessions expirées / révoquées ; conflits de liaison ;
rotation de K_abuse ; refus fermé ; absence de secrets en base et dans
les journaux. Les fournisseurs sont des doubles (aucun réseau) qui
vérifient qu'aucune transaction n'est ouverte pendant leurs appels.
"""
import logging
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.abuse import AbuseUnavailable
from core.auth_f2.crypto import Redacted, b64url_encode
from core.auth_f2.policy import (
    EMAIL_BLOCK_DURATION,
    ISSUE_IP_LIMIT,
    OTP_TTL,
    RESULT_RESUME_WINDOW,
    SESSION_ABSOLUTE_TTL,
    VERIFY_IP_LIMIT,
)
from core.auth_f2.providers import (
    MailSent,
    ProviderRejected,
    ProviderUncertain,
    VerifiedIdentity,
)
from core.auth_f2.service import CandidateCredential, DuplicatePendingInvitation, F2AuthService, SessionCredential
from tests.f2_support import (
    F2_TABLES,
    FixedClock,
    SimulatedCrash,
    build_service,
    clean_f2,
    create_profile_with_identity,
    make_keyring,
    prepare_schema,
    rows,
    scalar,
    service_engine,
)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

ALICE = "alice@example.com"
BOB = "bob@example.com"
IP1 = "203.0.113.7"
IP2 = "198.51.100.20"
US = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


class Env:
    def __init__(self, pg_url):
        self.pg_url = pg_url
        self.observer = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
        clean_f2(self.observer)
        self.engine = service_engine(pg_url)
        self.clock = None
        self.hooks = {}

    def hook(self, point):
        action = self.hooks.get(point)
        if action is not None:
            action()

    def service(self, *, fixed_clock=False, keyring=None, init_abuse=True):
        if fixed_clock and self.clock is None:
            self.clock = FixedClock(self.observer)
        svc, supabase, mailer = build_service(self.engine, self.observer, keyring, clock=self.clock if fixed_clock
                                              else None, fault_hook=self.hook, init_abuse=init_abuse)
        self.svc, self.supabase, self.mailer = svc, supabase, mailer
        return svc

    def count(self, table, where="TRUE", **params):
        return scalar(self.observer, f"SELECT count(*) FROM {table} WHERE {where}", **params)

    def events(self, kind):
        return self.count("auth_abuse_events", "event_kind = :k", k=kind)

    def request(self, request_id=None, operation_id=None):
        if request_id:
            return rows(self.observer, "SELECT * FROM auth_otp_requests WHERE id = :i", i=request_id)[0]
        return rows(self.observer, "SELECT * FROM auth_otp_requests WHERE auth_operation_id = :o "
                                   "ORDER BY code_generation DESC LIMIT 1", o=operation_id)[0]

    def dispose(self):
        self.engine.dispose()
        self.observer.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = Env(pg_url)
    yield environment
    environment.dispose()


def signed_in(env, email=ALICE, ip=IP1):
    start = env.svc.start_sign_in(email=email, client_ip=ip)
    assert start.status == "issued", start
    return start


def verify(env, start, code, ip=IP1):
    return env.svc.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate, code=code, client_ip=ip)


def wrong(code):
    return f"{(int(code) + 1) % 100000000:08d}"


# --------------------------------------------------------------------------
# 1. Connexion OTP
# --------------------------------------------------------------------------

def test_sign_in_end_to_end_stores_only_hmacs(env):
    svc = env.service()
    user = create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = svc.start_sign_in(email="  Alice@Example.COM ", client_ip=IP1)
    assert start.status == "issued" and start.candidate is not None
    code = env.mailer.last_code(ALICE)
    request = env.request(operation_id=start.operation_id)
    assert (request["status"], request["code_generation"], request["purpose"]) == ("issued", 1, "sign_in")
    assert request["expires_at"] - request["requested_at"] == OTP_TTL
    assert len(request["expected_code_hmac"]) == 64 and request["expected_code_hmac_key_version"] == "t1"

    done = verify(env, start, code)
    assert done.status == "authenticated" and done.session.user_id == user
    session = rows(env.observer, "SELECT * FROM oryx_sessions")[0]
    generation = rows(env.observer, "SELECT * FROM oryx_session_generations")[0]
    candidate = rows(env.observer, "SELECT * FROM auth_preauth_candidates")[0]
    result = rows(env.observer, "SELECT * FROM auth_operation_results")[0]
    op = rows(env.observer, "SELECT * FROM auth_operations")[0]
    assert session["absolute_expires_at"] - session["created_at"] == SESSION_ABSOLUTE_TTL
    assert generation["generation_no"] == candidate["generation_no"] == 1
    assert generation["derivation_version"] == "f2-c2-hkdf-sha256-v1:t1" and generation["hash_key_version"] == "t1"
    assert candidate["status"] == "bound" and candidate["bound_at"] == result["committed_at"]
    assert result["result_payload"] == {"outcome": "authenticated"} and result["result_version"] == "f2-c2-result-v1"
    assert result["resume_until"] - result["committed_at"] == RESULT_RESUME_WINDOW
    assert (op["status"], op["identity_resolution"], op["proven_issuer"]) == ("committed", "existing_identity",
                                                                             env.supabase.issuer)
    assert env.supabase.generate_calls == [ALICE] and env.supabase.verify_calls == [(ALICE, code)]
    assert env.events("otp_request") == 2 and env.events("otp_verification_attempt") == 1
    assert env.supabase.probe.checks == 3


def test_local_match_alone_never_creates_a_session(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)
    env.supabase.verify_script = [ProviderUncertain("transport")]
    first = verify(env, start, code)
    assert first.status == "uncertain"
    assert env.count("oryx_sessions") == 0
    assert env.request(operation_id=start.operation_id)["status"] == "verify_uncertain"
    # Aucune nouvelle tentative aveugle avec un code peut-être consommé.
    again = verify(env, start, code)
    assert (again.status, again.reason) == ("rejected", "verification_uncertain")
    assert len(env.supabase.verify_calls) == 1
    assert env.request(operation_id=start.operation_id)["failure_count"] == 0 and env.events("otp_failure") == 0


@pytest.mark.parametrize("status,body", [(200, {"user": "x"}), (500, None), (401, {"error_code": "no_authorization"}),
                                         (403, {"msg": "Forbidden"}), (429, {"error_code": "over_request_rate_limit"}),
                                         (422, {"error_code": "unknown_code"}), (403, None), (403, "<html>")])
def test_ambiguous_provider_answers_are_never_success_nor_typing_failures(env, status, body):
    """P2 : seule la liste positive (403, otp_expired) est un refus certain ;
    toute autre réponse (401, 403 sans code, 429, code inconnu, corps absent
    ou invalide) reste incertaine : ni session, ni échec de saisie."""
    from core.auth_f2.providers import parse_verify

    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    env.supabase.verify_script = [parse_verify(status, body, ALICE)]
    assert verify(env, start, env.mailer.last_code(ALICE)).status == "uncertain"
    assert env.count("oryx_sessions") == 0 and env.count("auth_operations", "status = 'committed'") == 0
    request = env.request(operation_id=start.operation_id)
    assert (request["status"], request["failure_count"]) == ("verify_uncertain", 0) and env.events("otp_failure") == 0


def test_certain_provider_rejection_after_local_match_is_not_a_typing_failure(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    env.supabase.verify_script = [ProviderRejected("otp_expired")]
    result = verify(env, start, env.mailer.last_code(ALICE))
    assert (result.status, result.reason) == ("rejected", "provider_rejected")
    request = env.request(operation_id=start.operation_id)
    assert (request["status"], request["failure_count"]) == ("expired", 0)
    attempt = rows(env.observer, "SELECT * FROM auth_otp_verification_attempts")[0]
    assert (attempt["state"], attempt["local_match"], attempt["provider_error_code"]) == ("rejected", True, "otp_expired")
    assert env.events("otp_failure") == 0 and env.count("oryx_sessions") == 0


def test_unknown_identity_is_never_signed_up(env):
    env.service()
    start = signed_in(env)
    result = verify(env, start, env.mailer.last_code(ALICE))
    assert (result.status, result.reason) == ("rejected", "identity_not_linked")
    assert env.count("users") == 0 and env.count("auth_identities") == 0 and env.count("oryx_sessions") == 0
    op = rows(env.observer, "SELECT * FROM auth_operations")[0]
    assert (op["status"], op["failure_code"], op["user_id"]) == ("failed", "identity_not_linked", None)
    assert rows(env.observer, "SELECT status FROM auth_preauth_candidates")[0]["status"] == "revoked"


def test_identity_is_resolved_by_subject_never_by_email_equality(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE, user_id="owner-a")
    # Supabase rend un autre subject pour la même adresse : aucune fusion.
    env.supabase.subjects[ALICE] = "99999999-9999-4999-8999-999999999999"
    start = signed_in(env)
    result = verify(env, start, env.mailer.last_code(ALICE))
    assert (result.status, result.reason) == ("rejected", "identity_not_linked")
    assert env.count("oryx_sessions") == 0


# --------------------------------------------------------------------------
# 2. Échecs, épuisement, blocage global
# --------------------------------------------------------------------------

def test_fifth_certain_failure_exhausts_the_request_forever(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)
    reasons = [verify(env, start, wrong(code)).reason for _ in range(5)]
    assert reasons == ["code_incorrect"] * 4 + ["email_blocked"]
    request = env.request(operation_id=start.operation_id)
    assert (request["status"], request["failure_count"]) == ("exhausted", 5) and request["closed_at"] is not None
    late = verify(env, start, code)
    assert late.status in ("rejected", "refused") and env.supabase.verify_calls == []
    assert env.count("oryx_sessions") == 0


@pytest.mark.parametrize("code", ["1234567", "123456789", "abcdefgh", "１２３４５６７８", "1234 678", "", None, 12345678,
                                  "9" * 10000])
def test_malformed_code_is_a_local_failure_without_attempt_row(env, code):
    env.service()
    start = signed_in(env)
    result = verify(env, start, code)
    assert (result.status, result.reason) == ("rejected", "code_malformed")
    assert env.request(operation_id=start.operation_id)["failure_count"] == 1
    assert env.count("auth_otp_verification_attempts") == 0
    assert env.events("otp_failure") == 1 and env.events("otp_verification_attempt") == 1
    assert env.supabase.verify_calls == []


def test_global_email_block_spans_operations_ips_and_browsers(env):
    env.service(fixed_clock=True)
    first = signed_in(env, ip=IP1)
    for _ in range(3):
        assert verify(env, first, wrong(env.mailer.last_code(ALICE)), ip=IP1).reason == "code_incorrect"
    env.clock.advance(seconds=60)
    second = signed_in(env, ip=IP2)  # autre opération, autre IP, autre « navigateur »
    assert verify(env, second, wrong(env.mailer.last_code(ALICE)), ip=IP2).reason == "code_incorrect"
    blocked_at = env.clock.advance(seconds=1)
    fifth = verify(env, second, wrong(env.mailer.last_code(ALICE)), ip=IP2)
    assert fifth.reason == "email_blocked"
    block = rows(env.observer, "SELECT * FROM auth_abuse_events WHERE event_kind = 'otp_email_block'")
    assert len(block) == 1 and block[0]["occurred_at"] == blocked_at
    assert block[0]["blocked_until"] == blocked_at + EMAIL_BLOCK_DURATION

    calls = len(env.supabase.generate_calls)
    env.clock.advance(minutes=1)
    assert env.svc.start_sign_in(email=ALICE, client_ip="192.0.2.1").reason == "email_blocked"
    refused = verify(env, second, env.mailer.last_code(ALICE), ip="192.0.2.2")
    assert (refused.status, refused.reason) == ("refused", "email_blocked")
    assert len(env.supabase.generate_calls) == calls and env.supabase.verify_calls == []
    # Les appels rejetés ne prolongent pas le blocage.
    assert env.events("otp_email_block") == 1

    env.clock.now = blocked_at + EMAIL_BLOCK_DURATION - US
    assert env.svc.start_sign_in(email=ALICE, client_ip="192.0.2.3").reason == "email_blocked"
    env.clock.now = blocked_at + EMAIL_BLOCK_DURATION
    again = signed_in(env, ip="192.0.2.4")
    # Les échecs consommés par le blocage ne sont pas recomptés.
    assert verify(env, again, wrong(env.mailer.last_code(ALICE)), ip="192.0.2.4").reason == "code_incorrect"
    assert env.events("otp_email_block") == 1


def test_failures_without_admissible_request_never_touch_the_email(env):
    env.service()
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)
    forged = CandidateCredential(start.candidate.candidate_id, Redacted(b64url_encode(bytes(32))))
    other = CandidateCredential(uuid.uuid4(), start.candidate.secret)
    outcomes = [
        env.svc.verify_sign_in(operation_id=uuid.uuid4(), candidate=start.candidate, code=code, client_ip=IP2),
        env.svc.verify_sign_in(operation_id=start.operation_id, candidate=forged, code=wrong(code), client_ip=IP2),
        env.svc.verify_sign_in(operation_id=start.operation_id, candidate=other, code=wrong(code), client_ip=IP2),
        env.svc.verify_sign_in(operation_id="not-a-uuid", candidate=None, code=code, client_ip=IP2),
        env.svc.verify_email_linking(operation_id=start.operation_id, session=None, code=wrong(code), client_ip=IP2),
    ]
    assert [o.reason for o in outcomes] == ["operation_unknown", "credential_invalid", "credential_invalid",
                                            "operation_unknown", "operation_unknown"]
    assert env.events("otp_verification_attempt") == 5 and env.events("otp_failure") == 0
    assert env.request(operation_id=start.operation_id)["failure_count"] == 0
    assert env.supabase.verify_calls == []


def test_replaced_or_expired_generation_codes_are_not_typing_failures(env):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    old = env.mailer.last_code(ALICE)
    env.clock.advance(seconds=60)
    resent = env.svc.resend_sign_in_otp(operation_id=start.operation_id, candidate=start.candidate, client_ip=IP1)
    assert resent.status == "issued"
    requests = rows(env.observer, "SELECT status, code_generation FROM auth_otp_requests ORDER BY code_generation")
    assert [(r["status"], r["code_generation"]) for r in requests] == [("superseded", 1), ("issued", 2)]
    assert verify(env, start, old).reason == "code_replaced"
    assert env.request(operation_id=start.operation_id)["failure_count"] == 0 and env.events("otp_failure") == 0
    assert verify(env, start, env.mailer.last_code(ALICE)).status == "authenticated"


def test_otp_ttl_boundary_is_exact(env):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    request = env.request(operation_id=start.operation_id)
    env.clock.now = request["expires_at"]
    expired = verify(env, start, env.mailer.last_code(ALICE))
    assert expired.reason in ("code_expired", "operation_expired") and env.events("otp_failure") == 0

    env.clock.advance(seconds=1)
    second = signed_in(env)
    env.clock.now = env.request(operation_id=second.operation_id)["expires_at"] - US
    assert verify(env, second, env.mailer.last_code(ALICE)).status == "authenticated"


# --------------------------------------------------------------------------
# 3. Quotas : frontières exactes, refus sans fournisseur, marqueurs bornés
# --------------------------------------------------------------------------

def test_sixty_seconds_between_emissions_exact_boundary(env):
    env.service(fixed_clock=True)
    t0 = env.clock.now
    signed_in(env)
    env.clock.now = t0 + timedelta(seconds=60) - US
    assert env.svc.start_sign_in(email=ALICE, client_ip=IP2).reason == "issue_interval"
    env.clock.now = t0 + timedelta(seconds=60)
    assert env.svc.start_sign_in(email=ALICE, client_ip=IP2).status == "issued"
    assert len(env.supabase.generate_calls) == 2


def test_five_emissions_per_hour_per_email_sliding(env):
    env.service(fixed_clock=True)
    t0 = env.clock.now
    for i in range(5):
        env.clock.now = t0 + timedelta(minutes=i)
        signed_in(env, ip=f"192.0.2.{i + 1}")
    env.clock.now = t0 + timedelta(hours=1) - US
    assert env.svc.start_sign_in(email=ALICE, client_ip="192.0.2.50").reason == "quota_email_issue"
    env.clock.now = t0 + timedelta(hours=1)
    assert env.svc.start_sign_in(email=ALICE, client_ip="192.0.2.51").status == "issued"
    assert len(env.supabase.generate_calls) == 6


def test_twenty_emissions_per_hour_per_ip_and_ipv6_prefix(env):
    env.service(fixed_clock=True)
    t0 = env.clock.now
    for i in range(ISSUE_IP_LIMIT):
        assert env.svc.start_sign_in(email=f"user{i}@example.com", client_ip=f"2001:db8:0:1::{i + 1}").status == "issued"
    refused = env.svc.start_sign_in(email="late@example.com", client_ip="2001:db8:0:1:ffff::9")
    assert (refused.status, refused.reason) == ("refused", "quota_ip_issue")
    assert env.svc.start_sign_in(email="late@example.com", client_ip="2001:db8:0:2::1").status == "issued"
    env.clock.now = t0 + timedelta(hours=1)
    assert env.svc.start_sign_in(email="later@example.com", client_ip="2001:db8:0:1::77").status == "issued"


def test_thirty_admitted_verifications_per_five_minutes_per_ip(env):
    env.service(fixed_clock=True)
    t0 = env.clock.now
    start = signed_in(env, ip=IP2)
    for i in range(VERIFY_IP_LIMIT):
        env.clock.now = t0 + timedelta(seconds=i)
        assert env.svc.verify_sign_in(operation_id=uuid.uuid4(), candidate=start.candidate, code="00000000",
                                      client_ip=IP1).status == "rejected"
    env.clock.now = t0 + timedelta(minutes=5) - US
    thirty_first = verify(env, start, env.mailer.last_code(ALICE), ip=IP1)
    assert (thirty_first.status, thirty_first.reason) == ("refused", "quota_ip_verification")
    assert env.events("otp_verification_attempt") == VERIFY_IP_LIMIT and env.supabase.verify_calls == []
    env.clock.now = t0 + timedelta(minutes=5)
    assert env.svc.verify_sign_in(operation_id=uuid.uuid4(), candidate=start.candidate, code="00000000",
                                  client_ip=IP1).status == "rejected"
    assert env.events("otp_verification_attempt") == VERIFY_IP_LIMIT + 1


def test_refusals_never_call_providers_and_markers_stay_bounded(env):
    env.service(fixed_clock=True)
    env.clock.now = env.clock.now.replace(minute=(env.clock.now.minute // 5) * 5, second=0, microsecond=0)
    bucket = env.clock.now
    signed_in(env)
    for i in range(50):
        env.clock.now = bucket + timedelta(seconds=1 + i)
        assert env.svc.start_sign_in(email=ALICE, client_ip=IP1).status == "refused"
    markers = rows(env.observer, "SELECT * FROM auth_abuse_events WHERE event_kind = 'otp_request_refused'")
    assert len(markers) == 1 and markers[0]["occurred_at"] == bucket
    assert len(env.supabase.generate_calls) == 1 and len(env.mailer.sent) == 1
    env.clock.now = bucket + timedelta(minutes=5)
    signed_in(env, email=BOB)
    env.clock.now = bucket + timedelta(minutes=5, seconds=1)
    env.svc.start_sign_in(email=BOB, client_ip=IP1)
    env.svc.start_sign_in(email=BOB, client_ip=IP1)
    assert env.events("otp_request_refused") == 2
    assert env.count("auth_abuse_events", "event_kind = 'otp_request_refused' AND occurred_at = :b",
                     b=bucket + timedelta(minutes=5)) == 1


def test_quotas_are_shared_between_sign_in_and_linking(env):
    env.service(fixed_clock=True)
    invitation = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=invitation.token)
    session = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session
    signed_in(env, email=BOB, ip=IP1)
    linking = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP2)
    assert (linking.status, linking.reason) == ("refused", "issue_interval")


def test_invalid_ip_or_email_fail_closed_without_any_write(env):
    env.service()
    for result in (env.svc.start_sign_in(email=ALICE, client_ip=None),
                   env.svc.start_sign_in(email=ALICE, client_ip="10.0.0.1, 203.0.113.7"),
                   env.svc.start_sign_in(email="not-an-email", client_ip=IP1),
                   env.svc.verify_sign_in(operation_id=uuid.uuid4(), candidate=None, code="12345678", client_ip="")):
        assert result.status == "rejected"
    assert env.count("auth_abuse_events") == 0 and env.count("auth_abuse_subjects") == 0
    assert env.supabase.generate_calls == []


# --------------------------------------------------------------------------
# 4. Pannes et incertitudes fournisseur à l'émission
# --------------------------------------------------------------------------

@pytest.mark.parametrize("generated", [ProviderRejected("email_address_invalid"),
                                       ProviderRejected("nonconforming_output")])
def test_certain_generate_rejection_closes_the_request_without_sending(env, generated):
    env.service()
    env.supabase.generate_script = [generated]
    result = env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    assert result.status == "send_failed" and env.mailer.sent == []
    request = env.request(operation_id=result.operation_id)
    assert request["status"] == "send_failed" and request["expected_code_hmac"] is None
    assert request["closed_at"] is not None
    assert env.events("otp_request") == 2  # la réservation reste comptée


@pytest.mark.parametrize("generated", [ProviderUncertain("transport"), ProviderUncertain("http_503"),
                                       ProviderUncertain("http_401:no_authorization"),
                                       ProviderUncertain("http_429:over_email_send_rate_limit")])
def test_uncertain_generate_link_is_issue_uncertain_never_send_failed(env, generated):
    """P2 : une issue inconnue de generateLink reste incertaine (issue_uncertain),
    sans envoi, sans renvoi aveugle, sans restitution de quota ; seule une
    nouvelle génération explicite, sous quotas, la remplace."""
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    env.supabase.generate_script = [generated]
    start = env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    assert (start.status, start.reason) == ("issue_uncertain", "generate_uncertain")
    assert start.candidate is not None and env.mailer.sent == []
    request = env.request(operation_id=start.operation_id)
    assert (request["status"], request["expected_code_hmac"], request["closed_at"]) == ("issue_uncertain", None, None)
    assert env.events("otp_request") == 2
    # Aucune vérification possible sans code lié, aucun appel fournisseur.
    assert verify(env, start, "12345678").reason == "code_not_ready"
    assert env.supabase.verify_calls == [] and len(env.supabase.generate_calls) == 1
    # Pas de renvoi automatique : une nouvelle génération reste soumise aux quotas.
    env.clock.advance(seconds=59)
    assert env.svc.resend_sign_in_otp(operation_id=start.operation_id, candidate=start.candidate,
                                      client_ip=IP1).reason == "issue_interval"
    env.clock.advance(seconds=1)
    resent = env.svc.resend_sign_in_otp(operation_id=start.operation_id, candidate=start.candidate, client_ip=IP1)
    assert resent.status == "issued"
    statuses = rows(env.observer, "SELECT status, code_generation FROM auth_otp_requests ORDER BY code_generation")
    assert [(r["status"], r["code_generation"]) for r in statuses] == [("superseded", 1), ("issued", 2)]
    assert env.events("otp_request") == 4  # aucune restitution de la réservation incertaine
    assert verify(env, start, env.mailer.last_code(ALICE)).status == "authenticated"


@pytest.mark.parametrize("sent,status", [(MailSent(), "issued"), (ProviderRejected("http_429"), "send_failed"),
                                         (ProviderUncertain("transport"), "issue_uncertain")])
def test_send_outcomes(env, sent, status):
    env.service()
    env.mailer.script = [sent]
    result = env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    assert result.status == status and len(env.mailer.sent) == 1
    assert env.request(operation_id=result.operation_id)["status"] == status


def test_issue_uncertain_code_can_still_be_verified_with_provider_proof(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    env.mailer.script = [ProviderUncertain("transport")]
    start = env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    assert start.status == "issue_uncertain"
    assert verify(env, start, env.mailer.last_code(ALICE)).status == "authenticated"


@pytest.mark.parametrize("point", ["issue.reserved", "issue.generated", "issue.bound", "issue.sent"])
def test_crash_around_each_issue_step_never_double_sends_and_resend_supersedes(env, point):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)

    def crash():
        raise SimulatedCrash(point)

    env.hooks[point] = crash
    with pytest.raises(SimulatedCrash):
        env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    env.hooks.clear()
    request = rows(env.observer, "SELECT * FROM auth_otp_requests")[0]
    assert request["status"] == "issuing"
    assert (request["expected_code_hmac"] is not None) == (point in ("issue.bound", "issue.sent"))
    assert len(env.mailer.sent) == (1 if point == "issue.sent" else 0)
    assert env.events("otp_request") == 2
    # Une nouvelle demande reste soumise aux quotas, puis remplace l'ancienne.
    candidate = rows(env.observer, "SELECT * FROM auth_preauth_candidates")[0]
    assert candidate["status"] == "open"
    env.clock.advance(seconds=59)
    assert env.svc.start_sign_in(email=ALICE, client_ip=IP1).reason == "issue_interval"


def test_crash_after_send_loses_the_candidate_so_nothing_can_be_verified(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    env.hooks["issue.sent"] = lambda: (_ for _ in ()).throw(SimulatedCrash("issue.sent"))
    with pytest.raises(SimulatedCrash):
        env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    env.hooks.clear()
    op_id = rows(env.observer, "SELECT id FROM auth_operations")[0]["id"]
    assert env.request(operation_id=op_id)["status"] == "issuing"
    # Le secret candidat était perdu avec le processus : aucune session possible.
    assert env.svc.verify_sign_in(operation_id=op_id, candidate=None, code=env.mailer.last_code(ALICE),
                                  client_ip=IP1).reason == "credential_invalid"
    assert env.supabase.verify_calls == []


# --------------------------------------------------------------------------
# 5. Pannes et incertitudes à la vérification
# --------------------------------------------------------------------------

def test_crash_before_admission_commit_leaves_nothing(env):
    env.service()
    start = signed_in(env)
    env.hooks["verify.admitted"] = lambda: (_ for _ in ()).throw(SimulatedCrash("admitted"))
    with pytest.raises(SimulatedCrash):
        verify(env, start, env.mailer.last_code(ALICE))
    assert env.events("otp_verification_attempt") == 0 and env.count("auth_otp_verification_attempts") == 0


@pytest.mark.parametrize("point", ["verify.reserved", "verify.provider_returned", "verify.before_commit"])
def test_crash_around_provider_call_never_grants_a_session(env, point):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)
    env.hooks[point] = lambda: (_ for _ in ()).throw(SimulatedCrash(point))
    with pytest.raises(SimulatedCrash):
        verify(env, start, code)
    env.hooks.clear()
    assert env.count("oryx_sessions") == 0 and env.count("auth_operations", "status = 'committed'") == 0
    attempt = rows(env.observer, "SELECT * FROM auth_otp_verification_attempts")[0]
    assert attempt["state"] == "in_flight"
    assert env.request(operation_id=start.operation_id)["status"] == "verifying"
    # Pas de seconde tentative fournisseur pendant le vol...
    assert verify(env, start, code).reason == "verification_in_progress"
    # ...ni après : la tentative est requalifiée incertaine, jamais réussie.
    env.clock.advance(seconds=60)
    assert verify(env, start, code).reason == "verification_uncertain"
    assert rows(env.observer, "SELECT state FROM auth_otp_verification_attempts")[0]["state"] == "uncertain"
    calls = len(env.supabase.verify_calls)
    # Seule une nouvelle génération (sous quotas) permet de continuer.
    resent = env.svc.resend_sign_in_otp(operation_id=start.operation_id, candidate=start.candidate, client_ip=IP1)
    assert resent.status == "issued"
    assert rows(env.observer, "SELECT state FROM auth_otp_verification_attempts")[0]["state"] == "abandoned"
    assert verify(env, start, env.mailer.last_code(ALICE)).status == "authenticated"
    assert len(env.supabase.verify_calls) == calls + 1 and env.count("oryx_sessions") == 1


def test_technical_finalization_retry_is_not_billed_twice(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    failures = []

    def transient():
        if not failures:
            failures.append(1)
            raise sa.exc.OperationalError("SELECT 1", {}, Exception("connexion perdue"))

    env.hooks["verify.finalizing"] = transient
    assert verify(env, start, env.mailer.last_code(ALICE)).status == "authenticated"
    assert failures == [1]
    assert env.events("otp_verification_attempt") == 1 and env.count("oryx_sessions") == 1
    assert len(env.supabase.verify_calls) == 1


def test_late_success_for_a_replaced_generation_grants_nothing(env):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)

    def resend_during_call():
        env.clock.advance(seconds=60)
        assert env.svc.resend_sign_in_otp(operation_id=start.operation_id, candidate=start.candidate,
                                          client_ip=IP1).status == "issued"

    env.hooks["verify.reserved"] = resend_during_call
    env.supabase.verify_script = [VerifiedIdentity(env.supabase.subject_for(ALICE), ALICE)]
    late = verify(env, start, code)
    assert (late.status, late.reason) == ("rejected", "verification_superseded")
    assert env.count("oryx_sessions") == 0


def test_late_success_after_expiry_or_block_grants_nothing(env):
    env.service(fixed_clock=True)
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)
    env.hooks["verify.reserved"] = lambda: env.clock.advance(seconds=600)
    result = verify(env, start, code)
    assert (result.status, result.reason) == ("rejected", "proof_not_applicable")
    assert env.count("oryx_sessions") == 0
    assert env.request(operation_id=start.operation_id)["status"] == "expired"


def test_revoked_initiating_session_blocks_linking_finalization(env):
    env.service()
    invitation = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=invitation.token)
    session = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session
    linking = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP1)
    env.hooks["verify.reserved"] = lambda: env.svc.revoke_session(session.session_id, "security")
    result = env.svc.verify_email_linking(operation_id=linking.operation_id, session=session.credential(),
                                          code=env.mailer.last_code(BOB), client_ip=IP1)
    assert (result.status, result.reason) == ("rejected", "proof_not_applicable")
    assert env.count("auth_identities") == 0


# --------------------------------------------------------------------------
# 6. Reprise et sessions
# --------------------------------------------------------------------------

def _authenticated(env):
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    done = verify(env, start, env.mailer.last_code(ALICE))
    assert done.status == "authenticated"
    return start, done


def _snapshot(env):
    return {t: rows(env.observer, f"SELECT * FROM {t} ORDER BY 1") for t in F2_TABLES}


def test_resume_returns_exactly_the_same_session_without_writing(env):
    env.service(fixed_clock=True)
    start, done = _authenticated(env)
    before = _snapshot(env)
    env.clock.advance(minutes=4, seconds=59)
    resumed = env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate)
    assert resumed.status == "resumed" and resumed.session.resumed
    for name in ("session_id", "generation_id", "user_id", "active_secret", "absolute_expires_at"):
        assert getattr(resumed.session, name) == getattr(done.session, name)
    assert _snapshot(env) == before


def test_resume_window_boundary_is_exact(env):
    env.service(fixed_clock=True)
    start, _ = _authenticated(env)
    resume_until = rows(env.observer, "SELECT resume_until FROM auth_operation_results")[0]["resume_until"]
    env.clock.now = resume_until - US
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate).status == "resumed"
    env.clock.now = resume_until
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate).status == "rejected"


def test_resume_refusals(env):
    env.service()
    start, done = _authenticated(env)
    forged = CandidateCredential(start.candidate.candidate_id, Redacted(b64url_encode(bytes(32))))
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=forged).status == "rejected"
    assert env.svc.resume_result(operation_id=uuid.uuid4(), candidate=start.candidate).status == "rejected"
    other = signed_in(env, email=BOB, ip=IP2)  # candidat 'open' : aucune reprise
    assert env.svc.resume_result(operation_id=other.operation_id, candidate=other.candidate).status == "rejected"
    assert env.svc.resume_result(operation_id=start.operation_id,
                                 candidate=CandidateCredential(other.candidate.candidate_id,
                                                               other.candidate.secret)).status == "rejected"
    # Un candidat lié ne peut pas recommencer une authentification.
    again = verify(env, start, "12345678")
    assert (again.status, again.reason) == ("rejected", "operation_closed")
    env.svc.revoke_session(done.session.session_id, "logout")
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate).status == "rejected"


def test_resume_refuses_revoked_generation_and_unknown_keys(env):
    env.service()
    start, done = _authenticated(env)
    keyring = make_keyring({p: ("t1",) if p != "derive" else ("t9",) for p in
                            ("candidate", "derive", "active", "otp", "invitation", "abuse")})
    other = F2AuthService(env.engine, keyring, env.supabase, env.mailer)
    refused = other.resume_result(operation_id=start.operation_id, candidate=start.candidate)
    assert (refused.status, refused.reason) == ("rejected", "key_unavailable")
    with env.observer.begin() as c:
        c.execute(sa.text("UPDATE oryx_session_generations SET derivation_version = 'f2-hkdf-v1:t1'"))
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate).reason == "key_unavailable"
    with env.observer.begin() as c:
        c.execute(sa.text("UPDATE oryx_session_generations SET derivation_version = 'f2-c2-hkdf-sha256-v1:t1', "
                          "status = 'revoked', revoked_at = issued_at, revocation_reason = 'security'"))
    assert env.svc.resume_result(operation_id=start.operation_id, candidate=start.candidate).status == "rejected"


def test_authenticate_session_confirms_delivery_and_enforces_expiry_and_revocation(env):
    env.service(fixed_clock=True)
    _, done = _authenticated(env)
    principal = env.svc.authenticate_session(done.session.credential())
    assert principal.user_id == done.session.user_id and principal.auth_method == "supabase_email_sign_in"
    generation = rows(env.observer, "SELECT * FROM oryx_session_generations")[0]
    assert generation["delivery_status"] == "confirmed" and generation["delivery_proof_version"]
    confirmed_at = generation["delivery_confirmed_at"]
    env.clock.advance(seconds=5)
    env.svc.authenticate_session(done.session.credential())
    assert rows(env.observer, "SELECT delivery_confirmed_at FROM oryx_session_generations")[0][
        "delivery_confirmed_at"] == confirmed_at

    forged = SessionCredential(done.session.generation_id, Redacted(b64url_encode(bytes(32))))
    assert env.svc.authenticate_session(forged) is None
    assert env.svc.authenticate_session(SessionCredential(uuid.uuid4(), done.session.active_secret)) is None
    env.clock.now = done.session.absolute_expires_at - US
    assert env.svc.authenticate_session(done.session.credential()) is not None
    env.clock.now = done.session.absolute_expires_at
    assert env.svc.authenticate_session(done.session.credential()) is None
    env.clock.now = done.session.absolute_expires_at - timedelta(days=1)
    assert env.svc.revoke_session(done.session.session_id, "logout") is True
    assert env.svc.revoke_session(done.session.session_id, "logout") is False
    assert env.svc.authenticate_session(done.session.credential()) is None
    with pytest.raises(ValueError):
        env.svc.revoke_session(done.session.session_id, "because")


# --------------------------------------------------------------------------
# 7. Invitations
# --------------------------------------------------------------------------

def test_invitation_activation_creates_a_backend_profile_and_session(env):
    env.service()
    created = env.svc.create_invitation(cohort="beta-1", recipient_email="Guest@Example.com")
    stored = rows(env.observer, "SELECT * FROM invitations")[0]
    assert stored["token_hash"] != created.token.reveal() and len(stored["token_hash"]) == 64
    assert stored["recipient_email_normalized"] == "guest@example.com"
    assert stored["expires_at"] - stored["created_at"] == timedelta(days=7)
    begin = env.svc.begin_invitation_activation(token=created.token)
    assert begin.status == "candidate_issued"
    done = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate)
    assert done.status == "invitation_activated"
    assert uuid.UUID(done.session.user_id) and env.count("users", "id = :u", u=done.session.user_id) == 1
    assert rows(env.observer, "SELECT status FROM invitations")[0]["status"] == "accepted"
    result = rows(env.observer, "SELECT * FROM auth_operation_results")[0]
    assert result["result_payload"] == {"outcome": "invitation_activated"} and result["auth_identity_id"] is None
    # Doublon : le candidat lié ne recrée rien ; seule la reprise rend la session.
    again = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate)
    assert again.status == "rejected" and env.count("oryx_sessions") == 1
    resumed = env.svc.resume_result(operation_id=begin.operation_id, candidate=begin.candidate)
    assert resumed.session.active_secret == done.session.active_secret
    assert env.svc.begin_invitation_activation(token=created.token).reason == "invitation_unavailable"


def test_invitation_single_use_across_operations_and_revocation(env):
    env.service()
    created = env.svc.create_invitation(cohort="beta")
    first = env.svc.begin_invitation_activation(token=created.token)
    second = env.svc.begin_invitation_activation(token=created.token)
    assert env.svc.complete_invitation_activation(operation_id=first.operation_id,
                                                  candidate=first.candidate).status == "invitation_activated"
    lost = env.svc.complete_invitation_activation(operation_id=second.operation_id, candidate=second.candidate)
    assert (lost.status, lost.reason) == ("rejected", "invitation_unavailable")
    assert env.count("users") == 1 and env.count("oryx_sessions") == 1

    revoked = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=revoked.token)
    assert env.svc.revoke_invitation(revoked.invitation_id) is True
    assert env.svc.complete_invitation_activation(operation_id=begin.operation_id,
                                                  candidate=begin.candidate).reason == "invitation_unavailable"


def test_invitation_duplicates_expiry_and_malformed_tokens(env):
    env.service(fixed_clock=True)
    created = env.svc.create_invitation(cohort="beta", recipient_email="x@example.com")
    with pytest.raises(DuplicatePendingInvitation):
        env.svc.create_invitation(cohort="beta", recipient_email="X@example.com")
    raw = created.token.reveal()
    for token in (Redacted(raw[:-1] + ("B" if raw[-1] == "A" else "A")), Redacted(raw + "="), Redacted("short"),
                  raw, None):
        assert env.svc.begin_invitation_activation(token=token).reason == "invitation_unavailable"
    env.clock.now = created.expires_at - US
    begin = env.svc.begin_invitation_activation(token=created.token)
    assert begin.status == "candidate_issued"
    env.clock.now = created.expires_at
    assert env.svc.complete_invitation_activation(operation_id=begin.operation_id,
                                                  candidate=begin.candidate).status == "rejected"
    assert env.svc.begin_invitation_activation(token=created.token).reason == "invitation_unavailable"
    assert env.count("users") == 0


# --------------------------------------------------------------------------
# 8. Liaison e-mail
# --------------------------------------------------------------------------

def _invited_session(env):
    created = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=created.token)
    return env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate).session


def test_email_linking_creates_identity_without_new_session(env):
    env.service(fixed_clock=True)
    session = _invited_session(env)
    linking = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP1)
    assert linking.status == "issued" and linking.candidate is None
    linked = env.svc.verify_email_linking(operation_id=linking.operation_id, session=session.credential(),
                                          code=env.mailer.last_code(BOB), client_ip=IP1)
    assert (linked.status, linked.reason, linked.session) == ("email_linked", "new_identity", None)
    assert env.count("oryx_sessions") == 1
    identity = rows(env.observer, "SELECT * FROM auth_identities")[0]
    assert identity["user_id"] == session.user_id and identity["issuer"] == env.supabase.issuer
    assert rows(env.observer, "SELECT result_payload FROM auth_operation_results WHERE operation_kind = "
                              "'email_linking'")[0]["result_payload"] == {"outcome": "email_linked"}
    env.clock.advance(seconds=60)
    start = signed_in(env, email=BOB, ip=IP2)
    assert verify(env, start, env.mailer.last_code(BOB), ip=IP2).session.user_id == session.user_id


def test_linking_conflict_never_merges_profiles(env):
    env.service()
    create_profile_with_identity(env.observer, env.supabase, BOB, user_id="owner-b")
    session = _invited_session(env)
    linking = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP1)
    result = env.svc.verify_email_linking(operation_id=linking.operation_id, session=session.credential(),
                                          code=env.mailer.last_code(BOB), client_ip=IP1)
    assert (result.status, result.reason) == ("rejected", "conflict_other_profile")
    op = rows(env.observer, "SELECT * FROM auth_operations WHERE kind = 'email_linking'")[0]
    assert (op["status"], op["identity_resolution"], op["auth_identity_id"]) == ("failed", "conflict_other_profile",
                                                                                 None)
    assert [r["user_id"] for r in rows(env.observer, "SELECT user_id FROM auth_identities")] == ["owner-b"]


def test_one_identity_per_profile_and_session_kind_checks(env):
    env.service(fixed_clock=True)
    session = _invited_session(env)
    first = env.svc.start_email_linking(session=session.credential(), email=BOB, client_ip=IP1)
    env.clock.advance(seconds=1)
    second = env.svc.start_email_linking(session=session.credential(), email=ALICE, client_ip=IP1)
    assert env.svc.verify_email_linking(operation_id=first.operation_id, session=session.credential(),
                                        code=env.mailer.last_code(BOB), client_ip=IP1).status == "email_linked"
    late = env.svc.verify_email_linking(operation_id=second.operation_id, session=session.credential(),
                                        code=env.mailer.last_code(ALICE), client_ip=IP1)
    assert (late.status, late.reason) == ("rejected", "profile_already_linked")
    assert env.svc.start_email_linking(session=session.credential(), email="c@example.com",
                                       client_ip=IP1).reason == "profile_already_linked"
    assert env.count("auth_identities") == 1
    # Une session de connexion OTP ne peut pas initier une liaison.
    env.clock.advance(seconds=60)
    start = signed_in(env, email=BOB, ip=IP2)
    otp_session = verify(env, start, env.mailer.last_code(BOB), ip=IP2).session
    assert env.svc.start_email_linking(session=otp_session.credential(), email="d@example.com",
                                       client_ip=IP2).reason == "session_invalid"


# --------------------------------------------------------------------------
# 9. Rotation K_abuse et refus fermé
# --------------------------------------------------------------------------

PURPOSES = ("candidate", "derive", "active", "otp", "invitation", "abuse")


def _keyring(abuse_versions, current="v1"):
    versions = {p: ("t1",) for p in PURPOSES}
    versions["abuse"] = abuse_versions
    return make_keyring(versions, {**{p: "t1" for p in PURPOSES}, "abuse": current})


def test_rotation_never_resets_quotas_and_retirement_waits(env):
    both = _keyring(("v1", "v2"))
    env.service(fixed_clock=True, keyring=both, init_abuse=False)
    abuse.initialize_abuse_key_state(env.engine, both, "v1")
    t0 = env.clock.now
    for i in range(5):
        env.clock.now = t0 + timedelta(minutes=i)
        signed_in(env, ip=f"192.0.2.{i + 1}")
    abuse.rotate_abuse_key(env.engine, both, "v2", clock=env.clock)
    env.clock.now = t0 + timedelta(minutes=30)
    assert env.svc.start_sign_in(email=ALICE, client_ip="192.0.2.99").reason == "quota_email_issue"
    with pytest.raises(AbuseUnavailable):
        abuse.retire_previous_abuse_key(env.engine, clock=env.clock)
    env.clock.now = t0 + timedelta(hours=1)
    assert signed_in(env, ip="192.0.2.100").status == "issued"
    versions = rows(env.observer, "SELECT s.hash_key_version, count(*) AS n FROM auth_abuse_events e JOIN "
                                  "auth_abuse_subjects s ON s.id = e.subject_id WHERE e.event_kind = 'otp_request' "
                                  "AND s.scope_kind = 'email' GROUP BY 1 ORDER BY 1")
    assert [(r["hash_key_version"], r["n"]) for r in versions] == [("v1", 5), ("v2", 1)]
    # Une instance qui ignore v1 refuse tant que v1 est comptée.
    stale = F2AuthService(env.engine, _keyring(("v2",), "v2"), env.supabase, env.mailer, clock=env.clock)
    with pytest.raises(AbuseUnavailable):
        stale.start_sign_in(email=ALICE, client_ip="192.0.2.101")
    env.clock.now = t0 + timedelta(minutes=5 + 90)
    abuse.retire_previous_abuse_key(env.engine, clock=env.clock)
    with pytest.raises(AbuseUnavailable):
        F2AuthService(env.engine, _keyring(("v1",), "v1"), env.supabase, env.mailer,
                      clock=env.clock).start_sign_in(email=BOB, client_ip=IP1)
    assert stale.start_sign_in(email=BOB, client_ip=IP1).status == "issued"


def test_missing_abuse_state_or_key_fails_closed_without_provider_call(env):
    env.service(init_abuse=False)
    with pytest.raises(AbuseUnavailable):
        env.svc.start_sign_in(email=ALICE, client_ip=IP1)
    with pytest.raises(AbuseUnavailable):
        env.svc.verify_sign_in(operation_id=uuid.uuid4(), candidate=None, code="12345678", client_ip=IP1)
    assert env.supabase.generate_calls == [] and env.count("auth_operations") == 0
    with pytest.raises(AbuseUnavailable):
        abuse.rotate_abuse_key(env.engine, make_keyring(), "nope")


def test_database_saturation_refuses_instead_of_admitting(env, monkeypatch):
    env.service()
    signed_in(env)
    monkeypatch.setattr("core.auth_f2.txn.LOCK_TIMEOUT", "200ms")
    with env.observer.connect() as holder:
        trans = holder.begin()
        holder.execute(sa.text("SELECT id FROM auth_abuse_subjects FOR UPDATE"))
        with pytest.raises(sa.exc.OperationalError):
            env.svc.verify_sign_in(operation_id=uuid.uuid4(), candidate=None, code="12345678", client_ip=IP1)
        trans.rollback()
    assert env.events("otp_verification_attempt") == 0 and env.supabase.verify_calls == []


def test_purge_keeps_active_windows_and_refuses_short_retention(env):
    env.service(fixed_clock=True)
    signed_in(env)
    with pytest.raises(ValueError):
        abuse.purge_abuse_history(env.engine, older_than=timedelta(hours=1))
    assert abuse.purge_abuse_history(env.engine, older_than=timedelta(hours=2), clock=env.clock) == (0, 0)
    env.clock.advance(hours=3)
    signed_in(env, email=BOB)  # même IP : son sujet garde un événement récent
    events, subjects = abuse.purge_abuse_history(env.engine, older_than=timedelta(hours=2), clock=env.clock)
    assert (events, subjects) == (2, 1) and env.count("auth_abuse_events") == 2
    assert env.count("auth_abuse_subjects") == 2
    env.clock.advance(minutes=1)
    assert signed_in(env).status == "issued" and env.count("auth_abuse_subjects") == 3


# --------------------------------------------------------------------------
# 10. Aucun secret en base, dans les résultats ni dans les journaux
# --------------------------------------------------------------------------

def test_no_secret_in_database_results_or_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.service()
    create_profile_with_identity(env.observer, env.supabase, ALICE)
    start = signed_in(env)
    code = env.mailer.last_code(ALICE)
    verify(env, start, wrong(code))
    done = verify(env, start, code)
    invitation = env.svc.create_invitation(cohort="beta")
    begin = env.svc.begin_invitation_activation(token=invitation.token)
    activated = env.svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate)
    secrets = [code, wrong(code), start.candidate.secret.reveal(), done.session.active_secret.reveal(),
               invitation.token.reveal(), begin.candidate.secret.reveal(), activated.session.active_secret.reveal()]
    with env.observer.connect() as c:
        dump = []
        for table in F2_TABLES + ("users",):
            for row in c.execute(sa.text(f"SELECT * FROM {table}")).all():
                dump.append(" ".join(str(v) for v in row))
    dump = "\n".join(dump)
    logs = caplog.text
    reprs = repr([start, done, invitation, begin, activated])
    for secret in secrets:
        assert secret not in dump and secret not in logs and secret not in reprs
    assert ALICE not in logs and IP1 not in logs
