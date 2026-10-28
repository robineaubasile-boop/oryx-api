"""Portefeuille F2 versionné et idempotent (R1 PR 7) — runner contre un vrai
PostgreSQL jetable.

Sessions émises par le vrai moteur F2 ; positions et reçus observés par une
connexion indépendante (float8send, xmin, dates). Couvre :

- CAS : création à la révision 0, matrice (exacte / périmée / future,
  valeurs différentes / identiques), no-op sans UPDATE (xmin et updated_at
  inchangés) mais avec reçu, ABA, révision maximale (identique 200,
  changement 503 + alerte, suppression 200), réparation explicite d'une
  ligne historique invalide, suppression sans réparation, écriture
  conditionnelle sans ligne = invariant ;
- idempotence durable : rejeux CREATE / UPDATE / DELETE (octets identiques,
  aucune écriture), rejeux après suppression, nouvelle opération sur une
  position disparue (404), rejeu contradictoire (409), même UUID pour un
  autre compte ou un autre type, deux UUID pour un même contenu, reçus
  corrompus ou de version inconnue (503, aucune réexécution), rotation et
  conservation des clés, commit incertain puis rejeu, rollback puis
  reprise unique, panne à chaque point (aucun demi-état) ;
- historique : lecture diagnostique d'une liste mixte, indépendance de
  extra_float_digits, GET sans écriture ;
- autorisation : session invalide prioritaire sur 404 / 409 / rejeu, rejeu
  soumis aux mêmes contrôles, propriété par user_id serveur (position ou
  reçu d'autrui jamais lu ni verrouillé), READ COMMITTED exigé, trousseau
  absent, ordre exact des verrous, délai de verrou.
"""
import json
import uuid

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import portfolio_values as pv
from core.authz_f2.principal import AuthorizedContext, AuthorizedPrincipal
from core.authz_f2.runner import (
    AUTHORIZED,
    IDEMPOTENCY_CONFLICT,
    NOT_FOUND,
    REVISION_CONFLICT,
    SESSION_INVALID,
    UNAVAILABLE,
    AuthorizationOutcome,
)
from tests.authz_f2_support import (
    BOB,
    K1,
    K2,
    USER_A,
    USER_B,
    AuthzEnv,
    create_command,
    delete_command,
    portfolio_keyring,
    replace_command,
)
from tests.f2_support import SimulatedCrash, prepare_schema, rows, scalar
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

MAX = 2**63 - 1


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


def created(env, issued, **values):
    outcome = run(env, issued, create_command(**values))
    assert outcome.status == AUTHORIZED, outcome
    return outcome.value.position_id


def body(outcome):
    return json.loads(outcome.value.body)


# --------------------------------------------------------------------------
# Création et CAS
# --------------------------------------------------------------------------

def test_create_starts_at_revision_zero_with_its_receipt_in_the_same_transaction(env, alice):
    op = uuid.UUID("11111111-1111-4111-8111-111111111111")
    outcome = run(env, alice, create_command(op, quantity="4", envelope="PEA"))
    assert outcome.status == AUTHORIZED and outcome.principal.user_id == USER_A
    result = outcome.value
    assert result.status == 201 and result.view.position_revision == 0 and result.operation_id == op
    row = env.position(result.position_id)
    assert (row["user_id"], row["ticker"], row["q"], row["p"], row["tp"], row["envelope"], row["position_revision"]) \
        == (USER_A, "AIR.PA", 4.0, None, None, "PEA", 0)
    assert row["created_at"] == row["updated_at"]  # T_authorization (UTC, colonne sans fuseau)
    (receipt,) = env.receipts()
    assert (receipt["operation_type"], receipt["operation_id"], receipt["fingerprint_version"],
            receipt["fingerprint_key_version"], receipt["result_version"], receipt["result_status"],
            receipt["position_id"]) == ("portfolio_position_create", op, "oryx-portfolio-request-v1", "k1",
                                        "portfolio-write-result-v1", 201, result.position_id)
    # Vecteur normatif CREATE (test-user-a) : même trame, autre user_id ici.
    assert len(receipt["request_fingerprint"]) == 64
    assert json.loads(receipt["payload_text"]) == body(outcome)
    assert USER_A not in result.body.decode() and receipt["request_fingerprint"] not in result.body.decode()
    assert body(outcome)["position"] == {
        "position_id": result.position_id, "position_revision": "0", "ticker": "AIR.PA", "quantity": "4",
        "purchase_price": None, "target_percent": None, "envelope": "PEA", "numeric_encoding": "binary64-decimal-v1",
        "data_status": "valid", "validation_issues": []}


