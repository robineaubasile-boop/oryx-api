"""V0-T08 Lot 2B-2 bloc C — chaîne RÉELLE F2 -> bloc B -> bloc C et
frontière privée de transaction (core/_activity_event_finalization.py),
contre un VRAI PostgreSQL (16 et 18 en CI).

1. dormance : aucun appelant de production, imports exacts, api:app ne
   charge ni la frontière ni le service, aucune lecture F2 ;
2. chaîne réelle (vrai runner F2, vraie frontière B, registre et service de
   production) : premier ACK Décrypter, CREATE, UPDATE effectif, DELETE
   Portfolio -> certified -> recorded ; continuité exacte des identités
   (bloc 0 : reçu / attestation, A : registre, B : origine émise, C :
   événement et attribution) ; rejeu, no-op, refus F2 et COMMIT F2 incertain
   : aucun événement ;
3. frontière : COMMIT confirmé, COMMIT incertain réellement effectué
   (confirmé par relecture), non effectué (aucun succès annoncé, retry
   explicite propre), impossible à confirmer (unknown), panne avant COMMIT,
   résolution explicite d'une preuve pending expirée, journaux sans
   identifiant ; décision devenue périmée pendant une attente sur l'index
   unique (horloge réelle, garde-fous de la frontière) : aucun événement,
   aucun changement du résultat F2, aucun rejeu, aucune certification.
"""
import ast
import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from core import _activity_event_finalization as boundary
from core import _activity_proof_bridge as bridge
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, REVISION_CONFLICT, UNAVAILABLE, AuthorizationOutcome
from core.product_analytics import common
from core.product_analytics import finalization as fz
from core.product_analytics import proofs
from core.product_analytics import sessions as ps
from tests.activity_proof_support import (
    ACK,
    ATTRIBUTIONS,
    CREATE,
    DELETE,
    EVENTS,
    REGISTRY,
    UPDATE,
    Background,
    Clock,
    Holder,
    analytics_engine,
    open_wave,
    opened,
    register,
    rows,
    utc,
)
from tests.authz_f2_support import BOB, USER_A, USER_B, AuthzEnv, create_command, delete_command, replace_command
from tests.f2_support import prepare_schema
from tests.test_activity_event_finalization import (
    _active_campaign_and_window,
    _outside_event,
    _sleep_until,
    _unique_index_waits,
    _wait,
)
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

BOUNDARY = REPO_ROOT / "core" / "_activity_event_finalization.py"
SERVICE = REPO_ROOT / "core" / "product_analytics" / "finalization.py"
# V0-T08 Lot 2C PR 1 (nominatif) : seul module, hors du service et de la
# frontière, qui les nomme (réutilisation de la transaction et de l'issue
# établie du bloc C) ; lui-même sans appelant (tests/test_activity_proof_lifecycle.py).
LIFECYCLE_BOUNDARY = REPO_ROOT / "core" / "_activity_proof_lifecycle.py"
# V0-T08 Lot 2C PR 2 (nominatif) : la frontière du cycle de vie des événements
# réutilise la seule transaction analytics du bloc C (_transaction), jamais ses
# points d'entrée ; elle-même sans appelant (tests/test_activity_event_lifecycle.py).
EVENT_LIFECYCLE_BOUNDARY = REPO_ROOT / "core" / "_activity_event_lifecycle.py"
# V0-T08 Lot 2C PR 3 (nominatif) : la frontière du cycle de vie des participations
# réutilise, elle aussi, la seule transaction analytics du bloc C ; elle-même sans
# appelant (tests/test_activity_participation_lifecycle.py).
PARTICIPATION_LIFECYCLE_BOUNDARY = REPO_ROOT / "core" / "_activity_participation_lifecycle.py"
# V0-T08 Lot 2C PR 4 (nominatif) : la frontière du cycle de vie des campagnes
# réutilise, elle aussi, la seule transaction analytics du bloc C ; elle-même sans
# appelant (tests/test_activity_campaign_lifecycle.py).
CAMPAIGN_LIFECYCLE_BOUNDARY = REPO_ROOT / "core" / "_activity_campaign_lifecycle.py"
IDLE = lambda point: None  # noqa: E731
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
GRACE = timedelta(minutes=30)


