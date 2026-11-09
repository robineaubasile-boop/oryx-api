"""ACT-L1-PR1 — identité e-mail liée et vérifiée exigée par le runner F2 pour
toute commande personnelle, contre un VRAI PostgreSQL (16 et 18 en CI), sous
le VRAI runner, le registre de PRODUCTION (vingt commandes), le VRAI moteur
F2 (invitation, liaison e-mail OTP, connexion OTP ; doubles Supabase / Resend
sans réseau) et la VRAIE application personnelle isolée (transports EODHD /
FMP / Claude simulés). Chaque état durable est observé par une connexion
indépendante ; « aucune écriture » = lignes ET xmin inchangés.

1. statut fermé et règle d'admissibilité ;
2. invitation sans liaison : refus des vingt commandes, sans verrou métier,
   sans écriture ; liaison OTP puis même profil admis ; connexion OTP
   ultérieure sans création ni fusion ; liaison au profil d'autrui ;
3. priorités : session invalide (401) et profil absent (503) inchangés ;
4. identité non vérifiée, vérifiée puis expurgée, sans preuve ;
5. concurrence liaison / runner dans les deux ordres, pour chaque plan ;
6. HTTP : 403 identity_link_required sur toutes les routes personnelles,
   parcours de liaison HTTP encore fonctionnel, puis accès ;
7. Décrypter : B1, réautorisation et lectures de contexte B2 (aucun
   fournisseur après le refus), C1-B, replay et ACK C2 ;
8. dormance : aucune route F2 sur api:app.

Les états « vérifiée sans adresse ni expurgation » et « vérification retirée
après coup » ne sont jamais écrits par F2 : ils sont FORCÉS ici (données de
test explicites) pour éprouver le refus fermé et les lectures postérieures à
une première autorisation.
"""
import json
import re
import threading
import uuid

import pytest
import sqlalchemy as sa

from core.auth_f2.service import F2AuthService
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2 import runner as runner_module
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import (AUTHORIZED, IDENTITY_LINK_REQUIRED, SESSION_INVALID, UNAVAILABLE,
                                  AuthorizationOutcome)
from core.authz_f2_http import decryptage as c3
from core.authz_f2_http import routes as personal_routes
from tests.authz_f2_pr9a_support import BOB_EMAIL, USER_A, USER_B
from tests.authz_f2_pr9b1_support import reserve_command
from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock, FakeEodhd
from tests.authz_f2_pr9c1b_support import next_turn, schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c3_support import ACK, REPLAY, TURNS, HttpEnv, body_of, c3_headers, identity_fields, post, \
    turn_fields
from tests.authz_f2_support import (ALICE, IDENTITY_LOCK_SQL, ME, ME_LEVEL, PORTFOLIO, PersonalBrowser,
                                    active_cookie, compose_test_app, create_command, delete_command, level_body,
                                    level_headers, me_headers, me_request, portfolio_headers, position_body,
                                    replace_command)
from tests.f2_pr4_support import activate, link_start, link_verify
from tests.f2_support import FakeMailer, FakeSupabase
from tests.test_authz_f2_concurrency import CONCURRENT_APP, Background, Pause, wait_until, waiting
from tests.test_authz_f2_http import NO_STORE
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CAROL = "carol@example.com"
OVERVIEW = "/api/v2/me/analysis-overview"
THESES = "/api/v2/me/theses"
IDENTITY_BODY = {"error": "identity_link_required"}


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = HttpEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()  # connexion OTP : identité liée et vérifiée


@pytest.fixture
def invited(env):
    return env.invited()  # activation d'invitation : AUCUNE identité


def refused(outcome):
    """Refus fermé exact : ni valeur, ni principal."""
    return outcome == AuthorizationOutcome(IDENTITY_LINK_REQUIRED) and outcome.value is None \
        and outcome.principal is None


def durable_state(env):
    state = env.full_state()
    for table, key in (("portfolio_positions", "id"), ("portfolio_write_idempotency", "operation_id"),
                       ("auth_identities", "id"), ("oryx_sessions", "id"), ("auth_operations", "id")):
        state[table] = env.rows(f"SELECT *, xmin::text AS xmin FROM {table} ORDER BY {key}")
    return state


def degrade_identity(env, user_id=USER_A):
    """Vérification retirée APRÈS COUP (état forcé : F2 ne l'écrit jamais)."""
    env.update("auth_identities", "verified_email_normalized = NULL, email_verified_at = NULL", "user_id = :u",
               u=user_id)


def restore_identity(env, user_id=USER_A, email=ALICE):
    env.update("auth_identities", "verified_email_normalized = :e, email_verified_at = now()", "user_id = :u",
               u=user_id, e=email)


def assert_identity_refusal(response):
    assert response.status_code == 403, (response.status_code, response.content[:300])
    assert response.json() == IDENTITY_BODY
    assert response.content == b'{"error":"identity_link_required"}'
    headers = response.headers
    for name, value in NO_STORE.items():
        assert headers.get_list(name) == [value], name
    assert headers.get_list("set-cookie") == [] and headers.get_list("access-control-allow-origin") == []


# --------------------------------------------------------------------------
# 1. Statut fermé et règle d'admissibilité
# --------------------------------------------------------------------------