@pytest.mark.parametrize("revision,quantity,status,after", [
    (7, "5", AUTHORIZED, 8),          # exacte, différente : +1
    (7, "4", AUTHORIZED, 7),          # exacte, identique : aucun UPDATE
    (6, "5", REVISION_CONFLICT, 7),   # périmée, différente
    (6, "4", REVISION_CONFLICT, 7),   # périmée, identique
    (8, "4", REVISION_CONFLICT, 7),   # future : périmée aussi
])
def test_cas_matrix(env, alice, revision, quantity, status, after):
    target = env.insert_position(revision=7, quantity=4.0, purchase_price=100.0, target_percent=10.0)
    before = env.position(target)
    outcome = run(env, alice, replace_command(target, revision, quantity=quantity))
    assert outcome.status == status
    row = env.position(target)
    assert row["position_revision"] == after
    if status == AUTHORIZED:
        assert outcome.value.view.position_revision == after and outcome.value.status == 200
        assert len(env.receipts()) == 1
    else:
        assert outcome == AuthorizationOutcome(REVISION_CONFLICT)  # ni révision courante ni principal
        assert env.receipts() == []
    if after == 7:
        assert row == before  # xmin, updated_at et valeurs inchangés : aucune écriture de la position
    else:
        assert row["xmin"] != before["xmin"] and row["q"] == 5.0 and row["updated_at"] != before["updated_at"]


def test_put_is_a_full_replacement_never_a_patch(env, alice):
    target = env.insert_position(quantity=4.0, purchase_price=100.0, target_percent=10.0, envelope="PEA")
    outcome = run(env, alice, replace_command(target, 0, ticker="MC.PA", quantity="4", purchase_price=None,
                                              target_percent=None, envelope=None))
    assert outcome.status == AUTHORIZED
    row = env.position(target)
    assert (row["ticker"], row["q"], row["p"], row["tp"], row["envelope"], row["position_revision"]) == (
        "MC.PA", 4.0, None, None, None, 1)


def test_noop_records_a_receipt_and_two_distinct_noops_both_succeed(env, alice):
    target = env.insert_position(revision=3, quantity=4.0, purchase_price=100.0, target_percent=10.0)
    before = env.position(target)
    first = run(env, alice, replace_command(target, 3, quantity="4"))
    second = run(env, alice, replace_command(target, 3, quantity="4"))
    assert first.status == second.status == AUTHORIZED
    assert first.value.view.position_revision == second.value.view.position_revision == 3
    assert env.position(target) == before
    assert len(env.receipts()) == 2


def test_aba_increments_twice_and_the_old_revision_conflicts(env, alice):
    target = env.insert_position(quantity=4.0, purchase_price=100.0, target_percent=10.0)
    assert run(env, alice, replace_command(target, 0, quantity="5")).value.view.position_revision == 1
    assert run(env, alice, replace_command(target, 1, quantity="4")).value.view.position_revision == 2
    stale = run(env, alice, replace_command(target, 0, quantity="4"))  # valeurs identiques à A : conflit quand même
    assert stale.status == REVISION_CONFLICT
    assert env.position(target)["position_revision"] == 2


def test_delete_requires_the_exact_revision(env, alice):
    target = env.insert_position(revision=4)
    assert run(env, alice, delete_command(target, 3)).status == REVISION_CONFLICT
    assert env.position(target) is not None and env.receipts() == []
    outcome = run(env, alice, delete_command(target, 4))
    assert outcome.status == AUTHORIZED and outcome.value.deleted_revision == 4 and outcome.value.status == 200
    assert env.position(target) is None
    assert body(outcome) == {"operation_id": str(outcome.value.operation_id),
                             "operation_type": "portfolio_position_delete", "position_id": target,
                             "outcome": "deleted", "deleted_revision": "4"}


@pytest.mark.parametrize("start", [2**53 - 1, 2**53, 2**53 + 1, MAX - 1])
def test_large_revisions_increment_exactly(env, alice, start):
    target = env.insert_position(revision=start, quantity=4.0, purchase_price=100.0, target_percent=10.0)
    outcome = run(env, alice, replace_command(target, start, quantity="5"))
    assert outcome.value.view.position_revision == start + 1
    assert body(outcome)["position"]["position_revision"] == str(start + 1)
    assert env.position(target)["position_revision"] == start + 1


