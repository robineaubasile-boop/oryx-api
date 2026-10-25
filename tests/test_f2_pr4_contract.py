"""Contrat HTTP des sept routes de R1 PR 4 (C.4.5 §5 et §10), sans PostgreSQL.

Moteur et admission HTTP simulés : chaque double ÉCHOUE silencieusement
d'aucune manière, il enregistre ses appels, ce qui prouve qu'aucun refus de
transport n'atteint l'admission ni le moteur.

- La matrice livrée est EXACTEMENT la table normative C.4.5 §5.2 à §5.8.
- Chaque entrée est exercée par la route réelle : statut, JSON exact,
  cookies (nom, attributs, durée), en-têtes anti-cache.
- Triplets inconnus, d'un autre endpoint ou de PR 3, résultats de forme
  incohérente : 503 temporarily_unavailable sans cookie.
- Refus de transport (§10.2) : statut et JSON exacts, zéro Set-Cookie,
  zéro admission, zéro appel moteur ; ordre de priorité des défauts.
- Admission refusée : 429 rate_limited sans moteur ; admission en panne ou
  absente : 503 sans moteur.
- Durées des cookies (§10.3.2), candidat conservé (§10.3.3), aucune
  réponse PR 4 avec expires_in.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import INVITATION, SIGN_IN, CandidateCredential, Outcome, SessionCredential
from core.auth_f2_http import contract, contract_pr4
from core.auth_f2_http.client_ip import ClientIpUnavailable
from core.auth_f2_http.cookies import CSRF_BINDING, active_name, candidate_name
from core.auth_f2_http.isolated import make_app
from core.auth_f2_http.routes import Dependencies
from tests.f2_http_support import ORIGIN, Browser, StaticIp, csrf_keyring, set_cookies
from tests.test_f2_http_contract import CAND, GEN, OP, REFERENCE as PR3_REFERENCE, StubService, issued, secret
from tests.test_f2_http_transport import FakeClock

B, CO, RS = contract_pr4.BEGIN, contract_pr4.COMPLETE, contract_pr4.RESUME
LS, LR, LV, LX = contract_pr4.LINK_START, contract_pr4.LINK_RESEND, contract_pr4.LINK_VERIFY, contract_pr4.LINK_RESULT
CA, SE, OPR, ER = contract_pr4.CANDIDATE, contract_pr4.SESSION, contract_pr4.OPERATION, contract_pr4.ERROR
QUOTAS = ("email_blocked", "issue_interval", "quota_email_issue", "quota_ip_issue")
TOKEN = "invitation-token-value"


def _issue(endpoint):
    rows = {
        (endpoint, "issued", None): (202, "code_sent", OPR),
        (endpoint, "issue_uncertain", "generate_uncertain"): (503, "delivery_uncertain", OPR),
        (endpoint, "issue_uncertain", None): (503, "delivery_uncertain", OPR),
        (endpoint, "send_failed", "send_failed"): (503, "delivery_failed", OPR),
        (endpoint, "send_failed", "superseded"): (503, "delivery_failed", OPR),
        (endpoint, "send_failed", None): (503, "delivery_failed", OPR),
        (endpoint, "rejected", "ip_unavailable"): (503, "temporarily_unavailable", ER),
    }
    rows.update({(endpoint, "refused", q): (429, "rate_limited", ER) for q in QUOTAS})
    return rows


# Table normative C.4.5 §5.2 à §5.8, recopiée indépendamment du code.
REFERENCE = {
    (B, "candidate_issued", None): (202, "activation_started", CA),
    (B, "rejected", "invitation_unavailable"): (409, "invitation_unavailable", ER),
    (CO, "invitation_activated", None): (200, "invitation_activated", SE),
    **{(CO, "rejected", r): (409, "activation_unavailable", ER)
       for r in ("operation_unknown", "operation_closed", "credential_invalid", "invitation_unavailable")},
    (RS, "resumed", None): (200, "resumed", SE),
    (RS, "rejected", "resume_unavailable"): (409, "resume_unavailable", ER),
    (RS, "rejected", "key_unavailable"): (503, "temporarily_unavailable", ER),
    (RS, "refused", "quota_operation_resume"): (429, "rate_limited", ER),
    **_issue(LS),
    (LS, "rejected", "invalid_email"): (422, "invalid_email", ER),
    (LS, "rejected", "session_invalid"): (401, "session_invalid", ER),
    (LS, "rejected", "profile_already_linked"): (409, "profile_already_linked", ER),
    **_issue(LR),
    (LR, "rejected", "operation_unavailable"): (409, "operation_unavailable", ER),
    (LR, "rejected", "credential_invalid"): (409, "operation_unavailable", ER),
    (LV, "email_linked", "new_identity"): (200, "email_linked", OPR),
    (LV, "email_linked", "existing_identity"): (200, "email_linked", OPR),
    (LV, "refused", "quota_ip_verification"): (429, "rate_limited", ER),
    (LV, "refused", "email_blocked"): (429, "rate_limited", ER),
    (LV, "rejected", "email_blocked"): (429, "rate_limited", ER),
    (LV, "rejected", "ip_unavailable"): (503, "temporarily_unavailable", ER),
    (LV, "rejected", "key_unavailable"): (503, "temporarily_unavailable", ER),
    **{(LV, "rejected", r): (409, "operation_unavailable", ER)
       for r in ("operation_unknown", "operation_closed", "operation_expired", "credential_invalid")},
    (LV, "rejected", "no_active_code"): (409, "code_unavailable", ER),
    (LV, "rejected", "code_expired"): (409, "code_expired", ER),
    (LV, "rejected", "request_exhausted"): (409, "code_exhausted", ER),
    (LV, "rejected", "code_not_ready"): (409, "code_not_ready", ER),
    (LV, "rejected", "verification_in_progress"): (409, "verification_in_progress", ER),
    (LV, "rejected", "verification_uncertain"): (503, "verification_uncertain", ER),
    (LV, "uncertain", "provider_uncertain"): (503, "verification_uncertain", ER),
    (LV, "rejected", "code_malformed"): (401, "code_incorrect", ER),
    (LV, "rejected", "code_incorrect"): (401, "code_incorrect", ER),
    (LV, "rejected", "code_replaced"): (401, "code_replaced", ER),
    **{(LV, "rejected", r): (401, "linking_failed", ER)
       for r in ("provider_rejected", "proof_not_applicable", "verification_superseded")},
    (LV, "rejected", "profile_already_linked"): (409, "linking_unavailable", ER),
    (LV, "rejected", "conflict_other_profile"): (409, "linking_unavailable", ER),
    (LX, "email_linked", None): (200, "email_linked", OPR),
    (LX, "rejected", "session_invalid"): (401, "session_invalid", ER),
    (LX, "rejected", "result_unavailable"): (409, "result_unavailable", ER),
    (LX, "refused", "quota_operation_result"): (429, "rate_limited", ER),
    (LX, "rejected", "key_unavailable"): (503, "temporarily_unavailable", ER),
}
ACTIONS = {B: "invitation_begin", CO: "invitation_complete", RS: "invitation_resume", LS: "email_link_start",
           LR: "email_link_resend", LV: "email_link_verify", LX: "email_link_result"}
SECURITY_HEADERS = {"content-type": "application/json", "cache-control": "no-store", "pragma": "no-cache",
                    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer"}


def test_matrix_is_exactly_the_normative_table():
    assert {k: (r.http_status, r.code, r.kind) for k, r in contract_pr4.MATRIX.items()} == REFERENCE
    assert {k[0] for k in REFERENCE} == set(contract_pr4.ENDPOINTS)


def test_pr3_matrix_is_untouched_and_disjoint():
    assert {k: (r.http_status, r.code, r.kind) for k, r in contract.MATRIX.items()} == PR3_REFERENCE
    assert not {k[0] for k in contract.MATRIX} & set(contract_pr4.ENDPOINTS)


def test_only_activation_and_resume_produce_sessions_and_only_begin_a_candidate():
    assert {(e, s) for (e, s, _), r in contract_pr4.MATRIX.items() if r.kind == SE} == {
        (CO, "invitation_activated"), (RS, "resumed")}
    assert {(e, s) for (e, s, _), r in contract_pr4.MATRIX.items() if r.kind == CA} == {(B, "candidate_issued")}
    linking = {e for (e, _, _), r in contract_pr4.MATRIX.items() if r.kind in (SE, CA)}
    assert not linking & {LS, LR, LV, LX}


def test_expires_at_serialization_is_deterministic_utc():
    paris = timezone(timedelta(hours=2))
    assert contract_pr4.expires_at_text(datetime(2026, 11, 8, 11, 0, 0, tzinfo=paris)) == "2026-11-08T09:00:00.000000Z"
    assert contract_pr4.expires_at_text(datetime(2026, 11, 8, 9, 0, 0, 123, tzinfo=timezone.utc)) == (
        "2026-11-08T09:00:00.000123Z")


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------

class Pr4Stub(StubService):
    def begin_invitation_activation(self, **kw):
        return self._answer("begin", kw)

    def complete_invitation_activation(self, **kw):
        return self._answer("complete", kw)

    def start_email_linking(self, **kw):
        return self._answer("link_start", kw)

    def resend_email_linking_otp(self, **kw):
        return self._answer("link_resend", kw)

    def verify_email_linking(self, **kw):
        return self._answer("link_verify", kw)

    def read_email_linking_result(self, **kw):
        return self._answer("link_result", kw)


class StubAdmission:
    def __init__(self):
        self.result = True
        self.calls = []

    def admit(self, action, client_ip):
        self.calls.append((action, client_ip))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Env:
    def __init__(self, *, resolver=None, admission=True):
        self.service = Pr4Stub()
        self.admission = StubAdmission() if admission else None
        self.clock = FakeClock()
        self.resolver = resolver or StaticIp()
        self.app = make_app(Dependencies(self.service, csrf_keyring(), self.resolver, frozenset({ORIGIN}),
                                         self.clock.time, self.clock.monotonic, self.admission))
        self.browser = Browser(self.app)
        self.browser.bootstrap()

    def nothing_reached(self):
        return self.service.calls == [] and (self.admission is None or self.admission.calls == [])


@pytest.fixture
def env():
    return Env()


CANDIDATE_SECRET = secret(5).reveal()
ACTIVE_SECRET = secret(7).reveal()
LINK = {"generation_id": str(GEN), "operation_id": str(OP)}
BODIES = {
    B: {"token": TOKEN},
    CO: {"operation_id": str(OP), "candidate_id": str(CAND)},
    RS: {"operation_id": str(OP), "candidate_id": str(CAND)},
    LS: {"generation_id": str(GEN), "email": "alice@example.com"},
    LR: dict(LINK),
    LV: {**LINK, "code": "12345678"},
    LX: dict(LINK),
}


def drive(env, endpoint, body=None, **kwargs):
    if endpoint in (CO, RS):
        env.browser.jar[candidate_name(CAND)] = CANDIDATE_SECRET
    if endpoint in (LS, LR, LV, LX):
        env.browser.jar[active_name(GEN)] = ACTIVE_SECRET
    return env.browser.post("/" + endpoint, BODIES[endpoint] if body is None else body, **kwargs)


def fresh(env, status, **over):
    """IssuedSession cohérente avec l'horloge simulée de la couche HTTP."""
    over.setdefault("absolute_expires_at", datetime.fromtimestamp(env.clock.wall, timezone.utc) + timedelta(days=15))
    return issued("resumed" if status == "resumed" else "authenticated", **over)