# --------------------------------------------------------------------------
# 1. Dormance
# --------------------------------------------------------------------------

def test_the_boundary_and_the_service_are_dormant_and_import_exactly_their_side():
    tree = ast.parse(BOUNDARY.read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported == {"logging", "uuid", "dataclasses", "sqlalchemy", "sqlalchemy.orm", "core.product_analytics"}
    assert [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "core.product_analytics"
            for a in n.names] == ["finalization"]
    text = BOUNDARY.read_text(encoding="utf-8")
    for forbidden in ("authz_f2", "auth_f2", "_activity_proof_bridge", "oryx_sessions", "_run_attested", "os.environ",
                      "getenv", "Thread", "asyncio", "schedule", "socket", "requests", "httpx", "APIRouter",
                      "FastAPI", "import_module", "record_event", "INSERT", "UPDATE ", "DELETE"):
        assert forbidden not in text, forbidden
    # Aucun appelant : aucun module hors des tests et de la documentation ne nomme la frontière ni le service.
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if path in (EVENT_LIFECYCLE_BOUNDARY, PARTICIPATION_LIFECYCLE_BOUNDARY, CAMPAIGN_LIFECYCLE_BOUNDARY):
            text = path.read_text(encoding="utf-8")
            assert "from core import _activity_event_finalization as finalization_boundary" in text
            assert {a.attr for a in ast.walk(ast.parse(text)) if isinstance(a, ast.Attribute)
                    and isinstance(a.value, ast.Name) and a.value.id == "finalization_boundary"} == {
                "_transaction", "_COMMITTED", "_FAILED"}  # ni _run, ni les points d'entrée historiques
            continue
        if (not path.is_file() or path in (BOUNDARY, SERVICE, LIFECYCLE_BOUNDARY) or ".git" in path.parts
                or "__pycache__" in path.parts
                or "node_modules" in path.parts or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh",
                                                                        ".yml", ".yaml", ".toml", ".ini", ".cfg")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs"):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in source for marker in ("_activity_event_finalization", "finalize_in_transaction",
                                               "expire_pending_in_transaction", "finalize_certified_proof",
                                               "resolve_expired_pending_proof", "product_analytics.finalization",
                                               "import finalization")):
            offenders.append(str(relative))
    assert offenders == []


PROBE = r'''
import json, socket, sys
socket.socket.connect = lambda self, address: (_ for _ in ()).throw(OSError("réseau interdit"))
import api
loaded = sorted(n for n in sys.modules if n in ("core._activity_event_finalization", "core._activity_proof_bridge")
                or n.startswith("core.product_analytics"))
print(json.dumps({"loaded": loaded}))
'''


def test_the_real_api_never_loads_the_finalization_whatever_the_environment():
    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1", "ORYX_ACTIVITY_PROOFS": "1",
                             "ORYX_EVENT_FINALIZATION": "1", "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        assert json.loads(out.stdout.strip().splitlines()[-1])["loaded"] == []


def test_the_boundary_receives_only_a_proof_identifier():
    for function in (boundary.finalize_in_transaction, boundary.expire_pending_in_transaction):
        assert list(__import__("inspect").signature(function).parameters) == ["engine", "proof_id"]
        for bad in (str(uuid.uuid4()), None):
            with pytest.raises(TypeError):
                function(None, proof_id=bad)  # avant toute connexion
        with pytest.raises(TypeError):
            function(None, proof_id=uuid.uuid4(), user_id=USER_A)
    assert boundary.REPORT_STATES == fz.FINALIZATION_STATES + (boundary.UNKNOWN,)


# --------------------------------------------------------------------------
# PostgreSQL réel : préparation
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


def _deadlocks(engine) -> int:
    return rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]


