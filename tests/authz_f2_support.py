"""Outils des tests R1 PR 5 : sessions F2 réelles, deux utilisateurs A / B
et leurs ressources personnelles, composition de test F2 + /api/v2/me.

Les sessions sont émises par le VRAI moteur F2 (F2AuthService, doubles
Supabase / Resend sans réseau) puis, selon le scénario, confirmées par
authenticate_session (même effet que /session/resolve) ; le runner PR 5 les
juge avec le même trousseau. Aucune clé, aucune URL de production.

compose_test_app réunit l'application F2 isolée (PR 3 / PR 4, inchangée)
et l'application personnelle isolée (PR 5 / PR 6) derrière un aiguillage
par préfixe : même PostgreSQL, mêmes clés F2, même trousseau CSRF (le
bootstrap de PR 3 émet les jetons que PUT /api/v2/me/level vérifie), mêmes
sessions persistantes. Seuls les tests construisent cette composition.
"""
import asyncio
import json
import time
import uuid

import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.service import F2AuthService
from core.auth_f2_http import csrf
from core.auth_f2_http.isolated import build_isolated_f2_app
from core.auth_f2_http.routes import NO_STORE_HEADERS
from core.authz_f2.runner import AuthorizedTransactionRunner
from core.authz_f2_http.isolated import build_isolated_personal_app
from tests.f2_http_support import ORIGIN, PEER, Browser, StaticIp, _send, csrf_keyring
from tests.f2_support import (
    APP_NAME,
    FakeMailer,
    FakeSupabase,
    TransactionProbe,
    clean_f2,
    create_profile_with_identity,
    make_keyring,
    scalar,
    service_engine,
)

ALICE, BOB = "alice@example.com", "bob@example.com"
USER_A, USER_B = "user-a", "user-b"
ME = "/api/v2/me"
ME_LEVEL = "/api/v2/me/level"
F2_PREFIX = "/api/auth/f2"
V2_PREFIX = "/api/v2"


