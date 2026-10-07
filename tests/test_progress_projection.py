"""Tests de l'Étape 6.4A : projection descriptive de l'état pédagogique
actuel (core/progress_projection.py).

1. Contrats : versions, vocabulaires fermés, libellés visibles exacts,
   dataclasses immuables, API publique, hiérarchie d'erreurs.
2. Douze cartes C1 -> C12, toujours (snapshot vide, partiel, ordre d'entrée).
3. Absence Step 5 != non_etabli.
4. Stade visible = current_stage Step 5, libellés V1.
5. Couverture : localized / competency_only / mixed / none ; jamais de repli
   sur une claim inférieure ; implied et mastery projetés tels quels.
6. Chaîne 6-1A réelle (projection pure de 6-1A depuis des enregistrements
   construits à la main) -> 6-4A.
7. Inertie : tensions, contexte de révision, besoins de validation, profil
   de confiance, évaluation de maîtrise, compteur technique, provenance.
8. Fail-closed : aucune corruption convertie en carte « sans état ».
9. Exclusions : aucun UUID, aucun identifiant, aucun score, aucune
   recommandation, aucun classement, aucune capacité « moins observée ».
10. Contrat statique : imports, pureté, aucune base / migration / runtime,
    libellés de compétences jamais recopiés, taxonomie inchangée.
11. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
    SKIPPÉS) : SPEC V1 + T3 -> T5 -> T6 réels -> load_adaptation_state ->
    project_current_progress, sans aucune requête pendant la projection.
"""
import ast
import dataclasses
import inspect
import re
import uuid
from types import MappingProxyType

import pytest
import sqlalchemy as sa

from core import progress_projection as projection_module
from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateSnapshot,
    AdaptationTension,
    AdaptationValidationNeed,
    CapabilitySemanticRef,
    CompetencyAdaptationSnapshot,
    load_adaptation_state,
)
from core.inference_service import CLAIM_STAGES as STEP5_CLAIM_STAGES
from core.inference_service import CURRENT_STAGES as STEP5_CURRENT_STAGES
from core.inference_service import NON_ETABLI as STEP5_NON_ETABLI
from core.pedagogy import taxonomy_v1 as v1
from core.progress_projection import (
    CLAIM_STAGE_ORDER,
    COVERAGE_COMPETENCY_ONLY,
    COVERAGE_LOCALIZED,
    COVERAGE_MIXED,
    COVERAGE_MODES,
    COVERAGE_NONE,
    CURRENT_PROGRESS_POLICY_VERSION,
    CURRENT_PROGRESS_SCHEMA_VERSION,
    NO_STATE_LABEL,
    NON_ETABLI,
    VISIBLE_STAGE_LABELS,
    CompetencyCurrentProgress,
    CurrentProgressProjection,
    InvalidProgressProjectionArgument,
    InvalidProgressProjectionState,
    ProgressProjectionError,
    UnsupportedProgressProjectionVersion,
    VisibleCapabilityProjection,
    project_current_progress,
)
from tests.test_adaptation_assumptions import _dump, _pipeline, _v1
from tests.test_adaptation_state import World, application_world, mastery_world, profile
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_taxonomy_v1 import GOLDEN_V1_FINGERPRINT

MODULE_PATH = REPO_ROOT / "core" / "progress_projection.py"
NS = uuid.UUID("6a4a0000-0000-4000-8000-000000000000")
STAGES = ("discovery", "comprehension", "application", "mastery")
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


RELEASE = uid("release")


def d(code: str) -> uuid.UUID:
    """definition_id stable d'une capacité (ex. d("C7_A"))."""
    return uid(f"d-{code}")


def canonical_labels() -> dict:
    spec, _ = v1.load_taxonomy_v1()
    return {c["competency_code"]: c["label"] for c in spec["competencies"]}


# --------------------------------------------------------------------------
# Constructeurs (objets publics de 6-1A construits à la main)
# --------------------------------------------------------------------------

def cap(code, definition_id=None, *, revision=1, label=None):
    return CapabilitySemanticRef(membership_id=uid(f"m-{code}-{definition_id}"),
                                 definition_id=definition_id or d(code), capability_code=code,
                                 semantic_revision=revision, label=label or f"label {code}")


def claim(stage, ids=(), only=False, *, status=None, basis_mode=None, confidence=None, assessment=None):
    status = status or ("established" if ids or only else "not_established")
    established = status == "established"
    return AdaptationStageClaim(
        stage=stage, status=status,
        basis_mode=basis_mode or ("direct" if established else "none"),
        represented_capability_definition_ids=tuple(ids), competency_only_basis=only,
        confidence_profile=(confidence if confidence is not None else MappingProxyType(profile(*ids)))
        if established else None,
        mastery_assessment=assessment if stage == "mastery" else None)


def claims(ladder=None, **overrides):
    """ladder : {stage: (ids, competency_only)} ; stade absent => not_established."""
    ladder = ladder or {}
    return tuple(claim(stage, *(ladder[stage] if stage in ladder else ((), False)), **overrides.get(stage, {}))
                 for stage in STAGES)


def snap(code="C7", current="application", ladder=None, *, capabilities=None, claim_tuple=None,
         generation=1, tension_state="none", tensions=(), context=None, needs=(), claim_overrides=None,
         release=RELEASE):
    capabilities = capabilities if capabilities is not None else tuple(cap(f"{code}_{x}") for x in "ABCD")
    return CompetencyAdaptationSnapshot(
        competency_code=code, current_stage=current, tension_state=tension_state,
        active_inference_run_id=uid(f"run-{code}"), longitudinal_assessment_run_id=uid(f"t5-{code}"),
        state_generation=generation, taxonomy_release_id=release, capabilities=capabilities,
        claims=claim_tuple if claim_tuple is not None else claims(ladder, **(claim_overrides or {})),
        tensions=tensions, unresolved_revision_context=context, validation_needs=needs)


def state(*competencies, user_id="u"):
    return AdaptationStateSnapshot(user_id=user_id, competencies=tuple(competencies))


def project(st):
    return project_current_progress(state=st)


def card(projection, code):
    (found,) = [c for c in projection.competencies if c.competency_code == code]
    return found


def caps(card_):
    return [(c.capability_code, c.semantic_revision, c.label) for c in card_.represented_capabilities]


def absent(code, label):
    return CompetencyCurrentProgress(competency_code=code, competency_label=label, state_present=False,
                                     stage_code=None, stage_label=NO_STATE_LABEL, coverage_mode=COVERAGE_NONE,
                                     represented_capabilities=())


