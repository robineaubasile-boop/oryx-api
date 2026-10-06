"""Tests de T4-B : service interne transactionnel de la taxonomie
pédagogique et du mapping Observation -> Capability
(core/taxonomy_service.py), et son intégration minimale à
complete_evaluation_run (core/observation_service.py).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0007_pedagogical_taxonomy, T4-A, puis
0008_longitudinal_relations depuis T5-A et 0009_competency_inference_state
depuis T6-A, tables vides non utilisées par ce service). Aucune donnée
pédagogique n'est seedée : chaque test crée ses propres releases,
définitions et memberships (codes réels, libellés factices).

1. Tests sans base (toujours exécutés) : aucune migration, API publique
   exacte, hiérarchie d'exceptions, aucun commit/rollback, aucun cycle
   d'import, aucune route / LLM / score / progression / lignée / delete,
   exactement les 45 codes de T4-A, clé advisory déterministe, validation
   structurelle complète AVANT tout accès à la base, requêtes de
   verrouillage.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1/T2/T3/T4-A) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite (FOR NO KEY UPDATE, verrou consultatif,
   index unique partiel, FK, isolation).

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t4b_test \\
           python -m pytest tests/test_taxonomy_service.py

   Les tests de concurrence utilisent des connexions réelles et attendent,
   via pg_blocking_pids(), que la seconde soit effectivement bloquée par la
   première — et vérifient sur quelle requête (pg_stat_activity) — avant de
   la libérer : aucun sleep() arbitraire. Les tests à N threads utilisent
   une barrière et vérifient l'invariant final, jamais un vainqueur
   arbitraire.
"""
import ast
import hashlib
import inspect
import itertools
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from core import observation_service as obs
from core import taxonomy_service as tax
from core.models import (
    CapabilityTaxonomyMembership,
    CoreCapabilityDefinition,
    ObservationCapability,
    PedagogicalTaxonomyRelease,
)
from core.observation_service import InvalidEvaluationState
from core.taxonomy_service import (
    CapabilityDefinitionNotFound,
    CapabilityMembershipNotFound,
    DuplicateCapabilityDefinition,
    DuplicateCapabilityMembership,
    DuplicateObservationCapability,
    DuplicateTaxonomyRelease,
    InvalidCapabilityMapping,
    InvalidTaxonomyPayload,
    InvalidTaxonomyState,
    PedagogicalObservationNotFound,
    TaxonomyReleaseImmutable,
    TaxonomyReleaseNotFound,
    TaxonomyServiceError,
)
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    REPO_ROOT,
    T1A,
    _script_directory,
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0003_analysis_session_links import T1B1
from tests.test_migration_0004_drop_company_analyses import T1C2, T4A_TABLES, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import T2A, _upgrade_head_with_users
from tests.test_migration_0006_observation_layer import T3A
from tests.test_migration_0007_pedagogical_taxonomy import (
    CAPABILITY_CODES,
    COMPETENCY_CODES,
    ONE_ACTIVE_INDEX,
    T4A,
)
from tests.test_observation_service import (
    INVALID_JSON_VALUES,
    _blocked,
    _NoDB,
    _obs_kwargs,
    _reaches_db,
    _recording,
    _start_kwargs,
)
from tests.test_observation_service import _event as _finalized_event

# Migrations de T5-A (relations longitudinales) et de T6-A (inférence de
# l'état C1-C12), tête depuis T6-A.
T5A = "0008_longitudinal_relations"
T6A = "0009_competency_inference_state"
R1B = "0010_r1b_event_idempotence"
R1C1 = "0011_assistant_deliveries"
R1C2 = "0012_decryptage_cognitive_links"
R1C4 = "0013_decryptage_conv_affinity"  # R1-C4 (fichier : R1C4_FILE, identifiant court)
R1C4_FILE = "0013_decryptage_conversation_affinity.py"

SERVICE_PATH = REPO_ROOT / "core" / "taxonomy_service.py"
PUBLIC_API = {
    "create_candidate_release",
    "create_capability_definition",
    "attach_capability_to_release",
    "activate_release",
    "map_observation_capability",
    "get_active_release",
    "get_release_capabilities",
    "get_observation_capabilities",
}
EXCEPTIONS = {
    TaxonomyReleaseNotFound,
    CapabilityDefinitionNotFound,
    CapabilityMembershipNotFound,
    PedagogicalObservationNotFound,
    InvalidTaxonomyPayload,
    InvalidTaxonomyState,
    InvalidCapabilityMapping,
    DuplicateTaxonomyRelease,
    DuplicateCapabilityDefinition,
    DuplicateCapabilityMembership,
    DuplicateObservationCapability,
    TaxonomyReleaseImmutable,
}
ADVISORY_SEED = b"oryx:pedagogical_taxonomy_release_activation"


class _Code(str, Enum):
    C7_A = "C7_A"


def _def_kwargs(code="C7_A", revision=1, **overrides):
    kwargs = {
        "capability_code": code,
        "semantic_revision": revision,
        "competency_code": code.split("_")[0] if isinstance(code, str) else "C7",
        "label": f"libellé de test {code}",
        "definition": f"définition de test {code} r{revision}",
        "mapping_guidance": {"notes": ["test"]},
    }
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t4b_head_is_0013():
    """T4-B est service-only : aucune migration ajoutée par T4-B ; les seules
    ajoutées depuis sont 0008 (T5-A), 0009 (T6-A), 0010 (R1-B), 0011 (R1-C1),
    0012 (R1-C2) et 0013 (R1-C4), qui est la tête."""
    script = _script_directory()
    assert script.get_heads() == [R1C4]
    assert script.get_revision(R1C4).down_revision == R1C2
    revisions = (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1, R1C2, R1C4)
    assert {rev.revision for rev in script.walk_revisions()} == set(revisions)
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in revisions[:-1]] + [R1C4_FILE]


def test_public_api_is_exactly_the_eight_operations():
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(tax, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name
    # Ni update / delete / unmap / replace, ni transition générique, ni
    # retrait direct, ni score / progression, ni compatibilité / lignée.
    for forbidden in ("update_capability_definition", "update_release", "delete_release",
                      "delete_capability_definition", "detach_capability", "remove_membership",
                      "unmap_observation_capability", "delete_observation_capability",
                      "replace_observation_capability", "update_observation_capability",
                      "retire_release", "set_release_status", "transition_release",
                      "calculate_coverage_score", "coverage_percent", "capability_completion",
                      "mastered_capabilities", "capability_progress", "compatibility_service",
                      "remap_history", "taxonomy_migration", "capability_lineage",
                      "historical_reclassification", "calculate_spec_fingerprint", "seed_taxonomy",
                      "bootstrap_release", "get_or_create_release"):
        assert not hasattr(tax, forbidden), forbidden


def test_exact_signatures():
    def params(fn):
        return {name: p.default for name, p in inspect.signature(fn).parameters.items() if name != "db"}

    empty = inspect.Parameter.empty
    assert params(tax.create_candidate_release) == {"version_key": empty, "spec_fingerprint": empty}
    assert params(tax.create_capability_definition) == {name: empty for name in _def_kwargs()}
    assert params(tax.attach_capability_to_release) == {"release_id": empty, "capability_definition_id": empty}
    assert params(tax.activate_release) == {"release_id": empty}
    assert params(tax.map_observation_capability) == {"observation_id": empty,
                                                       "capability_membership_id": empty}
    assert params(tax.get_active_release) == {}
    assert params(tax.get_release_capabilities) == {"release_id": empty}
    assert params(tax.get_observation_capabilities) == {"observation_id": empty}


def test_lifecycle_fields_cannot_be_supplied():
    for field in ("status", "activated_at", "created_at", "id"):
        with pytest.raises(TypeError):
            tax.create_candidate_release(_NoDB(), version_key="v", spec_fingerprint="f", **{field: None})
    for field in ("id", "created_at"):
        with pytest.raises(TypeError):
            tax.create_capability_definition(_NoDB(), **_def_kwargs(), **{field: None})


def test_exceptions_are_a_small_business_hierarchy():
    public = {o for o in vars(tax).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == tax.__name__}
    assert public == EXCEPTIONS | {TaxonomyServiceError}
    for exc in EXCEPTIONS:
        assert exc.__bases__ == (TaxonomyServiceError,), exc
    assert TaxonomyServiceError.__bases__ == (Exception,)
    assert not issubclass(TaxonomyServiceError, (LookupError, ValueError, obs.ObservationServiceError))


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_service_owns_no_transaction_and_has_no_framework_or_llm_dependency():
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "close", "SessionLocal", "get_db", "HTTPException",
                      "delete", "update", "merge", "expunge", "text", "pg_advisory_lock",
                      "pg_try_advisory_lock", "pg_advisory_unlock", "create_all"):
        assert forbidden not in tokens, forbidden
    # Un seul savepoint : l'INSERT d'une release ou d'une définition.
    assert tokens.count("begin_nested") == 1
    # Verrou consultatif TRANSACTION-LEVEL uniquement.
    assert tokens.count("pg_advisory_xact_lock") == 1
    # Aucune dépendance vers observation_service (pas de cycle d'import),
    # ni framework, ni client LLM / HTTP.
    assert _imports(SERVICE_PATH) == {"math", "uuid", "datetime", "sqlalchemy", "sqlalchemy.exc", "core.models"}


def test_import_graph_is_unidirectional():
    """observation_service -> taxonomy_service -> models, jamais l'inverse."""
    assert "core.taxonomy_service" in _imports(REPO_ROOT / "core" / "observation_service.py")
    assert not any("observation_service" in m for m in _imports(SERVICE_PATH))
    assert not any("service" in m for m in _imports(REPO_ROOT / "core" / "models.py"))


