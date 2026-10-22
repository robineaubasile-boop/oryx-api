"""Normalisation (C.2.1-A D1, D2) et validation des réponses fournisseurs
(C.2.1 §28, C.2.1-A option 1) — R1 PR 2.

Les adaptateurs HTTP sont exercés avec httpx.MockTransport : aucune
requête ne quitte le processus. Aucune clé réelle.
"""
import json
import uuid

import httpx
import pytest

from core.auth_f2.crypto import Redacted
from core.auth_f2.http_providers import HttpResendMailer, HttpSupabaseOtpProvider, ProviderConfigError
from core.auth_f2.normalize import InvalidClientIp, InvalidEmail, normalize_client_ip, normalize_email
from core.auth_f2.providers import (
    GeneratedOtp,
    MailSent,
    ProviderRejected,
    ProviderUncertain,
    VerifiedIdentity,
    is_valid_issuer,
    parse_generate_link,
    parse_resend,
    parse_verify,
)

EMAIL = "alice+oryx@example.com"
SUBJECT = "0b5c4f86-6a8e-4f43-9b3c-2f6b5d1c9e10"


# --------------------------------------------------------------------------
# D1 — e-mail
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Alice+Oryx@Example.COM", "alice+oryx@example.com"),
    ("  a.b.c@d.e\t\n", "a.b.c@d.e"),
    ("first.last+tag@sub.example.org", "first.last+tag@sub.example.org"),
    ("x@y", "x@y"),
])
def test_email_normalization(raw, expected):
    assert normalize_email(raw) == expected
    assert normalize_email(expected) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "no-at", "two@@at.com", "a@b@c", "@example.com", "alice@", "al ice@example.com", "alice@exa mple.com",
    "alice\t@example.com", "alicé@example.com", "alice@exämple.com", "ａlice@example.com", "alice\x00@example.com",
    "alice\x7f@example.com", "a" * 245 + "@example.com", None, b"alice@example.com", " alice@example.com",
])
def test_email_rejections(raw):
    with pytest.raises(InvalidEmail):
        normalize_email(raw)


def test_email_limit_is_254_bytes():
    local = "a" * (254 - len("@example.com"))
    assert normalize_email(local + "@example.com") == local + "@example.com"
    with pytest.raises(InvalidEmail):
        normalize_email("a" + local + "@example.com")


# --------------------------------------------------------------------------
# D2 — IP
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("203.0.113.7", "ipv4:203.0.113.7"),
    ("::ffff:203.0.113.7", "ipv4:203.0.113.7"),
    ("2001:db8:1:2:3:4:5:6", "ipv6:2001:db8:1:2::/64"),
    ("2001:DB8:1:2::ffff", "ipv6:2001:db8:1:2::/64"),
    ("2001:db8:1:3::1", "ipv6:2001:db8:1:3::/64"),
    ("::1", "ipv6:::/64"),
])
def test_ip_normalization(raw, expected):
    assert normalize_client_ip(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", " 203.0.113.7", "203.0.113.7 ", "fe80::1%eth0", "203.0.113.7, 10.0.0.1", "unknown", "300.1.1.1",
    "203.0.113.7:443", b"203.0.113.7", 3405803783,
])
def test_ip_rejections_fail_closed(raw):
    with pytest.raises(InvalidClientIp):
        normalize_client_ip(raw)


# --------------------------------------------------------------------------
# Réponses Supabase / Resend
# --------------------------------------------------------------------------

def verify_body(**overrides):
    body = {"access_token": "header.payload.signature", "token_type": "bearer", "refresh_token": "r",
            "user": {"id": SUBJECT, "aud": "authenticated", "email": EMAIL, "email_confirmed_at": "2026-10-22T10:00:00Z"}}
    for key, value in overrides.items():
        if key.startswith("user_"):
            body["user"][key[5:]] = value
        else:
            body[key] = value
    return body