class AuthzEnv:
    def __init__(self, pg_url):
        self.pg_url = pg_url
        self.observer = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
        clean_f2(self.observer)
        self.engine = service_engine(pg_url)
        self.keyring = make_keyring()
        probe = TransactionProbe(self.engine, self.observer)
        self.supabase, self.mailer = FakeSupabase(probe), FakeMailer(probe)
        abuse.initialize_abuse_key_state(self.engine, self.keyring, self.keyring.current_version("abuse"))
        self.service = F2AuthService(self.engine, self.keyring, self.supabase, self.mailer)
        self.hooks = {}
        self.hook_calls = []

    def hook(self, point):
        self.hook_calls.append(point)
        action = self.hooks.get(point)
        if action is not None:
            action()

    def runner(self, *, clock=None, keyring=None, engine=None, hook=None):
        kwargs = {"fault_hook": hook or self.hook}
        if clock is not None:
            kwargs["clock"] = clock
        return AuthorizedTransactionRunner(engine or self.engine, keyring or self.keyring, **kwargs)

    # -- sessions F2 réelles --------------------------------------------

    def sign_in(self, email=ALICE, user_id=USER_A, *, profile=True):
        """IssuedSession (livraison 'pending') émise par le moteur F2."""
        if profile:
            create_profile_with_identity(self.engine, self.supabase, email, user_id)
        start = self.service.start_sign_in(email=email, client_ip="203.0.113.9")
        assert start.status == "issued", start
        outcome = self.service.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                              code=self.mailer.last_code(email), client_ip="203.0.113.9")
        assert outcome.status == "authenticated", outcome
        return outcome.session

    def confirmed(self, email=ALICE, user_id=USER_A, *, profile=True):
        issued = self.sign_in(email, user_id, profile=profile)
        assert self.service.authenticate_session(issued.credential()) is not None
        return issued

    def generation(self, generation_id):
        with self.observer.connect() as conn:
            return conn.execute(sa.text("SELECT * FROM oryx_session_generations WHERE id = :g"),
                                {"g": generation_id}).mappings().one()

    def expire_at(self, session_id, sql_instant: str):
        """Échéance absolue de la session à l'instant SQL donné (création
        décalée d'autant : contrainte ck_oryx_sessions_absolute_ttl)."""
        with self.observer.begin() as conn:
            conn.execute(sa.text(
                "UPDATE oryx_sessions SET absolute_expires_at = t.at, created_at = t.at - interval '15 days' "
                f"FROM (SELECT {sql_instant} AS at) t WHERE id = :s"), {"s": session_id})

    def count(self, table, where="TRUE", **params):
        return scalar(self.observer, f"SELECT count(*) FROM {table} WHERE {where}", **params)

    def profile(self, user_id=USER_A):
        """(level, level_revision, xmin) : xmin inchangé = aucune écriture."""
        with self.observer.connect() as conn:
            row = conn.execute(sa.text("SELECT level, level_revision, xmin::text AS xmin FROM users WHERE id = :u"),
                               {"u": user_id}).mappings().one()
        return row["level"], row["level_revision"], row["xmin"]

    def set_profile(self, level, revision, user_id=USER_A):
        with self.observer.begin() as conn:
            conn.execute(sa.text("UPDATE users SET level = :l, level_revision = :r WHERE id = :u"),
                         {"l": level, "r": revision, "u": user_id})

    def waiting_on_lock(self) -> int:
        """Connexions du service (application_name) en attente d'un verrou."""
        return scalar(self.observer, "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a "
                                     "AND wait_event_type = 'Lock'", a=APP_NAME)

    # -- ressources personnelles ----------------------------------------

    def seed_resources(self):
        """Portefeuilles, thèses, conversations et livraisons de A et de B,
        avec des tickers communs (même ticker, propriétaires différents)."""
        ids = {}
        with self.observer.begin() as conn:
            for user, rows in ((USER_A, (("AIR.PA", 3.0), ("MC.PA", 1.0))), (USER_B, (("AIR.PA", 7.0),
                                                                                       ("TTE.PA", 2.0)))):
                conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant') ON CONFLICT DO NOTHING"),
                             {"u": user})
                ids[(user, "positions")] = [conn.execute(sa.text(
                    "INSERT INTO portfolio_positions (user_id, ticker, quantity, purchase_price, target_percent, "
                    "envelope) VALUES (:u, :t, :q, 100.0, 10.0, 'PEA') RETURNING id"),
                    {"u": user, "t": t, "q": q}).scalar_one() for t, q in rows]
                ids[(user, "theses")] = [conn.execute(sa.text(
                    "INSERT INTO investment_theses (user_id, ticker, thesis_text) VALUES (:u, 'AIR.PA', :x) "
                    "RETURNING id"), {"u": user, "x": f"thèse de {user}"}).scalar_one()]
                key = f"conv-{user}"
                conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                                     "VALUES (:k, :u, now())"), {"k": key, "u": user})
                turn = uuid.uuid4()
                conn.execute(sa.text(
                    "INSERT INTO assistant_deliveries (id, user_id, conversation_key, surface, source_user_turn_id, "
                    "delivery_ordinal, request_fingerprint, visible_content_fingerprint, response_payload, "
                    "private_metadata, status, delivery_schema_version, generated_at) VALUES (:id, :u, :k, "
                    "'decryptage', :src, 1, :rf, :vf, CAST(:p AS JSONB), CAST(:m AS JSONB), 'pending', 'v1', now())"),
                    {"id": turn, "u": user, "k": key, "src": uuid.uuid4(), "rf": "f" * 64, "vf": "e" * 64,
                     "p": json.dumps({"answer": f"réponse {user}"}), "m": json.dumps({"secret": f"privé {user}"})})
                ids[(user, "delivery")] = turn
                ids[(user, "conversation")] = key
        return ids

    def dispose(self):
        self.engine.dispose()
        self.observer.dispose()


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

def personal_app(env, *, clock=None, wall_clock=time.time):
    return build_isolated_personal_app(engine=env.engine, keyring=env.keyring, allowed_origins=[ORIGIN],
                                       csrf_keyring=csrf_keyring(), clock=clock, fault_hook=env.hook,
                                       wall_clock=wall_clock)


def f2_app(env):
    return build_isolated_f2_app(engine=env.engine, keyring=env.keyring, supabase=env.supabase, mailer=env.mailer,
                                 csrf_keyring=csrf_keyring(), ip_resolver=StaticIp(), allowed_origins=[ORIGIN])