def leaves(value):
    """Toutes les valeurs atomiques d'une projection (dataclasses / tuples)."""
    if dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from leaves(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from leaves(item)
    else:
        yield value


A, B, C, D = d("C7_A"), d("C7_B"), d("C7_C"), d("C7_D")
# C7 Application localisée A/B/C ; Comprehension / Discovery implied (même périmètre).
C7_APPLICATION = {"discovery": ((A, B, C), False), "comprehension": ((A, B, C), False),
                  "application": ((A, B, C), False)}


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_exact():
    assert CURRENT_PROGRESS_SCHEMA_VERSION == "current-progress-projection-v1"
    assert CURRENT_PROGRESS_POLICY_VERSION == "current-progress-policy-1"


def test_visible_stage_labels_are_exactly_the_v1_mapping():
    assert dict(VISIBLE_STAGE_LABELS) == {
        "non_etabli": "Encore peu observé par Oryx",
        "discovery": "Notion reconnue",
        "comprehension": "Mécanisme compris",
        "application": "Utilisé en situation",
        "mastery": "Raisonnement solide dans des contextes variés",
    }
    assert NO_STATE_LABEL == "Pas encore observé par Oryx"
    assert NO_STATE_LABEL not in VISIBLE_STAGE_LABELS.values()
    assert isinstance(VISIBLE_STAGE_LABELS, MappingProxyType)
    with pytest.raises(TypeError):
        VISIBLE_STAGE_LABELS["application"] = "Avancé"  # type: ignore[index]


def test_visible_labels_never_use_level_vocabulary():
    texts = [*VISIBLE_STAGE_LABELS.values(), NO_STATE_LABEL]
    for text in texts:
        lowered = text.lower()
        for word in ("débutant", "intermédiaire", "avancé", "expert", "niveau", "level", "%", "/", "score",
                     "faible", "zéro", "0", "1", "2", "3", "4"):
            assert word not in lowered, (text, word)


def test_stage_vocabulary_mirrors_step5_exactly():
    assert NON_ETABLI == STEP5_NON_ETABLI == "non_etabli"
    assert CLAIM_STAGE_ORDER == STEP5_CLAIM_STAGES == STAGES
    assert set(VISIBLE_STAGE_LABELS) == set(STEP5_CURRENT_STAGES) == {NON_ETABLI, *CLAIM_STAGE_ORDER}


def test_coverage_vocabulary_is_closed():
    assert (COVERAGE_NONE, COVERAGE_LOCALIZED, COVERAGE_COMPETENCY_ONLY, COVERAGE_MIXED) == (
        "none", "localized", "competency_only", "mixed")
    assert COVERAGE_MODES == ("none", "localized", "competency_only", "mixed")


def test_dataclasses_are_frozen_keyword_only_with_the_exact_fields():
    expected = {
        VisibleCapabilityProjection: ["capability_code", "semantic_revision", "label"],
        CompetencyCurrentProgress: ["competency_code", "competency_label", "state_present", "stage_code",
                                    "stage_label", "coverage_mode", "represented_capabilities"],
        CurrentProgressProjection: ["schema_version", "policy_version", "competencies"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls))
        with pytest.raises(TypeError):
            cls(*range(len(names)))  # positionnel refusé
    projection = project(state(snap("C7", "application", C7_APPLICATION)))
    for obj in (projection, projection.competencies[0], card(projection, "C7").represented_capabilities[0]):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, dataclasses.fields(obj)[0].name, "x")


def test_output_containers_are_tuples_never_lists_or_dicts():
    projection = project(state(snap("C7", "application", C7_APPLICATION)))
    assert type(projection.competencies) is tuple
    for card_ in projection.competencies:
        assert type(card_.represented_capabilities) is tuple
        assert all(type(c) is VisibleCapabilityProjection for c in card_.represented_capabilities)
    for value in leaves(projection):
        assert value is None or type(value) in (str, int, bool), value


def test_error_hierarchy():
    for cls in (InvalidProgressProjectionArgument, InvalidProgressProjectionState,
                UnsupportedProgressProjectionVersion):
        assert issubclass(cls, ProgressProjectionError)
    assert issubclass(ProgressProjectionError, Exception)
    assert not issubclass(InvalidProgressProjectionState, InvalidProgressProjectionArgument)


def test_public_api_is_exactly_project_current_progress_keyword_only():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"project_current_progress"}
    params = list(inspect.signature(project_current_progress).parameters.values())
    assert [(p.name, p.kind) for p in params] == [("state", inspect.Parameter.KEYWORD_ONLY)]
    with pytest.raises(TypeError):
        project_current_progress(state())  # type: ignore[misc]


# --------------------------------------------------------------------------
# 2. Douze cartes C1 -> C12
# --------------------------------------------------------------------------

def test_empty_snapshot_is_valid_and_yields_twelve_cards_without_state():
    projection = project(state())
    assert projection.schema_version == CURRENT_PROGRESS_SCHEMA_VERSION
    assert projection.policy_version == CURRENT_PROGRESS_POLICY_VERSION
    assert [c.competency_code for c in projection.competencies] == list(COMPETENCY_ORDER)
    assert len(projection.competencies) == 12
    labels = canonical_labels()
    assert projection.competencies == tuple(absent(code, labels[code]) for code in COMPETENCY_ORDER)
    for card_ in projection.competencies:
        assert card_.state_present is False and card_.stage_code is None
        assert card_.stage_label == "Pas encore observé par Oryx"
        assert card_.coverage_mode == "none" and card_.represented_capabilities == ()


def test_competency_labels_are_the_canonical_v1_labels_in_natural_order():
    projection = project(state())
    assert [(c.competency_code, c.competency_label) for c in projection.competencies] == [
        (code, canonical_labels()[code]) for code in COMPETENCY_ORDER]
    assert card(projection, "C1").competency_label == "Fondations de l'investissement"
    assert card(projection, "C8").competency_label == "Efficacité du capital / ROIC"
    assert card(projection, "C12").competency_label == "Raisonnement portefeuille"
    # Ordre naturel, jamais lexical (C2 avant C10).
    codes = [c.competency_code for c in projection.competencies]
    assert codes.index("C2") < codes.index("C10")


def test_partial_snapshot_still_projects_twelve_cards():
    projection = project(state(snap("C7", "application", C7_APPLICATION)))
    assert [c.competency_code for c in projection.competencies] == list(COMPETENCY_ORDER)
    assert [c.state_present for c in projection.competencies] == [code == "C7" for code in COMPETENCY_ORDER]


def test_full_snapshot_projects_twelve_present_cards():
    full = state(*(snap(code, "comprehension", {"discovery": ((d(f"{code}_A"),), False),
                                                "comprehension": ((d(f"{code}_A"),), False)})
                   for code in COMPETENCY_ORDER))
    projection = project(full)
    assert len(projection.competencies) == 12 and all(c.state_present for c in projection.competencies)
    for code, card_ in zip(COMPETENCY_ORDER, projection.competencies):
        assert card_.competency_code == code
        assert caps(card_) == [(f"{code}_A", 1, f"label {code}_A")]


def test_input_order_never_becomes_output_order():
    parts = [snap("C12", "discovery", {"discovery": ((d("C12_B"),), False)}),
             snap("C2", NON_ETABLI), snap("C10", "comprehension", {"comprehension": ((), True)}),
             snap("C7", "application", C7_APPLICATION)]
    projections = [project(state(*order)) for order in (parts, parts[::-1], [parts[2], parts[0], parts[3], parts[1]])]
    assert projections[0] == projections[1] == projections[2]
    assert [c.competency_code for c in projections[0].competencies] == list(COMPETENCY_ORDER)


