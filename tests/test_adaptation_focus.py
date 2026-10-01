"""Tests de l'étape 6.1B1 : contrats, catalogue sémantique courant et
validateur déterministe du focus de compétence (core/adaptation_focus.py).

1. Tests sans base (toujours exécutés) :
   - catalogue : registre de policies fail-closed, validation stricte
     SPEC <-> lignes de la release (lectures T4-B simulées par des lignes
     factices construites depuis la SPEC canonique), ordres canoniques,
     definition_id issus de la base, immutabilité ;
   - proposition : versions, statuts de résolution, scopes, tokens
     sémantiques stricts, cible / supports, ordres, pureté, déterminisme ;
   - constitution : aucune lecture 6-1A / Step 5, aucun LLM, aucun runtime
     chat, aucun routage, aucune écriture, aucun modèle, aucune migration,
     aucune brique 6-1B2 / 6-1C, aucun score.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T4-B / T4-C) :
   uniquement si ORYX_TEST_DATABASE_URL vise une base DÉDIÉE dont le nom
   contient "test" (schéma public détruit et recréé), sinon SKIPPÉS ; rien
   n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_focus_test \\
           python -m pytest tests/test_adaptation_focus.py

   SPEC V1 -> bootstrap + activation réels (T4-C) -> load_current_focus_taxonomy
   -> FocusProposal -> validate_focus_proposal -> definition_id exacts de
   PostgreSQL ; même capability_code sous une autre révision / un autre
   definition_id : aucun remapping ; aucune mutation de la base.
"""
import ast
import copy
import inspect
import json
import random
import uuid
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from types import MappingProxyType, SimpleNamespace

import pytest
import sqlalchemy as sa

from core import adaptation_focus as af
from core.adaptation_focus import (
    COMPETENCY_ONLY,
    FOCUS_POLICY_VERSION,
    FOCUS_SCHEMA_VERSION,
    LOCALIZED,
    CompetencyFocus,
    CurrentFocusTaxonomy,
    FocusCapabilityDefinition,
    FocusCompetencyDefinition,
    FocusError,
    FocusProposal,
    FocusTaxonomyUnavailable,
    InteractionCompetencyFocus,
    InvalidFocusArgument,
    InvalidFocusProposal,
    InvalidFocusTaxonomy,
    ProposedCompetencyFocus,
    UnsupportedFocusPolicy,
    load_current_focus_taxonomy,
    validate_focus_proposal,
)
from core.pedagogy import taxonomy_v1 as v1
from core.taxonomy_service import InvalidTaxonomyState, TaxonomyServiceError
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_taxonomy_service import (
    _rows,
    db,  # noqa: F401 — fixture
    engine,  # noqa: F401 — fixture
    Sessions,  # noqa: F401 — fixture
)
from tests.test_taxonomy_v1 import GOLDEN_V1_FINGERPRINT

MODULE_PATH = REPO_ROOT / "core" / "adaptation_focus.py"
PUBLIC_FUNCTIONS = {"load_current_focus_taxonomy", "validate_focus_proposal"}
PUBLIC_DATACLASSES = {
    FocusCompetencyDefinition: ["competency_code", "label", "central_question"],
    FocusCapabilityDefinition: ["definition_id", "capability_code", "semantic_revision", "competency_code", "label",
                                "definition", "mapping_guidance"],
    CurrentFocusTaxonomy: ["taxonomy_release_id", "taxonomy_schema_version", "taxonomy_version_key",
                           "taxonomy_spec_fingerprint", "focus_policy_version", "competencies", "capabilities"],
    ProposedCompetencyFocus: ["competency_code", "scope_mode", "capability_tokens"],
    FocusProposal: ["schema_version", "policy_version", "resolution_status", "target", "supporting"],
    CompetencyFocus: ["competency_code", "scope_mode", "capability_definition_ids"],
    InteractionCompetencyFocus: ["schema_version", "policy_version", "taxonomy_release_id",
                                 "taxonomy_spec_fingerprint", "resolution_status", "target", "supporting"],
}
EXCEPTIONS = {FocusError, InvalidFocusArgument, FocusTaxonomyUnavailable, UnsupportedFocusPolicy,
              InvalidFocusTaxonomy, InvalidFocusProposal}
SPEC = v1.taxonomy_v1_spec()
SPEC_BY_CODE = {c["capability_code"]: c for c in SPEC["capabilities"]}
NATURAL_CAPABILITIES = list(v1.CAPABILITY_CODES)
NATURAL_COMPETENCIES = [f"C{i}" for i in range(1, 13)]


# --------------------------------------------------------------------------
# Doublures : lectures T4-B simulées (lignes construites depuis la SPEC)
# --------------------------------------------------------------------------

class _ReadOnlyDB:
    """Session factice : seul no_autoflush est autorisé ; tout autre accès
    (add, flush, commit, execute...) échoue."""

    def __init__(self):
        self.autoflush_blocks = 0

    @property
    @contextmanager
    def no_autoflush(self):
        self.autoflush_blocks += 1
        yield

    def __getattr__(self, name):
        raise AssertionError(f"accès à la session inattendu : {name}")


def _fake_release(version_key="oryx-v1", spec_fingerprint=GOLDEN_V1_FINGERPRINT, release_id=None):
    return SimpleNamespace(id=release_id or uuid.uuid4(), version_key=version_key,
                           spec_fingerprint=spec_fingerprint, status="active")


def _fake_rows(release, codes=None, **overrides):
    """[(membership, définition)] conformes à la SPEC (definition_id
    aléatoires, comme en base) ; overrides = {code: {champ: valeur}}."""
    rows = []
    for code in (codes or NATURAL_CAPABILITIES):
        capability = copy.deepcopy(SPEC_BY_CODE[code])
        capability.update(overrides.get(code, {}))
        definition = SimpleNamespace(id=uuid.uuid4(), **capability)
        membership = SimpleNamespace(id=uuid.uuid4(), taxonomy_release_id=release.id,
                                     capability_definition_id=definition.id)
        rows.append((membership, definition))
    return rows


def _install(monkeypatch, release, rows):
    calls = []

    def active(db):
        calls.append(("get_active_release",))
        return release

    def capabilities(db, *, release_id):
        calls.append(("get_release_capabilities", release_id))
        return list(rows)

    monkeypatch.setattr(af, "get_active_release", active)
    monkeypatch.setattr(af, "get_release_capabilities", capabilities)
    return calls


def _load(monkeypatch, release=None, rows=None):
    release = release or _fake_release()
    rows = _fake_rows(release) if rows is None else rows
    _install(monkeypatch, release, rows)
    return load_current_focus_taxonomy(_ReadOnlyDB()), release, rows


@pytest.fixture
def loaded(monkeypatch):
    return _load(monkeypatch)


@pytest.fixture
def taxonomy(loaded):
    return loaded[0]


def _ids(taxonomy):
    return {f"{c.capability_code}@r{c.semantic_revision}": c.definition_id for c in taxonomy.capabilities}


_DEFAULT = object()


def _focus(code, *tokens, mode=_DEFAULT):
    if mode is _DEFAULT:
        mode = LOCALIZED if tokens else COMPETENCY_ONLY
    return ProposedCompetencyFocus(competency_code=code, scope_mode=mode, capability_tokens=tuple(tokens))


def _proposal(status="resolved", target=_DEFAULT, supporting=(), **overrides):
    if target is _DEFAULT:
        target = _focus("C10", "C10_B@r1") if status == "resolved" else None
    kwargs = dict(schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION,
                  resolution_status=status, target=target, supporting=supporting)
    kwargs.update(overrides)
    return FocusProposal(**kwargs)


# --------------------------------------------------------------------------
# 1a. Contrats, versions, registre
# --------------------------------------------------------------------------

def test_frozen_versions():
    assert FOCUS_SCHEMA_VERSION == "interaction-focus-v1"
    assert FOCUS_POLICY_VERSION == "focus-policy-1"
    assert af.RESOLUTION_STATUSES == ("resolved", "neutral", "ambiguous", "composite")
    assert af.SCOPE_MODES == ("localized", "competency_only")
    assert "whole_competency" not in af.SCOPE_MODES


