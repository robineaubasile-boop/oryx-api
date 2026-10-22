"""Service F2 interne — DORMANT (R1 PR 2, C.2.1 et C.2.1-A).

Parcours :

- Connexion OTP : start_sign_in / resend_sign_in_otp / verify_sign_in.
- Liaison e-mail (depuis une session d'activation d'invitation) :
  start_email_linking / resend_email_linking_otp / verify_email_linking.
  Aucune nouvelle session n'est créée.
- Invitations : create_invitation / revoke_invitation /
  begin_invitation_activation / complete_invitation_activation.
- Sessions : resume_result (reprise exacte du résultat committé),
  authenticate_session (preuve de livraison), revoke_session.

Protocole des opérations sensibles (C.2.1 §9) :

  1. pré-contrôle des quotas sans verrou (un refus n'écrit qu'un marqueur
     borné et n'appelle aucun fournisseur) ;
  2. création des sujets anti-abus manquants (transaction courte) ;
  3. transaction : sujets verrouillés (ordre trié), puis opération,
     candidat, demande OTP, tentative, session ; horloge PostgreSQL lue
     après les verrous ; quotas revérifiés ; réservation durable ; COMMIT ;
  4. appel fournisseur HORS transaction (aucune connexion empruntée) ;
  5. nouvelle transaction : verrous dans le même ordre, revalidation
     complète (états, générations, échéances, blocage), finalisation.

Ordre de verrouillage global : sujets anti-abus -> invitation -> profil
users -> opération -> candidat -> demande OTP -> tentative -> session ->
génération. Aucune transaction ne le remonte : pas d'interblocage.

Garanties :

- Jamais de session sans preuve fournisseur vérifiée (option 1 de
  C.2.1-A) ; une correspondance HMAC locale n'autorise que l'appel
  verifyOtp. Un résultat incertain ou tardif n'autorise jamais rien.
- Aucun code OTP, secret candidat, secret actif ou jeton fournisseur en
  base ni dans les journaux : seulement des HMAC à clé versionnée.
- Aucune identité choisie par un user_id client : le profil vient de
  (provider, issuer, subject) prouvé, ou d'une invitation consommée.
- Reprise : même session, même génération, même secret (dérivé de nouveau
  du secret candidat et des identifiants immuables), sans écriture.
"""
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timezone

import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.abuse import EMAIL, IP, AbuseUnavailable, Scope, SubjectsMissing
from core.auth_f2.crypto import (
    CryptoContractError,
    Redacted,
    active_secret_hash,
    b64url_decode_secret,
    b64url_encode,
    candidate_hash,
    derivation_version,
    derive_active_secret,
    hashes_equal,
    invitation_hash,
    is_valid_otp_code,
    new_secret,
    otp_hmac,
    parse_derivation_version,
)
from core.auth_f2.keys import Keyring, UnknownKeyVersion
from core.auth_f2.normalize import InvalidClientIp, InvalidEmail, normalize_client_ip, normalize_email
from core.auth_f2.policy import (
    CANDIDATE_TTL,
    DELIVERY_PROOF_VERSION,
    INVITATION_TTL,
    OPERATION_SCHEMA_VERSION,
    OPERATION_TTL,
    OTP_MAX_FAILURES_PER_REQUEST,
    OTP_TTL,
    PROVIDER_STALE_AFTER,
    RESULT_RESUME_WINDOW,
    RESULT_VERSION,
    SESSION_ABSOLUTE_TTL,
    SESSION_SCHEMA_VERSION,
    SUBMITTED_CODE_MAX_CHARS,
)
from core.auth_f2.providers import (
    GeneratedOtp,
    MailSent,
    ProviderRejected,
    VerifiedIdentity,
    is_valid_issuer,
)
from core.auth_f2.txn import pg_clock, transaction

logger = logging.getLogger("oryx.auth_f2")

SIGN_IN = "supabase_email_sign_in"
INVITATION = "invitation_activation"
LINKING = "email_linking"
PURPOSE = {SIGN_IN: "sign_in", LINKING: "email_linking"}
PROVIDER = "supabase"
LIVE_STATUSES = ("issuing", "issue_uncertain", "issued", "verifying", "verify_uncertain", "verified")
VERIFIABLE_STATUSES = ("issuing", "issue_uncertain", "issued")
REVOCATION_REASONS = ("logout", "admin", "security")
PAYLOAD = {SIGN_IN: "authenticated", INVITATION: "invitation_activated", LINKING: "email_linked"}
_RETRIES = 3


class DuplicatePendingInvitation(Exception):
    """Une invitation en attente existe déjà pour cette adresse."""


# --------------------------------------------------------------------------
# Valeurs échangées (aucun secret visible par repr())
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateCredential:
    candidate_id: uuid.UUID
    secret: Redacted


@dataclass(frozen=True)
class SessionCredential:
    generation_id: uuid.UUID
    secret: Redacted


@dataclass(frozen=True)
class IssuedSession:
    operation_id: uuid.UUID
    session_id: uuid.UUID
    generation_id: uuid.UUID
    user_id: str
    active_secret: Redacted
    absolute_expires_at: object
    resumed: bool

    def credential(self) -> SessionCredential:
        return SessionCredential(self.generation_id, self.active_secret)


@dataclass(frozen=True)
class SessionPrincipal:
    user_id: str
    session_id: uuid.UUID
    generation_id: uuid.UUID
    auth_method: str
    absolute_expires_at: object


@dataclass(frozen=True)
class InvitationCreated:
    invitation_id: uuid.UUID
    token: Redacted
    expires_at: object


@dataclass(frozen=True)
class Outcome:
    """status : issued | issue_uncertain | send_failed | candidate_issued |
    authenticated | email_linked | invitation_activated | resumed |
    uncertain | refused (quota, marqueur borné) | rejected."""
    status: str
    reason: object = None
    operation_id: object = None
    candidate: object = None
    session: object = None


@dataclass(frozen=True)
class _Reserved:
    operation_id: uuid.UUID
    candidate: object
    request_id: uuid.UUID


@dataclass(frozen=True)
class _Pending:
    kind: str
    operation_id: uuid.UUID
    request_id: uuid.UUID
    attempt_id: uuid.UUID
    email: str = field(repr=False)


# --------------------------------------------------------------------------
# Accès SQL
# --------------------------------------------------------------------------

def _insert(conn, table: str, **values) -> None:
    placeholders = [f"CAST(:{c} AS JSONB)" if c == "result_payload" else f":{c}" for c in values]
    params = {c: (json.dumps(v) if c == "result_payload" else v) for c, v in values.items()}
    conn.execute(sa.text(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join(placeholders)})"), params)


def _update(conn, table: str, row_id, **values) -> None:
    assignments = ", ".join(f"{c} = :{c}" for c in values)
    conn.execute(sa.text(f"UPDATE {table} SET {assignments} WHERE id = :_id"), {**values, "_id": row_id})