# --------------------------------------------------------------------------
# 3. Absence != non_etabli
# --------------------------------------------------------------------------

def test_absent_and_non_etabli_are_never_merged():
    projection = project(state(snap("C8", NON_ETABLI)))
    missing, evaluated = card(projection, "C7"), card(projection, "C8")
    assert (missing.state_present, missing.stage_code, missing.stage_label) == (
        False, None, "Pas encore observé par Oryx")
    assert (evaluated.state_present, evaluated.stage_code, evaluated.stage_label) == (
        True, "non_etabli", "Encore peu observé par Oryx")
    assert missing.coverage_mode == evaluated.coverage_mode == "none"
    assert missing.represented_capabilities == evaluated.represented_capabilities == ()
    assert dataclasses.replace(missing, competency_code="C8", competency_label=evaluated.competency_label) != evaluated


def test_non_etabli_never_looks_for_a_non_etabli_claim():
    """Les claims Step 5 sont discovery -> mastery : non_etabli => coverage
    none sans rien chercher (même des claims établies ne changent rien)."""
    quiet = project(state(snap("C8", NON_ETABLI)))
    noisy = project(state(snap("C8", NON_ETABLI, {"discovery": ((d("C8_A"),), True)})))
    assert card(quiet, "C8") == card(noisy, "C8")
    assert card(noisy, "C8").represented_capabilities == ()


# --------------------------------------------------------------------------
# 4. Stade visible = current_stage Step 5
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stage, label", [
    ("non_etabli", "Encore peu observé par Oryx"),
    ("discovery", "Notion reconnue"),
    ("comprehension", "Mécanisme compris"),
    ("application", "Utilisé en situation"),
    ("mastery", "Raisonnement solide dans des contextes variés"),
])
def test_stage_label_is_exactly_the_v1_label_of_current_stage(stage, label):
    ladder = {} if stage == NON_ETABLI else {s: ((A,), False) for s in STAGES[:STAGES.index(stage) + 1]}
    result = card(project(state(snap("C7", stage, ladder))), "C7")
    assert (result.stage_code, result.stage_label, result.state_present) == (stage, label, True)


def test_stage_is_never_recomputed_from_the_claims():
    """current_stage application maintenu alors que seule Discovery est
    établie : le stade visible reste Application (aucune seconde inférence)."""
    result = card(project(state(snap("C7", "application", {"discovery": ((A,), False)}))), "C7")
    assert (result.stage_code, result.stage_label) == ("application", "Utilisé en situation")


def test_stage_is_never_raised_by_a_higher_established_claim():
    ladder = {s: ((A, B), False) for s in STAGES}
    result = card(project(state(snap("C7", "comprehension", ladder))), "C7")
    assert (result.stage_code, result.stage_label) == ("comprehension", "Mécanisme compris")


# --------------------------------------------------------------------------
# 5. Couverture
# --------------------------------------------------------------------------

def test_localized_projects_exactly_the_represented_capabilities():
    result = card(project(state(snap("C7", "application", C7_APPLICATION))), "C7")
    assert result.coverage_mode == "localized"
    assert caps(result) == [("C7_A", 1, "label C7_A"), ("C7_B", 1, "label C7_B"), ("C7_C", 1, "label C7_C")]
    assert "C7_D" not in repr(result)  # aucun complément


def test_localized_capability_fields_come_from_the_snapshot_catalogue():
    catalogue = (cap("C7_A", label="Cash conversion"), cap("C7_B", revision=2, label="Besoin en fonds de roulement"),
                 cap("C7_C"), cap("C7_D"))
    result = card(project(state(snap("C7", "application", {"application": ((A, B), False)},
                                     capabilities=catalogue))), "C7")
    assert result.represented_capabilities == (
        VisibleCapabilityProjection(capability_code="C7_A", semantic_revision=1, label="Cash conversion"),
        VisibleCapabilityProjection(capability_code="C7_B", semantic_revision=2,
                                    label="Besoin en fonds de roulement"))


def test_localized_order_is_the_catalogue_order_not_lexical_nor_uuid():
    # Catalogue volontairement non lexical : D, B, A, C.
    catalogue = (cap("C7_D"), cap("C7_B"), cap("C7_A"), cap("C7_C"))
    result = card(project(state(snap("C7", "application", {"application": ((D, B, A), False)},
                                     capabilities=catalogue))), "C7")
    assert [c.capability_code for c in result.represented_capabilities] == ["C7_D", "C7_B", "C7_A"]


def test_competency_only_never_invents_a_capability():
    result = card(project(state(snap("C7", "comprehension", {"comprehension": ((), True)}))), "C7")
    assert (result.coverage_mode, result.represented_capabilities) == ("competency_only", ())


def test_mixed_projects_only_the_explicitly_localized_capabilities():
    result = card(project(state(snap("C7", "application", {"application": ((A, B), True)}))), "C7")
    assert result.coverage_mode == "mixed"
    assert [c.capability_code for c in result.represented_capabilities] == ["C7_A", "C7_B"]
    assert "C7_C" not in repr(result) and "C7_D" not in repr(result)


@pytest.mark.parametrize("current", ["discovery", "comprehension", "application", "mastery"])
def test_current_claim_not_established_gives_no_coverage_and_never_borrows_another_scope(current):
    """Stade maintenu, claim courante not_established : stade conservé,
    aucune capacité, aucun emprunt à une claim inférieure ou supérieure."""
    ladder = {s: ((A, B), True) for s in STAGES if s != current}
    result = card(project(state(snap("C7", current, ladder))), "C7")
    assert (result.stage_code, result.stage_label) == (current, VISIBLE_STAGE_LABELS[current])
    assert (result.coverage_mode, result.represented_capabilities) == ("none", ())


def test_application_maintained_with_comprehension_established_keeps_application_without_scope():
    ladder = {"discovery": ((A, B, C), False), "comprehension": ((A, B, C), False)}
    result = card(project(state(snap("C7", "application", ladder, tension_state="open", tensions=(
        AdaptationTension(fragilized_stage="application", scope_mode="localized", capability_definition_ids=(C,),
                          revision_status="revalidation_needed"),)))), "C7")
    assert result == CompetencyCurrentProgress(
        competency_code="C7", competency_label=canonical_labels()["C7"], state_present=True,
        stage_code="application", stage_label="Utilisé en situation", coverage_mode="none",
        represented_capabilities=())


def test_only_the_current_claim_content_is_read():
    """Remplacer le contenu de TOUTES les autres claims (même par un contenu
    absurde) ne change jamais la carte : aucune claim inférieure n'est lue."""
    base = snap("C7", "application", C7_APPLICATION)
    reference = project(state(base))
    garbage = tuple(c if c.stage == "application" else dataclasses.replace(
        c, status="bogus", basis_mode="?", represented_capability_definition_ids=(uid("ghost"), uid("ghost")),
        competency_only_basis="yes") for c in base.claims)
    assert project(state(dataclasses.replace(base, claims=garbage))) == reference


