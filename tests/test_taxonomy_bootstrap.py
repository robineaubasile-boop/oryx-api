"""Tests de T4-C (2/2) : bootstrap, vérification et activation contrôlée de
la taxonomie Oryx V1 (core/pedagogy/taxonomy_bootstrap.py) et CLI interne
(scripts/bootstrap_pedagogical_taxonomy_v1.py). Contenu de la SPEC :
tests/test_taxonomy_v1.py.

Aucune migration dans ce chantier : 0007_pedagogical_taxonomy reste la
tête ; le contenu V1 est inséré par l'application, via le service T4-B.

1. Tests sans base (toujours exécutés) : aucune migration, API exacte,
   exceptions, aucune transaction possédée, écritures uniquement via T4-B,
   aucune route / LLM / score, comparaison pure DB <-> SPEC (dont la
   compétence, qu'aucune ligne ne peut contredire en base), CLI sans base.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1..T4-B) :
   uniquement si ORYX_TEST_DATABASE_URL vise une base DÉDIÉE dont le nom
   contient "test" (schéma public détruit et recréé), sinon SKIPPÉS.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t4c_test \\
           python -m pytest tests/test_taxonomy_v1.py tests/test_taxonomy_bootstrap.py

   Les corruptions de V1 (membership supprimé ou ajouté, définition réécrite,
   fingerprint modifié) sont simulées en SQL brut, hors service : c'est
   précisément ce que le service interdit et ce que verify doit détecter.
   Concurrence : pg_blocking_pids() + pg_stat_activity (la seconde
   transaction est réellement bloquée, et sur quelle requête) ou barrière à
   N threads avec vérification de l'invariant final ; aucun sleep()
   arbitraire.
"""
import ast
import inspect
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import FrozenInstanceError, fields, is_dataclass
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from core import db as core_db
from core import taxonomy_service as tax
from core.pedagogy import taxonomy_bootstrap as boot
from core.pedagogy import taxonomy_v1 as v1
from core.pedagogy.taxonomy_bootstrap import (
    TaxonomyActivationResult,
    TaxonomyBootstrapConflict,
    TaxonomyBootstrapError,
    TaxonomyBootstrapResult,
    TaxonomyVerificationError,
    TaxonomyVerificationResult,
    activate_taxonomy_v1,
    bootstrap_taxonomy_v1,
    verify_taxonomy_v1,
)
from core.pedagogy.taxonomy_v1 import TaxonomySpecError
from core.taxonomy_service import InvalidTaxonomyState, TaxonomyReleaseNotFound, TaxonomyServiceError
from scripts import bootstrap_pedagogical_taxonomy_v1 as cli
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _script_directory
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_migration_0007_pedagogical_taxonomy import T4A, _application_sources
from tests.test_observation_service import _NoDB, _backend_pid, _blocked, _recording, _wait_until_blocked_by
from tests.test_taxonomy_service import (
    ON_ADVISORY,
    _complete,
    _count,
    _map,
    _release,
    _release_row,
    _releases,
    _rows,
    _run,
    db,  # noqa: F401 — fixture
    engine,  # noqa: F401 — fixture
    Sessions,  # noqa: F401 — fixture
)
from tests.test_taxonomy_v1 import GOLDEN_V1_FINGERPRINT

MODULE_PATH = REPO_ROOT / "core" / "pedagogy" / "taxonomy_bootstrap.py"
CLI_PATH = REPO_ROOT / "scripts" / "bootstrap_pedagogical_taxonomy_v1.py"
PUBLIC_API = {"bootstrap_taxonomy_v1", "verify_taxonomy_v1", "activate_taxonomy_v1"}
T4C_EXCEPTIONS = {TaxonomyBootstrapError, TaxonomyBootstrapConflict, TaxonomyVerificationError}
ON_DEFINITION_INSERT = "INSERT INTO core_capability_definitions"
ON_RELEASE_INSERT = "INSERT INTO pedagogical_taxonomy_releases"
SPEC = v1.taxonomy_v1_spec()
SPEC_BY_CODE = {c["capability_code"]: c for c in SPEC["capabilities"]}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_head_is_still_0007():
    """T4-C est du CONTENU : ni 0008, ni fichier de migration ajouté."""
    script = _script_directory()
    assert script.get_heads() == [T4A]
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-1] == f"{T4A}.py" and len(files) == 7
    assert not any(name.startswith("0008") for name in files)


def test_public_api_is_exactly_bootstrap_verify_activate():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        assert list(inspect.signature(getattr(boot, name)).parameters) == ["db"], name
    for forbidden in ("repair_taxonomy_v1", "update_capability_definition", "bootstrap_taxonomy_v2",
                      "migrate_taxonomy", "remap_observations", "capability_progress", "coverage_percent",
                      "retire_taxonomy_v1", "reactivate_taxonomy_v1"):
        assert not hasattr(boot, forbidden), forbidden


def test_exceptions_are_a_minimal_explicit_hierarchy():
    public = {o for o in vars(boot).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == boot.__name__}
    assert public == T4C_EXCEPTIONS
    assert TaxonomyBootstrapError.__bases__ == (Exception,)
    assert TaxonomyBootstrapConflict.__bases__ == (TaxonomyBootstrapError,)
    assert TaxonomyVerificationError.__bases__ == (TaxonomyBootstrapError,)
    assert TaxonomySpecError.__bases__ == (Exception,)
    for exc in (*T4C_EXCEPTIONS, TaxonomySpecError):
        assert not issubclass(exc, (ValueError, RuntimeError, LookupError, TaxonomyServiceError)), exc


def test_results_are_immutable_dataclasses_without_score_or_progress():
    base = ["release_id", "version_key", "spec_fingerprint", "status", "capability_count"]
    assert [f.name for f in fields(TaxonomyVerificationResult)] == base
    assert [f.name for f in fields(TaxonomyBootstrapResult)] == base + [
        "created_release", "created_definitions", "reused_definitions", "created_memberships"]
    assert [f.name for f in fields(TaxonomyActivationResult)] == base + ["activated"]
    result = TaxonomyVerificationResult(uuid.uuid4(), "oryx-v1", "f", "candidate", 45)
    assert is_dataclass(result)
    with pytest.raises(FrozenInstanceError):
        result.status = "active"