def test_policy_registry_is_exactly_v1_with_frozen_identity():
    assert isinstance(af._FOCUS_POLICIES, MappingProxyType)
    assert list(af._FOCUS_POLICIES) == [("oryx-v1", GOLDEN_V1_FINGERPRINT)]
    # Identité figée dans le registre ET égale à celle de la SPEC canonique.
    assert ("oryx-v1", GOLDEN_V1_FINGERPRINT) == (v1.VERSION_KEY, v1.EXPECTED_V1_FINGERPRINT)
    policy = af._FOCUS_POLICIES[("oryx-v1", GOLDEN_V1_FINGERPRINT)]
    assert policy.policy_version == FOCUS_POLICY_VERSION
    assert (policy.taxonomy_version_key, policy.taxonomy_spec_fingerprint) == ("oryx-v1", GOLDEN_V1_FINGERPRINT)
    assert policy.load_spec is v1.load_taxonomy_v1
    assert policy.competency_order == v1.COMPETENCY_CODES
    assert policy.capability_order == v1.CAPABILITY_CODES
    assert af._SUPPORTED_POLICY_VERSIONS == frozenset({FOCUS_POLICY_VERSION})
    with pytest.raises(FrozenInstanceError):
        policy.policy_version = "focus-policy-2"
    with pytest.raises(TypeError):
        af._FOCUS_POLICIES[("oryx-v2", "x")] = policy


def test_policy_registry_fingerprint_is_a_frozen_literal():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert GOLDEN_V1_FINGERPRINT in constants and "oryx-v1" in constants
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "EXPECTED_V1_FINGERPRINT" not in names and "VERSION_KEY" not in names


def test_v1_identity_resolves_to_focus_policy_1():
    assert af._resolve_policy("oryx-v1", GOLDEN_V1_FINGERPRINT).policy_version == "focus-policy-1"


@pytest.mark.parametrize("identity", [
    ("oryx-v2", GOLDEN_V1_FINGERPRINT),
    ("test-release", GOLDEN_V1_FINGERPRINT),
    ("oryx-v1", "0" * 64),
    ("oryx-v1", GOLDEN_V1_FINGERPRINT.upper()),
    ("oryx-v1", GOLDEN_V1_FINGERPRINT + " "),
    ("ORYX-V1", GOLDEN_V1_FINGERPRINT),
    ("oryx-v1 ", GOLDEN_V1_FINGERPRINT),
    ("oryx-v1", None),
    (None, GOLDEN_V1_FINGERPRINT),
])
def test_unknown_identity_is_unsupported_without_fallback(identity):
    with pytest.raises(UnsupportedFocusPolicy):
        af._resolve_policy(*identity)


def test_public_api_is_exactly_load_and_validate():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_FUNCTIONS
    assert list(inspect.signature(load_current_focus_taxonomy).parameters) == ["db"]
    assert list(inspect.signature(validate_focus_proposal).parameters) == ["proposal", "taxonomy"]
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef) and not n.name.startswith("_")}
    assert classes == {c.__name__ for c in PUBLIC_DATACLASSES} | {e.__name__ for e in EXCEPTIONS}


def test_public_dataclasses_are_frozen_kw_only_with_exact_fields():
    for cls, expected in PUBLIC_DATACLASSES.items():
        assert is_dataclass(cls), cls
        assert cls.__dataclass_params__.frozen, cls
        assert [f.name for f in fields(cls)] == expected, cls
        assert all(f.kw_only for f in fields(cls)), cls
    with pytest.raises(TypeError):
        CompetencyFocus("C1", "competency_only", ())  # kw_only
    focus = CompetencyFocus(competency_code="C1", scope_mode="competency_only", capability_definition_ids=())
    with pytest.raises(FrozenInstanceError):
        focus.competency_code = "C2"


def test_exceptions_are_a_small_explicit_hierarchy():
    public = {o for o in vars(af).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == af.__name__}
    assert public == EXCEPTIONS
    assert FocusError.__bases__ == (Exception,)
    for exc in EXCEPTIONS - {FocusError}:
        assert exc.__bases__ == (FocusError,), exc
        assert not issubclass(exc, (ValueError, LookupError, TaxonomyServiceError)), exc


# --------------------------------------------------------------------------
# 1b. Catalogue (lectures T4-B simulées)
# --------------------------------------------------------------------------

def test_1_no_active_release_is_unavailable(monkeypatch):
    calls = _install(monkeypatch, None, [])
    with pytest.raises(FocusTaxonomyUnavailable):
        load_current_focus_taxonomy(_ReadOnlyDB())
    assert calls == [("get_active_release",)]


def test_several_active_releases_are_invalid_never_chosen(monkeypatch):
    def broken(db):
        raise InvalidTaxonomyState("2 releases actives observées : intégrité rompue")
    monkeypatch.setattr(af, "get_active_release", broken)
    with pytest.raises(InvalidFocusTaxonomy):
        load_current_focus_taxonomy(_ReadOnlyDB())


def test_2_v1_with_exact_fingerprint_is_focus_policy_1(loaded):
    taxonomy, release, _ = loaded
    assert taxonomy.focus_policy_version == "focus-policy-1"
    assert taxonomy.taxonomy_release_id == release.id
    assert taxonomy.taxonomy_version_key == "oryx-v1"
    assert taxonomy.taxonomy_spec_fingerprint == GOLDEN_V1_FINGERPRINT
    assert taxonomy.taxonomy_schema_version == v1.TAXONOMY_SCHEMA_VERSION == 1


@pytest.mark.parametrize("version_key", ["oryx-v2", "test-release", "ORYX-V1"])
def test_3_unknown_version_key_is_unsupported(monkeypatch, version_key):
    release = _fake_release(version_key=version_key)
    calls = _install(monkeypatch, release, _fake_rows(release))
    with pytest.raises(UnsupportedFocusPolicy):
        load_current_focus_taxonomy(_ReadOnlyDB())
    # Refus AVANT toute lecture du contenu : aucun repli par capability_code.
    assert calls == [("get_active_release",)]


@pytest.mark.parametrize("fingerprint", ["0" * 64, GOLDEN_V1_FINGERPRINT[:-1] + "0", "sha256:x"])
def test_4_v1_with_another_fingerprint_is_unsupported(monkeypatch, fingerprint):
    release = _fake_release(spec_fingerprint=fingerprint)
    calls = _install(monkeypatch, release, _fake_rows(release))
    with pytest.raises(UnsupportedFocusPolicy):
        load_current_focus_taxonomy(_ReadOnlyDB())
    assert calls == [("get_active_release",)]


def _refused(monkeypatch, rows, release, match=None):
    _install(monkeypatch, release, rows)
    with pytest.raises(InvalidFocusTaxonomy, match=match):
        load_current_focus_taxonomy(_ReadOnlyDB())


@pytest.mark.parametrize("missing", ["C1_A", "C10_B", "C12_D"])
def test_5_missing_capability_is_invalid(monkeypatch, missing):
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release, [c for c in NATURAL_CAPABILITIES if c != missing]), release,
             match=missing)


def test_5_empty_release_is_invalid(monkeypatch):
    _refused(monkeypatch, [], _fake_release())


def test_6_extra_capability_revision_is_invalid(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    rows += _fake_rows(release, ["C10_B"], C10_B={"semantic_revision": 2, "definition": "sens révisé"})
    _refused(monkeypatch, rows, release, match="plusieurs révisions \\['C10_B'\\]")


def test_6_extra_unknown_capability_is_invalid(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    extra = SimpleNamespace(**{**rows[0][1].__dict__, "id": uuid.uuid4(), "capability_code": "C13_A"})
    rows.append((SimpleNamespace(id=uuid.uuid4(), taxonomy_release_id=release.id,
                                 capability_definition_id=extra.id), extra))
    _refused(monkeypatch, rows, release, match="C13_A")


def test_6_duplicated_definition_is_invalid(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    membership, definition = rows[3]
    rows.append((SimpleNamespace(id=uuid.uuid4(), taxonomy_release_id=release.id,
                                 capability_definition_id=definition.id), definition))
    _refused(monkeypatch, rows, release, match="double")


@pytest.mark.parametrize("revision", [2, True, 1.0, "1"])
def test_7_divergent_semantic_revision_is_invalid(monkeypatch, revision):
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release, C10_B={"semantic_revision": revision}), release,
             match="C10_B .*semantic_revision")


def test_8_divergent_competency_code_is_invalid(monkeypatch):
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release, C7_A={"competency_code": "C8"}), release,
             match="C7_A .*competency_code")