def test_verify_accepts_only_a_fully_consistent_success():
    proof = parse_verify(200, verify_body(), EMAIL)
    assert proof == VerifiedIdentity(subject=SUBJECT, email=EMAIL)
    assert EMAIL not in repr(proof) and "header" not in repr(proof)
    assert parse_verify(200, verify_body(user_email="Alice+Oryx@EXAMPLE.com"), EMAIL) == proof


@pytest.mark.parametrize("body", [
    verify_body(user_id=SUBJECT.upper()), verify_body(user_id="not-a-uuid"), verify_body(user_id=None),
    verify_body(user_aud="anon"), verify_body(user_email="mallory@example.com"), verify_body(user_email=None),
    verify_body(user_email_confirmed_at=None), verify_body(user_email_confirmed_at=""), verify_body(access_token=""),
    verify_body(access_token=None), verify_body(user=None), {"user": "x"}, [], None, "ok",
])
def test_verify_nonconforming_success_is_uncertain_never_a_proof(body):
    assert parse_verify(200, body, EMAIL) == ProviderUncertain("nonconforming_success")


@pytest.mark.parametrize("status,body,expected", [
    (403, {"code": 403, "error_code": "otp_expired", "msg": "Token has expired or is invalid"},
     ProviderRejected("otp_expired")),
    (400, {"error_code": "Bad Code With Spaces"}, ProviderRejected("http_400")),
    (429, {"error_code": "over_request_rate_limit"}, ProviderRejected("over_request_rate_limit")),
    (500, {"error_code": "unexpected_failure"}, ProviderUncertain("http_500")),
    (502, None, ProviderUncertain("http_502")),
    (302, None, ProviderUncertain("http_302")),
    (201, verify_body(), ProviderUncertain("http_201")),
])
def test_verify_status_mapping(status, body, expected):
    assert parse_verify(status, body, EMAIL) == expected


def test_generate_link_requires_eight_ascii_digits_and_the_requested_email():
    assert parse_generate_link(200, {"email_otp": "00123456", "email": EMAIL}, EMAIL) == GeneratedOtp("00123456")
    assert "00123456" not in repr(GeneratedOtp("00123456"))
    for body in ({"email_otp": "123456", "email": EMAIL}, {"email_otp": "001234567", "email": EMAIL},
                 {"email_otp": 123456, "email": EMAIL}, {"email_otp": "００１２３４５６", "email": EMAIL},
                 {"email_otp": "00123456", "email": "other@example.com"}, {"email_otp": "00123456"}, {}, None):
        assert parse_generate_link(200, body, EMAIL) == ProviderRejected("nonconforming_output")
    assert parse_generate_link(422, {"error_code": "email_address_invalid"}, EMAIL) == ProviderRejected(
        "email_address_invalid")
    assert parse_generate_link(503, None, EMAIL) == ProviderUncertain("http_503")


def test_resend_status_mapping():
    assert parse_resend(200, {"id": "49a3999c"}) == MailSent()
    assert parse_resend(200, {}) == ProviderUncertain("http_200")
    assert parse_resend(409, {"name": "concurrent_idempotent_requests"}) == ProviderUncertain("http_409")
    assert parse_resend(429, {"name": "rate_limit_exceeded"}) == ProviderRejected("http_429")
    assert parse_resend(422, {"error_code": "validation_error"}) == ProviderRejected("validation_error")
    assert parse_resend(500, None) == ProviderUncertain("http_500")


def test_issuer_format():
    assert is_valid_issuer("https://abcd.supabase.co/auth/v1")
    for bad in ("http://abcd.supabase.co/auth/v1", "https://abcd.supabase.co/auth/v1/", "https://abcd.supabase.co",
                "https://user@abcd.supabase.co/auth/v1", None):
        assert not is_valid_issuer(bad)


# --------------------------------------------------------------------------
# Adaptateurs HTTP (MockTransport, aucun réseau)
# --------------------------------------------------------------------------

def supabase_with(handler):
    return HttpSupabaseOtpProvider(base_url="https://oryx-auth-fake.supabase.co", service_key=Redacted("svc-fake"),
                                   api_key=Redacted("anon-fake"), transport=httpx.MockTransport(handler))


