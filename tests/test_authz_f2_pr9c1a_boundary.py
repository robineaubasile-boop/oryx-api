"""Contrôles statiques, unitaires et de dormance de R1 PR 9-C1-A, sans base
de données.

Ils complètent tests/test_authz_f2_pr9a_boundary.py (graphe d'appels des
deux opérations du pont), tests/test_authz_f2_boundary.py (exception
nominative BF1 du pont : SAVEPOINT C1) et tests/test_authz_f2_dormancy.py
(matrice d'imports) :

1. périmètre : registre de production à dix-sept commandes, inventaire du
   pont à deux opérations, aucune commande de finalisation, aucune
   primitive C1-A atteinte par la production (OPERATIONS, _adapters,
   runner, routes, api.py), aucune réservation lue ni écrite par le pont,
   bail B1 inchangé, aucune migration ;
2. F-01 : aucun import _adapters -> pont (ni direct, ni dynamique) ;
3. graphe d'appels FERMÉ des primitives Prepare / Apply du pont (liste
   exacte des fonctions atteintes ; aucune ne possède la transaction) ;
4. Apply ne re-sélectionne rien (aucune lecture de la session active,
   aucune ancre, aucun classement) ; legacy et F2 partagent les mêmes
   règles (aucune duplication) ; politiques de sélection fermées ;
5. SAVEPOINT C1 : structure exacte ;
6. vues et décisions : validation, immuabilité, non-sérialisation,
   comparaison de revalidation (table de vérité).
"""
import ast
import copy
import pickle
import uuid

import pytest
import sqlalchemy as sa