def _orphans(engine) -> list:
    return rows(engine, f"""
        SELECT e.id FROM {EVENTS} e LEFT JOIN {ATTRIBUTIONS} a ON a.event_id = e.id WHERE a.event_id IS NULL
        UNION ALL
        SELECT p.id FROM {REGISTRY} p LEFT JOIN {ATTRIBUTIONS} a ON a.event_id = p.recorded_event_id
         WHERE p.status = 'recorded' AND a.event_id IS NULL""")


@pytest.fixture
def analytics(pg_url, schema):  # noqa: F811
    engine = analytics_engine(pg_url)
    before = _deadlocks(engine)
    yield engine
    assert _orphans(engine) == [], "événement orphelin ou preuve recorded sans attribution"
    assert _deadlocks(engine) == before, "interblocage PostgreSQL détecté"
    engine.dispose()


@pytest.fixture
def env(pg_url, schema, analytics, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="oryx.activity_finalization")
    environment = AuthzEnv(pg_url)  # vide F2 et users (CASCADE : analytics des tests précédents)
    yield environment
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def run(env, analytics, issued, command, campaign_id, *, hook=None, runner=None):
    runner = runner or env.runner(hook=hook or IDLE)
    return bridge.run_with_proof_certification(runner, issued.credential(), command, analytics_engine=analytics,
                                               campaign_id=campaign_id)


def events(analytics, user_id=USER_A):
    return rows(analytics, f"SELECT *, xmin::text AS xmin FROM {EVENTS} WHERE user_id = :u ORDER BY recorded_at",
                u=user_id)


def attributions(analytics):
    return rows(analytics, f"SELECT *, xmin::text AS xmin FROM {ATTRIBUTIONS} ORDER BY event_id")


def proof_of(analytics, operation_id):
    found = rows(analytics, f"SELECT *, xmin::text AS xmin FROM {REGISTRY} WHERE source_operation_id = :o",
                 o=operation_id)
    return found[0] if found else None


def no_identifier_logged(caplog):
    for record in caplog.records:
        if record.name == "oryx.activity_finalization":
            message = record.getMessage()
            assert USER_A not in message and USER_B not in message and not UUID_PATTERN.search(message), message
            assert re.fullmatch(r"activity_finalization state=[a-z]+ reason=([a-z_]+|None)", message), message


def continuity(analytics, issued, kind, operation_id, report, *, receipt=None):
    """Identités EXACTEMENT continues : opération F2 (bloc 0) -> preuve
    (bloc A) -> origine émise pour la session F2 (bloc B) -> événement et
    attribution (bloc C)."""
    proof = proof_of(analytics, operation_id)
    assert (proof["status"], proof["source_operation_kind"], proof["user_id"]) == ("recorded", kind, USER_A)
    assert report == boundary.FinalizationReport(fz.RECORDED, None, proof["recorded_event_id"])
    [event] = [e for e in events(analytics) if e["id"] == report.event_id]
    [attribution] = [a for a in attributions(analytics) if a["event_id"] == event["id"]]
    [interaction] = rows(analytics, "SELECT * FROM product_activity_session_interactions WHERE interaction_key = :k",
                         k=proof["interaction_key"])
    assert (interaction["session_id"], interaction["interaction_kind"], interaction["environment_digest"]) == (
        proof["visit_id"], "activity", ps.environment_digest(issued.session_id))  # session F2 qui a exécuté
    assert (event["user_id"], event["source_operation_kind"], event["source_operation_id"], event["visit_id"],
            event["idempotency_key"], event["source"], event["outcome"], event["executed_route"]) == (
        USER_A, kind, operation_id, proof["visit_id"], proof["id"], "server_business", "succeeded", "f2")
    assert {k: attribution[k] for k in fz.IDENTITY_FIELDS} == {k: proof[k] for k in fz.IDENTITY_FIELDS}
    assert attribution["certified_at"] == proof["certified_at"] == event["resolved_at"]
    assert event["occurred_at"] == interaction["recorded_at"]
    assert (utc(interaction["recorded_at"]) <= utc(proof["created_at"]) <= utc(proof["certified_at"])
            <= utc(proof["resolved_at"]) == utc(event["recorded_at"]))
    if receipt is not None:  # bloc 0 : reçu F2 committé de CETTE opération, antérieur à l'origine
        assert (receipt["operation_type"], receipt["operation_id"], receipt["user_id"]) == (kind, operation_id,
                                                                                          USER_A)
        assert utc(proof["created_at"]) > utc(receipt["created_at"])
    return proof, event, attribution


