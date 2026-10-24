"""Contrat des résultats HTTP F2 (R1 PR 3) : matrice exhaustive
(endpoint, Outcome.status, Outcome.reason), sans PostgreSQL.

- La matrice livrée est EXACTEMENT la table de référence ci-dessous.
- Chaque Outcome que core/auth_f2/service.py peut produire (inventaire
  par analyse syntaxique, valeurs dynamiques comprises) est soit dans la
  matrice, soit propre aux parcours invitation / liaison (absents de PR 3)
  et alors traduit en 503.
- Chaque entrée est exercée de bout en bout par la route réelle, avec un
  moteur simulé : statut, corps, cookies.
- Seuls authenticated / resumed avec une IssuedSession complète et
  cohérente posent un cookie actif ; toute autre branche, toute
  incohérence et tout triplet inconnu donnent 503 sans cookie actif.
"""
import ast
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.auth_f2.crypto import Redacted, b64url_encode
from core.auth_f2.service import CandidateCredential, IssuedSession, Outcome
from core.auth_f2_http import contract
from core.auth_f2_http.cookies import active_name, candidate_name
from core.auth_f2_http.isolated import make_app
from core.auth_f2_http.routes import Dependencies
from tests.f2_http_support import ORIGIN, Browser, StaticIp, active_cookies_set, csrf_keyring, set_cookies
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

S, R, V, U = contract.START, contract.RESEND, contract.VERIFY, contract.RESUME
NC, PG, SE, ER = contract.NEW_CANDIDATE, contract.PROGRESS, contract.SESSION, contract.ERROR
QUOTAS = ("email_blocked", "issue_interval", "quota_email_issue", "quota_ip_issue")

REFERENCE = {
    **{(e, "issued", None): (202, "code_sent", k) for e, k in ((S, NC), (R, PG))},
    **{(e, "issue_uncertain", r): (503, "delivery_uncertain", k) for e, k in ((S, NC), (R, PG))
       for r in ("generate_uncertain", None)},
    **{(e, "send_failed", r): (503, "delivery_failed", k) for e, k in ((S, NC), (R, PG))
       for r in ("send_failed", "superseded", None)},
    **{(e, "rejected", "ip_unavailable"): (503, "temporarily_unavailable", ER) for e in (S, R, V)},
    **{(e, "refused", q): (429, "rate_limited", ER) for e in (S, R) for q in QUOTAS},
    (S, "rejected", "invalid_email"): (422, "invalid_email", ER),
    (R, "rejected", "operation_unavailable"): (409, "operation_unavailable", ER),
    (R, "rejected", "credential_invalid"): (409, "operation_unavailable", ER),
    (V, "authenticated", None): (200, "authenticated", SE),
    (V, "resumed", None): (200, "resumed", SE),
    (V, "refused", "quota_ip_verification"): (429, "rate_limited", ER),
    (V, "refused", "email_blocked"): (429, "rate_limited", ER),
    (V, "rejected", "email_blocked"): (429, "rate_limited", ER),
    **{(V, "rejected", r): (409, "operation_unavailable", ER)
       for r in ("operation_unknown", "operation_closed", "operation_expired", "credential_invalid")},
    (V, "rejected", "no_active_code"): (409, "code_unavailable", ER),
    (V, "rejected", "code_expired"): (409, "code_expired", ER),
    (V, "rejected", "request_exhausted"): (409, "code_exhausted", ER),
    (V, "rejected", "code_not_ready"): (409, "code_not_ready", ER),
    (V, "rejected", "verification_in_progress"): (409, "verification_in_progress", ER),
    (V, "rejected", "verification_uncertain"): (503, "verification_uncertain", ER),
    (V, "uncertain", "provider_uncertain"): (503, "verification_uncertain", ER),
    (V, "rejected", "code_malformed"): (401, "code_incorrect", ER),
    (V, "rejected", "code_incorrect"): (401, "code_incorrect", ER),
    (V, "rejected", "code_replaced"): (401, "code_replaced", ER),
    (V, "rejected", "key_unavailable"): (503, "temporarily_unavailable", ER),
    **{(V, "rejected", r): (401, "sign_in_failed", ER) for r in (
        "provider_rejected", "proof_not_applicable", "identity_not_linked", "verification_superseded",
        "resume_unavailable")},
    (U, "resumed", None): (200, "resumed", SE),
    (U, "rejected", "resume_unavailable"): (409, "resume_unavailable", ER),
    (U, "rejected", "key_unavailable"): (503, "temporarily_unavailable", ER),
}

