"""Interfaces fournisseurs F2 et validation stricte de leurs réponses.

Flux retenu (C.2.1 §28) : Supabase generateLink -> HMAC OTP local ->
Resend -> Supabase verifyOtp -> finalisation Oryx.

Preuve fournisseur (C.2.1-A, option 1) : la réponse de verifyOtp reçue
DIRECTEMENT par le backend depuis le projet Supabase configuré (TLS
vérifié, aucune redirection suivie, URL jamais dérivée d'une entrée
client) est la preuve. Elle n'est acceptée que si :

  - HTTP 200 et corps JSON objet contenant un objet « user » ;
  - user.id est un UUID canonique (minuscule) : c'est le subject ;
  - user.aud vaut « authenticated » ;
  - user.email, normalisé (D1), est EXACTEMENT l'e-mail de l'opération ;
  - user.email_confirmed_at est une chaîne non vide ;
  - access_token est une chaîne non vide (présence seulement : le jeton
    n'est jamais décodé, journalisé, persisté ni utilisé pour décider).

L'issuer est celui de la CONFIGURATION (https://<projet>/auth/v1), jamais
lu dans la réponse. Un 4xx est un rejet certain (aucune faute de saisie
supposée) ; un 200 non conforme, un 3xx, un 5xx, un délai dépassé ou une
coupure sont INCERTAINS : jamais un succès, jamais une session.

Ces classes ne transportent aucun jeton fournisseur : GeneratedOtp ne
garde que le code (masqué par repr), VerifiedIdentity que le subject et
l'e-mail.
"""
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol, Union

from core.auth_f2.crypto import is_valid_otp_code
from core.auth_f2.normalize import InvalidEmail, normalize_email

_ERROR_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")
_ISSUER_RE = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?/auth/v1")


@dataclass(frozen=True)
class ProviderRejected:
    """Refus certain du fournisseur (code d'erreur assaini, sans donnée)."""
    code: str


@dataclass(frozen=True)
class ProviderUncertain:
    """Issue inconnue : délai, coupure, 5xx, réponse non conforme."""
    reason: str


@dataclass(frozen=True)
class GeneratedOtp:
    code: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    email: str = field(repr=False)


@dataclass(frozen=True)
class MailSent:
    pass


GenerateOutcome = Union[GeneratedOtp, ProviderRejected, ProviderUncertain]
VerifyOutcome = Union[VerifiedIdentity, ProviderRejected, ProviderUncertain]
SendOutcome = Union[MailSent, ProviderRejected, ProviderUncertain]


class SupabaseOtpProvider(Protocol):
    """issuer : 'https://<projet>/auth/v1' du projet autorisé (configuration)."""
    issuer: str

    def generate_email_otp(self, email: str) -> GenerateOutcome: ...

    def verify_email_otp(self, email: str, code: str) -> VerifyOutcome: ...


class OtpMailer(Protocol):
    def send_otp(self, *, to: str, code: str, idempotency_key: str) -> SendOutcome: ...


def is_valid_issuer(issuer) -> bool:
    return isinstance(issuer, str) and _ISSUER_RE.fullmatch(issuer) is not None


def error_code(status: int, body) -> str:
    """Code d'erreur assaini : body.error_code ou body.code s'il est de la
    forme [a-z0-9_]{1,64}, sinon 'http_<statut>'. Aucun texte libre."""
    if isinstance(body, dict):
        for key in ("error_code", "code"):
            value = body.get(key)
            if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value):
                return value
    return f"http_{int(status)}"


def _same_email(raw, expected: str) -> bool:
    try:
        return normalize_email(raw) == expected
    except InvalidEmail:
        return False


def parse_generate_link(status: int, body, expected_email: str) -> GenerateOutcome:
    """Réponse de POST /auth/v1/admin/generate_link (type magiclink).

    Seul un 200 portant un email_otp de huit chiffres ASCII et l'e-mail
    demandé produit un code ; toute autre sortie est refusée sans envoi
    (aucun OTP non conforme n'est jamais transmis à Resend)."""
    if status == 200:
        code = body.get("email_otp") if isinstance(body, dict) else None
        if is_valid_otp_code(code) and _same_email(body.get("email"), expected_email):
            return GeneratedOtp(code=code)
        return ProviderRejected("nonconforming_output")
    if 400 <= status < 500:
        return ProviderRejected(error_code(status, body))
    return ProviderUncertain(f"http_{int(status)}")


def parse_verify(status: int, body, expected_email: str) -> VerifyOutcome:
    """Réponse de POST /auth/v1/verify (type email) : voir la docstring du
    module pour les contrôles de la preuve."""
    if 400 <= status < 500:
        return ProviderRejected(error_code(status, body))
    if status != 200:
        return ProviderUncertain(f"http_{int(status)}")
    if not isinstance(body, dict) or not isinstance(body.get("user"), dict):
        return ProviderUncertain("nonconforming_success")
    user = body["user"]
    subject = user.get("id")
    try:
        canonical = isinstance(subject, str) and str(uuid.UUID(subject)) == subject
    except ValueError:
        canonical = False
    confirmed = user.get("email_confirmed_at")
    token = body.get("access_token")
    if not (canonical and user.get("aud") == "authenticated" and _same_email(user.get("email"), expected_email)
            and isinstance(confirmed, str) and confirmed and isinstance(token, str) and token):
        return ProviderUncertain("nonconforming_success")
    return VerifiedIdentity(subject=subject, email=expected_email)


def parse_resend(status: int, body) -> SendOutcome:
    """Réponse de POST https://api.resend.com/emails. 200 avec un id :
    envoyé. 409 (requête idempotente concurrente) : incertain. Autre 4xx
    (dont 429) : non envoyé. Le reste : incertain."""
    if status == 200 and isinstance(body, dict) and isinstance(body.get("id"), str) and body["id"]:
        return MailSent()
    if status == 409:
        return ProviderUncertain("http_409")
    if 400 <= status < 500:
        return ProviderRejected(error_code(status, body))
    return ProviderUncertain(f"http_{int(status)}")
