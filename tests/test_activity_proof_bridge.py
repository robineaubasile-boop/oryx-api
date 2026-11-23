"""V0-T08 Lot 2B-2 bloc B — frontière privée F2 -> analytics
(core/_activity_proof_bridge.py) sous le VRAI runner F2 et le registre de
PRODUCTION, contre un VRAI PostgreSQL (16 et 18 en CI).

1. sans base : contrat de sortie, matrice des commandes certifiables égale à
   celle de F2, liaison stricte attestation / principal / commande, dormance
   (aucun appelant, imports exacts, api:app ne la charge pas) ;
2. contrat F2 : création, modification, suppression effectives certifiées ;
   no-op, rejeux, refus, ROLLBACK, COMMIT refusé ou incertain, absence
   d'attestation : rien n'est certifié ; premier ACK Décrypter certifié, ACK
   rejoué non certifiable ;
3. origine : autre propriétaire, interaction créée PENDANT l'opération,
   appareils multiples, opérations concurrentes ;
4. non-blocage : base analytics indisponible après le COMMIT F2, délai de
   verrou, violation de contrainte, échec de l'inscription, échec de la
   certification, erreur imprévue, COMMIT analytics incertain (dans les deux
   sens) : résultat F2 identique (même objet), un seul appel F2, reçu
   inchangé, aucune preuve faussement certifiée, journaux sans identifiant.
"""
import ast
import json
import logging
import re
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa

from core import _activity_proof_bridge as bridge
from core.authz_f2 import _adapters
from core.authz_f2 import attestations as att
from core.authz_f2 import commands as c
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import (
    AUTHORIZED,
    IDEMPOTENCY_CONFLICT,
    NOT_FOUND,
    REVISION_CONFLICT,
    SESSION_INVALID,
    UNAVAILABLE,
    AttestedOutcome,
    AuthorizationOutcome,
    AuthorizedTransactionRunner,
)
from core.product_analytics import proofs
from tests.activity_proof_support import (
    CREATE,
    DELETE,
    LAPTOP,
    PHONE,
    UPDATE,
    Background,
    Holder,
    analytics_engine,
    definitive_rows,
    open_wave,
    registry,
    touch,
    utc,
)
from tests.authz_f2_support import BOB, USER_A, USER_B, AuthzEnv, create_command, delete_command, replace_command
from tests.f2_support import prepare_schema
from tests.test_authz_f2_attestations import inject_fault
from tests.test_authz_f2_concurrency import Pause, wait_until
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

BRIDGE = REPO_ROOT / "core" / "_activity_proof_bridge.py"
BRIDGE_MODULE = "core._activity_proof_bridge"
IDLE = lambda point: None  # noqa: E731
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_the_certifiable_commands_are_exactly_the_attested_f2_commands():
    assert dict(bridge._CERTIFIABLE_COMMANDS) == dict(_adapters.ATTESTED_COMMANDS)
    assert {operation for operation, _ in bridge._CERTIFIABLE_COMMANDS.values()} == set(proofs.OPERATION_KINDS)


def test_the_result_contracts_are_closed():
    import dataclasses
    assert [f.name for f in dataclasses.fields(bridge.CertifiedRun)] == ["outcome", "proof"]
    assert [f.name for f in dataclasses.fields(bridge.ProofReport)] == ["state", "reason"]
    assert bridge.REPORT_STATES == ("absent", "pending", "certified", "recorded", "rejected", "indeterminate",
                                    "unknown")


PRINCIPAL = AuthorizedPrincipal(USER_A, uuid.uuid4(), uuid.uuid4(), "email_otp")
OP_ID = uuid.uuid4()


def _create(operation_id=OP_ID):
    return create_command(operation_id)


def _attested(qualification=att.PORTFOLIO_CREATED, *, owner=USER_A, operation=att.PORTFOLIO_CREATE,
              operation_id=OP_ID, status=AUTHORIZED, principal=PRINCIPAL):
    attestation = att.BusinessAttestation(owner, operation, operation_id, qualification)
    return AttestedOutcome(AuthorizationOutcome(status, object(), principal), attestation)