def outcome_for(env, endpoint, status, reason, kind):
    if kind == CA:
        return Outcome(status, reason, OP, CandidateCredential(CAND, secret(5)))
    if kind == SE:
        return Outcome(status, reason, OP, session=fresh(env, status))
    return Outcome(status, reason, OP)


def assert_headers(response):
    for key, value in SECURITY_HEADERS.items():
        assert response.headers[key] == value, key


# --------------------------------------------------------------------------
# Chaque entrée de la matrice par la route réelle
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(REFERENCE, key=repr), ids=lambda k: "|".join(map(str, k)))
def test_each_matrix_entry_through_the_route(env, key):
    endpoint, status, reason = key
    http_status, code, kind = REFERENCE[key]
    env.service.result = outcome_for(env, endpoint, status, reason, kind)
    response = drive(env, endpoint)
    assert response.status_code == http_status, response.text
    assert_headers(response)
    assert env.admission.calls == [(ACTIONS[endpoint], "203.0.113.7")]
    assert len(env.service.calls) == 1
    body = response.json()
    cookies = set_cookies(response)
    if kind == ER:
        assert body == {"error": code} and cookies == []
    elif kind == CA:
        assert body == {"status": code, "operation_id": str(OP), "candidate_id": str(CAND)}
        assert [c["name"] for c in cookies] == [candidate_name(CAND)]
        assert cookies[0]["attrs"] == {"max-age": "900", "path": "/", "secure": "", "httponly": "", "samesite": "Lax"}
        assert cookies[0]["value"] == CANDIDATE_SECRET
    elif kind == OPR:
        assert body == {("status" if http_status < 400 else "error"): code, "operation_id": str(OP)}
        assert cookies == []
    else:
        session = env.service.result.session
        assert body == {"status": code, "operation_id": str(OP), "generation_id": str(GEN),
                        "expires_at": contract_pr4.expires_at_text(session.absolute_expires_at)}
        assert "expires_in" not in body
        # Seul l'actif est posé ; le candidat est conservé tel quel.
        assert [c["name"] for c in cookies] == [active_name(GEN)]
        assert cookies[0]["value"] == secret(9).reveal()
        assert set(cookies[0]["attrs"]) == {"max-age", "path", "secure", "httponly", "samesite"}
        assert env.browser.jar[candidate_name(CAND)] == CANDIDATE_SECRET
    # Aucun secret ni identifiant de profil dans le corps (les secrets ne
    # voyagent que dans Set-Cookie).
    for value in (CANDIDATE_SECRET, ACTIVE_SECRET, secret(9).reveal(), TOKEN, "12345678", "user-1"):
        assert value not in response.text
    if code in ("code_sent", "delivery_uncertain", "delivery_failed"):
        assert ("code_sent" in response.text) is (code == "code_sent")


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_engine_receives_exactly_the_designated_credentials(env, endpoint):
    env.service.result = Outcome("weird", None)
    drive(env, endpoint)
    ((name, kwargs),) = env.service.calls
    if endpoint == B:
        assert name == "begin" and set(kwargs) == {"token"} and kwargs["token"].reveal() == TOKEN
    elif endpoint == CO:
        assert name == "complete" and set(kwargs) == {"operation_id", "candidate"}
        assert kwargs["candidate"] == CandidateCredential(CAND, Redacted(CANDIDATE_SECRET))
    elif endpoint == RS:
        assert name == "resume" and kwargs["expected_kind"] == INVITATION and kwargs["client_ip"] == "203.0.113.7"
        assert kwargs["operation_id"] == OP and kwargs["candidate"].candidate_id == CAND
    else:
        assert kwargs["session"] == SessionCredential(GEN, Redacted(ACTIVE_SECRET))
        assert kwargs["client_ip"] == "203.0.113.7"
        if endpoint != LS:
            assert kwargs["operation_id"] == OP
    # La consultation du résultat n'authentifie jamais la session (aucune
    # confirmation de livraison) : seul read_email_linking_result est appelé.
    assert all(call[0] != "authenticate" for call in env.service.calls)