@pytest.mark.parametrize("label", ["x", SPEC_BY_CODE["C5_C"]["label"] + " ", SPEC_BY_CODE["C5_C"]["label"].upper()])
def test_9_divergent_label_is_invalid(monkeypatch, label):
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release, C5_C={"label": label}), release, match="C5_C .*label")


def test_10_divergent_definition_is_invalid(monkeypatch):
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release, C11_D={"definition": SPEC_BY_CODE["C11_D"]["definition"] + " "}),
             release, match="C11_D .*definition")


def _guidance(code, mutate):
    guidance = copy.deepcopy(SPEC_BY_CODE[code]["mapping_guidance"])
    mutate(guidance)
    return guidance


@pytest.mark.parametrize("mutate", [
    lambda g: g["include"].append("ajout"),
    lambda g: g["exclude"].reverse(),
    lambda g: g["exclude"].pop(),
    lambda g: g.update(extra=[]),
    lambda g: g.pop("boundary_notes"),
    lambda g: g["boundary_notes"].append({"against": "C1_A", "rule": "r"}),
    lambda g: g.clear(),
], ids=["include+", "exclude-order", "exclude-", "extra-key", "no-notes", "notes+", "empty"])
def test_11_divergent_mapping_guidance_is_invalid(monkeypatch, mutate):
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release, C9_B={"mapping_guidance": _guidance("C9_B", mutate)}), release,
             match="C9_B .*mapping_guidance")


def test_11_mapping_guidance_key_order_is_not_a_divergence(monkeypatch):
    release = _fake_release()
    reordered = dict(reversed(list(SPEC_BY_CODE["C9_B"]["mapping_guidance"].items())))
    taxonomy, _, _ = _load(monkeypatch, release, _fake_rows(release, C9_B={"mapping_guidance": reordered}))
    assert len(taxonomy.capabilities) == 45