def test_identity_link_required_is_a_seventh_distinct_closed_status():
    assert IDENTITY_LINK_REQUIRED == "identity_link_required"
    assert runner_module.STATUSES[-1] == IDENTITY_LINK_REQUIRED and len(set(runner_module.STATUSES)) == 7
    for other in (SESSION_INVALID, runner_module.NOT_FOUND, UNAVAILABLE, runner_module.REVISION_CONFLICT,
                  runner_module.IDEMPOTENCY_CONFLICT):
        assert IDENTITY_LINK_REQUIRED != other
    assert (personal_routes.IDENTITY_LINK_HTTP_STATUS, personal_routes.IDENTITY_LINK_CODE) == (403,
                                                                                            "identity_link_required")


def test_the_identity_statement_reads_no_personal_data_and_is_owner_scoped():
    sql = _adapters._LOCK_LINKED_IDENTITIES
    assert sql.startswith("SELECT id, provider, email_verified_at, ") and sql.endswith(
        "FROM auth_identities WHERE user_id = :u ORDER BY id FOR KEY SHARE")
    projected = sql[len("SELECT "):sql.index(" FROM ")]
    # Ni l'adresse, ni l'issuer, ni le subject : seulement leur présence / expurgation.
    assert "verified_email_normalized IS NOT NULL AS email_present" in projected
    assert "pii_redacted_at IS NOT NULL AS pii_redacted" in projected
    for column in ("issuer", "subject", "last_authenticated_at", "created_at"):
        assert column not in projected
    assert projected.count("verified_email_normalized") == 1 and sql.count(":u") == 1
    assert _adapters.LINKED_IDENTITY_PROVIDERS == ("supabase",)


# --------------------------------------------------------------------------
# 2. Invitation sans liaison, liaison OTP, connexion OTP ultérieure
# --------------------------------------------------------------------------

def every_registered_command(env, alice):
    """Une instance de CHAQUE commande du registre de production ; les
    capacités Décrypter (bail, candidat, livraison finalisée) appartiennent à
    alice : un refus ne doit ni les lire ni les consommer."""
    position = env.insert_position(USER_A)
    finalized = env.finalized(alice)
    command, lease, candidate = env.generate(alice, next_turn("conv-act-l1-b"))
    commands = [
        c.ReadProfile(), c.ListPositions(), c.ReadPosition(position), c.ListTheses(), c.ListThesesForTicker("AIR.PA"),
        c.ReadDelivery(uuid.uuid4()), c.ListConversationDeliveries("conv-act-l1-b"), c.UpdateProfileLevel("avance", 0),
        create_command(), replace_command(position), delete_command(position),
        c.ListAnalysisOverviewPage(10, None), c.ListThesesPage(10, None, None, None), c.ReadThesis(1),
        reserve_command(), c.RecordDecryptageAttemptFailure.from_lease(lease, dr.GENERATION_UNAVAILABLE),
        c.ReadDecryptagePreparationContext.from_lease(lease, command, "MC.PA"),
        c.FinalizeDecryptageCandidate(candidate), finalized.replay(), finalized.ack()]
    assert {type(command) for command in commands} == set(_adapters.REGISTRY) and len(commands) == 20
    return commands


def test_an_unlinked_invitation_session_is_refused_for_all_twenty_commands(env, alice, invited):
    owner = env.owner(invited)
    assert env.identities(owner) == []
    commands = every_registered_command(env, alice)
    before = durable_state(env)
    providers = env.provider_calls()
    for command in commands:
        env.hook_calls.clear()
        outcome = env.run(invited, command)
        assert refused(outcome), (type(command).__name__, outcome)
        # Refus à T_authorization, AVANT toute exécution : jamais « authorized ».
        assert env.hook_calls == ["prevalidated", "locked"], type(command).__name__
    assert durable_state(env) == before  # aucune écriture, aucune ressource d'alice consommée
    assert env.provider_calls() == providers  # aucun fournisseur
    # La session invitée reste valide (aucune révocation) et alice est inchangée.
    assert env.service.authenticate_session(invited.credential()) is not None
    assert env.run(alice, c.ReadProfile()).status == AUTHORIZED


def test_an_invitation_email_is_never_a_proof_of_link(env):
    invited = env.invited(recipient_email=CAROL)
    owner = env.owner(invited)
    assert env.count("invitations", "recipient_email_normalized = :e", e=CAROL) == 1
    assert env.identities(owner) == []
    assert refused(env.run(invited, c.ReadProfile()))


def test_the_refusal_takes_no_business_lock_and_decides_after_the_identity_rank(env, invited):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        outcome = env.run(invited, replace_command(1))
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    assert refused(outcome)
    phase_b = statements[statements.index("SELECT current_setting('transaction_isolation')"):]
    markers = ["SELECT id FROM users WHERE id = %(u)s FOR UPDATE",
               "SELECT * FROM oryx_sessions WHERE id = %(s)s FOR SHARE",
               "SELECT * FROM oryx_session_generations WHERE id = %(g)s FOR SHARE",
               IDENTITY_LOCK_SQL, "SELECT clock_timestamp()"]
    positions = [phase_b.index(m) for m in markers]
    assert positions == sorted(positions)
    assert not any("portfolio_" in s or s.startswith(("INSERT", "UPDATE", "DELETE")) for s in phase_b)