def test_sign_in_resume_now_requires_the_sign_in_kind(env):
    """Seul changement PR 3 intentionnel (C.4.5 §6)."""
    env.browser.jar[candidate_name(CAND)] = CANDIDATE_SECRET
    env.service.result = Outcome("resumed", None, OP, session=fresh(env, "resumed"))
    response = env.browser.post("/sign-in/resume", {"operation_id": str(OP), "candidate_id": str(CAND)})
    assert response.status_code == 200 and set(response.json()) == {"status", "generation_id", "expires_in"}
    ((name, kwargs),) = env.service.calls
    assert name == "resume" and kwargs["expected_kind"] == SIGN_IN and "client_ip" not in kwargs
    assert env.admission.calls == []  # aucune admission PR 4 sur les routes PR 3


# --------------------------------------------------------------------------
# Refus fermé : triplets inconnus, formes incohérentes
# --------------------------------------------------------------------------

UNKNOWN_RESULTS = [
    Outcome("rejected", "something_new", OP), Outcome("rejected", None, OP), Outcome("weird", None, OP),
    Outcome("rejected", {"reason": "dict"}, OP), Outcome(None, None), "not an outcome", None,
    Outcome("authenticated", None, OP, session=issued()), Outcome("rejected", "identity_not_linked", OP),
    Outcome("rejected", "resume_unavailable", OP), RuntimeError("boom"), TimeoutError("db"),
]


