"""Outils des tests R1 PR 8 : AnalysisSession et InvestmentThesis écrites
DIRECTEMENT en SQL (données historiques, dates nulles, égales, anciennes ou
infinies, tickers atypiques, provenances incohérentes), observation exacte
(xmin : inchangé = aucune écriture) et requêtes HTTP des trois lectures.

Aucune donnée réelle, aucune base distante : la base jetable des tests.
"""
import base64
import json
import uuid

import sqlalchemy as sa

from tests.authz_f2_support import USER_A, active_cookie, me_headers, me_request, raw_request
from tests.f2_support import rows, scalar

OVERVIEW = "/api/v2/me/analysis-overview"
THESES = "/api/v2/me/theses"


def thesis_path(thesis_id) -> str:
    return f"{THESES}/{thesis_id}"


def ensure_user(conn, user_id):
    conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant') ON CONFLICT DO NOTHING"),
                 {"u": user_id})


def insert_session(env, user_id=USER_A, ticker="AIR.PA", status="in_progress", current_step="business",
                   started_at="2026-10-01 10:00:00+00", updated_at=None, session_id=None):
    """AnalysisSession écrite telle quelle (dates SQL littérales)."""
    session_id = session_id or uuid.uuid4()
    with env.observer.begin() as conn:
        ensure_user(conn, user_id)
        conn.execute(sa.text(
            "INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, updated_at) "
            "VALUES (:id, :u, :t, :s, :step, CAST(:start AS timestamptz), CAST(:upd AS timestamptz))"),
            {"id": session_id, "u": user_id, "t": ticker, "s": status, "step": current_step, "start": started_at,
             "upd": updated_at or started_at})
    return session_id


def insert_thesis(env, user_id=USER_A, ticker="AIR.PA", text="Thèse.", created_at="2026-10-02 12:00:00",
                  session_id=None):
    """InvestmentThesis écrite telle quelle ; created_at : littéral SQL
    TIMESTAMP (« infinity » compris) ou None (NULL historique)."""
    with env.observer.begin() as conn:
        ensure_user(conn, user_id)
        return conn.execute(sa.text(
            "INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at, analysis_session_id) "
            "VALUES (:u, :t, :x, CAST(:c AS timestamp), :s) RETURNING id"),
            {"u": user_id, "t": ticker, "x": text, "c": created_at, "s": session_id}).scalar_one()


def write_state(env):
    """Empreinte des écritures : xmin de chaque ligne des tables lues et des
    tables pédagogiques / F2 (identique = aucune écriture)."""
    state = {}
    for table in ("investment_theses", "analysis_sessions", "users", "oryx_sessions", "oryx_session_generations",
                  "assistant_deliveries", "cognitive_events", "user_statements", "analysis_facts"):
        state[table] = [tuple(r) for r in rows(env.observer, f"SELECT ctid::text, xmin::text FROM {table} ORDER BY 1")]
    return state


def count(env, table):
    return scalar(env.observer, f"SELECT count(*) FROM {table}")


def get(app, issued, path, *, query=None, headers=None):
    url = path if query is None else f"{path}?{query}"
    return me_request(app, path=url, headers=headers or me_headers(issued.generation_id, active_cookie(issued)))


def raw_get(app, issued, path, query=b"", *, headers=None, chunks=(), disconnect=False):
    """GET ASGI brut : query et chemin EXACTS (aucune normalisation client)."""
    sent = headers if headers is not None else [("host", "oryx.test")] + list(
        me_headers(issued.generation_id, active_cookie(issued)).items())
    return raw_request(app, method="GET", path=path, query=query, headers=sent, chunks=chunks, disconnect=disconnect)


def cursor_of(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decoded_cursor(text) -> dict:
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))


def walk(app, issued, path, *, limit, extra=""):
    """Parcours complet d'une liste par curseurs ; [(page JSON)]."""
    pages = []
    cursor = None
    while True:
        query = f"limit={limit}" + extra + ("" if cursor is None else f"&cursor={cursor}")
        response = get(app, issued, path, query=query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 10000