def test_maximum_revision_is_never_overflowed(env, alice, caplog):
    target = env.insert_position(revision=MAX, quantity=4.0, purchase_price=100.0, target_percent=10.0)
    before = env.position(target)
    same = run(env, alice, replace_command(target, MAX, quantity="4"))
    assert same.status == AUTHORIZED and same.value.view.position_revision == MAX
    with caplog.at_level("DEBUG", logger="oryx.authz_f2"):
        change = run(env, alice, replace_command(target, MAX, quantity="5"))
    assert change == AuthorizationOutcome(UNAVAILABLE)
    assert env.position(target) == before and len(env.receipts()) == 1  # seul le no-op a un reçu
    alerts = [rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"]
    assert alerts == ["authz_f2 ReplacePortfolioPosition status=unavailable alert=position_revision_exhausted"]
    assert str(MAX) not in caplog.text and USER_A not in caplog.text
    deleted = run(env, alice, delete_command(target, MAX))
    assert deleted.status == AUTHORIZED and deleted.value.deleted_revision == MAX
    assert env.position(target) is None


def test_legacy_invalid_position_is_repaired_only_by_an_explicit_full_put(env, alice):
    target = env.insert_position(ticker="air pa", quantity=-2.0, purchase_price=float("nan"),
                                 target_percent=150.0, envelope="AV")
    read = run(env, alice, c.ReadPosition(target))
    assert read.value.data_status == "legacy_invalid" and read.value.validation_issues == pv.ISSUE_CODES
    assert (read.value.quantity, read.value.purchase_price) == ("-2", "NaN")
    before = env.position(target)["xmin"]
    run(env, alice, c.ListPositions())
    assert env.position(target)["xmin"] == before  # GET ne répare jamais (NaN != NaN : xmin comparé)
    repaired = run(env, alice, replace_command(target, 0, ticker="AIR.PA", quantity="2", purchase_price="10",
                                               target_percent="15", envelope="CTO"))
    assert repaired.status == AUTHORIZED and repaired.value.view.data_status == "valid"
    assert repaired.value.view.position_revision == 1


def test_legacy_invalid_position_can_be_deleted_without_repair(env, alice):
    target = env.insert_position(ticker="", quantity=float("-inf"), envelope="??")
    outcome = run(env, alice, delete_command(target, 0))
    assert outcome.status == AUTHORIZED and env.position(target) is None


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _Conn:
    """Connexion simulée (frontière seulement) : rend les lignes données."""

    def __init__(self, *rows_):
        self.rows = list(rows_)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _Rows(self.rows.pop(0))


def _context():
    principal = AuthorizedPrincipal(USER_A, uuid.uuid4(), uuid.uuid4(), "supabase_email_sign_in")
    from datetime import datetime, timezone
    return AuthorizedContext(principal, datetime.now(timezone.utc))


def _locked_row(revision=7, quantity=4.0):
    import struct
    return {"id": 42, "position_revision": revision, "ticker": "AIR.PA", "quantity": struct.pack(">d", quantity),
            "purchase_price": struct.pack(">d", 100.0), "target_percent": struct.pack(">d", 10.0), "envelope": "PEA"}


@pytest.mark.parametrize("command", [replace_command(42, 7, quantity="5"), delete_command(42, 7)])
def test_conditional_write_returning_no_row_is_an_invariant_not_a_conflict(command):
    conn = _Conn(None)
    with pytest.raises(_adapters.AdapterInvariantBroken) as raised:
        _adapters._write_portfolio(conn, _context(), command, _adapters.PositionFound(_locked_row()),
                                   portfolio_keyring(), lambda point: None)
    assert not isinstance(raised.value, _adapters.RevisionExhausted)
    assert conn.statements[0].startswith(("UPDATE portfolio_positions", "DELETE FROM portfolio_positions"))
    assert len(conn.statements) == 1  # aucun reçu après l'échec


@pytest.mark.parametrize("row", [
    {**_locked_row(), "position_revision": -1}, {**_locked_row(), "position_revision": True},
    {**_locked_row(), "position_revision": "7"}, {**_locked_row(), "quantity": None},
    {**_locked_row(), "quantity": b"\x00" * 4}, {**_locked_row(), "ticker": None}, {**_locked_row(), "envelope": 3},
    {**_locked_row(), "id": 43},
])
def test_impossible_locked_rows_are_unavailable_never_written(row):
    conn = _Conn()
    with pytest.raises(_adapters.AdapterInvariantBroken):
        _adapters._write_portfolio(conn, _context(), replace_command(42, 7, quantity="5"),
                                   _adapters.PositionFound(row), portfolio_keyring(), lambda point: None)
    assert conn.statements == []


def test_unexpected_lock_state_is_refused():
    for state in (_adapters.CreateReady(), _adapters.PositionMissing()):
        with pytest.raises(_adapters.AdapterInvariantBroken):
            _adapters._write_portfolio(_Conn(), _context(), create_command() if type(state) is not
                                       _adapters.CreateReady else replace_command(42), state, portfolio_keyring(),
                                       lambda point: None)


def test_legacy_writer_bypasses_the_revision_known_limit(env, alice):
    """Écriture historique (PUT /api/user/{id}/portfolio/{id} : UPDATE sans
    révision) : le CAS F2 ne peut pas la détecter. Limite documentée,
    bloquante pour l'activation publique."""
    target = env.insert_position(quantity=4.0, purchase_price=100.0, target_percent=10.0)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE portfolio_positions SET quantity = 9.0 WHERE id = :i"), {"i": target})
    outcome = run(env, alice, replace_command(target, 0, quantity="5"))
    assert outcome.status == AUTHORIZED  # fondée sur la révision 0, l'écriture intermédiaire est écrasée
    assert env.position(target)["q"] == 5.0


# --------------------------------------------------------------------------
# Idempotence durable
# --------------------------------------------------------------------------

def test_create_replay_returns_the_committed_bytes_without_any_write(env, alice):
    command = create_command(quantity="4.5", purchase_price="100.1", target_percent="20", envelope="CTO")
    first = run(env, alice, command)
    receipt_before = rows(env.observer, "SELECT xmin::text AS x FROM portfolio_write_idempotency")
    position_before = env.position(first.value.position_id)
    replay = run(env, alice, command)
    assert replay.status == AUTHORIZED and replay.value == first.value and replay.value.body == first.value.body
    assert env.count("portfolio_positions", "user_id = :u", u=USER_A) == 1
    assert rows(env.observer, "SELECT xmin::text AS x FROM portfolio_write_idempotency") == receipt_before
    assert env.position(first.value.position_id) == position_before


def test_update_replay_returns_the_recorded_snapshot_without_rewrite(env, alice):
    target = created(env, alice)
    command = replace_command(target, 0, quantity="5")
    first = run(env, alice, command)
    assert run(env, alice, replace_command(target, 1, quantity="6")).status == AUTHORIZED
    before = env.position(target)
    replay = run(env, alice, command)
    assert replay.value.body == first.value.body and replay.value.view.quantity == "5"
    assert env.position(target) == before  # révision 2, quantité 6 : rien n'est réécrit


def test_replays_after_deletion(env, alice):
    create = create_command()
    position_id = run(env, alice, create).value.position_id
    update = replace_command(position_id, 0, quantity="5")
    first_update = run(env, alice, update)
    delete = delete_command(position_id, 1)
    first_delete = run(env, alice, delete)
    assert env.position(position_id) is None
    # DELETE committé rejoué : 200, même corps, ligne déjà supprimée.
    assert run(env, alice, delete).value.body == first_delete.value.body
    # POST rejoué après suppression : ancien 201, aucune recréation.
    replay_create = run(env, alice, create)
    assert replay_create.value.status == 201 and replay_create.value.position_id == position_id
    assert env.count("portfolio_positions", "user_id = :u", u=USER_A) == 0
    # PUT ancien rejoué : son snapshot enregistré, aucune réécriture.
    assert run(env, alice, update).value.body == first_update.value.body
    assert env.position(position_id) is None
    # Nouvelle opération (nouvel UUID) sur la position disparue : 404.
    assert run(env, alice, replace_command(position_id, 1, quantity="7")) == AuthorizationOutcome(NOT_FOUND)
    assert run(env, alice, delete_command(position_id, 1)) == AuthorizationOutcome(NOT_FOUND)
    assert len(env.receipts()) == 3


@pytest.mark.parametrize("change", [
    {"quantity": "5"}, {"ticker": "MC.PA"}, {"purchase_price": "1"}, {"target_percent": "0"}, {"envelope": None},
])
def test_contradictory_create_replay_is_an_idempotency_conflict(env, alice, change):
    op = uuid.uuid4()
    first = run(env, alice, create_command(op))
    conflict = run(env, alice, create_command(op, **change))
    assert conflict == AuthorizationOutcome(IDEMPOTENCY_CONFLICT)
    assert env.count("portfolio_positions", "user_id = :u", u=USER_A) == 1 and len(env.receipts()) == 1
    assert run(env, alice, create_command(op)).value.body == first.value.body


def test_contradictory_update_replay_on_another_position_reveals_nothing(env, alice):
    a1, a2 = created(env, alice), created(env, alice)
    op = uuid.uuid4()
    assert run(env, alice, replace_command(a1, 0, op, quantity="5")).status == AUTHORIZED
    # Même UUID, autre cible (existante ou non) ou autre révision : conflit
    # d'idempotence avant tout examen de la nouvelle cible.
    for other in (replace_command(a2, 0, op, quantity="5"), replace_command(2**31 - 1, 0, op, quantity="5"),
                  replace_command(a1, 1, op, quantity="5")):
        assert run(env, alice, other) == AuthorizationOutcome(IDEMPOTENCY_CONFLICT)
    assert env.position(a2)["position_revision"] == 0


def test_same_uuid_for_another_type_or_account_is_another_key(env, alice):
    bob = env.confirmed(BOB, USER_B)
    op = uuid.uuid4()
    position_id = run(env, alice, create_command(op)).value.position_id
    # Même UUID, autre type : nouvelle clé, nouvelle exécution.
    assert run(env, alice, replace_command(position_id, 0, op, quantity="5")).status == AUTHORIZED
    assert run(env, alice, delete_command(position_id, 1, op)).status == AUTHORIZED
    # Même UUID chez B : son propre triplet, jamais le reçu de A.
    other = run(env, bob, create_command(op, ticker="MC.PA"))
    assert other.status == AUTHORIZED and other.value.position_id != position_id
    assert len(env.receipts(USER_A)) == 3 and len(env.receipts(USER_B)) == 1


def test_two_operation_ids_with_identical_content_create_two_positions(env, alice):
    first, second = run(env, alice, create_command()), run(env, alice, create_command())
    assert first.value.position_id != second.value.position_id
    assert env.count("portfolio_positions", "user_id = :u", u=USER_A) == 2


def _corrupt(env, sql, **params):
    with env.observer.begin() as conn:
        conn.execute(sa.text(f"UPDATE portfolio_write_idempotency SET {sql}"), params)


@pytest.mark.parametrize("sql", [
    "result_payload = result_payload || '{\"extra\": 1}'::jsonb",
    "result_payload = jsonb_set(result_payload, '{position,quantity}', '\"5\"')",
    "result_payload = jsonb_set(result_payload, '{position,quantity}', '4')",
    "result_payload = jsonb_set(result_payload, '{position,position_revision}', '\"1\"')",
    "result_payload = jsonb_set(result_payload, '{position_id}', '999')",
    "result_payload = jsonb_set(result_payload, '{position,ticker}', '\"MC.PA\"')",
    "result_payload = '[]'::jsonb",
    "result_version = 'portfolio-write-result-v9'",
    "fingerprint_version = 'oryx-portfolio-request-v9'",
    "result_status = 200",
    "position_id = position_id + 1",
])
def test_corrupted_or_unknown_receipts_are_unavailable_without_reexecution(env, alice, sql, caplog):
    command = create_command()
    run(env, alice, command)
    _corrupt(env, sql)
    before = (env.receipts(), env.count("portfolio_positions"))
    with caplog.at_level("DEBUG", logger="oryx.authz_f2"):
        assert run(env, alice, command) == AuthorizationOutcome(UNAVAILABLE)
    assert (env.receipts(), env.count("portfolio_positions")) == before
    assert any(r.levelname == "ERROR" and "alert=portfolio_" in r.getMessage() for r in caplog.records)


def test_receipt_key_rotation_and_retention(env, alice):
    command = create_command()
    first = run(env, alice, command, portfolio_keys=portfolio_keyring({"k1": K1}, "k1"))
    # Trousseau qui lit k1 et k2, courante k2 : le rejeu vérifie sous k1 (enregistrée).
    rotated = portfolio_keyring({"k1": K1, "k2": K2}, "k2")
    assert run(env, alice, command, portfolio_keys=rotated).value.body == first.value.body
    second = run(env, alice, create_command(), portfolio_keys=rotated)
    assert [r["fingerprint_key_version"] for r in env.receipts()] == ["k1", "k2"]
    # Redémarrage avec trousseau conservé (nouvel objet, mêmes clés).
    restarted = portfolio_keyring({"k2": bytes(K2), "k1": bytes(K1)}, "k2")
    assert run(env, alice, command, portfolio_keys=restarted).value.body == first.value.body
    # Ancienne clé retirée : 503 sans repli, aucune mutation ; k2 reste lisible.
    without_k1 = portfolio_keyring({"k2": K2}, "k2")
    before = env.count("portfolio_positions")
    assert run(env, alice, command, portfolio_keys=without_k1) == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("portfolio_positions") == before
    replay_second = run(env, alice, create_command(second.value.operation_id), portfolio_keys=without_k1)
    assert replay_second.value.body == second.value.body
    # Une autre clé sous le nom k1 (version réattribuée : configuration
    # interdite, non détectable) ne reconnaît pas l'intention.
    reassigned = portfolio_keyring({"k1": bytes([7]) * 32}, "k1")
    assert run(env, alice, command, portfolio_keys=reassigned).status == IDEMPOTENCY_CONFLICT


def test_missing_portfolio_keyring_is_unavailable_before_any_transaction(env, alice):
    outcome = run(env, alice, create_command(), portfolio_keys=None)
    assert outcome == AuthorizationOutcome(UNAVAILABLE) and env.hook_calls == []
    assert env.count("portfolio_positions") == 0
    assert run(env, alice, c.ListPositions(), portfolio_keys=None).status == AUTHORIZED  # lectures inchangées


@pytest.mark.parametrize("point", ["locked", "authorized", "position_written", "receipt_ready", "receipt_written",
                                   "executed"])
@pytest.mark.parametrize("kind", ["create", "replace", "delete"])
def test_failure_at_every_point_leaves_no_half_state(env, alice, point, kind):
    target = created(env, alice)
    before = (env.position(target), env.receipts(), env.count("portfolio_positions"))
    command = {"create": create_command(), "replace": replace_command(target, 0, quantity="5"),
               "delete": delete_command(target, 0)}[kind]

    def fail():
        raise RuntimeError(f"panne au point {point}")

    env.hooks[point] = fail
    assert run(env, alice, command) == AuthorizationOutcome(UNAVAILABLE)
    assert (env.position(target), env.receipts(), env.count("portfolio_positions")) == before
    env.hooks.clear()
    # Reprise avec la même intention : une seule exécution.
    assert run(env, alice, command).status == AUTHORIZED
    assert len(env.receipts()) == 2


def test_crash_after_receipt_before_commit_rolls_back(env, alice):
    env.hooks["receipt_written"] = lambda: (_ for _ in ()).throw(SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        run(env, alice, create_command())
    assert env.count("portfolio_positions") == 0 and env.receipts() == []
    assert env.engine.pool.checkedout() == 0


def test_failed_commit_writes_nothing(env, alice):
    def refuse_commit(conn):
        raise OSError("COMMIT refusé")

    sa.event.listen(env.engine, "commit", refuse_commit)
    try:
        outcome = run(env, alice, create_command())
    finally:
        sa.event.remove(env.engine, "commit", refuse_commit)
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("portfolio_positions") == 0 and env.receipts() == []


def test_uncertain_commit_is_unavailable_and_the_same_intention_finds_its_receipt(env, alice):
    """COMMIT effectué par le serveur, connexion perdue avant confirmation :
    503 (jamais « annulé »), aucune relance ; la même intention (même UUID)
    retrouve le reçu et rend le résultat committé, sans seconde création."""
    commits = []

    def commit_then_lose_connection(conn):
        if not commits and conn.info.get("receipt"):
            commits.append("commit")
            conn.connection.dbapi_connection.commit()
            raise OSError("connexion perdue après COMMIT")

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO portfolio_write_idempotency"):
            conn.info["receipt"] = True

    command = create_command()
    sa.event.listen(env.engine, "before_cursor_execute", mark)
    sa.event.listen(env.engine, "commit", commit_then_lose_connection)
    try:
        outcome = run(env, alice, command)
    finally:
        sa.event.remove(env.engine, "commit", commit_then_lose_connection)
        sa.event.remove(env.engine, "before_cursor_execute", mark)
        env.engine.dispose()
    assert commits == ["commit"] and outcome == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("portfolio_positions") == 1 and len(env.receipts()) == 1  # committé malgré le 503
    retry = run(env, alice, command)
    assert retry.status == AUTHORIZED and retry.value.status == 201
    assert env.count("portfolio_positions") == 1 and len(env.receipts()) == 1


def test_rolled_back_attempt_then_retry_executes_exactly_once(env, alice):
    command = create_command()
    env.hooks["executed"] = lambda: (_ for _ in ()).throw(RuntimeError("panne avant COMMIT"))
    assert run(env, alice, command) == AuthorizationOutcome(UNAVAILABLE)
    env.hooks.clear()
    first = run(env, alice, command)
    assert first.status == AUTHORIZED and run(env, alice, command).value == first.value
    assert env.count("portfolio_positions") == 1 and len(env.receipts()) == 1


# --------------------------------------------------------------------------
# Historique et représentation
# --------------------------------------------------------------------------

def test_mixed_list_is_ordered_and_diagnosed_without_any_write(env, alice):
    ids = [env.insert_position(quantity=4.0),
           env.insert_position(ticker="bad ticker", quantity=0.1 + 0.2, envelope="AV"),
           env.insert_position(quantity=5e-324, purchase_price=-0.0, target_percent=float("inf")),
           env.insert_position(quantity=1.7976931348623157e308, purchase_price=float("-inf"))]
    before = [env.position(i) for i in ids]
    outcome = run(env, alice, c.ListPositions())
    views = outcome.value
    assert [view.position_id for view in views] == sorted(ids)
    assert [view.data_status for view in views] == ["valid", "legacy_invalid", "legacy_invalid", "legacy_invalid"]
    assert views[1].validation_issues == ("ticker_invalid", "quantity_invalid", "envelope_invalid")
    assert views[1].quantity == "0.30000000000000004"
    assert views[2].quantity == "0." + "0" * 323 + "5" and views[2].purchase_price == "0"
    assert views[2].target_percent == "Infinity" and views[3].purchase_price == "-Infinity"
    assert views[2].validation_issues == ("quantity_invalid", "target_percent_invalid")
    assert [env.position(i) for i in ids] == before


@pytest.mark.parametrize("digits", [-15, 0, 1, 3])
def test_reading_is_independent_of_extra_float_digits(env, alice, pg_url, digits):  # noqa: F811
    target = env.insert_position(quantity=0.1 + 0.2, purchase_price=100.1, target_percent=1 / 3)
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool,
                              connect_args={"options": f"-c extra_float_digits={digits}"})
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SHOW extra_float_digits")).scalar_one() == str(digits)
        view = run(env, alice, c.ReadPosition(target), engine=engine).value
        written = run(env, alice, replace_command(target, 0, quantity="0.1", purchase_price="100.1",
                                                  target_percent="20"), engine=engine).value.view
    finally:
        engine.dispose()
    assert (view.quantity, view.purchase_price, view.target_percent) == (
        "0.30000000000000004", "100.1", "0.3333333333333333")
    row = env.position(target)
    assert (row["q"], row["p"], row["tp"]) == (0.1, 100.1, 20.0)
    assert (written.quantity, written.purchase_price, written.target_percent) == ("0.1", "100.1", "20")


@pytest.mark.parametrize("text", ["0.000001", "999999999999.5", "123456789.123456", "1000000000000", "100.1",
                                  "0.1", "12345678901.2345"])
def test_stored_values_render_back_identically(env, alice, text):
    position_id = created(env, alice, quantity=text, purchase_price=text, target_percent=None)
    view = run(env, alice, c.ReadPosition(position_id)).value
    assert view.quantity == view.purchase_price == text
    assert pv.render_binary64_decimal_v1(env.position(position_id)["q"]) == text


def test_reads_write_nothing(env, alice):
    target = created(env, alice)
    before = (env.position(target), env.receipts())
    for command in (c.ListPositions(), c.ReadPosition(target), c.ReadPosition(2**31 - 1)):
        run(env, alice, command)
    assert (env.position(target), env.receipts()) == before


# --------------------------------------------------------------------------
# Autorisation, propriété, isolation
# --------------------------------------------------------------------------

def test_invalid_session_wins_over_every_business_outcome(env, alice):
    target = created(env, alice)
    command = replace_command(target, 0, quantity="5")
    run(env, alice, command)
    env.service.revoke_session(alice.session_id, "logout")
    for attempt in (command,                                   # rejeu
                    replace_command(target, 7, quantity="6"),  # conflit
                    replace_command(2**31 - 1, 0),             # absente
                    create_command(), delete_command(target, 1)):
        assert run(env, alice, attempt) == AuthorizationOutcome(SESSION_INVALID)
    assert env.position(target)["position_revision"] == 1 and len(env.receipts()) == 2


def test_pending_session_cannot_replay_with_a_known_operation_id(env):
    confirmed = env.confirmed()
    command = create_command()
    run(env, confirmed, command)
    pending = env.sign_in("alice2@example.com", USER_A)
    assert run(env, pending, command) == AuthorizationOutcome(SESSION_INVALID)


def test_foreign_positions_are_indistinguishable_and_never_locked(env, alice):
    bob = env.confirmed(BOB, USER_B)
    foreign = created(env, bob)
    before = env.position(foreign)
    with env.observer.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(sa.text("SELECT id FROM portfolio_positions WHERE id = :i FOR UPDATE"), {"i": foreign})
        outcomes = [run(env, alice, cmd) for cmd in (replace_command(foreign, 0), delete_command(foreign, 0),
                                                     c.ReadPosition(foreign))]
        tx.rollback()
    missing = [run(env, alice, cmd) for cmd in (replace_command(2**31 - 1, 0), delete_command(2**31 - 1, 0),
                                                c.ReadPosition(2**31 - 1))]
    assert outcomes == missing == [AuthorizationOutcome(NOT_FOUND)] * 3
    assert env.position(foreign) == before
    assert [p.position_id for p in run(env, alice, c.ListPositions()).value] == []


def test_receipt_of_another_account_is_never_consulted(env, alice):
    bob = env.confirmed(BOB, USER_B)
    op = uuid.uuid4()
    bob_result = run(env, bob, create_command(op, ticker="MC.PA"))
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, dict(parameters or {})))

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        mine = run(env, alice, create_command(op))
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    assert mine.status == AUTHORIZED and mine.value.body != bob_result.value.body
    receipt_reads = [p for s, p in statements if "FROM portfolio_write_idempotency" in s]
    assert receipt_reads and all(p["u"] == USER_A for p in receipt_reads)
    assert all(USER_B not in json.dumps(p, default=str) for _, p in statements)