# Motifs et états des parcours hors PR 3 (invitation, liaison e-mail ; R1 PR 4 :
# résultat de liaison, quotas par opération) : jamais dans la matrice PR 3,
# donc 503 s'ils parvenaient à une route de connexion (matrice PR 4 :
# tests/test_f2_pr4_contract.py).
OUT_OF_SCOPE = {
    ("rejected", "session_invalid"), ("rejected", "profile_already_linked"), ("rejected", "invitation_unavailable"),
    ("rejected", "conflict_other_profile"), ("candidate_issued", None), ("invitation_activated", None),
    ("email_linked", "dynamic"), ("rejected", "result_unavailable"),
}


def test_matrix_is_exactly_the_reference_table():
    assert {k: (r.http_status, r.code, r.kind) for k, r in contract.MATRIX.items()} == REFERENCE


def test_only_authenticated_and_resumed_are_session_rules():
    sessions = {(s, r) for (_, s, r), rule in contract.MATRIX.items() if rule.kind == SE}
    assert sessions == {("authenticated", None), ("resumed", None)}


def _service_outcomes():
    """(status, reason) de chaque Outcome(...) de service.py ; reason
    'dynamic' pour une expression non constante, avec son texte."""
    tree = ast.parse((REPO_ROOT / "core" / "auth_f2" / "service.py").read_text(encoding="utf-8"))
    found, dynamic = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Outcome":
            status = node.args[0]
            reason = node.args[1] if len(node.args) > 1 else None
            status_v = status.value if isinstance(status, ast.Constant) else "dynamic:" + ast.unparse(status)
            if reason is None or isinstance(reason, ast.Constant):
                found.add((status_v, reason.value if reason is not None else None))
            else:
                dynamic.add((status_v, ast.unparse(reason)))
    return found, dynamic


def test_every_engine_outcome_is_mapped_or_out_of_scope():
    found, dynamic = _service_outcomes()
    # Expressions dynamiques connues ; une nouvelle expression doit être
    # inventoriée ici et dans la matrice.
    assert dynamic == {
        ("refused", "refused"),            # quotas d'émission (abuse.issue_refusals) et refus de réservation
        ("send_failed", "bound"),          # bind_code : send_failed | superseded
        ("rejected", "op['failure_code']"),  # rejeu : motif persisté de l'opération
        ("rejected", "reason"),            # _certain_failure : code_malformed | code_incorrect -> email_blocked |
                                           # request_exhausted
        ("rejected", "'verification_uncertain' if request['status'] == 'verify_uncertain' else 'code_not_ready'"),
        ("email_linked", "resolution"),
        ("email_linked", "op['identity_resolution']"),
        ("refused", "reason"),             # R1 PR 4, _debit_operation : quota_operation_resume |
                                           # quota_operation_result (hors matrice PR 3)
    }
    for reason in ("send_failed", "superseded"):
        assert (S, "send_failed", reason) in contract.MATRIX and (R, "send_failed", reason) in contract.MATRIX
    for reason in ("code_malformed", "code_incorrect", "email_blocked", "request_exhausted", "verification_uncertain",
                   "code_not_ready"):
        assert (V, "rejected", reason) in contract.MATRIX
    assert ("dynamic:status", None) in found  # record_send : issued | issue_uncertain | send_failed
    mapped = {(s, r) for (_, s, r) in contract.MATRIX}
    for status, reason in found:
        if status == "dynamic:status":
            assert {("issued", None), ("issue_uncertain", None), ("send_failed", None)} <= mapped
        elif status == "email_linked":
            assert (status, "dynamic") in OUT_OF_SCOPE
        else:
            assert (status, reason) in mapped or (status, reason) in OUT_OF_SCOPE, (status, reason)
    for _, reason in {(s, r) for s, r in OUT_OF_SCOPE}:
        assert all(r != reason for (_, _, r) in contract.MATRIX if reason not in (None, "dynamic"))
    # Refus de quota d'émission : chaque motif de abuse.issue_refusals.
    abuse_tree = ast.parse((REPO_ROOT / "core" / "auth_f2" / "abuse.py").read_text(encoding="utf-8"))
    refusal_reasons = {n.elts[1].value for n in ast.walk(abuse_tree) if isinstance(n, ast.Tuple) and len(n.elts) == 2
                       and isinstance(n.elts[1], ast.Constant) and isinstance(n.elts[1].value, str)
                       and n.elts[1].value in QUOTAS + ("quota_ip_verification",)}
    assert refusal_reasons == set(QUOTAS)
    for endpoint in (S, R):
        for reason in QUOTAS:
            assert (endpoint, "refused", reason) in contract.MATRIX
    # Motif persisté (rejeu) pour la connexion : seul identity_not_linked.
    assert (V, "rejected", "identity_not_linked") in contract.MATRIX


# --------------------------------------------------------------------------
# Moteur simulé et exercice de chaque entrée par la route réelle
# --------------------------------------------------------------------------