def test_supabase_adapter_requests_and_proof():
    seen = []

    def handler(request):
        seen.append(request)
        payload = json.loads(request.content)
        if request.url.path == "/auth/v1/admin/generate_link":
            return httpx.Response(200, json={"email_otp": "00123456", "email": payload["email"], "id": SUBJECT,
                                             "hashed_token": "h", "action_link": "https://x"})
        return httpx.Response(200, json=verify_body())

    provider = supabase_with(handler)
    assert provider.issuer == "https://oryx-auth-fake.supabase.co/auth/v1"
    assert provider.generate_email_otp(EMAIL) == GeneratedOtp("00123456")
    assert provider.verify_email_otp(EMAIL, "00123456") == VerifiedIdentity(SUBJECT, EMAIL)
    assert [r.url.host for r in seen] == ["oryx-auth-fake.supabase.co"] * 2
    assert json.loads(seen[0].content) == {"type": "magiclink", "email": EMAIL}
    assert json.loads(seen[1].content) == {"type": "email", "email": EMAIL, "token": "00123456"}
    assert seen[0].headers["apikey"] == "svc-fake" and seen[1].headers["apikey"] == "anon-fake"
    assert "svc-fake" not in repr(provider) and "anon-fake" not in repr(provider)


def test_supabase_adapter_never_follows_redirects_and_maps_transport_errors():
    calls = []

    def redirect(request):
        calls.append(request.url)
        return httpx.Response(307, headers={"location": "https://evil.example/auth/v1/verify"})

    assert supabase_with(redirect).verify_email_otp(EMAIL, "00123456") == ProviderUncertain("http_307")
    assert len(calls) == 1

    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)

    assert supabase_with(timeout).verify_email_otp(EMAIL, "00123456") == ProviderUncertain("transport")
    assert supabase_with(timeout).generate_email_otp(EMAIL) == ProviderUncertain("transport")

    def not_json(request):
        return httpx.Response(200, content=b"<html>")

    assert supabase_with(not_json).verify_email_otp(EMAIL, "00123456") == ProviderUncertain("nonconforming_success")


def test_http_clients_ignore_environment_proxies_and_verify_tls():
    provider = supabase_with(lambda r: httpx.Response(500))
    client = provider._client
    assert client.follow_redirects is False and client._trust_env is False
    assert client.timeout.read == 10.0


@pytest.mark.parametrize("base_url", ["http://x.supabase.co", "https://x.supabase.co/", "https://x.supabase.co/auth",
                                      "https://x.supabase.co?y", "x.supabase.co", None])
def test_supabase_adapter_rejects_unsafe_configuration(base_url):
    with pytest.raises(ProviderConfigError):
        HttpSupabaseOtpProvider(base_url=base_url, service_key=Redacted("a"), api_key=Redacted("b"))


def test_supabase_adapter_requires_redacted_keys():
    with pytest.raises(ProviderConfigError):
        HttpSupabaseOtpProvider(base_url="https://x.supabase.co", service_key="plain", api_key=Redacted("b"))


def test_resend_adapter_sends_once_with_idempotency_key():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": str(uuid.uuid4())})

    mailer = HttpResendMailer(api_key=Redacted("re-fake"), sender="Oryx <auth@oryx.example>",
                              transport=httpx.MockTransport(handler))
    assert mailer.send_otp(to=EMAIL, code="00123456", idempotency_key="req-1") == MailSent()
    assert len(seen) == 1 and seen[0].url == "https://api.resend.com/emails"
    assert seen[0].headers["Idempotency-Key"] == "req-1"
    assert json.loads(seen[0].content)["to"] == [EMAIL]
    assert "re-fake" not in repr(mailer)

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    flaky = HttpResendMailer(api_key=Redacted("re-fake"), sender="auth@oryx.example", transport=httpx.MockTransport(boom))
    assert flaky.send_otp(to=EMAIL, code="00123456", idempotency_key="req-2") == ProviderUncertain("transport")
