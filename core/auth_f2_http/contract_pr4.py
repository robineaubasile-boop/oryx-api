"""Contrat des résultats HTTP F2 des sept routes de R1 PR 4 (C.4.5 §5).

Matrice FERMÉE (endpoint, Outcome.status, Outcome.reason) -> réponse,
DISTINCTE de celle de PR 3 (core.auth_f2_http.contract), qui reste
inchangée et ne s'applique qu'aux routes de connexion : un triplet d'un
autre endpoint, d'une route PR 3 ou absent de cette matrice donne 503
temporarily_unavailable SANS cookie, de même que toute valeur incohérente
avec la règle retenue (candidat, session, identifiants, secret).

Rendus (C.4.5 §5.1) :

  A  CANDIDATE  202 {"status":"activation_started","operation_id","candidate_id"}
                + cookie du NOUVEAU candidat ;
  B  SESSION    200 {"status":"invitation_activated","operation_id","generation_id","expires_at"}
                + cookie actif nouvellement créé ;
  C  SESSION    200 {"status":"resumed","operation_id","generation_id","expires_at"}
                + même cookie actif canonique ;
  D  OPERATION  202 {"status":"code_sent","operation_id"} ; aucun cookie ;
  E  OPERATION  200 {"status":"email_linked","operation_id"} ; aucun cookie ;
  F  OPERATION  503 {"error":"delivery_uncertain"|"delivery_failed","operation_id"} ;
                aucun cookie ; sans operation_id cohérent : 503 générique ;
  G  ERROR      {"error":"<code>"} ; aucun cookie.

expires_at = session.absolute_expires_at, en UTC, sérialisé de façon
déterministe (AAAA-MM-JJTHH:MM:SS.ffffffZ, microsecondes toujours
présentes) ; aucune réponse PR 4 ne porte expires_in. Les refus de transport
(C.4.5 §10.2) et le refus d'admission HTTP (429 rate_limited) sont des
branches de la couche HTTP, antérieures au moteur, hors de cette matrice.
"""
import uuid
from datetime import timezone

from core.auth_f2.service import Outcome
from core.auth_f2_http.contract import RATE_LIMITED, UNAVAILABLE, Decision, Rule, _candidate_ok, _session_ok

BEGIN, COMPLETE, RESUME = "invitation/begin", "invitation/complete", "invitation/resume"
LINK_START, LINK_RESEND, LINK_VERIFY, LINK_RESULT = (
    "email-link/start", "email-link/resend", "email-link/verify", "email-link/result")
ENDPOINTS = (BEGIN, COMPLETE, RESUME, LINK_START, LINK_RESEND, LINK_VERIFY, LINK_RESULT)

# Genres de règle.
CANDIDATE = "candidate"  # A : opération créée, cookie candidat posé
SESSION = "session"      # B, C : session canonique, cookie actif posé
OPERATION = "operation"  # D, E, F : sélecteur operation_id seul, aucun cookie
ERROR = "error"          # G : aucun cookie

ISSUE_QUOTAS = ("email_blocked", "issue_interval", "quota_email_issue", "quota_ip_issue")
ACTIVATION_UNAVAILABLE = Rule(409, "activation_unavailable", ERROR)
OPERATION_UNAVAILABLE = Rule(409, "operation_unavailable", ERROR)
LINKING_FAILED = Rule(401, "linking_failed", ERROR)
LINKING_UNAVAILABLE = Rule(409, "linking_unavailable", ERROR)
CODE_INCORRECT = Rule(401, "code_incorrect", ERROR)
VERIFICATION_UNCERTAIN = Rule(503, "verification_uncertain", ERROR)
CODE_SENT = Rule(202, "code_sent", OPERATION)
EMAIL_LINKED = Rule(200, "email_linked", OPERATION)
DELIVERY_UNCERTAIN = Rule(503, "delivery_uncertain", OPERATION)
DELIVERY_FAILED = Rule(503, "delivery_failed", OPERATION)


def _issue_rules(endpoint: str) -> dict:
    rules = {
        (endpoint, "issued", None): CODE_SENT,
        (endpoint, "issue_uncertain", "generate_uncertain"): DELIVERY_UNCERTAIN,
        (endpoint, "issue_uncertain", None): DELIVERY_UNCERTAIN,
        (endpoint, "send_failed", "send_failed"): DELIVERY_FAILED,
        (endpoint, "send_failed", "superseded"): DELIVERY_FAILED,
        (endpoint, "send_failed", None): DELIVERY_FAILED,
        (endpoint, "rejected", "ip_unavailable"): UNAVAILABLE,
    }
    rules.update({(endpoint, "refused", reason): RATE_LIMITED for reason in ISSUE_QUOTAS})
    return rules