@pytest.mark.parametrize("case", ["refused", "foreign_owner", "other_nature", "other_identifier", "no_attestation",
                                  "foreign_object", "principal_type", "unknown_command", "not_attested_outcome"])
def test_only_an_exactly_bound_attestation_establishes_an_operation(case):
    command, attested = _create(), _attested()
    if case == "refused":
        attested = AttestedOutcome(AuthorizationOutcome(UNAVAILABLE), attested.attestation)
    elif case == "foreign_owner":
        attested = _attested(owner=USER_B)
    elif case == "other_nature":
        attested = _attested(att.PORTFOLIO_UPDATED, operation=att.PORTFOLIO_UPDATE)
    elif case == "other_identifier":
        attested = _attested(operation_id=uuid.uuid4())
    elif case == "no_attestation":
        attested = AttestedOutcome(attested.outcome, None)
    elif case == "foreign_object":
        fake = type("BusinessAttestation", (), {"owner_user_id": USER_A, "operation": att.PORTFOLIO_CREATE,
                                                "operation_id": OP_ID, "qualification": att.PORTFOLIO_CREATED})()
        attested = AttestedOutcome(attested.outcome, fake)
    elif case == "principal_type":
        attested = _attested(principal=type("P", (), {"user_id": USER_A})())
    elif case == "unknown_command":
        command = c.ListPositions()
    elif case == "not_attested_outcome":
        attested = (attested.outcome, attested.attestation)
    assert bridge._committed_operation(command, attested) == (None, proofs.SOURCE_UNVERIFIED)


@pytest.mark.parametrize("qualification, new_effect", [(att.PORTFOLIO_CREATED, True), (att.PORTFOLIO_REPLAY, False)])
def test_the_operation_uses_the_canonical_nature_and_the_principal(qualification, new_effect):
    operation, reason = bridge._committed_operation(_create(), _attested(qualification))
    assert reason is None
    assert operation == proofs.CommittedOperation(USER_A, "portfolio_position_create", OP_ID, new_effect)


def test_the_bridge_is_dormant_and_imports_exactly_its_two_sides():
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported == {"logging", "uuid", "dataclasses", "types", "sqlalchemy", "sqlalchemy.orm", "core.authz_f2",
                        "core.authz_f2.principal", "core.authz_f2.runner", "core.product_analytics"}
    text = BRIDGE.read_text(encoding="utf-8")
    for forbidden in ("os.environ", "getenv", "Thread", "asyncio", "schedule", "socket", "requests", "httpx",
                      "APIRouter", "FastAPI", "record_event", "events", "ProductActivityEventAttribution",
                      "_adapters", "import_module"):
        assert forbidden not in text, forbidden
    # Aucun appelant : aucun module hors des tests ne l'importe ni ne le nomme.
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if (not path.is_file() or path == BRIDGE or ".git" in path.parts or "__pycache__" in path.parts
                or "node_modules" in path.parts or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh",
                                                                        ".yml", ".yaml", ".toml", ".ini", ".cfg")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "_activity_proof_bridge" in text or "run_with_proof_certification" in text:
            offenders.append(str(relative))
    assert offenders == []


PROBE = r'''
import json, socket, sys
socket.socket.connect = lambda self, address: (_ for _ in ()).throw(OSError("réseau interdit"))
import api
loaded = sorted(n for n in sys.modules if n == "core._activity_proof_bridge" or n.startswith("core.product_analytics"))
print(json.dumps({"loaded": loaded, "routes": len(api.app.routes)}))
'''


def test_the_real_api_never_loads_the_bridge_whatever_the_environment():
    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1", "ORYX_ACTIVITY_PROOFS": "1",
                             "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        assert json.loads(out.stdout.strip().splitlines()[-1])["loaded"] == []