def test_service_contains_no_inference_score_or_lineage_vocabulary():
    """Une capacité n'a ni score, ni stade, ni confiance, ni progression, ni
    maîtrise ; aucune compatibilité / lignée / réclassification, aucun
    modèle (docstrings et commentaires exclus)."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("score", "confidence", "mastery", "master", "progress", "infer", "priorit", "coverage",
                 "percent", "completion", "transfer", "durab", "stage", "evidence", "polarity",
                 "weight", "coefficient", "user_id", "user_capabilit", "users", "lineage", "compatibility",
                 "remap", "reclassif", "retired_at", "sha256", "hashlib", "prompt", "anthropic",
                 "openai", "claude", "llm", "requests", "http", "xp_", "level"):
        assert word not in tokens, word


def test_service_is_not_wired_to_the_application():
    """Aucune route ni module applicatif n'utilise le service, hormis le
    contrôle interne appelé par complete_evaluation_run (T3-B) et, depuis
    T4-C, l'orchestration interne core/pedagogy/taxonomy_bootstrap.py
    (bootstrap / vérification / activation de V1, sans route ; voir
    tests/test_taxonomy_bootstrap.py)."""
    checked = 0
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or rel.startswith(("tests/", ".git/")) or "node_modules" in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            source = _code_tokens(source)
        checked += 1
        if rel == "core/pedagogy/taxonomy_bootstrap.py":
            assert "_validate_run_capability_mappings" not in source, rel
        elif rel not in ("core/taxonomy_service.py", "core/observation_service.py"):
            assert "taxonomy_service" not in source, rel
            assert "_validate_run_capability_mappings" not in source, rel
    assert checked > 0
    observation_tokens = _code_tokens((REPO_ROOT / "core" / "observation_service.py").read_text(encoding="utf-8"))
    for name in PUBLIC_API:
        assert name not in observation_tokens, name
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8")).lower()
    for word in ("taxonom", "capabilit"):
        assert word not in api, word


def test_no_seed_bootstrap_or_real_taxonomy_content():
    """Aucune donnée pédagogique : le service ne contient ni libellé, ni
    définition, ni release nommée, ni boucle de création sur les codes.
    Depuis T4-C, le contenu V1 vit dans UN seul module,
    core/pedagogy/taxonomy_v1.py (voir tests/test_taxonomy_v1.py) : aucun
    autre fichier de seed / bootstrap de taxonomie."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("oryx-v1", "oryx_v1", "production-v1", "seed", "bootstrap", "fixture"):
        assert word not in tokens, word
    found = []
    for path in REPO_ROOT.rglob("*"):
        if (".git" in path.parts or "node_modules" in path.parts or "__pycache__" in path.parts
                or not path.is_file()):
            continue
        if path.name.lower().startswith(("seed_taxonomy", "bootstrap_taxonomy", "taxonomy_v1", "capabilities_v1")):
            found.append(path.relative_to(REPO_ROOT).as_posix())
    assert found == ["core/pedagogy/taxonomy_v1.py"]


def test_capability_vocabulary_is_exactly_the_45_t4a_codes_in_natural_order():
    assert tax._CAPABILITY_CODES == CAPABILITY_CODES
    assert len(tax._CAPABILITY_CODES) == len(set(tax._CAPABILITY_CODES)) == 45
    assert tax._COMPETENCY_CODES == frozenset(COMPETENCY_CODES)
    assert list(tax._CAPABILITY_ORDER) == list(CAPABILITY_CODES)
    # Ordre naturel C1 -> C12 puis A -> D, et non lexical.
    assert sorted(CAPABILITY_CODES) != list(CAPABILITY_CODES)
    assert tax._CAPABILITY_ORDER["C2_A"] < tax._CAPABILITY_ORDER["C10_A"]
    per_competency = {c: [k for k in CAPABILITY_CODES if k.split("_")[0] == c] for c in COMPETENCY_CODES}
    assert {c: len(v) for c, v in per_competency.items()} == {
        **{f"C{i}": 3 for i in range(1, 4)}, **{f"C{i}": 4 for i in range(4, 13)}}


def test_activation_lock_key_is_deterministic_documented_and_bigint():
    expected = int.from_bytes(hashlib.sha256(ADVISORY_SEED).digest()[:8], "big", signed=True)
    assert tax.ACTIVATION_LOCK_KEY == expected
    assert -2 ** 63 <= tax.ACTIVATION_LOCK_KEY < 2 ** 63
    assert type(tax.ACTIVATION_LOCK_KEY) is int
    # Littéral figé dans le source (jamais calculé à l'import, jamais hash()).
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    assign = next(n for n in tree.body if isinstance(n, ast.Assign)
                  and [t.id for t in n.targets if isinstance(t, ast.Name)] == ["ACTIVATION_LOCK_KEY"])
    assert isinstance(assign.value, ast.UnaryOp) and isinstance(assign.value.operand, ast.Constant)
    assert "hash" not in _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n")


def test_utcnow_is_utc_aware():
    now = tax._utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# --- validation : releases --------------------------------------------------

def test_valid_release_arguments_reach_the_database():
    _reaches_db(lambda db: tax.create_candidate_release(db, version_key="v1", spec_fingerprint="sha256:x"))


@pytest.mark.parametrize("field", ["version_key", "spec_fingerprint"])
@pytest.mark.parametrize("value", ["", " ", "\t\n", None, 42, b"x", "a\x00b", uuid.uuid4()])
def test_release_rejects_invalid_strings(field, value):
    kwargs = {"version_key": "v", "spec_fingerprint": "f", field: value}
    with pytest.raises(InvalidTaxonomyPayload, match=field):
        tax.create_candidate_release(_NoDB(), **kwargs)


# --- validation : définitions -----------------------------------------------

@pytest.mark.parametrize("code", CAPABILITY_CODES)
def test_every_one_of_the_45_codes_passes_validation(code):
    _reaches_db(lambda db: tax.create_capability_definition(db, **_def_kwargs(code)))


@pytest.mark.parametrize("code", ["C13_A", "C0_A", "C1_D", "C3_D", "C4_E", "C12_E", "c7_a", "C7A", "C7-A",
                                  "C7_", "C7", "C07_A", " C7_A", "C7_A ", "", None, 7, b"C7_A", _Code.C7_A])
def test_unknown_capability_codes_are_refused(code):
    with pytest.raises(InvalidTaxonomyPayload, match="capability_code"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(code, competency_code="C7"))


@pytest.mark.parametrize("competency", ["C0", "C13", "c7", "C7 ", "C07", "", None, 7])
def test_unknown_competency_codes_are_refused(competency):
    with pytest.raises(InvalidTaxonomyPayload, match="competency_code"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs("C7_A", competency_code=competency))


@pytest.mark.parametrize("code, competency", [("C7_A", "C8"), ("C10_D", "C1"), ("C1_A", "C10"),
                                              ("C11_B", "C1"), ("C12_A", "C2"), ("C2_A", "C12")])
def test_capability_and_competency_must_be_coherent(code, competency):
    with pytest.raises(InvalidTaxonomyPayload, match="n'appartient pas"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(code, competency_code=competency))


@pytest.mark.parametrize("code, competency", [("C7_A", "C7"), ("C10_D", "C10"), ("C1_C", "C1"),
                                              ("C12_D", "C12")])
def test_coherent_capability_and_competency_pass(code, competency):
    _reaches_db(lambda db: tax.create_capability_definition(db, **_def_kwargs(code, competency_code=competency)))


@pytest.mark.parametrize("revision", [1, 2, 17, 2 ** 31 - 1])
def test_valid_semantic_revisions(revision):
    _reaches_db(lambda db: tax.create_capability_definition(db, **_def_kwargs(revision=revision)))


@pytest.mark.parametrize("revision", [0, -1, -2 ** 31, 2 ** 31, True, False, 1.0, "1", None, Decimal(1)])
def test_invalid_semantic_revisions(revision):
    with pytest.raises(InvalidTaxonomyPayload, match="semantic_revision"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(revision=revision))


@pytest.mark.parametrize("field", ["label", "definition"])
@pytest.mark.parametrize("value", ["", "  ", "\n", None, 3, b"x", "a\x00"])
def test_label_and_definition_are_required_text(field, value):
    with pytest.raises(InvalidTaxonomyPayload, match=field):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(**{field: value}))


@pytest.mark.parametrize("value", [None, [], "texte", 42, 1.5, True, ({"a": 1},), set(), frozenset()])
def test_mapping_guidance_root_must_be_a_dict(value):
    with pytest.raises(InvalidTaxonomyPayload, match="mapping_guidance"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(mapping_guidance=value))


@pytest.mark.parametrize("value", [{}, {"include": ["a"], "exclude": [], "n": {"deep": [1, 2.5, None, True]}},
                                   OrderedDict(a=1)])
def test_valid_mapping_guidance_reaches_the_database(value):
    _reaches_db(lambda db: tax.create_capability_definition(db, **_def_kwargs(mapping_guidance=value)))


@pytest.mark.parametrize("value", INVALID_JSON_VALUES, ids=range(len(INVALID_JSON_VALUES)))
def test_mapping_guidance_must_be_strictly_json_compatible(value):
    guidance = value if isinstance(value, dict) else {"v": value}
    with pytest.raises(InvalidTaxonomyPayload, match="mapping_guidance"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(mapping_guidance=guidance))


def test_mapping_guidance_rejects_circular_references():
    cyclic = {"a": []}
    cyclic["a"].append(cyclic)
    with pytest.raises(InvalidTaxonomyPayload, match="circulaire"):
        tax.create_capability_definition(_NoDB(), **_def_kwargs(mapping_guidance=cyclic))


def test_json_object_returns_an_independent_deep_copy():
    shared = {"k": 1}
    guidance = OrderedDict(s="é", i=-3, big=10 ** 30, f=0.5, t=True, n=None, e={}, l=[],
                           nested=[{"a": [1, "b", None]}], shared_twice=[shared, shared])
    copy = tax._json_object(guidance, "mapping_guidance")
    assert copy == guidance and type(copy) is dict
    guidance["nested"][0]["a"].append("mutation")
    shared["k"] = 2
    assert copy["nested"] == [{"a": [1, "b", None]}]
    assert copy["shared_twice"] == [{"k": 1}, {"k": 1}]


# --- validation : identifiants ----------------------------------------------

UUID_CALLS = {
    "attach.release_id": lambda db, v: tax.attach_capability_to_release(
        db, release_id=v, capability_definition_id=uuid.uuid4()),
    "attach.capability_definition_id": lambda db, v: tax.attach_capability_to_release(
        db, release_id=uuid.uuid4(), capability_definition_id=v),
    "activate.release_id": lambda db, v: tax.activate_release(db, release_id=v),
    "map.observation_id": lambda db, v: tax.map_observation_capability(
        db, observation_id=v, capability_membership_id=uuid.uuid4()),
    "map.capability_membership_id": lambda db, v: tax.map_observation_capability(
        db, observation_id=uuid.uuid4(), capability_membership_id=v),
    "get_release_capabilities.release_id": lambda db, v: tax.get_release_capabilities(db, release_id=v),
    "get_observation_capabilities.observation_id": lambda db, v: tax.get_observation_capabilities(
        db, observation_id=v),
}


@pytest.mark.parametrize("name", sorted(UUID_CALLS))
@pytest.mark.parametrize("value", [None, "", str(uuid.uuid4()), 1, uuid.uuid4().bytes, uuid.uuid4().int])
def test_identifiers_must_be_uuids(name, value):
    with pytest.raises(InvalidTaxonomyPayload, match=name.split(".")[1]):
        UUID_CALLS[name](_NoDB(), value)