# --------------------------------------------------------------------------
# 2. Chaîne réelle F2 -> B -> C
# --------------------------------------------------------------------------

def test_an_effective_creation_goes_from_f2_to_a_recorded_event(env, analytics, alice, caplog):
    campaign = open_wave(analytics, USER_A)
    command = create_command()
    result = run(env, analytics, alice, command, campaign)
    assert result.outcome.status == AUTHORIZED and result.proof == bridge.ProofReport(proofs.CERTIFIED, None)
    proof = proof_of(analytics, command.operation_id)
    report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    [receipt] = env.receipts()
    _, event, _ = continuity(analytics, alice, CREATE, command.operation_id, report, receipt=receipt)
    assert (event["event_type"], event["surface"], event["delivery_status"], event["delivered_at"]) == (
        "portfolio_used", "portfolio", "not_applicable", None)
    no_identifier_logged(caplog)


def test_an_effective_update_and_a_deletion_go_to_recorded_events(env, analytics, alice):
    campaign = open_wave(analytics, USER_A)
    position = env.insert_position(quantity=4.0)
    update = replace_command(position, 0, quantity="5")
    delete = delete_command(position, 1)
    for command, kind in ((update, UPDATE), (delete, DELETE)):
        assert run(env, analytics, alice, command, campaign).proof.state == proofs.CERTIFIED
        report = boundary.finalize_in_transaction(analytics, proof_id=proof_of(analytics, command.operation_id)["id"])
        [receipt] = [r for r in env.receipts() if r["operation_id"] == command.operation_id]
        continuity(analytics, alice, kind, command.operation_id, report, receipt=receipt)
    assert len(events(analytics)) == 2 and len(attributions(analytics)) == 2


def test_replays_noops_refusals_and_an_uncertain_f2_commit_never_produce_an_event(env, analytics, alice):
    campaign = open_wave(analytics, USER_A)
    command = create_command()
    run(env, analytics, alice, command, campaign)
    proof = proof_of(analytics, command.operation_id)
    first = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    snapshot = (events(analytics), attributions(analytics), proof_of(analytics, command.operation_id))
    # Rejeu du même reçu : aucune preuve nouvelle ; finalisation répétée : même événement, rien d'écrit.
    again = run(env, analytics, alice, command, campaign)
    assert again.proof == bridge.ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT)
    assert boundary.finalize_in_transaction(analytics, proof_id=proof["id"]) == first
    assert (events(analytics), attributions(analytics), proof_of(analytics, command.operation_id)) == snapshot
    # No-op : aucune preuve, aucun événement.
    position = env.insert_position(quantity=4.5, purchase_price=100.0, target_percent=10.0)
    noop = replace_command(position, 0)
    assert run(env, analytics, alice, noop, campaign).proof.reason == proofs.NO_NEW_EFFECT
    assert proof_of(analytics, noop.operation_id) is None
    # Refus F2 : aucune preuve, aucun événement.
    stale = env.insert_position(revision=3)
    theirs = env.insert_position(user_id=USER_B)
    env.confirmed(BOB, USER_B)
    for refused, status in ((replace_command(stale, 2, quantity="9"), REVISION_CONFLICT),
                            (delete_command(theirs, 0), NOT_FOUND)):
        result = run(env, analytics, alice, refused, campaign)
        assert result.outcome == AuthorizationOutcome(status) and proof_of(analytics, refused.operation_id) is None
    assert (events(analytics), attributions(analytics)) == snapshot[:2]