def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_module_owns_no_transaction_and_writes_only_through_t4b():
    source = MODULE_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "begin_nested", "close", "flush", "add", "add_all",
                      "delete", "update", "merge", "insert", "text", "execute_many", "SessionLocal",
                      "get_db", "pg_advisory_xact_lock", "pg_advisory_lock", "with_for_update",
                      "HTTPException", "create_all", "CapabilityTaxonomyMembership", "ObservationCapability",
                      "map_observation_capability", "_lock_activation", "_lock_release"):
        assert forbidden not in tokens, forbidden
    assert _imports(MODULE_PATH) == {"json", "uuid", "dataclasses", "sqlalchemy", "core", "core.models",
                                     "core.pedagogy.taxonomy_v1"}
    tree = ast.parse(source)
    assert [[a.name for a in n.names] for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module == "core"] == [["taxonomy_service"]]
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "tax"}
    assert used == {
        # écritures (primitives T4-B)
        "create_capability_definition", "create_candidate_release", "attach_capability_to_release",
        "activate_release",
        # lecture T4-B
        "get_release_capabilities",
        # vocabulaire et exceptions T4-B réutilisés
        "RELEASE_CANDIDATE", "RELEASE_ACTIVE", "RELEASE_RETIRED", "DuplicateCapabilityDefinition",
        "DuplicateTaxonomyRelease", "InvalidTaxonomyState", "TaxonomyReleaseNotFound",
    }
    # Aucune constante / fonction privée de T4-B.
    assert not any(name.startswith("_") for name in used)