def _row(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params).mappings().first()


def _uuid(value):
    return value if isinstance(value, uuid.UUID) else None


def _decode(secret) -> object:
    if not isinstance(secret, Redacted):
        return None
    try:
        return b64url_decode_secret(secret.reveal())
    except CryptoContractError:
        return None


class F2AuthService:
    """engine : moteur SQLAlchemy PostgreSQL ; keyring : trousseau injecté ;
    supabase / mailer : fournisseurs injectés (doubles en test) ; clock :
    horloge PostgreSQL (pg_clock), remplaçable par les tests de frontières ;
    fault_hook : points d'interruption des tests de panne (aucun en
    production)."""

    def __init__(self, engine, keyring: Keyring, supabase, mailer, *, clock=pg_clock, fault_hook=None):
        if not isinstance(keyring, Keyring):
            raise TypeError("keyring : Keyring attendu")
        if not is_valid_issuer(getattr(supabase, "issuer", None)):
            raise ValueError("fournisseur Supabase : issuer https://<projet>/auth/v1 attendu")
        self._engine = engine
        self._keys = keyring
        self._supabase = supabase
        self._mailer = mailer
        self._clock = clock
        self._hook = fault_hook or (lambda point: None)

    def __repr__(self) -> str:
        return "F2AuthService()"

    # ------------------------------------------------------------------
    # Aides internes
    # ------------------------------------------------------------------

    def _log(self, action: str, outcome: Outcome) -> Outcome:
        logger.info("f2 %s status=%s reason=%s operation=%s", action, outcome.status, outcome.reason,
                    outcome.operation_id)
        return outcome

    def _lock_subjects(self, conn, scopes):
        state = abuse.load_key_state(conn, self._keys)
        return abuse.select_subjects(conn, self._keys, state, scopes, lock=True)

    def _with_subjects(self, scopes, body):
        """Étapes 2 et 3 : création des sujets puis transaction principale ;
        rejouée seulement si un sujet a disparu AVANT toute écriture."""
        for _ in range(_RETRIES):
            abuse.ensure_subjects(self._engine, self._keys, scopes)
            try:
                with transaction(self._engine) as conn:
                    return body(conn, self._lock_subjects(conn, scopes))
            except SubjectsMissing:
                continue
        raise AbuseUnavailable("sujets anti-abus indisponibles")

    def _finalize(self, body):
        """Transaction de finalisation (après un appel fournisseur) : reprise
        purement technique en cas d'erreur de connexion, sans nouvelle
        admission ni nouvel appel ; le corps est idempotent (états relus)."""
        for attempt in range(_RETRIES):
            try:
                with transaction(self._engine) as conn:
                    return body(conn)
            except (sa.exc.OperationalError, SubjectsMissing):
                if attempt == _RETRIES - 1:
                    raise

    def _candidate_ok(self, cand, operation_id, credential: CandidateCredential, at) -> bool:
        if cand is None or not isinstance(credential, CandidateCredential):
            return False
        secret = _decode(credential.secret)
        if secret is None or cand["id"] != credential.candidate_id or cand["auth_operation_id"] != operation_id:
            return False
        if cand["status"] != "open" or at >= cand["authentication_expires_at"]:
            return False
        return self._candidate_hash_ok(cand, secret)

    def _candidate_hash_ok(self, cand, secret: bytes) -> bool:
        try:
            key = self._keys.key("candidate", cand["hash_key_version"])
        except UnknownKeyVersion:
            return False
        return hashes_equal(candidate_hash(key, cand["auth_operation_id"], cand["id"], secret), cand["candidate_hash"])

    def _session_rows(self, conn, credential, at, *, generation_lock: str):
        """(session, génération) valides pour un secret actif présenté, ou
        None. Verrous : session FOR SHARE puis génération."""
        if not isinstance(credential, SessionCredential) or _uuid(credential.generation_id) is None:
            return None
        secret = _decode(credential.secret)
        if secret is None:
            return None
        session_id = conn.execute(sa.text("SELECT session_id FROM oryx_session_generations WHERE id = :g"),
                                  {"g": credential.generation_id}).scalar()
        if session_id is None:
            return None
        session = _row(conn, "SELECT * FROM oryx_sessions WHERE id = :s FOR SHARE", s=session_id)
        generation = _row(conn, f"SELECT * FROM oryx_session_generations WHERE id = :g {generation_lock}",
                          g=credential.generation_id)
        if session["status"] != "active" or generation["status"] != "active" or at >= session["absolute_expires_at"]:
            return None
        try:
            key = self._keys.key("active", generation["hash_key_version"])
        except UnknownKeyVersion:
            return None
        if not hashes_equal(active_secret_hash(key, session["id"], generation["id"], secret),
                            generation["active_secret_hash"]):
            return None
        return session, generation

    def _live_request(self, conn, operation_id):
        """Demande vivante de l'opération ET ses tentatives non résolues,
        verrouillées dans l'ordre global (demande -> tentative) avant tout
        contrôle de session (rang supérieur)."""
        request = _row(conn, "SELECT * FROM auth_otp_requests WHERE auth_operation_id = :op AND status IN "
                             f"{LIVE_STATUSES} FOR UPDATE", op=operation_id)
        if request is not None:
            conn.execute(sa.text("SELECT id FROM auth_otp_verification_attempts WHERE otp_request_id = :r "
                                 "AND state IN ('in_flight', 'uncertain') ORDER BY id FOR UPDATE"),
                         {"r": request["id"]})
        return request

    def _supersede(self, conn, request, at) -> None:
        _update(conn, "auth_otp_requests", request["id"], status="superseded", closed_at=at)
        conn.execute(sa.text(
            "UPDATE auth_otp_verification_attempts SET state = 'abandoned', resolved_at = :at "
            "WHERE otp_request_id = :r AND state IN ('in_flight', 'uncertain')"), {"at": at, "r": request["id"]})

    def _new_candidate(self, conn, operation_id, kind, at, expires_at) -> CandidateCredential:
        candidate_id = uuid.uuid4()
        secret = new_secret()
        version, key = self._keys.current("candidate")
        _insert(conn, "auth_preauth_candidates", id=candidate_id, auth_operation_id=operation_id, operation_kind=kind,
                generation_no=1, candidate_hash=candidate_hash(key, operation_id, candidate_id, secret),
                hash_key_version=version, status="open", created_at=at,
                authentication_expires_at=min(expires_at, at + CANDIDATE_TTL))
        return CandidateCredential(candidate_id, Redacted(b64url_encode(secret)))

    def _new_operation(self, conn, kind, at, *, expires_at=None, **fields) -> uuid.UUID:
        operation_id = uuid.uuid4()
        _insert(conn, "auth_operations", id=operation_id, kind=kind, status="pending",
                operation_schema_version=OPERATION_SCHEMA_VERSION, created_at=at, updated_at=at,
                expires_at=expires_at or at + OPERATION_TTL, **fields)
        return operation_id

    def _fail_operation(self, conn, op, code: str, at, **fields) -> None:
        _update(conn, "auth_operations", op["id"], status="failed", failure_code=code, updated_at=at, **fields)

    def _close_candidate(self, conn, cand, at) -> None:
        if cand is not None and cand["status"] in ("open", "bound"):
            _update(conn, "auth_preauth_candidates", cand["id"], status="revoked", closed_at=at)

    def _issue_session(self, conn, at, op, cand, secret: bytes, user_id, *, identity_id=None, request=None):
        """Session canonique + génération + candidat lié + résultat, dans la
        transaction qui committe l'opération (déjà passée à 'committed')."""
        session_id, generation_id = uuid.uuid4(), uuid.uuid4()
        derive_version, derive_key = self._keys.current("derive")
        active_version, active_key = self._keys.current("active")
        active = derive_active_secret(derive_key, derive_version, op["id"], cand["id"], session_id, generation_id,
                                      cand["generation_no"], secret)
        expires = at + SESSION_ABSOLUTE_TTL
        _update(conn, "auth_preauth_candidates", cand["id"], status="bound", bound_at=at)
        _insert(conn, "oryx_sessions", id=session_id, user_id=user_id, created_by_operation_id=op["id"],
                auth_method=op["kind"], invitation_id=op["invitation_id"], auth_identity_id=identity_id,
                status="active", created_at=at, absolute_expires_at=expires, session_schema_version=SESSION_SCHEMA_VERSION)
        _insert(conn, "oryx_session_generations", id=generation_id, session_id=session_id, auth_operation_id=op["id"],
                candidate_id=cand["id"], generation_no=cand["generation_no"],
                active_secret_hash=active_secret_hash(active_key, session_id, generation_id, active),
                derivation_version=derivation_version(derive_version), hash_key_version=active_version,
                status="active", issued_at=at, delivery_status="pending")
        self._insert_result(conn, at, op, session_id=session_id, candidate_id=cand["id"], candidate_bound_at=at,
                            generation_id=generation_id, identity_id=identity_id, request=request)
        return IssuedSession(op["id"], session_id, generation_id, user_id, Redacted(b64url_encode(active)), expires,
                             resumed=False)

    def _insert_result(self, conn, at, op, *, identity_id=None, request=None, **session_parts) -> None:
        _insert(conn, "auth_operation_results", operation_id=op["id"], operation_kind=op["kind"],
                operation_status="committed", committed_at=at, resume_until=at + RESULT_RESUME_WINDOW,
                session_id=session_parts.get("session_id"), candidate_id=session_parts.get("candidate_id"),
                candidate_bound_at=session_parts.get("candidate_bound_at"),
                generation_id=session_parts.get("generation_id"), auth_identity_id=identity_id,
                otp_request_id=request["id"] if request else None, otp_verified_at=at if request else None,
                otp_expires_at=request["expires_at"] if request else None, result_version=RESULT_VERSION,
                result_payload={"outcome": PAYLOAD[op["kind"]]})

    # ------------------------------------------------------------------
    # Émission OTP
    # ------------------------------------------------------------------

    def start_sign_in(self, *, email, client_ip) -> Outcome:
        try:
            email = normalize_email(email)
            ip = normalize_client_ip(client_ip)
        except InvalidEmail:
            return self._log("issue", Outcome("rejected", "invalid_email"))
        except InvalidClientIp:
            return self._log("issue", Outcome("rejected", "ip_unavailable"))
        return self._issue(SIGN_IN, email, ip, operation_id=None, credential=None)

    def resend_sign_in_otp(self, *, operation_id, candidate: CandidateCredential, client_ip) -> Outcome:
        return self._resend(SIGN_IN, operation_id, candidate, client_ip)

    def start_email_linking(self, *, session: SessionCredential, email, client_ip) -> Outcome:
        try:
            email = normalize_email(email)
            ip = normalize_client_ip(client_ip)
        except InvalidEmail:
            return self._log("issue", Outcome("rejected", "invalid_email"))
        except InvalidClientIp:
            return self._log("issue", Outcome("rejected", "ip_unavailable"))
        with transaction(self._engine) as conn:
            at = self._clock(conn)
            found = self._session_rows(conn, session, at, generation_lock="FOR SHARE")
            if found is None or found[0]["auth_method"] != INVITATION:
                return self._log("issue", Outcome("rejected", "session_invalid"))
            if self._profile_has_identity(conn, found[0]["user_id"]):
                return self._log("issue", Outcome("rejected", "profile_already_linked"))
        return self._issue(LINKING, email, ip, operation_id=None, credential=session)

    def resend_email_linking_otp(self, *, operation_id, session: SessionCredential, client_ip) -> Outcome:
        return self._resend(LINKING, operation_id, session, client_ip)

    def _profile_has_identity(self, conn, user_id) -> bool:
        return conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM auth_identities WHERE user_id = :u)"),
                            {"u": user_id}).scalar_one()

    def _credential_ok(self, conn, kind, op, credential, at, *, cand=None) -> bool:
        if kind == SIGN_IN:
            return self._candidate_ok(cand, op["id"], credential, at)
        found = self._session_rows(conn, credential, at, generation_lock="FOR SHARE")
        return (found is not None and found[0]["id"] == op["initiating_session_id"]
                and found[0]["user_id"] == op["target_user_id"])

    def _resend(self, kind, operation_id, credential, client_ip) -> Outcome:
        try:
            ip = normalize_client_ip(client_ip)
        except InvalidClientIp:
            return self._log("issue", Outcome("rejected", "ip_unavailable"))
        operation_id = _uuid(operation_id)
        with transaction(self._engine) as conn:
            at = self._clock(conn)
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=operation_id) if operation_id else None
            if op is None or op["kind"] != kind or op["status"] != "pending" or at >= op["expires_at"]:
                return self._log("issue", Outcome("rejected", "operation_unavailable", operation_id))
            cand = None
            if kind == SIGN_IN and isinstance(credential, CandidateCredential):
                cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id",
                            id=_uuid(credential.candidate_id))
            if not self._credential_ok(conn, kind, op, credential, at, cand=cand) or op["email_normalized"] is None:
                return self._log("issue", Outcome("rejected", "credential_invalid", operation_id))
        return self._issue(kind, op["email_normalized"], ip, operation_id=operation_id, credential=credential)

    def _issue(self, kind, email: str, ip: str, *, operation_id, credential) -> Outcome:
        email_scope, ip_scope = Scope(EMAIL, email), Scope(IP, ip)
        scopes = [email_scope, ip_scope]
        # 1. Pré-contrôle sans verrou : un refus n'écrit qu'un marqueur borné.
        with transaction(self._engine) as conn:
            state = abuse.load_key_state(conn, self._keys)
            subjects = abuse.select_subjects(conn, self._keys, state, scopes, lock=False)
            at = self._clock(conn)
            refused = self._issue_refused(conn, subjects, email_scope, ip_scope, at)
            if refused:
                return self._log("issue", Outcome("refused", refused, operation_id))

        # 2-3. Réservation durable sous verrous, puis COMMIT.
        def reserve(conn, subjects):
            op = cand = None
            if operation_id is not None:
                op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE", id=operation_id)
                if kind == SIGN_IN:
                    cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id FOR UPDATE",
                                id=_uuid(credential.candidate_id))
            live = self._live_request(conn, operation_id) if operation_id is not None else None
            at = self._clock(conn)
            refused = self._issue_refused(conn, subjects, email_scope, ip_scope, at)
            if refused:
                return Outcome("refused", refused, operation_id)
            issued_candidate = None
            if op is None:
                fields = {"email_normalized": email}
                if kind == LINKING:
                    # Ordre global : profil (FK target_user_id) avant session.
                    owner = conn.execute(sa.text(
                        "SELECT s.user_id FROM oryx_session_generations g JOIN oryx_sessions s ON s.id = g.session_id "
                        "WHERE g.id = :g"), {"g": _uuid(getattr(credential, "generation_id", None))}).scalar()
                    if owner is not None:
                        conn.execute(sa.text("SELECT 1 FROM users WHERE id = :u FOR KEY SHARE"), {"u": owner})
                    found = self._session_rows(conn, credential, at, generation_lock="FOR SHARE")
                    if found is None or found[0]["auth_method"] != INVITATION:
                        return Outcome("rejected", "session_invalid")
                    if self._profile_has_identity(conn, found[0]["user_id"]):
                        return Outcome("rejected", "profile_already_linked")
                    fields.update(target_user_id=found[0]["user_id"], initiating_session_id=found[0]["id"],
                                  initiating_session_method=INVITATION)
                new_id = self._new_operation(conn, kind, at, **fields)
                op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=new_id)
                if kind == SIGN_IN:
                    issued_candidate = self._new_candidate(conn, new_id, kind, at, op["expires_at"])
            else:
                if op["status"] != "pending" or at >= op["expires_at"]:
                    return Outcome("rejected", "operation_unavailable", op["id"])
                if not self._credential_ok(conn, kind, op, credential, at, cand=cand):
                    return Outcome("rejected", "credential_invalid", op["id"])
                if live is not None:
                    self._supersede(conn, live, at)
            generation = conn.execute(sa.text(
                "SELECT coalesce(max(code_generation), 0) + 1 FROM auth_otp_requests WHERE auth_operation_id = :op"),
                {"op": op["id"]}).scalar_one()
            request_id = uuid.uuid4()
            _insert(conn, "auth_otp_requests", id=request_id, auth_operation_id=op["id"], operation_kind=kind,
                    purpose=PURPOSE[kind], code_generation=generation, email_normalized=email, status="issuing",
                    failure_count=0, requested_at=at, expires_at=min(at + OTP_TTL, op["expires_at"]))
            for scope in scopes:
                abuse.insert_event(conn, subject_id=subjects.recording_id(scope), kind=abuse.OTP_REQUEST, at=at,
                                   operation_id=op["id"], otp_request_id=request_id)
            _update(conn, "auth_operations", op["id"], updated_at=at)
            return _Reserved(op["id"], issued_candidate, request_id)

        reserved = self._with_subjects(scopes, reserve)
        if not isinstance(reserved, _Reserved):
            return self._log("issue", reserved)
        operation_id, candidate, request_id = reserved.operation_id, reserved.candidate, reserved.request_id
        self._hook("issue.reserved")

        # 4. Supabase generateLink, hors transaction.
        generated = self._supabase.generate_email_otp(email)
        self._hook("issue.generated")

        def bind_code(conn):
            _row(conn, "SELECT id FROM auth_operations WHERE id = :id FOR UPDATE", id=operation_id)
            request = _row(conn, "SELECT * FROM auth_otp_requests WHERE id = :id FOR UPDATE", id=request_id)
            at = self._clock(conn)
            if request["status"] != "issuing":
                return "superseded"
            if request["expected_code_hmac"] is not None:
                return "bound"
            if not isinstance(generated, GeneratedOtp) or at >= request["expires_at"]:
                _update(conn, "auth_otp_requests", request_id, status="send_failed", closed_at=at)
                return "send_failed"
            version, key = self._keys.current("otp")
            _update(conn, "auth_otp_requests", request_id, expected_code_hmac_key_version=version,
                    expected_code_hmac=otp_hmac(key, request_id, request["code_generation"], request["purpose"],
                                                generated.code))
            return "bound"

        bound = self._finalize(bind_code)
        if bound != "bound":
            return self._log("issue", Outcome("send_failed", bound, operation_id, candidate))
        self._hook("issue.bound")

        # 5. Resend, hors transaction ; aucune nouvelle tentative automatique.
        sent = self._mailer.send_otp(to=email, code=generated.code, idempotency_key=str(request_id))
        self._hook("issue.sent")

        def record_send(conn):
            _row(conn, "SELECT id FROM auth_operations WHERE id = :id FOR UPDATE", id=operation_id)
            request = _row(conn, "SELECT * FROM auth_otp_requests WHERE id = :id FOR UPDATE", id=request_id)
            at = self._clock(conn)
            if request["status"] == "issuing":
                if isinstance(sent, MailSent):
                    _update(conn, "auth_otp_requests", request_id, status="issued", provider_sent_at=at)
                    return "issued"
                if isinstance(sent, ProviderRejected):
                    _update(conn, "auth_otp_requests", request_id, status="send_failed", closed_at=at)
                    return "send_failed"
                _update(conn, "auth_otp_requests", request_id, status="issue_uncertain")
                return "issue_uncertain"
            return {"issued": "issued", "issue_uncertain": "issue_uncertain"}.get(request["status"], "send_failed")

        status = self._finalize(record_send)
        return self._log("issue", Outcome(status, None, operation_id, candidate))

    def _issue_refused(self, conn, subjects, email_scope, ip_scope, at):
        refusals = abuse.issue_refusals(conn, subjects, email_scope, ip_scope, at)
        for scope, _ in refusals:
            abuse.insert_refusal_marker(conn, subject_id=subjects.marker_id(scope), kind=abuse.OTP_REQUEST_REFUSED,
                                        at=at)
        return refusals[0][1] if refusals else None

    # ------------------------------------------------------------------
    # Vérification OTP
    # ------------------------------------------------------------------

    def verify_sign_in(self, *, operation_id, candidate: CandidateCredential, code, client_ip) -> Outcome:
        return self._verify(SIGN_IN, operation_id, candidate, code, client_ip)

    def verify_email_linking(self, *, operation_id, session: SessionCredential, code, client_ip) -> Outcome:
        return self._verify(LINKING, operation_id, session, code, client_ip)

    def _verify(self, kind, operation_id, credential, code, client_ip) -> Outcome:
        try:
            ip_scope = Scope(IP, normalize_client_ip(client_ip))
        except InvalidClientIp:
            return self._log("verify", Outcome("rejected", "ip_unavailable"))
        operation_id = _uuid(operation_id)

        # 1. Pré-contrôle sans verrou du budget IP.
        with transaction(self._engine) as conn:
            state = abuse.load_key_state(conn, self._keys)
            subjects = abuse.select_subjects(conn, self._keys, state, [ip_scope], lock=False)
            at = self._clock(conn)
            if abuse.verification_ip_exhausted(conn, subjects, ip_scope, at):
                abuse.insert_refusal_marker(conn, subject_id=subjects.marker_id(ip_scope),
                                            kind=abuse.OTP_VERIFICATION_REFUSED, at=at)
                return self._log("verify", Outcome("refused", "quota_ip_verification", operation_id))
            op = _row(conn, "SELECT kind, email_normalized FROM auth_operations WHERE id = :id",
                      id=operation_id) if operation_id else None
        email = op["email_normalized"] if op is not None and op["kind"] == kind else None
        email_scope = Scope(EMAIL, email) if email else None
        scopes = [ip_scope] + ([email_scope] if email_scope else [])

        # 2-3. Admission durable, examen local, réservation de l'appel.
        result = self._with_subjects(scopes, lambda conn, subjects: self._admit_and_examine(
            conn, subjects, kind, operation_id, credential, code, ip_scope, email_scope))
        if not isinstance(result, _Pending):
            return self._log("verify", result)
        self._hook("verify.reserved")

        # 4. Supabase verifyOtp, hors transaction.
        outcome = self._supabase.verify_email_otp(result.email, code)
        self._hook("verify.provider_returned")

        # 5. Finalisation sous verrous, revalidation complète.
        return self._log("verify", self._finalize(
            lambda conn: self._finalize_verification(conn, result, credential, outcome, email_scope)))

    def _admit_and_examine(self, conn, subjects, kind, operation_id, credential, code, ip_scope, email_scope):
        op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE",
                  id=operation_id) if operation_id else None
        cand = None
        if kind == SIGN_IN and isinstance(credential, CandidateCredential) and _uuid(credential.candidate_id):
            cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id FOR UPDATE",
                        id=credential.candidate_id)
        request = self._live_request(conn, op["id"]) if op is not None and op["kind"] == kind else None
        at = self._clock(conn)
        if abuse.verification_ip_exhausted(conn, subjects, ip_scope, at):
            abuse.insert_refusal_marker(conn, subject_id=subjects.marker_id(ip_scope),
                                        kind=abuse.OTP_VERIFICATION_REFUSED, at=at)
            return Outcome("refused", "quota_ip_verification", operation_id)
        # Admission : consomme le budget IP quel que soit le résultat.
        abuse.insert_event(conn, subject_id=subjects.recording_id(ip_scope), kind=abuse.OTP_VERIFICATION_ATTEMPT,
                           at=at, operation_id=op["id"] if op is not None else None,
                           otp_request_id=request["id"] if request is not None else None)
        self._hook("verify.admitted")
        if op is None or op["kind"] != kind or email_scope is None:
            return Outcome("rejected", "operation_unknown", operation_id)
        if op["status"] != "pending":
            return Outcome("rejected", "operation_closed", op["id"])
        if at >= op["expires_at"]:
            _update(conn, "auth_operations", op["id"], status="expired", updated_at=at)
            if cand is not None and cand["auth_operation_id"] == op["id"] and cand["status"] == "open":
                _update(conn, "auth_preauth_candidates", cand["id"], status="expired", closed_at=at)
            if request is not None:
                self._expire_request(conn, request, at)
            return Outcome("rejected", "operation_expired", op["id"])
        if not self._credential_ok(conn, kind, op, credential, at, cand=cand):
            return Outcome("rejected", "credential_invalid", op["id"])
        email_ids = subjects.ids(email_scope)
        if abuse.block_active(conn, email_ids, at):
            abuse.insert_refusal_marker(conn, subject_id=subjects.marker_id(email_scope),
                                        kind=abuse.OTP_VERIFICATION_REFUSED, at=at)
            return Outcome("refused", "email_blocked", op["id"])
        if request is None:
            return Outcome("rejected", "no_active_code", op["id"])
        if at >= request["expires_at"]:
            self._expire_request(conn, request, at)
            return Outcome("rejected", "code_expired", op["id"])
        if request["status"] == "verifying":
            attempt = _row(conn, "SELECT * FROM auth_otp_verification_attempts WHERE otp_request_id = :r "
                                 "AND state = 'in_flight'", r=request["id"])  # déjà verrouillée
            if attempt is not None and attempt["started_at"] > at - PROVIDER_STALE_AFTER:
                return Outcome("rejected", "verification_in_progress", op["id"])
            if attempt is not None:
                _update(conn, "auth_otp_verification_attempts", attempt["id"], state="uncertain", uncertain_since=at)
            _update(conn, "auth_otp_requests", request["id"], status="verify_uncertain")
            return Outcome("rejected", "verification_uncertain", op["id"])
        if request["status"] not in VERIFIABLE_STATUSES or request["expected_code_hmac"] is None:
            return Outcome("rejected", "verification_uncertain" if request["status"] == "verify_uncertain"
                           else "code_not_ready", op["id"])

        # Demande et preuve d'origine valides : examen local du code.
        if not (isinstance(code, str) and len(code) <= SUBMITTED_CODE_MAX_CHARS and is_valid_otp_code(code)):
            return self._certain_failure(conn, subjects, email_scope, op, request, at, "code_malformed")
        try:
            key = self._keys.key("otp", request["expected_code_hmac_key_version"])
        except UnknownKeyVersion:
            return Outcome("rejected", "key_unavailable", op["id"])
        submitted = otp_hmac(key, request["id"], request["code_generation"], request["purpose"], code)
        match = hashes_equal(submitted, request["expected_code_hmac"])
        attempt_id = uuid.uuid4()
        attempt_no = conn.execute(sa.text(
            "SELECT coalesce(max(attempt_no), 0) + 1 FROM auth_otp_verification_attempts WHERE otp_request_id = :r"),
            {"r": request["id"]}).scalar_one()
        _insert(conn, "auth_otp_verification_attempts", id=attempt_id, otp_request_id=request["id"],
                attempt_no=attempt_no, code_generation=request["code_generation"], purpose=request["purpose"],
                request_expires_at=request["expires_at"],
                code_hmac_key_version=request["expected_code_hmac_key_version"],
                expected_code_hmac=request["expected_code_hmac"], code_hmac=submitted, local_match=match,
                state="in_flight" if match else "rejected", started_at=at, resolved_at=None if match else at)
        if not match:
            if self._matches_previous_generation(conn, op["id"], code):
                return Outcome("rejected", "code_replaced", op["id"])
            return self._certain_failure(conn, subjects, email_scope, op, request, at, "code_incorrect")
        _update(conn, "auth_otp_requests", request["id"], status="verifying")
        return _Pending(kind, op["id"], request["id"], attempt_id, op["email_normalized"])

    def _expire_request(self, conn, request, at) -> None:
        _update(conn, "auth_otp_requests", request["id"], status="expired", closed_at=at)
        conn.execute(sa.text(
            "UPDATE auth_otp_verification_attempts SET state = 'abandoned', resolved_at = :at "
            "WHERE otp_request_id = :r AND state IN ('in_flight', 'uncertain')"), {"at": at, "r": request["id"]})

    def _matches_previous_generation(self, conn, operation_id, code: str) -> bool:
        """Code d'une génération remplacée ou expirée de la même opération :
        pas un nouvel échec de saisie (D8)."""
        rows = conn.execute(sa.text(
            "SELECT id, code_generation, purpose, expected_code_hmac, expected_code_hmac_key_version "
            "FROM auth_otp_requests WHERE auth_operation_id = :op AND status IN ('superseded', 'expired') "
            "AND expected_code_hmac IS NOT NULL"), {"op": operation_id}).mappings().all()
        for row in rows:
            if not self._keys.has("otp", row["expected_code_hmac_key_version"]):
                continue
            key = self._keys.key("otp", row["expected_code_hmac_key_version"])
            if hashes_equal(otp_hmac(key, row["id"], row["code_generation"], row["purpose"], code),
                            row["expected_code_hmac"]):
                return True
        return False

    def _certain_failure(self, conn, subjects, email_scope, op, request, at, reason) -> Outcome:
        """Échec certain : compteur de la demande (5 -> exhausted) et
        compteur e-mail (5 sur 15 min -> blocage), sous verrous."""
        failures = request["failure_count"] + 1
        if failures >= OTP_MAX_FAILURES_PER_REQUEST:
            _update(conn, "auth_otp_requests", request["id"], failure_count=OTP_MAX_FAILURES_PER_REQUEST,
                    status="exhausted", closed_at=at)
        else:
            _update(conn, "auth_otp_requests", request["id"], failure_count=failures)
        blocked = abuse.record_failure(conn, subjects, email_scope, at=at, operation_id=op["id"],
                                       otp_request_id=request["id"])
        if blocked:
            reason = "email_blocked"
        elif failures >= OTP_MAX_FAILURES_PER_REQUEST:
            reason = "request_exhausted"
        return Outcome("rejected", reason, op["id"])

    def _finalize_verification(self, conn, pending: _Pending, credential, outcome, email_scope) -> Outcome:
        subjects = self._lock_subjects(conn, [email_scope])
        # Profil avant opération (ordre global) : verrou exclusif pour la
        # liaison (une identité par profil), partagé pour la connexion (FK
        # de la session et de l'opération vers users).
        if pending.kind == LINKING:
            target = conn.execute(sa.text("SELECT target_user_id FROM auth_operations WHERE id = :id"),
                                  {"id": pending.operation_id}).scalar_one()
            conn.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": target})
        elif isinstance(outcome, VerifiedIdentity):
            owner = conn.execute(sa.text(
                "SELECT user_id FROM auth_identities WHERE provider = :p AND issuer = :i AND subject = :s"),
                {"p": PROVIDER, "i": self._supabase.issuer, "s": outcome.subject}).scalar()
            if owner is not None:
                conn.execute(sa.text("SELECT id FROM users WHERE id = :u FOR KEY SHARE"), {"u": owner})
        op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE", id=pending.operation_id)
        cand = None
        if pending.kind == SIGN_IN:
            cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id FOR UPDATE",
                        id=credential.candidate_id)
        request = _row(conn, "SELECT * FROM auth_otp_requests WHERE id = :id FOR UPDATE", id=pending.request_id)
        attempt = _row(conn, "SELECT * FROM auth_otp_verification_attempts WHERE id = :id FOR UPDATE",
                       id=pending.attempt_id)
        at = self._clock(conn)
        self._hook("verify.finalizing")

        if attempt["state"] != "in_flight":
            return self._replay(conn, pending, op, cand, credential, attempt, at)
        if not isinstance(outcome, (VerifiedIdentity, ProviderRejected)):
            _update(conn, "auth_otp_verification_attempts", attempt["id"], state="uncertain", uncertain_since=at)
            if request["status"] == "verifying":
                _update(conn, "auth_otp_requests", request["id"], status="verify_uncertain")
            return Outcome("uncertain", "provider_uncertain", op["id"])
        if isinstance(outcome, ProviderRejected):
            _update(conn, "auth_otp_verification_attempts", attempt["id"], state="rejected", resolved_at=at,
                    provider_error_code=outcome.code)
            if request["status"] == "verifying":
                _update(conn, "auth_otp_requests", request["id"], status="expired", closed_at=at)
            return Outcome("rejected", "provider_rejected", op["id"])

        # Preuve fournisseur : revalidation complète avant toute écriture.
        session_ok = True
        if pending.kind == SIGN_IN:
            session_ok = self._candidate_ok(cand, op["id"], credential, at)
        else:
            found = self._session_rows(conn, credential, at, generation_lock="FOR SHARE")
            session_ok = found is not None and found[0]["id"] == op["initiating_session_id"]
        valid = (session_ok and request["status"] == "verifying" and at < request["expires_at"]
                 and op["status"] == "pending" and at < op["expires_at"] and outcome.email == op["email_normalized"]
                 and not abuse.block_active(conn, subjects.ids(email_scope), at))
        if not valid:
            _update(conn, "auth_otp_verification_attempts", attempt["id"], state="abandoned", resolved_at=at)
            if request["status"] == "verifying":
                _update(conn, "auth_otp_requests", request["id"], status="expired", closed_at=at)
            return Outcome("rejected", "proof_not_applicable", op["id"])

        _update(conn, "auth_otp_verification_attempts", attempt["id"], state="accepted", resolved_at=at)
        _update(conn, "auth_otp_requests", request["id"], status="verified", verified_at=at)
        proven = dict(proven_provider=PROVIDER, proven_issuer=self._supabase.issuer, proven_subject=outcome.subject)
        # Lecture sans verrou : (provider, issuer, subject, user_id) d'une
        # identité ne change jamais. Un verrou ici, pris après l'opération,
        # inverserait l'ordre profil -> identité de la liaison.
        identity = _row(conn, "SELECT * FROM auth_identities WHERE provider = :p AND issuer = :i AND subject = :s",
                        p=PROVIDER, i=self._supabase.issuer, s=outcome.subject)
        if pending.kind == SIGN_IN:
            if identity is None:
                # Aucune inscription implicite : l'identité doit avoir été liée.
                self._fail_operation(conn, op, "identity_not_linked", at, **proven)
                self._close_candidate(conn, cand, at)
                return Outcome("rejected", "identity_not_linked", op["id"])
            self._hook("verify.before_commit")
            _update(conn, "auth_operations", op["id"], status="committed", committed_at=at, updated_at=at,
                    user_id=identity["user_id"], auth_identity_id=identity["id"], identity_resolution="existing_identity",
                    **proven)
            _update(conn, "auth_identities", identity["id"], last_authenticated_at=at)
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=op["id"])
            session = self._issue_session(conn, at, op, cand, _decode(credential.secret), identity["user_id"],
                                          identity_id=identity["id"], request=request)
            return Outcome("authenticated", None, op["id"], session=session)
        return self._commit_linking(conn, op, request, identity, outcome, proven, at)

    def _commit_linking(self, conn, op, request, identity, outcome, proven, at) -> Outcome:
        target = op["target_user_id"]
        resolution = "existing_identity"
        if identity is None:
            if self._profile_has_identity(conn, target):
                self._fail_operation(conn, op, "profile_already_linked", at, **proven)
                return Outcome("rejected", "profile_already_linked", op["id"])
            new_id = conn.execute(sa.text(
                "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
                "email_verified_at, created_at) VALUES (:id, :u, :p, :i, :s, :e, :at, :at) "
                "ON CONFLICT (provider, issuer, subject) DO NOTHING RETURNING id"),
                {"id": uuid.uuid4(), "u": target, "p": PROVIDER, "i": proven["proven_issuer"], "s": outcome.subject,
                 "e": op["email_normalized"], "at": at}).scalar()
            if new_id is None:
                # ON CONFLICT a attendu le commit de l'insertion concurrente.
                identity = _row(conn, "SELECT * FROM auth_identities WHERE provider = :p AND issuer = :i "
                                      "AND subject = :s", p=PROVIDER, i=proven["proven_issuer"], s=outcome.subject)
            else:
                identity = _row(conn, "SELECT * FROM auth_identities WHERE id = :id", id=new_id)
                resolution = "new_identity"
        if identity["user_id"] != target:
            # Aucune fusion : l'identité prouvée appartient à un autre profil.
            self._fail_operation(conn, op, "conflict_other_profile", at, identity_resolution="conflict_other_profile",
                                 **proven)
            return Outcome("rejected", "conflict_other_profile", op["id"])
        self._hook("verify.before_commit")
        _update(conn, "auth_operations", op["id"], status="committed", committed_at=at, updated_at=at, user_id=target,
                auth_identity_id=identity["id"], identity_resolution=resolution, **proven)
        op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=op["id"])
        self._insert_result(conn, at, op, identity_id=identity["id"], request=request)
        return Outcome("email_linked", resolution, op["id"])

    def _replay(self, conn, pending, op, cand, credential, attempt, at) -> Outcome:
        """Finalisation rejouée (reprise technique) ou tentative abandonnée
        entre-temps : l'état durable fait foi, rien n'est réécrit."""
        if attempt["state"] == "accepted":
            if op["status"] == "committed" and pending.kind == SIGN_IN:
                return self._resume_locked(conn, op, credential, at)
            if op["status"] == "committed":
                return Outcome("email_linked", op["identity_resolution"], op["id"])
            return Outcome("rejected", op["failure_code"], op["id"])
        if attempt["state"] == "uncertain":
            return Outcome("uncertain", "provider_uncertain", op["id"])
        if attempt["state"] == "rejected":
            return Outcome("rejected", "provider_rejected", op["id"])
        return Outcome("rejected", "verification_superseded", op["id"])

    # ------------------------------------------------------------------
    # Reprise, authentification, révocation
    # ------------------------------------------------------------------

    def resume_result(self, *, operation_id, candidate: CandidateCredential) -> Outcome:
        """Reprise du résultat committé : candidat 'bound' seulement, fenêtre
        de 5 minutes, session et génération actives et non expirées. Rend
        EXACTEMENT la même session, génération et secret ; aucune écriture,
        aucun droit nouveau."""
        operation_id = _uuid(operation_id)
        if operation_id is None or not isinstance(candidate, CandidateCredential):
            return self._log("resume", Outcome("rejected", "resume_unavailable"))
        with transaction(self._engine) as conn:
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR SHARE", id=operation_id)
            if op is None:
                return self._log("resume", Outcome("rejected", "resume_unavailable"))
            return self._log("resume", self._resume_locked(conn, op, candidate, self._clock(conn)))

    def _resume_locked(self, conn, op, credential: CandidateCredential, at) -> Outcome:
        refused = Outcome("rejected", "resume_unavailable", op["id"])
        if op["status"] != "committed" or op["kind"] not in (SIGN_IN, INVITATION):
            return refused
        secret = _decode(credential.secret) if isinstance(credential, CandidateCredential) else None
        if secret is None or _uuid(credential.candidate_id) is None:
            return refused
        cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id FOR SHARE", id=credential.candidate_id)
        result = _row(conn, "SELECT * FROM auth_operation_results WHERE operation_id = :id", id=op["id"])
        if cand is None or result is None or at >= result["resume_until"]:
            return refused
        if (cand["status"] != "bound" or cand["auth_operation_id"] != op["id"] or result["candidate_id"] != cand["id"]
                or not self._candidate_hash_ok(cand, secret)):
            return refused
        session = _row(conn, "SELECT * FROM oryx_sessions WHERE id = :id FOR SHARE", id=result["session_id"])
        generation = _row(conn, "SELECT * FROM oryx_session_generations WHERE id = :id FOR SHARE",
                          id=result["generation_id"])
        if (session["status"] != "active" or at >= session["absolute_expires_at"] or generation["status"] != "active"
                or session["created_by_operation_id"] != op["id"] or generation["session_id"] != session["id"]
                or generation["candidate_id"] != cand["id"] or generation["generation_no"] != cand["generation_no"]):
            return refused
        try:
            derive_version = parse_derivation_version(generation["derivation_version"])
            derive_key = self._keys.key("derive", derive_version)
            active_key = self._keys.key("active", generation["hash_key_version"])
        except (CryptoContractError, UnknownKeyVersion):
            return Outcome("rejected", "key_unavailable", op["id"])
        active = derive_active_secret(derive_key, derive_version, op["id"], cand["id"], session["id"], generation["id"],
                                      cand["generation_no"], secret)
        if not hashes_equal(active_secret_hash(active_key, session["id"], generation["id"], active),
                            generation["active_secret_hash"]):
            return refused
        issued = IssuedSession(op["id"], session["id"], generation["id"], session["user_id"],
                               Redacted(b64url_encode(active)), session["absolute_expires_at"], resumed=True)
        return Outcome("resumed", None, op["id"], session=issued)

    def authenticate_session(self, credential: SessionCredential):
        """Principal de la session active présentée, ou None. La première
        présentation valide confirme la livraison (delivery_confirmed_at)."""
        with transaction(self._engine) as conn:
            at = self._clock(conn)
            found = self._session_rows(conn, credential, at, generation_lock="FOR UPDATE")
            if found is None:
                return None
            session, generation = found
            if generation["delivery_status"] == "pending":
                _update(conn, "oryx_session_generations", generation["id"], delivery_status="confirmed",
                        delivery_confirmed_at=at, delivery_proof_version=DELIVERY_PROOF_VERSION)
            return SessionPrincipal(session["user_id"], session["id"], generation["id"], session["auth_method"],
                                    session["absolute_expires_at"])

    def revoke_session(self, session_id, reason: str) -> bool:
        if reason not in REVOCATION_REASONS or _uuid(session_id) is None:
            raise ValueError("révocation : session et motif (logout, admin, security) attendus")
        with transaction(self._engine) as conn:
            session = _row(conn, "SELECT * FROM oryx_sessions WHERE id = :id FOR UPDATE", id=session_id)
            if session is None or session["status"] != "active":
                return False
            generations = conn.execute(sa.text(
                "SELECT id FROM oryx_session_generations WHERE session_id = :s AND status = 'active' ORDER BY id "
                "FOR UPDATE"), {"s": session_id}).scalars().all()
            at = self._clock(conn)
            _update(conn, "oryx_sessions", session_id, status="revoked", revoked_at=at, revocation_reason=reason)
            for generation_id in generations:
                _update(conn, "oryx_session_generations", generation_id, status="revoked", revoked_at=at,
                        revocation_reason=reason)
            return True

    # ------------------------------------------------------------------
    # Invitations
    # ------------------------------------------------------------------

    def create_invitation(self, *, cohort: str, recipient_email=None, invited_by_user_id=None) -> InvitationCreated:
        if not isinstance(cohort, str) or not cohort.strip():
            raise ValueError("invitation : cohorte attendue")
        email = normalize_email(recipient_email) if recipient_email is not None else None
        token = new_secret()
        version, key = self._keys.current("invitation")
        invitation_id = uuid.uuid4()
        try:
            with transaction(self._engine) as conn:
                at = self._clock(conn)
                _insert(conn, "invitations", id=invitation_id, token_hash=invitation_hash(key, token),
                        hash_key_version=version, recipient_email_normalized=email, cohort=cohort, status="pending",
                        invited_by_user_id=invited_by_user_id, created_at=at, expires_at=at + INVITATION_TTL)
        except sa.exc.IntegrityError as exc:
            if "uq_invitations_one_pending_email" in str(exc.orig):
                raise DuplicatePendingInvitation() from None
            raise
        return InvitationCreated(invitation_id, Redacted(b64url_encode(token)), at + INVITATION_TTL)

    def revoke_invitation(self, invitation_id) -> bool:
        with transaction(self._engine) as conn:
            invitation = _row(conn, "SELECT * FROM invitations WHERE id = :id FOR UPDATE", id=_uuid(invitation_id))
            if invitation is None or invitation["status"] != "pending":
                return False
            _update(conn, "invitations", invitation["id"], status="revoked", revoked_at=self._clock(conn))
            return True

    def _find_invitation(self, conn, token: Redacted):
        raw = _decode(token)
        if raw is None:
            return None
        hashes = {v: invitation_hash(self._keys.key("invitation", v), raw) for v in self._keys.versions("invitation")}
        rows = conn.execute(sa.text("SELECT id, token_hash, hash_key_version FROM invitations "
                                    "WHERE token_hash = ANY(:h)"), {"h": list(hashes.values())}).mappings().all()
        for row in rows:
            expected = hashes.get(row["hash_key_version"])
            if expected is not None and hashes_equal(expected, row["token_hash"]):
                return row["id"]
        return None

    def begin_invitation_activation(self, *, token: Redacted) -> Outcome:
        """Invitation valide -> opération + candidat 'open' (secret rendu une
        seule fois). La consommation a lieu à complete_invitation_activation."""
        with transaction(self._engine) as conn:
            invitation_id = self._find_invitation(conn, token)
            invitation = _row(conn, "SELECT * FROM invitations WHERE id = :id FOR UPDATE",
                              id=invitation_id) if invitation_id else None
            at = self._clock(conn)
            if invitation is None or invitation["status"] != "pending" or at >= invitation["expires_at"]:
                return self._log("invitation", Outcome("rejected", "invitation_unavailable"))
            expires = min(at + OPERATION_TTL, invitation["expires_at"])
            operation_id = self._new_operation(conn, INVITATION, at, expires_at=expires, invitation_id=invitation["id"])
            candidate = self._new_candidate(conn, operation_id, INVITATION, at, expires)
        return self._log("invitation", Outcome("candidate_issued", None, operation_id, candidate))

    def complete_invitation_activation(self, *, operation_id, candidate: CandidateCredential) -> Outcome:
        """Consommation atomique : invitation 'accepted', profil créé avec un
        identifiant généré par le backend, opération committée, session."""
        operation_id = _uuid(operation_id)
        with transaction(self._engine) as conn:
            head = _row(conn, "SELECT kind, invitation_id FROM auth_operations WHERE id = :id",
                        id=operation_id) if operation_id else None
            if head is None or head["kind"] != INVITATION:
                return self._log("invitation", Outcome("rejected", "operation_unknown", operation_id))
            invitation = _row(conn, "SELECT * FROM invitations WHERE id = :id FOR UPDATE", id=head["invitation_id"])
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE", id=operation_id)
            cand = None
            if isinstance(candidate, CandidateCredential) and _uuid(candidate.candidate_id):
                cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id FOR UPDATE",
                            id=candidate.candidate_id)
            at = self._clock(conn)
            if op["status"] != "pending" or at >= op["expires_at"]:
                return self._log("invitation", Outcome("rejected", "operation_closed", op["id"]))
            if not self._candidate_ok(cand, op["id"], candidate, at):
                return self._log("invitation", Outcome("rejected", "credential_invalid", op["id"]))
            if invitation["status"] != "pending" or at >= invitation["expires_at"]:
                self._fail_operation(conn, op, "invitation_unavailable", at)
                self._close_candidate(conn, cand, at)
                return self._log("invitation", Outcome("rejected", "invitation_unavailable", op["id"]))
            user_id = str(uuid.uuid4())
            conn.execute(sa.text("INSERT INTO users (id, level, created_at) VALUES (:id, 'debutant', :at)"),
                         {"id": user_id, "at": at.astimezone(timezone.utc).replace(tzinfo=None)})
            _update(conn, "invitations", invitation["id"], status="accepted", accepted_at=at)
            self._hook("invitation.before_commit")
            _update(conn, "auth_operations", op["id"], status="committed", committed_at=at, updated_at=at,
                    user_id=user_id)
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=op["id"])
            session = self._issue_session(conn, at, op, cand, _decode(candidate.secret), user_id)
        return self._log("invitation", Outcome("invitation_activated", None, op["id"], session=session))