def test_isolation_other_than_read_committed_is_refused(env, alice, pg_url):  # noqa: F811
    for level in ("REPEATABLE READ", "SERIALIZABLE"):
        engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, isolation_level=level)
        try:
            outcome = run(env, alice, create_command(), engine=engine)
            read = run(env, alice, c.ListPositions(), engine=engine)
        finally:
            engine.dispose()
        assert outcome == AuthorizationOutcome(UNAVAILABLE)
        assert read.status == AUTHORIZED  # plan READ inchangé
    assert env.count("portfolio_positions") == 0 and env.receipts() == []


def test_portfolio_lock_order_and_t_authorization(env, alice):
    target = created(env, alice)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        assert run(env, alice, replace_command(target, 0, quantity="5")).status == AUTHORIZED
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    phase_b = statements[statements.index("SELECT current_setting('transaction_isolation')"):]
    markers = ["SELECT current_setting('transaction_isolation')",
               "SELECT id FROM users WHERE id = %(u)s FOR UPDATE",
               "SELECT * FROM oryx_sessions WHERE id = %(s)s FOR SHARE",
               "SELECT * FROM oryx_session_generations WHERE id = %(g)s FOR SHARE"]
    positions = [phase_b.index(m) for m in markers]
    receipt = next(i for i, s in enumerate(phase_b) if "FROM portfolio_write_idempotency" in s)
    position = next(i for i, s in enumerate(phase_b) if s.startswith("SELECT id, position_revision")
                    and s.endswith("FOR UPDATE"))
    clock = phase_b.index("SELECT clock_timestamp()")
    update = next(i for i, s in enumerate(phase_b) if s.startswith("UPDATE portfolio_positions"))
    insert = next(i for i, s in enumerate(phase_b) if s.startswith("INSERT INTO portfolio_write_idempotency"))
    assert positions == sorted(positions) and positions[-1] < receipt < position < clock < update < insert
    start = statements.index("SELECT current_setting('transaction_isolation')")
    assert [s.split(" =")[0] for s in statements[start - 2:start]] == ["SET LOCAL lock_timeout",
                                                                        "SET LOCAL statement_timeout"]