# --- requêtes de verrouillage -----------------------------------------------

class _RecordingDB:
    """Enregistre les requêtes ; toute ligne cherchée est absente."""

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return SimpleNamespace(scalar_one_or_none=lambda: None)


RELEASE_LOCK = ("SELECT pedagogical_taxonomy_releases.id",
                "FROM pedagogical_taxonomy_releases WHERE pedagogical_taxonomy_releases.id = ",
                "FOR NO KEY UPDATE")


def test_attach_first_query_is_the_release_row_lock():
    db = _RecordingDB()
    with pytest.raises(TaxonomyReleaseNotFound):
        tax.attach_capability_to_release(db, release_id=uuid.uuid4(), capability_definition_id=uuid.uuid4())
    assert len(db.statements) == 1
    sql = db.statements[0]
    assert sql.startswith(RELEASE_LOCK[0]) and RELEASE_LOCK[1] in sql and sql.endswith(RELEASE_LOCK[2])


def test_activate_takes_the_transaction_advisory_lock_before_any_row():
    db = _RecordingDB()
    with pytest.raises(TaxonomyReleaseNotFound):
        tax.activate_release(db, release_id=uuid.uuid4())
    assert len(db.statements) == 2
    assert db.statements[0].startswith("SELECT pg_advisory_xact_lock(")
    sql = db.statements[1]
    assert sql.startswith(RELEASE_LOCK[0]) and RELEASE_LOCK[1] in sql and sql.endswith(RELEASE_LOCK[2])


def test_violated_constraint_detection_is_targeted():
    def error(constraint):
        return sa.exc.IntegrityError("INSERT", {}, SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint)))

    assert tax._violated_constraint(error("uq_x")) == "uq_x"
    assert tax._violated_constraint(error(None)) is None
    assert tax._violated_constraint(sa.exc.IntegrityError("INSERT", {}, Exception("uq_x"))) is None


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

T4_CLEANUP = ("observation_capabilities", "capability_taxonomy_memberships", "core_capability_definitions",
              "pedagogical_observations", "observation_evaluation_runs", "pedagogical_taxonomy_releases",
              "support_traces", "cognitive_events", "conversation_identities")
ON_RUN = "SELECT observation_evaluation_runs.id,"
ON_RELEASE = "SELECT pedagogical_taxonomy_releases.id,"
ON_ADVISORY = "SELECT pg_advisory_xact_lock("


@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    """Schéma = head (0009) + deux utilisateurs ; chaque test nettoie ce
    qu'il a créé."""
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _upgrade_head_with_users(pg_url, eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        for table in T4_CLEANUP:
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN ('u', 'u2')"))


@pytest.fixture
def db(Sessions):
    session = Sessions()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def clock(monkeypatch):
    """`clock()` installe (après la préparation du test) une horloge
    déterministe du service T4-B et retourne son premier instant ; chaque
    appel suivant avance d'une seconde."""
    def start():
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        ticks = itertools.count()
        monkeypatch.setattr(tax, "_utcnow", lambda: base + timedelta(seconds=next(ticks)))
        return base
    return start


def _assert_utc(value):
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _rows(engine, sql, **params):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _releases(engine):
    """État commité : {id: (status, activated_at)}."""
    return {r["id"]: (r["status"], r["activated_at"])
            for r in _rows(engine, "SELECT id, status, activated_at FROM pedagogical_taxonomy_releases")}


def _release_row(engine, release_id):
    rows = _rows(engine, "SELECT * FROM pedagogical_taxonomy_releases WHERE id = :id", id=release_id)
    return rows[0] if rows else None


def _memberships(engine, release_id):
    """{capability_code: semantic_revision} commités de la release."""
    return {r["capability_code"]: r["semantic_revision"] for r in _rows(
        engine,
        "SELECT d.capability_code, d.semantic_revision FROM capability_taxonomy_memberships m "
        "JOIN core_capability_definitions d ON d.id = m.capability_definition_id "
        "WHERE m.taxonomy_release_id = :r", r=release_id)}


def _mappings(engine, observation_id):
    return sorted(r["capability_membership_id"] for r in _rows(
        engine, "SELECT capability_membership_id FROM observation_capabilities WHERE observation_id = :o",
        o=observation_id))


def _run_state(engine, run_id):
    row = _rows(engine, "SELECT execution_status, interpretation_status FROM observation_evaluation_runs "
                        "WHERE id = :id", id=run_id)[0]
    return row["execution_status"], row["interpretation_status"]


def _release(Sessions, *, status="candidate") -> uuid.UUID:
    """Release commitée via le service (candidate, ou activée)."""
    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key=f"test-{uuid.uuid4()}",
                                               spec_fingerprint=f"sha256:{uuid.uuid4()}")
        if status in ("active", "retired"):
            tax.activate_release(session, release_id=release.id)
        session.commit()
        release_id = release.id
    if status == "retired":
        _release(Sessions, status="active")
    return release_id


def _definition(Sessions, code="C7_A", revision=1, **overrides) -> uuid.UUID:
    with Sessions() as session:
        definition = tax.create_capability_definition(session, **_def_kwargs(code, revision, **overrides))
        session.commit()
        return definition.id


def _attach(Sessions, release_id, definition_id) -> uuid.UUID:
    with Sessions() as session:
        membership = tax.attach_capability_to_release(session, release_id=release_id,
                                                      capability_definition_id=definition_id)
        session.commit()
        return membership.id


def _taxonomy(Sessions, codes=("C7_A", "C7_B", "C7_C", "C8_A")):
    """Release candidate + une définition r1 par code : (release_id,
    {code: membership_id})."""
    release_id = _release(Sessions)
    memberships = {code: _attach(Sessions, release_id, _definition(Sessions, code)) for code in codes}
    return release_id, memberships


def _run(Sessions, release_id, *observations, end=None):
    """Run commité (en tant qu'appelant), évalué sous release_id (ou
    aucune), avec une observation par dict d'overrides : (run_id,
    [observation_ids])."""
    event_id = _finalized_event(Sessions)
    with Sessions() as session:
        run = obs.start_evaluation_run(session, **_start_kwargs(event_id, pedagogical_taxonomy_release_id=release_id))
        ids = [obs.add_observation(session, run_id=run.id, **_obs_kwargs(**o)).id for o in observations]
        if end == "fail":
            obs.fail_evaluation_run(session, run_id=run.id, failure_code="evaluator_error")
        session.commit()
        return run.id, ids


def _map(Sessions, observation_id, membership_id):
    with Sessions() as session:
        tax.map_observation_capability(session, observation_id=observation_id,
                                       capability_membership_id=membership_id)
        session.commit()


def _complete(Sessions, run_id):
    with Sessions() as session:
        obs.complete_evaluation_run(session, run_id=run_id, output_fingerprint="sha256:out")
        session.commit()


def _insert_raw_mapping(engine, observation_id, membership_id):
    """Contourne le service (données incohérentes volontaires)."""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO observation_capabilities (observation_id, capability_membership_id, "
                             "created_at) VALUES (:o, :m, now())"), {"o": observation_id, "m": membership_id})


def _assert_no_unlocalized_active_run(engine):
    """Invariant global T4-B : aucun run taxonomisé completed / active
    n'a d'observation localized sans mapping ni competency_only avec
    mapping."""
    bad = _rows(engine, """
        SELECT o.id FROM pedagogical_observations o
        JOIN observation_evaluation_runs r ON r.id = o.evaluation_run_id
        WHERE r.pedagogical_taxonomy_release_id IS NOT NULL AND r.execution_status = 'completed'
          AND ((o.capability_localization = 'localized'
                AND NOT EXISTS (SELECT 1 FROM observation_capabilities c WHERE c.observation_id = o.id))
            OR (o.capability_localization = 'competency_only'
                AND EXISTS (SELECT 1 FROM observation_capabilities c WHERE c.observation_id = o.id)))""")
    assert bad == []


def _pids(*sessions):
    return [s.execute(sa.text("SELECT pg_backend_pid()")).scalar_one() for s in sessions]


# --- 0. aucune donnée seedée ------------------------------------------------

def test_pg_fresh_head_has_empty_taxonomy_tables_and_no_active_release(pg_url, engine, db):  # noqa: F811
    """Après upgrade head (0012 depuis R1-C2) et import du service T4-B, les
    quatre tables T4 sont vides : ni release V1, ni capacité, ni mapping."""
    _upgrade_head_with_users(pg_url, engine)
    for table in sorted(T4A_TABLES):
        assert _count(engine, table) == 0, table
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == R1C4
    assert tax.get_active_release(db) is None


# --- A. create_candidate_release --------------------------------------------

def test_pg_create_candidate_release(engine, db, clock):
    now = clock()
    release = tax.create_candidate_release(db, version_key="release-a", spec_fingerprint="sha256:a")
    assert isinstance(release, PedagogicalTaxonomyRelease) and isinstance(release.id, uuid.UUID)
    assert (release.version_key, release.spec_fingerprint, release.status) == ("release-a", "sha256:a",
                                                                                "candidate")
    assert release.activated_at is None and release.created_at == now
    # Flushée (visible dans la transaction de l'appelant), pas commitée.
    assert db.execute(sa.text("SELECT status FROM pedagogical_taxonomy_releases WHERE id = :id"),
                      {"id": release.id}).scalar_one() == "candidate"
    assert _release_row(engine, release.id) is None
    db.commit()
    stored = _release_row(engine, release.id)
    assert (stored["status"], stored["activated_at"], stored["created_at"]) == ("candidate", None, now)
    # Aucune activation automatique.
    assert tax.get_active_release(db) is None


def test_pg_create_candidate_release_real_clock_and_strings_kept_as_provided(engine, db):
    release = tax.create_candidate_release(db, version_key=" V ", spec_fingerprint="f\n")
    db.commit()
    stored = _release_row(engine, release.id)
    _assert_utc(stored["created_at"])
    assert (stored["version_key"], stored["spec_fingerprint"]) == (" V ", "f\n")


@pytest.mark.parametrize("duplicate", [{"version_key": "k"}, {"spec_fingerprint": "f"}])
def test_pg_duplicate_release_is_a_business_error(engine, Sessions, db, duplicate):
    with Sessions() as session:
        existing = tax.create_candidate_release(session, version_key="k", spec_fingerprint="f").id
        session.commit()
    before = _release_row(engine, existing)
    kwargs = {"version_key": "other", "spec_fingerprint": "other", **duplicate}
    with pytest.raises(DuplicateTaxonomyRelease):
        tax.create_candidate_release(db, **kwargs)
    # La transaction de l'appelant reste utilisable.
    created = tax.create_candidate_release(db, version_key="k2", spec_fingerprint="f2")
    db.commit()
    assert _release_row(engine, existing) == before
    assert set(_releases(engine)) == {existing, created.id}