from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from core.authz_f2 import _adapters
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_reservation as dr
from tests.test_authz_f2_pr9a_boundary import (
    BRIDGE,
    FORBIDDEN_ATTRIBUTE_CALLS,
    FORBIDDEN_NAMES,
    SAVEPOINT_OWNERS,
    SERVICES,
    _functions,
    _reachable,
    _tree,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

# R1 PR 9-C1-B : _require_session_live est remplacée par la sonde de la
# capacité TransactionProbe (SQL des adaptateurs), passée à _business_savepoint.
C1A_PRIMITIVES = ("_business_savepoint", "_prepare_turn", "_apply_prepared_turn", "_cognitive_context_matches",
                  "_context_of", "_has_pending_writes")
# R1 PR 9-C1-B : SEULS attributs du pont utilisés par les adaptateurs SQL (unique
# importateur) : l'exécution, la troisième opération, ses vues fermées, ses
# motifs, le SHA historique, et deux types vérifiés par le FinalizationReadPort.
ADAPTERS_BRIDGE_ATTRIBUTES = {
    "_execute", "CandidateFinalization", "PreparedTurnApplied", "FinalizationRolledBack", "CONTEXT_CHANGED",
    "PREPARATION_REFUSED", "LEGACY_COLLISION", "LEASE_STALE", "INTENT_MISMATCH", "NOT_FOUND",
    "_delivery_request_fingerprint", "DELIVERY_STATUSES", "DELIVERY_SCHEMA_VERSION", "_BridgeSession",
    "_SavepointScope",
    # R1 PR 9-C2 : opération ACK INCHANGÉE et sa vue, constantes de provenance
    # V2 et empreinte visible historique (aucune primitive C1-A).
    "DeliveryAcknowledgement", "DeliveryAcknowledged", "DELIVERED", "COGNITIVE_RUNTIME_V2", "CANONICAL_COUPLES",
    "RESPONSE_ACTIONS", "_visible_content_fingerprint"}
C1A_SERVICE_ENTRIES = ("prepare_locked_cognitive_turn", "apply_prepared_cognitive_turn",
                       "apply_prepared_construction_these_progress")

# Graphe d'appels FERMÉ des primitives C1-A du pont (fonctions des services
# atteintes). Toute nouvelle dépendance doit être ajoutée ici, explicitement.
REACHABLE_C1A = {
    "_prepare_turn": {
        "core.decryptage_cognitive_runtime": {
            "prepare_locked_cognitive_turn", "_classify_user_turn", "_runtime_version", "_lock_link",
            "_lock_active_analysis_sessions", "_conversation_anchor_event_id", "_anchor_event_id",
            "_lock_turn_events", "_session_ticker", "_event_step"},
    },
    "_apply_prepared_turn": {
        "core.decryptage_cognitive_runtime": {
            "apply_prepared_cognitive_turn", "record_turn_progress", "_write_user_turn", "_append_contribution",
            "_conversation_support_refs", "_utcnow"},
        "core.cognitive_capture": {"append_user_contribution", "_require_uuid", "_require_exact_uuid",
                                   "_require_identifier", "_require_text", "_lock_event", "_utcnow"},
        "core.decryptage_progress": {"apply_prepared_construction_these_progress", "_require_progress_request",
                                     "_visible_in_progress_session_ids", "_apply_progress_rules", "_progress_action"},
        "core.assistant_delivery": {"bind_locked_analysis_session", "_require_owned", "_bind_locked",
                                    "_require_identifier", "_require_schema_version", "_require_uuid"},
    },
}


def _source(path, name) -> str:
    return ast.unparse(_functions(path)[name])


def _called_names(node) -> set:
    out = set()
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        if isinstance(call.func, ast.Name):
            out.add(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            out.add(call.func.attr)
    return out


def _class(path, name):
    (node,) = [n for n in _tree(path).body if isinstance(n, ast.ClassDef) and n.name == name]
    return node


# --------------------------------------------------------------------------
# 1. Périmètre : rien n'est raccordé, rien n'est activé
# --------------------------------------------------------------------------

def test_production_registry_and_bridge_inventory_are_unchanged():
    # R1 PR 9-C1-B : 18 commandes (FinalizeDecryptageCandidate) et 3 opérations
    # (CandidateFinalization) ; aucune autre. La commande n'est nommée que par
    # sa définition et par les adaptateurs SQL ; jamais par une route.
    # R1 PR 9-C2 : 20 commandes (replay READ, ACK DECRYPTAGE_MUTATION) ; les 3
    # opérations du pont sont inchangées (l'ACK atteint DeliveryAcknowledgement).
    assert len(_adapters.REGISTRY) == 20
    assert {cmd for cmd, spec in _adapters.REGISTRY.items() if spec.plan is c.DECRYPTAGE_MUTATION} == {
        c.ReserveDecryptageTurn, c.RecordDecryptageAttemptFailure, c.FinalizeDecryptageCandidate,
        c.AcknowledgeDecryptageDelivery}
    assert dict(bridge.OPERATIONS) == {bridge.TurnFinalization: bridge._finalize_turn,
                                       bridge.DeliveryAcknowledgement: bridge._acknowledge,
                                       bridge.CandidateFinalization: bridge._finalize_candidate}
    naming = set()
    for root in (REPO_ROOT / "core", REPO_ROOT / "scripts", REPO_ROOT / "ci"):
        for path in root.rglob("*.py"):
            if "FinalizeDecryptageCandidate" in _code_tokens(path.read_text(encoding="utf-8")):
                naming.add(path.relative_to(REPO_ROOT).as_posix())
    # R1 PR 9-C3 (nominatif) : + le routeur HTTP isolé, qui construit la
    # commande à partir du candidat B2 (aucune autre voie, aucune route publique).
    assert naming == {"core/authz_f2/commands.py", "core/authz_f2/_adapters.py", "core/authz_f2_http/decryptage.py"}
    for path in (REPO_ROOT / "api.py", REPO_ROOT / "web-v2" / "index.html"):
        assert "FinalizeDecryptageCandidate" not in path.read_text(encoding="utf-8")


def test_c1a_primitives_are_private_and_unreachable_from_production():
    bridge_functions = _functions(BRIDGE)
    for name in C1A_PRIMITIVES:
        assert name in bridge_functions and name.startswith("_"), name
    # Aucun point d'entrée HISTORIQUE du pont ne les atteint ; seule la
    # finalisation C1-B (_finalize_candidate) les compose (R1 PR 9-C1-B).
    called = _called_names(bridge_functions["_finalize_candidate"])
    assert {"_business_savepoint", "_prepare_turn", "_apply_prepared_turn", "_same_preparation"} <= called
    for entry in ("_execute", "_finalize_turn", "_acknowledge", "_require_owned_provenance"):
        called = _called_names(bridge_functions[entry])
        assert not called & set(C1A_PRIMITIVES) - {"_require_live"}, entry
        assert not called & {"prepare_locked_cognitive_turn", "apply_prepared_cognitive_turn",
                             "apply_prepared_construction_these_progress"}, entry
    # Hors du pont, aucun module applicatif ne nomme les primitives ni les
    # nouveaux points d'entrée des services.
    names = C1A_PRIMITIVES[:4] + C1A_SERVICE_ENTRIES + ("_PreparedTurn", "PreparedCognitiveContext",
                                                          "_LocalRollback", "PreparedTurnApplied")
    services = {REPO_ROOT / "core" / "decryptage_cognitive_runtime.py", REPO_ROOT / "core" / "decryptage_progress.py"}
    adapters = REPO_ROOT / "core" / "authz_f2" / "_adapters.py"
    for root in (REPO_ROOT / "core", REPO_ROOT / "scripts", REPO_ROOT / "ci"):
        for path in root.rglob("*.py"):
            if path == BRIDGE or path in services:
                continue
            text = path.read_text(encoding="utf-8")
            if path == adapters:  # R1 PR 9-C1-B : attributs du pont FERMÉS, jamais une primitive C1-A
                used = {n.attr for n in ast.walk(_tree(path)) if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name) and n.value.id == "bridge"}
                assert used == ADAPTERS_BRIDGE_ATTRIBUTES, used ^ ADAPTERS_BRIDGE_ATTRIBUTES
                tokens = _code_tokens(text).split("\n")
                assert not [n for n in C1A_PRIMITIVES[:4] + C1A_SERVICE_ENTRIES + (
                    "_PreparedTurn", "PreparedCognitiveContext", "_LocalRollback") if n in tokens], path
                continue
            assert not [n for n in names if n in text], path
    for path in (REPO_ROOT / "api.py", REPO_ROOT / "web-v2" / "index.html"):
        text = path.read_text(encoding="utf-8")
        assert not [n for n in names if n in text], path


def test_the_bridge_never_reads_nor_writes_the_b1_reservation():
    """Code du pont (identifiants et chaînes, hors docstrings) : aucune table,
    aucun état ni aucune capacité de réservation ; l'UPDATE reserved ->
    finalized appartient à _adapters.py (PR 9-C1-B)."""
    tokens = _code_tokens(BRIDGE.read_text(encoding="utf-8"))
    for forbidden in ("decryptage_turn_reservations", "decryptage_reservation", "DecryptageTurnReservation",
                      "lease_epoch", "lease_token", "LeaseGranted", "DecryptageLease", "reserved", "finalized",
                      "FINALIZED", "RESERVED"):
        assert forbidden not in tokens, forbidden
    imported = {n.module for n in ast.walk(_tree(BRIDGE)) if isinstance(n, ast.ImportFrom)} | {
        f"{n.module}.{a.name}" for n in ast.walk(_tree(BRIDGE)) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not [i for i in imported if i and "reservation" in i]
    assert dr.LEASE_DURATION.total_seconds() == 360  # politique de bail B1 inchangée
    assert (dr.RESERVED, dr.BLOCKED, dr.FINALIZED) == ("reserved", "blocked", "finalized")


def test_no_migration_is_added():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 21 and versions[-1] == "0021_decryptage_turn_reservations.py"


def test_http_routers_and_runner_know_nothing_of_c1a():
    # R1 PR 9-C1-B : les adaptateurs SQL importent le pont (freeze) mais ne
    # nomment aucune primitive C1-A ni aucun point d'entrée des services.
    for path in list((REPO_ROOT / "core" / "authz_f2_http").glob("*.py")) + [
            REPO_ROOT / "core" / "authz_f2" / "runner.py", REPO_ROOT / "core" / "authz_f2" / "_adapters.py",
            REPO_ROOT / "core" / "authz_f2" / "commands.py"]:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")  # hors docstrings (historique)
        forbidden = C1A_PRIMITIVES[:4] + C1A_SERVICE_ENTRIES
        if path.name != "_adapters.py":
            forbidden += ("_decryptage_orm_bridge",)
        for name in forbidden:
            assert name not in tokens, (path.name, name)


# --------------------------------------------------------------------------
# 2. F-01 : aucune dépendance _adapters -> pont
# --------------------------------------------------------------------------

def test_no_import_cycle_between_adapters_and_the_bridge():
    for path in sorted((REPO_ROOT / "core" / "authz_f2").glob("*.py")):
        tree = _tree(path)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
            elif isinstance(node, ast.Call) and ast.unparse(node.func) in ("importlib.import_module", "__import__"):
                pytest.fail(f"import dynamique dans {path.name}")
        # R1 PR 9-C1-B (F-01) : UNIQUE arête _adapters -> pont ; le pont
        # n'importe plus _adapters ; les contrats communs n'importent rien.
        if path.name not in ("_adapters.py", "_decryptage_orm_bridge.py"):
            assert not any("_decryptage_orm_bridge" in i for i in imported), path.name
        if path.name == "_bridge_contracts.py":
            assert imported == set(), imported
    bridge_imports = {f"{n.module}.{a.name}" for n in ast.walk(_tree(BRIDGE)) if isinstance(n, ast.ImportFrom)
                      and n.module for a in n.names}
    assert "core.authz_f2._adapters" not in bridge_imports and "core.authz_f2._bridge_contracts" in bridge_imports
    adapters_imports = {f"{n.module}.{a.name}" for n in ast.walk(_tree(REPO_ROOT / "core" / "authz_f2" / "_adapters.py"))
                        if isinstance(n, ast.ImportFrom) and n.module for a in n.names}
    assert "core.authz_f2._decryptage_orm_bridge" in adapters_imports


# --------------------------------------------------------------------------
# 3. Graphe d'appels fermé des primitives C1-A
# --------------------------------------------------------------------------

@pytest.mark.parametrize("entry", sorted(REACHABLE_C1A))
def test_closed_call_graph_of_the_c1a_primitives(entry):
    assert _reachable(entry) == REACHABLE_C1A[entry]


@pytest.mark.parametrize("entry", sorted(REACHABLE_C1A))
def test_c1a_reachable_services_never_own_the_transaction(entry):
    for module, names in _reachable(entry).items():
        functions = _functions(SERVICES[module])
        for name in names:
            fn = functions[name]
            attrs = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert not attrs & FORBIDDEN_ATTRIBUTE_CALLS, (module, name, attrs & FORBIDDEN_ATTRIBUTE_CALLS)
            assert "begin_nested" not in attrs or (module, name) in SAVEPOINT_OWNERS, (module, name)
            called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "text" not in called, (module, name)
            assert not {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} & FORBIDDEN_NAMES, (module, name)


def test_decision_classes_own_no_transaction():
    """Les méthodes des décisions (hors du graphe par fonctions) ne pilotent
    ni ne lisent la transaction et n'exécutent aucun SQL."""
    for path, name in ((SERVICES["core.decryptage_cognitive_runtime"], "PreparedCognitiveTurn"),
                       (SERVICES["core.decryptage_cognitive_runtime"], "_TurnClassification"),
                       (BRIDGE, "_PreparedTurn")):
        node = _class(path, name)
        attrs = {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not attrs & (FORBIDDEN_ATTRIBUTE_CALLS | {"execute", "query", "flush", "add", "begin_nested",
                                                         "get_transaction", "get_nested_transaction"}), (name, attrs)
    # Le runtime ne lit jamais la transaction (la liaison au SAVEPOINT est au pont).
    runtime = SERVICES["core.decryptage_cognitive_runtime"].read_text(encoding="utf-8")
    progress = SERVICES["core.decryptage_progress"].read_text(encoding="utf-8")
    for text in (runtime, progress):
        assert "get_transaction" not in text and "get_nested_transaction" not in text
    located = {(n.func.attr, f.name) for f in ast.walk(_tree(BRIDGE)) if isinstance(f, ast.FunctionDef)
               for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "get_nested_transaction"}
    assert located == {("get_nested_transaction", "_require_current"),
                       ("get_nested_transaction", "_business_savepoint")}


# --------------------------------------------------------------------------
# 4. Aucune re-sélection en Apply ; règles partagées, jamais dupliquées
# --------------------------------------------------------------------------

def test_apply_reuses_the_prepared_decision_and_never_reselects():
    apply_bridge = _source(BRIDGE, "_apply_prepared_turn")
    assert "analysis_session=decision.analysis_session" in apply_bridge
    assert "decision = prepared._consume(db, scope, user_id)" in apply_bridge
    order = ["prepared._consume(", "dcr.apply_prepared_cognitive_turn(", "dp.apply_prepared_construction_these_progress(",
             "ad.bind_locked_analysis_session(", "dcr.record_turn_progress(", "db.flush()",
             "scope._require_current(db)", "return PreparedTurnApplied("]
    assert [apply_bridge.index(m) for m in order] == sorted(apply_bridge.index(m) for m in order)
    for forbidden in ("find_active_analysis_session", "capture_user_turn", "apply_construction_these_progress(",
                      "claim_delivery", "lock_owned_delivery", "answered_step(db", "prepare_locked_cognitive_turn"):
        assert forbidden not in apply_bridge, forbidden
    prepare_bridge = _source(BRIDGE, "_prepare_turn")
    assert prepare_bridge.count("dcr.") == 1 and "dcr.prepare_locked_cognitive_turn(" in prepare_bridge
    runtime_apply = _source(SERVICES["core.decryptage_cognitive_runtime"], "apply_prepared_cognitive_turn")
    assert _called_names(_functions(SERVICES["core.decryptage_cognitive_runtime"])["apply_prepared_cognitive_turn"]) \
        == {"type", "PreparedCognitiveTurnUnusable", "_consume", "_write_user_turn"}
    assert "classification=prepared._classification" in runtime_apply
    write = _functions(SERVICES["core.decryptage_cognitive_runtime"])["_write_user_turn"]
    assert not {n for n in _called_names(write) if n.startswith("_lock")} and not _called_names(write) & {
        "_conversation_anchor_event_id", "_classify_user_turn", "_session_ticker", "find_active_analysis_session"}


def test_selection_policies_are_closed_and_used_as_frozen():
    runtime = SERVICES["core.decryptage_cognitive_runtime"]
    assert "selection=_LEGACY_SELECTION" in _source(runtime, "capture_user_turn")
    assert "selection=_F2_SELECTION" in _source(runtime, "prepare_locked_cognitive_turn")
    assert dcr._SELECTIONS == (dcr._LEGACY_SELECTION, dcr._F2_SELECTION)
    classify = _source(runtime, "_classify_user_turn")
    assert "if len(sessions) > 1 and selection == _F2_SELECTION:" in classify
    assert "active = sessions[0] if sessions else None" in classify  # legacy : la plus récente
    # Même instruction de verrouillage, quelle que soit la politique.
    assert classify.count("_lock_active_analysis_sessions(") == 1


def test_progress_rules_exist_once_and_both_entries_use_them():
    progress = SERVICES["core.decryptage_progress"]
    functions = _functions(progress)
    legacy, prepared = (_source(progress, n) for n in ("apply_construction_these_progress",
                                                      "apply_prepared_construction_these_progress"))
    assert "find_active_analysis_session(db, user_id, ticker, for_update=True)" in legacy
    assert "find_active_analysis_session" not in prepared and "for_update" not in prepared
    for source in (legacy, prepared):
        assert source.count("_apply_progress_rules(") == 1 and source.count("_require_progress_request(") == 1
        for marker in ("PROGRESS_FORWARD", "STEP_ORDER", "UserStatement(", "InvestmentThesis(", "AnalysisFact("):
            assert marker not in source, marker  # aucune règle dupliquée hors de _apply_progress_rules
    rules = functions["_apply_progress_rules"]
    assert not _called_names(rules) & {"query", "execute", "find_active_analysis_session", "with_for_update"}
    assert "with_for_update" not in _source(progress, "_visible_in_progress_session_ids")


# --------------------------------------------------------------------------
# 5. Structure du SAVEPOINT C1
# --------------------------------------------------------------------------

def test_business_savepoint_structure_is_exact():
    fn = _functions(BRIDGE)["_business_savepoint"]
    assert isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant)
    source = "\n".join(["@" + ast.unparse(d) for d in fn.decorator_list] + [ast.unparse(stmt) for stmt in fn.body[1:]])
    assert [ast.unparse(d) for d in fn.decorator_list] == ["contextmanager"]
    # R1 PR 9-C1-B : capacité exigée d'abord ; SAVEPOINT matérialisé dès
    # l'ouverture ; port lié UNE fois à CE SAVEPOINT ; sondes par la capacité.
    order = ["type(db) is not _BridgeSession", "_require_probe(probe, db.bind)",
             "db.get_nested_transaction() is not None", "_has_pending_writes(db)", "probe.require_live()",
             "with db.begin_nested() as transaction:", "db._materialize_savepoint()",
             "scope._savepoint = db.bind.get_nested_transaction()", "probe.bind_scope(scope)", "yield scope",
             "except _LocalRollback:", "db.flush()", "except _LocalRollback as refusal:", "db.expunge_all()",
             "except BaseException:", "scope._state = _RELEASED"]
    assert [source.index(m) for m in order] == sorted(source.index(m) for m in order)
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    assert sorted(ast.unparse(h.type) for h in handlers) == ["BaseException", "_LocalRollback", "_LocalRollback"]
    for handler in handlers:  # toute exception est re-levée, sauf l'annulation locale fermée (traitée)
        body = ast.unparse(handler)
        assert "raise" in body or ("db.expunge_all()" in body and "probe.require_live()" in body)
    assert source.count("probe.require_live()") == 4
    assert source.count("expunge_all") == 1
    assert "commit" not in source and "rollback(" not in source


def test_savepoint_refuses_any_other_session_before_touching_it():
    with pytest.raises(TypeError):
        with bridge._business_savepoint(object(), None):
            pass
    engine = sa.create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/oryx_never_test")
    plain = sa.orm.Session(bind=engine)
    with pytest.raises(TypeError):
        with bridge._business_savepoint(plain, None):
            pass
    assert engine.pool.checkedout() == 0
    plain.close()


# --------------------------------------------------------------------------
# 6. Vues, décisions et revalidation (unitaires)
# --------------------------------------------------------------------------

def test_local_rollback_reasons_are_closed():
    # R1 PR 9-C1-B (freeze V1.2.1) : six motifs PRIVÉS et fermés, aucun statut
    # public nouveau ; seul preparation_refused porte un détail (motif B2 fermé).
    assert bridge.LOCAL_ROLLBACK_REASONS == ("context_changed", "preparation_refused", "legacy_collision",
                                             "lease_stale", "intent_mismatch", "not_found")
    assert bridge._LocalRollback("context_changed").reason == "context_changed"
    for reason in bridge.LOCAL_ROLLBACK_REASONS:
        detail = "stale_conversation_context" if reason == "preparation_refused" else None
        assert (bridge._LocalRollback(reason, detail).reason, bridge._LocalRollback(reason, detail).detail) == (
            reason, detail)
    for reason in ("other", "", None, 1, "CONTEXT_CHANGED"):
        with pytest.raises(ValueError):
            bridge._LocalRollback(reason)
    for reason, detail in (("preparation_refused", None), ("preparation_refused", "autre"),
                           ("context_changed", "stale_conversation_context")):
        with pytest.raises(ValueError):
            bridge._LocalRollback(reason, detail)
    assert not issubclass(bridge._LocalRollback, _adapters.AdapterInvariantBroken)
    for exc, alert in ((bridge.PendingWritesBeforeSavepoint, "decryptage_bridge_pending_writes"),
                       (bridge.PreparedTurnUnusable, "decryptage_bridge_prepared_turn_unusable")):
        assert issubclass(exc, _adapters.AdapterInvariantBroken) and exc.alert == alert


S1, S2, E1, E2 = (uuid.UUID(int=i) for i in (1, 2, 3, 4))


def context(**overrides):
    base = dict(ticker="MC.PA", analysis_session_id=S1, current_step="moat", anchor_event_id=E1,
                anchor_analysis_session_id=S1, input_action="contribution_appended", contribution_event_id=E1,
                response_context_event_id=E1, answered_step="moat")
    return bridge.PreparedCognitiveContext(**{**base, **overrides})


def test_prepared_context_is_validated_immutable_and_unserializable():
    view = context()
    with pytest.raises(dataclasses_frozen_error()):
        view.ticker = "AI.PA"
    with pytest.raises(TypeError):
        pickle.dumps(view)
    invalid = [
        dict(ticker=" MC.PA"), dict(analysis_session_id=str(S1)), dict(input_action="event_closed_context_change"),
        dict(input_action="unknown"), dict(contribution_event_id=None), dict(contribution_event_id=E2),
        dict(answered_step=None), dict(answered_step="swot_final"), dict(anchor_analysis_session_id=None),
        dict(response_context_event_id=E2), dict(analysis_session_id=S2), dict(analysis_session_id=None),
        dict(current_step="x" * 33),
        dict(input_action="no_open_event", contribution_event_id=None, answered_step=None),  # ancre présente
    ]
    for overrides in invalid:
        with pytest.raises(ValueError):
            context(**overrides)
    # Formes valides : absence préparée, navigation, reprise ancrée.
    context(input_action="no_open_event", analysis_session_id=None, current_step=None, anchor_event_id=None,
            anchor_analysis_session_id=None, contribution_event_id=None, response_context_event_id=None,
            answered_step=None)
    context(input_action="context_switched", analysis_session_id=S2, current_step="business",
            anchor_analysis_session_id=S1, contribution_event_id=None, response_context_event_id=None,
            answered_step=None)
    context(input_action="no_user_contribution", contribution_event_id=None, answered_step=None)


def dataclasses_frozen_error():
    import dataclasses
    return dataclasses.FrozenInstanceError


def test_cognitive_revalidation_truth_table():
    active = dc.PreparationActiveSession(S1, "moat")
    current = dc.PreparationAnchor(dc.ANCHOR_CURRENT, E1, S1)
    match = bridge._cognitive_context_matches
    assert match(context(), ticker="MC.PA", anchor=current, active_session=active)
    assert not match(context(), ticker="AI.PA", anchor=current, active_session=active)
    assert not match(context(), ticker="MC.PA", anchor=current, active_session=dc.PreparationActiveSession(S1, "business"))
    assert not match(context(), ticker="MC.PA", anchor=current, active_session=dc.PreparationActiveSession(S2, "moat"))
    assert not match(context(), ticker="MC.PA", anchor=current, active_session=None)
    assert not match(context(), ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_CURRENT, E2, S1),
                     active_session=active)
    assert not match(context(), ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_OTHER_TICKER, E1, S1),
                     active_session=active)
    assert not match(context(), ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_NONE, None, None),
                     active_session=active)
    assert not match(context(), ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_UNBOUND, E1, None),
                     active_session=active)
    resumed = context(input_action="no_user_contribution", contribution_event_id=None, answered_step=None)
    assert match(resumed, ticker="MC.PA", anchor=current, active_session=active)
    absent = context(input_action="no_open_event", analysis_session_id=None, current_step=None, anchor_event_id=None,
                     anchor_analysis_session_id=None, contribution_event_id=None, response_context_event_id=None,
                     answered_step=None)
    assert match(absent, ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_NONE, None, None), active_session=None)
    assert not match(absent, ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_NONE, None, None),
                     active_session=active)
    switched = context(input_action="context_switched", analysis_session_id=None, current_step=None,
                       anchor_analysis_session_id=S2, contribution_event_id=None, response_context_event_id=None,
                       answered_step=None)
    other = dc.PreparationAnchor(dc.ANCHOR_OTHER_TICKER, E1, S2)
    assert match(switched, ticker="MC.PA", anchor=other, active_session=None)
    assert not match(switched, ticker="MC.PA", anchor=dc.PreparationAnchor(dc.ANCHOR_CURRENT, E1, S2),
                     active_session=None)
    for bad in (dict(anchor=None), dict(active_session=("x",)), dict(anchor=("current", E1, S1))):
        kwargs = dict(ticker="MC.PA", anchor=current, active_session=active) | bad
        with pytest.raises(TypeError):
            match(context(), **kwargs)


