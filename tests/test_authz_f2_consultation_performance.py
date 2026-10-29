"""Performance réelle des lectures R1 PR 8, sans migration ni index nouveau.

Jeu de charge (base jetable) : 10 000 AnalysisSession et 10 000
InvestmentThesis réparties entre 21 utilisateurs, dont 6 000 + 6 000 sur un
seul utilisateur (1 500 tickers, plusieurs sessions in_progress anormales
par ticker, étapes invalides, dates nulles et égales, tickers distincts par
la casse et à caractères spéciaux, provenances vérifiées et non).

Pour chaque parcours représentatif (synthèse : première et dernière page ;
thèses : première page, page profonde par curseur, filtre ticker, détail),
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) de l'instruction EXACTE de
l'adaptateur, sous SET LOCAL statement_timeout = '10s' comme le runner :
durée, scans, tris, lignes examinées et écartées, buffers, méthode et
mémoire de tri. Puis la même lecture par le VRAI runner (plan READ,
statement_timeout = 10s). Un dépassement est un échec du test. Le résumé
mesuré est écrit dans reports/r1_pr8_explain_pg<version>.json (artefact CI)
et reproduit dans docs/f2_r1_pr8.md ; aucune métrique n'est inventée.
"""
import json
import pathlib
import time
from datetime import datetime

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED
from tests.authz_f2_support import USER_A, AuthzEnv
from tests.f2_support import prepare_schema, scalar
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture

TIMEOUT_MS = 10_000
SEED = [
    "INSERT INTO users (id, level) SELECT 'perf-' || g, 'debutant' FROM generate_series(1, 20) g "
    "ON CONFLICT DO NOTHING",
    # 6 000 sessions de l'utilisateur concentré : 1 500 tickers, 1/3 in_progress
    # (plusieurs par ticker), étapes connues, nulles ou inconnues, dates égales.
    "INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, updated_at) "
    "SELECT gen_random_uuid(), :u, 'T' || lpad((g % 1500)::text, 4, '0'), "
    "(ARRAY['in_progress', 'completed', 'abandoned'])[1 + g % 3], "
    "(ARRAY['business', 'moat', 'chiffres', 'valorisation', 'risques', 'swot_final', NULL, 'inconnue'])[1 + g % 8], "
    "timestamptz '2026-01-01 00:00:00+00' + (g % 400) * interval '1 hour', "
    "timestamptz '2026-01-01 00:00:00+00' + (g % 700) * interval '1 hour' FROM generate_series(1, 6000) g",
    # 4 000 sessions de 20 autres utilisateurs, mêmes tickers.
    "INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, updated_at) "
    "SELECT gen_random_uuid(), 'perf-' || (1 + g % 20), 'T' || lpad((g % 1500)::text, 4, '0'), "
    "(ARRAY['in_progress', 'completed', 'abandoned'])[1 + g % 3], 'moat', now(), now() "
    "FROM generate_series(1, 4000) g",
    # 6 000 thèses de l'utilisateur concentré : 3 000 tickers (casse, caractères
    # spéciaux), 10 % de dates nulles, dates égales, provenance pour 1 sur 4.
    "INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at, analysis_session_id) "
    "SELECT :u, (ARRAY['T', 't', 'É-', 'A&B=']::text[])[1 + g % 4] || lpad((g % 750)::text, 4, '0'), "
    "repeat('thèse ', 200), CASE WHEN g % 10 = 0 THEN NULL WHEN g % 7 = 0 THEN timestamp '2026-03-01 00:00:00' "
    "ELSE timestamp '2025-01-01 00:00:00' + g * interval '1 minute' END, NULL FROM generate_series(1, 6000) g",
    "UPDATE investment_theses t SET analysis_session_id = s.id FROM (SELECT DISTINCT ON (ticker) id, ticker "
    "FROM analysis_sessions WHERE user_id = :u ORDER BY ticker, id) s WHERE t.user_id = :u AND t.ticker = s.ticker "
    "AND t.id % 4 = 0",
    "INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at) "
    "SELECT 'perf-' || (1 + g % 20), 'T' || lpad((g % 750)::text, 4, '0'), repeat('autre ', 200), "
    "timestamp '2025-06-01 00:00:00' + g * interval '1 minute' FROM generate_series(1, 4000) g",
    "ANALYZE analysis_sessions",
    "ANALYZE investment_theses",
]


@pytest.fixture(scope="module")
def loaded(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()
    env = AuthzEnv(pg_url)
    issued = env.confirmed()
    with env.observer.begin() as conn:
        for sql in SEED:
            conn.execute(sa.text(sql), {"u": USER_A})
    assert scalar(env.observer, "SELECT count(*) FROM analysis_sessions") == 10_000
    assert scalar(env.observer, "SELECT count(*) FROM investment_theses") == 10_000
    yield env, issued
    env.dispose()


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


def explain(env, sql, params):
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL statement_timeout = '10s'"))
        (document,) = conn.execute(sa.text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params).scalar_one()
    root = document["Plan"]
    nodes = []
    for node in _nodes(root):
        summary = {key: node[key] for key in ("Node Type", "Relation Name", "Actual Rows", "Actual Loops",
                                              "Rows Removed by Filter", "Sort Method", "Sort Space Used",
                                              "Sort Space Type", "Shared Hit Blocks", "Shared Read Blocks",
                                              "Temp Read Blocks", "Temp Written Blocks") if key in node}
        if summary.get("Node Type") in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Sort",
                                        "Incremental Sort", "WindowAgg", "HashAggregate", "Unique", "Hash Join",
                                        "Merge Join", "Nested Loop", "Hash Right Join", "Limit", "Append"):
            nodes.append(summary)
    return {"execution_ms": document["Execution Time"], "planning_ms": document["Planning Time"],
            "rows": root["Actual Rows"], "shared_hit": root.get("Shared Hit Blocks"),
            "shared_read": root.get("Shared Read Blocks"), "temp_written": root.get("Temp Written Blocks"),
            "nodes": nodes}