def test_a_foreign_runner_is_refused_before_anything_runs():
    with pytest.raises(TypeError):
        bridge.run_with_proof_certification(object(), None, _create(), analytics_engine=None, campaign_id=None,
                                            user_id=None, visit_id=None, interaction_key=None)


# --------------------------------------------------------------------------
# 2. PostgreSQL réel : préparation
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def analytics(pg_url, schema):  # noqa: F811
    engine = analytics_engine(pg_url)
    yield engine
    assert definitive_rows(engine) == (0, 0)  # jamais d'événement ni d'attribution définitive
    engine.dispose()


@pytest.fixture
def env(pg_url, schema, analytics, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="oryx.activity_proofs")
    environment = AuthzEnv(pg_url)  # vide F2 et users (CASCADE : analytics des tests précédents)
    yield environment
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


class F2Spy:
    """Compte les appels F2 et garde l'objet EXACT rendu par _run_attested."""

    def __init__(self, monkeypatch, after=None):
        self.calls, self.returned = 0, []
        original = AuthorizedTransactionRunner._run_attested

        def spy(runner, credential, command):
            self.calls += 1
            out = original(runner, credential, command)
            self.returned.append(out)
            if after is not None:
                after()
            return out
        monkeypatch.setattr(AuthorizedTransactionRunner, "_run_attested", spy)


@pytest.fixture
def spy(monkeypatch):
    return F2Spy(monkeypatch)


def origin_keys(analytics, user_id=USER_A, *, environment=LAPTOP, campaign_id=None) -> dict:
    campaign_id = campaign_id or open_wave(analytics, user_id)
    visit_id, key = touch(analytics, campaign_id, user_id, environment=environment)
    return {"campaign_id": campaign_id, "user_id": user_id, "visit_id": visit_id, "interaction_key": key}


def run(env, analytics, issued, command, origin, *, hook=None, runner=None) -> bridge.CertifiedRun:
    runner = runner or env.runner(hook=hook or IDLE)
    return bridge.run_with_proof_certification(runner, issued.credential(), command, analytics_engine=analytics,
                                               **origin)


def exact(spy, result, status=AUTHORIZED):
    """Résultat F2 rendu TEL QUEL : même objet que la sortie attestée, un seul appel."""
    assert spy.calls == 1 and result.outcome is spy.returned[0].outcome and result.outcome.status == status
    return result


def no_identifier_logged(caplog):
    for record in caplog.records:
        if record.name == "oryx.activity_proofs":
            message = record.getMessage()
            assert USER_A not in message and USER_B not in message and not UUID_PATTERN.search(message), message
            assert re.fullmatch(r"activity_proof state=[a-z]+ reason=[a-z_]+|activity_proof state=[a-z]+ reason=None",
                                message), message


def certified_row(analytics, kind, operation_id, origin):
    [row] = registry(analytics, origin["user_id"])
    assert (row["status"], row["source_operation_kind"], row["source_operation_id"]) == ("certified", kind,
                                                                                         operation_id)
    assert (row["campaign_id"], row["visit_id"], row["interaction_key"]) == (
        origin["campaign_id"], origin["visit_id"], origin["interaction_key"])
    assert row["resolved_at"] is None and row["recorded_event_id"] is None and row["purge_after_at"] is None
    return row


# --------------------------------------------------------------------------
# 3. Contrat F2 — Portfolio
# --------------------------------------------------------------------------

def test_an_effective_creation_is_certified_after_the_f2_commit(env, analytics, alice, spy, caplog):
    origin = origin_keys(analytics)
    command = create_command()
    result = exact(spy, run(env, analytics, alice, command, origin))
    assert result.proof == bridge.ProofReport(proofs.CERTIFIED, None)
    row = certified_row(analytics, CREATE, command.operation_id, origin)
    assert (row["event_type"], row["surface"]) == ("portfolio_used", "portfolio")
    [receipt] = env.receipts()
    # Certification strictement après le COMMIT F2 (horloge PostgreSQL, jamais T_authorization).
    assert utc(row["created_at"]) > utc(receipt["created_at"]) and row["certified_at"] > row["created_at"]
    assert result.outcome == env.runner().run(alice.credential(), command)  # même réponse qu'un rejeu historique
    no_identifier_logged(caplog)