def test_an_uncertain_f2_commit_never_produces_an_event(env, analytics, alice):
    commits = []

    def commit_then_lose_connection(conn):
        if not commits and conn.info.get("receipt"):
            commits.append("commit")
            conn.connection.dbapi_connection.commit()
            raise OSError("connexion perdue après COMMIT")

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO portfolio_write_idempotency"):
            conn.info["receipt"] = True

    campaign = open_wave(analytics, USER_A)
    command = create_command()
    sa.event.listen(env.engine, "before_cursor_execute", mark)
    sa.event.listen(env.engine, "commit", commit_then_lose_connection)
    try:
        result = run(env, analytics, alice, command, campaign)
    finally:
        sa.event.remove(env.engine, "commit", commit_then_lose_connection)
        sa.event.remove(env.engine, "before_cursor_execute", mark)
        env.engine.dispose()
    assert commits == ["commit"] and result.outcome == AuthorizationOutcome(UNAVAILABLE) and len(env.receipts()) == 1
    assert proof_of(analytics, command.operation_id) is None and events(analytics) == []
    # Le retry F2 est un rejeu : la statistique perdue n'est jamais rattrapée.
    assert run(env, analytics, alice, command, campaign).proof.reason == proofs.NO_NEW_EFFECT
    assert proof_of(analytics, command.operation_id) is None and events(analytics) == []


@pytest.fixture
def decryptage(pg_url, schema, analytics):  # noqa: F811
    from tests.authz_f2_pr9c3_support import HttpEnv
    environment = HttpEnv(pg_url)
    yield environment
    environment.dispose()


def test_the_first_decrypter_ack_goes_to_a_confirmed_delivery_event_and_a_replay_does_not(decryptage, analytics):
    alice = decryptage.confirmed()
    done = decryptage.finalized(alice)
    campaign = open_wave(analytics, USER_A)
    runner = decryptage.b1_runner(decryptage_keys=None, hook=IDLE)
    first = bridge.run_with_proof_certification(runner, alice.credential(), done.ack(), analytics_engine=analytics,
                                                campaign_id=campaign)
    assert first.outcome.status == AUTHORIZED and first.proof.state == proofs.CERTIFIED
    proof = proof_of(analytics, done.assistant_turn_id)
    report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    _, event, _ = continuity(analytics, alice, ACK, done.assistant_turn_id, report)
    # Livraison CONFIRMÉE par le premier ACK ; delivered_at = instant serveur d'observation.
    assert (event["event_type"], event["surface"], event["delivery_status"], event["delivered_at"]) == (
        "decrypter_used", "decrypter", "confirmed", event["occurred_at"])
    snapshot = (events(analytics), attributions(analytics))
    replay = bridge.run_with_proof_certification(runner, alice.credential(), done.ack(), analytics_engine=analytics,
                                                 campaign_id=campaign)
    assert replay.proof == bridge.ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT)
    assert boundary.finalize_in_transaction(analytics, proof_id=proof["id"]) == report
    assert (events(analytics), attributions(analytics)) == snapshot


# --------------------------------------------------------------------------
# 3. Frontière de transaction : COMMIT incertain et pannes
# --------------------------------------------------------------------------

def _uncertain_commit(analytics, statement_prefix, *, really_commit):
    seen = []

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(statement_prefix):
            conn.info["target"] = True

    def lose(conn):
        if conn.info.get("target") and not seen:
            seen.append("lost")
            if really_commit:
                conn.connection.dbapi_connection.commit()
            raise OSError("confirmation du COMMIT analytics perdue")
    sa.event.listen(analytics, "before_cursor_execute", mark)
    sa.event.listen(analytics, "commit", lose)
    return seen, lambda: (sa.event.remove(analytics, "commit", lose),
                          sa.event.remove(analytics, "before_cursor_execute", mark))


def _certified_by_the_bridge(env, analytics, alice):
    campaign = open_wave(analytics, USER_A)
    command = create_command()
    assert run(env, analytics, alice, command, campaign).proof.state == proofs.CERTIFIED
    return proof_of(analytics, command.operation_id)