def scenarios(env):
    """(nom, instruction exacte, paramètres, commande équivalente)."""
    with env.observer.connect() as conn:
        tickers = [r[0] for r in conn.execute(sa.text(
            "SELECT DISTINCT ticker COLLATE \"C\" FROM (SELECT ticker FROM analysis_sessions WHERE user_id = :u "
            "AND status = 'in_progress' UNION SELECT ticker FROM investment_theses WHERE user_id = :u) k "
            "ORDER BY 1"), {"u": USER_A})]
        deep = conn.execute(sa.text("SELECT id, created_at FROM investment_theses WHERE user_id = :u "
                                    "ORDER BY created_at DESC NULLS LAST, id DESC OFFSET 5900 LIMIT 1"),
                            {"u": USER_A}).one()
        dated = conn.execute(sa.text("SELECT id, created_at FROM investment_theses WHERE user_id = :u "
                                     "AND created_at IS NOT NULL ORDER BY created_at DESC, id DESC OFFSET 4000 LIMIT 1"),
                             {"u": USER_A}).one()
        thesis = conn.execute(sa.text("SELECT max(id) FROM investment_theses WHERE user_id = :u"),
                              {"u": USER_A}).scalar_one()
    base = {"u": USER_A, "n": 51}
    last = tickers[-60]
    return [
        ("overview_first_page", _adapters._ANALYSIS_OVERVIEW_PAGE["first"], base, c.ListAnalysisOverviewPage(50, None)),
        ("overview_last_page", _adapters._ANALYSIS_OVERVIEW_PAGE["after"], {**base, "a": last},
         c.ListAnalysisOverviewPage(50, last)),
        ("theses_first_page", _adapters._THESES_PAGE["first"], base, c.ListThesesPage(50, None, None, None)),
        ("theses_deep_page_after_date", _adapters._THESES_PAGE["after_date"],
         {**base, "d": dated.created_at, "i": dated.id}, c.ListThesesPage(50, None, dated.id, dated.created_at)),
        ("theses_deep_page_after_null", _adapters._THESES_PAGE["after_null"], {**base, "i": deep.id},
         c.ListThesesPage(50, None, deep.id, None) if deep.created_at is None else None),
        ("theses_filter_ticker", _adapters._THESES_PAGE["first+ticker"], {**base, "t": "T0002"},
         c.ListThesesPage(50, "T0002", None, None)),
        ("thesis_detail", _adapters._READ_THESIS, {"u": USER_A, "id": thesis}, c.ReadThesis(thesis)),
    ]


def test_representative_plans_stay_far_below_the_statement_timeout(loaded):
    env, issued = loaded
    version = scalar(env.observer, "SHOW server_version")
    report = {"server_version": version, "measured_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
              "analysis_sessions": 10_000, "investment_theses": 10_000,
              "concentrated_user": {"analysis_sessions": 6_000, "investment_theses": 6_000}, "scenarios": {}}
    for name, sql, params, command in scenarios(env):
        measured = explain(env, sql, params)
        assert measured["execution_ms"] < TIMEOUT_MS, (name, measured)
        assert measured["rows"] >= 1, name  # parcours non vide : mesure représentative
        if command is not None:  # même lecture par le VRAI runner (statement_timeout = 10s)
            started = time.perf_counter()
            outcome = env.runner().run(issued.credential(), command)
            measured["runner_total_ms"] = round((time.perf_counter() - started) * 1000, 2)
            assert outcome.status == AUTHORIZED, (name, outcome)
        report["scenarios"][name] = measured
    reports = REPO_ROOT / "reports"
    if reports.is_dir():
        major = version.split(".")[0]
        pathlib.Path(reports / f"r1_pr8_explain_pg{major}.json").write_text(json.dumps(report, indent=1,
                                                                                       ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))


def test_full_walks_of_the_concentrated_user(loaded):
    """Parcours complets par curseurs (runner) : chaque thèse et chaque
    ticker exactement une fois, chaque page sous le délai."""
    env, issued = loaded
    runner = env.runner()
    seen, command, slowest = [], c.ListThesesPage(50, None, None, None), 0.0
    while True:
        started = time.perf_counter()
        outcome = runner.run(issued.credential(), command)
        slowest = max(slowest, time.perf_counter() - started)
        assert outcome.status == AUTHORIZED
        seen += [t.thesis_id for t in outcome.value.entries]
        if not outcome.value.has_more:
            break
        last = outcome.value.entries[-1]
        command = c.ListThesesPage(50, None, last.thesis_id, last.created_at)
    assert len(seen) == len(set(seen)) == 6_000
    tickers, command = [], c.ListAnalysisOverviewPage(50, None)
    while True:
        outcome = runner.run(issued.credential(), command)
        assert outcome.status == AUTHORIZED
        tickers += [e.ticker for e in outcome.value.entries]
        if not outcome.value.has_more:
            break
        command = c.ListAnalysisOverviewPage(50, outcome.value.entries[-1].ticker)
    assert tickers == sorted(set(tickers))
    assert slowest < TIMEOUT_MS / 1000