def test_create_locks_no_position(env, alice):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        assert run(env, alice, create_command()).status == AUTHORIZED
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    assert not any("FROM portfolio_positions" in s and "FOR UPDATE" in s for s in statements)


def test_portfolio_lock_timeout_is_unavailable(env, alice):
    import time
    target = created(env, alice)
    before = env.position(target)
    with env.observer.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(sa.text("SELECT id FROM portfolio_positions WHERE id = :i FOR UPDATE"), {"i": target})
        started = time.monotonic()
        outcome = run(env, alice, replace_command(target, 0, quantity="5"))
        elapsed = time.monotonic() - started
        tx.rollback()
    assert outcome == AuthorizationOutcome(UNAVAILABLE)
    assert 4.5 <= elapsed < 9  # lock_timeout = 5 s
    assert env.position(target) == before and len(env.receipts()) == 1


def test_missing_profile_with_valid_session_is_unavailable(env, alice):
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": USER_A})
    for command in (create_command(), replace_command(1), delete_command(1)):
        assert run(env, alice, command) == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("users", "id = :u", u=USER_A) == 0 and env.count("portfolio_positions") == 0


def test_no_pedagogical_data_is_created(env, alice):
    tables = ("cognitive_events", "observation_events", "competency_inference_runs", "competency_stage_claims",
              "analysis_sessions", "investment_theses")
    existing = [t for t in tables if scalar(env.observer, "SELECT to_regclass(:t) IS NOT NULL", t=t)]
    before = {t: env.count(t) for t in existing}
    target = created(env, alice)
    run(env, alice, replace_command(target, 0, quantity="5"))
    run(env, alice, delete_command(target, 1))
    assert {t: env.count(t) for t in existing} == before and existing
