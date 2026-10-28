"""Lectures R1 PR 8 au niveau du runner, sur PostgreSQL réel.

Vrai AuthorizedTransactionRunner, sessions du vrai moteur F2, données
écrites directement (historiques, atypiques, incohérentes). Couvre :

- A : propriété (A / B aux mêmes tickers, thèse étrangère et inexistante
  indiscernables), identité serveur, sessions pending, révoquée, génération
  révoquée, expirée, expiration exactement à T_authorization, clé F2
  historique absente, profil absent, aucune écriture ni confirmation ;
- B : synthèse (session active seule, thèse seule, session active
  prioritaire, sessions actives multiples et sélection déterministe,
  étapes invalide / nulle / volumineuse, abandoned et completed sans thèse
  exclues, dernière thèse, tickers distincts par la casse, caractères
  spéciaux, collation "C" sur une base de collation linguistique, une
  seule instruction SQL métier, aucun thesis_text sélectionné) ;
- C : ordre total des thèses (dates égales, nulles, anciennes, départage
  par id), filtre exact ;
- D : provenance relationnelle (null, vérifiée quel que soit l'état de la
  session, étrangère, autre ticker, casse, session inexistante), sans
  fuite d'identifiant ni réparation ;
- E : dates naïves UTC, TIMESTAMPTZ, microsecondes, ±infinity et hors
  représentation (503, données intactes), texte au-delà de la borne jamais
  transféré, tickers de 512 / 513 octets, multioctets, page entière refusée.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from core.auth_f2.keys import PURPOSES, Keyring
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, SESSION_INVALID, UNAVAILABLE, AuthorizationOutcome
from tests.authz_f2_consultation_support import insert_session, insert_thesis, write_state
from tests.authz_f2_support import BOB, USER_A, USER_B, AuthzEnv
from tests.f2_support import fake_key, prepare_schema, rows, scalar
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

US = timedelta(microseconds=1)
OVERVIEW = c.ListAnalysisOverviewPage(50, None)
THESES = c.ListThesesPage(50, None, None, None)


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = AuthzEnv(pg_url)
    yield environment
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def run(env, issued, command, **kwargs):
    return env.runner(**kwargs).run(issued.credential(), command)


def value(env, issued, command):
    outcome = run(env, issued, command)
    assert outcome.status == AUTHORIZED, outcome
    assert outcome.principal.user_id == USER_A
    return outcome.value


def overview(env, issued, command=OVERVIEW):
    return {e.ticker: e for e in value(env, issued, command).entries}


def theses(env, issued, command=THESES):
    return value(env, issued, command).entries


@pytest.fixture
def statements(env):
    """Instructions SQL réellement exécutées par le moteur du service."""
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    yield seen
    sa.event.remove(env.engine, "before_cursor_execute", record)


def business(statements):
    """Instructions métier, paramètres remis sous la forme :nom des constantes."""
    return [re.sub(r"%\((\w+)\)s", r":\1", s) for s in statements
            if "investment_theses" in s or "analysis_sessions" in s]


# --------------------------------------------------------------------------
# A — Propriété et autorisation
# --------------------------------------------------------------------------

def test_each_user_reads_only_own_analyses_and_theses(env, alice):
    bob = env.confirmed(BOB, USER_B)
    a_thesis = insert_thesis(env, USER_A, "AIR.PA", "thèse de A")
    b_thesis = insert_thesis(env, USER_B, "AIR.PA", "thèse de B")
    b_session = insert_session(env, USER_B, "AIR.PA")
    insert_session(env, USER_B, "TTE.PA")
    a_view = overview(env, alice)
    assert list(a_view) == ["AIR.PA"]
    assert a_view["AIR.PA"].state == c.THESIS_AVAILABLE and a_view["AIR.PA"].latest_thesis_id == a_thesis
    b_outcome = env.runner().run(bob.credential(), OVERVIEW)
    assert [(e.ticker, e.state, e.analysis_session_id) for e in b_outcome.value.entries] == [
        ("AIR.PA", c.IN_PROGRESS, b_session), ("TTE.PA", c.IN_PROGRESS, b_outcome.value.entries[1].analysis_session_id)]
    assert [t.thesis_id for t in theses(env, alice)] == [a_thesis]
    assert value(env, alice, c.ReadThesis(a_thesis)).thesis_text == "thèse de A"
    foreign = run(env, alice, c.ReadThesis(b_thesis))
    missing = run(env, alice, c.ReadThesis(c.MAX_THESIS_ID))
    assert foreign == missing == AuthorizationOutcome(NOT_FOUND)  # ni valeur ni principal transportés


def test_identity_is_the_server_principal_only(env, alice):
    """Deux sessions de deux comptes : chaque justificatif ne lit que son
    propriétaire ; aucune commande ne porte de user_id."""
    bob = env.confirmed(BOB, USER_B)
    insert_thesis(env, USER_A, "A.PA")
    insert_thesis(env, USER_B, "B.PA")
    assert [t.ticker for t in theses(env, alice)] == ["A.PA"]
    assert [t.ticker for t in env.runner().run(bob.credential(), THESES).value.entries] == ["B.PA"]
    for command in (OVERVIEW, THESES, c.ReadThesis(1)):
        assert not hasattr(command, "user_id")


COMMANDS = [OVERVIEW, THESES, c.ReadThesis(1)]


@pytest.mark.parametrize("command", COMMANDS, ids=["overview", "theses", "thesis"])
def test_pending_revoked_and_expired_credentials_are_session_invalid(env, command):
    insert_thesis(env, USER_A, "AIR.PA")
    pending = env.sign_in()
    assert run(env, pending, command) == AuthorizationOutcome(SESSION_INVALID)
    assert env.generation(pending.generation_id)["delivery_status"] == "pending"  # jamais confirmée implicitement
    assert env.hook_calls == []  # refus de phase A : aucune phase B
    revoked = env.confirmed("alice2@example.com", USER_A)
    assert env.service.revoke_session(revoked.session_id, "logout") is True
    generation = env.confirmed("alice3@example.com", USER_A)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE oryx_session_generations SET status = 'revoked', revoked_at = clock_timestamp(), "
                             "revocation_reason = 'security' WHERE id = :g"), {"g": generation.generation_id})
    expired = env.confirmed("alice4@example.com", USER_A)
    env.expire_at(expired.session_id, "clock_timestamp() - interval '1 second'")
    for issued in (revoked, generation, expired):
        assert run(env, issued, command) == AuthorizationOutcome(SESSION_INVALID)


def _scripted_clock(*instants):
    remaining = list(instants)
    return lambda conn: remaining.pop(0)


@pytest.mark.parametrize("command", COMMANDS, ids=["overview", "theses", "thesis"])
def test_expiry_exactly_at_t_authorization_is_refused(env, alice, command, statements):
    insert_thesis(env, USER_A, "AIR.PA")
    expires = scalar(env.observer, "SELECT absolute_expires_at FROM oryx_sessions WHERE id = :s", s=alice.session_id)
    statements.clear()
    refused = run(env, alice, command, clock=_scripted_clock(expires - timedelta(seconds=1), expires))
    assert refused == AuthorizationOutcome(SESSION_INVALID)
    assert business(statements) == []  # décision à T_authorization AVANT toute lecture métier
    accepted = run(env, alice, command, clock=_scripted_clock(expires - timedelta(seconds=1), expires - US))
    assert accepted.status in (AUTHORIZED, NOT_FOUND)


def test_unknown_historical_f2_key_is_refused_without_substitution(env, alice):
    keys = {p: {"t1": fake_key(p, "t1")} for p in PURPOSES}
    keys["active"] = {"t9": fake_key("active", "t1")}
    substituting = Keyring(keys, {**{p: "t1" for p in PURPOSES}, "active": "t9"})
    for command in COMMANDS:
        assert run(env, alice, command, keyring=substituting) == AuthorizationOutcome(SESSION_INVALID)


def test_missing_profile_is_unavailable(env, alice):
    insert_thesis(env, USER_A, "AIR.PA")
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": USER_A})
    for command in COMMANDS:
        assert run(env, alice, command) == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("users") == 0  # jamais recréé


def test_database_outage_is_unavailable(env, alice):
    down = sa.create_engine("postgresql+psycopg2://postgres@127.0.0.1:1/oryx_down_test", poolclass=sa.pool.NullPool)
    for command in COMMANDS:
        assert run(env, alice, command, engine=down) == AuthorizationOutcome(UNAVAILABLE)


def test_reads_write_nothing_and_confirm_nothing(env):
    pending = env.sign_in()
    alice = env.confirmed("alice2@example.com", USER_A)
    session = insert_session(env, USER_A, "AIR.PA", current_step="inconnue")
    insert_session(env, USER_A, "AIR.PA", started_at="2026-01-01 00:00:00+00")
    insert_thesis(env, USER_A, "AIR.PA", session_id=session)
    insert_thesis(env, USER_A, "MC.PA", created_at=None)
    before = write_state(env)
    generation = dict(env.generation(pending.generation_id))
    for command in COMMANDS + [c.ReadThesis(c.MAX_THESIS_ID), c.ListThesesPage(1, "AIR.PA", None, None)]:
        run(env, alice, command)
        run(env, pending, command)
    assert write_state(env) == before  # aucune réparation, aucune livraison confirmée
    assert dict(env.generation(pending.generation_id)) == generation


# --------------------------------------------------------------------------
# B — Synthèse
# --------------------------------------------------------------------------

def test_active_session_alone(env, alice):
    session = insert_session(env, USER_A, "AIR.PA", current_step="moat", started_at="2026-10-01 10:00:00+00",
                             updated_at="2026-10-02 11:00:00.000001+00")
    (entry,) = overview(env, alice).values()
    assert entry == c.AnalysisOverviewEntry("AIR.PA", c.IN_PROGRESS, session, "moat",
                                            datetime(2026, 10, 2, 11, 0, 0, 1, tzinfo=timezone.utc), None, ())


def test_historical_thesis_alone(env, alice):
    thesis = insert_thesis(env, USER_A, "AIR.PA", created_at="2026-10-02 12:00:00.000007")
    (entry,) = overview(env, alice).values()
    assert entry == c.AnalysisOverviewEntry("AIR.PA", c.THESIS_AVAILABLE, None, None,
                                            datetime(2026, 10, 2, 12, 0, 0, 7), thesis, ())


def test_active_session_wins_over_older_theses(env, alice):
    insert_thesis(env, USER_A, "AIR.PA")
    insert_thesis(env, USER_A, "AIR.PA")
    session = insert_session(env, USER_A, "AIR.PA", current_step="chiffres")
    entry = overview(env, alice)["AIR.PA"]
    assert (entry.state, entry.analysis_session_id, entry.latest_thesis_id, entry.current_step) == (
        c.IN_PROGRESS, session, None, "chiffres")


def test_several_active_sessions_select_the_most_recent_deterministically(env, alice):
    low, high = sorted((uuid.uuid4(), uuid.uuid4()), key=lambda u: u.int)
    insert_session(env, USER_A, "A", current_step="business", started_at="2026-01-01 00:00:00+00")
    newest = insert_session(env, USER_A, "A", current_step="moat", started_at="2026-02-01 00:00:00+00")
    # Même started_at : updated_at départage.
    insert_session(env, USER_A, "B", started_at="2026-01-01 00:00:00+00", updated_at="2026-01-02 00:00:00+00")
    fresher = insert_session(env, USER_A, "B", started_at="2026-01-01 00:00:00+00",
                             updated_at="2026-01-03 00:00:00+00", current_step="risques")
    # Mêmes dates : id DESC départage.
    insert_session(env, USER_A, "C", started_at="2026-01-01 00:00:00+00", session_id=low)
    insert_session(env, USER_A, "C", started_at="2026-01-01 00:00:00+00", session_id=high, current_step="moat")
    insert_session(env, USER_A, "C", status="completed", started_at="2027-01-01 00:00:00+00")  # non active
    before = write_state(env)
    view = overview(env, alice)
    assert view["A"].analysis_session_id == newest and view["A"].current_step == "moat"
    assert view["B"].analysis_session_id == fresher and view["B"].current_step == "risques"
    assert view["C"].analysis_session_id == high
    for ticker in "ABC":
        assert view[ticker].diagnostics == (c.MULTIPLE_ACTIVE_SESSIONS,)
    assert write_state(env) == before  # les autres sessions ne sont ni réparées ni terminées
    assert scalar(env.observer, "SELECT count(*) FROM analysis_sessions WHERE status = 'in_progress'") == 6


@pytest.mark.parametrize("step", [None, "", "inconnue", "BUSINESS", "business ", "x" * 5000])
def test_invalid_or_null_step_is_null_with_a_diagnostic(env, alice, step):
    session = insert_session(env, USER_A, "AIR.PA", current_step=step)
    entry = overview(env, alice)["AIR.PA"]
    assert entry.analysis_session_id == session and entry.current_step is None
    assert entry.diagnostics == (c.INVALID_CURRENT_STEP,)
    insert_session(env, USER_A, "AIR.PA", current_step="moat", started_at="2020-01-01 00:00:00+00")
    entry = overview(env, alice)["AIR.PA"]
    assert entry.analysis_session_id == session  # aucune étape de substitution
    assert entry.diagnostics == (c.MULTIPLE_ACTIVE_SESSIONS, c.INVALID_CURRENT_STEP)


@pytest.mark.parametrize("step", c.ANALYSIS_STEPS)
def test_every_known_step_is_rendered(env, alice, step):
    insert_session(env, USER_A, "AIR.PA", current_step=step)
    assert overview(env, alice)["AIR.PA"].current_step == step


def test_abandoned_or_completed_sessions_without_thesis_are_excluded(env, alice):
    insert_session(env, USER_A, "ABANDONED", status="abandoned")
    insert_session(env, USER_A, "COMPLETED", status="completed", current_step="swot_final")
    done = insert_session(env, USER_A, "DONE", status="completed", current_step="swot_final")
    thesis = insert_thesis(env, USER_A, "DONE", session_id=done)
    view = overview(env, alice)
    assert list(view) == ["DONE"]
    # Jamais « completed » inféré : état de présentation, sans étape ni session.
    assert (view["DONE"].state, view["DONE"].analysis_session_id, view["DONE"].current_step,
            view["DONE"].latest_thesis_id) == (c.THESIS_AVAILABLE, None, None, thesis)


def test_latest_thesis_is_created_at_desc_nulls_last_then_id_desc(env, alice):
    old = insert_thesis(env, USER_A, "A", created_at="2020-01-01 00:00:00")
    insert_thesis(env, USER_A, "A", created_at=None)  # id plus grand, date nulle : jamais la dernière
    assert overview(env, alice)["A"].latest_thesis_id == old
    first = insert_thesis(env, USER_A, "B", created_at="2026-01-01 00:00:00")
    second = insert_thesis(env, USER_A, "B", created_at="2026-01-01 00:00:00")
    assert overview(env, alice)["B"].latest_thesis_id == max(first, second)
    insert_thesis(env, USER_A, "C", created_at=None)
    last_null = insert_thesis(env, USER_A, "C", created_at=None)
    entry = overview(env, alice)["C"]
    assert entry.latest_thesis_id == last_null and entry.updated_at is None


# Ordre "C" (points de code) ≠ ordre linguistique de la base de test.
TICKERS = ["", " A", "A&B=C", "AIR.PA", "Air.pa", "B", "Z", "_x", "a", "air.pa", "z", "É", "é", "😀", "a\u0301",
           "\u00e1"]  # « á » décomposé et précomposé : deux tickers, aucune normalisation Unicode


def test_tickers_are_exact_and_ordered_by_collation_c(env, alice):
    linguistic = scalar(env.observer, "SELECT 'a' < 'B'")
    for ticker in TICKERS:
        insert_thesis(env, USER_A, ticker)
    insert_session(env, USER_A, "air.pa")
    entries = value(env, alice, OVERVIEW).entries
    assert [e.ticker for e in entries] == sorted(TICKERS)  # aucune fusion par casse, aucune normalisation
    assert entries[[e.ticker for e in entries].index("air.pa")].state == c.IN_PROGRESS
    assert entries[[e.ticker for e in entries].index("AIR.PA")].state == c.THESIS_AVAILABLE
    if linguistic:  # la base de test n'est pas en "C" : la collation explicite est prouvée
        assert scalar(env.observer, "SELECT 'Z' < '_x'") is False
    by_bytes = sorted(TICKERS, key=lambda t: t.encode("utf-8"))
    assert by_bytes == sorted(TICKERS)


def test_overview_keyset_uses_collation_c(env, alice):
    for ticker in TICKERS:
        insert_thesis(env, USER_A, ticker)
    seen, after = [], None
    while True:
        page = value(env, alice, c.ListAnalysisOverviewPage(4, after))
        seen += [e.ticker for e in page.entries]
        if not page.has_more:
            break
        after = page.entries[-1].ticker
    assert seen == sorted(TICKERS)
    assert [e.ticker for e in value(env, alice, c.ListAnalysisOverviewPage(50, "Z")).entries] == [
        t for t in sorted(TICKERS) if t > "Z"]


def test_overview_is_one_business_statement_without_thesis_text(env, alice, statements):
    insert_session(env, USER_A, "A")
    insert_thesis(env, USER_A, "B", text="secret" * 1000)
    statements.clear()
    for command in (OVERVIEW, c.ListAnalysisOverviewPage(1, "A")):
        statements.clear()
        assert value(env, alice, command) is not None
        (sql,) = business(statements)
        assert sql in _adapters._ANALYSIS_OVERVIEW_PAGE.values()
        assert "thesis_text" not in sql
    for command in (THESES, c.ListThesesPage(1, "B", 5, None), c.ListThesesPage(1, None, 5, datetime(2026, 1, 1))):
        statements.clear()
        value(env, alice, command)
        (sql,) = business(statements)
        assert sql in _adapters._THESES_PAGE.values() and "thesis_text" not in sql
    statements.clear()
    run(env, alice, c.ReadThesis(1))
    assert business(statements) == [_adapters._READ_THESIS]


# --------------------------------------------------------------------------
# C — Ordre et filtre des thèses
# --------------------------------------------------------------------------

def test_theses_total_order_with_equal_null_and_ancient_dates(env, alice):
    ids = {}
    for name, created in (("same1", "2026-01-01 00:00:00"), ("null1", None), ("old", "1900-01-01 00:00:00"),
                          ("same2", "2026-01-01 00:00:00"), ("first", "0001-01-01 00:00:00"),
                          ("null2", None), ("new", "2026-10-28 12:00:00.000001"), ("same3", "2026-01-01 00:00:00")):
        ids[name] = insert_thesis(env, USER_A, "AIR.PA", created_at=created)
    order = [ids[n] for n in ("new", "same3", "same2", "same1", "old", "first", "null2", "null1")]
    assert [t.thesis_id for t in theses(env, alice)] == order
    page = theses(env, alice)
    assert page[0].created_at == datetime(2026, 10, 28, 12, 0, 0, 1)
    assert page[5].created_at == datetime(1, 1, 1)
    assert [t.created_at for t in page[-2:]] == [None, None]


def test_theses_filter_is_exact_under_collation_c(env, alice):
    exact = insert_thesis(env, USER_A, "AIR.PA")
    insert_thesis(env, USER_A, "air.pa")
    insert_thesis(env, USER_A, "AIR.PA ")
    special = insert_thesis(env, USER_A, "A B+C&D")
    assert [t.thesis_id for t in theses(env, alice, c.ListThesesPage(50, "AIR.PA", None, None))] == [exact]
    assert [t.thesis_id for t in theses(env, alice, c.ListThesesPage(50, "A B+C&D", None, None))] == [special]
    assert theses(env, alice, c.ListThesesPage(50, "MC.PA", None, None)) == ()
    # Ticker historique non conforme au filtre (blanc de bord) : liste générale.
    assert "AIR.PA " in [t.ticker for t in theses(env, alice)]


def test_existing_pr5_thesis_commands_are_unchanged(env, alice):
    first = insert_thesis(env, USER_A, "AIR.PA", text="a")
    insert_thesis(env, USER_A, "MC.PA", text="b")
    listed = value(env, alice, c.ListTheses())
    assert [type(t) for t in listed] == [c.ThesisView, c.ThesisView] and listed[0].thesis_text == "a"
    assert [t.thesis_id for t in value(env, alice, c.ListThesesForTicker("AIR.PA"))] == [first]


# --------------------------------------------------------------------------
# D — Provenance relationnelle
# --------------------------------------------------------------------------

def _detail(env, issued, thesis_id):
    return value(env, issued, c.ReadThesis(thesis_id))


def _listed(env, issued):
    return {t.thesis_id: t.provenance_status for t in theses(env, issued)}


@pytest.mark.parametrize("status", ["in_progress", "completed", "abandoned"])
def test_relational_provenance_is_verified_whatever_the_session_status(env, alice, status):
    session = insert_session(env, USER_A, "AIR.PA", status=status)
    thesis = insert_thesis(env, USER_A, "AIR.PA", session_id=session)
    assert _detail(env, alice, thesis).analysis_session_id == session
    assert _detail(env, alice, thesis).provenance_status == c.VERIFIED
    assert _listed(env, alice) == {thesis: c.VERIFIED}


def test_incoherent_provenances_are_unverified_without_leak_or_repair(env, alice):
    env.confirmed(BOB, USER_B)
    foreign = insert_session(env, USER_B, "AIR.PA")
    other_ticker = insert_session(env, USER_A, "MC.PA")
    other_case = insert_session(env, USER_A, "air.pa")
    ids = {
        "null": insert_thesis(env, USER_A, "AIR.PA", session_id=None),
        "foreign": insert_thesis(env, USER_A, "AIR.PA", session_id=foreign),
        "other_ticker": insert_thesis(env, USER_A, "AIR.PA", session_id=other_ticker),
        "other_case": insert_thesis(env, USER_A, "AIR.PA", session_id=other_case),
    }
    missing = uuid.uuid4()
    with env.observer.begin() as conn:  # session inexistante (fixture historique, FK contournée)
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        ids["missing"] = conn.execute(sa.text(
            "INSERT INTO investment_theses (user_id, ticker, thesis_text, analysis_session_id) "
            "VALUES (:u, 'AIR.PA', 'x', :s) RETURNING id"), {"u": USER_A, "s": missing}).scalar_one()
    before = write_state(env)
    listed = _listed(env, alice)
    for name, thesis in ids.items():
        detail = _detail(env, alice, thesis)
        assert detail.analysis_session_id is None and detail.provenance_status == c.UNVERIFIED, name
        assert listed[thesis] == c.UNVERIFIED, name
        assert repr(detail).count("UUID") == 0, name  # identifiant incohérent jamais transporté
    assert write_state(env) == before  # aucune réparation du rattachement
    stored = {r["id"]: r["analysis_session_id"] for r in rows(env.observer, "SELECT id, analysis_session_id FROM "
                                                                            "investment_theses")}
    assert stored[ids["foreign"]] == foreign and stored[ids["missing"]] == missing


def test_verified_session_of_another_user_is_never_exposed(env, alice):
    """Même si une session ÉTRANGÈRE porte le même ticker et l'identifiant
    stocké : la jointure exige le même user_id."""
    env.confirmed(BOB, USER_B)
    foreign = insert_session(env, USER_B, "AIR.PA")
    thesis = insert_thesis(env, USER_A, "AIR.PA", session_id=foreign)
    assert _detail(env, alice, thesis).analysis_session_id is None
    bob_thesis = insert_thesis(env, USER_B, "AIR.PA", session_id=foreign)
    assert run(env, alice, c.ReadThesis(bob_thesis)) == AuthorizationOutcome(NOT_FOUND)


# --------------------------------------------------------------------------
# E — Dates et données pathologiques
# --------------------------------------------------------------------------

def test_dates_are_returned_exactly(env, alice):
    insert_session(env, USER_A, "S", started_at="2026-10-28 12:00:00+05:30",
                   updated_at="2026-10-28 12:00:00.000001+05:30")
    thesis = insert_thesis(env, USER_A, "T", created_at="2026-10-28 12:00:00.000001")
    view = overview(env, alice)
    assert view["S"].updated_at.astimezone(timezone.utc) == datetime(2026, 10, 28, 6, 30, 0, 1, tzinfo=timezone.utc)
    assert view["T"].updated_at == datetime(2026, 10, 28, 12, 0, 0, 1)  # naïf, aucune conversion
    assert _detail(env, alice, thesis).created_at == datetime(2026, 10, 28, 12, 0, 0, 1)


@pytest.mark.parametrize("created", ["infinity", "-infinity", "10000-01-01 00:00:00", "0044-03-15 00:00:00 BC"])
def test_unrepresentable_thesis_dates_are_unavailable_and_untouched(env, alice, created):
    thesis = insert_thesis(env, USER_A, "AIR.PA", created_at=created)
    before = write_state(env)
    for command in (OVERVIEW, THESES, c.ReadThesis(thesis)):
        assert run(env, alice, command) == AuthorizationOutcome(UNAVAILABLE)
    insert_session(env, USER_A, "AIR.PA")  # session active : la date de la thèse n'est plus restituée
    assert overview(env, alice)["AIR.PA"].state == c.IN_PROGRESS
    assert write_state(env)["investment_theses"] == before["investment_theses"]


@pytest.mark.parametrize("updated", ["infinity", "-infinity"])
def test_unrepresentable_session_dates_are_unavailable(env, alice, updated):
    insert_session(env, USER_A, "AIR.PA", started_at="2026-01-01 00:00:00+00", updated_at=updated)
    assert run(env, alice, OVERVIEW) == AuthorizationOutcome(UNAVAILABLE)


def test_text_beyond_the_raw_bound_is_never_transferred(env, alice):
    exact = insert_thesis(env, USER_A, "A", text="x" * c.THESIS_TEXT_MAX_BYTES)
    over = insert_thesis(env, USER_A, "A", text="é" * (c.THESIS_TEXT_MAX_BYTES // 2) + "x")  # 131073 octets
    assert len(_detail(env, alice, exact).thesis_text) == c.THESIS_TEXT_MAX_BYTES  # intégral
    assert run(env, alice, c.ReadThesis(over)) == AuthorizationOutcome(UNAVAILABLE)
    with env.observer.connect() as conn:  # projection : valeur hors borne distinguée, jamais transférée
        row = conn.execute(sa.text(_adapters._READ_THESIS), {"id": over, "u": USER_A}).mappings().one()
    assert row["thesis_text"] is None and row["thesis_text_bytes"] == c.THESIS_TEXT_MAX_BYTES + 1
    assert len(scalar(env.observer, "SELECT thesis_text FROM investment_theses WHERE id = :i", i=over)) == \
        c.THESIS_TEXT_MAX_BYTES // 2 + 1  # aucune modification


@pytest.mark.parametrize("ticker,ok", [("A" * 512, True), ("A" * 513, False), ("é" * 256, True), ("é" * 256 + "a", False),
                                       ("€" * 170 + "ab", True), ("😀" * 128, True), ("😀" * 128 + "a", False)])
def test_ticker_byte_bound_512(env, alice, ticker, ok):
    thesis = insert_thesis(env, USER_A, ticker)
    for command in (OVERVIEW, THESES, c.ReadThesis(thesis)):
        outcome = run(env, alice, command)
        assert outcome.status == (AUTHORIZED if ok else UNAVAILABLE), command
    if ok:
        assert _detail(env, alice, thesis).ticker == ticker  # jamais tronqué


def test_one_unrestorable_ticker_fails_its_whole_page_only(env, alice):
    insert_thesis(env, USER_A, "A", created_at="2026-01-03 00:00:00")
    insert_thesis(env, USER_A, "X" * 513, created_at="2026-01-02 00:00:00")
    last = insert_thesis(env, USER_A, "B", created_at="2026-01-01 00:00:00")
    assert run(env, alice, THESES) == AuthorizationOutcome(UNAVAILABLE)  # aucune entrée omise
    assert [t.ticker for t in theses(env, alice, c.ListThesesPage(1, None, None, None))] == ["A"]
    assert [t.thesis_id for t in theses(env, alice, c.ListThesesPage(50, "B", None, None))] == [last]
    assert run(env, alice, OVERVIEW) == AuthorizationOutcome(UNAVAILABLE)
    assert [e.ticker for e in value(env, alice, c.ListAnalysisOverviewPage(2, None)).entries] == ["A", "B"]


def test_atypical_historical_values_stay_readable(env, alice):
    for ticker in ("", " lead", "trail ", "lower.pa", "A\tB", "日本"):
        insert_thesis(env, USER_A, ticker, text="")
    assert sorted(t.ticker for t in theses(env, alice)) == sorted(("", " lead", "trail ", "lower.pa", "A\tB", "日本"))
    assert all(t.provenance_status == c.UNVERIFIED for t in theses(env, alice))


def test_sql_bounds_match_the_contract():
    assert "octet_length(ticker) <= 512" in _adapters._READ_THESIS and c.TICKER_MAX_BYTES == 512
    assert "octet_length(thesis_text) <= 131072" in _adapters._READ_THESIS and c.THESIS_TEXT_MAX_BYTES == 131072
    for sql in list(_adapters._THESES_PAGE.values()) + list(_adapters._ANALYSIS_OVERVIEW_PAGE.values()):
        assert "<= 512 THEN" in sql and "thesis_text" not in sql


def test_an_unrepresentable_extra_row_never_fails_the_previous_page(env, alice):
    """La ligne supplémentaire (limit + 1) ne dit que l'existence d'une
    page suivante : une date hors représentation n'y est jamais lue."""
    first = insert_thesis(env, USER_A, "A", created_at="2026-01-02 00:00:00")
    bad = insert_thesis(env, USER_A, "B", created_at="0044-03-15 00:00:00 BC")
    insert_thesis(env, USER_A, "C" * 600, created_at="0001-01-01 00:00:00")  # plus loin encore
    page = value(env, alice, c.ListThesesPage(1, None, None, None))
    assert [t.thesis_id for t in page.entries] == [first] and page.has_more is True
    assert run(env, alice, c.ListThesesPage(1, None, first, datetime(2026, 1, 2))) == AuthorizationOutcome(UNAVAILABLE)
    assert run(env, alice, c.ReadThesis(bad)) == AuthorizationOutcome(UNAVAILABLE)
    insert_session(env, USER_A, "Z", started_at="2026-01-01 00:00:00+00", updated_at="infinity")
    page = value(env, alice, c.ListAnalysisOverviewPage(1, None))
    assert [e.ticker for e in page.entries] == ["A"] and page.has_more is True
    assert run(env, alice, c.ListAnalysisOverviewPage(1, "A")) == AuthorizationOutcome(UNAVAILABLE)  # B
    # « Z » (date de session infinie) n'est que la ligne supplémentaire de la page « C... » :
    # cette page échoue pour son PROPRE ticker de 600 octets, jamais pour « Z ».
    assert run(env, alice, c.ListAnalysisOverviewPage(1, "B")) == AuthorizationOutcome(UNAVAILABLE)
    assert run(env, alice, c.ListAnalysisOverviewPage(1, "C" * 600)) == AuthorizationOutcome(UNAVAILABLE)  # Z
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE investment_theses SET ticker = 'C' WHERE ticker = :t"), {"t": "C" * 600})
    page = value(env, alice, c.ListAnalysisOverviewPage(1, "B"))
    assert [e.ticker for e in page.entries] == ["C"] and page.has_more is True


