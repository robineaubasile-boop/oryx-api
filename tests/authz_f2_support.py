"""Outils des tests R1 PR 5 : sessions F2 réelles, deux utilisateurs A / B
et leurs ressources personnelles, composition de test F2 + /api/v2/me.

Les sessions sont émises par le VRAI moteur F2 (F2AuthService, doubles
Supabase / Resend sans réseau) puis, selon le scénario, confirmées par
authenticate_session (même effet que /session/resolve) ; le runner PR 5 les
juge avec le même trousseau. Aucune clé, aucune URL de production.

compose_test_app réunit l'application F2 isolée (PR 3 / PR 4, inchangée)
et l'application personnelle isolée (PR 5) derrière un aiguillage par
préfixe : même PostgreSQL, mêmes clés, mêmes sessions persistantes. Seuls
les tests construisent cette composition.
"""
import asyncio
import json
import uuid

import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.service import F2AuthService
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

    def runner(self, *, clock=None, keyring=None, engine=None):
        kwargs = {"fault_hook": self.hook}
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

def personal_app(env, *, clock=None):
    return build_isolated_personal_app(engine=env.engine, keyring=env.keyring, allowed_origins=[ORIGIN], clock=clock,
                                       fault_hook=env.hook)


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