def test_pg_duplicate_release_race_is_translated_via_savepoint(engine, Sessions):
    """Aucun verrou parent ne sérialise deux créations : la
    pré-vérification ne voit pas l'INSERT non commité du premier ; le second
    attend sur l'UNIQUE puis la violation de CETTE contrainte devient
    DuplicateTaxonomyRelease, sans casser la transaction de l'appelant."""
    def racing(session):
        session.add(PedagogicalTaxonomyRelease(id=uuid.uuid4(), version_key="before", status="candidate",
                                               spec_fingerprint="before", created_at=tax._utcnow()))
        session.flush()
        try:
            tax.create_candidate_release(session, version_key="shared", spec_fingerprint="second")
        except DuplicateTaxonomyRelease as exc:
            assert isinstance(exc.__cause__, sa.exc.IntegrityError)
            assert "uq_pedagogical_taxonomy_releases_version_key" in str(exc.__cause__)
            return "duplicate"
        return "created"

    with Sessions() as first, Sessions() as second:
        first_id = tax.create_candidate_release(first, version_key="shared", spec_fingerprint="first").id
        result, error = _blocked(engine, first, second, racing,
                                 waiting_on="INSERT INTO pedagogical_taxonomy_releases")
    assert error is None and result == "duplicate"
    assert {r["version_key"] for r in _rows(engine, "SELECT version_key FROM pedagogical_taxonomy_releases")} == {
        "shared", "before"}
    assert _release_row(engine, first_id)["spec_fingerprint"] == "first"


def test_pg_other_integrity_errors_are_not_masked(engine, db, monkeypatch):
    monkeypatch.setattr(tax, "RELEASE_CANDIDATE", "bogus")
    with pytest.raises(sa.exc.IntegrityError, match="ck_pedagogical_taxonomy_releases_status") as info:
        tax.create_candidate_release(db, version_key="k", spec_fingerprint="f")
    assert not isinstance(info.value, TaxonomyServiceError)
    monkeypatch.undo()
    release = tax.create_candidate_release(db, version_key="k", spec_fingerprint="f")
    db.commit()
    assert set(_releases(engine)) == {release.id}


def test_pg_caller_rollback_discards_the_release(engine, db):
    tax.create_candidate_release(db, version_key="k", spec_fingerprint="f")
    db.rollback()
    assert _count(engine, "pedagogical_taxonomy_releases") == 0


# --- B. create_capability_definition ----------------------------------------

def test_pg_create_capability_definition(engine, db, clock):
    guidance = {"include": ["lire un ratio"], "exclude": [], "notes": {"deep": [1, 2.5, None, True]}}
    now = clock()
    definition = tax.create_capability_definition(db, **_def_kwargs("C10_B", 1, mapping_guidance=guidance))
    assert isinstance(definition, CoreCapabilityDefinition) and isinstance(definition.id, uuid.UUID)
    assert (definition.capability_code, definition.semantic_revision, definition.competency_code) == (
        "C10_B", 1, "C10")
    assert definition.created_at == now
    # Copie défensive : l'appelant peut modifier son dict après l'appel.
    guidance["include"].append("mutation")
    guidance["notes"]["deep"].clear()
    guidance["new"] = 1
    expected = {"include": ["lire un ratio"], "exclude": [], "notes": {"deep": [1, 2.5, None, True]}}
    assert definition.mapping_guidance == expected
    assert _count(engine, "core_capability_definitions") == 0  # flushée, pas commitée
    db.commit()
    stored = _rows(engine, "SELECT *, jsonb_typeof(mapping_guidance) AS t FROM core_capability_definitions")[0]
    assert stored["mapping_guidance"] == expected and stored["t"] == "object"
    assert (stored["label"], stored["definition"]) == ("libellé de test C10_B", "définition de test C10_B r1")


def test_pg_all_45_codes_and_empty_guidance_are_persisted(engine, db):
    for code in CAPABILITY_CODES:
        tax.create_capability_definition(db, **_def_kwargs(code, mapping_guidance={}))
    db.commit()
    stored = _rows(engine, "SELECT capability_code, competency_code, semantic_revision, mapping_guidance "
                           "FROM core_capability_definitions")
    assert sorted(r["capability_code"] for r in stored) == sorted(CAPABILITY_CODES)
    assert all(r["capability_code"].split("_")[0] == r["competency_code"] for r in stored)
    assert all(r["semantic_revision"] == 1 and r["mapping_guidance"] == {} for r in stored)


def test_pg_new_semantic_revision_never_mutates_the_previous_one(engine, Sessions, db):
    first = _definition(Sessions, "C10_B", 1, label="sens initial")
    before = _rows(engine, "SELECT * FROM core_capability_definitions WHERE id = :id", id=first)
    second = tax.create_capability_definition(db, **_def_kwargs("C10_B", 2, label="nouveau sens"))
    db.commit()
    assert second.id != first
    assert _rows(engine, "SELECT * FROM core_capability_definitions WHERE id = :id", id=first) == before
    assert {(r["semantic_revision"], r["label"]) for r in _rows(
        engine, "SELECT semantic_revision, label FROM core_capability_definitions")} == {
        (1, "sens initial"), (2, "nouveau sens")}


def test_pg_duplicate_code_and_revision_is_refused_and_existing_untouched(engine, Sessions, db):
    existing = _definition(Sessions, "C7_A", 1, label="original")
    before = _rows(engine, "SELECT * FROM core_capability_definitions")
    with pytest.raises(DuplicateCapabilityDefinition, match="C7_A"):
        tax.create_capability_definition(db, **_def_kwargs("C7_A", 1, label="tentative de réécriture"))
    tax.create_capability_definition(db, **_def_kwargs("C7_A", 2))
    db.commit()
    assert [r for r in _rows(engine, "SELECT * FROM core_capability_definitions") if r["id"] == existing] == before


def test_pg_duplicate_definition_race_is_translated_via_savepoint(engine, Sessions):
    with Sessions() as first, Sessions() as second:
        tax.create_capability_definition(first, **_def_kwargs("C3_C", 1))
        _, error = _blocked(engine, first, second,
                            lambda s: tax.create_capability_definition(s, **_def_kwargs("C3_C", 1)),
                            waiting_on="INSERT INTO core_capability_definitions")
    assert isinstance(error, DuplicateCapabilityDefinition)
    assert isinstance(error.__cause__, sa.exc.IntegrityError)
    assert _count(engine, "core_capability_definitions") == 1


@pytest.mark.parametrize("value", INVALID_JSON_VALUES[:6], ids=range(6))
def test_pg_invalid_guidance_never_reaches_the_database(engine, db, value):
    with pytest.raises(InvalidTaxonomyPayload):
        tax.create_capability_definition(db, **_def_kwargs(mapping_guidance=value))
    db.commit()
    assert _count(engine, "core_capability_definitions") == 0


# --- C. attach_capability_to_release ----------------------------------------

def test_pg_attach_to_a_candidate(engine, Sessions, db, clock):
    release_id, definition_id = _release(Sessions), _definition(Sessions, "C7_A")
    now = clock()
    membership = tax.attach_capability_to_release(db, release_id=release_id, capability_definition_id=definition_id)
    assert isinstance(membership, CapabilityTaxonomyMembership) and isinstance(membership.id, uuid.UUID)
    assert (membership.taxonomy_release_id, membership.capability_definition_id) == (release_id, definition_id)
    assert membership.created_at == now
    assert _memberships(engine, release_id) == {}
    db.commit()
    assert _memberships(engine, release_id) == {"C7_A": 1}
    assert _releases(engine)[release_id][0] == "candidate"  # la release n'est pas modifiée


def test_pg_attach_unknown_release_or_definition(Sessions, db):
    release_id, definition_id = _release(Sessions), _definition(Sessions)
    with pytest.raises(TaxonomyReleaseNotFound):
        tax.attach_capability_to_release(db, release_id=uuid.uuid4(), capability_definition_id=definition_id)
    with pytest.raises(CapabilityDefinitionNotFound):
        tax.attach_capability_to_release(db, release_id=release_id, capability_definition_id=uuid.uuid4())


@pytest.mark.parametrize("status", ["active", "retired"])
def test_pg_attach_to_a_non_candidate_is_refused(engine, Sessions, db, status):
    release_id = _release(Sessions, status=status)
    assert _releases(engine)[release_id][0] == status
    with pytest.raises(TaxonomyReleaseImmutable, match=status):
        tax.attach_capability_to_release(db, release_id=release_id,
                                         capability_definition_id=_definition(Sessions))
    db.commit()
    assert _memberships(engine, release_id) == {}


def test_pg_duplicate_exact_membership_is_refused(engine, Sessions, db):
    release_id, definition_id = _release(Sessions), _definition(Sessions, "C7_A")
    _attach(Sessions, release_id, definition_id)
    with pytest.raises(DuplicateCapabilityMembership):
        tax.attach_capability_to_release(db, release_id=release_id, capability_definition_id=definition_id)
    db.commit()
    assert _count(engine, "capability_taxonomy_memberships") == 1


def test_pg_same_definition_is_reusable_across_releases(engine, Sessions, db):
    definition_id = _definition(Sessions, "C7_A")
    v1, v2 = _release(Sessions), _release(Sessions)
    first = tax.attach_capability_to_release(db, release_id=v1, capability_definition_id=definition_id)
    second = tax.attach_capability_to_release(db, release_id=v2, capability_definition_id=definition_id)
    db.commit()
    assert first.id != second.id
    assert _memberships(engine, v1) == _memberships(engine, v2) == {"C7_A": 1}
    assert _count(engine, "core_capability_definitions") == 1


def test_pg_two_revisions_of_a_code_in_one_release_are_refused(engine, Sessions, db):
    release_id = _release(Sessions)
    r1, r2 = _definition(Sessions, "C10_B", 1), _definition(Sessions, "C10_B", 2)
    other = _definition(Sessions, "C10_C", 1)
    _attach(Sessions, release_id, r1)
    with pytest.raises(InvalidTaxonomyState, match="C10_B révision 1"):
        tax.attach_capability_to_release(db, release_id=release_id, capability_definition_id=r2)
    # Une autre capacité reste acceptée dans la même transaction.
    tax.attach_capability_to_release(db, release_id=release_id, capability_definition_id=other)
    db.commit()
    assert _memberships(engine, release_id) == {"C10_B": 1, "C10_C": 1}
    # La révision 2 reste attachable à une autre release.
    _attach(Sessions, _release(Sessions), r2)


