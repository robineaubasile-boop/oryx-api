"""Processus de T1 (R1 PR 4, C.4.5 §10.4) lancés en parallèle
(multiprocessing, contexte spawn).

Chaque processus construit SA PROPRE application isolée (moteur F2 réel,
admission HTTP réelle) sur SON PROPRE moteur SQLAlchemy (NullPool) : ses
connexions PostgreSQL sont indépendantes de celles des autres processus et
du processus de test. Les pg_backend_pid() de toutes ses connexions sont
relevés (événement « connect ») et rapportés pour preuve. Le navigateur
simulé se présente avec l'IP du scénario (pair TCP lu par
PeerAddressResolver, normalisé par le moteur). Les secrets transitent en
chaîne et ne sont jamais rapportés.
"""
import sqlalchemy as sa

from core.auth_f2.normalize import normalize_client_ip
from core.auth_f2_http.client_ip import PeerAddressResolver
from core.auth_f2_http.isolated import build_isolated_f2_app
from tests.f2_concurrency_worker import CountingSupabase, SilentMailer
from tests.f2_http_support import ORIGIN, Browser, csrf_keyring
from tests.f2_support import make_keyring


def worker(url, args, barrier, queue):
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    pids = set()

    @sa.event.listens_for(engine, "connect")
    def record_pid(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SELECT pg_backend_pid()")
        pids.add(cursor.fetchone()[0])
        cursor.close()

    try:
        app = build_isolated_f2_app(engine=engine, keyring=make_keyring(), supabase=CountingSupabase(),
                                    mailer=SilentMailer(), csrf_keyring=csrf_keyring(),
                                    ip_resolver=PeerAddressResolver(), allowed_origins=[ORIGIN])
        browser = Browser(app, peer=(args["ip"], 41000))
        browser.bootstrap()
        browser.jar[args["cookie_name"]] = args["cookie_value"]
        barrier.wait(timeout=120)
        response = browser.post(args["path"], args["body"])
        queue.put(("ok", response.status_code, response.json(), sorted(pids), normalize_client_ip(args["ip"]),
                   response.headers.get_list("set-cookie") != []))
    except BaseException as exc:  # noqa: BLE001 — rapporté au processus de test
        queue.put(("error", f"{type(exc).__name__}: {str(exc)[:300]}", None, sorted(pids), None, None))
    finally:
        engine.dispose()