def test_implied_current_claim_is_projected_as_reconstructed_by_6_1a():
    """Claim courante implied_by_higher_claim : périmètre déjà reconstruit
    par 6-1A, projeté tel quel ; aucune recherche de claim source."""
    ladder = {"comprehension": ((A, C), False), "application": ((A, C), False)}
    implied = snap("C7", "comprehension", ladder,
                   claim_overrides={"comprehension": {"basis_mode": "implied_by_higher_claim"}})
    result = card(project(state(implied)), "C7")
    assert (result.coverage_mode, [c.capability_code for c in result.represented_capabilities]) == (
        "localized", ["C7_A", "C7_C"])
    # basis_mode jamais lu : direct ou implied => même carte.
    direct = snap("C7", "comprehension", ladder)
    assert project(state(direct)) == project(state(implied))
    # La claim supérieure (source) n'est jamais relue : la vider ne change rien.
    orphan = dataclasses.replace(implied, claims=tuple(
        claim("application") if c.stage == "application" else c for c in implied.claims))
    assert project(state(orphan)) == project(state(implied))


def test_mastery_projects_its_canonical_partial_scope_never_the_whole_competency():
    ladder = {s: ((A, B), False) for s in STAGES}
    result = card(project(state(snap("C7", "mastery", ladder))), "C7")
    assert result.stage_label == "Raisonnement solide dans des contextes variés"
    assert (result.coverage_mode, [c.capability_code for c in result.represented_capabilities]) == (
        "localized", ["C7_A", "C7_B"])


def test_mastery_competency_only_and_mixed():
    only = card(project(state(snap("C7", "mastery", {"mastery": ((), True)}))), "C7")
    mixed = card(project(state(snap("C7", "mastery", {"mastery": ((B,), True)}))), "C7")
    assert (only.coverage_mode, only.represented_capabilities) == ("competency_only", ())
    assert (mixed.coverage_mode, caps(mixed)) == ("mixed", [("C7_B", 1, "label C7_B")])


def test_capability_identity_is_definition_id_never_capability_code():
    """Même capability_code, autre definition_id (révision 2) dans le
    catalogue : seul le definition_id EXACT est projeté."""
    r2 = uid("d-C7_A-r2")
    catalogue = (cap("C7_A", r2, revision=2, label="A révisée"), cap("C7_B"), cap("C7_C"), cap("C7_D"))
    result = card(project(state(snap("C7", "application", {"application": ((r2,), False)},
                                     capabilities=catalogue))), "C7")
    assert caps(result) == [("C7_A", 2, "A révisée")]
    with pytest.raises(InvalidProgressProjectionState):  # le definition_id r1 n'est jamais remappé par code
        project(state(snap("C7", "application", {"application": ((A,), False)}, capabilities=catalogue)))


# --------------------------------------------------------------------------
# 6. Chaîne 6-1A réelle (projection pure de 6-1A) -> 6-4A
# --------------------------------------------------------------------------

def test_real_6_1a_application_world():
    w = application_world()  # C8 Application directe localized C/A/B, inférieures implied
    result = card(project(state(w.project())), "C8")
    assert (result.stage_code, result.coverage_mode) == ("application", "localized")
    assert caps(result) == [("C8_A", 1, "label C8_A"), ("C8_B", 1, "label C8_B"), ("C8_C", 1, "label C8_C")]


def test_real_6_1a_implied_current_claim():
    w = application_world()
    w.current_stage = "comprehension"  # claim comprehension implied_by_higher_claim
    snapshot = w.project()
    assert w.claim("comprehension", snapshot).basis_mode == "implied_by_higher_claim"
    result = card(project(state(snapshot)), "C8")
    assert (result.stage_code, result.coverage_mode) == ("comprehension", "localized")
    assert [c.capability_code for c in result.represented_capabilities] == ["C8_A", "C8_B", "C8_C"]


def test_real_6_1a_maintained_stage_under_open_tension():
    """test_current_stage_is_never_turned_into_a_claim_scope (6-1A) ->
    Application conservé, aucune capacité, aucune tension exposée."""
    w = World()
    w.direct("comprehension", w.obs("comp", "A"))
    w.implied("discovery")
    w.current_stage = "application"
    w.context = {"schema_version": "revision-context-v1", "reason_code": "held", "motifs": [{"k": ["x"]}]}
    w.tension("application", "localized", w.m("C"), status="revalidation_needed")
    snapshot = w.project()
    assert snapshot.tension_state == "open" and snapshot.unresolved_revision_context is not None
    result = card(project(state(snapshot)), "C8")
    assert (result.stage_code, result.stage_label, result.coverage_mode, result.represented_capabilities) == (
        "application", "Utilisé en situation", "none", ())


def test_real_6_1a_mastery_partial_scope():
    reference = World()
    w = mastery_world(reference.d("B"), reference.d("A"))
    snapshot = w.project()
    result = card(project(state(snapshot)), "C8")
    # Refs A/B/C, périmètre canonique Mastery A/B : C n'est jamais ajouté.
    assert (result.stage_code, result.coverage_mode) == ("mastery", "localized")
    assert [c.capability_code for c in result.represented_capabilities] == ["C8_A", "C8_B"]


@pytest.mark.parametrize("observations, expected_mode, expected_codes", [
    ((("only", (), "competency_only"),), "competency_only", []),
    ((("loc", ("A", "B"), "localized"), ("only", (), "competency_only")), "mixed", ["C8_A", "C8_B"]),
])
def test_real_6_1a_competency_only_and_mixed(observations, expected_mode, expected_codes):
    w = World()
    w.direct("application", *(w.obs(name, *letters, localization=loc) for name, letters, loc in observations))
    w.current_stage = "application"
    result = card(project(state(w.project())), "C8")
    assert result.coverage_mode == expected_mode
    assert [c.capability_code for c in result.represented_capabilities] == expected_codes


def test_real_6_1a_non_etabli():
    w = World()  # current_stage non_etabli, quatre claims not_established
    result = card(project(state(w.project())), "C8")
    assert (result.state_present, result.stage_code, result.coverage_mode) == (True, "non_etabli", "none")


# --------------------------------------------------------------------------
# 7. Inertie
# --------------------------------------------------------------------------

def _rich_snapshot(**overrides):
    return snap("C7", "application", C7_APPLICATION, **overrides)


def test_validation_needs_are_inert():
    needs = (AdaptationValidationNeed(intent="confirmation", target_stage="mastery", scope_mode="localized",
                                      capability_definition_ids=(D,),
                                      reason_codes=("independence_not_established_on_current_basis",)),
             AdaptationValidationNeed(intent="revalidation", target_stage="application", scope_mode="whole_competency",
                                      capability_definition_ids=(), reason_codes=("unresolved_revision_motif",)))
    assert project(state(_rich_snapshot(needs=needs))) == project(state(_rich_snapshot()))