def test_an_uncertain_commit_that_really_happened_is_confirmed_by_rereading(env, analytics, alice, caplog):
    proof = _certified_by_the_bridge(env, analytics, alice)
    seen, remove = _uncertain_commit(analytics, "INSERT INTO product_activity_events", really_commit=True)
    try:
        report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    finally:
        remove()
    assert seen == ["lost"]
    after = proof_of(analytics, proof["source_operation_id"])
    assert report == boundary.FinalizationReport(fz.RECORDED, None, after["recorded_event_id"])
    assert len(events(analytics)) == 1 and len(attributions(analytics)) == 1
    no_identifier_logged(caplog)


def test_an_uncertain_commit_that_did_not_happen_announces_no_success_and_the_explicit_retry_is_clean(
        env, analytics, alice):
    proof = _certified_by_the_bridge(env, analytics, alice)
    seen, remove = _uncertain_commit(analytics, "INSERT INTO product_activity_events", really_commit=False)
    try:
        report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    finally:
        remove()
    assert seen == ["lost"]
    assert report == boundary.FinalizationReport(fz.CERTIFIED, proofs.TECHNICAL_UNCERTAINTY)  # jamais « recorded »
    assert proof_of(analytics, proof["source_operation_id"]) == proof and events(analytics) == []
    # Aucun retry automatique ; un retry EXPLICITE, sans F2 ni nouvelle certification, enregistre une fois.
    retried = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    assert retried.state == fz.RECORDED and len(events(analytics)) == 1
    after = proof_of(analytics, proof["source_operation_id"])
    assert (after["certified_at"], after["created_at"]) == (proof["certified_at"], proof["created_at"])


def test_an_uncertain_commit_that_cannot_be_confirmed_is_unknown(env, analytics, alice, monkeypatch):
    proof = _certified_by_the_bridge(env, analytics, alice)

    def unreadable(db, *, proof_id):
        raise OSError("relecture impossible")
    monkeypatch.setattr(fz, "observe_proof", unreadable)
    seen, remove = _uncertain_commit(analytics, "INSERT INTO product_activity_events", really_commit=True)
    try:
        report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    finally:
        remove()
    assert seen == ["lost"] and report == boundary.FinalizationReport(boundary.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)


def test_an_uncertain_rejection_commit_is_confirmed_or_reported_as_not_written(env, analytics, alice):
    proof = _certified_by_the_bridge(env, analytics, alice)
    with analytics.begin() as conn:  # retrait : la finalisation rejette
        conn.execute(sa.text("UPDATE product_campaign_participants SET participation_status = 'withdrawn' "
                             "WHERE campaign_id = :c AND user_id = :u"), {"c": proof["campaign_id"], "u": USER_A})
    seen, remove = _uncertain_commit(analytics, f"UPDATE {REGISTRY}", really_commit=True)
    try:
        report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    finally:
        remove()
    assert seen == ["lost"] and report == boundary.FinalizationReport(fz.REJECTED, proofs.NOT_AUTHORIZED)
    assert proof_of(analytics, proof["source_operation_id"])["status"] == "rejected" and events(analytics) == []