def test_pg_caller_rollback_discards_memberships(engine, Sessions, db):
    release_id = _release(Sessions)
    for code in ("C1_A", "C1_B"):
        tax.attach_capability_to_release(db, release_id=release_id,
                                         capability_definition_id=_definition(Sessions, code))
    db.rollback()
    assert _memberships(engine, release_id) == {}


def test_pg_attach_reloads_a_stale_release_under_the_lock(Sessions, db):
    release_id = _release(Sessions)
    stale = db.get(PedagogicalTaxonomyRelease, release_id)
    assert stale.status == "candidate"
    with Sessions() as other:
        tax.activate_release(other, release_id=release_id)
        other.commit()
    with pytest.raises(TaxonomyReleaseImmutable):
        tax.attach_capability_to_release(db, release_id=release_id, capability_definition_id=_definition(Sessions))
    assert stale.status == "active"


# --- D. concurrence des memberships -----------------------------------------

def test_pg_concurrent_revisions_of_a_code_are_serialized_by_the_release_lock(engine, Sessions):
    """V2 candidate, C10_B r1 et r2 attachées concurremment : la seconde
    transaction est bloquée sur le verrou de la release (pas plus loin),
    puis voit la première et est refusée. Sans ce verrou, les deux
    pré-vérifications passeraient (aucune contrainte en base)."""
    release_id = _release(Sessions)
    r1, r2 = _definition(Sessions, "C10_B", 1), _definition(Sessions, "C10_B", 2)
    with Sessions() as first, Sessions() as second:
        tax.attach_capability_to_release(first, release_id=release_id, capability_definition_id=r1)
        _, error = _blocked(engine, first, second, lambda s: tax.attach_capability_to_release(
            s, release_id=release_id, capability_definition_id=r2), waiting_on=ON_RELEASE)
    assert isinstance(error, InvalidTaxonomyState)
    assert _memberships(engine, release_id) == {"C10_B": 1}


def test_pg_concurrent_identical_attach_is_a_duplicate(engine, Sessions):
    release_id, definition_id = _release(Sessions), _definition(Sessions, "C4_D")
    with Sessions() as first, Sessions() as second:
        tax.attach_capability_to_release(first, release_id=release_id, capability_definition_id=definition_id)
        _, error = _blocked(engine, first, second, lambda s: tax.attach_capability_to_release(
            s, release_id=release_id, capability_definition_id=definition_id), waiting_on=ON_RELEASE)
    assert isinstance(error, DuplicateCapabilityMembership)
    assert _count(engine, "capability_taxonomy_memberships") == 1


def test_pg_many_concurrent_revisions_leave_exactly_one_in_the_release(engine, Sessions):
    release_id = _release(Sessions)
    revisions = [_definition(Sessions, "C10_B", r) for r in range(1, 7)]
    barrier = threading.Barrier(len(revisions))
    done, errors = [], []

    def work(definition_id):
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                tax.attach_capability_to_release(session, release_id=release_id,
                                                 capability_definition_id=definition_id)
                session.commit()
                done.append(definition_id)
        except Exception as exc:  # noqa: BLE001 — restitué au test
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(d,)) for d in revisions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert len(done) == 1 and len(errors) == len(revisions) - 1
    assert all(isinstance(e, InvalidTaxonomyState) for e in errors)
    assert len(_memberships(engine, release_id)) == 1
    assert _count(engine, "capability_taxonomy_memberships") == 1


def test_pg_release_lock_does_not_block_fk_references_to_the_release(engine, Sessions):
    """Justification de FOR NO KEY UPDATE sur les releases : un run évalué
    sous la release (FK -> FOR KEY SHARE) n'attend pas un attach en cours.
    Avec FOR UPDATE il attendrait (et deux orchestrations start + attach
    croisées formeraient un cycle)."""
    release_id = _release(Sessions)
    event_id = _finalized_event(Sessions)
    with Sessions() as holder, Sessions() as other:
        tax.attach_capability_to_release(holder, release_id=release_id,
                                         capability_definition_id=_definition(Sessions))
        other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
        run = obs.start_evaluation_run(other, **_start_kwargs(event_id, pedagogical_taxonomy_release_id=release_id))
        assert run.pedagogical_taxonomy_release_id == release_id
        other.rollback()
        holder.rollback()
    with engine.connect() as holder, Sessions() as other:
        holder.execute(sa.text("SELECT id FROM pedagogical_taxonomy_releases WHERE id = :id FOR UPDATE"),
                       {"id": release_id})
        other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
        with pytest.raises(sa.exc.OperationalError, match="lock timeout"):
            obs.start_evaluation_run(other, **_start_kwargs(event_id, pedagogical_taxonomy_release_id=release_id))
        other.rollback()
        holder.rollback()


# --- E. activate_release ----------------------------------------------------

def test_pg_activate_first_release(engine, Sessions, db, clock):
    release_id = _release(Sessions)
    now = clock()
    release = tax.activate_release(db, release_id=release_id)
    assert (release.id, release.status, release.activated_at) == (release_id, "active", now)
    assert _releases(engine)[release_id] == ("candidate", None)  # rien de commité
    db.commit()
    status, activated_at = _releases(engine)[release_id]
    assert (status, activated_at) == ("active", now)
    assert tax.get_active_release(db).id == release_id


def test_pg_activate_retires_the_previous_active_and_keeps_its_activated_at(engine, Sessions, db, clock):
    v1, v2 = _release(Sessions, status="active"), _release(Sessions)
    v1_before = _release_row(engine, v1)
    _assert_utc(v1_before["activated_at"])
    now = clock()
    tax.activate_release(db, release_id=v2)
    db.commit()
    releases = _releases(engine)
    assert releases[v1] == ("retired", v1_before["activated_at"])
    assert releases[v2] == ("active", now)
    # Seul status change sur l'ancienne active.
    assert {k: v for k, v in _release_row(engine, v1).items() if k != "status"} == {
        k: v for k, v in v1_before.items() if k != "status"}
    assert [s for s, _ in releases.values()].count("active") == 1


def test_pg_empty_candidate_is_activatable_no_45_rule(engine, Sessions, db):
    """T4-B est le moteur générique : la complétude du référentiel V1 (45
    capacités) relève de T4-C."""
    release_id = _release(Sessions)
    tax.activate_release(db, release_id=release_id)
    db.commit()
    assert _releases(engine)[release_id][0] == "active"
    assert _memberships(engine, release_id) == {}


@pytest.mark.parametrize("status", ["active", "retired"])
def test_pg_activate_a_non_candidate_is_refused_never_a_no_op(engine, Sessions, db, status):
    release_id = _release(Sessions, status=status)
    before = _releases(engine)
    with pytest.raises(InvalidTaxonomyState, match=status):
        tax.activate_release(db, release_id=release_id)
    db.commit()
    assert _releases(engine) == before


def test_pg_activate_unknown_release(Sessions, db):
    active = _release(Sessions, status="active")
    with pytest.raises(TaxonomyReleaseNotFound):
        tax.activate_release(db, release_id=uuid.uuid4())
    assert tax.get_active_release(db).id == active


def test_pg_caller_rollback_cancels_the_whole_activation(engine, Sessions, db):
    v1, v2 = _release(Sessions, status="active"), _release(Sessions)
    before = _releases(engine)
    tax.activate_release(db, release_id=v2)
    db.rollback()
    assert _releases(engine) == before
    assert before[v1][0] == "active" and before[v2] == ("candidate", None)


def test_pg_activate_flushes_the_retirement_before_the_activation(engine, Sessions, db):
    """Index unique partiel non différable : l'UPDATE retired doit précéder
    l'UPDATE active, quel que soit l'ordre des clés primaires (forcé dans
    les deux sens)."""
    active = _release(Sessions, status="active")
    for lower in (True, False, True, False):
        candidate = _release(Sessions)
        while (candidate < active) != lower:
            candidate = _release(Sessions)
        statements, record = _recording(engine)
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            tax.activate_release(db, release_id=candidate)
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        db.commit()
        updates = [p for sql, p in statements if sql.startswith("UPDATE pedagogical_taxonomy_releases")]
        assert [p["status"] for p in updates] == ["retired", "active"]
        assert updates[0]["pedagogical_taxonomy_releases_id"] == active
        assert updates[1]["pedagogical_taxonomy_releases_id"] == candidate
        assert "activated_at" not in updates[0]  # l'historique d'activation est conservé
        active = candidate
    assert [s for s, _ in _releases(engine).values()].count("active") == 1


