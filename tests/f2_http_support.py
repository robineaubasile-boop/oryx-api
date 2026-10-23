"""Outils des tests HTTP F2 (R1 PR 3) : navigateur simulé, clés CSRF
FICTIVES, résolveur d'IP de test.

Le TestClient de Starlette 0.36.3 n'est pas compatible avec httpx 0.28.1
(verrou de la CI) : l'application est pilotée par httpx.ASGITransport,
une requête par boucle asyncio, ce qui permet aussi de fixer l'adresse du
pair TCP (scope ASGI « client ») pour les tests de résolution d'IP.

Browser tient SA PROPRE boîte à cookies (dict nom -> valeur), applique les
Set-Cookie reçus (Max-Age=0 : suppression) et envoie un en-tête Cookie
explicite : chaque test voit et contrôle exactement les cookies envoyés.
Aucune clé ni URL de production.
"""
import asyncio
import json

import httpx

from core.auth_f2_http.csrf import CsrfKeyring

ORIGIN = "https://oryx.test"
F2 = "/api/auth/f2"
PEER = ("192.0.2.10", 40000)


def csrf_keyring(versions=("c1",), current=None) -> CsrfKeyring:
    return CsrfKeyring({v: bytes([0xC0 + i]) * 32 for i, v in enumerate(versions)}, current or versions[0])


class StaticIp:
    """Résolveur de test : IP fixée par le test (jamais lue dans la requête)."""

    def __init__(self, ip="203.0.113.7"):
        self.ip = ip
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return self.ip


def parse_set_cookie(value: str) -> dict:
    parts = [p.strip() for p in value.split(";")]
    name, _, cookie_value = parts[0].partition("=")
    attrs = {}
    for part in parts[1:]:
        key, _, attr_value = part.partition("=")
        attrs[key.lower()] = attr_value
    return {"name": name, "value": cookie_value, "attrs": attrs, "raw": value}


async def _send(app, method, url, *, headers, content, peer):
    transport = httpx.ASGITransport(app=app, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        return await client.request(method, url, headers=headers, content=content)


class Browser:
    """Navigateur simulé, même origine par défaut."""

    def __init__(self, app, *, peer=PEER):
        self.app = app
        self.jar = {}
        self.peer = peer
        self.csrf = None

    def cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.jar.items())

    def apply(self, response):
        for raw in response.headers.get_list("set-cookie"):
            cookie = parse_set_cookie(raw)
            if cookie["attrs"].get("max-age") == "0":
                self.jar.pop(cookie["name"], None)
            else:
                self.jar[cookie["name"]] = cookie["value"]

    def request(self, method, path, *, json_body=None, content=None, headers=None, origin=ORIGIN, csrf=True,
                cookies=True, store=True):
        sent = {}
        if origin is not None:
            sent["origin"] = origin
        if csrf and self.csrf is not None:
            sent["x-oryx-csrf"] = self.csrf
        if cookies and self.jar:
            sent["cookie"] = self.cookie_header()
        if json_body is not None:
            content = json.dumps(json_body).encode()
            sent["content-type"] = "application/json"
        sent.update(headers or {})
        response = asyncio.run(_send(self.app, method, F2 + path, headers=sent, content=content, peer=self.peer))
        if store:
            self.apply(response)
        return response

    def bootstrap(self):
        response = self.request("GET", "/csrf/bootstrap", origin=None, headers={"sec-fetch-site": "same-origin"})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        return response

    def post(self, path, body, **kwargs):
        return self.request("POST", path, json_body=body, **kwargs)

    def start(self, email, **kwargs):
        return self.post("/sign-in/start", {"email": email}, **kwargs)

    def verify(self, start_body, code, **kwargs):
        return self.post("/sign-in/verify", {"operation_id": start_body["operation_id"],
                                             "candidate_id": start_body["candidate_id"], "code": code}, **kwargs)

    def resend(self, start_body, **kwargs):
        return self.post("/sign-in/resend", {"operation_id": start_body["operation_id"],
                                             "candidate_id": start_body["candidate_id"]}, **kwargs)

    def resume(self, start_body, **kwargs):
        return self.post("/sign-in/resume", {"operation_id": start_body["operation_id"],
                                             "candidate_id": start_body["candidate_id"]}, **kwargs)

    def resolve(self, generation_id, **kwargs):
        return self.post("/session/resolve", {"generation_id": generation_id}, **kwargs)

    def logout(self, generation_id, **kwargs):
        return self.post("/logout", {"generation_id": generation_id}, **kwargs)

    def actives(self):
        return {k: v for k, v in self.jar.items() if k.startswith("__Host-oryx-active-")}

    def candidates(self):
        return {k: v for k, v in self.jar.items() if k.startswith("__Host-oryx-candidate-")}


def set_cookies(response) -> list:
    return [parse_set_cookie(v) for v in response.headers.get_list("set-cookie")]


def active_cookies_set(response) -> list:
    return [c for c in set_cookies(response) if c["name"].startswith("__Host-oryx-active-")
            and c["attrs"].get("max-age") != "0"]