def test_a_failure_before_the_commit_writes_nothing_and_claims_nothing(env, analytics, alice, monkeypatch):
    proof = _certified_by_the_bridge(env, analytics, alice)

    def failing(*args, **kwargs):
        raise RuntimeError("panne injectée")
    monkeypatch.setattr(fz.events, "_stage_certified_event", failing)
    report = boundary.finalize_in_transaction(analytics, proof_id=proof["id"])
    assert report == boundary.FinalizationReport(boundary.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert proof_of(analytics, proof["source_operation_id"]) == proof and events(analytics) == []
    down = sa.create_engine(analytics.url.set(port=1), poolclass=sa.pool.NullPool)  # base injoignable
    assert boundary.finalize_in_transaction(down, proof_id=proof["id"]).state == boundary.UNKNOWN


def test_an_expired_pending_proof_is_resolved_through_the_boundary(env, analytics, monkeypatch):
    with analytics.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": USER_A})
    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5))
    monkeypatch.setattr(common, "utcnow", clock)
    monkeypatch.setattr(proofs, "server_clock", clock)
    origin, op = opened(analytics, USER_A)
    pending = register(analytics, origin, op)
    assert boundary.expire_pending_in_transaction(analytics, proof_id=pending.proof_id) == \
        boundary.FinalizationReport(fz.PENDING)
    assert boundary.finalize_in_transaction(analytics, proof_id=pending.proof_id) == boundary.FinalizationReport(
        fz.PENDING)  # jamais enregistrable
    [participation] = rows(analytics, "SELECT window_end_at FROM product_campaign_participants WHERE user_id = :u",
                           u=USER_A)
    clock.set(utc(participation["window_end_at"]) + GRACE)
    assert boundary.expire_pending_in_transaction(analytics, proof_id=pending.proof_id) == \
        boundary.FinalizationReport(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
    row = proof_of(analytics, op.operation_id)
    assert (row["status"], row["certified_at"], row["recorded_event_id"]) == ("indeterminate", None, None)
    assert events(analytics) == []


def test_a_decision_made_stale_by_a_unique_index_wait_records_nothing_and_changes_no_f2_result(env, analytics,
                                                                                              alice):
    """Contre-épreuve R1 sur la chaîne réelle, à travers la frontière
    (READ COMMITTED, lock_timeout 2 s) : vrai runner F2, vraie certification
    B, puis un écrivain hors service tient l'identité canonique sans
    committer ; la finalisation lit son horloge avant close_by_at, attend
    l'index unique moins que le lock_timeout, l'écrivain annule après
    close_by_at : aucun événement, preuve résolue à l'instant réel."""
    campaign, now = _active_campaign_and_window(analytics, window=timedelta(seconds=4),
                                                close_by=timedelta(seconds=4), user=USER_A)
    bound = now + timedelta(seconds=4)
    _sleep_until(now + timedelta(milliseconds=300))
    command = create_command()
    result = run(env, analytics, alice, command, campaign)
    assert result.outcome.status == AUTHORIZED and result.proof == bridge.ProofReport(proofs.CERTIFIED, None)
    proof = proof_of(analytics, command.operation_id)
    f2_before = (env.receipts(), rows(env.observer, "SELECT * FROM portfolio_positions WHERE user_id = :u", u=USER_A))
    analytics_before = rows(analytics, "SELECT * FROM product_activity_session_interactions ORDER BY recorded_at")
    target = SimpleNamespace(proof_id=proof["id"], campaign_id=campaign, origin=SimpleNamespace(
        visit_id=proof["visit_id"]), op=SimpleNamespace(owner_user_id=USER_A, operation_kind=CREATE,
                                                        operation_id=command.operation_id))
    holder = Holder(analytics, lambda s: _outside_event(s, target))
    _sleep_until(bound - timedelta(seconds=1))
    contender = Background(lambda: boundary.finalize_in_transaction(analytics, proof_id=proof["id"]))
    contender.start()
    try:
        _wait(lambda: _unique_index_waits(analytics) == 1, "la finalisation n'attend pas l'index unique")
        _sleep_until(bound + timedelta(milliseconds=300))
    finally:
        holder.rollback()
    report = contender.outcome()
    assert report == boundary.FinalizationReport(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
    assert events(analytics) == [] and attributions(analytics) == []
    after = proof_of(analytics, command.operation_id)
    assert (after["status"], after["resolution_reason"], after["recorded_event_id"]) == (
        "indeterminate", "finalization_expired", None)
    assert (after["certified_at"], after["created_at"], after["id"]) == (proof["certified_at"], proof["created_at"],
                                                                         proof["id"])
    assert utc(after["resolved_at"]) >= bound and after["purge_after_at"] - after["resolved_at"] == timedelta(
        hours=168)
    # Aucun rejeu F2, aucun changement de son résultat, aucune certification ni interaction nouvelles.
    assert (env.receipts(), rows(env.observer, "SELECT * FROM portfolio_positions WHERE user_id = :u",
                                 u=USER_A)) == f2_before
    assert len(rows(analytics, f"SELECT id FROM {REGISTRY}")) == 1
    assert rows(analytics, "SELECT * FROM product_activity_session_interactions ORDER BY recorded_at") == (
        analytics_before)