def test_confidence_profile_is_inert():
    other = MappingProxyType(profile(A, facts=("independence_not_established", "diversity_established")))
    weaker = _rich_snapshot(claim_overrides={s: {"confidence": other} for s in STAGES})
    assert project(state(weaker)) == project(state(_rich_snapshot()))


def test_state_generation_is_inert():
    assert project(state(_rich_snapshot(generation=1))) == project(state(_rich_snapshot(generation=97)))


def test_tensions_and_revision_context_are_inert():
    tensions = (AdaptationTension(fragilized_stage="application", scope_mode="localized",
                                  capability_definition_ids=(A,), revision_status="unresolved"),
                AdaptationTension(fragilized_stage="comprehension", scope_mode="whole_competency",
                                  capability_definition_ids=(), revision_status="revalidation_needed"))
    open_ = _rich_snapshot(tension_state="open", tensions=tensions,
                           context=MappingProxyType({"schema_version": "revision-context-v1", "reason_code": "held"}))
    reference = project(state(_rich_snapshot()))
    assert project(state(open_)) == reference
    text = repr(project(state(open_))).lower()
    # semantic_revision est la révision sémantique d'une capacité, jamais une révision Step 5.
    for word in ("tension", "warning", "alert", "severity", "fragil", "revision_status", "revalidation",
                 "red_flag", "unresolved"):
        assert word not in text, word


def test_mastery_assessment_is_inert():
    ladder = {s: ((A,), False) for s in STAGES}
    plain = snap("C7", "mastery", ladder)
    assessed = snap("C7", "mastery", ladder, claim_overrides={"mastery": {"assessment": MappingProxyType(
        {"schema_version": "mastery-assessment-v1", "represented_capability_definition_ids": (str(A), str(B))})}})
    assert project(state(plain)) == project(state(assessed))


def test_user_identity_and_technical_provenance_are_inert():
    base = _rich_snapshot()
    moved = dataclasses.replace(base, active_inference_run_id=uid("other-run"),
                                longitudinal_assessment_run_id=uid("other-t5"), taxonomy_release_id=uid("other-rel"),
                                capabilities=tuple(dataclasses.replace(c, membership_id=uid(f"x-{c.capability_code}"))
                                                   for c in base.capabilities))
    assert project(state(base, user_id="alice")) == project(state(moved, user_id="bob"))


def test_projection_is_deterministic_and_hashable():
    st = state(_rich_snapshot(), snap("C2", NON_ETABLI), snap("C10", "comprehension", {"comprehension": ((), True)}))
    first, second = project(st), project(st)
    assert first == second and hash(first) == hash(second)


# --------------------------------------------------------------------------
# 8. Fail-closed
# --------------------------------------------------------------------------

class _SubSnapshot(AdaptationStateSnapshot):
    pass


class _Str(str):
    pass


@pytest.mark.parametrize("value", [None, {}, "u", (), [snap()], snap()])
def test_wrong_snapshot_type_is_refused(value):
    with pytest.raises(InvalidProgressProjectionArgument):
        project_current_progress(state=value)


def test_snapshot_subclass_is_refused():
    with pytest.raises(InvalidProgressProjectionArgument):
        project_current_progress(state=_SubSnapshot(user_id="u", competencies=()))


def _bad_state(tamper):
    good = snap("C7", "application", C7_APPLICATION)
    claims_ = list(good.claims)
    current = claims_[2]

    def with_current(**changes):
        claims_[2] = dataclasses.replace(current, **changes)
        return state(dataclasses.replace(good, claims=tuple(claims_)))

    def with_catalogue(catalogue):
        return state(dataclasses.replace(good, capabilities=tuple(catalogue)))

    return {
        "competencies_list": lambda: AdaptationStateSnapshot(user_id="u", competencies=[good]),
        "competency_wrong_type": lambda: state(object()),
        "competency_dict": lambda: state({"competency_code": "C7"}),
        "unknown_competency": lambda: state(dataclasses.replace(good, competency_code="C13")),
        "lowercase_competency": lambda: state(dataclasses.replace(good, competency_code="c7")),
        "str_subclass_competency": lambda: state(dataclasses.replace(good, competency_code=_Str("C7"))),
        "duplicate_competency": lambda: state(good, good),
        "duplicate_competency_other_state": lambda: state(good, snap("C7", NON_ETABLI)),
        "unknown_stage": lambda: state(dataclasses.replace(good, current_stage="expert")),
        "stage_none": lambda: state(dataclasses.replace(good, current_stage=None)),
        "stage_str_subclass": lambda: state(dataclasses.replace(good, current_stage=_Str("application"))),
        "stage_level_number": lambda: state(dataclasses.replace(good, current_stage=3)),
        "claims_list": lambda: state(dataclasses.replace(good, claims=list(good.claims))),
        "claims_three": lambda: state(dataclasses.replace(good, claims=good.claims[:3])),
        "current_claim_missing": lambda: state(dataclasses.replace(
            good, claims=tuple(c for c in good.claims if c.stage != "application"))),
        "claims_disordered": lambda: state(dataclasses.replace(good, claims=good.claims[::-1])),
        "claims_duplicate": lambda: state(dataclasses.replace(
            good, claims=(good.claims[0], good.claims[1], good.claims[1], good.claims[3]))),
        "claim_wrong_type": lambda: state(dataclasses.replace(good, claims=(*good.claims[:3], object()))),
        "unknown_status": lambda: with_current(status="maybe"),
        "represented_unknown": lambda: with_current(represented_capability_definition_ids=(A, uid("ghost"))),
        "represented_duplicate": lambda: with_current(represented_capability_definition_ids=(A, A)),
        "represented_out_of_order": lambda: with_current(represented_capability_definition_ids=(B, A)),
        "represented_list": lambda: with_current(represented_capability_definition_ids=[A]),
        "represented_str": lambda: with_current(represented_capability_definition_ids=(str(A),)),
        "represented_by_code": lambda: with_current(represented_capability_definition_ids=("C7_A",)),
        "competency_only_not_bool": lambda: with_current(competency_only_basis=1),
        "established_without_basis": lambda: with_current(represented_capability_definition_ids=(),
                                                          competency_only_basis=False),
        "not_established_with_scope": lambda: with_current(status="not_established"),
        "not_established_with_competency_only": lambda: with_current(
            status="not_established", represented_capability_definition_ids=(), competency_only_basis=True),
        "other_competency_capability": lambda: state(dataclasses.replace(
            good, capabilities=(*good.capabilities, cap("C9_A")), claims=tuple(
                dataclasses.replace(c, represented_capability_definition_ids=(A, d("C9_A")))
                if c.stage == "application" else c for c in good.claims))),
        "catalogue_other_competency_only": lambda: with_catalogue((*good.capabilities, cap("C9_A"))),
        "catalogue_list": lambda: state(dataclasses.replace(good, capabilities=list(good.capabilities))),
        "catalogue_wrong_type": lambda: with_catalogue((*good.capabilities, object())),
        "catalogue_duplicate_definition": lambda: with_catalogue((*good.capabilities, cap("C7_E", A))),
        "catalogue_duplicate_code": lambda: with_catalogue((*good.capabilities, cap("C7_A", uid("other")))),
        "catalogue_definition_not_uuid": lambda: with_catalogue((cap("C7_A", str(A)), *good.capabilities[1:])),
        "catalogue_revision_bool": lambda: with_catalogue((cap("C7_A", revision=True), *good.capabilities[1:])),
        "catalogue_revision_zero": lambda: with_catalogue((cap("C7_A", revision=0), *good.capabilities[1:])),
        "catalogue_revision_str": lambda: with_catalogue((cap("C7_A", revision="1"), *good.capabilities[1:])),
        "catalogue_empty_label": lambda: with_catalogue((cap("C7_A", label="  "), *good.capabilities[1:])),
        "catalogue_bad_code": lambda: with_catalogue((cap("C7-A", A), *good.capabilities[1:])),
        "catalogue_code_prefix": lambda: with_catalogue((cap("C77_A", A), *good.capabilities[1:])),
    }[tamper]()