@pytest.mark.parametrize("endpoint,result", [
    (e, r) for e in contract_pr4.ENDPOINTS for r in UNKNOWN_RESULTS
    if not (isinstance(r, Outcome) and isinstance(r.reason, (str, type(None)))
            and (e, r.status, r.reason) in REFERENCE)], ids=repr)
def test_unknown_results_are_503_without_cookie(env, endpoint, result):
    env.service.result = result
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []
    assert_headers(response)


@pytest.mark.parametrize("endpoint,key", [
    (e, k) for e in contract_pr4.ENDPOINTS for k in sorted(set(REFERENCE) | set(PR3_REFERENCE), key=repr)
    if (e, k[1], k[2]) not in REFERENCE], ids=repr)
def test_triplets_of_another_endpoint_are_unknown(env, endpoint, key):
    other, status, reason = key
    session = issued(status) if status in ("authenticated", "resumed", "invitation_activated") else None
    candidate = CandidateCredential(CAND, secret(5)) if status in ("candidate_issued", "issued") and other in (
        B, contract.START) else None
    env.service.result = Outcome(status, reason, OP, candidate, session)
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []


@pytest.mark.parametrize("key", sorted((k for k, v in REFERENCE.items() if v[2] != SE), key=repr),
                         ids=lambda k: "|".join(map(str, k)))