def test_a_linked_session_uses_the_identity_rank_before_business_locks(env, alice):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    try:
        assert env.run(alice, create_command()).status == AUTHORIZED
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", record)
    identity = statements.index(IDENTITY_LOCK_SQL)
    assert statements.index("SELECT * FROM oryx_session_generations WHERE id = %(g)s FOR SHARE") < identity
    receipt = next(i for i, s in enumerate(statements) if "FROM portfolio_write_idempotency" in s)
    assert identity < receipt < statements.index("SELECT clock_timestamp()", identity)


def test_otp_linking_admits_the_same_profile_and_a_later_sign_in_finds_it(env, invited):
    owner = env.owner(invited)
    users_before = env.count("users")
    assert refused(env.run(invited, c.ReadProfile()))
    # Le parcours de liaison reste ouvert à la session d'invitation non liée.
    linked = env.link_email(invited, CAROL)
    assert (linked.status, linked.reason) == ("email_linked", "new_identity")
    (identity,) = env.identities(owner)
    assert identity["verified_email_normalized"] == CAROL and identity["email_verified_at"] is not None
    # Même session, même profil : désormais admissible (auth_method inchangé).
    outcome = env.run(invited, c.ReadProfile())
    assert outcome.status == AUTHORIZED and outcome.principal.user_id == owner == outcome.value.user_id
    assert outcome.principal.auth_method == "invitation_activation"
    created = env.run(invited, create_command(ticker="TTE.PA"))
    assert created.status == AUTHORIZED and created.principal.user_id == owner
    # Connexion OTP ultérieure : même profil, aucune création, aucune fusion.
    env.age_otp_requests()  # intervalle anti-abus entre deux envois OTP écoulé
    later = env.sign_in(CAROL, user_id="must-not-be-used", profile=False)
    assert later.user_id == owner and env.count("users") == users_before
    assert env.service.authenticate_session(later.credential()) is not None
    via_sign_in = env.run(later, c.ListPositions())
    assert via_sign_in.status == AUTHORIZED and via_sign_in.principal.user_id == owner
    assert [view.ticker for view in via_sign_in.value] == ["TTE.PA"]
    (after,) = env.identities(owner)  # même identité : seul last_authenticated_at avance
    assert {k: v for k, v in after.items() if k not in ("last_authenticated_at", "xmin")} == {
        k: v for k, v in identity.items() if k not in ("last_authenticated_at", "xmin")}
    assert after["last_authenticated_at"] is not None
    assert env.count("auth_identities") == 1 and env.count("users", "id = 'must-not-be-used'") == 0


def test_linking_an_identity_owned_by_another_profile_transfers_nothing(env, alice, invited):
    owner = env.owner(invited)
    alice_identity = env.identities(USER_A)
    before = env.profile(USER_A)
    env.age_otp_requests()  # alice vient de recevoir un OTP de connexion
    outcome = env.link_email(invited, ALICE)
    assert (outcome.status, outcome.reason) == ("rejected", "conflict_other_profile")
    assert env.identities(USER_A) == alice_identity and env.identities(owner) == []
    assert refused(env.run(invited, c.ReadProfile()))
    assert refused(env.run(invited, create_command()))
    assert env.count("portfolio_positions", "user_id = :u", u=owner) == 0
    alice_view = env.run(alice, c.ReadProfile())
    assert alice_view.status == AUTHORIZED and alice_view.principal.user_id == USER_A
    assert env.profile(USER_A) == before


def test_each_user_is_judged_on_their_own_identities(env, alice, invited):
    """Multi-utilisateurs : la liaison de l'un ne profite jamais à l'autre."""
    other = env.invited()
    assert env.link_email(other, CAROL).status == "email_linked"
    assert env.run(other, c.ReadProfile()).status == AUTHORIZED
    assert refused(env.run(invited, c.ReadProfile()))
    assert env.run(alice, c.ReadProfile()).principal.user_id == USER_A


# --------------------------------------------------------------------------
# 3. Priorités existantes conservées
# --------------------------------------------------------------------------

def test_an_invalid_session_keeps_session_invalid_whatever_the_identity(env, alice):
    unlinked, pending = env.invited(), env.invited(confirm=False)
    expired_unlinked = env.invited()
    env.expire_at(expired_unlinked.session_id, "clock_timestamp() - interval '1 second'")
    env.service.revoke_session(unlinked.session_id, "logout")
    env.service.revoke_session(alice.session_id, "security")
    for issued in (unlinked, pending, expired_unlinked, alice):
        for command in (c.ReadProfile(), create_command(), reserve_command()):
            assert env.run(issued, command) == AuthorizationOutcome(SESSION_INVALID)
    # Révocation PENDANT l'attente (entre phase A et phase B) : toujours 401.
    late = env.invited()
    env.hooks["prevalidated"] = lambda: env.service.revoke_session(late.session_id, "security")
    assert env.run(late, c.ReadProfile()) == AuthorizationOutcome(SESSION_INVALID)


def test_a_missing_profile_stays_unavailable_and_is_never_created(env, invited):
    owner = env.owner(invited)
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": owner})
    for command in (c.ReadProfile(), create_command(), reserve_command()):
        assert env.run(invited, command) == AuthorizationOutcome(UNAVAILABLE)
    assert env.count("users", "id = :u", u=owner) == 0