@pytest.mark.parametrize("tamper", [
    "competencies_list", "competency_wrong_type", "competency_dict", "unknown_competency", "lowercase_competency",
    "str_subclass_competency", "duplicate_competency", "duplicate_competency_other_state", "unknown_stage",
    "stage_none", "stage_str_subclass", "stage_level_number", "claims_list", "claims_three",
    "current_claim_missing", "claims_disordered", "claims_duplicate", "claim_wrong_type", "unknown_status",
    "represented_unknown", "represented_duplicate", "represented_out_of_order", "represented_list",
    "represented_str", "represented_by_code", "competency_only_not_bool", "established_without_basis",
    "not_established_with_scope", "not_established_with_competency_only", "other_competency_capability",
    "catalogue_other_competency_only", "catalogue_list", "catalogue_wrong_type", "catalogue_duplicate_definition",
    "catalogue_duplicate_code", "catalogue_definition_not_uuid", "catalogue_revision_bool",
    "catalogue_revision_zero", "catalogue_revision_str", "catalogue_empty_label", "catalogue_bad_code",
    "catalogue_code_prefix",
])
def test_corruption_fails_closed_and_is_never_a_no_state_card(tamper):
    st = _bad_state(tamper)
    with pytest.raises(InvalidProgressProjectionState):
        project(st)


def test_established_without_basis_is_never_converted_to_none():
    bad = snap("C7", "application", claim_overrides={"application": {"status": "established",
                                                                     "basis_mode": "direct"}})
    assert bad.claims[2].status == "established" and bad.claims[2].represented_capability_definition_ids == ()
    with pytest.raises(InvalidProgressProjectionState, match="sans aucune base"):
        project(state(bad))


def test_one_corrupted_competency_never_yields_a_partial_projection():
    with pytest.raises(InvalidProgressProjectionState):
        project(state(snap("C2", NON_ETABLI), snap("C7", "application", {"application": ((uid("ghost"),), False)})))


def test_unsupported_taxonomy_spec_fails_closed(monkeypatch):
    def broken():
        raise v1.TaxonomySpecError("fingerprint différent")

    monkeypatch.setattr(projection_module, "load_taxonomy_v1", broken)
    with pytest.raises(UnsupportedProgressProjectionVersion):
        project(state())


def test_taxonomy_spec_without_twelve_competencies_fails_closed(monkeypatch):
    spec, fingerprint = v1.load_taxonomy_v1()
    spec["competencies"] = spec["competencies"][:11]
    monkeypatch.setattr(projection_module, "load_taxonomy_v1", lambda: (spec, fingerprint))
    with pytest.raises(UnsupportedProgressProjectionVersion):
        project(state())


def test_competency_labels_are_read_from_the_canonical_spec(monkeypatch):
    """Les libellés viennent de load_taxonomy_v1 (jamais d'une copie locale)."""
    spec, fingerprint = v1.load_taxonomy_v1()
    for competency in spec["competencies"]:
        competency["label"] = f"<{competency['competency_code']}>"
    monkeypatch.setattr(projection_module, "load_taxonomy_v1", lambda: (spec, fingerprint))
    assert [c.competency_label for c in project(state()).competencies] == [f"<{c}>" for c in COMPETENCY_ORDER]


# --------------------------------------------------------------------------
# 9. Exclusions
# --------------------------------------------------------------------------

def _public_field_names():
    return {f.name for cls in (VisibleCapabilityProjection, CompetencyCurrentProgress, CurrentProgressProjection)
            for f in dataclasses.fields(cls)}


def test_no_technical_identifier_nor_step5_internal_in_public_contracts():
    names = _public_field_names()
    for forbidden in ("user_id", "uuid", "definition_id", "membership_id", "taxonomy_release_id",
                      "active_inference_run_id", "longitudinal_assessment_run_id", "state_generation",
                      "generation", "confidence_profile", "confidence", "mastery_assessment", "validation_needs",
                      "tensions", "tension_state", "unresolved_revision_context", "basis_mode", "claims",
                      "mapping_guidance", "definition"):
        assert not any(forbidden in name for name in names), forbidden
    assert not any("uuid" in name or name.endswith("_id") for name in names)


def test_no_uuid_nor_user_identity_in_any_output_value():
    snapshot = _rich_snapshot(tension_state="open", tensions=(AdaptationTension(
        fragilized_stage="application", scope_mode="localized", capability_definition_ids=(A,),
        revision_status="unresolved"),))
    projection = project(state(snapshot, user_id="user-secret-42"))
    assert not any(isinstance(value, uuid.UUID) for value in leaves(projection))
    text = repr(projection)
    assert not UUID_PATTERN.search(text)
    assert "user-secret-42" not in text and "user" not in text.lower()


def test_no_less_observed_missing_weak_or_gap_capabilities():
    projection = project(state(snap("C7", "application", {"application": ((A, B), False)})))
    result = card(projection, "C7")
    assert [c.capability_code for c in result.represented_capabilities] == ["C7_A", "C7_B"]
    text = repr(projection)
    assert "C7_C" not in text and "C7_D" not in text  # non représentées : ni listées, ni qualifiées
    names = _public_field_names()
    for word in ("less_observed", "missing", "unestablished", "weak", "gap", "todo", "to_improve", "lacking",
                 "absent_capabilities", "not_observed"):
        assert not any(word in name for name in names), word
        assert word not in text.lower(), word


def test_no_score_percent_xp_rank_level_average_or_count_field():
    parts = {part for name in _public_field_names() for part in name.split("_")}
    for word in ("score", "percent", "percentage", "xp", "points", "rank", "level", "average", "completion",
                 "count", "progress", "total", "ratio", "overall", "global", "percentile"):
        assert word not in parts, word