def test_module_has_no_inference_score_llm_or_lineage_vocabulary():
    tokens = _code_tokens(MODULE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("score", "confidence", "mastery", "mastered", "progress", "infer", "coverage", "percent",
                 "stage", "evidence", "weight", "user_id", "users", "lineage", "compatib", "remap",
                 "reclassif", "prompt", "anthropic", "openai", "claude", "llm", "requests", "http", "level",
                 "oryx-v2"):
        assert word not in tokens, word


def test_t4c_is_not_wired_to_the_application():
    """Aucune route : seuls le paquet core/pedagogy et la CLI interne
    mentionnent T4-C ; api.py ne parle ni de taxonomie ni de capacités."""
    allowed = {"core/pedagogy/__init__.py", "core/pedagogy/taxonomy_v1.py", "core/pedagogy/taxonomy_bootstrap.py",
               "scripts/bootstrap_pedagogical_taxonomy_v1.py"}
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        if rel not in allowed:
            for needle in ("core.pedagogy", "taxonomy_bootstrap", "taxonomy_v1", "bootstrap_taxonomy_v1",
                           "verify_taxonomy_v1", "activate_taxonomy_v1", "oryx-v1"):
                assert needle not in source, (rel, needle)
    assert checked > 0
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8")).lower()
    for word in ("taxonom", "capabilit", "pedagogy", "bootstrap"):
        assert word not in api, word


def test_cli_never_activates():
    tokens = _code_tokens(CLI_PATH.read_text(encoding="utf-8")).split("\n")
    for forbidden in ("activate_taxonomy_v1", "activate_release", "taxonomy_service"):
        assert forbidden not in tokens, forbidden
    assert _imports(CLI_PATH) == {"argparse", "sys", "typing", "sqlalchemy", "sqlalchemy.exc", "core",
                                  "core.pedagogy.taxonomy_bootstrap"}


@pytest.mark.parametrize("operation", sorted(PUBLIC_API))
def test_an_invalid_spec_fails_before_any_database_access(operation, monkeypatch):
    broken = v1.taxonomy_v1_spec()
    broken["capabilities"].pop()
    monkeypatch.setattr(v1, "_SPEC", broken)
    with pytest.raises(TaxonomySpecError):
        getattr(boot, operation)(_NoDB())


@pytest.mark.parametrize("operation", sorted(PUBLIC_API))
def test_a_drifted_spec_fails_before_any_database_access(operation, monkeypatch):
    drifted = v1.taxonomy_v1_spec()
    drifted["competencies"][9]["central_question"] += " "
    monkeypatch.setattr(v1, "_SPEC", drifted)
    with pytest.raises(TaxonomySpecError, match="EXPECTED_V1_FINGERPRINT"):
        getattr(boot, operation)(_NoDB())


# --- comparaison pure DB <-> SPEC ------------------------------------------

def _fake_rows(**overrides):
    """[(membership, définition)] conformes à la SPEC ; overrides :
    {code: {champ: valeur}} ou {code: None} pour retirer la capacité."""
    rows = []
    for code, capability in SPEC_BY_CODE.items():
        if code in overrides and overrides[code] is None:
            continue
        definition = SimpleNamespace(id=uuid.uuid4(), **{f: capability[f] for f in v1.PERSISTED_CAPABILITY_FIELDS})
        for field, value in (overrides.get(code) or {}).items():
            setattr(definition, field, value)
        rows.append((SimpleNamespace(id=uuid.uuid4()), definition))
    return rows


def _fake_release(**overrides):
    values = {"id": uuid.uuid4(), "version_key": "oryx-v1", "spec_fingerprint": GOLDEN_V1_FINGERPRINT,
              "status": "candidate"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_pure_verification_of_an_exact_v1():
    release = _fake_release()
    result = boot._verify_rows(release, _fake_rows(), SPEC, GOLDEN_V1_FINGERPRINT)
    assert result == TaxonomyVerificationResult(release.id, "oryx-v1", GOLDEN_V1_FINGERPRINT, "candidate", 45)


PURE_DIVERGENCES = {
    "wrong_competency": ({"C7_A": {"competency_code": "C8"}}, "C7_A .*competency_code"),
    "wrong_revision": ({"C7_A": {"semantic_revision": 2}}, "C7_A .*semantic_revision"),
    "revision_as_bool": ({"C7_A": {"semantic_revision": True}}, "semantic_revision"),
    "wrong_label": ({"C12_D": {"label": "Conséquence globale"}}, "C12_D .*label"),
    "nfd_label": ({"C1_A": {"label": "Nature économique de l'investissement"}}, "C1_A .*label"),
    "wrong_definition": ({"C4_B": {"definition": SPEC_BY_CODE["C4_B"]["definition"] + " "}}, "definition"),
    "guidance_list_order": ({"C7_B": {"mapping_guidance": {
        **SPEC_BY_CODE["C7_B"]["mapping_guidance"],
        "include": SPEC_BY_CODE["C7_B"]["mapping_guidance"]["include"][::-1]}}}, "mapping_guidance"),
    "guidance_extra_key": ({"C7_B": {"mapping_guidance": {**SPEC_BY_CODE["C7_B"]["mapping_guidance"],
                                                          "notes": []}}}, "mapping_guidance"),
    "missing_capability": ({"C10_B": None}, "manquantes \\['C10_B'\\]"),
}


@pytest.mark.parametrize("name", sorted(PURE_DIVERGENCES))
def test_pure_verification_detects_every_divergence(name):
    overrides, match = PURE_DIVERGENCES[name]
    with pytest.raises(TaxonomyVerificationError, match=match):
        boot._verify_rows(_fake_release(), _fake_rows(**overrides), SPEC, GOLDEN_V1_FINGERPRINT)


def test_pure_verification_detects_extra_and_doubled_memberships():
    rows = _fake_rows()
    extra = SimpleNamespace(id=uuid.uuid4(), **{**{f: SPEC_BY_CODE["C7_A"][f] for f in v1.PERSISTED_CAPABILITY_FIELDS},
                                                "semantic_revision": 2})
    with pytest.raises(TaxonomyVerificationError, match="46 memberships .*plusieurs révisions \\['C7_A'\\]"):
        boot._verify_rows(_fake_release(), rows + [(SimpleNamespace(), extra)], SPEC, GOLDEN_V1_FINGERPRINT)
    alien = SimpleNamespace(id=uuid.uuid4(), capability_code="C3_D")
    with pytest.raises(TaxonomyVerificationError, match="hors SPEC \\['C3_D'\\]"):
        boot._verify_rows(_fake_release(), rows + [(SimpleNamespace(), alien)], SPEC, GOLDEN_V1_FINGERPRINT)


def test_pure_verification_requires_both_fingerprint_and_content():
    """Un fingerprint correct ne prouve rien du contenu, et inversement."""
    with pytest.raises(TaxonomyVerificationError, match="spec_fingerprint"):
        boot._verify_rows(_fake_release(spec_fingerprint="0" * 64), _fake_rows(), SPEC, GOLDEN_V1_FINGERPRINT)
    with pytest.raises(TaxonomyVerificationError, match="C9_C"):
        boot._verify_rows(_fake_release(), _fake_rows(C9_C={"label": "x"}), SPEC, GOLDEN_V1_FINGERPRINT)
    with pytest.raises(TaxonomyVerificationError, match="version_key"):
        boot._verify_rows(_fake_release(version_key="oryx-v2"), _fake_rows(), SPEC, GOLDEN_V1_FINGERPRINT)
    with pytest.raises(TaxonomyVerificationError, match="statut"):
        boot._verify_rows(_fake_release(status="archived"), _fake_rows(), SPEC, GOLDEN_V1_FINGERPRINT)


def test_comparison_distinguishes_json_types():
    assert boot._canonical(1) != boot._canonical(True) != boot._canonical(1.0) != boot._canonical("1")
    assert boot._canonical({"a": 1, "b": [1, 2]}) == boot._canonical({"b": [1, 2], "a": 1})
    assert boot._canonical([1, 2]) != boot._canonical([2, 1])


# --- CLI sans base ----------------------------------------------------------

def test_cli_without_database_url_fails_clearly(monkeypatch, capsys):
    monkeypatch.setattr(core_db, "SessionLocal", None)
    assert cli.main([]) == 2
    assert cli.main(["--verify-only"]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL n'est pas défini" in err


def test_cli_module_entry_point_without_database_url():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    completed = subprocess.run([sys.executable, "-m", "scripts.bootstrap_pedagogical_taxonomy_v1"], cwd=REPO_ROOT,
                               env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 2
    assert "DATABASE_URL n'est pas défini" in completed.stderr and completed.stdout == ""


def test_cli_rejects_unknown_options():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--activate"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

def _counts(engine):  # noqa: F811
    return (_count(engine, "pedagogical_taxonomy_releases"), _count(engine, "core_capability_definitions"),
            _count(engine, "capability_taxonomy_memberships"))


def _session_counts(session):
    """Mêmes comptes, vus DE la transaction (écritures non commitées
    comprises)."""
    return tuple(session.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one()
                 for t in ("pedagogical_taxonomy_releases", "core_capability_definitions",
                           "capability_taxonomy_memberships"))


def _v1(engine):  # noqa: F811
    rows = _rows(engine, "SELECT * FROM pedagogical_taxonomy_releases WHERE version_key = 'oryx-v1'")
    return rows[0] if rows else None


def _v1_contents(engine):  # noqa: F811
    """{code: (revision, competency, label, definition, guidance)} des
    memberships commités de oryx-v1."""
    return {r["capability_code"]: (r["semantic_revision"], r["competency_code"], r["label"], r["definition"],
                                   r["mapping_guidance"])
            for r in _rows(engine, """
                SELECT d.* FROM capability_taxonomy_memberships m
                JOIN core_capability_definitions d ON d.id = m.capability_definition_id
                JOIN pedagogical_taxonomy_releases r ON r.id = m.taxonomy_release_id
                WHERE r.version_key = 'oryx-v1'""")}


def _expected_contents():
    return {code: (c["semantic_revision"], c["competency_code"], c["label"], c["definition"], c["mapping_guidance"])
            for code, c in SPEC_BY_CODE.items()}


def _definition_ids(engine):  # noqa: F811
    return {(r["capability_code"], r["semantic_revision"]): r["id"]
            for r in _rows(engine, "SELECT id, capability_code, semantic_revision FROM core_capability_definitions")}


def _bootstrap(Sessions):  # noqa: F811
    with Sessions() as session:
        result = bootstrap_taxonomy_v1(session)
        session.commit()
        return result


def _activate(Sessions):  # noqa: F811
    with Sessions() as session:
        result = activate_taxonomy_v1(session)
        session.commit()
        return result


def _spec_definition(Sessions, code, revision=1, **overrides):  # noqa: F811
    """Définition commitée via T4-B, contenu de la SPEC sauf overrides."""
    kwargs = {f: SPEC_BY_CODE[code][f] for f in v1.PERSISTED_CAPABILITY_FIELDS}
    kwargs.update(semantic_revision=revision, **overrides)
    with Sessions() as session:
        definition = tax.create_capability_definition(session, **kwargs)
        session.commit()
        return definition.id


def _partial_v1(Sessions, codes):  # noqa: F811
    """Release oryx-v1 au BON fingerprint, construite via T4-B avec
    seulement `codes` (définitions r1 conformes)."""
    ids = [_spec_definition(Sessions, code) for code in codes]
    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key="oryx-v1", spec_fingerprint=GOLDEN_V1_FINGERPRINT)
        for definition_id in ids:
            tax.attach_capability_to_release(session, release_id=release.id, capability_definition_id=definition_id)
        session.commit()
        return release.id


def _raw(engine, sql, **params):  # noqa: F811
    """Écriture hors service (corruption volontaire)."""
    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def _raw_extra_membership(engine, Sessions):  # noqa: F811
    """C7_A r2 (via T4-B) glissée en SQL brut dans oryx-v1."""
    r2 = _spec_definition(Sessions, "C7_A", revision=2, definition="sens révisé (test)")
    _raw(engine, "INSERT INTO capability_taxonomy_memberships (id, taxonomy_release_id, capability_definition_id, "
                 "created_at) SELECT :id, id, :d, now() FROM pedagogical_taxonomy_releases "
                 "WHERE version_key = 'oryx-v1'", id=uuid.uuid4(), d=r2)


def _writes(statements):
    return [sql for sql, _ in statements if not sql.startswith(("SELECT", "SAVEPOINT", "RELEASE SAVEPOINT"))]


def _record(engine):  # noqa: F811
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: sa.event.remove(engine, "before_cursor_execute", record)


class _Spy:
    """Enveloppe tax.activate_release pour compter les appels réels à T4-B."""

    def __init__(self, monkeypatch, after=None):
        self.calls = 0
        original = tax.activate_release

        def spy(db, *, release_id):
            self.calls += 1
            release = original(db, release_id=release_id)
            if after:
                after(db)
            return release

        monkeypatch.setattr(tax, "activate_release", spy)


# --- bootstrap ----------------------------------------------------------------

def test_pg_fresh_head_has_no_v1(engine, db):  # noqa: F811
    assert _counts(engine) == (0, 0, 0)
    with pytest.raises(TaxonomyReleaseNotFound, match="oryx-v1"):
        verify_taxonomy_v1(db)


def test_pg_first_bootstrap_creates_the_complete_candidate_v1(engine, Sessions, db):  # noqa: F811
    result = bootstrap_taxonomy_v1(db)
    assert isinstance(result, TaxonomyBootstrapResult)
    assert (result.version_key, result.spec_fingerprint, result.status, result.capability_count) == (
        "oryx-v1", GOLDEN_V1_FINGERPRINT, "candidate", 45)
    assert (result.created_release, result.created_definitions, result.reused_definitions,
            result.created_memberships) == (True, 45, 0, 45)
    assert _session_counts(db) == (1, 45, 45)
    assert _counts(engine) == (0, 0, 0)  # rien de commité : l'appelant possède la transaction
    db.commit()

    assert _counts(engine) == (1, 45, 45)
    release = _v1(engine)
    assert release["id"] == result.release_id and release["status"] == "candidate"
    assert release["activated_at"] is None and release["spec_fingerprint"] == GOLDEN_V1_FINGERPRINT
    assert _v1_contents(engine) == _expected_contents()
    assert tax.get_active_release(db) is None  # bootstrap != activation
    pairs = tax.get_release_capabilities(db, release_id=result.release_id)
    assert [d.capability_code for _, d in pairs] == list(v1.CAPABILITY_CODES)
    assert verify_taxonomy_v1(db) == TaxonomyVerificationResult(
        result.release_id, "oryx-v1", GOLDEN_V1_FINGERPRINT, "candidate", 45)


def test_pg_unicode_is_stored_exactly(engine, Sessions):  # noqa: F811
    _bootstrap(Sessions)
    row = _rows(engine, "SELECT label, definition, mapping_guidance FROM core_capability_definitions "
                        "WHERE capability_code = 'C9_B'")[0]
    assert row["label"] == "Liquidité, service de la dette & échéances"
    assert row["mapping_guidance"] == SPEC_BY_CODE["C9_B"]["mapping_guidance"]
    raw = _rows(engine, "SELECT label FROM core_capability_definitions WHERE label = :l",
                l="Nature économique de l'investissement")
    assert len(raw) == 1


def test_pg_second_bootstrap_is_a_semantic_no_op(engine, Sessions, db):  # noqa: F811
    first = _bootstrap(Sessions)
    before = (_v1(engine), _definition_ids(engine), _rows(engine, "SELECT * FROM capability_taxonomy_memberships"))
    statements, stop = _record(engine)
    try:
        second = bootstrap_taxonomy_v1(db)
    finally:
        stop()
    db.commit()
    assert _writes(statements) == []
    assert (second.created_release, second.created_definitions, second.reused_definitions,
            second.created_memberships) == (False, 0, 0, 0)
    assert (second.release_id, second.status, second.capability_count) == (first.release_id, "candidate", 45)
    assert _counts(engine) == (1, 45, 45)
    after = (_v1(engine), _definition_ids(engine), _rows(engine, "SELECT * FROM capability_taxonomy_memberships"))
    assert after == before


def test_pg_bootstrap_reuses_identical_existing_definitions(engine, Sessions):  # noqa: F811
    existing = {code: _spec_definition(Sessions, code) for code in ("C7_A", "C12_D", "C1_A")}
    result = _bootstrap(Sessions)
    assert (result.created_definitions, result.reused_definitions, result.created_memberships) == (42, 3, 45)
    ids = _definition_ids(engine)
    assert len(ids) == 45
    assert all(ids[(code, 1)] == definition_id for code, definition_id in existing.items())
    attached = {r["capability_definition_id"] for r in _rows(engine, "SELECT capability_definition_id "
                                                                     "FROM capability_taxonomy_memberships")}
    assert set(existing.values()) <= attached


def test_pg_other_revisions_are_ignored_v2_preparation(engine, Sessions):  # noqa: F811
    """Une C7_B r2 (future V2) n'interfère pas : V1 attache C7_B r1."""
    r2 = _spec_definition(Sessions, "C7_B", revision=2, definition="sens révisé (future V2)")
    _bootstrap(Sessions)
    assert _counts(engine) == (1, 46, 45)
    assert _v1_contents(engine)["C7_B"][0] == 1
    assert _definition_ids(engine)[("C7_B", 2)] == r2


CONFLICTS = {
    "label": {"label": "Résultat comptable ou cash"},
    "definition": {"definition": SPEC_BY_CODE["C7_A"]["definition"].rstrip(".")},
    "include_order": {"mapping_guidance": {**SPEC_BY_CODE["C7_A"]["mapping_guidance"],
                                           "include": SPEC_BY_CODE["C7_A"]["mapping_guidance"]["include"][::-1]}},
    "boundary_notes_empty": {"mapping_guidance": {**SPEC_BY_CODE["C7_A"]["mapping_guidance"],
                                                  "boundary_notes": []}},
    "t4b_only_guidance": {"mapping_guidance": {"notes": ["test"]}},
}


@pytest.mark.parametrize("name", sorted(CONFLICTS))
def test_pg_conflicting_definition_fails_before_any_write(engine, Sessions, db, name):  # noqa: F811
    conflicting = _spec_definition(Sessions, "C7_A", **CONFLICTS[name])
    before = _rows(engine, "SELECT * FROM core_capability_definitions")
    statements, stop = _record(engine)
    try:
        with pytest.raises(TaxonomyBootstrapConflict, match=f"C7_A révision 1 déjà persistée \\({conflicting}\\)"):
            bootstrap_taxonomy_v1(db)
    finally:
        stop()
    assert _writes(statements) == []
    assert _session_counts(db) == (0, 1, 0)
    db.rollback()
    assert _rows(engine, "SELECT * FROM core_capability_definitions") == before  # jamais réécrite


def test_pg_existing_v1_with_another_fingerprint_is_a_conflict(engine, Sessions, db):  # noqa: F811
    with Sessions() as session:
        tax.create_candidate_release(session, version_key="oryx-v1", spec_fingerprint="sha256:autre-contenu")
        session.commit()
    with pytest.raises(TaxonomyBootstrapConflict, match="sha256:autre-contenu"):
        bootstrap_taxonomy_v1(db)
    assert _session_counts(db) == (1, 0, 0)
    db.rollback()
    assert _v1(engine)["spec_fingerprint"] == "sha256:autre-contenu"  # jamais modifié
    with pytest.raises(TaxonomyVerificationError, match="spec_fingerprint"):
        verify_taxonomy_v1(db)


def test_pg_golden_fingerprint_under_another_version_key_is_a_conflict(engine, Sessions, db):  # noqa: F811
    with Sessions() as session:
        tax.create_candidate_release(session, version_key="oryx-v1-copie", spec_fingerprint=GOLDEN_V1_FINGERPRINT)
        session.commit()
    with pytest.raises(TaxonomyBootstrapConflict, match="oryx-v1-copie"):
        bootstrap_taxonomy_v1(db)
    assert _session_counts(db) == (1, 0, 0)


@pytest.mark.parametrize("present", [0, 1, 44])
def test_pg_partial_v1_is_never_repaired(engine, Sessions, db, present):  # noqa: F811
    codes = v1.CAPABILITY_CODES[:present]
    _partial_v1(Sessions, codes)
    before = _counts(engine)
    statements, stop = _record(engine)
    try:
        with pytest.raises(TaxonomyVerificationError, match=f"{present} memberships pour 45"):
            bootstrap_taxonomy_v1(db)
    finally:
        stop()
    assert _writes(statements) == []
    db.rollback()
    assert _counts(engine) == before == (1, present, present)


def test_pg_v1_with_an_extra_membership_is_rejected(engine, Sessions, db):  # noqa: F811
    _bootstrap(Sessions)
    _raw_extra_membership(engine, Sessions)
    with pytest.raises(TaxonomyVerificationError, match="46 memberships .*plusieurs révisions \\['C7_A'\\]"):
        bootstrap_taxonomy_v1(db)
    db.rollback()
    assert _counts(engine) == (1, 46, 46)


def test_pg_active_conform_v1_bootstrap_is_idempotent(engine, Sessions, db):  # noqa: F811
    _bootstrap(Sessions)
    _activate(Sessions)
    before = (_v1(engine), _definition_ids(engine))
    statements, stop = _record(engine)
    try:
        result = bootstrap_taxonomy_v1(db)
    finally:
        stop()
    db.commit()
    assert _writes(statements) == []
    assert (result.status, result.created_release, result.capability_count) == ("active", False, 45)
    assert (_v1(engine), _definition_ids(engine)) == before


def test_pg_retired_v1_is_never_reactivated_or_modified(engine, Sessions, db):  # noqa: F811
    _bootstrap(Sessions)
    _activate(Sessions)
    newer = _release(Sessions)
    with Sessions() as session:
        tax.activate_release(session, release_id=newer)
        session.commit()
    retired = _v1(engine)
    assert retired["status"] == "retired" and retired["activated_at"] is not None
    statements, stop = _record(engine)
    try:
        result = bootstrap_taxonomy_v1(db)
        assert result.status == "retired" and not result.created_release
        assert verify_taxonomy_v1(db).status == "retired"
        with pytest.raises(InvalidTaxonomyState, match="retired"):
            activate_taxonomy_v1(db)
    finally:
        stop()
    db.commit()
    assert _writes(statements) == []
    assert _v1(engine) == retired and _releases(engine)[newer][0] == "active"


def test_pg_bootstrap_leaves_an_existing_active_release_untouched(engine, Sessions, db):  # noqa: F811
    other = _release(Sessions, status="active")
    before = _release_row(engine, other)
    bootstrap_taxonomy_v1(db)
    db.commit()
    assert _release_row(engine, other) == before
    assert tax.get_active_release(db).id == other


def test_pg_caller_rollback_after_first_bootstrap_persists_nothing(engine, Sessions, db):  # noqa: F811
    bootstrap_taxonomy_v1(db)
    verify_taxonomy_v1(db)
    db.rollback()
    assert _counts(engine) == (0, 0, 0)
    assert _bootstrap(Sessions).created_release  # et un nouveau bootstrap repart de zéro


# --- verify -------------------------------------------------------------------

def test_pg_verify_writes_nothing(engine, Sessions, db):  # noqa: F811
    _bootstrap(Sessions)
    statements, stop = _record(engine)
    try:
        result = verify_taxonomy_v1(db)
    finally:
        stop()
    assert all(sql.startswith("SELECT") for sql, _ in statements)
    assert not (db.new or db.dirty or db.deleted)
    assert result.status == "candidate" and result.capability_count == 45
    db.rollback()
    with Sessions() as session:  # et réussit dans une transaction READ ONLY
        session.execute(sa.text("SET TRANSACTION READ ONLY"))
        assert verify_taxonomy_v1(session) == result
        session.rollback()


def _everywhere(error, match):
    return (error, match), (error, match), (error, match)


# nom -> (SQL brut, attendu pour verify, bootstrap, activate).
CORRUPTIONS = {
    "missing_membership": (
        "DELETE FROM capability_taxonomy_memberships WHERE capability_definition_id = "
        "(SELECT id FROM core_capability_definitions WHERE capability_code = 'C4_B')",
        *_everywhere(TaxonomyVerificationError, "manquantes \\['C4_B'\\]")),
    "wrong_definition": (
        "UPDATE core_capability_definitions SET definition = definition || ' ' WHERE capability_code = 'C7_A'",
        *_everywhere(TaxonomyVerificationError, "C7_A .*definition")),
    "wrong_label": (
        "UPDATE core_capability_definitions SET label = 'Nature economique de l''investissement' "
        "WHERE capability_code = 'C1_A'",
        *_everywhere(TaxonomyVerificationError, "C1_A .*label")),
    "wrong_mapping_guidance": (
        "UPDATE core_capability_definitions SET mapping_guidance = jsonb_set(mapping_guidance, '{include}', "
        "(mapping_guidance->'include') || CAST('[\"critère ajouté\"]' AS JSONB)) WHERE capability_code = 'C10_D'",
        *_everywhere(TaxonomyVerificationError, "C10_D .*mapping_guidance")),
    "wrong_boundary_notes": (
        "UPDATE core_capability_definitions SET mapping_guidance = jsonb_set(mapping_guidance, '{boundary_notes}', "
        "CAST('[]' AS JSONB)) WHERE capability_code = 'C12_B'",
        *_everywhere(TaxonomyVerificationError, "C12_B .*mapping_guidance")),
    "wrong_guidance_type": (
        "UPDATE core_capability_definitions SET mapping_guidance = jsonb_set(mapping_guidance, '{exclude}', "
        "CAST('[\"CAPEX.\", 1]' AS JSONB)) WHERE capability_code = 'C7_B'",
        *_everywhere(TaxonomyVerificationError, "C7_B .*mapping_guidance")),
    "wrong_fingerprint": (
        "UPDATE pedagogical_taxonomy_releases SET spec_fingerprint = 'sha256:falsifie' WHERE version_key = 'oryx-v1'",
        (TaxonomyVerificationError, "spec_fingerprint"), (TaxonomyBootstrapConflict, "sha256:falsifie"),
        (TaxonomyVerificationError, "spec_fingerprint")),
    "wrong_version_key": (
        "UPDATE pedagogical_taxonomy_releases SET version_key = 'oryx-v1b' WHERE version_key = 'oryx-v1'",
        (TaxonomyReleaseNotFound, "oryx-v1"), (TaxonomyBootstrapConflict, "oryx-v1b"),
        (TaxonomyReleaseNotFound, "oryx-v1")),
}


@pytest.mark.parametrize("name", sorted(CORRUPTIONS))
def test_pg_verify_bootstrap_and_activate_detect_every_corruption(engine, Sessions, db, name):  # noqa: F811
    """Une V1 corrompue hors service est détectée par les trois opérations,
    sans aucune écriture : jamais réparée, jamais activée."""
    _bootstrap(Sessions)
    sql, *expectations = CORRUPTIONS[name]
    _raw(engine, sql)
    snapshot = lambda: (_counts(engine), _rows(engine, "SELECT * FROM pedagogical_taxonomy_releases"),  # noqa: E731
                        _rows(engine, "SELECT * FROM core_capability_definitions"))
    before = snapshot()
    statements, stop = _record(engine)
    try:
        for operation, (error, match) in zip((verify_taxonomy_v1, bootstrap_taxonomy_v1, activate_taxonomy_v1),
                                             expectations):
            with pytest.raises(error, match=match):
                operation(db)
    finally:
        stop()
    assert _writes(statements) == []
    db.rollback()
    assert snapshot() == before


def test_pg_wrong_revision_in_v1_is_detected(engine, Sessions, db):  # noqa: F811
    """oryx-v1 dont C7_A pointe (SQL brut) vers une r2 au contenu
    identique : la révision seule suffit à refuser."""
    _bootstrap(Sessions)
    r2 = _spec_definition(Sessions, "C7_A", revision=2)
    _raw(engine, "UPDATE capability_taxonomy_memberships SET capability_definition_id = :r2 "
                 "WHERE capability_definition_id = (SELECT id FROM core_capability_definitions "
                 "WHERE capability_code = 'C7_A' AND semantic_revision = 1)", r2=r2)
    with pytest.raises(TaxonomyVerificationError, match="C7_A .*semantic_revision"):
        verify_taxonomy_v1(db)
    with pytest.raises(TaxonomyVerificationError, match="semantic_revision"):
        activate_taxonomy_v1(db)


def test_pg_wrong_competency_cannot_even_be_persisted(engine, Sessions):  # noqa: F811
    """La compétence est garantie par le CHECK de 0007 (C7_A => C7) ;
    verify la contrôle quand même (voir le test pur wrong_competency)."""
    _bootstrap(Sessions)
    with pytest.raises(sa.exc.IntegrityError, match="ck_core_capability_definitions"):
        _raw(engine, "UPDATE core_capability_definitions SET competency_code = 'C8' WHERE capability_code = 'C7_A'")


def test_pg_extra_membership_is_detected_by_verify(engine, Sessions, db):  # noqa: F811
    _bootstrap(Sessions)
    _raw_extra_membership(engine, Sessions)
    with pytest.raises(TaxonomyVerificationError, match="46 memberships"):
        verify_taxonomy_v1(db)


# --- activation -----------------------------------------------------------------

def test_pg_activate_exact_candidate_through_t4b(engine, Sessions, db, monkeypatch):  # noqa: F811
    _bootstrap(Sessions)
    spy = _Spy(monkeypatch)
    result = activate_taxonomy_v1(db)
    assert spy.calls == 1
    assert isinstance(result, TaxonomyActivationResult)
    assert (result.status, result.activated, result.capability_count) == ("active", True, 45)
    assert _v1(engine)["status"] == "candidate"  # rien de commité
    db.commit()
    release = _v1(engine)
    assert release["status"] == "active" and release["activated_at"] is not None
    assert tax.get_active_release(db).id == result.release_id


@pytest.mark.parametrize("present", [0, 44])
def test_pg_incomplete_candidate_is_refused_before_t4b(engine, Sessions, db, monkeypatch, present):  # noqa: F811
    """T4-B activerait une candidate vide ; T4-C refuse avant de l'appeler."""
    _partial_v1(Sessions, v1.CAPABILITY_CODES[:present])
    spy = _Spy(monkeypatch)
    with pytest.raises(TaxonomyVerificationError, match=f"{present} memberships pour 45"):
        activate_taxonomy_v1(db)
    assert spy.calls == 0
    db.rollback()
    assert _v1(engine)["status"] == "candidate" and tax.get_active_release(db) is None


def test_pg_divergent_candidate_is_refused_before_t4b(engine, Sessions, db, monkeypatch):  # noqa: F811
    _bootstrap(Sessions)
    _raw(engine, "UPDATE core_capability_definitions SET label = 'x' WHERE capability_code = 'C5_C'")
    spy = _Spy(monkeypatch)
    with pytest.raises(TaxonomyVerificationError, match="C5_C"):
        activate_taxonomy_v1(db)
    assert spy.calls == 0


def test_pg_activate_already_active_v1_is_idempotent(engine, Sessions, db, monkeypatch):  # noqa: F811
    _bootstrap(Sessions)
    first = _activate(Sessions)
    before = _v1(engine)
    spy = _Spy(monkeypatch)
    statements, stop = _record(engine)
    try:
        again = activate_taxonomy_v1(db)
    finally:
        stop()
    db.commit()
    assert spy.calls == 0 and _writes(statements) == []
    assert (again.release_id, again.status, again.activated) == (first.release_id, "active", False)
    assert _v1(engine) == before
    # Le comportement générique de T4-B est inchangé : il refuse toujours.
    with pytest.raises(InvalidTaxonomyState):
        tax.activate_release(db, release_id=first.release_id)


def test_pg_activation_retires_the_previous_active_through_t4b(engine, Sessions, db):  # noqa: F811
    previous = _release(Sessions, status="active")
    previous_activated_at = _release_row(engine, previous)["activated_at"]
    _bootstrap(Sessions)
    result = activate_taxonomy_v1(db)
    db.commit()
    releases = _releases(engine)
    assert releases[previous] == ("retired", previous_activated_at)
    assert releases[result.release_id][0] == "active"
    assert [s for s, _ in releases.values()].count("active") == 1


def test_pg_caller_rollback_cancels_the_v1_activation(engine, Sessions, db):  # noqa: F811
    previous = _release(Sessions, status="active")
    _bootstrap(Sessions)
    activate_taxonomy_v1(db)
    db.rollback()
    assert _v1(engine)["status"] == "candidate" and _releases(engine)[previous][0] == "active"


def test_pg_activate_missing_v1(engine, db):  # noqa: F811
    with pytest.raises(TaxonomyReleaseNotFound, match="oryx-v1"):
        activate_taxonomy_v1(db)


def test_pg_activation_is_rechecked_under_the_t4b_release_lock(engine, Sessions, db, monkeypatch):  # noqa: F811
    """Test de mutation : un membership glissé (hors service) entre le
    contrôle préalable et le verrou de T4-B est détecté par la
    revérification ; l'appelant annule, l'activation aussi."""
    _bootstrap(Sessions)
    r2 = _spec_definition(Sessions, "C7_A", revision=2)

    def sneak(session):
        session.execute(sa.text("INSERT INTO capability_taxonomy_memberships (id, taxonomy_release_id, "
                                "capability_definition_id, created_at) SELECT :id, id, :d, now() "
                                "FROM pedagogical_taxonomy_releases WHERE version_key = 'oryx-v1'"),
                        {"id": uuid.uuid4(), "d": r2})

    spy = _Spy(monkeypatch, after=sneak)
    with pytest.raises(TaxonomyVerificationError, match="46 memberships"):
        activate_taxonomy_v1(db)
    assert spy.calls == 1
    db.rollback()
    assert _v1(engine)["status"] == "candidate" and _counts(engine) == (1, 46, 45)


def test_pg_concurrent_v1_activations_end_idempotently(engine, Sessions):  # noqa: F811
    """La seconde activation attend le verrou consultatif de T4-B, observe
    V1 active sous verrou et retourne le résultat idempotent."""
    _bootstrap(Sessions)
    with Sessions() as first, Sessions() as second:
        assert activate_taxonomy_v1(first).activated
        result, error = _blocked(engine, first, second, activate_taxonomy_v1, waiting_on=ON_ADVISORY)
    assert error is None and (result.status, result.activated) == ("active", False)
    assert _v1(engine)["status"] == "active"


# --- doctrine sous la vraie V1 --------------------------------------------------

def test_pg_one_observation_localized_on_two_c7_capabilities_stays_one_proof(engine, Sessions, db):  # noqa: F811
    """Sous oryx-v1 active : O(C7) -> C7_A + C7_B reste UNE observation ;
    C8_A est refusé (jamais cross-competency) ; competency_only n'invente
    aucun mapping."""
    release_id = _bootstrap(Sessions).release_id
    _activate(Sessions)
    memberships = {d.capability_code: m.id for m, d in tax.get_release_capabilities(db, release_id=release_id)}
    run_id, (observation, unlocated) = _run(Sessions, release_id, {}, {"competency_code": "C10",
                                                                       "capability_localization": "competency_only"})
    _map(Sessions, observation, memberships["C7_A"])
    _map(Sessions, observation, memberships["C7_B"])
    with pytest.raises(tax.InvalidCapabilityMapping, match="C8_A"):
        tax.map_observation_capability(db, observation_id=observation, capability_membership_id=memberships["C8_A"])
    db.rollback()
    with pytest.raises(tax.InvalidCapabilityMapping):
        tax.map_observation_capability(db, observation_id=unlocated, capability_membership_id=memberships["C10_A"])
    db.rollback()
    _complete(Sessions, run_id)
    assert _count(engine, "pedagogical_observations") == 2
    assert _count(engine, "observation_capabilities") == 2
    assert [c.capability_code for _, _, c in tax.get_observation_capabilities(db, observation_id=observation)] == [
        "C7_A", "C7_B"]


def test_pg_runs_without_release_are_unchanged_by_v1(engine, Sessions):  # noqa: F811
    _bootstrap(Sessions)
    _activate(Sessions)
    run_id, _ = _run(Sessions, None, {"capability_localization": "localized"})
    _complete(Sessions, run_id)  # historique T3-B : aucun contrôle de mapping
    assert _rows(engine, "SELECT pedagogical_taxonomy_release_id FROM observation_evaluation_runs "
                         "WHERE id = :r", r=run_id) == [{"pedagogical_taxonomy_release_id": None}]


# --- concurrence du bootstrap ---------------------------------------------------

def _assert_final_v1(engine):  # noqa: F811
    assert _counts(engine) == (1, 45, 45)
    assert _rows(engine, "SELECT capability_code, semantic_revision FROM core_capability_definitions "
                         "GROUP BY 1, 2 HAVING count(*) > 1") == []
    assert _v1(engine)["spec_fingerprint"] == GOLDEN_V1_FINGERPRINT
    assert _v1_contents(engine) == _expected_contents()


def _blocked_then(engine, holder, waiter, action, *, waiting_on, end):  # noqa: F811
    """Comme _blocked (T3-B), mais le détenteur termine par `end` (commit
    ou rollback) une fois `action(waiter)` réellement bloquée sur une
    requête commençant par `waiting_on`."""
    holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
    outcome = {}

    def work():
        try:
            outcome["result"] = action(waiter)
            waiter.commit()
        except Exception as exc:  # noqa: BLE001 — restitué au test
            outcome["error"] = exc
            waiter.rollback()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        _wait_until_blocked_by(engine, waiter_pid, holder_pid)
        query = _rows(engine, "SELECT query FROM pg_stat_activity WHERE pid = :p", p=waiter_pid)[0]["query"]
        assert " ".join(query.split()).startswith(waiting_on), (waiting_on, query)
        assert not outcome
        getattr(holder, end)()
    finally:
        if thread.is_alive() and holder.in_transaction():
            holder.rollback()
        thread.join(timeout=30)
    assert not thread.is_alive()
    return outcome.get("result"), outcome.get("error")


def test_pg_concurrent_bootstrap_waits_on_the_first_definition_then_is_a_no_op(engine, Sessions):  # noqa: F811
    """A construit V1 sans commiter ; B attend sur l'index unique de C1_A r1
    (savepoint T4-B), puis, A commité, relit et réutilise tout : NO-OP."""
    with Sessions() as first, Sessions() as second:
        assert bootstrap_taxonomy_v1(first).created_release
        result, error = _blocked_then(engine, first, second, bootstrap_taxonomy_v1,
                                      waiting_on=ON_DEFINITION_INSERT, end="commit")
    assert error is None
    assert (result.created_release, result.created_definitions, result.created_memberships) == (False, 0, 0)
    assert result.status == "candidate" and result.capability_count == 45
    _assert_final_v1(engine)


def test_pg_concurrent_bootstrap_builds_v1_when_the_first_rolls_back(engine, Sessions):  # noqa: F811
    with Sessions() as first, Sessions() as second:
        bootstrap_taxonomy_v1(first)
        result, error = _blocked_then(engine, first, second, bootstrap_taxonomy_v1,
                                      waiting_on=ON_DEFINITION_INSERT, end="rollback")
    assert error is None
    assert (result.created_release, result.created_definitions, result.created_memberships) == (True, 45, 45)
    _assert_final_v1(engine)


def test_pg_concurrent_bootstrap_over_existing_definitions_waits_on_the_release(engine, Sessions):  # noqa: F811
    """Les 45 définitions existent déjà : B attend sur l'index unique de la
    release, puis DuplicateTaxonomyRelease => relecture stricte : NO-OP."""
    for code in v1.CAPABILITY_CODES:
        _spec_definition(Sessions, code)
    with Sessions() as first, Sessions() as second:
        assert bootstrap_taxonomy_v1(first).reused_definitions == 45
        result, error = _blocked_then(engine, first, second, bootstrap_taxonomy_v1,
                                      waiting_on=ON_RELEASE_INSERT, end="commit")
    assert error is None and not result.created_release and result.capability_count == 45
    _assert_final_v1(engine)


def test_pg_concurrent_conflicting_definition_is_never_absorbed(engine, Sessions):  # noqa: F811
    """Un écrivain concurrent crée C1_A r1 avec un AUTRE contenu : le
    bootstrap attend, relit, détecte le conflit et échoue ; aucune V1."""
    with Sessions() as writer, Sessions() as bootstrapper:
        tax.create_capability_definition(writer, **{**{f: SPEC_BY_CODE["C1_A"][f]
                                                       for f in v1.PERSISTED_CAPABILITY_FIELDS},
                                                    "label": "autre sens"})
        _, error = _blocked_then(engine, writer, bootstrapper, bootstrap_taxonomy_v1,
                                 waiting_on=ON_DEFINITION_INSERT, end="commit")
    assert isinstance(error, TaxonomyBootstrapConflict) and "C1_A" in str(error)
    assert _counts(engine) == (0, 1, 0)


def test_pg_many_concurrent_bootstraps_converge_to_one_v1(engine, Sessions):  # noqa: F811
    workers = 6
    barrier = threading.Barrier(workers)
    results, errors = [], []

    def work():
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                result = bootstrap_taxonomy_v1(session)
                verify_taxonomy_v1(session)
                session.commit()
                results.append(result)
        except Exception as exc:  # noqa: BLE001 — restitué au test
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [] and len(results) == workers
    assert sum(r.created_release for r in results) == 1
    assert sum(r.created_definitions for r in results) == 45
    assert len({r.release_id for r in results}) == 1
    _assert_final_v1(engine)


# --- CLI ------------------------------------------------------------------------

def test_pg_cli_bootstraps_verifies_and_commits_without_activating(engine, Sessions, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(core_db, "SessionLocal", Sessions)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "version_key: oryx-v1" in out and f"spec_fingerprint: {GOLDEN_V1_FINGERPRINT}" in out
    assert "capability_count: 45" in out and "status: candidate" in out and "bootstrap: created" in out
    _assert_final_v1(engine)
    assert _v1(engine)["status"] == "candidate" and _v1(engine)["activated_at"] is None
    assert cli.main([]) == 0
    assert "bootstrap: unchanged" in capsys.readouterr().out
    _assert_final_v1(engine)


def test_pg_cli_verify_only(engine, Sessions, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(core_db, "SessionLocal", Sessions)
    assert cli.main(["--verify-only"]) == 1
    assert "TaxonomyReleaseNotFound" in capsys.readouterr().err
    assert _counts(engine) == (0, 0, 0)
    _bootstrap(Sessions)
    assert cli.main(["--verify-only"]) == 0
    out = capsys.readouterr().out
    assert "VERIFY OK" in out and "status: candidate" in out and "bootstrap:" not in out


def test_pg_cli_rolls_back_everything_on_error(engine, Sessions, monkeypatch, capsys):  # noqa: F811
    _spec_definition(Sessions, "C12_D", label="autre sens")
    monkeypatch.setattr(core_db, "SessionLocal", Sessions)
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "TaxonomyBootstrapConflict" in err and "C12_D" in err and "rollback" in err
    assert _counts(engine) == (0, 1, 0)


def test_pg_cli_hides_database_error_details(engine, Sessions, monkeypatch, capsys):  # noqa: F811
    def broken(db):
        db.execute(sa.text("SELECT * FROM table_inexistante_secret"))

    monkeypatch.setattr(cli, "bootstrap_taxonomy_v1", broken)
    monkeypatch.setattr(core_db, "SessionLocal", Sessions)
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "ProgrammingError" in err and "table_inexistante_secret" not in err


def test_pg_cli_module_entry_point_end_to_end(engine, pg_url):  # noqa: F811
    env = {**os.environ, "DATABASE_URL": pg_url}
    command = [sys.executable, "-m", "scripts.bootstrap_pedagogical_taxonomy_v1"]
    try:
        first = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert first.returncode == 0, first.stderr
        assert f"spec_fingerprint: {GOLDEN_V1_FINGERPRINT}" in first.stdout and "status: candidate" in first.stdout
        verified = subprocess.run(command + ["--verify-only"], cwd=REPO_ROOT, env=env, capture_output=True,
                                  text=True, timeout=120)
        assert verified.returncode == 0 and "VERIFY OK" in verified.stdout
        _assert_final_v1(engine)
    finally:
        with engine.begin() as conn:
            for table in ("capability_taxonomy_memberships", "core_capability_definitions",
                          "pedagogical_taxonomy_releases"):
                conn.execute(sa.text(f"DELETE FROM {table}"))
