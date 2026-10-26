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
- R1 PR 4 : quotas d'ouverture par invitation (begin_invitation_activation),
  reprise d'activation sous quota par opération (resume_result avec
  expected_kind='invitation_activation'), consultation du résultat de
  liaison par la session initiatrice (read_email_linking_result).

Protocole des opérations sensibles (C.2.1 §9) :

  1. pré-contrôle des quotas sans verrou (un refus n'écrit qu'un marqueur
     borné et n'appelle aucun fournisseur) ;
  2. création des sujets anti-abus manquants (transaction courte) ;
  3. transaction : état de clé (verrou partagé) et sujets verrouillés
     (ordre trié), puis opération, candidat, demande OTP, tentative,
     session ; horloge PostgreSQL lue après les verrous ; quotas
     revérifiés ; réservation durable ; COMMIT ;
  4. appel fournisseur HORS transaction (aucune connexion empruntée) ;
  5. nouvelle transaction : verrous dans le même ordre, revalidation
     complète (états, générations, échéances, blocage), finalisation.

Ordre de verrouillage global : verrou consultatif partagé de l'état de
clé anti-abus -> sujets anti-abus -> invitation -> profil users ->
opération -> candidat -> demande OTP -> tentative -> session ->
génération -> identité. Aucune transaction ne le remonte : pas
d'interblocage. L'heure de décision (clock_timestamp()) est lue APRÈS
tous les verrous et toute attente possible : aucun droit n'est jugé
valide à un instant antérieur à une attente.

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

from core.auth_f2 import abuse, sessions
from core.auth_f2.abuse import EMAIL, IP, AbuseUnavailable, Scope, SubjectsMissing
from core.auth_f2.crypto import (
    CryptoContractError,
    Redacted,
    active_secret_hash,
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
    HTTP_POLICY_VERSION,
    INVITATION_OPEN_TOTAL_LIMIT,
    INVITATION_OPEN_WINDOW,
    INVITATION_OPEN_WINDOW_LIMIT,
    INVITATION_TTL,
    OPERATION_RESUME_LIMIT,
    OPERATION_RESUME_WINDOW,
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


_decode = sessions.decode_secret


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
        """Rang 1 et 2 de l'ordre global : état de clé en FOR SHARE (jusqu'au
        COMMIT), puis sujets de toutes les versions lues sous ce verrou."""
        state = abuse.lock_key_state(conn, self._keys)
        self._hook("abuse.state_locked")
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

    def _lock_session(self, conn, credential, *, generation_lock: str):
        """Verrous de la session présentée (session FOR SHARE, puis
        génération) SANS décision : (session, génération, secret) ou None.
        La validité se juge ensuite avec _session_valid, à une heure lue
        après tous les verrous de la transaction (core.auth_f2.sessions,
        interface partagée avec R1 PR 5)."""
        return sessions.lock_session(conn, credential, generation_lock=generation_lock)

    def _session_valid(self, locked, at) -> bool:
        """Session et génération actives, non expirées à l'instant `at` (lu
        après les verrous), secret actif conforme à l'empreinte."""
        return sessions.session_valid(self._keys, locked, at)

    def _session_rows(self, conn, credential, *, generation_lock: str):
        """Verrouille puis lit l'heure PostgreSQL, puis juge : (session,
        génération) valides ou None. Transactions qui ne verrouillent rien
        d'autre ensuite."""
        locked = self._lock_session(conn, credential, generation_lock=generation_lock)
        if not self._session_valid(locked, self._clock(conn)):
            return None
        return locked[0], locked[1]

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
            found = self._session_rows(conn, session, generation_lock="FOR SHARE")
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

    def _credential_ok(self, kind, op, credential, at, *, cand=None, session=None) -> bool:
        """Décision pure (aucun verrou) sur des lignes déjà verrouillées :
        candidat pour la connexion, session initiatrice pour la liaison."""
        if kind == SIGN_IN:
            return self._candidate_ok(cand, op["id"], credential, at)
        return (self._session_valid(session, at) and session[0]["id"] == op["initiating_session_id"]
                and session[0]["user_id"] == op["target_user_id"])

    def _lock_credential(self, conn, kind, credential, *, for_update: bool):
        """Verrous du justificatif (rangs candidat ou session/génération),
        sans décision : (candidat, session verrouillée)."""
        if kind == SIGN_IN:
            if not isinstance(credential, CandidateCredential) or _uuid(credential.candidate_id) is None:
                return None, None
            lock = " FOR UPDATE" if for_update else " FOR SHARE"
            return _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id" + lock,
                        id=credential.candidate_id), None
        return None, self._lock_session(conn, credential, generation_lock="FOR SHARE")

    def _resend(self, kind, operation_id, credential, client_ip) -> Outcome:
        try:
            ip = normalize_client_ip(client_ip)
        except InvalidClientIp:
            return self._log("issue", Outcome("rejected", "ip_unavailable"))
        operation_id = _uuid(operation_id)
        with transaction(self._engine) as conn:
            # Pré-contrôle (la décision est refaite sous verrous à la
            # réservation) : verrous de partage, puis heure, puis décision.
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR SHARE",
                      id=operation_id) if operation_id else None
            cand, session = self._lock_credential(conn, kind, credential, for_update=False)
            at = self._clock(conn)
            if op is None or op["kind"] != kind or op["status"] != "pending" or at >= op["expires_at"]:
                return self._log("issue", Outcome("rejected", "operation_unavailable", operation_id))
            if (not self._credential_ok(kind, op, credential, at, cand=cand, session=session)
                    or op["email_normalized"] is None):
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
            # Tous les verrous dans l'ordre global, PUIS l'heure, PUIS les
            # décisions (aucune décision sur un instant antérieur à une
            # attente de verrou).
            op = cand = session = live = None
            if operation_id is not None:
                op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE", id=operation_id)
                if kind == SIGN_IN:
                    cand, _ = self._lock_credential(conn, kind, credential, for_update=True)
                live = self._live_request(conn, operation_id)
                if kind == LINKING:
                    _, session = self._lock_credential(conn, kind, credential, for_update=False)
            elif kind == LINKING:
                # Profil (FK target_user_id) avant session.
                owner = conn.execute(sa.text(
                    "SELECT s.user_id FROM oryx_session_generations g JOIN oryx_sessions s ON s.id = g.session_id "
                    "WHERE g.id = :g"), {"g": _uuid(getattr(credential, "generation_id", None))}).scalar()
                if owner is not None:
                    conn.execute(sa.text("SELECT 1 FROM users WHERE id = :u FOR KEY SHARE"), {"u": owner})
                _, session = self._lock_credential(conn, kind, credential, for_update=False)
            at = self._clock(conn)
            refused = self._issue_refused(conn, subjects, email_scope, ip_scope, at)
            if refused:
                return Outcome("refused", refused, operation_id)
            issued_candidate = None
            if op is None:
                fields = {"email_normalized": email}
                if kind == LINKING:
                    if not self._session_valid(session, at) or session[0]["auth_method"] != INVITATION:
                        return Outcome("rejected", "session_invalid")
                    if self._profile_has_identity(conn, session[0]["user_id"]):
                        return Outcome("rejected", "profile_already_linked")
                    fields.update(target_user_id=session[0]["user_id"], initiating_session_id=session[0]["id"],
                                  initiating_session_method=INVITATION)
                new_id = self._new_operation(conn, kind, at, **fields)
                op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=new_id)
                if kind == SIGN_IN:
                    issued_candidate = self._new_candidate(conn, new_id, kind, at, op["expires_at"])
            else:
                if op["status"] != "pending" or at >= op["expires_at"]:
                    return Outcome("rejected", "operation_unavailable", op["id"])
                if not self._credential_ok(kind, op, credential, at, cand=cand, session=session):
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
            if isinstance(generated, ProviderRejected) or (isinstance(generated, GeneratedOtp)
                                                           and at >= request["expires_at"]):
                # Refus certain (liste positive) ou code déjà expiré : rien
                # n'est envoyé, la demande est close.
                _update(conn, "auth_otp_requests", request_id, status="send_failed", closed_at=at)
                return "send_failed"
            if not isinstance(generated, GeneratedOtp):
                # Issue inconnue (délai, 5xx, 4xx non documenté) : jamais
                # assimilée à un refus certain ; aucun envoi, aucune
                # restitution de quota ; seule une nouvelle génération
                # explicite (sous quotas) la remplacera.
                _update(conn, "auth_otp_requests", request_id, status="issue_uncertain")
                return "issue_uncertain"
            version, key = self._keys.current("otp")
            _update(conn, "auth_otp_requests", request_id, expected_code_hmac_key_version=version,
                    expected_code_hmac=otp_hmac(key, request_id, request["code_generation"], request["purpose"],
                                                generated.code))
            return "bound"

        bound = self._finalize(bind_code)
        if bound == "issue_uncertain":
            return self._log("issue", Outcome("issue_uncertain", "generate_uncertain", operation_id, candidate))
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
        # Verrous dans l'ordre global (opération, candidat, demande et
        # tentatives, session), PUIS l'heure de décision.
        op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE",
                  id=operation_id) if operation_id else None
        cand = session = None
        if kind == SIGN_IN:
            cand, _ = self._lock_credential(conn, kind, credential, for_update=True)
        request = self._live_request(conn, op["id"]) if op is not None and op["kind"] == kind else None
        if kind == LINKING and op is not None and op["kind"] == kind:
            _, session = self._lock_credential(conn, kind, credential, for_update=False)
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
        if not self._credential_ok(kind, op, credential, at, cand=cand, session=session):
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
        """Finalisation après verifyOtp. Tous les verrous (et toute attente
        possible, dont l'insertion concurrente d'une identité) précèdent la
        lecture de l'heure PostgreSQL ; toutes les échéances sont jugées à
        cette heure-là, jamais à un instant antérieur à une attente."""
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
        cand = session = None
        if pending.kind == SIGN_IN:
            cand, _ = self._lock_credential(conn, SIGN_IN, credential, for_update=True)
        request = _row(conn, "SELECT * FROM auth_otp_requests WHERE id = :id FOR UPDATE", id=pending.request_id)
        attempt = _row(conn, "SELECT * FROM auth_otp_verification_attempts WHERE id = :id FOR UPDATE",
                       id=pending.attempt_id)
        if pending.kind == LINKING:
            _, session = self._lock_credential(conn, LINKING, credential, for_update=False)
        proven = identity = resolution = savepoint = None
        if attempt["state"] == "in_flight" and isinstance(outcome, VerifiedIdentity) and op["status"] == "pending":
            proven = dict(proven_provider=PROVIDER, proven_issuer=self._supabase.issuer, proven_subject=outcome.subject)
            if pending.kind == SIGN_IN:
                # (provider, issuer, subject, user_id) sont immuables ; le
                # verrou (rang identité) couvre la mise à jour de
                # last_authenticated_at.
                identity = _row(conn, "SELECT * FROM auth_identities WHERE provider = :p AND issuer = :i "
                                      "AND subject = :s FOR NO KEY UPDATE", p=PROVIDER, i=self._supabase.issuer,
                                s=outcome.subject)
            else:
                identity, resolution, savepoint = self._prepare_linked_identity(conn, op, outcome.subject)
        at = self._clock(conn)
        self._hook("verify.finalizing")

        if attempt["state"] != "in_flight":
            return self._replay(conn, pending, op, credential, attempt)
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

        # Preuve fournisseur : revalidation complète, à l'heure lue après
        # tous les verrous, avant toute écriture.
        if pending.kind == SIGN_IN:
            credential_ok = self._candidate_ok(cand, op["id"], credential, at)
        else:
            credential_ok = self._session_valid(session, at) and session[0]["id"] == op["initiating_session_id"]
        valid = (credential_ok and request["status"] == "verifying" and at < request["expires_at"]
                 and op["status"] == "pending" and at < op["expires_at"] and outcome.email == op["email_normalized"]
                 and not abuse.block_active(conn, subjects.ids(email_scope), at))
        if not valid:
            if savepoint is not None:
                savepoint.rollback()  # aucune identité créée sans décision valide
            _update(conn, "auth_otp_verification_attempts", attempt["id"], state="abandoned", resolved_at=at)
            if request["status"] == "verifying":
                _update(conn, "auth_otp_requests", request["id"], status="expired", closed_at=at)
            return Outcome("rejected", "proof_not_applicable", op["id"])

        _update(conn, "auth_otp_verification_attempts", attempt["id"], state="accepted", resolved_at=at)
        _update(conn, "auth_otp_requests", request["id"], status="verified", verified_at=at)
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
        return self._commit_linking(conn, op, request, identity, resolution, savepoint, proven, at)

    def _prepare_linked_identity(self, conn, op, subject):
        """Résolution de l'identité d'une liaison AVANT la lecture de
        l'heure : l'insertion (ON CONFLICT DO NOTHING) peut attendre une
        insertion concurrente. Elle a lieu dans un point de sauvegarde que
        l'appelant annule si la revalidation échoue. Rend (identité,
        résolution, point de sauvegarde ou None)."""
        target = op["target_user_id"]
        query = "SELECT * FROM auth_identities WHERE provider = :p AND issuer = :i AND subject = :s"
        params = {"p": PROVIDER, "i": self._supabase.issuer, "s": subject}
        identity = _row(conn, query, **params)
        if identity is None:
            if self._profile_has_identity(conn, target):
                return None, "profile_already_linked", None
            savepoint = conn.begin_nested()
            new_id = conn.execute(sa.text(
                "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
                "email_verified_at, created_at) VALUES (:id, :u, :p, :i, :s, :e, clock_timestamp(), clock_timestamp()) "
                "ON CONFLICT (provider, issuer, subject) DO NOTHING RETURNING id"),
                {"id": uuid.uuid4(), "u": target, "e": op["email_normalized"], **params}).scalar()
            if new_id is not None:
                return _row(conn, "SELECT * FROM auth_identities WHERE id = :id", id=new_id), "new_identity", savepoint
            savepoint.commit()  # rien inséré : l'insertion concurrente a été committée
            identity = _row(conn, query, **params)
        resolution = "existing_identity" if identity["user_id"] == target else "conflict_other_profile"
        return identity, resolution, None

    def _commit_linking(self, conn, op, request, identity, resolution, savepoint, proven, at) -> Outcome:
        if resolution == "profile_already_linked":
            self._fail_operation(conn, op, "profile_already_linked", at, **proven)
            return Outcome("rejected", "profile_already_linked", op["id"])
        if resolution == "conflict_other_profile":
            # Aucune fusion : l'identité prouvée appartient à un autre profil.
            self._fail_operation(conn, op, "conflict_other_profile", at, identity_resolution="conflict_other_profile",
                                 **proven)
            return Outcome("rejected", "conflict_other_profile", op["id"])
        if savepoint is not None:
            _update(conn, "auth_identities", identity["id"], created_at=at, email_verified_at=at)
            savepoint.commit()
        self._hook("verify.before_commit")
        _update(conn, "auth_operations", op["id"], status="committed", committed_at=at, updated_at=at,
                user_id=op["target_user_id"], auth_identity_id=identity["id"], identity_resolution=resolution, **proven)
        op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id", id=op["id"])
        self._insert_result(conn, at, op, identity_id=identity["id"], request=request)
        return Outcome("email_linked", resolution, op["id"])

    def _replay(self, conn, pending, op, credential, attempt) -> Outcome:
        """Finalisation rejouée (reprise technique) ou tentative abandonnée
        entre-temps : l'état durable fait foi, rien n'est réécrit."""
        if attempt["state"] == "accepted":
            if op["status"] == "committed" and pending.kind == SIGN_IN:
                return self._resume_locked(conn, op, credential)
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

    def resume_result(self, *, operation_id, candidate: CandidateCredential, expected_kind=None,
                      client_ip=None) -> Outcome:
        """Reprise du résultat committé : candidat 'bound' seulement, fenêtre
        de 5 minutes, session et génération actives et non expirées. Rend
        EXACTEMENT la même session, génération et secret ; aucun droit
        nouveau.

        expected_kind (R1 PR 4) : None pour l'entrée interne historique
        (connexion ou invitation, sans écriture) ; SIGN_IN impose une
        opération de connexion (route /sign-in/resume, sans écriture) ;
        INVITATION impose une opération d'activation et passe par le quota
        par opération (client_ip requis, un resume_attempt par admission).
        Les routes HTTP passent toujours expected_kind."""
        if expected_kind == INVITATION:
            return self._log("resume", self._resume_invitation(operation_id, candidate, client_ip))
        if expected_kind not in (None, SIGN_IN):
            raise ValueError("reprise : type d'opération attendu inconnu")
        operation_id = _uuid(operation_id)
        if operation_id is None or not isinstance(candidate, CandidateCredential):
            return self._log("resume", Outcome("rejected", "resume_unavailable"))
        with transaction(self._engine) as conn:
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR SHARE", id=operation_id)
            if op is None:
                return self._log("resume", Outcome("rejected", "resume_unavailable"))
            return self._log("resume", self._resume_locked(conn, op, candidate, expected_kind=expected_kind))

    def _resume_invitation(self, operation_id, candidate, client_ip) -> Outcome:
        """Reprise d'activation sous quota par opération (C.4.5 §2.4 et
        §3.1). Ordre des verrous : état de clé -> sujets IP -> opération
        FOR UPDATE (pris d'emblée : deux admissions concurrentes de la même
        opération se sérialisent, aucune conversion depuis FOR SHARE) ->
        candidat -> session -> génération -> heure PostgreSQL. Le
        justificatif candidat et toute la reprise sont validés AVANT le
        débit : un UUID isolé, un faux secret ou une opération inconnue
        n'écrivent rien."""
        operation_id = _uuid(operation_id)
        if operation_id is None or not isinstance(candidate, CandidateCredential):
            return Outcome("rejected", "resume_unavailable")
        try:
            ip_scope = Scope(IP, normalize_client_ip(client_ip))
        except InvalidClientIp:
            return Outcome("rejected", "ip_unavailable")

        def body(conn, subjects):
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE", id=operation_id)
            if op is None:
                return Outcome("rejected", "resume_unavailable")
            return self._resume_locked(conn, op, candidate, expected_kind=INVITATION,
                                       quota=(subjects, ip_scope, "quota_operation_resume"))

        return self._with_subjects([ip_scope], body)

    def _debit_operation(self, conn, quota, operation_id, at):
        """Quota par opération (10 / 5 min), sous le verrou exclusif de
        l'opération : compte TOUS les resume_attempt de l'opération (toutes
        IP, toutes versions de clé), puis en enregistre un sur le sujet IP
        courant. Rend un refus, ou None si l'admission est enregistrée."""
        subjects, ip_scope, reason = quota
        since = at - OPERATION_RESUME_WINDOW
        if abuse.count_operation_since(conn, operation_id, abuse.RESUME_ATTEMPT, since) >= OPERATION_RESUME_LIMIT:
            return Outcome("refused", reason, operation_id)
        abuse.insert_event(conn, subject_id=subjects.recording_id(ip_scope), kind=abuse.RESUME_ATTEMPT, at=at,
                           operation_id=operation_id, policy_version=HTTP_POLICY_VERSION)
        return None

    def _resume_locked(self, conn, op, credential: CandidateCredential, *, expected_kind=None,
                       quota=None) -> Outcome:
        """Opération déjà verrouillée par l'appelant. Verrouille candidat,
        session et génération, PUIS lit l'heure PostgreSQL, PUIS juge
        révocation et expirations (fenêtre de reprise, session). quota : le
        débit par opération n'a lieu qu'après TOUTES les validations."""
        refused = Outcome("rejected", "resume_unavailable", op["id"])
        if op["status"] != "committed" or op["kind"] not in (SIGN_IN, INVITATION):
            return refused
        if expected_kind is not None and op["kind"] != expected_kind:
            return refused
        secret = _decode(credential.secret) if isinstance(credential, CandidateCredential) else None
        if secret is None or _uuid(credential.candidate_id) is None:
            return refused
        cand = _row(conn, "SELECT * FROM auth_preauth_candidates WHERE id = :id FOR SHARE", id=credential.candidate_id)
        result = _row(conn, "SELECT * FROM auth_operation_results WHERE operation_id = :id", id=op["id"])
        if cand is None or result is None or result["candidate_id"] != cand["id"]:
            return refused
        session = _row(conn, "SELECT * FROM oryx_sessions WHERE id = :id FOR SHARE", id=result["session_id"])
        generation = _row(conn, "SELECT * FROM oryx_session_generations WHERE id = :id FOR SHARE",
                          id=result["generation_id"])
        at = self._clock(conn)
        if at >= result["resume_until"]:
            return refused
        if (cand["status"] != "bound" or cand["auth_operation_id"] != op["id"]
                or not self._candidate_hash_ok(cand, secret)):
            return refused
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
        if quota is not None:
            denied = self._debit_operation(conn, quota, op["id"], at)
            if denied is not None:
                return denied
        issued = IssuedSession(op["id"], session["id"], generation["id"], session["user_id"],
                               Redacted(b64url_encode(active)), session["absolute_expires_at"], resumed=True)
        return Outcome("resumed", None, op["id"], session=issued)

    def authenticate_session(self, credential: SessionCredential):
        """Principal de la session active présentée, ou None. La première
        présentation valide confirme la livraison (delivery_confirmed_at)."""
        with transaction(self._engine) as conn:
            locked = self._lock_session(conn, credential, generation_lock="FOR UPDATE")
            at = self._clock(conn)  # après les verrous : jamais un instant antérieur à une attente
            if not self._session_valid(locked, at):
                return None
            session, generation, _ = locked
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
            if self._invitation_openings_exhausted(conn, invitation["id"], at):
                # Même réponse publique qu'une invitation indisponible.
                logger.info("f2 invitation openings exhausted")
                return self._log("invitation", Outcome("rejected", "invitation_unavailable"))
            expires = min(at + OPERATION_TTL, invitation["expires_at"])
            operation_id = self._new_operation(conn, INVITATION, at, expires_at=expires, invitation_id=invitation["id"])
            candidate = self._new_candidate(conn, operation_id, INVITATION, at, expires)
        return self._log("invitation", Outcome("candidate_issued", None, operation_id, candidate))

    def _invitation_openings_exhausted(self, conn, invitation_id, at) -> bool:
        """Plafonds d'ouverture (R1 PR 4) : 3 par heure glissante, 12 sur la
        vie de l'invitation. Comptés sur les opérations invitation_activation
        CONSERVÉES (aucune purge ; statut indifférent : expirées, échouées et
        committées comptent), sous le verrou exclusif de l'invitation tenu
        par l'appelant, à l'heure lue après ce verrou."""
        recent, total = conn.execute(sa.text(
            "SELECT count(*) FILTER (WHERE created_at > :since), count(*) FROM auth_operations "
            "WHERE invitation_id = :i AND kind = :kind"
        ), {"i": invitation_id, "kind": INVITATION, "since": at - INVITATION_OPEN_WINDOW}).one()
        return recent >= INVITATION_OPEN_WINDOW_LIMIT or total >= INVITATION_OPEN_TOTAL_LIMIT

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

    # ------------------------------------------------------------------
    # Résultat de liaison (R1 PR 4, C.4.5 §3.2)
    # ------------------------------------------------------------------

    def read_email_linking_result(self, *, operation_id, session: SessionCredential, client_ip) -> Outcome:
        """Consultation du résultat durable d'une liaison par la session
        initiatrice EXACTE. N'appelle ni authenticate_session ni aucune
        écriture de session : aucune confirmation de livraison, aucune
        session, identité, opération ni OTP créés ; seule écriture : un
        resume_attempt par admission (quota par opération).

        Phase 1 (transaction sans verrou, aucune écriture) : la session
        désignée est jugée seule, SANS lire l'opération ; invalide ->
        session_invalid, quelle que soit operation_id.
        Phase 2 (transaction distincte, aucune conservation de verrou depuis
        la phase 1) : état de clé -> sujets IP -> opération FOR UPDATE ->
        session -> génération -> heure PostgreSQL ; session réauthentifiée
        (révocation ou expiration intermédiaires -> session_invalid) ; puis
        éligibilité (sinon result_unavailable), quota par opération, succès."""
        with transaction(self._engine) as conn:
            prevalidated = self._session_prevalidated(conn, session)
        if not prevalidated:
            return self._log("result", Outcome("rejected", "session_invalid"))
        self._hook("result.prevalidated")
        operation_id = _uuid(operation_id)
        try:
            ip_scope = Scope(IP, normalize_client_ip(client_ip))
        except InvalidClientIp:
            return self._log("result", Outcome("rejected", "ip_unavailable"))

        def body(conn, subjects):
            op = _row(conn, "SELECT * FROM auth_operations WHERE id = :id FOR UPDATE",
                      id=operation_id) if operation_id else None
            locked = self._lock_session(conn, session, generation_lock="FOR SHARE")
            result = identity = request = None
            if op is not None:
                result = _row(conn, "SELECT * FROM auth_operation_results WHERE operation_id = :id", id=op["id"])
            if result is not None and result["auth_identity_id"] is not None:
                identity = _row(conn, "SELECT * FROM auth_identities WHERE id = :id", id=result["auth_identity_id"])
            if result is not None and result["otp_request_id"] is not None:
                request = _row(conn, "SELECT * FROM auth_otp_requests WHERE id = :id", id=result["otp_request_id"])
            at = self._clock(conn)  # après tous les verrous
            if not self._session_valid(locked, at):
                return Outcome("rejected", "session_invalid")
            if not self._linking_result_ok(op, result, identity, request, locked[0], at):
                return Outcome("rejected", "result_unavailable")
            denied = self._debit_operation(conn, (subjects, ip_scope, "quota_operation_result"), op["id"], at)
            if denied is not None:
                return denied
            return Outcome("email_linked", None, op["id"])

        return self._log("result", self._with_subjects([ip_scope], body))

    def _session_prevalidated(self, conn, credential) -> bool:
        """Phase 1 : session et génération désignées, actives, non expirées,
        secret actif conforme ; lecture seule, sans verrou ni écriture."""
        locked = sessions.read_session(conn, credential)
        return locked is not None and self._session_valid(locked, self._clock(conn))

    def _linking_result_ok(self, op, result, identity, request, session, at) -> bool:
        """Relations exigées entre opération, session initiatrice, résultat
        durable, identité liée et preuve OTP, à l'instant `at`."""
        if op is None or op["kind"] != LINKING or op["status"] != "committed" or result is None:
            return False
        if (session["id"] != op["initiating_session_id"] or session["user_id"] != op["target_user_id"]
                or session["auth_method"] != INVITATION or op["initiating_session_method"] != INVITATION):
            return False
        if (op["user_id"] != op["target_user_id"] or op["auth_identity_id"] is None
                or op["identity_resolution"] not in ("new_identity", "existing_identity")):
            return False
        if (result["operation_kind"] != LINKING or result["operation_status"] != "committed"
                or result["result_version"] != RESULT_VERSION
                or result["result_payload"] != {"outcome": PAYLOAD[LINKING]}
                or result["committed_at"] != op["committed_at"] or result["auth_identity_id"] != op["auth_identity_id"]
                or result["session_id"] is not None or result["generation_id"] is not None
                or result["candidate_id"] is not None or not at < result["resume_until"]):
            return False
        if (identity is None or identity["user_id"] != op["target_user_id"]
                or (identity["provider"], identity["issuer"], identity["subject"])
                != (op["proven_provider"], op["proven_issuer"], op["proven_subject"])):
            return False
        return (request is not None and request["auth_operation_id"] == op["id"] and request["status"] == "verified"
                and request["verified_at"] == result["otp_verified_at"]
                and request["expires_at"] == result["otp_expires_at"])