def test_no_recommendation_next_step_or_priority_field():
    names = _public_field_names()
    for word in ("next", "recommend", "priority", "suggest", "todo", "action", "module", "surface", "step",
                 "validation", "confirm"):
        assert not any(word in name for name in names), word


def test_no_ranking_or_comparison_between_competencies():
    names = _public_field_names()
    for word in ("strongest", "weakest", "best", "worst", "top", "areas_to_improve", "ranking", "strength"):
        assert not any(word in name for name in names), word
    # Les cartes ne dépendent jamais des autres compétences : C7 seule ou
    # entourée de compétences plus / moins documentées => même carte.
    alone = card(project(state(snap("C7", "application", C7_APPLICATION))), "C7")
    crowd = card(project(state(snap("C7", "application", C7_APPLICATION), snap("C8", "mastery", {
        s: ((d("C8_A"),), False) for s in STAGES}), snap("C2", NON_ETABLI))), "C7")
    assert alone == crowd


def test_no_timeline_why_or_evidence_field():
    names = _public_field_names()
    for word in ("history", "timeline", "milestone", "previous", "old_stage", "date", "since", "why", "evidence",
                 "example", "proof", "observation", "basis", "explanation", "trace"):
        assert not any(word in name for name in names), word


# --------------------------------------------------------------------------
# 10. Contrat statique
# --------------------------------------------------------------------------

def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def _accessed_names():
    """Attributs lus + chaînes littérales du code (getattr par nom compris)."""
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    return names


def test_imports_are_exactly_stdlib_6_1a_contracts_and_the_canonical_spec():
    by_module = {}
    for node in _tree().body:
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                by_module.setdefault(alias.name, set())
    assert by_module == {
        "re": set(),
        "uuid": set(),
        "dataclasses": {"dataclass"},
        "types": {"MappingProxyType"},
        "core.adaptation_state": {"COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot",
                                  "CapabilitySemanticRef", "CompetencyAdaptationSnapshot"},
        "core.pedagogy.taxonomy_v1": {"TaxonomySpecError", "load_taxonomy_v1"},
    }
    imported_names = set().union(*by_module.values())
    assert "load_adaptation_state" not in imported_names
    assert not any(name.startswith("_") for name in imported_names)


def test_no_6_1_to_6_3_interaction_dependency():
    for module in _imports():
        assert not (module.startswith("core.adaptation_") and module != "core.adaptation_state"), module
    tokens = _code_tokens(_source())
    for word in ("InteractionTaskProfile", "InteractionPostureBaseline", "InteractionSupportPlan",
                 "InteractionPedagogicalMovement", "InteractionCompetencyFocus", "AssumptionBaseline",
                 "PedagogicalResponseContext"):
        assert word not in tokens, word


def test_no_db_sqlalchemy_model_service_or_session():
    imported = _imports()
    assert not {"sqlalchemy", "core.models", "core.db", "core.taxonomy_service", "core.observation_service",
                "core.inference_service", "core.longitudinal_service", "alembic", "psycopg2"} & imported
    names = _accessed_names()
    for forbidden in ("Session", "session", "db", "get_db", "execute", "query", "commit", "flush", "add_all",
                      "rollback", "no_autoflush", "select", "merge", "delete", "refresh", "SessionLocal"):
        assert forbidden not in names, forbidden
    # Les seuls .add sont ceux des sets locaux de validation (jamais une session).
    adds = sorted(n.func.value.id for n in ast.walk(_tree()) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "add")
    assert adds == ["codes", "definitions"]


def test_no_llm_network_clock_randomness_nor_environment():
    imported = _imports()
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "random", "secrets", "datetime",
                "time", "os", "sys", "pathlib", "json", "logging"} & imported
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "environ", "getenv", "now(",
                 "uuid4", "random"):
        assert word not in tokens, word
    assert not {"llm", "model", "provider", "now", "today"} & set(tokens.split())
    assert "uuid4" not in {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}


def test_no_write_persistence_or_support_trace():
    tokens = _code_tokens(_source()).lower()
    for word in ("actual_support_trace", "support_trace", "cognitive_event", "save", "persist", "insert",
                 "update", "write", "open(", "cache"):
        assert word not in tokens, word


def test_step5_internals_beyond_stage_claim_and_catalogue_are_never_read():
    names = _accessed_names()
    for forbidden in ("user_id", "tensions", "tension_state", "fragilized_stage", "revision_status",
                      "validation_needs", "unresolved_revision_context", "reason_codes", "intent", "target_stage",
                      "confidence_profile", "mastery_assessment", "basis_mode", "state_generation",
                      "active_inference_run_id", "longitudinal_assessment_run_id", "taxonomy_release_id",
                      "membership_id", "mapping_guidance", "central_question"):
        assert forbidden not in names, forbidden


def test_no_fallback_to_another_claim_in_the_code():
    """La claim courante est désignée par son stade ; aucun parcours de
    claims inférieures / supérieures, aucun « plus haut établi »."""
    tokens = _code_tokens(_source()).lower()
    for word in ("highest", "lower", "higher", "fallback", "previous", "best", "max", "min", "reversed"):
        assert word not in tokens.split(), word
    for word in ("highest", "fallback", "borrow"):
        assert word not in tokens, word


def test_no_score_threshold_or_aggregation_in_the_code():
    tokens = _code_tokens(_source()).lower()
    for word in ("score", "threshold", "probab", "weight", "average", "percent", "xp", "ranking", "recommend",
                 "next_competency", "less_observed", "missing_capabilit", "weak"):
        assert word not in tokens, word
    parts = {part for token in tokens.split() for part in token.split("_")}
    assert not {"mean", "ratio", "level", "sum", "count", "total"} & parts
    assert not {"statistics", "math", "fractions", "decimal"} & _imports()


def test_competency_labels_are_never_duplicated_in_the_module():
    tokens = _code_tokens(_source())
    source = _source()
    for label in canonical_labels().values():
        assert label not in tokens, label
        assert label not in source, label


def test_taxonomy_v1_is_unchanged_and_fingerprint_is_frozen():
    spec, fingerprint = v1.load_taxonomy_v1()
    assert fingerprint == GOLDEN_V1_FINGERPRINT == v1.EXPECTED_V1_FINGERPRINT
    project(state(snap("C7", "application", C7_APPLICATION)))
    assert v1.load_taxonomy_v1() == (spec, fingerprint)  # aucune mutation de la SPEC du module