def test_non_session_branches_never_set_an_active_cookie(env, key):
    endpoint, status, reason = key
    env.service.result = Outcome(status, reason, OP, session=issued())
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []


@pytest.mark.parametrize("key", sorted((k for k, v in REFERENCE.items() if v[2] in (ER, OPR)), key=repr),
                         ids=lambda k: "|".join(map(str, k)))
def test_no_branch_but_begin_may_add_a_candidate(env, key):
    endpoint, status, reason = key
    env.service.result = Outcome(status, reason, OP, CandidateCredential(uuid.uuid4(), secret(6)))
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []


@pytest.mark.parametrize("endpoint,status", [(CO, "invitation_activated"), (RS, "resumed")])
@pytest.mark.parametrize("broken", [
    "no_session", "wrong_resumed_flag", "other_operation", "outcome_other_operation", "bad_secret", "not_redacted",
    "naive_expiry", "with_candidate", "session_not_issued", "past_expiry"])
def test_inconsistent_session_outcomes_fail_closed(env, endpoint, status, broken):
    def make(**over):
        return fresh(env, status, **over)

    session, candidate, operation = make(), None, OP
    if broken == "no_session":
        session = None
    elif broken == "wrong_resumed_flag":
        session = fresh(env, "invitation_activated" if status == "resumed" else "resumed")
    elif broken == "other_operation":
        session = make(operation_id=uuid.uuid4())
    elif broken == "outcome_other_operation":
        operation = uuid.uuid4()
    elif broken == "bad_secret":
        session = make(active_secret=Redacted("x" * 43))
    elif broken == "not_redacted":
        session = make(active_secret=secret(9).reveal())
    elif broken == "naive_expiry":
        session = make(absolute_expires_at=datetime.utcnow() + timedelta(days=1))
    elif broken == "with_candidate":
        candidate = CandidateCredential(CAND, secret(5))
    elif broken == "session_not_issued":
        session = object()
    elif broken == "past_expiry":
        session = make(absolute_expires_at=datetime.fromtimestamp(env.clock.wall, timezone.utc) - timedelta(seconds=1))
    env.service.result = Outcome(status, None, operation, candidate, session)
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []
    assert env.browser.jar[candidate_name(CAND)] == CANDIDATE_SECRET


@pytest.mark.parametrize("endpoint", [LS, LR, LV, LX])
def test_operation_renders_require_a_coherent_operation(env, endpoint):
    status, reason = {LS: ("issue_uncertain", None), LR: ("send_failed", None), LV: ("email_linked", "new_identity"),
                      LX: ("email_linked", None)}[endpoint]
    for operation in (None, str(OP), uuid.uuid4() if endpoint != LS else None):
        env.service.result = Outcome(status, reason, operation)
        response = drive(env, endpoint)
        # F sans operation_id cohérent : 503 générique, jamais delivery_*.
        assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
        assert set_cookies(response) == []
    env.service.result = Outcome(status, reason, OP, session=issued())
    assert drive(env, endpoint).json() == {"error": "temporarily_unavailable"}


def test_begin_requires_a_complete_new_candidate(env):
    for broken in (None, CandidateCredential(str(CAND), secret(5)), CandidateCredential(CAND, "raw"),
                   CandidateCredential(CAND, Redacted("short"))):
        env.service.result = Outcome("candidate_issued", None, OP, broken)
        response = drive(env, B)
        assert response.status_code == 503 and set_cookies(response) == []
    env.service.result = Outcome("candidate_issued", None, None, CandidateCredential(CAND, secret(5)))
    assert drive(env, B).status_code == 503


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_engine_exception_is_503_without_detail(env, endpoint, caplog):
    caplog.set_level(logging.DEBUG)
    env.service.result = RuntimeError("password=hunter2 alice@example.com")
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert "hunter2" not in caplog.text and "alice" not in caplog.text and set_cookies(response) == []


# --------------------------------------------------------------------------
# Admission HTTP
# --------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_refused_admission_is_429_without_engine_nor_cookie(env, endpoint):
    env.admission.result = False
    env.service.result = outcome_for(env, endpoint, *next((k[1], k[2], v[2]) for k, v in REFERENCE.items()
                                                      if k[0] == endpoint and v[2] != ER))
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert set_cookies(response) == [] and env.service.calls == []
    assert env.admission.calls == [(ACTIONS[endpoint], "203.0.113.7")]
    assert_headers(response)


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
@pytest.mark.parametrize("failure", [RuntimeError("lock timeout"), TimeoutError("commit"), None, "yes", 1])
def test_failed_or_uncertain_admission_never_reaches_the_engine(env, endpoint, failure):
    env.admission.result = failure
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == [] and env.service.calls == []


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_missing_admission_fails_closed(endpoint):
    env = Env(admission=False)
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert env.service.calls == []


