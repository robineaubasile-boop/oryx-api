"""Adaptateurs HTTP Supabase et Resend — DORMANTS (jamais instanciés par
l'application dans R1 PR 2 ; les tests les exercent avec
httpx.MockTransport, sans aucun accès réseau).

Transport : vérification TLS active, aucune redirection suivie,
variables d'environnement de proxy ignorées (trust_env=False), délais
bornés. Les URL viennent de la configuration serveur, jamais d'une entrée
client. Les corps de requête et de réponse ne sont jamais journalisés ;
les clés API sont masquées par repr().

httpx n'est importé qu'à la construction (dépendance de test de la CI,
absente de requirements.txt tant que F2 est dormant).
"""
import re

from core.auth_f2.crypto import Redacted
from core.auth_f2.providers import (
    ProviderUncertain,
    is_valid_issuer,
    parse_generate_link,
    parse_resend,
    parse_verify,
)

_BASE_URL_RE = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?")
RESEND_URL = "https://api.resend.com"
TIMEOUT_SECONDS = 10.0


class ProviderConfigError(ValueError):
    """Configuration fournisseur invalide."""


def _client(base_url: str, transport):
    import httpx

    return httpx.Client(
        base_url=base_url, timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=False, verify=True,
        trust_env=False, transport=transport,
    )


def _post(client, path: str, *, headers: dict, payload: dict):
    """(statut, corps JSON ou None) ; None si la requête n'a pas abouti à
    une réponse HTTP (délai, coupure) : l'appelant conclut à l'incertitude."""
    import httpx

    try:
        response = client.post(path, headers=headers, json=payload)
    except httpx.HTTPError:
        return None
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


class HttpSupabaseOtpProvider:
    """base_url : 'https://<projet>.supabase.co' du projet autorisé ;
    issuer = base_url + '/auth/v1'. service_key sert à generate_link
    (administration), api_key à verify."""

    def __init__(self, *, base_url: str, service_key: Redacted, api_key: Redacted, transport=None):
        if not isinstance(base_url, str) or not _BASE_URL_RE.fullmatch(base_url):
            raise ProviderConfigError("URL Supabase : https://<hôte> sans chemin attendu")
        if not isinstance(service_key, Redacted) or not isinstance(api_key, Redacted):
            raise ProviderConfigError("clés Supabase : Redacted attendu")
        self.issuer = base_url + "/auth/v1"
        if not is_valid_issuer(self.issuer):
            raise ProviderConfigError("issuer Supabase invalide")
        self._service_key = service_key
        self._api_key = api_key
        self._client = _client(base_url, transport)

    def __repr__(self) -> str:
        return f"HttpSupabaseOtpProvider(issuer={self.issuer!r})"

    def generate_email_otp(self, email: str):
        key = self._service_key.reveal()
        result = _post(self._client, "/auth/v1/admin/generate_link",
                       headers={"apikey": key, "Authorization": f"Bearer {key}"},
                       payload={"type": "magiclink", "email": email})
        if result is None:
            return ProviderUncertain("transport")
        return parse_generate_link(result[0], result[1], email)

    def verify_email_otp(self, email: str, code: str):
        key = self._api_key.reveal()
        result = _post(self._client, "/auth/v1/verify",
                       headers={"apikey": key, "Authorization": f"Bearer {key}"},
                       payload={"type": "email", "email": email, "token": code})
        if result is None:
            return ProviderUncertain("transport")
        return parse_verify(result[0], result[1], email)


class HttpResendMailer:
    def __init__(self, *, api_key: Redacted, sender: str, transport=None):
        if not isinstance(api_key, Redacted):
            raise ProviderConfigError("clé Resend : Redacted attendu")
        if not isinstance(sender, str) or "@" not in sender:
            raise ProviderConfigError("expéditeur Resend invalide")
        self._api_key = api_key
        self._sender = sender
        self._client = _client(RESEND_URL, transport)

    def __repr__(self) -> str:
        return "HttpResendMailer()"

    def send_otp(self, *, to: str, code: str, idempotency_key: str):
        result = _post(
            self._client, "/emails",
            headers={"Authorization": f"Bearer {self._api_key.reveal()}", "Idempotency-Key": idempotency_key},
            payload={
                "from": self._sender, "to": [to], "subject": "Votre code de connexion Oryx",
                "text": f"Votre code Oryx : {code}\n\nIl est valable 10 minutes au plus. "
                        "Si vous n'êtes pas à l'origine de cette demande, ignorez ce message.",
            },
        )
        if result is None:
            return ProviderUncertain("transport")
        return parse_resend(result[0], result[1])