class _Composition:
    """Aiguillage ASGI par préfixe : chaque application garde ses routes,
    ses contrôles et ses en-têtes ; tout autre chemin : 404 no-store."""

    def __init__(self, routes):
        self.routes = tuple(routes)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        for prefix, app in self.routes:
            if path == prefix or path.startswith(prefix + "/"):
                return await app(scope, receive, send)
        headers = [(k.encode(), v.encode()) for k, v in NO_STORE_HEADERS.items()]
        await send({"type": "http.response.start", "status": 404, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


def compose_test_app(env, *, clock=None):
    return _Composition([(F2_PREFIX, f2_app(env)), (V2_PREFIX, personal_app(env, clock=clock))])


def me_request(app, *, headers=None, path=ME, method="GET", content=None, peer=PEER):
    return asyncio.run(_send(app, method, path, headers=headers or {}, content=content, peer=peer))


def me_headers(generation_id=None, cookie=None, *, origin=None, fetch_site="same-origin", extra=None):
    """En-têtes d'un fetch GET même origine (Origin absent par défaut)."""
    sent = {}
    if fetch_site is not None:
        sent["sec-fetch-site"] = fetch_site
    if origin is not None:
        sent["origin"] = origin
    if generation_id is not None:
        sent["x-oryx-generation-id"] = str(generation_id)
    if cookie is not None:
        sent["cookie"] = cookie
    sent.update(extra or {})
    return sent


def active_cookie(issued) -> str:
    return f"__Host-oryx-active-{issued.generation_id}={issued.active_secret.reveal()}"


def csrf_pair(origin=ORIGIN, *, now=None, keyring=None):
    """(cookie de liaison, jeton) émis par le protocole PR 3 pour origin."""
    binding = csrf.new_binding()
    token = csrf.issue_token(keyring or csrf_keyring(), binding, origin, int(time.time() if now is None else now))
    return f"__Host-oryx-csrf-binding={binding}", token


def level_body(level="avance", expected_revision="0") -> bytes:
    return json.dumps({"level": level, "expected_revision": expected_revision}).encode()


def level_headers(issued=None, *, generation_id=None, cookie=None, origin=ORIGIN, fetch_site="same-origin",
                  csrf_token=True, content_type="application/json", extra=None):
    """En-têtes d'un fetch PUT même origine (Origin, CSRF, sélecteur,
    cookies actif + liaison CSRF). csrf_token : True (jeton valide émis
    ici), None (absent) ou une valeur explicite."""
    binding_cookie, token = csrf_pair()
    cookies = [binding_cookie]
    if issued is not None:
        cookies.insert(0, active_cookie(issued))
    sent = {}
    if origin is not None:
        sent["origin"] = origin
    if fetch_site is not None:
        sent["sec-fetch-site"] = fetch_site
    if csrf_token is True:
        sent["x-oryx-csrf"] = token
    elif csrf_token is not None:
        sent["x-oryx-csrf"] = csrf_token
    selector = generation_id if generation_id is not None else (issued.generation_id if issued else None)
    if selector is not None:
        sent["x-oryx-generation-id"] = str(selector)
    sent["cookie"] = "; ".join(cookies) if cookie is None else cookie
    if content_type is not None:
        sent["content-type"] = content_type
    sent.update(extra or {})
    return sent


def put_level(app, issued=None, *, body=None, headers=None, path=ME_LEVEL, **kwargs):
    content = level_body() if body is None else body
    return me_request(app, method="PUT", path=path, headers=headers or level_headers(issued, **kwargs),
                      content=content)


class PersonalBrowser(Browser):
    """Navigateur simulé sur la composition de test : routes F2 (héritées)
    et GET /api/v2/me avec SA boîte à cookies."""

    def me(self, generation_id, *, headers=None, store=True):
        sent = me_headers(generation_id, self.cookie_header() or None)
        sent.update(headers or {})
        response = me_request(self.app, headers=sent, peer=self.peer)
        if store:
            self.apply(response)
        return response

    def put_level(self, generation_id, level, expected_revision, *, headers=None, store=True):
        """PUT /api/v2/me/level avec SA boîte à cookies et le jeton CSRF
        obtenu par le bootstrap de PR 3 (application F2 de la composition)."""
        sent = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-oryx-generation-id": str(generation_id),
                "content-type": "application/json", "cookie": self.cookie_header()}
        if self.csrf is not None:
            sent["x-oryx-csrf"] = self.csrf
        sent.update(headers or {})
        response = me_request(self.app, method="PUT", path=ME_LEVEL, headers=sent, peer=self.peer,
                              content=level_body(level, expected_revision))
        if store:
            self.apply(response)
        return response


class RawResponse:
    """Réponse d'un appel ASGI brut (en-têtes en minuscules, dans l'ordre)."""

    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.header_list = headers
        self.content = content

    def get_list(self, name):
        return [v for k, v in self.header_list if k == name]

    def json(self):
        return json.loads(self.content)


def raw_request(app, *, method="PUT", path=ME_LEVEL, query=b"", headers=(), chunks=(), disconnect=False):
    """Appel ASGI direct : les en-têtes et les fragments de corps donnés
    sont EXACTEMENT ceux que reçoit l'application (aucun Content-Length ni
    Transfer-Encoding ajouté par un client). disconnect : http.disconnect
    au lieu de la fin du corps."""
    async def call():
        messages = [{"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks]
        if disconnect:
            messages.append({"type": "http.disconnect"})
        else:
            messages.append({"type": "http.request", "body": b"", "more_body": False})
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
                 "headers": [(k.lower().encode("latin-1"), v if isinstance(v, bytes) else v.encode("latin-1"))
                             for k, v in headers],
                 "client": PEER, "server": ("oryx.test", 443)}
        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return RawResponse(start["status"], [(k.decode("latin-1").lower(), v.decode("latin-1"))
                                             for k, v in start["headers"]], body)

    return asyncio.run(call())


def raw_level_headers(issued, **kwargs):
    """En-têtes d'un PUT valide pour raw_request : Host explicite, sans
    Content-Length (à ajouter par le test)."""
    return [("host", "oryx.test")] + list(level_headers(issued, **kwargs).items())