def test_an_effective_update_and_a_deletion_are_certified(env, analytics, alice):
    origin = origin_keys(analytics)
    position = env.insert_position(quantity=4.0)
    update = replace_command(position, 0, quantity="5")
    assert run(env, analytics, alice, update, origin).proof.state == proofs.CERTIFIED
    origin2 = origin_keys(analytics, campaign_id=origin["campaign_id"])
    delete = delete_command(position, 1)
    assert run(env, analytics, alice, delete, origin2).proof.state == proofs.CERTIFIED
    kinds = {(r["source_operation_kind"], r["source_operation_id"], r["status"]) for r in registry(analytics, USER_A)}
    assert kinds == {(UPDATE, update.operation_id, "certified"), (DELETE, delete.operation_id, "certified")}


def test_a_noop_update_is_never_certifiable(env, analytics, alice, spy):
    origin = origin_keys(analytics)
    position = env.insert_position(quantity=4.5, purchase_price=100.0, target_percent=10.0)
    result = exact(spy, run(env, analytics, alice, replace_command(position, 0), origin))
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT) and registry(analytics) == []


@pytest.mark.parametrize("make", ["create", "update", "noop", "delete"])
def test_a_receipt_replay_is_never_a_new_use(env, analytics, alice, make):
    origin = origin_keys(analytics)
    if make == "create":
        command = create_command()
    else:
        position = env.insert_position(quantity=4.5, purchase_price=100.0, target_percent=10.0)
        command = {"update": replace_command(position, 0, quantity="5"), "noop": replace_command(position, 0),
                   "delete": delete_command(position, 0)}[make]
    first = run(env, analytics, alice, command, origin)
    before = registry(analytics)
    origin2 = origin_keys(analytics, campaign_id=origin["campaign_id"])
    again = run(env, analytics, alice, command, origin2)
    assert again.outcome.value == first.outcome.value and again.outcome.value.body == first.outcome.value.body
    assert again.proof == bridge.ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT)
    assert registry(analytics) == before  # aucune écriture, aucune réattribution


def test_f2_refusals_certify_nothing(env, analytics, alice):
    origin = origin_keys(analytics)
    position = env.insert_position(revision=3)
    bob = env.confirmed(BOB, USER_B)
    theirs = env.insert_position(user_id=USER_B)
    done = create_command(quantity="4")
    assert run(env, analytics, alice, done, origin).proof.state == proofs.CERTIFIED
    before = registry(analytics)
    cases = [(replace_command(position, 2, quantity="9"), REVISION_CONFLICT),
             (create_command(done.operation_id, quantity="7"), IDEMPOTENCY_CONFLICT),
             (delete_command(theirs, 0), NOT_FOUND)]
    for command, status in cases:
        result = run(env, analytics, alice, command, origin_keys(analytics, campaign_id=origin["campaign_id"]))
        assert result.outcome == AuthorizationOutcome(status)
        assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.SOURCE_UNVERIFIED)
    env.service.revoke_session(alice.session_id, "security")
    result = run(env, analytics, alice, create_command(), origin)
    assert result.outcome == AuthorizationOutcome(SESSION_INVALID) and result.proof.reason == proofs.SOURCE_UNVERIFIED
    assert registry(analytics) == before and bob is not None


@pytest.mark.parametrize("point", ["position_written", "receipt_written", "executed"])
def test_an_f2_rollback_certifies_nothing(env, analytics, alice, point):
    def fail(at):
        if at == point:
            raise RuntimeError("panne avant COMMIT (simulée)")
    result = run(env, analytics, alice, create_command(), origin_keys(analytics), hook=fail)
    assert result.outcome == AuthorizationOutcome(UNAVAILABLE) and env.receipts() == []
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.SOURCE_UNVERIFIED) and registry(analytics) == []