def test_not_wired_to_api_runtime_or_any_other_module():
    # Étape 6.4B1 : il ne recopie aucune règle et exige l'égalité avec
    # project_current_progress(state) (voir tests/test_progress_evidence.py).
    # Étape 6.4B2 : contrats publics de la carte seulement (vocabulaires,
    # versions, dataclasses), jamais project_current_progress. Étape 6.4B3 :
    # même règle pour la frontière de rendu et son renderer. Aucun n'est
    # branché au runtime.
    step6_consumers = {
        "core/progress_evidence.py": {
            "COVERAGE_COMPETENCY_ONLY", "COVERAGE_LOCALIZED", "COVERAGE_MIXED", "CompetencyCurrentProgress",
            "CurrentProgressProjection", "ProgressProjectionError", "project_current_progress"},
        "core/progress_evidence_selection.py": {
            "CLAIM_STAGE_ORDER", "COVERAGE_COMPETENCY_ONLY", "COVERAGE_LOCALIZED", "COVERAGE_MIXED",
            "COVERAGE_MODES", "COVERAGE_NONE", "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
            "NO_STATE_LABEL", "NON_ETABLI", "VISIBLE_STAGE_LABELS", "CompetencyCurrentProgress",
            "VisibleCapabilityProjection"},
        "core/progress_evidence_selector.py": {"CompetencyCurrentProgress"},
        "core/progress_evidence_rendering.py": {
            "CLAIM_STAGE_ORDER", "COVERAGE_COMPETENCY_ONLY", "COVERAGE_LOCALIZED", "COVERAGE_MIXED",
            "COVERAGE_MODES", "COVERAGE_NONE", "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
            "NO_STATE_LABEL", "NON_ETABLI", "VISIBLE_STAGE_LABELS", "CompetencyCurrentProgress",
            "VisibleCapabilityProjection"},
        "core/progress_evidence_renderer.py": {"CompetencyCurrentProgress"},
        # Étape 6.4C1 : même règle que 6-4B1 (égalité exigée avec
        # project_current_progress(state), aucune règle recopiée) ; 6-4C3 :
        # libellés visibles des stades et leur version de policy (voir
        # tests/test_progress_history.py et
        # tests/test_progress_history_projection.py). Aucun branchement runtime.
        "core/progress_history.py": {
            "CompetencyCurrentProgress", "CurrentProgressProjection", "ProgressProjectionError",
            "project_current_progress"},
        "core/progress_history_projection.py": {"CURRENT_PROGRESS_POLICY_VERSION", "VISIBLE_STAGE_LABELS"},
        # Étape 6.4D : les limitations exigent l'égalité avec
        # project_current_progress(state) (aucune règle recopiée) ; la vue
        # générale et le détail réutilisent les cartes telles quelles (voir
        # tests/test_progress_limitations.py, tests/test_progression_view.py
        # et tests/test_competency_progress_detail.py). Aucun branchement runtime.
        "core/progress_limitations.py": {
            "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION", "NON_ETABLI",
            "CompetencyCurrentProgress", "CurrentProgressProjection", "ProgressProjectionError",
            "project_current_progress"},
        "core/progression_view.py": {
            "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION", "CompetencyCurrentProgress",
            "CurrentProgressProjection"},
        "core/competency_progress_detail.py": {
            "CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION", "CompetencyCurrentProgress"},
    }
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "progress_projection" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.progress_projection", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_projection.py" and "progress_projection" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    assert sorted(users) == sorted(step6_consumers)
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("project_current_progress", "CurrentProgressProjection", "CompetencyCurrentProgress"):
        assert name not in api, name
    tokens = _code_tokens(_source())
    for word in ("web_chat", "fastapi", "route", "request", "endpoint"):
        assert word not in tokens, word


def test_no_migration_no_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0014_evaluation_run_leases.py" and len(versions) == 14
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "Progress" not in models and "progress_projection" not in models
    tokens = _code_tokens(_source())
    for word in ("__tablename__", "Column", "Base", "mapped_column", "relationship"):
        assert word not in tokens.split(), word


# --------------------------------------------------------------------------
# 11. Intégration PostgreSQL : T3 / T5 / T6 réels -> 6-1A -> 6-4A
# --------------------------------------------------------------------------

def _project_without_any_statement(engine, snapshot):  # noqa: F811
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    before = _dump(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        projection = project_current_progress(state=snapshot)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements == []  # aucune lecture, aucune écriture pendant la projection
    assert _dump(engine) == before
    return projection


def _load(Sessions):  # noqa: N803
    with Sessions() as session:
        snapshot = load_adaptation_state(session, user_id="u")
        assert not session.new and not session.dirty and not session.deleted
    return snapshot


def test_pg_empty_user_state_projects_twelve_cards_without_state(Sessions, engine):  # noqa: F811
    from core import models
    with Sessions() as session:
        if session.get(models.User, "u") is None:
            session.add(models.User(id="u"))
            session.commit()
    snapshot = _load(Sessions)
    assert snapshot.competencies == ()
    projection = _project_without_any_statement(engine, snapshot)
    assert [(c.competency_code, c.state_present, c.stage_label) for c in projection.competencies] == [
        (code, False, NO_STATE_LABEL) for code in COMPETENCY_ORDER]


def test_pg_real_upstream_chain_to_localized_projection(Sessions, engine):  # noqa: F811
    """SPEC V1 active -> T3 (Application C7_A / C7_D, Comprehension C7_A /
    C7_B) -> T5 -> T6 -> 6-1A -> 6-4A : stade = current_stage persisté,
    capacités = périmètre exact de la claim courante, onze cartes sans état."""
    from tests.test_longitudinal_service import sup
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(p.app("C7_A", "C7_D", evidence_strength="strong"))
    p.t3(sup(p.m["C7_A"], p.m["C7_B"], evidence_strength="strong"))
    _, decision = p.infer()

    snapshot = _load(Sessions)
    (c7,) = snapshot.competencies
    projection = _project_without_any_statement(engine, snapshot)
    result = card(projection, "C7")
    current = next(c for c in c7.claims if c.stage == c7.current_stage)
    assert result.stage_code == c7.current_stage == decision.current_stage == "application"
    assert result.stage_label == "Utilisé en situation"
    by_definition = {c.definition_id: c for c in c7.capabilities}
    assert [c.capability_code for c in result.represented_capabilities] == [
        by_definition[i].capability_code for i in current.represented_capability_definition_ids]
    assert [c.capability_code for c in result.represented_capabilities] == ["C7_A", "C7_D"]
    assert result.coverage_mode == "localized"
    assert all(c.semantic_revision == 1 for c in result.represented_capabilities)
    spec_labels = {c["capability_code"]: c["label"] for c in v1.load_taxonomy_v1()[0]["capabilities"]}
    assert [c.label for c in result.represented_capabilities] == [spec_labels["C7_A"], spec_labels["C7_D"]]
    assert [c.state_present for c in projection.competencies] == [code == "C7" for code in COMPETENCY_ORDER]
    assert project_current_progress(state=snapshot) == projection
    assert not UUID_PATTERN.search(repr(projection))


def test_pg_real_competency_only(Sessions, engine):  # noqa: F811
    from tests.test_longitudinal_service import only
    p = _pipeline(Sessions, _v1(Sessions))
    p.t3(only(p.app(evidence_strength="strong")))
    p.infer()
    snapshot = _load(Sessions)
    (c7,) = snapshot.competencies
    result = card(_project_without_any_statement(engine, snapshot), "C7")
    assert result.stage_code == c7.current_stage
    assert (result.coverage_mode, result.represented_capabilities) == ("competency_only", ())
