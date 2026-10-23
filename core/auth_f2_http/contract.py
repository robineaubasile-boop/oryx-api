"""Contrat des résultats HTTP F2 (R1 PR 3).

Matrice FERMÉE (endpoint, Outcome.status, Outcome.reason) -> réponse.
Tout triplet absent de la matrice, et toute valeur incohérente avec la
règle retenue (candidat, session, identifiants, secret), donne 503
temporarily_unavailable SANS cookie (refus fermé). Seuls « authenticated »
et « resumed » portant une IssuedSession complète et cohérente peuvent
produire un cookie actif.

Motifs inventoriés dans core/auth_f2/service.py (y compris les valeurs
dynamiques) :

- émission (_issue, _resend) : invalid_email, ip_unavailable,
  operation_unavailable, credential_invalid ; refus de quota
  (abuse.issue_refusals) email_blocked, issue_interval, quota_email_issue,
  quota_ip_issue ; issue_uncertain : generate_uncertain (generateLink) ou
  None (Resend) ; send_failed : send_failed, superseded (bind_code) ou
  None (record_send) ; issued : None ;
- vérification (_verify, _admit_and_examine, _certain_failure,
  _finalize_verification) : ip_unavailable, quota_ip_verification,
  operation_unknown, operation_closed, operation_expired,
  credential_invalid, email_blocked (refused ou rejected), no_active_code,
  code_expired, verification_in_progress, verification_uncertain,
  code_not_ready, code_malformed, key_unavailable, code_replaced,
  code_incorrect, request_exhausted, provider_uncertain (uncertain),
  provider_rejected, proof_not_applicable, identity_not_linked ;
  reprise technique (_replay) : verification_superseded, resume_unavailable,
  key_unavailable, et failure_code PERSISTÉ de l'opération (pour la
  connexion : identity_not_linked seulement ; toute autre valeur est
  inconnue -> 503) ;
- reprise (resume_result) : resume_unavailable, key_unavailable.

Confidentialité : identity_not_linked n'est jamais exposé ; il partage
avec provider_rejected, proof_not_applicable, verification_superseded et
resume_unavailable (rejeu de vérification) EXACTEMENT la même réponse
(401 sign_in_failed : statut, corps, en-têtes). Les erreurs ne portent
qu'un code ; aucun identifiant fournisseur, user_id, OTP ni secret.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import CandidateCredential, IssuedSession, Outcome
from core.auth_f2_http.cookies import is_canonical_secret

START, RESEND, VERIFY, RESUME = "sign-in/start", "sign-in/resend", "sign-in/verify", "sign-in/resume"

# Genres de règle.
NEW_CANDIDATE = "new_candidate"  # opération créée : cookie candidat posé
PROGRESS = "progress"            # renvoi : aucun cookie
SESSION = "session"              # session délivrée : cookie actif posé
ERROR = "error"                  # aucun cookie posé


@dataclass(frozen=True)
class Rule:
    http_status: int
    code: str
    kind: str


UNAVAILABLE = Rule(503, "temporarily_unavailable", ERROR)
SIGN_IN_FAILED = Rule(401, "sign_in_failed", ERROR)
RATE_LIMITED = Rule(429, "rate_limited", ERROR)
OPERATION_UNAVAILABLE = Rule(409, "operation_unavailable", ERROR)
ISSUE_QUOTAS = ("email_blocked", "issue_interval", "quota_email_issue", "quota_ip_issue")


def _issue_rules(endpoint: str, kind: str) -> dict:
    rules = {
        (endpoint, "issued", None): Rule(200, "code_sent", kind),
        (endpoint, "issue_uncertain", "generate_uncertain"): Rule(202, "delivery_uncertain", kind),
        (endpoint, "issue_uncertain", None): Rule(202, "delivery_uncertain", kind),
        (endpoint, "send_failed", "send_failed"): Rule(202, "delivery_failed", kind),
        (endpoint, "send_failed", "superseded"): Rule(202, "delivery_failed", kind),
        (endpoint, "send_failed", None): Rule(202, "delivery_failed", kind),
        (endpoint, "rejected", "ip_unavailable"): UNAVAILABLE,
    }
    rules.update({(endpoint, "refused", reason): RATE_LIMITED for reason in ISSUE_QUOTAS})
    return rules


MATRIX = {
    **_issue_rules(START, NEW_CANDIDATE),
    (START, "rejected", "invalid_email"): Rule(422, "invalid_email", ERROR),
    **_issue_rules(RESEND, PROGRESS),
    (RESEND, "rejected", "operation_unavailable"): OPERATION_UNAVAILABLE,
    (RESEND, "rejected", "credential_invalid"): OPERATION_UNAVAILABLE,

    (VERIFY, "authenticated", None): Rule(200, "authenticated", SESSION),
    (VERIFY, "resumed", None): Rule(200, "resumed", SESSION),
    (VERIFY, "rejected", "ip_unavailable"): UNAVAILABLE,
    (VERIFY, "refused", "quota_ip_verification"): RATE_LIMITED,
    (VERIFY, "refused", "email_blocked"): RATE_LIMITED,
    (VERIFY, "rejected", "email_blocked"): RATE_LIMITED,
    (VERIFY, "rejected", "operation_unknown"): OPERATION_UNAVAILABLE,
    (VERIFY, "rejected", "operation_closed"): OPERATION_UNAVAILABLE,
    (VERIFY, "rejected", "operation_expired"): OPERATION_UNAVAILABLE,
    (VERIFY, "rejected", "credential_invalid"): OPERATION_UNAVAILABLE,
    (VERIFY, "rejected", "no_active_code"): Rule(409, "code_unavailable", ERROR),
    (VERIFY, "rejected", "code_expired"): Rule(409, "code_expired", ERROR),
    (VERIFY, "rejected", "request_exhausted"): Rule(409, "code_exhausted", ERROR),
    (VERIFY, "rejected", "code_not_ready"): Rule(409, "code_not_ready", ERROR),
    (VERIFY, "rejected", "verification_in_progress"): Rule(409, "verification_in_progress", ERROR),
    (VERIFY, "rejected", "verification_uncertain"): Rule(409, "verification_uncertain", ERROR),
    (VERIFY, "uncertain", "provider_uncertain"): Rule(409, "verification_uncertain", ERROR),
    (VERIFY, "rejected", "code_malformed"): Rule(401, "code_incorrect", ERROR),
    (VERIFY, "rejected", "code_incorrect"): Rule(401, "code_incorrect", ERROR),
    (VERIFY, "rejected", "code_replaced"): Rule(401, "code_replaced", ERROR),
    (VERIFY, "rejected", "key_unavailable"): UNAVAILABLE,
    (VERIFY, "rejected", "provider_rejected"): SIGN_IN_FAILED,
    (VERIFY, "rejected", "proof_not_applicable"): SIGN_IN_FAILED,
    (VERIFY, "rejected", "identity_not_linked"): SIGN_IN_FAILED,
    (VERIFY, "rejected", "verification_superseded"): SIGN_IN_FAILED,
    (VERIFY, "rejected", "resume_unavailable"): SIGN_IN_FAILED,

    (RESUME, "resumed", None): Rule(200, "resumed", SESSION),
    (RESUME, "rejected", "resume_unavailable"): Rule(409, "resume_unavailable", ERROR),
    (RESUME, "rejected", "key_unavailable"): UNAVAILABLE,
}


@dataclass(frozen=True)
class Decision:
    rule: Rule
    operation_id: object = None
    candidate: object = None   # CandidateCredential à poser (NEW_CANDIDATE)
    session: object = None     # IssuedSession à poser (SESSION)


def _secret_ok(secret) -> bool:
    return isinstance(secret, Redacted) and is_canonical_secret(secret.reveal())


def _candidate_ok(candidate) -> bool:
    return (isinstance(candidate, CandidateCredential) and isinstance(candidate.candidate_id, uuid.UUID)
            and _secret_ok(candidate.secret))


def _session_ok(session, status: str, operation_id) -> bool:
    return (isinstance(session, IssuedSession) and session.resumed is (status == "resumed")
            and isinstance(session.generation_id, uuid.UUID) and isinstance(session.session_id, uuid.UUID)
            and session.operation_id == operation_id and _secret_ok(session.active_secret)
            and isinstance(session.absolute_expires_at, datetime) and session.absolute_expires_at.tzinfo is not None)


def translate(endpoint: str, outcome, *, operation_id=None) -> Decision:
    """operation_id : opération désignée par la requête (renvoi,
    vérification, reprise) ; None pour le démarrage."""
    if not isinstance(outcome, Outcome) or not isinstance(outcome.status, str):
        return Decision(UNAVAILABLE)
    reason = outcome.reason
    if reason is not None and not isinstance(reason, str):
        return Decision(UNAVAILABLE)
    rule = MATRIX.get((endpoint, outcome.status, reason))
    if rule is None:
        return Decision(UNAVAILABLE)
    if rule.kind == ERROR:
        if outcome.candidate is not None or outcome.session is not None:
            return Decision(UNAVAILABLE)
        return Decision(rule)
    if rule.kind == NEW_CANDIDATE:
        if (outcome.session is not None or not isinstance(outcome.operation_id, uuid.UUID)
                or not _candidate_ok(outcome.candidate)):
            return Decision(UNAVAILABLE)
        return Decision(rule, outcome.operation_id, candidate=outcome.candidate)
    if rule.kind == PROGRESS:
        if outcome.session is not None or outcome.candidate is not None or outcome.operation_id != operation_id:
            return Decision(UNAVAILABLE)
        return Decision(rule, operation_id)
    if rule.kind == SESSION:
        if (outcome.candidate is not None or outcome.operation_id != operation_id
                or not _session_ok(outcome.session, outcome.status, operation_id)):
            return Decision(UNAVAILABLE)
        return Decision(rule, operation_id, session=outcome.session)
    return Decision(UNAVAILABLE)
