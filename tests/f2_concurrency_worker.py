"""Processus de test F2 (R1 PR 2) lancés en parallèle (multiprocessing,
contexte spawn) : chacun ouvre SES connexions PostgreSQL et son propre
service, attend la barrière commune, puis exécute un scénario. Les
secrets transitent en chaîne (Redacted n'est pas sérialisable) et sont
reconstruits dans le processus.
"""
import sqlalchemy as sa

from core.auth_f2.crypto import Redacted
from core.auth_f2.providers import GeneratedOtp, MailSent, VerifiedIdentity
from core.auth_f2.service import CandidateCredential, F2AuthService, SessionCredential
from tests.f2_support import ISSUER, make_keyring

PURPOSES = ("candidate", "derive", "active", "otp", "invitation", "abuse")


class CountingSupabase:
    """Accepte tout code vérifié (la correspondance locale a déjà eu lieu) ;
    subject fixé par le scénario, sinon dérivé de l'e-mail."""
    issuer = ISSUER

    def __init__(self, subject=None):
        self.subject = subject
        self.generated = 0
        self.verified = 0

    def generate_email_otp(self, email):
        self.generated += 1
        return GeneratedOtp(code="24681357")

    def verify_email_otp(self, email, code):
        self.verified += 1
        return VerifiedIdentity(subject=self.subject or "00000000-0000-4000-8000-00000000abcd", email=email)


class SilentMailer:
    def send_otp(self, *, to, code, idempotency_key):
        return MailSent()


def keyring(abuse_versions):
    versions = {p: ("t1",) for p in PURPOSES}
    versions["abuse"] = tuple(abuse_versions)
    return make_keyring(versions, {**{p: "t1" for p in PURPOSES}, "abuse": abuse_versions[0]})


def candidate(args):
    return CandidateCredential(args["candidate_id"], Redacted(args["candidate_secret"]))


def session(args):
    return SessionCredential(args["generation_id"], Redacted(args["active_secret"]))


def scenario(svc, name, args):
    if name == "verify_unknown":
        return svc.verify_sign_in(operation_id=args["operation_id"], candidate=None, code="00000000",
                                  client_ip=args["ip"]).status
    if name == "verify_code":
        return svc.verify_sign_in(operation_id=args["operation_id"], candidate=candidate(args), code=args["code"],
                                  client_ip=args["ip"]).reason or "authenticated"
    if name == "issue":
        return svc.start_sign_in(email=args["email"], client_ip=args["ip"]).status
    if name == "verify_linking":
        return svc.verify_email_linking(operation_id=args["operation_id"], session=session(args), code=args["code"],
                                        client_ip=args["ip"]).reason or "linked"
    if name == "complete_invitation":
        return svc.complete_invitation_activation(operation_id=args["operation_id"], candidate=candidate(args)).status
    if name == "resume":
        return svc.resume_result(operation_id=args["operation_id"], candidate=candidate(args)).status
    if name == "revoke":
        return "revoked" if svc.revoke_session(args["session_id"], "security") else "not_revoked"
    if name == "authenticate":
        return "ok" if svc.authenticate_session(session(args)) else "none"
    raise ValueError(name)


def worker(url, name, args, abuse_versions, barrier, queue):
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    supabase = CountingSupabase(args.get("subject"))
    try:
        svc = F2AuthService(engine, keyring(abuse_versions), supabase, SilentMailer())
        barrier.wait(timeout=120)
        result = scenario(svc, name, args)
        queue.put(("ok", result, supabase.generated, supabase.verified))
    except BaseException as exc:  # noqa: BLE001 — rapporté au processus de test
        queue.put(("error", f"{type(exc).__name__}: {str(exc)[:300]}", supabase.generated, supabase.verified))
    finally:
        engine.dispose()