def test_a_profile_mismatch_stays_unavailable_before_the_identity_rule(env, alice):
    """Session déplacée vers un autre profil entre les phases A et B (état
    forcé) : 503 prioritaire, alors que le candidat n'a AUCUNE identité."""
    unlinked = env.invited()
    owner = env.owner(unlinked)
    env.hooks["prevalidated"] = lambda: env.damage("UPDATE oryx_sessions SET user_id = :u WHERE id = :s",
                                                   u=USER_A, s=unlinked.session_id)
    assert env.run(unlinked, c.ReadProfile()) == AuthorizationOutcome(UNAVAILABLE)
    assert env.identities(owner) == []


# --------------------------------------------------------------------------
# 4. Admissibilité de la ligne d'identité
# --------------------------------------------------------------------------

@pytest.mark.parametrize("verified, redacted, admitted", [
    (False, False, False),  # ligne sans vérification e-mail : jamais une preuve
    (False, True, False),   # expurgée mais jamais vérifiée
    (True, True, True),     # vérifiée puis expurgée : preuve durable conservée
    (True, False, True),
])
def test_identity_rows_are_admitted_only_with_a_durable_verification(env, invited, verified, redacted, admitted):
    owner = env.owner(invited)
    env.force_identity(owner, verified=verified, redacted=redacted)
    outcome = env.run(invited, c.ReadProfile())
    if admitted:
        assert outcome.status == AUTHORIZED and outcome.principal.user_id == owner
    else:
        assert refused(outcome)


def test_a_verified_timestamp_without_address_nor_redaction_proves_nothing(env, invited):
    owner = env.owner(invited)
    identity = env.force_identity(owner)
    env.update("auth_identities", "verified_email_normalized = NULL", "id = :i", i=identity)
    assert refused(env.run(invited, c.ReadProfile()))


def test_redacting_a_linked_identity_keeps_access(env, invited):
    owner = env.owner(invited)
    assert env.link_email(invited, CAROL).status == "email_linked"
    env.update("auth_identities", "verified_email_normalized = NULL, pii_redacted_at = now()", "user_id = :u",
               u=owner)
    (identity,) = env.identities(owner)
    assert identity["verified_email_normalized"] is None and identity["email_verified_at"] is not None
    assert env.run(invited, c.ReadProfile()).status == AUTHORIZED


def test_a_sign_in_session_is_not_admitted_on_its_auth_method_alone(env, alice):
    """Session de connexion OTP (auth_method supabase_email_sign_in,
    auth_identity_id renseigné) : la décision relit auth_identities."""
    degrade_identity(env)
    assert refused(env.run(alice, c.ReadProfile()))
    restore_identity(env)
    assert env.run(alice, c.ReadProfile()).status == AUTHORIZED


# Clarification ISSUER (audit sur d6de88f) : le runner vérifie le fournisseur
# (liste fermée) sans comparer l'issuer ; l'issuer est imposé par la SEULE
# écriture applicative d'identités, la liaison F2, qui enregistre l'issuer
# configuré du fournisseur Supabase du moteur F2.

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


def test_identities_are_written_only_by_the_f2_linking_with_the_configured_issuer():
    writers = []
    for path in REPO.rglob("*.py"):
        relative = path.relative_to(REPO)
        if relative.parts[0] in ("tests", ".git") or "__pycache__" in relative.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in ("INSERT INTO auth_identities", "UPDATE auth_identities", "DELETE FROM auth_identities"):
            writers += [(str(relative), pattern)] * text.count(pattern)
        # Construction ORM (hors définition du modèle core/models.py).
        writers += [(str(relative), "AuthIdentity(")] * len(re.findall(r"(?<!class )\bAuthIdentity\(", text))
    # Une seule insertion (liaison), aucune suppression ni ORM ; mises à jour
    # par _update (colonnes listées dans le service, jamais issuer ni subject).
    assert writers == [("core/auth_f2/service.py", "INSERT INTO auth_identities")]
    service = (REPO / "core" / "auth_f2" / "service.py").read_text(encoding="utf-8")
    prepare = service[service.index("def _prepare_linked_identity"):service.index("def _commit_linking")]
    assert 'params = {"p": PROVIDER, "i": self._supabase.issuer, "s": subject}' in prepare
    updates = [line.strip() for line in service.splitlines() if '_update(conn, "auth_identities"' in line]
    assert updates == ['_update(conn, "auth_identities", identity["id"], last_authenticated_at=at)',
                       '_update(conn, "auth_identities", identity["id"], created_at=at, email_verified_at=at)']


def test_otp_linking_records_the_configured_provider_issuer_and_proven_subject(env, invited):
    owner = env.owner(invited)
    assert env.link_email(invited, CAROL).status == "email_linked"
    (identity,) = env.identities(owner)
    assert (identity["provider"], identity["issuer"], identity["subject"]) == (
        "supabase", env.supabase.issuer, env.supabase.subject_for(CAROL))
    assert identity["email_verified_at"] is not None and identity["pii_redacted_at"] is None