def test_a_refused_f2_commit_certifies_nothing(env, analytics, alice):
    def refuse_commit(conn):
        raise OSError("COMMIT refusé")
    sa.event.listen(env.engine, "commit", refuse_commit)
    try:
        result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    finally:
        sa.event.remove(env.engine, "commit", refuse_commit)
    assert result.outcome == AuthorizationOutcome(UNAVAILABLE)
    assert result.proof.reason == proofs.SOURCE_UNVERIFIED and registry(analytics) == []


def test_an_uncertain_f2_commit_certifies_nothing_even_though_the_receipt_exists(env, analytics, alice):
    commits = []

    def commit_then_lose_connection(conn):
        if not commits and conn.info.get("receipt"):
            commits.append("commit")
            conn.connection.dbapi_connection.commit()
            raise OSError("connexion perdue après COMMIT")

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO portfolio_write_idempotency"):
            conn.info["receipt"] = True

    origin = origin_keys(analytics)
    command = create_command()
    sa.event.listen(env.engine, "before_cursor_execute", mark)
    sa.event.listen(env.engine, "commit", commit_then_lose_connection)
    try:
        result = run(env, analytics, alice, command, origin)
    finally:
        sa.event.remove(env.engine, "commit", commit_then_lose_connection)
        sa.event.remove(env.engine, "before_cursor_execute", mark)
        env.engine.dispose()
    assert commits == ["commit"] and result.outcome == AuthorizationOutcome(UNAVAILABLE)
    assert len(env.receipts()) == 1  # committé malgré l'erreur : la statistique est perdue, jamais présumée
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.SOURCE_UNVERIFIED) and registry(analytics) == []
    # Le retry est un rejeu : il ne rattrape jamais la statistique perdue.
    again = run(env, analytics, alice, command, origin_keys(analytics, campaign_id=origin["campaign_id"]))
    assert again.outcome.status == AUTHORIZED and again.proof.reason == proofs.NO_NEW_EFFECT
    assert registry(analytics) == []


@pytest.mark.parametrize("fault", ["binding", "foreign_object"])
def test_a_dropped_attestation_certifies_nothing_while_f2_succeeds(env, analytics, alice, monkeypatch, fault):
    inject_fault(monkeypatch, fault)
    result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    assert result.outcome.status == AUTHORIZED and len(env.receipts()) == 1
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.SOURCE_UNVERIFIED) and registry(analytics) == []


def test_an_authorized_command_without_attestation_certifies_nothing(env, analytics, alice):
    result = run(env, analytics, alice, c.ListPositions(), origin_keys(analytics))
    assert result.outcome.status == AUTHORIZED
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.SOURCE_UNVERIFIED) and registry(analytics) == []


# --------------------------------------------------------------------------
# 4. Origine
# --------------------------------------------------------------------------

def test_an_origin_of_another_user_is_an_owner_conflict(env, analytics, alice):
    env.confirmed(BOB, USER_B)
    result = run(env, analytics, alice, create_command(), origin_keys(analytics, USER_B))
    assert result.outcome.status == AUTHORIZED
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.OWNER_CONFLICT) and registry(analytics) == []


def test_an_interaction_accepted_during_the_operation_is_never_its_origin(env, analytics, alice):
    """La clé n'est pas encore acceptée quand F2 commence ; elle l'est (et
    committée) pendant la transaction métier : aucune justification
    rétroactive."""
    origin = origin_keys(analytics)
    late_key = uuid.uuid4()

    def accept_during_f2(point):
        if point == "executed":
            touch(analytics, origin["campaign_id"], USER_A, key=late_key)
    result = run(env, analytics, alice, create_command(), {**origin, "interaction_key": late_key},
                 hook=accept_during_f2)
    assert result.outcome.status == AUTHORIZED
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.ORIGIN_UNVERIFIED) and registry(analytics) == []
    with analytics.connect() as conn:  # l'interaction existe bien désormais : seule la causalité la disqualifie
        assert conn.execute(sa.text("SELECT count(*) FROM product_activity_session_interactions "
                                    "WHERE interaction_key = :k"), {"k": late_key}).scalar_one() == 1