def test_unrepresentable_dates_keep_their_contractual_position(env, alice):
    """L'ordre porte sur la colonne réelle (created_at DESC NULLS LAST), jamais
    sur la projection bornée : « infinity » vient en tête, « -infinity » après
    toute date réelle, avant les dates nulles."""
    insert_thesis(env, USER_A, "A", created_at="2026-01-01 00:00:00")
    insert_thesis(env, USER_A, "A", created_at=None)
    insert_thesis(env, USER_A, "A", created_at="infinity")
    assert run(env, alice, c.ListThesesPage(1, None, None, None)) == AuthorizationOutcome(UNAVAILABLE)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE investment_theses SET created_at = '-infinity' WHERE created_at = 'infinity'"))
    first = value(env, alice, c.ListThesesPage(1, None, None, None)).entries[0]
    assert first.created_at == datetime(2026, 1, 1)
    after = c.ListThesesPage(1, None, first.thesis_id, first.created_at)
    assert run(env, alice, after) == AuthorizationOutcome(UNAVAILABLE)  # -infinity, avant la date nulle


def test_session_instants_do_not_depend_on_the_connection_time_zone(env, alice):
    insert_session(env, USER_A, "EARLY", started_at="0001-01-01 00:30:00+00")
    insert_session(env, USER_A, "LATE", started_at="9999-12-31 23:30:00+00")
    for zone in ("UTC", "America/New_York", "Pacific/Kiritimati", "Asia/Kathmandu"):
        engine = sa.create_engine(env.pg_url, poolclass=sa.pool.NullPool,
                                  connect_args={"options": f"-c TimeZone={zone}"})
        try:
            outcome = run(env, alice, OVERVIEW, engine=engine)
        finally:
            engine.dispose()
        assert outcome.status == AUTHORIZED, zone
        entries = {e.ticker: e.updated_at for e in outcome.value.entries}
        assert entries == {"EARLY": datetime(1, 1, 1, 0, 30, tzinfo=timezone.utc),
                           "LATE": datetime(9999, 12, 31, 23, 30, tzinfo=timezone.utc)}, zone