def test_pg_activate_lock_order_is_advisory_then_target_then_active(engine, Sessions, db):
    previous, release_id = _release(Sessions, status="active"), _release(Sessions)
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        tax.activate_release(db, release_id=release_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    selects = [(sql, p) for sql, p in statements if sql.startswith("SELECT")]
    assert selects[0][0].startswith(ON_ADVISORY) and tax.ACTIVATION_LOCK_KEY in selects[0][1].values()
    assert selects[1][0].endswith("FOR NO KEY UPDATE") and release_id in selects[1][1].values()
    assert "pedagogical_taxonomy_releases.status = " in selects[2][0]
    assert selects[2][0].endswith("FOR NO KEY UPDATE") and "active" in selects[2][1].values()
    assert len(selects) == 3
    assert statements[0][0] == selects[0][0]  # rien avant le verrou consultatif
    assert _releases(engine)[previous][0] == "active"  # rien de commité


def _advisory_locks(engine, pid):
    return _rows(engine, "SELECT classid::bigint AS classid, objid::bigint AS objid, objsubid, granted "
                         "FROM pg_locks WHERE locktype = 'advisory' AND pid = :p", p=pid)


def test_pg_advisory_lock_is_transaction_level(engine, Sessions):
    """Détecte un remplacement par pg_advisory_lock (session) : le verrou
    est détenu pendant la transaction, avec la clé documentée, et libéré
    par PostgreSQL au COMMIT comme au ROLLBACK, connexion toujours
    ouverte."""
    key = tax.ACTIVATION_LOCK_KEY & 0xFFFFFFFFFFFFFFFF
    expected = {"classid": key >> 32, "objid": key & 0xFFFFFFFF, "objsubid": 1, "granted": True}
    for end in ("commit", "rollback"):
        release_id = _release(Sessions)
        # Session liée à UNE connexion, qui survit au COMMIT / ROLLBACK
        # (avec NullPool, une Session ordinaire rendrait sa connexion).
        with engine.connect() as conn, Session(bind=conn) as session:
            (pid,) = _pids(session)
            assert _advisory_locks(engine, pid) == []
            tax.activate_release(session, release_id=release_id)
            assert _advisory_locks(engine, pid) == [expected]
            getattr(session, end)()
            assert _pids(session) == [pid]  # même connexion
            assert _advisory_locks(engine, pid) == []


def test_pg_concurrent_activations_are_serialized_by_the_advisory_lock(engine, Sessions):
    """V1 active, V2 et V3 candidates, activées concurremment : la seconde
    attend sur le verrou consultatif (avant toute ligne), puis retire
    légitimement la première. Jamais deux actives, aucune IntegrityError."""
    v1, v2, v3 = _release(Sessions, status="active"), _release(Sessions), _release(Sessions)
    with Sessions() as first, Sessions() as second:
        tax.activate_release(first, release_id=v2)
        result, error = _blocked(engine, first, second,
                                 lambda s: tax.activate_release(s, release_id=v3).status,
                                 waiting_on=ON_ADVISORY)
    assert error is None and result == "active"
    releases = _releases(engine)
    assert {r: s for r, (s, _) in releases.items()} == {v1: "retired", v2: "retired", v3: "active"}
    assert releases[v1][1] < releases[v2][1] < releases[v3][1]


def test_pg_without_the_advisory_lock_concurrent_activations_race(engine, Sessions, monkeypatch):
    """Test de mutation : sans le verrou consultatif, la seconde activation
    attend l'ancienne active verrouillée par la première, puis — la ligne
    n'étant plus active — ne trouve plus aucune active dans son instantané
    et viole l'index unique partiel. C'est la course que le verrou
    consultatif interdit."""
    v1, v2, v3 = _release(Sessions, status="active"), _release(Sessions), _release(Sessions)
    monkeypatch.setattr(tax, "_lock_activation", lambda db: None)
    with Sessions() as first, Sessions() as second:
        tax.activate_release(first, release_id=v2)
        _, error = _blocked(engine, first, second, lambda s: tax.activate_release(s, release_id=v3),
                            waiting_on=ON_RELEASE)
    assert isinstance(error, sa.exc.IntegrityError) and ONE_ACTIVE_INDEX in str(error)
    assert {r: s for r, (s, _) in _releases(engine).items()} == {v1: "retired", v2: "active", v3: "candidate"}


def test_pg_many_concurrent_activations_end_with_exactly_one_active(engine, Sessions):
    _release(Sessions, status="active")
    candidates = [_release(Sessions) for _ in range(6)]
    barrier = threading.Barrier(len(candidates))
    done, errors = [], []

    def work(release_id):
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                tax.activate_release(session, release_id=release_id)
                session.commit()
                done.append(release_id)
        except Exception as exc:  # noqa: BLE001 — restitué au test
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(r,)) for r in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [] and sorted(done) == sorted(candidates)
    releases = _releases(engine)
    statuses = [s for s, _ in releases.values()]
    assert statuses.count("active") == 1 and statuses.count("retired") == len(releases) - 1
    # Invariant, pas un vainqueur arbitraire : l'active est la dernière
    # activation sérialisée (activated_at pris sous le verrou consultatif).
    active = next(r for r, (s, _) in releases.items() if s == "active")
    assert active == max(candidates, key=lambda r: releases[r][1])
    assert len({releases[r][1] for r in candidates}) == len(candidates)


def test_pg_activate_reloads_a_stale_target(Sessions, db):
    release_id = _release(Sessions)
    stale = db.get(PedagogicalTaxonomyRelease, release_id)
    with Sessions() as other:
        tax.activate_release(other, release_id=release_id)
        other.commit()
    with pytest.raises(InvalidTaxonomyState):
        tax.activate_release(db, release_id=release_id)
    assert stale.status == "active"


def test_pg_partial_unique_index_remains_the_final_defense(Sessions, db):
    _release(Sessions, status="active")
    rogue = db.get(PedagogicalTaxonomyRelease, _release(Sessions))
    rogue.status = "active"
    with pytest.raises(sa.exc.IntegrityError, match=ONE_ACTIVE_INDEX):
        db.flush()


def _two_actives_bypassing_the_index(db):
    """Corruption simulée DANS la transaction du test (DDL transactionnel,
    annulé par le rollback du fixture)."""
    db.execute(sa.text(f"DROP INDEX {ONE_ACTIVE_INDEX}"))
    for _ in range(2):
        db.execute(sa.text("INSERT INTO pedagogical_taxonomy_releases (id, version_key, status, spec_fingerprint, "
                           "created_at, activated_at) VALUES (:id, :k, 'active', :k, now(), now())"),
                   {"id": uuid.uuid4(), "k": str(uuid.uuid4())})


def test_pg_several_actives_are_never_resolved_arbitrarily(Sessions, db):
    candidate = _release(Sessions)
    _two_actives_bypassing_the_index(db)
    with pytest.raises(InvalidTaxonomyState, match="2 releases actives"):
        tax.get_active_release(db)
    with pytest.raises(InvalidTaxonomyState, match="2 releases actives"):
        tax.activate_release(db, release_id=candidate)
    db.rollback()


# --- F. map_observation_capability ------------------------------------------

def test_pg_map_localized_observation(engine, Sessions, db, clock):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    now = clock()
    mapping = tax.map_observation_capability(db, observation_id=observation_id,
                                             capability_membership_id=memberships["C7_A"])
    assert isinstance(mapping, ObservationCapability)
    assert (mapping.observation_id, mapping.capability_membership_id, mapping.created_at) == (
        observation_id, memberships["C7_A"], now)
    assert _mappings(engine, observation_id) == []  # flushé, pas commité
    db.commit()
    assert _mappings(engine, observation_id) == [memberships["C7_A"]]
    assert _run_state(engine, run_id) == ("running", "candidate")  # le run n'est pas modifié


def test_pg_several_capabilities_of_the_same_competency_stay_one_observation(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {"evidence_strength": "strong"})
    before = _rows(engine, "SELECT * FROM pedagogical_observations")
    for code in ("C7_A", "C7_B", "C7_C"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships[code])
    db.commit()
    assert _mappings(engine, observation_id) == sorted(memberships[c] for c in ("C7_A", "C7_B", "C7_C"))
    # Toujours UNE observation, inchangée (evidence_strength compris).
    assert _rows(engine, "SELECT * FROM pedagogical_observations") == before