def test_an_unknown_or_close_interaction_is_never_an_origin(env, analytics, alice):
    origin = origin_keys(analytics)
    for keys in ({**origin, "interaction_key": uuid.uuid4()}, {**origin, "visit_id": str(origin["visit_id"])}):
        result = run(env, analytics, alice, create_command(), keys)
        assert result.outcome.status == AUTHORIZED and result.proof == bridge.ProofReport(proofs.ABSENT,
                                                                                          proofs.ORIGIN_UNVERIFIED)
    assert registry(analytics) == []


def test_a_replay_from_a_second_device_never_reattributes_the_certified_operation(env, analytics, alice):
    laptop = origin_keys(analytics, environment=LAPTOP)
    phone = origin_keys(analytics, environment=PHONE, campaign_id=laptop["campaign_id"])
    assert laptop["visit_id"] != phone["visit_id"]
    command = create_command()
    assert run(env, analytics, alice, command, laptop).proof.state == proofs.CERTIFIED
    row = certified_row(analytics, CREATE, command.operation_id, laptop)
    phone_session = env.confirmed("alice2@example.com", USER_A)  # second appareil, seconde session F2
    again = run(env, analytics, phone_session, command, phone)
    assert again.outcome.status == AUTHORIZED and again.proof.reason == proofs.NO_NEW_EFFECT
    assert registry(analytics) == [row]


def test_concurrent_identical_operations_from_two_devices_are_certified_once(env, analytics):
    sessions = env.confirmed(), env.confirmed("alice2@example.com", USER_A)
    laptop = origin_keys(analytics, environment=LAPTOP)
    phone = origin_keys(analytics, environment=PHONE, campaign_id=laptop["campaign_id"])
    command = create_command()
    pause = Pause("executed")
    holder = Background(lambda: run(env, analytics, sessions[0], command, laptop, hook=pause))
    holder.start()
    assert pause.reached.wait(10)
    contender = Background(lambda: run(env, analytics, sessions[1], command, phone))
    contender.start()
    try:
        wait_until(lambda: env.waiting_on_lock() == 1, message="le second n'attend pas")
    finally:
        pause.release.set()
    won, other = holder.outcome(), contender.outcome()
    assert (won.proof.state, other.proof.reason) == (proofs.CERTIFIED, proofs.NO_NEW_EFFECT)
    certified_row(analytics, CREATE, command.operation_id, laptop)


# --------------------------------------------------------------------------
# 5. Contrat F2 — ACK Décrypter (vraie chaîne B1 -> B2 -> C1-B)
# --------------------------------------------------------------------------

@pytest.fixture
def decryptage(pg_url, schema, analytics):  # noqa: F811
    from tests.authz_f2_pr9c3_support import HttpEnv
    environment = HttpEnv(pg_url)
    yield environment
    environment.dispose()


def test_the_first_ack_is_certified_and_a_replayed_ack_is_not(decryptage, analytics):
    alice = decryptage.confirmed()
    done = decryptage.finalized(alice)
    origin = origin_keys(analytics)
    runner = decryptage.b1_runner(decryptage_keys=None, hook=IDLE)
    first = bridge.run_with_proof_certification(runner, alice.credential(), done.ack(), analytics_engine=analytics,
                                                **origin)
    assert first.outcome.status == AUTHORIZED and first.outcome.value.delivery_status == "delivered"
    assert first.proof == bridge.ProofReport(proofs.CERTIFIED, None)
    row = certified_row(analytics, "decryptage_delivery_ack", done.assistant_turn_id, origin)
    assert (row["event_type"], row["surface"]) == ("decrypter_used", "decrypter")
    replay = bridge.run_with_proof_certification(runner, alice.credential(), done.ack(), analytics_engine=analytics,
                                                 **origin_keys(analytics, campaign_id=origin["campaign_id"]))
    assert replay.outcome.value == first.outcome.value
    assert replay.proof == bridge.ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT)
    assert registry(analytics) == [row]