# --------------------------------------------------------------------------
# Refus de transport (C.4.5 §10.2) : zéro admission, zéro moteur, zéro cookie
# --------------------------------------------------------------------------

def assert_transport_refusal(env, response, status, code):
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert set_cookies(response) == []
    assert_headers(response)
    assert env.nothing_reached()


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
@pytest.mark.parametrize("origin,headers", [
    (None, {}), (None, {"sec-fetch-site": "same-origin"}), ("https://evil.test", {}),
    (ORIGIN, {"sec-fetch-site": "cross-site"})])
def test_provenance_is_refused_first(env, endpoint, origin, headers):
    response = drive(env, endpoint, origin=origin, headers=headers, csrf=False, body={"x": 1})
    assert_transport_refusal(env, response, 403, "origin_refused")


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_strict_cookie_parser_refusal(env, endpoint):
    bad = f"__Host-oryx-candidate-{str(CAND)[:-1]}A"  # UUID non canonique (majuscule)
    env.browser.jar[bad] = CANDIDATE_SECRET
    assert_transport_refusal(env, drive(env, endpoint, csrf=False), 400, "invalid_request")
    env.browser.jar.pop(bad)
    for i in range(16):
        env.browser.jar[candidate_name(uuid.UUID(int=i + 1))] = CANDIDATE_SECRET
    assert_transport_refusal(env, drive(env, endpoint), 400, "invalid_request")  # 17 cookies Oryx et plus


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_csrf_is_required(env, endpoint):
    assert_transport_refusal(env, drive(env, endpoint, csrf=False), 403, "csrf_invalid")
    token = env.browser.csrf
    assert_transport_refusal(env, drive(env, endpoint, headers={"x-oryx-csrf": token + "x"}), 403, "csrf_invalid")
    env.browser.jar.pop(CSRF_BINDING)
    assert_transport_refusal(env, drive(env, endpoint), 403, "csrf_invalid")


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
@pytest.mark.parametrize("content,ctype,status,code", [
    (b'{"token":"x"}', "text/plain", 415, "invalid_request"),
    (b'{"token":"x"}', "application/json; charset=latin-1", 415, "invalid_request"),
    (b'{"a":"' + b"x" * 4100 + b'"}', "application/json", 413, "request_too_large"),
    (b"{", "application/json", 400, "invalid_request"),
    (b"\xff\xfe", "application/json", 400, "invalid_request"),
    (b'[]', "application/json", 400, "invalid_request"),
    (b'{"x":NaN}', "application/json", 400, "invalid_request"),
    (b'{"token":"a","token":"b"}', "application/json", 400, "invalid_request"),
])
def test_body_contract(env, endpoint, content, ctype, status, code):
    if endpoint in (CO, RS):
        env.browser.jar[candidate_name(CAND)] = CANDIDATE_SECRET
    env.browser.jar[active_name(GEN)] = ACTIVE_SECRET
    response = env.browser.request("POST", "/" + endpoint, content=content, headers={"content-type": ctype})
    assert_transport_refusal(env, response, status, code)


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_missing_content_type_and_ambiguous_length(env, endpoint):
    response = env.browser.request("POST", "/" + endpoint, content=b"{}")
    assert_transport_refusal(env, response, 415, "invalid_request")
    response = env.browser.request("POST", "/" + endpoint, content=b"{}", headers={
        "content-type": "application/json", "content-length": "4097"})
    assert response.status_code in (400, 413)  # en-tête forgé incohérent : jamais admis
    assert env.nothing_reached()


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_exact_keys_are_required(env, endpoint):
    body = BODIES[endpoint]
    assert_transport_refusal(env, drive(env, endpoint, body={**body, "user_id": "u"}), 400, "invalid_request")
    first = sorted(body)[0]
    assert_transport_refusal(env, drive(env, endpoint, body={k: v for k, v in body.items() if k != first}), 400,
                             "invalid_request")


