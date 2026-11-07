"""Outils des tests R1 PR 9-C3 : orchestration HTTP F2 Décrypter (dormante)
contre un VRAI PostgreSQL, sous le VRAI runner, le registre de PRODUCTION
(vingt commandes) et la VRAIE application personnelle isolée
(build_isolated_personal_app avec une DecryptageHttpConfig).

HttpEnv étend CanonicalEnv (R1 PR 9-C2) : sessions F2 réelles, réservation,
génération, finalisation, replay et ACK réels ; transports EODHD / FMP /
Claude simulés (sans réseau) sur une horloge monotone simulée commune au
budget C3 et au budget B2. Les requêtes HTTP sont pilotées par
httpx.ASGITransport, une boucle asyncio par requête, ou par appel ASGI brut
(en-têtes et fragments exacts). Les états committés sont toujours observés
par une connexion indépendante.
"""
import asyncio
import json
import threading
import uuid

import httpx

from core.authz_f2_http import decryptage as c3
from core.authz_f2_http.isolated import build_isolated_personal_app
from tests.authz_f2_pr9b1_support import CONV
from tests.authz_f2_pr9b2_support import MODEL, FakeClaude, FakeClock, FakeEodhd, FakeFmp
from tests.authz_f2_pr9c2_support import CanonicalEnv
from tests.authz_f2_support import active_cookie, csrf_pair, raw_request
from tests.f2_http_support import ORIGIN, PEER, csrf_keyring

TURNS = "/api/v2/me/decryptage/turns"
REPLAY = "/api/v2/me/decryptage/replay"
ACK = "/api/v2/me/decryptage/ack"
C3_PATHS = (TURNS, REPLAY, ACK)


def turn_fields(turn_id=None, *, conversation_key=CONV, ticker_input="MC.PA", question="Pourquoi ?", context="",
                last_method_id=None) -> dict:
    return {"conversation_key": conversation_key, "client_turn_id": str(turn_id or uuid.uuid4()),
            "ticker_input": ticker_input, "question": question, "context": context, "last_method_id": last_method_id}


def body_of(fields) -> bytes:
    return json.dumps(fields).encode()


def identity_fields(reservation_id, assistant_turn_id, conversation_key=CONV) -> dict:
    return {"reservation_id": str(reservation_id), "conversation_key": conversation_key,
            "assistant_turn_id": str(assistant_turn_id)}


def c3_headers(issued=None, *, generation_id=None, cookie=None, origin=ORIGIN, fetch_site="same-origin", csrf=True,
               content_type="application/json", extra=None) -> dict:
    """En-têtes d'un POST C3 même origine : Origin, CSRF oryx-f2-csrf-v1,
    sélecteur, cookie actif EXACT + liaison CSRF."""
    binding, token = csrf_pair()
    cookies = [binding] + ([active_cookie(issued)] if issued is not None else [])
    sent = {}
    if origin is not None:
        sent["origin"] = origin
    if fetch_site is not None:
        sent["sec-fetch-site"] = fetch_site
    if csrf is True:
        sent["x-oryx-csrf"] = token
    elif csrf is not None:
        sent["x-oryx-csrf"] = csrf
    selector = generation_id if generation_id is not None else (issued.generation_id if issued else None)
    if selector is not None:
        sent["x-oryx-generation-id"] = str(selector)
    # cookie : None (liaison + actif), chaîne brute, ou callable(liaison) -> chaîne
    # (le jeton CSRF reste lié à la liaison réellement envoyée).
    sent["cookie"] = "; ".join(cookies) if cookie is None else (cookie(binding) if callable(cookie) else cookie)
    if content_type is not None:
        sent["content-type"] = content_type
    sent.update(extra or {})
    return sent


async def _send(app, path, headers, content):
    transport = httpx.ASGITransport(app=app, client=PEER)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        return await client.request("POST", path, headers=headers, content=content)


def post(app, path, content, headers):
    return asyncio.run(_send(app, path, headers, content))


def raw_post(app, path, headers, chunks, **kwargs):
    return raw_request(app, method="POST", path=path, headers=[("host", "oryx.test")] + list(headers.items()),
                       chunks=chunks, **kwargs)


class Recorder:
    """Instances construites d'une classe (capacité, orchestrateur, runner) et
    leurs arguments : __init__ enveloppé sur la classe ELLE-MÊME (les contrôles
    de type exact restent vrais)."""

    def __init__(self, monkeypatch, cls):
        self.instances = []
        self.calls = []
        original = cls.__init__
        recorder = self

        def recording(instance, *args, **kwargs):
            original(instance, *args, **kwargs)
            recorder.instances.append(instance)
            recorder.calls.append((args, kwargs))

        monkeypatch.setattr(cls, "__init__", recording)


class HttpEnv(CanonicalEnv):
    """CanonicalEnv + application personnelle isolée AVEC C3."""

    def transports(self, clock=None, *, eodhd=None, fmp=None, claude=None):
        self.clock = clock or FakeClock()
        self.eodhd = eodhd or FakeEodhd(self.clock)
        self.fmp = fmp or FakeFmp(self.clock)
        self.claude = claude or FakeClaude(self.clock)
        return self.clock

    def config(self, *, decryptage_keys=False, model=MODEL, price_recent_max_age=None, **overrides):
        if not hasattr(self, "clock"):
            self.transports()
        fields = {"decryptage_keyring": self.decryptage_keys if decryptage_keys is False else decryptage_keys,
                  "eodhd": self.eodhd, "fmp": self.fmp, "claude": self.claude, "model": model,
                  "monotonic": self.clock, "price_recent_max_age": price_recent_max_age}
        fields.update(overrides)
        return c3.DecryptageHttpConfig(**fields)

    def c3_app(self, *, config=None, engine=None, clock=None, hook=None, portfolio_keys=False, **kwargs):
        return build_isolated_personal_app(
            engine=engine or self.engine, keyring=self.keyring, allowed_origins=[ORIGIN], csrf_keyring=csrf_keyring(),
            clock=clock, fault_hook=hook or self.hook,
            portfolio_keyring=self.portfolio_keys if portfolio_keys is False else portfolio_keys,
            decryptage=config or self.config(**kwargs))

    def provider_calls(self):
        return self.eodhd.methods() + self.fmp.methods() + self.claude.methods()


class Gate:
    """Bloque un appel fournisseur simulé jusqu'à `release` (fil de travail
    réellement en cours, aucune transaction ouverte : B2 n'en garde aucune)."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.count = 0

    def __call__(self):
        self.count += 1
        self.reached.set()
        assert self.release.wait(30), "porte non relâchée"


__all__ = ["ACK", "C3_PATHS", "CONV", "Gate", "HttpEnv", "REPLAY", "Recorder", "TURNS", "body_of", "c3_headers",
           "identity_fields", "post", "raw_post", "turn_fields"]