# --------------------------------------------------------------------------
# 6. Non-blocage : pannes analytics APRÈS le COMMIT F2
# --------------------------------------------------------------------------

def _after_commit(monkeypatch, action):
    return F2Spy(monkeypatch, after=action)


def _unchanged_f2(env, spy, result):
    exact(spy, result)
    assert result.outcome.value.status == 201 and len(env.receipts()) == 1  # reçu unique, réponse historique


def test_an_analytics_database_down_after_the_f2_commit_changes_nothing(env, analytics, alice, monkeypatch, caplog):
    down = []

    def refuse(dialect, conn_rec, cargs, cparams):
        if down:
            raise sa.exc.OperationalError("connect", {}, OSError("PostgreSQL analytics indisponible"))
    sa.event.listen(analytics, "do_connect", refuse)
    spy = _after_commit(monkeypatch, lambda: down.append(True))
    try:
        result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    finally:
        sa.event.remove(analytics, "do_connect", refuse)
    _unchanged_f2(env, spy, result)
    assert result.proof == bridge.ProofReport(bridge.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert registry(analytics) == []
    no_identifier_logged(caplog)


def test_an_analytics_database_down_before_f2_never_blocks_the_operation(env, analytics, alice, spy):
    origin = origin_keys(analytics)
    broken = sa.create_engine("postgresql+psycopg2://postgres:postgres@127.0.0.1:1/oryx_none_test",
                              poolclass=sa.pool.NullPool)
    result = bridge.run_with_proof_certification(env.runner(hook=IDLE), alice.credential(), create_command(),
                                                 analytics_engine=broken, **origin)
    _unchanged_f2(env, spy, result)
    assert result.proof == bridge.ProofReport(proofs.ABSENT, proofs.TECHNICAL_UNCERTAINTY)
    result = bridge.run_with_proof_certification(env.runner(hook=IDLE), alice.credential(), create_command(),
                                                 analytics_engine=None, **origin)  # moteur inutilisable
    assert result.outcome.status == AUTHORIZED and result.proof.reason == proofs.TECHNICAL_UNCERTAINTY


def test_a_lock_timeout_in_analytics_changes_nothing(env, analytics, alice, spy):
    origin = origin_keys(analytics)
    holder = Holder(analytics, sql="SELECT 1 FROM product_campaign_participants WHERE campaign_id = :c FOR UPDATE",
                    c=origin["campaign_id"])
    try:
        result = run(env, analytics, alice, create_command(), origin)
    finally:
        holder.rollback()
    _unchanged_f2(env, spy, result)
    assert result.proof == bridge.ProofReport(bridge.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert registry(analytics) == []


def test_a_constraint_violation_in_analytics_changes_nothing(env, analytics, alice, spy, monkeypatch):
    monkeypatch.setitem(proofs.OPERATION_RULES, CREATE, ("portfolio_used", "decrypter"))  # CHECK 0024 violé
    result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    _unchanged_f2(env, spy, result)
    assert result.proof == bridge.ProofReport(bridge.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert registry(analytics) == []


def _boom(*args, **kwargs):
    raise RuntimeError("panne analytics injectée")


def test_a_failed_pending_registration_changes_nothing(env, analytics, alice, spy, monkeypatch):
    monkeypatch.setattr(proofs, "register_pending", _boom)
    result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    _unchanged_f2(env, spy, result)
    assert result.proof == bridge.ProofReport(bridge.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert registry(analytics) == []


def test_a_failed_certification_leaves_an_indeterminate_proof_never_a_certified_one(env, analytics, alice, spy,
                                                                                   monkeypatch):
    monkeypatch.setattr(proofs, "certify_pending", _boom)
    result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    _unchanged_f2(env, spy, result)
    assert result.proof == bridge.ProofReport(proofs.INDETERMINATE, proofs.TECHNICAL_UNCERTAINTY)
    [row] = registry(analytics)
    assert (row["status"], row["certified_at"], row["resolution_reason"]) == ("indeterminate", None,
                                                                              "technical_uncertainty")


@pytest.mark.parametrize("target", ["read_origin", "_certify", "_committed_operation", "_log"])
def test_an_unexpected_analytics_error_changes_nothing(env, analytics, alice, spy, monkeypatch, target):
    monkeypatch.setattr(proofs if target == "read_origin" else bridge, target, _boom)
    result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    _unchanged_f2(env, spy, result)
    if target == "_log":
        assert result.proof.state == proofs.CERTIFIED
    else:
        assert result.proof.state in (proofs.ABSENT, bridge.UNKNOWN) and registry(analytics) == []
        assert result.proof.reason in (proofs.TECHNICAL_UNCERTAINTY, proofs.SOURCE_UNVERIFIED)


def _uncertain_commit(analytics, statement_prefix, *, really_commit):
    """COMMIT analytics dont la confirmation est perdue, pour la transaction
    qui a exécuté `statement_prefix` (effectué ou non, selon really_commit)."""
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


@pytest.mark.parametrize("really_commit, state, status", [(True, proofs.CERTIFIED, "certified"),
                                                          (False, proofs.INDETERMINATE, "indeterminate")])
def test_an_uncertain_certification_commit_is_settled_by_rereading_the_same_proof(env, analytics, alice, spy,
                                                                                  really_commit, state, status):
    seen, remove = _uncertain_commit(analytics, "UPDATE product_activity_proof_registry", really_commit=really_commit)
    try:
        result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    finally:
        remove()
    _unchanged_f2(env, spy, result)
    assert seen == ["lost"] and result.proof.state == state
    [row] = registry(analytics)
    assert row["status"] == status and (row["certified_at"] is not None) is really_commit


@pytest.mark.parametrize("really_commit, report", [
    (True, bridge.ProofReport(proofs.CERTIFIED, None)),
    (False, bridge.ProofReport(proofs.ABSENT, proofs.TECHNICAL_UNCERTAINTY))])
def test_an_uncertain_registration_commit_never_creates_a_proof_by_itself(env, analytics, alice, spy,
                                                                          really_commit, report):
    seen, remove = _uncertain_commit(analytics, "INSERT INTO product_activity_proof_registry",
                                     really_commit=really_commit)
    try:
        result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    finally:
        remove()
    _unchanged_f2(env, spy, result)
    assert seen == ["lost"] and result.proof == report
    assert [r["status"] for r in registry(analytics)] == (["certified"] if really_commit else [])


def test_an_uncertain_settlement_claims_nothing(env, analytics, alice, spy, monkeypatch):
    monkeypatch.setattr(proofs, "certify_pending", _boom)
    seen, remove = _uncertain_commit(analytics, "UPDATE product_activity_proof_registry", really_commit=False)
    try:
        result = run(env, analytics, alice, create_command(), origin_keys(analytics))
    finally:
        remove()
    _unchanged_f2(env, spy, result)
    assert seen == ["lost"] and result.proof == bridge.ProofReport(bridge.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert [r["status"] for r in registry(analytics)] == ["pending"]  # jamais présumée certifiée


def test_the_f2_response_bytes_are_those_of_run(env, analytics, alice):
    a, b = create_command(), create_command()
    historical = env.runner(hook=IDLE).run(alice.credential(), a)
    measured = run(env, analytics, alice, b, origin_keys(analytics))
    assert type(measured.outcome) is AuthorizationOutcome and measured.outcome.status == historical.status
    assert measured.outcome.value.status == historical.value.status and measured.outcome.principal == \
        historical.principal
    replayed = env.runner(hook=IDLE).run(alice.credential(), b)
    assert replayed.value.body == measured.outcome.value.body  # reçu canonique inchangé