@pytest.mark.parametrize("endpoint,field,value", [
    (B, "token", 1), (B, "token", None), (B, "token", ["x"]),
    *[(e, f, v) for e in (CO, RS) for f in ("operation_id", "candidate_id")
      for v in (str(OP)[:-1] + "A", "{" + str(OP) + "}", str(OP).replace("-", ""), 1, None)],
    *[(e, "generation_id", v) for e in (LS, LR, LV, LX) for v in (str(GEN)[:-1] + "B", 7, "x")],
    *[(e, "operation_id", v) for e in (LR, LV, LX) for v in (str(OP)[:-1], str(OP)[:-1] + "F", " " + str(OP))],
    (LS, "email", "a" * 321), (LS, "email", 5), (LS, "email", None), (LV, "code", 12345678), (LV, "code", None),
])
def test_field_types_and_bounds(env, endpoint, field, value):
    response = drive(env, endpoint, body={**BODIES[endpoint], field: value})
    assert_transport_refusal(env, response, 400, "invalid_request")


def test_email_of_exactly_320_characters_and_any_code_string_reach_the_engine(env):
    env.service.result = Outcome("rejected", "invalid_email")
    assert drive(env, LS, body={**BODIES[LS], "email": "a" * 320}).status_code == 422
    for code in ("", "abc", "1" * 500, " 12345678 "):
        env.service.calls.clear()
        env.service.result = Outcome("rejected", "code_malformed", OP)
        assert drive(env, LV, body={**BODIES[LV], "code": code}).json() == {"error": "code_incorrect"}
        assert env.service.calls[0][1]["code"] == code


@pytest.mark.parametrize("endpoint", [CO, RS])
def test_candidate_routes_need_the_exact_candidate_cookie(env, endpoint):
    other = uuid.uuid4()
    env.browser.jar[candidate_name(other)] = CANDIDATE_SECRET
    env.browser.jar[active_name(GEN)] = ACTIVE_SECRET
    response = env.browser.post("/" + endpoint, BODIES[endpoint])
    assert_transport_refusal(env, response, 401, "credential_missing")


@pytest.mark.parametrize("endpoint", [LS, LR, LV, LX])
def test_session_routes_need_the_exact_active_cookie(env, endpoint):
    env.browser.jar[active_name(uuid.uuid4())] = ACTIVE_SECRET
    env.browser.jar[candidate_name(CAND)] = CANDIDATE_SECRET
    response = env.browser.post("/" + endpoint, BODIES[endpoint])
    assert_transport_refusal(env, response, 401, "session_invalid")


def test_begin_reserves_the_candidate_and_its_future_active_cookie(env):
    # count + 1 + len(candidates) + 1 <= 16 (convention PR 3 _room_for).
    for i in range(6):
        env.browser.jar[candidate_name(uuid.UUID(int=i + 1))] = CANDIDATE_SECRET
    for i in range(1):
        env.browser.jar[active_name(uuid.UUID(int=100 + i))] = ACTIVE_SECRET
    # 1 liaison + 6 candidats + 1 actif = 8 ; 8 + 1 + 6 + 1 = 16 : admis.
    env.service.result = Outcome("rejected", "invitation_unavailable")
    assert drive(env, B).status_code == 409 and env.service.calls
    env.service.calls.clear()
    env.admission.calls.clear()
    env.browser.jar[active_name(uuid.UUID(int=200))] = ACTIVE_SECRET  # 9 + 1 + 6 + 1 = 17
    assert_transport_refusal(env, drive(env, B), 409, "credential_limit")


@pytest.mark.parametrize("endpoint", contract_pr4.ENDPOINTS)
def test_unreliable_ip_is_refused_before_admission(endpoint):
    class Raising:
        calls = 0

        def __call__(self, request):
            raise ClientIpUnavailable("non fiable")

    env = Env(resolver=Raising())
    assert_transport_refusal(env, drive(env, endpoint), 503, "temporarily_unavailable")


def test_defect_priority_is_fields_then_cookie_then_capacity_then_ip():
    class Raising:
        def __call__(self, request):
            raise ClientIpUnavailable("non fiable")

    env = Env(resolver=Raising())
    # Champ invalide ET cookie absent : 400 (les champs d'abord).
    response = env.browser.post("/" + LV, {**LINK, "code": 1})
    assert_transport_refusal(env, response, 400, "invalid_request")
    response = env.browser.post("/" + CO, {"operation_id": "x", "candidate_id": str(CAND)})
    assert_transport_refusal(env, response, 400, "invalid_request")
    # Cookie absent ET IP indisponible : 401 (le cookie d'abord).
    assert_transport_refusal(env, env.browser.post("/" + CO, BODIES[CO]), 401, "credential_missing")
    assert_transport_refusal(env, env.browser.post("/" + LX, BODIES[LX]), 401, "session_invalid")
    # Capacité ET IP : 409 (la capacité d'abord).
    for i in range(15):
        env.browser.jar[candidate_name(uuid.UUID(int=i + 1))] = CANDIDATE_SECRET
    assert_transport_refusal(env, env.browser.post("/" + B, BODIES[B]), 409, "credential_limit")
    # Champs dans l'ordre du schéma : generation_id invalide ET e-mail trop long.
    assert_transport_refusal(env, env.browser.post("/" + LS, {"generation_id": "x", "email": 1}), 400,
                             "invalid_request")