def test_the_runner_checks_the_provider_but_not_the_issuer_current_behaviour(env, invited):
    """État FORCÉ hors de tout chemin applicatif (écriture SQL directe) :
    identité vérifiée d'un autre issuer https -> admise par le runner, qui ne
    reçoit aucune configuration Supabase. Comportement actuel documenté
    (docs/act_l1_pr1.md § 16.2), pas une garantie supplémentaire."""
    owner = env.owner(invited)
    env.force_identity(owner)
    env.update("auth_identities", "issuer = 'https://other-project.supabase.co/auth/v1'", "user_id = :u", u=owner)
    assert env.run(invited, c.ReadProfile()).status == AUTHORIZED
    with pytest.raises(sa.exc.IntegrityError):  # fournisseur hors liste : refusé dès PostgreSQL
        env.update("auth_identities", "provider = 'other'", "user_id = :u", u=owner)


# --------------------------------------------------------------------------
# 5. Concurrence : liaison e-mail et runner, dans les deux ordres
# --------------------------------------------------------------------------

PLAN_COMMANDS = {"read": c.ReadProfile, "profile_mutation": lambda: c.UpdateProfileLevel("avance", 0),
                 "portfolio_mutation": create_command, "decryptage_mutation": reserve_command}


def concurrent_linking(env, pg_url, hook=None):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    return engine, F2AuthService(engine, env.keyring, supabase, mailer, fault_hook=hook), mailer


@pytest.mark.parametrize("plan", sorted(PLAN_COMMANDS))
def test_a_linking_committed_while_the_runner_waits_is_seen_at_t_authorization(env, pg_url, invited,  # noqa: F811
                                                                               plan):
    paused, resume = threading.Event(), threading.Event()

    def f2_hook(point):
        if point == "verify.finalizing":  # users FOR UPDATE détenu, identité insérée (non committée)
            paused.set()
            assert resume.wait(20)

    engine, f2, mailer = concurrent_linking(env, pg_url, f2_hook)
    try:
        start = f2.start_email_linking(session=invited.credential(), email=CAROL, client_ip="203.0.113.81")
        assert start.status == "issued"
        verify = Background(lambda: f2.verify_email_linking(operation_id=start.operation_id,
                                                            session=invited.credential(),
                                                            code=mailer.last_code(CAROL), client_ip="203.0.113.81"))
        verify.start()
        assert paused.wait(10)
        command = PLAN_COMMANDS[plan]()
        runner = Background(lambda: env.run(invited, command, hook=lambda point: None))
        runner.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="le runner n'attend pas la liaison au rang users")
        resume.set()
        assert verify.outcome().status == "email_linked"
        outcome = runner.outcome()
        assert outcome.status == AUTHORIZED and outcome.principal.user_id == env.owner(invited)
    finally:
        resume.set()
        engine.dispose()


@pytest.mark.parametrize("plan", sorted(PLAN_COMMANDS))
def test_a_linking_started_after_the_runner_locks_waits_and_the_command_is_refused(env, pg_url, invited,  # noqa: F811
                                                                                   plan):
    engine, f2, mailer = concurrent_linking(env, pg_url)
    owner = env.owner(invited)
    try:
        start = f2.start_email_linking(session=invited.credential(), email=CAROL, client_ip="203.0.113.82")
        assert start.status == "issued"
        before = durable_state(env)
        pause = Pause("locked")  # tous les verrous de la décision pris, T_authorization pas encore lue
        runner = Background(lambda: env.run(invited, PLAN_COMMANDS[plan](), hook=pause))
        runner.start()
        assert pause.reached.wait(10)
        verify = Background(lambda: f2.verify_email_linking(operation_id=start.operation_id,
                                                            session=invited.credential(),
                                                            code=mailer.last_code(CAROL), client_ip="203.0.113.82"))
        verify.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la liaison n'attend pas le runner")
        pause.release.set()
        assert refused(runner.outcome())
        assert verify.outcome().status == "email_linked"
        after = durable_state(env)
        for table in ("portfolio_positions", "portfolio_write_idempotency", "decryptage_turn_reservations",
                      "conversation_identities", "users"):
            assert after[table] == before[table], table  # le refus n'a rien écrit
        assert len(env.identities(owner)) == 1
        # La commande suivante voit la liaison committée.
        assert env.run(invited, PLAN_COMMANDS[plan]()).status == AUTHORIZED
    finally:
        pause.release.set()
        engine.dispose()


def test_a_sign_in_finalization_never_waits_on_the_identity_rank(env, pg_url, alice):  # noqa: F811
    """FOR KEY SHARE (runner) et FOR NO KEY UPDATE (connexion OTP sur la même
    identité) sont compatibles : aucune attente nouvelle, aucun interblocage."""
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    supabase.subjects.update(env.supabase.subjects)
    f2 = F2AuthService(engine, env.keyring, supabase, mailer)
    pause = Pause("locked")
    env.age_otp_requests()  # alice vient de recevoir un OTP de connexion
    try:
        start = f2.start_sign_in(email=ALICE, client_ip="203.0.113.83")
        assert start.status == "issued"
        holder = Background(lambda: env.run(alice, c.ReadProfile(), hook=pause))
        holder.start()
        assert pause.reached.wait(10)
        signed = f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                   code=mailer.last_code(ALICE), client_ip="203.0.113.83")
        assert signed.status == "authenticated" and signed.session.user_id == USER_A
        pause.release.set()
        assert holder.outcome().status == AUTHORIZED
    finally:
        pause.release.set()
        engine.dispose()


# --------------------------------------------------------------------------
# 6. HTTP : toutes les routes personnelles
# --------------------------------------------------------------------------