def secret(n=1):
    return Redacted(b64url_encode(bytes([n]) * 32))


class StubService:
    def __init__(self):
        self.result = None
        self.calls = []

    def _answer(self, name, kwargs):
        self.calls.append((name, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result(kwargs) if callable(self.result) else self.result

    def start_sign_in(self, **kw):
        return self._answer("start", kw)

    def resend_sign_in_otp(self, **kw):
        return self._answer("resend", kw)

    def verify_sign_in(self, **kw):
        return self._answer("verify", kw)

    def resume_result(self, **kw):
        return self._answer("resume", kw)

    def authenticate_session(self, credential):
        return self._answer("authenticate", {"credential": credential})

    def revoke_session(self, session_id, reason):
        return self._answer("revoke", {"session_id": session_id, "reason": reason})


@pytest.fixture
def stub():
    service = StubService()
    app = make_app(Dependencies(service, csrf_keyring(), StaticIp(), frozenset({ORIGIN}), time.time, time.monotonic))
    browser = Browser(app)
    browser.bootstrap()
    return service, browser


OP = uuid.UUID("11111111-1111-4111-8111-111111111111")
CAND = uuid.UUID("22222222-2222-4222-8222-222222222222")
GEN = uuid.UUID("33333333-3333-4333-8333-333333333333")
SESS = uuid.UUID("44444444-4444-4444-8444-444444444444")
REQ = {"operation_id": str(OP), "candidate_id": str(CAND)}


def issued(status="authenticated", **over):
    values = dict(operation_id=OP, session_id=SESS, generation_id=GEN, user_id="user-1", active_secret=secret(9),
                  absolute_expires_at=datetime.now(timezone.utc) + timedelta(days=15), resumed=status == "resumed")
    values.update(over)
    return IssuedSession(**values)


def outcome_for(endpoint, status, reason, kind):
    if kind == NC:
        return Outcome(status, reason, OP, CandidateCredential(CAND, secret(5)))
    if kind == SE:
        return Outcome(status, reason, OP, session=issued(status))
    return Outcome(status, reason, OP)


def drive(browser, endpoint):
    if endpoint == S:
        return browser.start("alice@example.com")
    browser.jar[candidate_name(CAND)] = secret(5).reveal()
    if endpoint == R:
        return browser.post("/sign-in/resend", REQ)
    if endpoint == V:
        return browser.post("/sign-in/verify", {**REQ, "code": "12345678"})
    return browser.post("/sign-in/resume", REQ)


@pytest.mark.parametrize("key", sorted(REFERENCE, key=repr), ids=lambda k: "|".join(map(str, k)))
def test_each_matrix_entry_through_the_route(stub, key):
    service, browser = stub
    endpoint, status, reason = key
    http_status, code, kind = REFERENCE[key]
    service.result = outcome_for(endpoint, status, reason, kind)
    response = drive(browser, endpoint)
    assert response.status_code == http_status, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    names = [c["name"] for c in set_cookies(response) if c["attrs"].get("max-age") != "0"]
    if kind == ER:
        assert body == {"error": code}
        assert names == []
    elif kind == NC:
        key = "status" if http_status < 400 else "error"
        assert body == {key: code, "operation_id": str(OP), "candidate_id": str(CAND)}
        assert names == [candidate_name(CAND)]
    elif kind == PG:
        key = "status" if http_status < 400 else "error"
        assert body == {key: code, "operation_id": str(OP), "candidate_id": str(CAND)}
        assert names == []
    else:
        assert body["status"] == code and body["generation_id"] == str(GEN) and set(body) == {
            "status", "generation_id", "expires_in"}
        assert names == [active_name(GEN)]
        assert browser.jar[active_name(GEN)] == secret(9).reveal()
        # Candidat conservé tel quel : ni effacé, ni renouvelé (reprise possible).
        assert [c["name"] for c in set_cookies(response)] == [active_name(GEN)]
        assert browser.jar[candidate_name(CAND)] == secret(5).reveal()
    if code in ("code_sent", "delivery_uncertain", "delivery_failed"):
        assert ("code_sent" in response.text) is (code == "code_sent")  # jamais « envoyé » si incertain


@pytest.mark.parametrize("key", sorted((k for k, v in REFERENCE.items() if v[2] != SE), key=repr), ids=lambda k: "|".join(
    map(str, k)))
def test_non_session_branches_can_never_set_an_active_cookie(stub, key):
    service, browser = stub
    endpoint, status, reason = key
    service.result = Outcome(status, reason, OP, session=issued())
    response = drive(browser, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert active_cookies_set(response) == []


@pytest.mark.parametrize("endpoint", [V, U])
@pytest.mark.parametrize("broken", [
    "no_session", "wrong_resumed_flag", "other_operation", "bad_secret", "not_redacted", "naive_expiry",
    "with_candidate", "session_not_issued", "past_expiry"])
def test_inconsistent_session_outcomes_fail_closed(stub, endpoint, broken):
    service, browser = stub
    status = "resumed"
    session = issued(status)
    candidate = None
    if broken == "no_session":
        session = None
    elif broken == "wrong_resumed_flag":
        session = issued(status, resumed=False)
    elif broken == "other_operation":
        session = issued(status, operation_id=uuid.uuid4())
    elif broken == "bad_secret":
        session = issued(status, active_secret=Redacted("x" * 43))
    elif broken == "not_redacted":
        session = issued(status, active_secret=secret(9).reveal())
    elif broken == "naive_expiry":
        session = issued(status, absolute_expires_at=datetime.utcnow() + timedelta(days=1))
    elif broken == "with_candidate":
        candidate = CandidateCredential(CAND, secret(5))
    elif broken == "session_not_issued":
        session = object()
    elif broken == "past_expiry":
        session = issued(status, absolute_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    service.result = Outcome(status, None, OP, candidate, session)
    response = drive(browser, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert active_cookies_set(response) == []
    assert candidate_name(CAND) in browser.jar  # le justificatif candidat est conservé


@pytest.mark.parametrize("endpoint", [S, R, V, U])
@pytest.mark.parametrize("result", [
    Outcome("email_linked", "new_identity", OP), Outcome("invitation_activated", None, OP, session=None),
    Outcome("candidate_issued", None, OP, CandidateCredential(CAND, secret(5))),
    Outcome("rejected", "session_invalid"), Outcome("rejected", "profile_already_linked", OP),
    Outcome("rejected", "conflict_other_profile", OP), Outcome("rejected", "invitation_unavailable", OP),
    Outcome("rejected", "something_new", OP), Outcome("rejected", None, OP), Outcome("weird", None, OP),
    Outcome("rejected", {"reason": "dict"}, OP), Outcome(None, None), "not an outcome", None,
    RuntimeError("boom"), TimeoutError("db"),
], ids=repr)
def test_unknown_or_out_of_scope_results_are_503_without_cookie(stub, endpoint, result):
    service, browser = stub
    service.result = result
    response = drive(browser, endpoint)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert [c for c in set_cookies(response) if c["attrs"].get("max-age") != "0"] == []


@pytest.mark.parametrize("endpoint,status,reason", [
    (R, "authenticated", None), (S, "authenticated", None), (S, "resumed", None), (R, "resumed", None),
    (U, "authenticated", None), (S, "rejected", "operation_unavailable"), (U, "issued", None),
    (S, "refused", "quota_ip_verification"), (V, "rejected", "invalid_email"), (V, "issued", None)])
def test_triplets_of_another_endpoint_are_unknown(stub, endpoint, status, reason):
    service, browser = stub
    service.result = Outcome(status, reason, OP, session=issued(status) if status in ("authenticated", "resumed")
                             else None)
    response = drive(browser, endpoint)
    assert response.status_code == 503
    assert active_cookies_set(response) == []


def test_new_candidate_requires_a_complete_credential(stub):
    service, browser = stub
    for broken in (None, CandidateCredential(str(CAND), secret(5)), CandidateCredential(CAND, "raw"),
                   CandidateCredential(CAND, Redacted("short"))):
        service.result = Outcome("issued", None, OP, broken)
        response = browser.start("alice@example.com")
        assert response.status_code == 503
        assert set_cookies(response) == []
    service.result = Outcome("issued", None, None, CandidateCredential(CAND, secret(5)))
    assert browser.start("alice@example.com").status_code == 503


def test_resend_must_not_return_a_new_candidate_nor_another_operation(stub):
    service, browser = stub
    browser.jar[candidate_name(CAND)] = secret(5).reveal()
    service.result = Outcome("issued", None, OP, CandidateCredential(uuid.uuid4(), secret(6)))
    response = browser.post("/sign-in/resend", REQ)
    assert response.status_code == 503 and set_cookies(response) == []
    service.result = Outcome("issued", None, uuid.uuid4())
    assert browser.post("/sign-in/resend", REQ).status_code == 503


def test_identity_not_linked_is_indistinguishable_from_other_sign_in_failures(stub):
    service, browser = stub
    responses = {}
    for reason in ("identity_not_linked", "provider_rejected", "proof_not_applicable", "verification_superseded"):
        service.result = Outcome("rejected", reason, OP)
        response = drive(browser, V)
        headers = {k: v for k, v in response.headers.items() if k != "date"}
        responses[reason] = (response.status_code, response.content, len(response.content), headers)
    assert len(set(map(repr, responses.values()))) == 1
    assert b"identity" not in responses["identity_not_linked"][1]