def test_decisions_cannot_be_built_outside_their_factories():
    with pytest.raises(dcr.PreparedCognitiveTurnUnusable):
        dcr.PreparedCognitiveTurn(object(), None, None, "MC.PA", "", None, None)
    with pytest.raises(bridge.PreparedTurnUnusable):
        bridge._PreparedTurn(object(), None, "u", None)
    with pytest.raises(dcr.PreparedCognitiveTurnUnusable):
        dcr.apply_prepared_cognitive_turn(None, prepared=object())
    with pytest.raises(bridge.PreparedTurnUnusable):
        bridge._apply_prepared_turn(None, None, "u", object(), method_id=None, step_marker=None,
                                    financial_snapshot=None)
    scope = bridge._SavepointScope(None)
    with pytest.raises(TypeError):
        pickle.dumps(scope)
    with pytest.raises(bridge.PreparedTurnUnusable):
        scope._require_current(None)
    assert copy.copy  # noqa: B018 — copie testée sur PostgreSQL (décision réelle)


@pytest.mark.parametrize("kwargs, error", [
    (dict(step="inconnu"), dp.InvalidDecryptageStep),
    (dict(user_contribution=1), TypeError),
    (dict(thesis_text="x"), ValueError),
    (dict(user_contribution=True, answered_step="swot_final"), ValueError),
    (dict(answered_step="business"), ValueError),
])
def test_prepared_progress_validates_before_any_read(kwargs, error):
    base = dict(analysis_session=None, user_id="u", ticker="MC.PA", step="business", thesis_text=None, data=None,
                user_contribution=False, answered_step=None)
    with pytest.raises(error):
        dp.apply_prepared_construction_these_progress(None, **{**base, **kwargs})  # aucune base touchée


def test_prepared_progress_refuses_a_foreign_object_without_reading():
    with pytest.raises(dp.PreparedAnalysisSessionMismatch):
        dp.apply_prepared_construction_these_progress(
            None, analysis_session=object(), user_id="u", ticker="MC.PA", step="business", thesis_text=None,
            data=None, user_contribution=False, answered_step=None)