def test_membership_of_another_release_is_invalid(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    rows[0][0].taxonomy_release_id = uuid.uuid4()
    _refused(monkeypatch, rows, release, match="autre release")


def test_t4b_read_error_is_invalid_taxonomy(monkeypatch):
    release = _fake_release()
    _install(monkeypatch, release, [])

    def broken(db, *, release_id):
        raise InvalidTaxonomyState("x")
    monkeypatch.setattr(af, "get_release_capabilities", broken)
    with pytest.raises(InvalidFocusTaxonomy):
        load_current_focus_taxonomy(_ReadOnlyDB())


def test_drifted_local_spec_is_invalid_never_supported(monkeypatch):
    """SPEC locale modifiée sous la même identité : load_taxonomy_v1 la
    refuse (fingerprint) ; jamais un catalogue construit dessus."""
    drifted = v1.taxonomy_v1_spec()
    drifted["competencies"][9]["central_question"] += " "
    monkeypatch.setattr(v1, "_SPEC", drifted)
    release = _fake_release()
    _refused(monkeypatch, _fake_rows(release), release, match="SPEC canonique")


def test_12_and_13_exact_45_capabilities_and_12_competencies_from_the_spec(taxonomy):
    assert len(taxonomy.capabilities) == 45
    for capability in taxonomy.capabilities:
        canonical = SPEC_BY_CODE[capability.capability_code]
        for name in ("capability_code", "semantic_revision", "competency_code", "label", "definition"):
            assert getattr(capability, name) == canonical[name]
        assert json.dumps(_thaw(capability.mapping_guidance), sort_keys=True) == json.dumps(
            canonical["mapping_guidance"], sort_keys=True)
    assert len(taxonomy.competencies) == 12
    spec_competencies = {c["competency_code"]: c for c in SPEC["competencies"]}
    for competency in taxonomy.competencies:
        canonical = spec_competencies[competency.competency_code]
        assert (competency.label, competency.central_question) == (canonical["label"], canonical["central_question"])


def test_14_competencies_c1_to_c12(taxonomy):
    assert [c.competency_code for c in taxonomy.competencies] == NATURAL_COMPETENCIES
    assert [c.competency_code for c in taxonomy.competencies] != sorted(NATURAL_COMPETENCIES)


def test_15_natural_order_of_the_45_capabilities_whatever_the_row_order(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    shuffled = list(rows)
    random.Random(7).shuffle(shuffled)
    taxonomy, _, _ = _load(monkeypatch, release, shuffled)
    assert [c.capability_code for c in taxonomy.capabilities] == NATURAL_CAPABILITIES
    index = NATURAL_CAPABILITIES.index
    assert index("C9_D") + 1 == index("C10_A") and index("C2_A") < index("C10_A")


def test_16_definition_ids_come_from_the_database(loaded):
    taxonomy, _, rows = loaded
    by_code = {d.capability_code: d.id for _, d in rows}
    assert {c.capability_code: c.definition_id for c in taxonomy.capabilities} == by_code
    # La SPEC ne porte aucun identifiant.
    assert not any(key == "id" or key.endswith("_id") for key in SPEC_BY_CODE["C10_B"])


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def test_17_mutating_sources_after_construction_never_changes_the_taxonomy(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    spec, fingerprint = v1.load_taxonomy_v1()
    policy = replace(af._FOCUS_POLICIES[("oryx-v1", GOLDEN_V1_FINGERPRINT)], load_spec=lambda: (spec, fingerprint))
    monkeypatch.setattr(af, "_FOCUS_POLICIES", MappingProxyType({("oryx-v1", GOLDEN_V1_FINGERPRINT): policy}))
    taxonomy, _, _ = _load(monkeypatch, release, rows)
    before = copy.deepcopy(_thaw(taxonomy.capabilities[0].mapping_guidance)), taxonomy.competencies[0].label

    spec["capabilities"][0]["mapping_guidance"]["include"].append("muté")
    spec["capabilities"][0]["mapping_guidance"]["boundary_notes"].clear()
    spec["competencies"][0]["label"] = "muté"
    rows[0][1].mapping_guidance["include"].append("muté")
    assert (_thaw(taxonomy.capabilities[0].mapping_guidance), taxonomy.competencies[0].label) == before

    guidance = taxonomy.capabilities[0].mapping_guidance
    assert isinstance(guidance, MappingProxyType) and isinstance(guidance["include"], tuple)
    with pytest.raises(TypeError):
        guidance["include"] = ()
    with pytest.raises(FrozenInstanceError):
        taxonomy.capabilities = ()
    assert type(taxonomy.capabilities) is tuple and type(taxonomy.competencies) is tuple


def test_17_direct_construction_deep_freezes_mapping_guidance():
    source = {"include": ["a"], "exclude": ["b"], "boundary_notes": [{"against": "C1_B", "rule": "r"}]}
    capability = FocusCapabilityDefinition(definition_id=uuid.uuid4(), capability_code="C1_A", semantic_revision=1,
                                           competency_code="C1", label="l", definition="d", mapping_guidance=source)
    source["include"].append("x")
    source["boundary_notes"][0]["rule"] = "x"
    assert _thaw(capability.mapping_guidance) == {"include": ["a"], "exclude": ["b"],
                                                  "boundary_notes": [{"against": "C1_B", "rule": "r"}]}
    with pytest.raises(InvalidFocusTaxonomy):
        FocusCapabilityDefinition(definition_id=uuid.uuid4(), capability_code="C1_A", semantic_revision=1,
                                  competency_code="C1", label="l", definition="d", mapping_guidance={"s": {1}})


def test_load_only_reads_through_t4b_under_no_autoflush(monkeypatch):
    release = _fake_release()
    calls = _install(monkeypatch, release, _fake_rows(release))
    session = _ReadOnlyDB()
    load_current_focus_taxonomy(session)
    assert calls == [("get_active_release",), ("get_release_capabilities", release.id)]
    assert session.autoflush_blocks == 1


def test_load_is_deterministic(monkeypatch):
    release = _fake_release()
    rows = _fake_rows(release)
    first, _, _ = _load(monkeypatch, release, rows)
    second, _, _ = _load(monkeypatch, release, rows)
    assert first == second


# --------------------------------------------------------------------------
# 1c. Validation des propositions
# --------------------------------------------------------------------------

def _invalid(proposal, taxonomy, match=None):
    with pytest.raises(InvalidFocusProposal, match=match):
        validate_focus_proposal(proposal, taxonomy)


def test_37_example_contract(taxonomy):
    focus = validate_focus_proposal(_proposal(), taxonomy)
    assert focus == InteractionCompetencyFocus(
        schema_version="interaction-focus-v1",
        policy_version="focus-policy-1",
        taxonomy_release_id=taxonomy.taxonomy_release_id,
        taxonomy_spec_fingerprint=GOLDEN_V1_FINGERPRINT,
        resolution_status="resolved",
        target=CompetencyFocus(competency_code="C10", scope_mode="localized",
                               capability_definition_ids=(_ids(taxonomy)["C10_B@r1"],)),
        supporting=(),
    )


@pytest.mark.parametrize("version", ["interaction-focus-v2", "interaction-focus-v0", "", None, 1,
                                     "INTERACTION-FOCUS-V1", " interaction-focus-v1"])
def test_18_unknown_schema_version(taxonomy, version):
    _invalid(_proposal(schema_version=version), taxonomy, match="schema_version")


@pytest.mark.parametrize("version", ["focus-policy-2", "focus-policy-0", "", None, "FOCUS-POLICY-1"])
def test_19_policy_version_other_than_the_taxonomy(taxonomy, version):
    _invalid(_proposal(policy_version=version), taxonomy, match="policy_version")


def test_19_registered_policy_but_not_the_one_of_the_taxonomy(monkeypatch, taxonomy):
    """Une policy enregistrée ailleurs ne s'applique jamais à ce catalogue."""
    monkeypatch.setattr(af, "_SUPPORTED_POLICY_VERSIONS", frozenset({FOCUS_POLICY_VERSION, "focus-policy-2"}))
    _invalid(_proposal(policy_version="focus-policy-2"), taxonomy, match="différente")


@pytest.mark.parametrize("status", ["unknown", "Resolved", "RESOLVED", "", None, "partial", "clarify"])
def test_20_unknown_resolution_status(taxonomy, status):
    _invalid(_proposal(status=status, target=None), taxonomy, match="resolution_status")


def test_21_resolved_without_target(taxonomy):
    _invalid(_proposal(target=None), taxonomy, match="sans cible")
    _invalid(_proposal(target=None, supporting=(_focus("C2"),)), taxonomy, match="sans cible")


def test_22_resolved_with_a_valid_target(taxonomy):
    for target in (_focus("C10", "C10_B@r1"), _focus("C3")):
        focus = validate_focus_proposal(_proposal(target=target), taxonomy)
        assert focus.resolution_status == "resolved" and focus.target.competency_code == target.competency_code
        assert focus.supporting == ()


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_23_to_26_unresolved_statuses_have_no_target_nor_support(taxonomy, status):
    focus = validate_focus_proposal(_proposal(status=status, target=None), taxonomy)
    assert focus == InteractionCompetencyFocus(
        schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION,
        taxonomy_release_id=taxonomy.taxonomy_release_id, taxonomy_spec_fingerprint=GOLDEN_V1_FINGERPRINT,
        resolution_status=status, target=None, supporting=())
    _invalid(_proposal(status=status, target=_focus("C10", "C10_B@r1")), taxonomy, match=status)
    _invalid(_proposal(status=status, target=None, supporting=(_focus("C2"),)), taxonomy, match=status)
    _invalid(_proposal(status=status, target=_focus("C1"), supporting=(_focus("C2"),)), taxonomy, match=status)


def test_27_localized_without_capability(taxonomy):
    _invalid(_proposal(target=_focus("C10", mode=LOCALIZED)), taxonomy, match="localized sans capacité")
    _invalid(_proposal(supporting=(_focus("C2", mode=LOCALIZED),)), taxonomy, match="localized sans capacité")


def test_28_competency_only_with_capability(taxonomy):
    _invalid(_proposal(target=_focus("C10", "C10_B@r1", mode=COMPETENCY_ONLY)), taxonomy, match="competency_only")
    _invalid(_proposal(supporting=(_focus("C2", "C2_A@r1", mode=COMPETENCY_ONLY),)), taxonomy,
             match="competency_only")


@pytest.mark.parametrize("mode", ["whole_competency", "Localized", "LOCALIZED", "", None, "partial"])
def test_29_scope_mode_outside_vocabulary(taxonomy, mode):
    _invalid(_proposal(target=_focus("C10", mode=mode)), taxonomy, match="scope_mode")
    _invalid(_proposal(target=_focus("C10", "C10_B@r1", mode=mode)), taxonomy, match="scope_mode")


@pytest.mark.parametrize("token", ["C10_E@r1", "C13_A@r1", "C0_A@r1", "X10_B@r1"])
def test_30_unknown_token(taxonomy, token):
    _invalid(_proposal(target=_focus("C10", token)), taxonomy)


@pytest.mark.parametrize("token", ["C10_B", "C10_B@1", "c10_b@r1", "C10_b@r1", "C10_B@R1", "C10_B@r01",
                                   "C10_B@r", "C10_B@r0", " C10_B@r1", "C10_B@r1 ", "C10_B@r1\n", "C10_B @r1",
                                   "C10_B@r1@r1", "C10_B@r+1", "C10_B@r١", "", 1, None])
def test_31_malformed_token(taxonomy, token):
    _invalid(_proposal(target=_focus("C10", token)), taxonomy, match="hors format")


@pytest.mark.parametrize("token", ["C10_B@r2", "C10_B@r10", "C10_B@r2147483647"])
def test_32_wrong_revision(taxonomy, token):
    _invalid(_proposal(target=_focus("C10", token)), taxonomy, match="absent de la taxonomie")


@pytest.mark.parametrize("token", ["C1_B@r1", "C9_B@r1", "C11_B@r1"])
def test_33_token_of_another_competency(taxonomy, token):
    _invalid(_proposal(target=_focus("C10", token)), taxonomy, match="appartient à")


def test_34_duplicated_token(taxonomy):
    _invalid(_proposal(target=_focus("C10", "C10_B@r1", "C10_B@r1")), taxonomy, match="double")
    _invalid(_proposal(supporting=(_focus("C9", "C9_B@r1", "C9_C@r1", "C9_B@r1"),)), taxonomy, match="double")


def test_35_target_also_in_supporting(taxonomy):
    _invalid(_proposal(supporting=(_focus("C10", "C10_D@r1"),)), taxonomy, match="déjà la cible")
    _invalid(_proposal(supporting=(_focus("C2"), _focus("C10"))), taxonomy, match="déjà la cible")


def test_36_duplicated_support_competency(taxonomy):
    """C9_B et C9_C sont regroupés dans UN focus C9, jamais deux."""
    _invalid(_proposal(supporting=(_focus("C9", "C9_B@r1"), _focus("C9", "C9_C@r1"))), taxonomy, match="double")
    _invalid(_proposal(supporting=(_focus("C9"), _focus("C9", "C9_C@r1"))), taxonomy, match="double")
    ids = _ids(taxonomy)
    focus = validate_focus_proposal(_proposal(target=_focus("C11", "C11_B@r1"),
                                              supporting=(_focus("C9", "C9_C@r1", "C9_B@r1"),)), taxonomy)
    assert focus.supporting == (CompetencyFocus(competency_code="C9", scope_mode="localized",
                                                capability_definition_ids=(ids["C9_B@r1"], ids["C9_C@r1"])),)


def test_37_capabilities_in_random_order_come_out_in_taxonomic_order(taxonomy):
    ids = _ids(taxonomy)
    tokens = ["C10_A@r1", "C10_B@r1", "C10_C@r1", "C10_D@r1"]
    expected = tuple(ids[t] for t in tokens)
    rng = random.Random(42)
    for _ in range(10):
        shuffled = list(tokens)
        rng.shuffle(shuffled)
        focus = validate_focus_proposal(_proposal(target=_focus("C10", *shuffled)), taxonomy)
        assert focus.target.capability_definition_ids == expected
    focus = validate_focus_proposal(_proposal(target=_focus("C10", "C10_D@r1", "C10_B@r1")), taxonomy)
    assert focus.target.capability_definition_ids == (ids["C10_B@r1"], ids["C10_D@r1"])


def test_38_supports_come_out_c1_to_c12(taxonomy):
    focus = validate_focus_proposal(_proposal(target=_focus("C11", "C11_A@r1"),
                                              supporting=(_focus("C10"), _focus("C2"), _focus("C7", "C7_A@r1"))),
                                    taxonomy)
    assert [s.competency_code for s in focus.supporting] == ["C2", "C7", "C10"]
    focus = validate_focus_proposal(_proposal(target=_focus("C1"), supporting=tuple(
        _focus(code) for code in reversed(NATURAL_COMPETENCIES[1:]))), taxonomy)
    assert [s.competency_code for s in focus.supporting] == NATURAL_COMPETENCIES[1:]


def test_39_competency_only_has_no_definition_id(taxonomy):
    focus = validate_focus_proposal(_proposal(target=_focus("C7"), supporting=(_focus("C3"),)), taxonomy)
    assert focus.target == CompetencyFocus(competency_code="C7", scope_mode="competency_only",
                                           capability_definition_ids=())
    assert focus.supporting[0].capability_definition_ids == ()


def test_40_localized_gives_the_exact_uuids_of_the_taxonomy(loaded):
    taxonomy, _, rows = loaded
    db_ids = {(d.capability_code, d.semantic_revision): d.id for _, d in rows}
    focus = validate_focus_proposal(_proposal(target=_focus("C7", "C7_C@r1", "C7_A@r1"),
                                              supporting=(_focus("C12", "C12_D@r1"),)), taxonomy)
    assert focus.target.capability_definition_ids == (db_ids[("C7_A", 1)], db_ids[("C7_C", 1)])
    assert focus.supporting[0].capability_definition_ids == (db_ids[("C12_D", 1)],)
    assert all(type(i) is uuid.UUID for i in focus.target.capability_definition_ids)
    assert focus.taxonomy_release_id == taxonomy.taxonomy_release_id


@pytest.mark.parametrize("code", ["C13", "C0", "c10", "C10_B", "", None])
def test_unknown_competency_code(taxonomy, code):
    _invalid(_proposal(target=_focus(code)), taxonomy, match="competency_code")


@pytest.mark.parametrize("bad", [["C10_B@r1"], "C10_B@r1", None, {"C10_B@r1"}, []])
def test_tokens_must_be_an_exact_tuple(taxonomy, bad):
    target = ProposedCompetencyFocus(competency_code="C10", scope_mode=LOCALIZED, capability_tokens=bad)
    _invalid(_proposal(target=target), taxonomy, match="tuple")


def test_supporting_must_be_an_exact_tuple_of_proposed_focuses(taxonomy):
    _invalid(_proposal(supporting=[_focus("C2")]), taxonomy, match="tuple")
    _invalid(_proposal(status="neutral", target=None, supporting=[]), taxonomy, match="tuple")
    _invalid(_proposal(supporting=(CompetencyFocus(competency_code="C2", scope_mode="competency_only",
                                                   capability_definition_ids=()),)), taxonomy,
             match="ProposedCompetencyFocus")
    _invalid(_proposal(target={"competency_code": "C10"}), taxonomy, match="ProposedCompetencyFocus")


def test_arguments_must_be_the_contract_types(taxonomy):
    with pytest.raises(InvalidFocusArgument):
        validate_focus_proposal({"schema_version": FOCUS_SCHEMA_VERSION}, taxonomy)
    with pytest.raises(InvalidFocusArgument):
        validate_focus_proposal(_proposal(), {"taxonomy_release_id": taxonomy.taxonomy_release_id})


def test_a_hand_built_incoherent_taxonomy_is_refused(taxonomy):
    with pytest.raises(UnsupportedFocusPolicy):
        validate_focus_proposal(_proposal(), replace(taxonomy, taxonomy_spec_fingerprint="0" * 64))
    with pytest.raises(InvalidFocusTaxonomy):
        validate_focus_proposal(_proposal(policy_version="focus-policy-2"),
                                replace(taxonomy, focus_policy_version="focus-policy-2"))
    doubled = replace(taxonomy, capabilities=taxonomy.capabilities + taxonomy.capabilities[:1])
    with pytest.raises(InvalidFocusTaxonomy, match="46 capacité"):
        validate_focus_proposal(_proposal(), doubled)


def test_another_release_of_the_same_codes_is_never_remapped(monkeypatch, taxonomy):
    """Même capability_code, même révision, autre release / autre
    definition_id : la sortie est toujours liée au catalogue fourni."""
    other, _, _ = _load(monkeypatch)
    proposal = _proposal()
    first = validate_focus_proposal(proposal, taxonomy)
    second = validate_focus_proposal(proposal, other)
    assert first.taxonomy_release_id != second.taxonomy_release_id
    assert first.target.capability_definition_ids != second.target.capability_definition_ids
    assert second.target.capability_definition_ids == (_ids(other)["C10_B@r1"],)


def test_errors_are_never_turned_into_neutral(taxonomy):
    for proposal in (_proposal(target=_focus("C10", "C10_B")), _proposal(status="unknown", target=None)):
        with pytest.raises(FocusError):
            validate_focus_proposal(proposal, taxonomy)


# --------------------------------------------------------------------------
# 1c-bis. Frontière publique : catalogue forgé sous l'identité officielle
# --------------------------------------------------------------------------

def _forged(taxonomy, **changes):
    """Catalogue construit à la main qui conserve oryx-v1, le fingerprint
    officiel et focus-policy-1."""
    forged = replace(taxonomy, **changes)
    assert (forged.taxonomy_version_key, forged.taxonomy_spec_fingerprint, forged.focus_policy_version) == (
        "oryx-v1", GOLDEN_V1_FINGERPRINT, "focus-policy-1")
    return forged


def _with_capability(taxonomy, code, **changes):
    return tuple(replace(c, **changes) if c.capability_code == code else c for c in taxonomy.capabilities)


def _with_competency(taxonomy, code, **changes):
    return tuple(replace(c, **changes) if c.competency_code == code else c for c in taxonomy.competencies)


def _never_validated(forged, *proposals, match=None):
    """Refus fail-closed, quel que soit le statut proposé (le catalogue est
    vérifié avant toute résolution) : jamais un focus validé."""
    for proposal in proposals or (_proposal(), _proposal(status="neutral"),
                                  _proposal(target=_focus("C7"), supporting=(_focus("C2", "C2_A@r1"),))):
        with pytest.raises(InvalidFocusTaxonomy, match=match):
            validate_focus_proposal(proposal, forged)


def _c99():
    return FocusCompetencyDefinition(competency_code="C99", label="Compétence forgée",
                                     central_question="Question forgée ?")


def _c99_a():
    return FocusCapabilityDefinition(definition_id=uuid.uuid4(), capability_code="C99_A", semantic_revision=1,
                                     competency_code="C99", label="Capacité forgée", definition="forgée",
                                     mapping_guidance={"include": ["x"], "exclude": ["y"], "boundary_notes": []})


@pytest.mark.parametrize("version", [2, 0, True, 1.0, "1", None])
def test_forged_1_schema_version(taxonomy, version):
    _never_validated(_forged(taxonomy, taxonomy_schema_version=version), match="taxonomy_schema_version")


def test_forged_2_c12_removed(taxonomy):
    _never_validated(_forged(taxonomy, competencies=taxonomy.competencies[:-1]), match="11 compétence")


def test_forged_3_extra_c99_competency(taxonomy):
    _never_validated(_forged(taxonomy, competencies=taxonomy.competencies + (_c99(),)), match="13 compétence")
    _never_validated(_forged(taxonomy, competencies=(_c99(),) + taxonomy.competencies[1:]))


def test_forged_4_reordered_competencies(taxonomy):
    swapped = (taxonomy.competencies[1], taxonomy.competencies[0]) + taxonomy.competencies[2:]
    _never_validated(_forged(taxonomy, competencies=swapped), match="C1 -> C12")
    lexical = tuple(sorted(taxonomy.competencies, key=lambda c: c.competency_code))
    _never_validated(_forged(taxonomy, competencies=lexical), match="C1 -> C12")
    _never_validated(_forged(taxonomy, competencies=taxonomy.competencies[:11] + taxonomy.competencies[:1]))


@pytest.mark.parametrize("field", ["label", "central_question"])
def test_forged_5_and_6_competency_text(taxonomy, field):
    current = getattr(taxonomy.competencies[9], field)
    for value in (current + " ", current.upper(), "texte forgé"):
        forged = _forged(taxonomy, competencies=_with_competency(taxonomy, "C10", **{field: value}))
        _never_validated(forged, match=field)


def test_forged_7_capability_removed(taxonomy):
    for removed in ("C1_A", "C10_B", "C12_D"):
        _never_validated(_forged(taxonomy, capabilities=tuple(
            c for c in taxonomy.capabilities if c.capability_code != removed)), match="44 capacité")


def test_forged_8_extra_capability(taxonomy):
    c10_b = next(c for c in taxonomy.capabilities if c.capability_code == "C10_B")
    r2 = replace(c10_b, definition_id=uuid.uuid4(), semantic_revision=2, definition="sens révisé")
    position = taxonomy.capabilities.index(c10_b) + 1
    inserted = taxonomy.capabilities[:position] + (r2,) + taxonomy.capabilities[position:]
    _never_validated(_forged(taxonomy, capabilities=inserted), match="46 capacité")
    _never_validated(_forged(taxonomy, capabilities=taxonomy.capabilities + (_c99_a(),)), match="46 capacité")


def test_forged_9_reordered_capabilities(taxonomy):
    capabilities = list(taxonomy.capabilities)
    capabilities[0], capabilities[1] = capabilities[1], capabilities[0]
    _never_validated(_forged(taxonomy, capabilities=tuple(capabilities)), match="ordre canonique")
    lexical = tuple(sorted(taxonomy.capabilities, key=lambda c: c.capability_code))
    _never_validated(_forged(taxonomy, capabilities=lexical))
    shuffled = list(taxonomy.capabilities)
    random.Random(3).shuffle(shuffled)
    _never_validated(_forged(taxonomy, capabilities=tuple(shuffled)))


@pytest.mark.parametrize("revision", [2, 0, True, 1.0, "1"])
def test_forged_10_semantic_revision(taxonomy, revision):
    forged = _forged(taxonomy, capabilities=_with_capability(taxonomy, "C10_B", semantic_revision=revision))
    _never_validated(forged, match="semantic_revision")
    _never_validated(forged, _proposal(target=_focus("C10", f"C10_B@r{revision}")), match="semantic_revision")


def test_forged_11_capability_competency_code(taxonomy):
    forged = _forged(taxonomy, capabilities=_with_capability(taxonomy, "C10_B", competency_code="C9"))
    _never_validated(forged, match="competency_code")
    _never_validated(forged, _proposal(target=_focus("C9", "C10_B@r1")), match="competency_code")


@pytest.mark.parametrize("field", ["label", "definition"])
def test_forged_12_and_13_capability_text(taxonomy, field):
    current = getattr(next(c for c in taxonomy.capabilities if c.capability_code == "C7_A"), field)
    for value in (current + " ", current.upper(), "texte forgé"):
        _never_validated(_forged(taxonomy, capabilities=_with_capability(taxonomy, "C7_A", **{field: value})),
                         match=field)


@pytest.mark.parametrize("mutate", [
    lambda g: g["include"].append("ajout"),
    lambda g: g["exclude"].reverse(),
    lambda g: g.update(extra=[]),
    lambda g: g.pop("boundary_notes"),
    lambda g: g.clear(),
], ids=["include+", "exclude-order", "extra-key", "no-notes", "empty"])
def test_forged_14_mapping_guidance(taxonomy, mutate):
    forged = _forged(taxonomy, capabilities=_with_capability(
        taxonomy, "C9_B", mapping_guidance=_guidance("C9_B", mutate)))
    _never_validated(forged, match="mapping_guidance")


@pytest.mark.parametrize("make", [str, lambda u: u.hex, lambda u: u.int, lambda u: None, lambda u: u.bytes],
                         ids=["str", "hex", "int", "none", "bytes"])
def test_forged_15_definition_id_not_uuid(taxonomy, make):
    c10_b = next(c for c in taxonomy.capabilities if c.capability_code == "C10_B")
    forged = _forged(taxonomy, capabilities=_with_capability(taxonomy, "C10_B",
                                                             definition_id=make(c10_b.definition_id)))
    _never_validated(forged, match="uuid.UUID")


def test_forged_15_release_id_not_uuid(taxonomy):
    _never_validated(_forged(taxonomy, taxonomy_release_id=str(taxonomy.taxonomy_release_id)), match="uuid.UUID")


def test_forged_16_duplicated_definition_id(taxonomy):
    c10_a = next(c for c in taxonomy.capabilities if c.capability_code == "C10_A")
    forged = _forged(taxonomy, capabilities=_with_capability(taxonomy, "C10_B", definition_id=c10_a.definition_id))
    _never_validated(forged, match="double")


def test_forged_17_fake_c99_catalogue_never_validates_a_c99_focus(taxonomy):
    with_c99 = _forged(taxonomy, competencies=taxonomy.competencies[:-1] + (_c99(),),
                       capabilities=taxonomy.capabilities[:-1] + (_c99_a(),))
    added_c99 = _forged(taxonomy, competencies=taxonomy.competencies + (_c99(),),
                        capabilities=taxonomy.capabilities + (_c99_a(),))
    for forged in (with_c99, added_c99):
        _never_validated(forged, _proposal(target=_focus("C99", "C99_A@r1")),
                         _proposal(target=_focus("C99")),
                         _proposal(target=_focus("C10", "C10_B@r1"), supporting=(_focus("C99", "C99_A@r1"),)))


def test_forged_wrong_types_of_entries(taxonomy):
    as_dicts = tuple({"competency_code": c.competency_code} for c in taxonomy.competencies)
    _never_validated(_forged(taxonomy, competencies=as_dicts))
    _never_validated(_forged(taxonomy, capabilities=(SimpleNamespace(**taxonomy.capabilities[0].__dict__),)
                             + taxonomy.capabilities[1:]))


def test_loaded_catalogue_passes_the_boundary_unchanged(loaded):
    """Un catalogue réellement produit par load_current_focus_taxonomy passe
    la seconde validation pure ; le résultat est inchangé."""
    taxonomy, release, rows = loaded
    index = af._index(taxonomy)
    assert list(index.competencies) == NATURAL_COMPETENCIES
    assert list(index.by_token) == [f"{code}@r1" for code in NATURAL_CAPABILITIES]
    db_ids = {d.capability_code: d.id for _, d in rows}
    assert dict(index.position) == {db_ids[code]: i for i, code in enumerate(NATURAL_CAPABILITIES)}
    focus = validate_focus_proposal(_proposal(target=_focus("C11", "C11_D@r1", "C11_B@r1"),
                                              supporting=(_focus("C9", "C9_C@r1"), _focus("C2"))), taxonomy)
    assert focus == InteractionCompetencyFocus(
        schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION,
        taxonomy_release_id=release.id, taxonomy_spec_fingerprint=GOLDEN_V1_FINGERPRINT,
        resolution_status="resolved",
        target=CompetencyFocus(competency_code="C11", scope_mode="localized",
                               capability_definition_ids=(db_ids["C11_B"], db_ids["C11_D"])),
        supporting=(CompetencyFocus(competency_code="C2", scope_mode="competency_only", capability_definition_ids=()),
                    CompetencyFocus(competency_code="C9", scope_mode="localized",
                                    capability_definition_ids=(db_ids["C9_C"],))))
    # Un catalogue reconstruit champ à champ (mêmes valeurs) passe aussi.
    rebuilt = CurrentFocusTaxonomy(**{f.name: getattr(taxonomy, f.name) for f in fields(CurrentFocusTaxonomy)})
    assert validate_focus_proposal(_proposal(), rebuilt) == validate_focus_proposal(_proposal(), taxonomy)


def test_the_boundary_rejects_a_drifted_local_spec(monkeypatch, taxonomy):
    """Si la SPEC locale ne correspond plus à l'identité enregistrée, même un
    catalogue chargé auparavant n'est plus utilisable."""
    drifted = v1.taxonomy_v1_spec()
    drifted["competencies"][0]["label"] += " "
    monkeypatch.setattr(v1, "_SPEC", drifted)
    _never_validated(taxonomy, match="SPEC canonique")


# --------------------------------------------------------------------------
# 1d. Pureté, déterminisme, constitution
# --------------------------------------------------------------------------

def _tree():
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def _tokens():
    return _code_tokens(MODULE_PATH.read_text(encoding="utf-8"))


def _words():
    """Mots des identifiants et chaînes du code (docstrings / commentaires
    exclus), découpés sur tout caractère non alphanumérique."""
    return {w for w in "".join(c if c.isalnum() else " " for c in _tokens().lower()).split()}


def _imports():
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_41_no_import_of_6_1a():
    assert _imports() == {"json", "re", "uuid", "collections.abc", "dataclasses", "types", "typing",
                          "core.pedagogy.taxonomy_v1", "core.taxonomy_service"}
    assert not any("adaptation" in module for module in _imports())


def test_42_no_reference_to_6_1a_objects():
    text = MODULE_PATH.read_text(encoding="utf-8")
    for name in ("AdaptationStateSnapshot", "CompetencyAdaptationSnapshot", "load_adaptation_state",
                 "adaptation_state"):
        assert name not in text, name


def test_43_no_step5_data():
    tokens = _tokens().lower()
    for word in ("user_id", "users", "current_stage", "confidence_profile", "confidence", "tension",
                 "validation_need", "state_generation", "unresolved_revision", "claim", "mastery",
                 "inference", "longitudinal", "observation"):
        assert word not in tokens, word


def test_44_and_45_no_llm():
    tokens = _tokens().lower()
    for word in ("anthropic", "claude", "openai", "client", "messages", "prompt", "classif", "loads",
                 "requests", "http", "completion"):
        assert word not in tokens, word
    for word in ("create", "model", "llm", "messages", "system", "chat", "json_loads"):
        assert word not in _words(), word
    assert not {"anthropic", "openai", "requests", "httpx", "os", "random", "datetime", "time"} & _imports()


def test_46_and_47_no_chat_runtime_nor_route_classifier():
    tokens = _tokens().lower()
    for word in ("web-chat", "web_chat", "_classify_intent", "education", "coach", "decrypt", "checklist",
                 "fastapi", "route", "request"):
        assert word not in tokens, word
    for word in ("current_message", "conversation_history", "context_turns", "interactionfocusinput",
                 "semanticcontextturn", "message", "conversation", "turn"):
        assert word not in tokens, word


def test_48_no_sqlalchemy_model_built_nor_imported():
    assert not {"core.models", "sqlalchemy", "sqlalchemy.orm"} & _imports()
    tokens = _tokens().split("\n")
    for name in ("PedagogicalTaxonomyRelease", "CoreCapabilityDefinition", "CapabilityTaxonomyMembership",
                 "ObservationCapability", "Base", "Column", "declarative_base", "mapped_column"):
        assert name not in tokens, name


def test_49_no_migration_and_no_step6_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0009_competency_inference_state.py" and len(versions) == 9
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "Focus" not in models and "Adaptation" not in models


def test_50_no_database_write_and_only_t4b_reads():
    tokens = _tokens().split("\n")
    for forbidden in ("add", "add_all", "flush", "commit", "rollback", "begin", "begin_nested", "delete", "update",
                      "insert", "merge", "execute", "text", "with_for_update", "SessionLocal", "get_db",
                      "create_candidate_release", "create_capability_definition", "attach_capability_to_release",
                      "activate_release", "map_observation_capability", "bootstrap_taxonomy_v1",
                      "activate_taxonomy_v1", "verify_taxonomy_v1", "get_observation_capabilities"):
        assert forbidden not in tokens, forbidden
    tree = _tree()
    t4b = [a.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "core.taxonomy_service"
           for a in n.names]
    assert sorted(t4b) == ["TaxonomyServiceError", "get_active_release", "get_release_capabilities"]
    spec = [a.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "core.pedagogy.taxonomy_v1"
            for a in n.names]
    assert sorted(spec) == ["CAPABILITY_CODES", "COMPETENCY_CODES", "TaxonomySpecError", "load_taxonomy_v1"]
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "db"}
    assert used == {"no_autoflush"}


def _function(name):
    return next(n for n in _tree().body if isinstance(n, ast.FunctionDef) and n.name == name)


def _reachable(name, seen=None):
    """Fonctions du module appelées (transitivement) depuis `name`."""
    seen = set() if seen is None else seen
    seen.add(name)
    module_functions = {n.name for n in _tree().body if isinstance(n, ast.FunctionDef)}
    for node in ast.walk(_function(name)):
        if isinstance(node, ast.Name) and node.id in module_functions and node.id not in seen:
            _reachable(node.id, seen)
    return seen


def test_51_validator_never_touches_the_database(monkeypatch, taxonomy):
    """Le validateur ne lit jamais la base ; il recharge seulement la SPEC
    canonique de la policy (policy.load_spec, Python pur) pour défendre la
    frontière publique."""
    reachable = _reachable("validate_focus_proposal")
    assert not {"load_current_focus_taxonomy", "_acquire_rows", "_policy_spec"} & reachable
    assert "_canonical_spec" in reachable
    for name in reachable:
        names = {n.id for n in ast.walk(_function(name)) if isinstance(n, ast.Name)}
        assert not {"db", "get_active_release", "get_release_capabilities"} & names, name

    def forbidden(*args, **kwargs):
        raise AssertionError("lecture de la base inattendue")
    for name in ("get_active_release", "get_release_capabilities"):
        monkeypatch.setattr(af, name, forbidden)
    assert validate_focus_proposal(_proposal(), taxonomy).target.competency_code == "C10"


def test_52_same_proposal_and_taxonomy_give_the_same_result(monkeypatch, loaded):
    taxonomy, release, rows = loaded
    proposal = _proposal(target=_focus("C11", "C11_D@r1", "C11_B@r1"),
                         supporting=(_focus("C9", "C9_C@r1", "C9_B@r1"), _focus("C2"), _focus("C10", "C10_A@r1")))
    results = [validate_focus_proposal(proposal, taxonomy) for _ in range(5)]
    rebuilt, _, _ = _load(monkeypatch, release, rows)
    assert rebuilt == taxonomy and rebuilt is not taxonomy
    results.append(validate_focus_proposal(copy.deepcopy(proposal), rebuilt))
    assert all(r == results[0] for r in results)
    assert hash(results[0].target) == hash(results[-1].target)


def test_no_score_rank_or_probability():
    words = _words()
    for word in ("score", "scores", "confidence", "probability", "weight", "weights", "rank", "ranking",
                 "similarity", "certainty", "best", "match_score", "threshold", "percent"):
        assert word not in words, word
    floats = [n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert floats == []


def test_no_response_logic_nor_later_step6_brick():
    words = _words()
    for name in ("safeassumption", "validationopportunity", "pedagogicalresponsecontext", "posturePlan".lower(),
                 "stay", "clarify", "deepen", "apply", "generalize", "integrate", "explain", "simplify",
                 "challenge", "surface", "candidate_targets", "candidates", "subgoals"):
        assert name not in words, name
    assert "candidate_targets" not in _tokens()


def test_module_is_not_wired_to_the_application():
    # Étape 6.1B2 : classificateur sémantique non branché, consommateur de la
    # seule frontière publique 6-1B1 (contrats + validate_focus_proposal ;
    # jamais load_current_focus_taxonomy, jamais un privé) ; lui-même non
    # branché : tests/test_adaptation_focus_classifier.py.
    # Étape 6.1C : socle positif localisé, consommateur du seul focus VALIDÉ
    # (contrats de sortie ; jamais FocusProposal, validate_focus_proposal ni
    # load_current_focus_taxonomy) ; non branché : tests/test_adaptation_assumptions.py.
    step6_consumers = {"core/adaptation_focus_classifier.py": {
        "AMBIGUOUS", "COMPETENCY_ONLY", "COMPOSITE", "FOCUS_POLICY_VERSION", "FOCUS_SCHEMA_VERSION", "LOCALIZED",
        "NEUTRAL", "RESOLVED", "CurrentFocusTaxonomy", "FocusError", "FocusProposal", "InvalidFocusProposal",
        "ProposedCompetencyFocus", "UnsupportedFocusPolicy", "validate_focus_proposal"},
        "core/adaptation_assumptions.py": {
        "COMPETENCY_ONLY", "FOCUS_POLICY_VERSION", "FOCUS_SCHEMA_VERSION", "LOCALIZED", "RESOLUTION_STATUSES",
        "RESOLVED", "SCOPE_MODES", "CompetencyFocus", "InteractionCompetencyFocus"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_focus" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_focus", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if (rel != "core/adaptation_focus.py" and rel not in step6_consumers
                and "adaptation_focus" in path.read_text(encoding="utf-8", errors="replace")):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("load_current_focus_taxonomy", "validate_focus_proposal", "FocusProposal",
                 "InteractionCompetencyFocus"):
        assert name not in api, name


# --------------------------------------------------------------------------
# 2. PostgreSQL réel : SPEC V1 -> bootstrap / activation -> 6-1B1
# --------------------------------------------------------------------------

T4_TABLES = ("pedagogical_taxonomy_releases", "core_capability_definitions", "capability_taxonomy_memberships",
             "observation_capabilities")


def _dump(engine):  # noqa: F811
    with engine.connect() as conn:
        return {table: sorted(map(repr, conn.execute(sa.text(f"SELECT * FROM {table}")).all()))
                for table in T4_TABLES}


def _raw(engine, sql, **params):  # noqa: F811
    """Écriture hors service (corruption volontaire, jamais par 6-1B1)."""
    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def _active_v1(Sessions):  # noqa: F811
    from core.pedagogy.taxonomy_bootstrap import activate_taxonomy_v1, bootstrap_taxonomy_v1
    with Sessions() as session:
        bootstrap_taxonomy_v1(session)
        result = activate_taxonomy_v1(session)
        session.commit()
        return result.release_id


def _db_definition_ids(engine):  # noqa: F811
    return {(r["capability_code"], r["semantic_revision"]): r["id"]
            for r in _rows(engine, "SELECT id, capability_code, semantic_revision FROM core_capability_definitions")}


def _spec_definition(Sessions, code, revision, **overrides):  # noqa: F811
    from core import taxonomy_service as tax
    kwargs = {f: SPEC_BY_CODE[code][f] for f in v1.PERSISTED_CAPABILITY_FIELDS}
    kwargs.update(semantic_revision=revision, **overrides)
    with Sessions() as session:
        definition = tax.create_capability_definition(session, **kwargs)
        session.commit()
        return definition.id


def _load_pg(Sessions):  # noqa: F811
    with Sessions() as session:
        taxonomy = load_current_focus_taxonomy(session)
        assert not session.new and not session.dirty and not session.deleted
        return taxonomy


def test_pg_no_active_release_is_unavailable(engine, Sessions):  # noqa: F811
    from core.pedagogy.taxonomy_bootstrap import bootstrap_taxonomy_v1
    with Sessions() as session:
        with pytest.raises(FocusTaxonomyUnavailable):
            load_current_focus_taxonomy(session)
        bootstrap_taxonomy_v1(session)  # candidate seulement
        session.commit()
    before = _dump(engine)
    with Sessions() as session:
        with pytest.raises(FocusTaxonomyUnavailable):
            load_current_focus_taxonomy(session)
    assert _dump(engine) == before
    assert _rows(engine, "SELECT status FROM pedagogical_taxonomy_releases") == [{"status": "candidate"}]


def test_pg_spec_v1_active_release_to_validated_focus(engine, Sessions):  # noqa: F811
    release_id = _active_v1(Sessions)
    before = _dump(engine)
    taxonomy = _load_pg(Sessions)
    db_ids = _db_definition_ids(engine)

    assert taxonomy.taxonomy_release_id == release_id
    assert (taxonomy.taxonomy_version_key, taxonomy.taxonomy_spec_fingerprint, taxonomy.focus_policy_version) == (
        "oryx-v1", GOLDEN_V1_FINGERPRINT, "focus-policy-1")
    assert [c.capability_code for c in taxonomy.capabilities] == NATURAL_CAPABILITIES
    assert [c.competency_code for c in taxonomy.competencies] == NATURAL_COMPETENCIES
    assert {(c.capability_code, c.semantic_revision): c.definition_id for c in taxonomy.capabilities} == db_ids

    proposal = _proposal(target=_focus("C10", "C10_D@r1", "C10_B@r1"),
                         supporting=(_focus("C7", "C7_A@r1"), _focus("C2")))
    focus = validate_focus_proposal(proposal, taxonomy)
    assert focus == InteractionCompetencyFocus(
        schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION,
        taxonomy_release_id=release_id, taxonomy_spec_fingerprint=GOLDEN_V1_FINGERPRINT,
        resolution_status="resolved",
        target=CompetencyFocus(competency_code="C10", scope_mode="localized",
                               capability_definition_ids=(db_ids[("C10_B", 1)], db_ids[("C10_D", 1)])),
        supporting=(CompetencyFocus(competency_code="C2", scope_mode="competency_only", capability_definition_ids=()),
                    CompetencyFocus(competency_code="C7", scope_mode="localized",
                                    capability_definition_ids=(db_ids[("C7_A", 1)],))),
    )
    assert _dump(engine) == before
    assert _load_pg(Sessions) == taxonomy
    forged = replace(taxonomy, capabilities=tuple(
        replace(c, label="forgé") if c.capability_code == "C10_B" else c for c in taxonomy.capabilities))
    with pytest.raises(InvalidFocusTaxonomy, match="label"):
        validate_focus_proposal(proposal, forged)


def test_pg_load_never_flushes_pending_caller_changes(engine, Sessions):  # noqa: F811
    from core.models import PedagogicalTaxonomyRelease
    _active_v1(Sessions)
    with Sessions() as session:
        pending = PedagogicalTaxonomyRelease(id=uuid.uuid4(), version_key="pending-test", status="candidate",
                                             spec_fingerprint="sha256:pending", created_at=sa.func.now())
        session.add(pending)
        load_current_focus_taxonomy(session)
        assert pending in session.new
        session.rollback()
    assert _rows(engine, "SELECT count(*) AS n FROM pedagogical_taxonomy_releases") == [{"n": 1}]


def test_pg_same_code_other_revision_is_never_remapped(engine, Sessions):  # noqa: F811
    """C10_B r2 (autre definition_id) existe en base, hors de la release
    active : le token C10_B@r2 est refusé, C10_B@r1 reste l'UUID r1."""
    _active_v1(Sessions)
    r2 = _spec_definition(Sessions, "C10_B", 2, definition="sens révisé (test)")
    taxonomy = _load_pg(Sessions)
    with pytest.raises(InvalidFocusProposal, match="absent de la taxonomie"):
        validate_focus_proposal(_proposal(target=_focus("C10", "C10_B@r2")), taxonomy)
    focus = validate_focus_proposal(_proposal(), taxonomy)
    assert focus.target.capability_definition_ids == (_db_definition_ids(engine)[("C10_B", 1)],)
    assert r2 not in focus.target.capability_definition_ids


def test_pg_active_release_pointing_to_another_revision_is_invalid(engine, Sessions):  # noqa: F811
    """oryx-v1 dont C10_B pointe (SQL brut) vers une r2 au contenu
    identique : jamais accepté par capability_code, jamais réparé."""
    _active_v1(Sessions)
    r2 = _spec_definition(Sessions, "C10_B", 2)
    _raw(engine, "UPDATE capability_taxonomy_memberships SET capability_definition_id = :r2 "
                 "WHERE capability_definition_id = (SELECT id FROM core_capability_definitions "
                 "WHERE capability_code = 'C10_B' AND semantic_revision = 1)", r2=r2)
    before = _dump(engine)
    with Sessions() as session:
        with pytest.raises(InvalidFocusTaxonomy, match="C10_B .*semantic_revision"):
            load_current_focus_taxonomy(session)
    assert _dump(engine) == before


def test_pg_drifted_definition_text_is_invalid(engine, Sessions):  # noqa: F811
    _active_v1(Sessions)
    _raw(engine, "UPDATE core_capability_definitions SET label = label || ' ' WHERE capability_code = 'C5_C'")
    with Sessions() as session:
        with pytest.raises(InvalidFocusTaxonomy, match="C5_C .*label"):
            load_current_focus_taxonomy(session)


def test_pg_another_active_release_with_the_same_codes_is_unsupported(engine, Sessions):  # noqa: F811
    """Release active « test » portant les 45 mêmes définitions r1 (mêmes
    capability_code, même sens) : identité non enregistrée => refus, jamais
    un repli sur focus-policy-1."""
    from core import taxonomy_service as tax
    _active_v1(Sessions)
    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key="test-same-codes",
                                               spec_fingerprint=f"sha256:{uuid.uuid4()}")
        for definition_id in _db_definition_ids(engine).values():
            tax.attach_capability_to_release(session, release_id=release.id, capability_definition_id=definition_id)
        tax.activate_release(session, release_id=release.id)
        session.commit()
    before = _dump(engine)
    with Sessions() as session:
        with pytest.raises(UnsupportedFocusPolicy):
            load_current_focus_taxonomy(session)
    assert _dump(engine) == before