def personal_requests(issued):
    """(méthode, gabarit de route, chemin, en-têtes, corps) de CHAQUE route personnelle."""
    get = me_headers(issued.generation_id, active_cookie(issued))
    op = uuid.uuid4()
    return [
        ("GET", ME, ME, get, None),
        ("PUT", ME_LEVEL, ME_LEVEL, level_headers(issued), level_body("avance", "0")),
        ("GET", PORTFOLIO, PORTFOLIO, get, None),
        ("GET", PORTFOLIO + "/{position_id}", PORTFOLIO + "/1", get, None),
        ("POST", PORTFOLIO, PORTFOLIO, portfolio_headers(issued, operation_id=op), position_body()),
        ("PUT", PORTFOLIO + "/{position_id}", PORTFOLIO + "/1",
         portfolio_headers(issued, operation_id=uuid.uuid4(), expected_revision=0), position_body()),
        ("DELETE", PORTFOLIO + "/{position_id}", PORTFOLIO + "/1",
         portfolio_headers(issued, operation_id=uuid.uuid4(), expected_revision=0, content_type=None), None),
        ("GET", OVERVIEW, OVERVIEW, get, None),
        ("GET", THESES, THESES, get, None),
        ("GET", THESES + "/{thesis_id}", THESES + "/1", get, None),
        ("POST", TURNS, TURNS, c3_headers(issued), body_of(turn_fields())),
        ("POST", REPLAY, REPLAY, c3_headers(issued), body_of(identity_fields(uuid.uuid4(), uuid.uuid4()))),
        ("POST", ACK, ACK, c3_headers(issued), body_of(identity_fields(uuid.uuid4(), uuid.uuid4()))),
    ]


def test_every_personal_route_answers_403_without_a_linked_identity(env, alice, invited):
    app = env.c3_app()
    requests = personal_requests(invited)
    routes = {(method, route.path) for route in app.routes if route.path.startswith("/api/v2")
              for method in getattr(route, "methods", ())}
    assert {(m, template) for m, template, *_ in requests} == routes  # aucune route personnelle oubliée
    env.insert_position(env.owner(invited))
    before = durable_state(env)
    env.hook_calls.clear()
    for method, _, path, headers, content in requests:
        response = me_request(app, method=method, path=path, headers=headers, content=content)
        assert_identity_refusal(response)
    assert env.hook_calls == ["prevalidated", "locked"] * len(requests)  # un refus du runner par route
    assert durable_state(env) == before and env.provider_calls() == []


def test_the_same_routes_are_served_once_the_email_is_linked(env, invited):
    app = env.c3_app()
    owner = env.owner(invited)
    assert env.link_email(invited, CAROL).status == "email_linked"
    me = me_request(app, headers=me_headers(invited.generation_id, active_cookie(invited)))
    assert me.status_code == 200 and me.json()["user_id"] == owner
    created = me_request(app, method="POST", path=PORTFOLIO, content=position_body(),
                         headers=portfolio_headers(invited, operation_id=uuid.uuid4()))
    assert created.status_code == 201
    listed = me_request(app, path=PORTFOLIO, headers=me_headers(invited.generation_id, active_cookie(invited)))
    assert listed.status_code == 200 and [p["ticker"] for p in listed.json()["positions"]] == ["AIR.PA"]
    for path in (OVERVIEW, THESES):
        assert me_request(app, path=path, headers=me_headers(invited.generation_id, active_cookie(invited))
                          ).status_code == 200
    level = me_request(app, method="PUT", path=ME_LEVEL, headers=level_headers(invited),
                       content=level_body("avance", "0"))
    assert level.status_code == 200 and level.json()["user_id"] == owner


def test_an_invalid_session_keeps_its_401_over_http(env):
    app = env.c3_app()
    unlinked = env.invited()
    env.service.revoke_session(unlinked.session_id, "logout")
    for method, _, path, headers, content in personal_requests(unlinked):
        response = me_request(app, method=method, path=path, headers=headers, content=content)
        assert (response.status_code, response.json()) == (401, {"error": "session_invalid"}), path


def test_http_invitation_then_http_linking_then_http_sign_in_reach_the_same_profile(env):
    composed = compose_test_app(env)
    browser = PersonalBrowser(composed)
    browser.bootstrap()
    _, done = activate(env, browser, env.service.create_invitation(cohort="act-l1").token.reveal())
    generation_id = done["generation_id"]
    assert browser.resolve(generation_id).status_code == 200
    assert_identity_refusal(browser.me(generation_id))
    owner = scalar_owner(env, generation_id)
    users = env.count("users")
    # Liaison e-mail HTTP depuis la session d'invitation non liée.
    start = link_start(browser, generation_id, CAROL)
    assert start.status_code == 202, start.text
    verify = link_verify(browser, generation_id, start.json()["operation_id"], env.mailer.last_code(CAROL))
    assert (verify.status_code, verify.json()["status"]) == (200, "email_linked")
    me = browser.me(generation_id)
    assert me.status_code == 200 and me.json()["user_id"] == owner
    # Connexion OTP HTTP ultérieure dans un autre navigateur : même profil.
    other = PersonalBrowser(composed)
    other.bootstrap()
    env.age_otp_requests()  # intervalle anti-abus entre deux envois OTP écoulé
    sign_in = other.start(CAROL)
    assert sign_in.status_code == 202, sign_in.text
    signed = other.verify(sign_in.json(), env.mailer.last_code(CAROL))
    assert signed.status_code == 200, signed.text
    second = signed.json()["generation_id"]
    assert other.resolve(second).status_code == 200
    again = other.me(second)
    assert again.status_code == 200 and again.json()["user_id"] == owner
    assert env.count("users") == users and env.count("auth_identities", "user_id = :u", u=owner) == 1