def test_ip_is_resolved_only_after_every_transport_check():
    resolver = StaticIp()
    env = Env(resolver=resolver)
    for endpoint in contract_pr4.ENDPOINTS:
        drive(env, endpoint, csrf=False)
        env.browser.post("/" + endpoint, {"x": 1})
    assert resolver.calls == 0 and env.nothing_reached()


# --------------------------------------------------------------------------
# Durées et conservation des cookies (C.4.5 §10.3)
# --------------------------------------------------------------------------

def _after(env, seconds, outcome):
    def answer(kw):
        env.clock.advance(seconds)
        return outcome
    return answer


@pytest.mark.parametrize("elapsed,expected", [(0, 900), (0.4, 899), (30.5, 869), (898.9, 1)])
def test_candidate_cookie_lives_at_most_15_minutes_from_request_entry(env, elapsed, expected):
    issued_candidate = Outcome("candidate_issued", None, OP, CandidateCredential(CAND, secret(5)))
    env.service.result = _after(env, elapsed, issued_candidate)
    (cookie,) = set_cookies(drive(env, B))
    assert cookie["attrs"] == {"max-age": str(expected), "path": "/", "secure": "", "httponly": "", "samesite": "Lax"}


def test_spent_candidate_lifetime_gives_503_without_cookie(env):
    issued_candidate = Outcome("candidate_issued", None, OP, CandidateCredential(CAND, secret(5)))
    env.service.result = _after(env, 899.5, issued_candidate)
    response = drive(env, B)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []


@pytest.mark.parametrize("endpoint,status,elapsed,expected", [
    (CO, "invitation_activated", 0, 1296000), (CO, "invitation_activated", 2.5, 1296000 - 3),
    (RS, "resumed", 0, 1295700), (RS, "resumed", 1.2, 1295700 - 2)])
def test_active_cookie_bounds(env, endpoint, status, elapsed, expected):
    expires = datetime.fromtimestamp(env.clock.wall, timezone.utc) + timedelta(days=15, seconds=elapsed)
    session = issued("resumed" if status == "resumed" else "authenticated", absolute_expires_at=expires)
    env.service.result = _after(env, elapsed, Outcome(status, None, OP, session=session))
    response = drive(env, endpoint)
    (active,) = set_cookies(response)
    assert int(active["attrs"]["max-age"]) == expected
    assert response.json()["expires_at"] == contract_pr4.expires_at_text(expires)
    assert "expires_in" not in response.json()


@pytest.mark.parametrize("endpoint,status", [(CO, "invitation_activated"), (RS, "resumed")])
def test_active_cookie_is_bounded_by_the_absolute_expiry(env, endpoint, status):
    expires = datetime.fromtimestamp(env.clock.wall, timezone.utc) + timedelta(hours=1, seconds=0.9)
    session = issued("resumed" if status == "resumed" else "authenticated", absolute_expires_at=expires)
    env.service.result = Outcome(status, None, OP, session=session)
    (active,) = set_cookies(drive(env, endpoint))
    assert active["attrs"]["max-age"] == "3600"
    env.service.result = Outcome(status, None, OP, session=type(session)(**{
        **session.__dict__, "absolute_expires_at": datetime.fromtimestamp(env.clock.wall, timezone.utc)
        + timedelta(seconds=0.5)}))
    response = drive(env, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == []


def test_logs_and_bodies_never_contain_secrets(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.service.result = Outcome("candidate_issued", None, OP, CandidateCredential(CAND, secret(5)))
    begin = drive(env, B)
    env.service.result = Outcome("invitation_activated", None, OP, session=fresh(env, "invitation_activated"))
    complete = drive(env, CO)
    env.service.result = Outcome("email_linked", "new_identity", OP)
    verify = drive(env, LV, body={**LINK, "code": "87654321"})
    for value in (TOKEN, "87654321", "alice@example.com", secret(5).reveal(), secret(9).reveal(), ACTIVE_SECRET,
                  env.browser.jar[CSRF_BINDING], env.browser.csrf, "203.0.113.7"):
        assert value not in caplog.text
    for response in (complete, verify):
        assert secret(9).reveal() not in response.text and CANDIDATE_SECRET not in response.text
    assert TOKEN not in begin.text and "user" not in complete.text and "new_identity" not in verify.text