MATRIX = {
    # §5.2 — invitations inexistantes, expirées, révoquées, consommées ou
    # au-delà de leurs plafonds : même réponse publique.
    (BEGIN, "candidate_issued", None): Rule(202, "activation_started", CANDIDATE),
    (BEGIN, "rejected", "invitation_unavailable"): Rule(409, "invitation_unavailable", ERROR),

    # §5.3
    (COMPLETE, "invitation_activated", None): Rule(200, "invitation_activated", SESSION),
    (COMPLETE, "rejected", "operation_unknown"): ACTIVATION_UNAVAILABLE,
    (COMPLETE, "rejected", "operation_closed"): ACTIVATION_UNAVAILABLE,
    (COMPLETE, "rejected", "credential_invalid"): ACTIVATION_UNAVAILABLE,
    (COMPLETE, "rejected", "invitation_unavailable"): ACTIVATION_UNAVAILABLE,

    # §5.4
    (RESUME, "resumed", None): Rule(200, "resumed", SESSION),
    (RESUME, "rejected", "resume_unavailable"): Rule(409, "resume_unavailable", ERROR),
    (RESUME, "rejected", "key_unavailable"): UNAVAILABLE,
    (RESUME, "refused", "quota_operation_resume"): RATE_LIMITED,

    # §5.5
    **_issue_rules(LINK_START),
    (LINK_START, "rejected", "invalid_email"): Rule(422, "invalid_email", ERROR),
    (LINK_START, "rejected", "session_invalid"): Rule(401, "session_invalid", ERROR),
    (LINK_START, "rejected", "profile_already_linked"): Rule(409, "profile_already_linked", ERROR),

    # §5.6
    **_issue_rules(LINK_RESEND),
    (LINK_RESEND, "rejected", "operation_unavailable"): OPERATION_UNAVAILABLE,
    (LINK_RESEND, "rejected", "credential_invalid"): OPERATION_UNAVAILABLE,

    # §5.7 — les deux résolutions d'identité réussies ont le même rendu.
    (LINK_VERIFY, "email_linked", "new_identity"): EMAIL_LINKED,
    (LINK_VERIFY, "email_linked", "existing_identity"): EMAIL_LINKED,
    (LINK_VERIFY, "refused", "quota_ip_verification"): RATE_LIMITED,
    (LINK_VERIFY, "refused", "email_blocked"): RATE_LIMITED,
    (LINK_VERIFY, "rejected", "email_blocked"): RATE_LIMITED,
    (LINK_VERIFY, "rejected", "ip_unavailable"): UNAVAILABLE,
    (LINK_VERIFY, "rejected", "key_unavailable"): UNAVAILABLE,
    (LINK_VERIFY, "rejected", "operation_unknown"): OPERATION_UNAVAILABLE,
    (LINK_VERIFY, "rejected", "operation_closed"): OPERATION_UNAVAILABLE,
    (LINK_VERIFY, "rejected", "operation_expired"): OPERATION_UNAVAILABLE,
    (LINK_VERIFY, "rejected", "credential_invalid"): OPERATION_UNAVAILABLE,
    (LINK_VERIFY, "rejected", "no_active_code"): Rule(409, "code_unavailable", ERROR),
    (LINK_VERIFY, "rejected", "code_expired"): Rule(409, "code_expired", ERROR),
    (LINK_VERIFY, "rejected", "request_exhausted"): Rule(409, "code_exhausted", ERROR),
    (LINK_VERIFY, "rejected", "code_not_ready"): Rule(409, "code_not_ready", ERROR),
    (LINK_VERIFY, "rejected", "verification_in_progress"): Rule(409, "verification_in_progress", ERROR),
    (LINK_VERIFY, "rejected", "verification_uncertain"): VERIFICATION_UNCERTAIN,
    (LINK_VERIFY, "uncertain", "provider_uncertain"): VERIFICATION_UNCERTAIN,
    (LINK_VERIFY, "rejected", "code_malformed"): CODE_INCORRECT,
    (LINK_VERIFY, "rejected", "code_incorrect"): CODE_INCORRECT,
    (LINK_VERIFY, "rejected", "code_replaced"): Rule(401, "code_replaced", ERROR),
    (LINK_VERIFY, "rejected", "provider_rejected"): LINKING_FAILED,
    (LINK_VERIFY, "rejected", "proof_not_applicable"): LINKING_FAILED,
    (LINK_VERIFY, "rejected", "verification_superseded"): LINKING_FAILED,
    (LINK_VERIFY, "rejected", "profile_already_linked"): LINKING_UNAVAILABLE,
    (LINK_VERIFY, "rejected", "conflict_other_profile"): LINKING_UNAVAILABLE,

    # §5.8
    (LINK_RESULT, "email_linked", None): EMAIL_LINKED,
    (LINK_RESULT, "rejected", "session_invalid"): Rule(401, "session_invalid", ERROR),
    (LINK_RESULT, "rejected", "result_unavailable"): Rule(409, "result_unavailable", ERROR),
    (LINK_RESULT, "refused", "quota_operation_result"): RATE_LIMITED,
    (LINK_RESULT, "rejected", "key_unavailable"): UNAVAILABLE,
}

# Statut attendu d'une IssuedSession par code de règle SESSION.
_RESUMED = {"invitation_activated": False, "resumed": True}


def expires_at_text(value) -> str:
    """Instant UTC déterministe (microsecondes toujours présentes, suffixe Z)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def translate(endpoint: str, outcome, *, operation_id=None) -> Decision:
    """operation_id : opération désignée par la requête (complétion,
    reprise, renvoi, vérification, résultat) ; None pour begin et
    email-link/start, dont l'opération est créée par le moteur."""
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
    if rule.kind == CANDIDATE:
        if (outcome.session is not None or not isinstance(outcome.operation_id, uuid.UUID)
                or not _candidate_ok(outcome.candidate)):
            return Decision(UNAVAILABLE)
        return Decision(rule, outcome.operation_id, candidate=outcome.candidate)
    if rule.kind == OPERATION:
        if outcome.session is not None or outcome.candidate is not None:
            return Decision(UNAVAILABLE)
        if not isinstance(outcome.operation_id, uuid.UUID):
            return Decision(UNAVAILABLE)
        if operation_id is not None and outcome.operation_id != operation_id:
            return Decision(UNAVAILABLE)
        return Decision(rule, outcome.operation_id)
    if rule.kind == SESSION:
        session = outcome.session
        if (outcome.candidate is not None or operation_id is None or outcome.operation_id != operation_id
                or not _session_ok(session, outcome.status, operation_id)
                or session.resumed is not _RESUMED[rule.code]):
            return Decision(UNAVAILABLE)
        return Decision(rule, operation_id, session=session)
    return Decision(UNAVAILABLE)