def scalar_owner(env, generation_id):
    return env.rows("SELECT s.user_id FROM oryx_session_generations g JOIN oryx_sessions s ON s.id = g.session_id "
                    "WHERE g.id = :g", g=uuid.UUID(str(generation_id)))[0]["user_id"]


# --------------------------------------------------------------------------
# 7. Décrypter : B1, B2, C1-B, C2
# --------------------------------------------------------------------------

def test_http_matrices_map_identity_link_required_to_403():
    reply = c3.IDENTITY_LINK_REPLY
    assert (reply.status, reply.code) == (403, "identity_link_required")
    for matrix in (c3._B1_REFUSALS, c3._C1B_REFUSALS, c3._C2_REFUSALS):
        assert matrix[IDENTITY_LINK_REQUIRED] is reply
    assert c3._B2_REFUSALS[dg.IDENTITY_REQUIRED] is reply and dg.IDENTITY_REQUIRED not in c3.B2_ALERT_REASONS
    assert dg._F2_REFUSALS[IDENTITY_LINK_REQUIRED] == dg.IDENTITY_REQUIRED == "identity_link_required"
    assert dg.IDENTITY_REQUIRED in dg.REFUSAL_REASONS
    assert dg.GenerationOutcome(dg.REFUSED, dg.IDENTITY_REQUIRED).reason == dg.IDENTITY_REQUIRED
    # Statuts du runner : chaque matrice reste fermée sur des statuts connus.
    for matrix in (c3._B1_REFUSALS, c3._C2_REFUSALS, dg._F2_REFUSALS):
        assert set(matrix) <= set(runner_module.STATUSES) - {AUTHORIZED}


def test_b2_reauthorization_refusal_calls_no_provider(env, alice, invited):
    command, reserved = env.reserve(alice)
    row = env.reservation(reserved.value.lease.reservation_id)
    orchestrator = env.orchestrator()
    outcome = orchestrator.generate(invited.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.declaration) == (dg.REFUSED, dg.IDENTITY_REQUIRED, None)
    assert env.provider_calls() == []  # ReadProfile refusé : ni recherche, ni EODHD, ni FMP, ni Claude
    assert env.reservation(row["id"]) == row  # aucune déclaration, bail intact


def test_b2_context_read_refusal_stops_before_any_further_provider(env, alice):
    command, reserved = env.reserve(alice)
    row = env.reservation(reserved.value.lease.reservation_id)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, actions={"search": lambda: degrade_identity(env)}))
    env.hook_calls.clear()
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.declaration) == (dg.REFUSED, dg.IDENTITY_REQUIRED, None)
    # ReadProfile autorisé, recherche, puis première lecture refusée : rien après.
    assert env.provider_calls() == ["search"]
    assert env.hook_calls.count("prevalidated") == 2 and env.hook_calls.count("executed") == 1
    assert env.reservation(row["id"]) == row


def test_http_turn_refusals_from_b1_b2_and_c1b_are_403(env, alice, invited):
    app = env.c3_app()
    # B1 : session d'invitation non liée.
    assert_identity_refusal(post(app, TURNS, body_of(turn_fields()), c3_headers(invited)))
    assert env.count("decryptage_turn_reservations") == 0 and env.provider_calls() == []
    # B2 : identité retirée pendant la préparation (après la recherche).
    env.eodhd.actions["search"] = lambda: degrade_identity(env)  # transport de CETTE application
    assert_identity_refusal(post(app, TURNS, body_of(turn_fields()), c3_headers(alice)))
    assert env.provider_calls() == ["search"]
    (reservation,) = env.reservations()
    assert reservation["state"] == "reserved" and reservation["assistant_delivery_id"] is None
    restore_identity(env)


@pytest.mark.parametrize("degrade", [True, False], ids=["identity_lost", "control"])
def test_b2_provider_failure_then_refused_declaration_keeps_the_lease(env, alice, degrade):
    """Échec fournisseur (Claude) PUIS RecordDecryptageAttemptFailure refusé
    IDENTITY_LINK_REQUIRED (identité retirée après coup, état forcé) :
    DECLARATION_REFUSED, bail JAMAIS présumé libéré (réservation, jeton et
    xmin inchangés : la transaction de déclaration a été annulée). Témoin :
    même scénario sans retrait -> DECLARED, réservation écrite."""
    command, reserved = env.reserve(alice)
    lease_id = reserved.value.lease.reservation_id
    before = env.reservation(lease_id)
    clock = FakeClock()

    def fail():
        if degrade:
            degrade_identity(env)
        raise TimeoutError("Claude lent")

    orchestrator = env.orchestrator(clock, claude=FakeClaude(clock, actions={"create_message": fail}))
    env.hook_calls.clear()
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.candidate) == (dg.PROVIDER_FAILED, dr.GENERATION_UNAVAILABLE,
                                                                   None)
    assert env.claude.methods() == ["create_message"]  # un seul appel, aucun rejeu
    if degrade:
        assert outcome.declaration == dg.DECLARATION_REFUSED
        assert env.hook_calls[-2:] == ["prevalidated", "locked"]  # déclaration refusée avant toute exécution
        assert env.reservation(lease_id) == before  # rien libéré, rien écrit
    else:
        assert outcome.declaration == dg.DECLARED
        assert env.reservation(lease_id) != before