def test_pg_map_competency_only_is_refused(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    _, (observation_id,) = _run(Sessions, release_id, {"capability_localization": "competency_only"})
    with pytest.raises(InvalidCapabilityMapping, match="competency_only"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
    db.commit()
    assert _count(engine, "observation_capabilities") == 0


def test_pg_map_unknown_observation_or_membership(Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    _, (observation_id,) = _run(Sessions, release_id, {})
    with pytest.raises(PedagogicalObservationNotFound):
        tax.map_observation_capability(db, observation_id=uuid.uuid4(), capability_membership_id=memberships["C7_A"])
    with pytest.raises(CapabilityMembershipNotFound):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=uuid.uuid4())


def test_pg_map_on_a_run_without_taxonomy_is_refused(engine, Sessions, db):
    _, memberships = _taxonomy(Sessions)
    _, (observation_id,) = _run(Sessions, None, {})
    with pytest.raises(InvalidCapabilityMapping, match="sans release"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
    db.commit()
    assert _count(engine, "observation_capabilities") == 0


def test_pg_map_to_another_release_is_refused(engine, Sessions, db):
    """Une observation évaluée sous V1 n'est jamais mappée sur V2, même
    pour une définition réutilisée à l'identique."""
    v1, _ = _taxonomy(Sessions, ("C7_A",))
    v2 = _release(Sessions)
    definition_id = _rows(engine, "SELECT id FROM core_capability_definitions WHERE capability_code = 'C7_A'")[0]["id"]
    v2_membership = _attach(Sessions, v2, definition_id)
    _, (observation_id,) = _run(Sessions, v1, {})
    with pytest.raises(InvalidCapabilityMapping, match="release"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=v2_membership)
    db.commit()
    assert _count(engine, "observation_capabilities") == 0


@pytest.mark.parametrize("competency, code", [("C7", "C8_A"), ("C8", "C7_A"), ("C1", "C10_A"), ("C10", "C1_A")])
def test_pg_map_to_another_competency_is_refused(engine, Sessions, db, competency, code):
    release_id, memberships = _taxonomy(Sessions, (code,))
    _, (observation_id,) = _run(Sessions, release_id, {"competency_code": competency})
    with pytest.raises(InvalidCapabilityMapping, match=f"hors de la compétence {competency}"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships[code])
    db.commit()
    assert _count(engine, "observation_capabilities") == 0


@pytest.mark.parametrize("end, states", [("complete", ("completed", "active")), ("fail", ("failed", "obsolete")),
                                         ("superseded", ("completed", "superseded"))])
def test_pg_map_on_a_terminal_run_is_refused(engine, Sessions, db, end, states):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {}, end="fail" if end == "fail" else None)
    if end != "fail":
        _map(Sessions, observation_id, memberships["C7_A"])
        _complete(Sessions, run_id)
    if end == "superseded":
        _complete(Sessions, _run_same_event(Sessions, run_id))
    assert _run_state(engine, run_id) == states
    before = _mappings(engine, observation_id)
    with pytest.raises(InvalidTaxonomyState, match="running / candidate requis"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_B"])
    db.commit()
    assert _mappings(engine, observation_id) == before


def _run_same_event(Sessions, run_id, release_id=None):
    """Nouveau run (sans observation) du même événement que run_id, évalué
    sous release_id (ou aucune), commité : sa complétion supersede
    run_id."""
    with Sessions() as session:
        event_id = obs.get_evaluation_run(session, run_id=run_id).event_id
        new = obs.start_evaluation_run(session, **_start_kwargs(
            event_id, re_evaluates_run_id=run_id, pedagogical_taxonomy_release_id=release_id))
        session.commit()
        return new.id


def test_pg_duplicate_mapping_is_refused(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    _, (observation_id,) = _run(Sessions, release_id, {})
    _map(Sessions, observation_id, memberships["C7_A"])
    with pytest.raises(DuplicateObservationCapability):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
    tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_B"])
    db.commit()
    assert _mappings(engine, observation_id) == sorted([memberships["C7_A"], memberships["C7_B"]])


def test_pg_map_under_a_candidate_release_while_another_is_active(engine, Sessions, db):
    """Un run peut volontairement être évalué sous une release candidate
    (réévaluation contrôlée sous V2 pendant que V1 est active)."""
    v1 = _release(Sessions, status="active")
    v2, memberships = _taxonomy(Sessions, ("C7_D",))
    run_id, (observation_id,) = _run(Sessions, v2, {})
    tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_D"])
    obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    db.commit()
    assert _run_state(engine, run_id) == ("completed", "active")
    assert _releases(engine)[v1][0] == "active" and _releases(engine)[v2][0] == "candidate"


def test_pg_caller_rollback_discards_uncommitted_mappings(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    _, (observation_id,) = _run(Sessions, release_id, {})
    for code in ("C7_A", "C7_B"):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships[code])
    db.rollback()
    assert _count(engine, "observation_capabilities") == 0


def test_pg_map_reloads_a_stale_run_under_the_lock(Sessions, db):
    """Le run chargé running dans la Session est rechargé sous le verrou :
    un échec commité entre-temps est vu."""
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    stale = obs.get_evaluation_run(db, run_id=run_id)
    assert stale.execution_status == "running"
    with Sessions() as other:
        obs.fail_evaluation_run(other, run_id=run_id, failure_code="f")
        other.commit()
    with pytest.raises(InvalidTaxonomyState):
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
    assert (stale.execution_status, stale.interpretation_status) == ("failed", "obsolete")


def test_pg_map_lock_order_is_observation_read_then_run_lock(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    selects = [sql for sql, _ in statements if sql.startswith("SELECT")]
    assert selects[0].startswith("SELECT pedagogical_observations.id") and "FOR " not in selects[0]
    assert selects[1].startswith(ON_RUN) and selects[1].endswith("FOR NO KEY UPDATE")
    # Un seul verrou explicite : le run parent.
    assert [s for s in selects if "FOR " in s] == [selects[1]]
    assert run_id in statements[1][1].values()


def test_pg_mapping_is_not_blocked_by_an_in_flight_invalidation(engine, Sessions):
    """Depuis T4-B, invalidate_observation verrouille en FOR NO KEY UPDATE :
    l'INSERT du mapping (FOR KEY SHARE de FK sur l'observation, pris sous le
    verrou du run) ne l'attend plus. Scénario de cycle évité : T1 invalide
    O puis ajoute une observation au run R ; T2 mappe O (détient R). Avec
    FOR UPDATE : T2 attend T1 (O), T1 attend T2 (R) -> deadlock."""
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    with Sessions() as t1, Sessions() as t2:
        obs.invalidate_observation(t1, observation_id=observation_id, reason="doublon")
        t2.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
        tax.map_observation_capability(t2, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
        result, error = _blocked(engine, t2, t1, lambda s: obs.add_observation(
            s, run_id=run_id, **_obs_kwargs(capability_localization="competency_only")).ordinal, waiting_on=ON_RUN)
    assert error is None and result == 2
    assert _mappings(engine, observation_id) == [memberships["C7_A"]]
    assert _rows(engine, "SELECT integrity_status FROM pedagogical_observations WHERE id = :id",
                 id=observation_id)[0]["integrity_status"] == "invalidated"


def test_pg_fk_share_lock_on_observations_conflicts_with_for_update_only(engine, Sessions):
    """Justification du changement : le mapping attend un FOR UPDATE sur
    l'observation, pas un FOR NO KEY UPDATE."""
    release_id, memberships = _taxonomy(Sessions)
    _, (observation_id,) = _run(Sessions, release_id, {})
    for mode, blocks in (("FOR UPDATE", True), ("FOR NO KEY UPDATE", False)):
        with engine.connect() as holder, Sessions() as other:
            holder.execute(sa.text(f"SELECT id FROM pedagogical_observations WHERE id = :id {mode}"),
                           {"id": observation_id})
            other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
            call = lambda: tax.map_observation_capability(  # noqa: E731
                other, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
            if blocks:
                with pytest.raises(sa.exc.OperationalError, match="lock timeout"):
                    call()
            else:
                call()
            other.rollback()
            holder.rollback()


# --- G. map vs complete -----------------------------------------------------

def test_pg_map_first_then_completion_waits_and_sees_the_mapping(engine, Sessions):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    with Sessions() as mapper, Sessions() as closer:
        tax.map_observation_capability(mapper, observation_id=observation_id,
                                       capability_membership_id=memberships["C7_A"])
        result, error = _blocked(engine, mapper, closer, lambda s: obs.complete_evaluation_run(
            s, run_id=run_id, output_fingerprint="o").interpretation_status, waiting_on=ON_RUN)
    assert error is None and result == "active"
    assert _run_state(engine, run_id) == ("completed", "active")
    assert _mappings(engine, observation_id) == [memberships["C7_A"]]
    _assert_no_unlocalized_active_run(engine)


def test_pg_completion_first_without_mapping_fails_then_mapping_proceeds(engine, Sessions):
    """La complétion obtient le verrou du run d'abord : elle valide l'état
    qu'elle voit (localized sans mapping) et échoue en gardant le verrou
    jusqu'à la fin de sa transaction ; le mapping attend, puis réussit ;
    une complétion ultérieure réussit."""
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    with Sessions() as closer, Sessions() as mapper:
        with pytest.raises(InvalidCapabilityMapping, match="localized sans mapping"):
            obs.complete_evaluation_run(closer, run_id=run_id, output_fingerprint="o")
        _, error = _blocked(engine, closer, mapper, lambda s: tax.map_observation_capability(
            s, observation_id=observation_id, capability_membership_id=memberships["C7_A"]), waiting_on=ON_RUN)
    assert error is None
    assert _run_state(engine, run_id) == ("running", "candidate")
    _complete(Sessions, run_id)
    assert _run_state(engine, run_id) == ("completed", "active")
    _assert_no_unlocalized_active_run(engine)


def test_pg_no_mapping_can_appear_after_a_successful_completion(engine, Sessions):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    _map(Sessions, observation_id, memberships["C7_A"])
    with Sessions() as closer, Sessions() as mapper:
        obs.complete_evaluation_run(closer, run_id=run_id, output_fingerprint="o")
        _, error = _blocked(engine, closer, mapper, lambda s: tax.map_observation_capability(
            s, observation_id=observation_id, capability_membership_id=memberships["C7_B"]), waiting_on=ON_RUN)
    assert isinstance(error, InvalidTaxonomyState)
    assert _run_state(engine, run_id) == ("completed", "active")
    assert _mappings(engine, observation_id) == [memberships["C7_A"]]


def test_pg_concurrent_map_and_complete_never_break_the_invariant(engine, Sessions):
    """Barrière : pour plusieurs runs, map et complete lancés ensemble.
    Quel que soit l'ordre, jamais completed / active + localized sans
    mapping, jamais de mapping sur un run terminé sans qu'il ait été vu."""
    release_id, memberships = _taxonomy(Sessions)
    runs = [_run(Sessions, release_id, {}) for _ in range(4)]
    barrier = threading.Barrier(2 * len(runs))
    outcomes = {}

    def mapper(run_id, observation_id):
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                tax.map_observation_capability(session, observation_id=observation_id,
                                               capability_membership_id=memberships["C7_A"])
                session.commit()
                outcomes[("map", run_id)] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcomes[("map", run_id)] = exc

    def closer(run_id):
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                obs.complete_evaluation_run(session, run_id=run_id, output_fingerprint="o")
                session.commit()
                outcomes[("complete", run_id)] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcomes[("complete", run_id)] = exc

    threads = []
    for run_id, (observation_id,) in runs:
        threads += [threading.Thread(target=mapper, args=(run_id, observation_id)),
                    threading.Thread(target=closer, args=(run_id,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    for run_id, (observation_id,) in runs:
        mapped, completed = outcomes[("map", run_id)], outcomes[("complete", run_id)]
        # Seuls deux ordres sont possibles, et tous deux cohérents.
        assert mapped == "ok"
        assert completed == "ok" or isinstance(completed, InvalidCapabilityMapping)
        expected = ("completed", "active") if completed == "ok" else ("running", "candidate")
        assert _run_state(engine, run_id) == expected
        assert _mappings(engine, observation_id) == [memberships["C7_A"]]
    _assert_no_unlocalized_active_run(engine)


# --- H. complete_evaluation_run d'un run taxonomisé -------------------------

def test_pg_taxonomized_run_with_zero_observation_completes(engine, Sessions, db):
    release_id, _ = _taxonomy(Sessions)
    run_id, _ = _run(Sessions, release_id)
    obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="sha256:empty")
    db.commit()
    assert _run_state(engine, run_id) == ("completed", "active")
    assert _count(engine, "pedagogical_observations") == 0


@pytest.mark.parametrize("codes", [("C7_A",), ("C7_A", "C7_B", "C7_C")], ids=["one", "several"])
def test_pg_localized_with_valid_mappings_completes(engine, Sessions, db, codes):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    for code in codes:
        _map(Sessions, observation_id, memberships[code])
    obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    db.commit()
    assert _run_state(engine, run_id) == ("completed", "active")


def test_pg_competency_only_without_mapping_and_mixed_runs_complete(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (localized, only, other) = _run(
        Sessions, release_id, {}, {"capability_localization": "competency_only"},
        {"competency_code": "C8", "capability_localization": "competency_only"})
    _map(Sessions, localized, memberships["C7_A"])
    obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    db.commit()
    assert _run_state(engine, run_id) == ("completed", "active")
    assert _mappings(engine, only) == _mappings(engine, other) == []


def _refused_completion(engine, db, run_id, match):
    """La complétion est refusée avant toute mutation : le run reste
    running / candidate dans la transaction, qui reste utilisable ; le
    caller décide du rollback."""
    with pytest.raises(InvalidCapabilityMapping, match=match):
        obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    run = obs.get_evaluation_run(db, run_id=run_id)
    assert (run.execution_status, run.interpretation_status, run.completed_at) == ("running", "candidate", None)
    assert db.execute(sa.text("SELECT 1")).scalar_one() == 1
    db.rollback()
    assert _run_state(engine, run_id) == ("running", "candidate")


def test_pg_localized_without_mapping_refuses_completion(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (mapped, unmapped) = _run(Sessions, release_id, {}, {"observation_text": "sans mapping"})
    _map(Sessions, mapped, memberships["C7_A"])
    _refused_completion(engine, db, run_id, "observation 2 : localized sans mapping")
    # Toutes les observations, invalidated comprises.
    with Sessions() as session:
        obs.invalidate_observation(session, observation_id=unmapped, reason="r")
        session.commit()
    _refused_completion(engine, db, run_id, "observation 2 : localized sans mapping")


def test_pg_competency_only_with_a_mapping_refuses_completion(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {"capability_localization": "competency_only"})
    _insert_raw_mapping(engine, observation_id, memberships["C7_A"])
    _refused_completion(engine, db, run_id, "competency_only avec 1 mapping")


def test_pg_mapping_of_another_release_refuses_completion(engine, Sessions, db):
    v1, _ = _taxonomy(Sessions, ("C7_A",))
    _, v2_memberships = _taxonomy(Sessions, ("C7_B",))
    run_id, (observation_id,) = _run(Sessions, v1, {})
    _insert_raw_mapping(engine, observation_id, v2_memberships["C7_B"])
    _refused_completion(engine, db, run_id, "de la release")


def test_pg_mapping_of_another_competency_refuses_completion(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    _insert_raw_mapping(engine, observation_id, memberships["C8_A"])
    _refused_completion(engine, db, run_id, "C8_A hors de C7")


def test_pg_refused_completion_can_be_fixed_in_the_same_transaction(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    run_id, (observation_id,) = _run(Sessions, release_id, {})
    with pytest.raises(InvalidCapabilityMapping):
        obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    tax.map_observation_capability(db, observation_id=observation_id, capability_membership_id=memberships["C7_A"])
    obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    db.commit()
    assert _run_state(engine, run_id) == ("completed", "active")


def test_pg_taxonomized_completion_keeps_the_t3b_supersession_and_lock_order(engine, Sessions, db):
    release_id, memberships = _taxonomy(Sessions)
    old, (old_obs,) = _run(Sessions, release_id, {})
    _map(Sessions, old_obs, memberships["C7_A"])
    _complete(Sessions, old)
    new = _run_same_event(Sessions, old, release_id)
    with Sessions() as session:
        new_obs = obs.add_observation(session, run_id=new, **_obs_kwargs()).id
        session.commit()
    _map(Sessions, new_obs, memberships["C7_B"])
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        obs.complete_evaluation_run(db, run_id=new, output_fingerprint="o")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    db.commit()
    assert (_run_state(engine, old), _run_state(engine, new)) == (("completed", "superseded"),
                                                                  ("completed", "active"))
    assert _mappings(engine, old_obs) == [memberships["C7_A"]]  # historique intact
    locks = [sql for sql, _ in statements if sql.startswith("SELECT") and "FOR " in sql]
    assert len(locks) == 3
    assert locks[0].startswith(ON_RUN) and "cognitive_events" in locks[1]
    assert "interpretation_status = " in locks[2]
    # Le contrôle T4 lit sans verrou, entre le verrou du run et celui de
    # l'événement.
    sqls = [sql for sql, _ in statements]
    first_t4 = next(i for i, sql in enumerate(sqls) if "pedagogical_observations" in sql)
    assert sqls.index(locks[0]) < first_t4 < sqls.index(locks[1])
    updates = [p for sql, p in statements if sql.startswith("UPDATE observation_evaluation_runs")]
    assert [p["interpretation_status"] for p in updates] == ["superseded", "active"]


def test_pg_run_without_taxonomy_keeps_the_exact_t3b_behavior(engine, Sessions, db):
    """Régression : aucun contrôle T4 (aucune requête sur les tables T4 ni
    sur les observations) pour un run sans release ; localized sans mapping
    reste complétable comme avant."""
    run_id, _ = _run(Sessions, None, {}, {"capability_localization": "competency_only"})
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    db.commit()
    assert _run_state(engine, run_id) == ("completed", "active")
    assert not any(t in sql for sql, _ in statements for t in (*T4A_TABLES, "pedagogical_observations"))
    assert len([sql for sql, _ in statements if sql.startswith("SELECT")]) == 3


def test_pg_full_batch_is_atomic_for_the_caller(engine, Sessions, db):
    """add_observation -> map -> complete dans UNE transaction : rien n'est
    visible avant le commit de l'appelant ; un rollback annule tout."""
    release_id, memberships = _taxonomy(Sessions)
    for end in ("rollback", "commit"):
        run_id, _ = _run(Sessions, release_id)
        observation = obs.add_observation(db, run_id=run_id, **_obs_kwargs())
        tax.map_observation_capability(db, observation_id=observation.id,
                                       capability_membership_id=memberships["C7_A"])
        obs.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
        assert _run_state(engine, run_id) == ("running", "candidate")
        assert _count(engine, "observation_capabilities") == 0
        getattr(db, end)()
        if end == "rollback":
            assert _run_state(engine, run_id) == ("running", "candidate")
            assert _rows(engine, "SELECT id FROM pedagogical_observations WHERE evaluation_run_id = :r",
                         r=run_id) == []
        else:
            assert _run_state(engine, run_id) == ("completed", "active")
            assert _mappings(engine, observation.id) == [memberships["C7_A"]]
    _assert_no_unlocalized_active_run(engine)


def test_pg_fail_evaluation_run_is_unaffected_by_taxonomy(engine, Sessions, db):
    """fail n'exige aucun mapping (le run ne deviendra jamais active) et
    conserve les mappings déjà ajoutés pour l'audit."""
    release_id, memberships = _taxonomy(Sessions)
    run_id, (mapped, _) = _run(Sessions, release_id, {}, {})
    _map(Sessions, mapped, memberships["C7_A"])
    obs.fail_evaluation_run(db, run_id=run_id, failure_code="timeout")
    db.commit()
    assert _run_state(engine, run_id) == ("failed", "obsolete")
    assert _mappings(engine, mapped) == [memberships["C7_A"]]


# --- I. lectures ------------------------------------------------------------

def test_pg_get_active_release(Sessions, db):
    assert tax.get_active_release(db) is None
    _release(Sessions)
    assert tax.get_active_release(db) is None
    active = _release(Sessions, status="active")
    assert tax.get_active_release(db).id == active
    newer = _release(Sessions, status="active")
    db.rollback()
    assert tax.get_active_release(db).id == newer


def test_pg_get_release_capabilities(engine, Sessions, db):
    with pytest.raises(TaxonomyReleaseNotFound):
        tax.get_release_capabilities(db, release_id=uuid.uuid4())
    empty = _release(Sessions)
    assert tax.get_release_capabilities(db, release_id=empty) == []
    scrambled = ("C12_D", "C10_A", "C2_B", "C1_C", "C4_A", "C10_B", "C1_A", "C11_C")
    release_id, memberships = _taxonomy(Sessions, scrambled)
    reused = _rows(engine, "SELECT id FROM core_capability_definitions WHERE capability_code = 'C10_A'")[0]["id"]
    other = _release(Sessions)
    other_membership = _attach(Sessions, other, reused)

    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        pairs = tax.get_release_capabilities(db, release_id=release_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert [d.capability_code for _, d in pairs] == ["C1_A", "C1_C", "C2_B", "C4_A", "C10_A", "C10_B",
                                                     "C11_C", "C12_D"]
    assert all(isinstance(m, CapabilityTaxonomyMembership) and isinstance(d, CoreCapabilityDefinition)
               and m.capability_definition_id == d.id and m.taxonomy_release_id == release_id for m, d in pairs)
    assert {m.id for m, _ in pairs} == set(memberships.values())
    assert not any("FOR " in sql for sql, _ in statements)
    ((membership, definition),) = tax.get_release_capabilities(db, release_id=other)
    assert (membership.id, definition.id, definition.capability_code) == (other_membership, reused, "C10_A")


def test_pg_get_observation_capabilities(engine, Sessions, db):
    with pytest.raises(PedagogicalObservationNotFound):
        tax.get_observation_capabilities(db, observation_id=uuid.uuid4())
    release_id, memberships = _taxonomy(Sessions, ("C10_D", "C10_A", "C10_B", "C1_A"))
    _, (c10, bare) = _run(Sessions, release_id, {"competency_code": "C10"},
                          {"competency_code": "C1", "capability_localization": "competency_only"})
    assert tax.get_observation_capabilities(db, observation_id=bare) == []
    for code in ("C10_D", "C10_A", "C10_B"):
        _map(Sessions, c10, memberships[code])
    # Invalidée ensuite : ses mappings restent son historique.
    with Sessions() as session:
        obs.invalidate_observation(session, observation_id=c10, reason="citation inexacte")
        session.commit()
    rows = tax.get_observation_capabilities(db, observation_id=c10)
    assert [d.capability_code for _, _, d in rows] == ["C10_A", "C10_B", "C10_D"]
    assert all(isinstance(o, ObservationCapability) and o.observation_id == c10
               and o.capability_membership_id == m.id and m.capability_definition_id == d.id
               and m.taxonomy_release_id == release_id for o, m, d in rows)


# --- J. propriété de la transaction -----------------------------------------

def test_pg_service_never_commits_nor_rolls_back(engine, Sessions, db):
    """Aucune opération (succès comme erreurs métier) n'émet COMMIT ni
    ROLLBACK ; seuls les INSERT de release / définition utilisent un
    savepoint local."""
    event_id = _finalized_event(Sessions)
    events = []
    listeners = {name: (lambda *a, _n=name: events.append(_n))
                 for name in ("commit", "rollback", "savepoint", "rollback_savepoint", "release_savepoint")}
    for name, fn in listeners.items():
        sa.event.listen(engine, name, fn)
    try:
        release = tax.create_candidate_release(db, version_key="k", spec_fingerprint="f")
        with pytest.raises(DuplicateTaxonomyRelease):
            tax.create_candidate_release(db, version_key="k", spec_fingerprint="g")
        definition = tax.create_capability_definition(db, **_def_kwargs("C7_A"))
        other = tax.create_capability_definition(db, **_def_kwargs("C7_A", 2))
        membership = tax.attach_capability_to_release(db, release_id=release.id, capability_definition_id=definition.id)
        with pytest.raises(InvalidTaxonomyState):
            tax.attach_capability_to_release(db, release_id=release.id, capability_definition_id=other.id)
        run = obs.start_evaluation_run(db, **_start_kwargs(event_id, pedagogical_taxonomy_release_id=release.id))
        observation = obs.add_observation(db, run_id=run.id, **_obs_kwargs())
        with pytest.raises(InvalidCapabilityMapping):
            obs.complete_evaluation_run(db, run_id=run.id, output_fingerprint="o")
        tax.map_observation_capability(db, observation_id=observation.id, capability_membership_id=membership.id)
        with pytest.raises(DuplicateObservationCapability):
            tax.map_observation_capability(db, observation_id=observation.id, capability_membership_id=membership.id)
        tax.activate_release(db, release_id=release.id)
        with pytest.raises(InvalidTaxonomyState):
            tax.activate_release(db, release_id=release.id)
        obs.complete_evaluation_run(db, run_id=run.id, output_fingerprint="o")
        tax.get_active_release(db)
        tax.get_release_capabilities(db, release_id=release.id)
        tax.get_observation_capabilities(db, observation_id=observation.id)
    finally:
        for name, fn in listeners.items():
            sa.event.remove(engine, name, fn)
    assert "commit" not in events and "rollback" not in events
    # release, 2 définitions (T4-B) + run (T3-B) : un savepoint chacun.
    assert events == ["savepoint", "release_savepoint"] * 4
    for table in T4A_TABLES:
        assert _count(engine, table) == 0, table
    db.rollback()
    for table in T4A_TABLES:
        assert _count(engine, table) == 0, table