def test_http_provider_failure_with_a_refused_declaration_is_503_and_b1_governs_the_resumption(env, alice):
    """Matrice C3 figée (R1 PR 9-C3, _PROVIDER_REPLIES) : DECLARATION_REFUSED
    -> 503 temporarily_unavailable, jamais provider_unavailable (déclaration
    non confirmée). La même intention, l'identité rétablie, retrouve le bail
    ACTIF (202 in_progress) : il n'a pas été présumé libéré."""
    clock = env.transports()

    def fail():
        degrade_identity(env)
        raise TimeoutError("Claude lent")

    env.claude = FakeClaude(clock, actions={"create_message": fail})
    app = env.c3_app()
    fields = turn_fields()
    response = post(app, TURNS, body_of(fields), c3_headers(alice))
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    (row,) = env.reservations()
    assert row["state"] == "reserved" and row["lease_token_hash"] is not None and row["retry_not_before_at"] is None
    # Toute reprise repasse par B1, donc par le runner : sans identité, refus direct 403 (jamais masqué).
    assert_identity_refusal(post(app, TURNS, body_of(fields), c3_headers(alice)))
    assert env.reservations() == [row]
    restore_identity(env)
    resumed = post(app, TURNS, body_of(fields), c3_headers(alice))
    assert resumed.status_code == 202 and resumed.json()["status"] == "in_progress"
    assert env.reservations() == [row]  # B1 rend le bail actif, sans nouvelle attribution
    assert env.claude.methods() == ["create_message"]


def test_http_c1b_refusal_writes_no_delivery(env, alice):
    clock = env.transports()
    env.claude = FakeClaude(clock, actions={"create_message": lambda: degrade_identity(env)})
    app = env.c3_app()
    deliveries = env.count("assistant_deliveries")
    assert_identity_refusal(post(app, TURNS, body_of(turn_fields()), c3_headers(alice)))
    assert env.claude.methods() == ["create_message"]
    assert env.count("assistant_deliveries") == deliveries
    (reservation,) = env.reservations()
    assert reservation["state"] == "reserved" and reservation["finalized_at"] is None


def test_replay_and_ack_are_403_without_identity_and_the_ack_applies_once_restored(env, alice):
    app = env.c3_app()
    finalized = env.finalized(alice)
    ids = body_of(identity_fields(finalized.reservation_id, finalized.assistant_turn_id))
    delivery = env.delivery(finalized.assistant_turn_id)
    assert delivery["status"] == "pending"
    degrade_identity(env)
    assert_identity_refusal(post(app, REPLAY, ids, c3_headers(alice)))
    assert_identity_refusal(post(app, ACK, ids, c3_headers(alice)))
    assert env.delivery(finalized.assistant_turn_id) == delivery  # aucun ACK appliqué
    restore_identity(env)
    acked = post(app, ACK, ids, c3_headers(alice))
    assert acked.status_code == 200 and acked.json()["delivery_status"] == "delivered"
    replayed = post(app, REPLAY, ids, c3_headers(alice))
    assert replayed.status_code == 200 and json.loads(replayed.content)["delivery_status"] == "delivered"


def test_an_invited_user_cannot_replay_or_ack_another_users_delivery(env, alice, invited):
    app = env.c3_app()
    finalized = env.finalized(alice)
    ids = body_of(identity_fields(finalized.reservation_id, finalized.assistant_turn_id))
    delivery = env.delivery(finalized.assistant_turn_id)
    for path in (REPLAY, ACK):
        assert_identity_refusal(post(app, path, ids, c3_headers(invited)))
    assert env.delivery(finalized.assistant_turn_id) == delivery
    # Une fois liée, la ressource d'autrui reste introuvable (404, inchangé).
    assert env.link_email(invited, CAROL).status == "email_linked"
    for path in (REPLAY, ACK):
        response = post(app, path, ids, c3_headers(invited))
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert env.delivery(finalized.assistant_turn_id) == delivery


def test_a_bob_session_is_unaffected_by_another_profiles_identity_state(env, alice):
    bob = env.confirmed(BOB_EMAIL, USER_B)
    degrade_identity(env, USER_A)
    assert refused(env.run(alice, c.ReadProfile()))
    outcome = env.run(bob, c.ReadProfile())
    assert outcome.status == AUTHORIZED and type(outcome.principal) is AuthorizedPrincipal
    assert outcome.principal.user_id == USER_B


# --------------------------------------------------------------------------
# 8. Dormance
# --------------------------------------------------------------------------

def test_api_app_still_exposes_no_f2_personal_route():
    """Complément de tests/test_authz_f2_dormancy.py (inventaire étendu) : la
    règle d'identité n'active rien sur api:app."""
    import api
    paths = {getattr(route, "path", "") for route in api.app.routes}
    assert not any(p.startswith(("/api/v2", "/api/auth/f2")) for p in paths)
    assert all(not hasattr(api, name) for name in ("IDENTITY_LINK_REQUIRED", "lock_linked_identities"))
