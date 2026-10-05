"""Tests de l'Étape 6.4D (préalable) : limitations actuelles
(core/progress_limitations.py).

1. Contrats : versions, vocabulaire less_observed_status, dataclasses
   immuables, invariants de statut, API publique, hiérarchie d'erreurs.
2. Carte canonique 6-4A : projection recalculée, égalité exigée.
3. not_applicable : aucune state, non_etabli, claim courante not_established.
4. confidence-profile-v2 : fait EXACT unobserved_capabilities_present, absent
   => available + (), identité par definition_id, ordre du catalogue, jamais
   les capacités d'un autre fait, sémantique « aucune observation positive
   située » (une capacité contradictoire n'est pas exclue).
5. confidence-profile-v1 : seule fact_codes est lue ; présent => unavailable
   + (), jamais l'agrégat ; absent => available + ().
6. Version inconnue / profils corrompus : fail closed.
7. Tensions : seulement celles du stade courant, gabarits fixes, aucune
   capacité inventée, revision_status jamais exposé, ordre canonique.
8. Inertie : validation_needs, unresolved_revision_context, mastery_assessment,
   capacités représentées (aucune soustraction).
9. Exclusions, pureté et contrat statique (aucune base, aucun modèle, aucune
   écriture, aucune trace de support, aucun branchement runtime).
10. Intégration PostgreSQL (ORYX_TEST_DATABASE_URL, sinon SKIPPÉS) : chaîne
    réelle T3 -> T5 -> T6 -> 6-1A -> 6-4A -> limitations, v2 courant et v1
    historique.
"""
import ast
import dataclasses
import inspect
import re
import uuid
from types import MappingProxyType

import pytest

from core import adaptation_state as ad
from core import progress_limitations as lim
from core.adaptation_state import AdaptationTension, AdaptationValidationNeed
from core.inference_state_policies import UNOBSERVED_CAPABILITIES_PRESENT
from core.progress_limitations import (
    COMPETENCY_TENSION_TEXT,
    CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION,
    CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION,
    LESS_OBSERVED_AVAILABLE,
    LESS_OBSERVED_NOT_APPLICABLE,
    LESS_OBSERVED_STATUSES,
    LESS_OBSERVED_UNAVAILABLE,
    CurrentProgressLimitations,
    CurrentProgressLimitationsError,
    IncompatibleCurrentProgressLimitationsInputs,
    InvalidCurrentProgressLimitationsArgument,
    InvalidCurrentProgressLimitationsState,
    LessObservedCapability,
    UnsupportedCurrentProgressLimitationsVersion,
    VisibleCurrentTension,
    project_current_progress_limitations,
)
from core.progress_projection import project_current_progress
from tests.test_adaptation_state import DIMENSIONS, V2_DEFAULT_FACTS, mastery_payload, need, thaw
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_progress_projection import (
    A,
    B,
    C,
    D,
    UUID_PATTERN,
    cap,
    card,
    claims,
    leaves,
    snap,
    state,
    uid,
)

MODULE_PATH = REPO_ROOT / "core" / "progress_limitations.py"
STAGES = ("discovery", "comprehension", "application", "mastery")
LABEL = {"A": "label C7_A", "B": "label C7_B", "C": "label C7_C", "D": "label C7_D"}


# --------------------------------------------------------------------------
# Constructeurs
# --------------------------------------------------------------------------

def frozen(payload):
    """Profil tel que 6-1A le copie (mappingproxy / tuples)."""
    return ad._frozen_json(payload, "confidence_profile")


def v2(*, unobserved=(D,), coverage=None):
    """confidence-profile-v2 : localized A/B/C, concentrated A/B/C,
    unobserved -> SES capacités (absent si ())."""
    if coverage is None:
        coverage = [("localized_representative_scope", (A, B, C)),
                    ("coverage_concentrated_on_claim_scope", (A, B, C))]
        if unobserved:
            coverage.append((UNOBSERVED_CAPABILITIES_PRESENT, unobserved))
    return frozen({"schema_version": "confidence-profile-v2", **{
        name: {"facts": [{"code": code, "capability_definition_ids": [str(i) for i in ids]}
                         for code, ids in (coverage if name == "coverage" else [(V2_DEFAULT_FACTS[name], ())])],
               "limitations": []} for name in DIMENSIONS}})


def v1(*aggregated, coverage_codes=("localized_representative_scope", UNOBSERVED_CAPABILITIES_PRESENT)):
    """confidence-profile-v1 HISTORIQUE : fact_codes + capacités AGRÉGÉES
    par dimension (aucune attribution par fait)."""
    return frozen({"schema_version": "confidence-profile-v1", **{
        name: {"fact_codes": list(coverage_codes if name == "coverage" else (V2_DEFAULT_FACTS[name],)),
               "capability_definition_ids": [str(i) for i in aggregated], "limitations": []}
        for name in DIMENSIONS}})


LADDER = {"discovery": ((A, B, C), False), "comprehension": ((A, B, C), False),
          "application": ((A, B, C), False)}


def c7(current="application", profile=None, *, tensions=(), ladder=None, needs=(), context=None,
       claim_tuple=None, capabilities=None):
    """Snapshot C7 : Application localisée A/B/C (ou ladder), profil de la
    claim courante = profile (v2 par défaut)."""
    ladder = LADDER if ladder is None else ladder
    overrides = {}
    if current in ladder:
        overrides[current] = {"confidence": v2() if profile is None else profile}
    return snap("C7", current, ladder, tensions=tensions, tension_state="open" if tensions else "none",
                needs=needs, context=context, claim_overrides=overrides, claim_tuple=claim_tuple,
                capabilities=capabilities)


def tension(stage, scope, *ids, status="unresolved"):
    return AdaptationTension(fragilized_stage=stage, scope_mode=scope, capability_definition_ids=tuple(ids),
                             revision_status=status)


def limits(*snapshots, code="C7", progress=None):
    st = state(*snapshots)
    return project_current_progress_limitations(
        state=st, progress=progress if progress is not None else project_current_progress(state=st),
        competency_code=code)


def less(result):
    return [(c.capability_code, c.semantic_revision, c.label) for c in result.less_observed_capabilities]


def texts(result):
    return [t.text for t in result.current_tensions]


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_are_exact():
    assert CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION == "current-progress-limitations-v1"
    assert CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION == "current-progress-limitations-policy-1"
    assert lim.SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION == "current-progress-projection-v1"
    assert lim.SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION == "current-progress-policy-1"


def test_less_observed_vocabulary_is_closed():
    assert (LESS_OBSERVED_AVAILABLE, LESS_OBSERVED_NOT_APPLICABLE, LESS_OBSERVED_UNAVAILABLE) == (
        "available", "not_applicable", "unavailable")
    assert LESS_OBSERVED_STATUSES == ("available", "not_applicable", "unavailable")


def test_the_canonical_step5_constant_is_imported_never_copied():
    assert lim.UNOBSERVED_CAPABILITIES_PRESENT is UNOBSERVED_CAPABILITIES_PRESENT
    assert '"unobserved_capabilities_present"' not in MODULE_PATH.read_text(encoding="utf-8")


def _fields(cls):
    return [(f.name, f.type) for f in dataclasses.fields(cls)]


def test_public_dataclasses_are_exact_and_frozen():
    assert _fields(LessObservedCapability) == [("capability_code", str), ("semantic_revision", int), ("label", str)]
    assert _fields(VisibleCurrentTension) == [("text", str)]
    assert _fields(CurrentProgressLimitations) == [
        ("schema_version", str), ("policy_version", str), ("competency_code", str),
        ("less_observed_status", str),
        ("less_observed_capabilities", tuple[LessObservedCapability, ...]),
        ("current_tensions", tuple[VisibleCurrentTension, ...])]
    result = limits(c7())
    for obj in (result, result.less_observed_capabilities[0]):
        with pytest.raises(dataclasses.FrozenInstanceError):
            obj.label = "x" if isinstance(obj, LessObservedCapability) else None
    for cls in (LessObservedCapability, VisibleCurrentTension, CurrentProgressLimitations):
        with pytest.raises(TypeError):
            cls("positional")


def test_less_observed_capability_has_the_visible_identity_of_6_4a():
    from core.progress_projection import VisibleCapabilityProjection
    assert _fields(LessObservedCapability) == _fields(VisibleCapabilityProjection)


def _build(status, capabilities=()):
    return CurrentProgressLimitations(
        schema_version=CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION,
        policy_version=CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION, competency_code="C7",
        less_observed_status=status, less_observed_capabilities=capabilities, current_tensions=())


ONE = (LessObservedCapability(capability_code="C7_D", semantic_revision=1, label="label C7_D"),)


@pytest.mark.parametrize("status", ["not_applicable", "unavailable"])
def test_status_invariant_non_empty_capabilities_are_impossible(status):
    assert _build(status).less_observed_capabilities == ()
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        _build(status, ONE)


@pytest.mark.parametrize("capabilities", [(), ONE])
def test_available_accepts_empty_and_non_empty(capabilities):
    assert _build("available", capabilities).less_observed_capabilities == capabilities


@pytest.mark.parametrize("status", ["unknown", "", None, "AVAILABLE"])
def test_status_outside_the_vocabulary_is_impossible(status):
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        _build(status)


def test_public_api_and_signature():
    public = {name for name, obj in inspect.getmembers(lim, inspect.isfunction)
              if obj.__module__ == lim.__name__ and not name.startswith("_")}
    assert public == {"project_current_progress_limitations"}
    signature = inspect.signature(project_current_progress_limitations)
    assert [(p.name, p.kind) for p in signature.parameters.values()] == [
        ("state", inspect.Parameter.KEYWORD_ONLY), ("progress", inspect.Parameter.KEYWORD_ONLY),
        ("competency_code", inspect.Parameter.KEYWORD_ONLY)]


def test_errors_are_a_dedicated_hierarchy():
    for error in (InvalidCurrentProgressLimitationsArgument, IncompatibleCurrentProgressLimitationsInputs,
                  UnsupportedCurrentProgressLimitationsVersion, InvalidCurrentProgressLimitationsState):
        assert issubclass(error, CurrentProgressLimitationsError)
    assert not issubclass(CurrentProgressLimitationsError, (ValueError, TypeError))


# --------------------------------------------------------------------------
# 2. Carte canonique 6-4A
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"state": None}, {"progress": "projection"}, {"competency_code": "C13"}, {"competency_code": "c7"},
    {"competency_code": 7}])
def test_arguments_are_validated(kwargs):
    st = state(c7())
    args = {"state": st, "progress": project_current_progress(state=st), "competency_code": "C7", **kwargs}
    with pytest.raises(InvalidCurrentProgressLimitationsArgument):
        project_current_progress_limitations(**args)


def test_projection_must_be_exactly_the_canonical_6_4a_projection():
    st = state(c7())
    progress = project_current_progress(state=st)
    forged_card = dataclasses.replace(card(progress, "C7"), stage_code="mastery")
    forged = dataclasses.replace(progress, competencies=tuple(
        forged_card if c.competency_code == "C7" else c for c in progress.competencies))
    with pytest.raises(IncompatibleCurrentProgressLimitationsInputs):
        project_current_progress_limitations(state=st, progress=forged, competency_code="C7")
    other = project_current_progress(state=state(c7(profile=v2(unobserved=(D,)), ladder={
        "discovery": ((A,), False), "comprehension": ((A,), False), "application": ((A,), False)})))
    with pytest.raises(IncompatibleCurrentProgressLimitationsInputs):
        project_current_progress_limitations(state=st, progress=other, competency_code="C7")
    duplicated = dataclasses.replace(progress, competencies=progress.competencies + (card(progress, "C7"),))
    with pytest.raises(IncompatibleCurrentProgressLimitationsInputs):
        project_current_progress_limitations(state=st, progress=duplicated, competency_code="C7")


def test_unsupported_6_4a_version_fails_closed(monkeypatch):
    st = state(c7())
    progress = project_current_progress(state=st)
    with pytest.raises(UnsupportedCurrentProgressLimitationsVersion):
        project_current_progress_limitations(state=st, progress=dataclasses.replace(
            progress, schema_version="current-progress-projection-v2"), competency_code="C7")
    monkeypatch.setattr(lim, "CURRENT_PROGRESS_POLICY_VERSION", "current-progress-policy-2")
    with pytest.raises(UnsupportedCurrentProgressLimitationsVersion):
        project_current_progress_limitations(state=st, progress=progress, competency_code="C7")


def test_snapshot_not_projectable_by_6_4a_fails_closed():
    broken = c7(capabilities=(cap("C7_A"), cap("C7_A", uid("other"))))
    st = state(broken)
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        project_current_progress_limitations(state=st, progress=project_current_progress(state=state()),
                                             competency_code="C7")


# --------------------------------------------------------------------------
# 3. not_applicable
# --------------------------------------------------------------------------

def test_no_state_is_not_applicable_without_anything():
    result = limits(c7(), code="C5")
    assert (result.competency_code, result.less_observed_status, result.less_observed_capabilities,
            result.current_tensions) == ("C5", "not_applicable", (), ())
    assert limits(code="C1").less_observed_status == "not_applicable"


def test_non_etabli_is_not_applicable_and_shows_no_tension():
    """Le libellé 6-4A « Encore peu observé par Oryx » suffit : aucun dossier
    interne n'est transformé en liste de problèmes."""
    result = limits(c7("non_etabli", ladder={}))
    assert (result.less_observed_status, result.less_observed_capabilities, result.current_tensions) == (
        "not_applicable", (), ())


def test_current_claim_not_established_is_not_applicable_but_keeps_its_current_tension():
    """État Step 5 réel : maintien du stade précédent sous tension ouverte
    (claim courante not_established, accepté par 6-1A / 6-4A / 6-4B1)."""
    ladder = {"discovery": ((A, B), False), "comprehension": ((A, B), False)}
    snapshot = c7("application", ladder=ladder, tensions=(tension("application", "localized", A),),
                  context=MappingProxyType({"schema_version": "revision-context-v1"}))
    result = limits(snapshot)
    assert (result.less_observed_status, result.less_observed_capabilities) == ("not_applicable", ())
    assert texts(result) == [lim.LOCALIZED_TENSION_SINGLE_TEMPLATE.format(labels="label C7_A")]


def test_not_established_current_claim_with_a_profile_fails_closed():
    forged = claims({"discovery": ((A,), False), "comprehension": ((A,), False)})
    forged = tuple(dataclasses.replace(c, confidence_profile=v2()) if c.stage == "application" else c
                   for c in forged)
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7("application", ladder={}, claim_tuple=forged))


# --------------------------------------------------------------------------
# 4. confidence-profile-v2
# --------------------------------------------------------------------------

def test_v2_exact_fact_gives_only_its_own_capabilities():
    """localized_representative_scope -> A/B/C, coverage_concentrated -> A/B/C,
    unobserved -> D : seule D est projetée."""
    result = limits(c7(profile=v2(unobserved=(D,))))
    assert result.less_observed_status == "available"
    assert less(result) == [("C7_D", 1, "label C7_D")]


def test_v2_fact_absent_is_available_and_empty():
    result = limits(c7(profile=v2(unobserved=())))
    assert (result.less_observed_status, result.less_observed_capabilities) == ("available", ())


def test_v2_never_uses_the_capabilities_of_another_fact():
    for coverage in (
            [("localized_representative_scope", (A, B, C)), ("additional_positive_scope_present", (A, B, C)),
             (UNOBSERVED_CAPABILITIES_PRESENT, (D,))],
            [("localized_representative_scope", (A, B, C)), ("coverage_concentrated_on_claim_scope", (A, B, C))]):
        result = limits(c7(profile=v2(coverage=coverage)))
        assert not {"C7_A", "C7_B", "C7_C"} & {c.capability_code for c in result.less_observed_capabilities}


def test_v2_never_subtracts_represented_capabilities_from_the_catalogue():
    """A/B/C représentées, D hors périmètre : le fait seul décide. S'il ne
    signale que C, seule C est projetée ; s'il est absent, rien (jamais
    « catalogue - représentées » = D)."""
    assert less(limits(c7(profile=v2(unobserved=(C,))))) == [("C7_C", 1, "label C7_C")]
    assert limits(c7(profile=v2(unobserved=()))).less_observed_capabilities == ()


def test_v2_capabilities_follow_the_snapshot_catalogue_order():
    result = limits(c7(profile=v2(coverage=[("localized_representative_scope", (A,)),
                                            ("coverage_concentrated_on_claim_scope", (A,)),
                                            (UNOBSERVED_CAPABILITIES_PRESENT, (B, C, D))]),
                       ladder={"discovery": ((A,), False), "comprehension": ((A,), False),
                               "application": ((A,), False)}))
    assert [c.capability_code for c in result.less_observed_capabilities] == ["C7_B", "C7_C", "C7_D"]


def test_v2_non_canonical_json_order_is_rejected_never_silently_reordered():
    profile = thaw(v2(unobserved=(B, D)))
    profile["coverage"]["facts"][2]["capability_definition_ids"].reverse()
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=frozen(profile)))


def test_v2_resolves_by_exact_definition_id_never_by_code():
    """Même capability_code, autre definition_id (autre révision) : jamais
    remappé ; definition_id inconnu du catalogue => fail closed."""
    revised = uid("d-C7_D-r2")
    capabilities = (cap("C7_A"), cap("C7_B"), cap("C7_C"), cap("C7_D", revised, revision=2, label="D révisée"))
    result = limits(c7(profile=v2(unobserved=(revised,)), capabilities=capabilities))
    assert less(result) == [("C7_D", 2, "D révisée")]
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=v2(unobserved=(D,)), capabilities=capabilities))


def test_v2_unknown_definition_fails_closed():
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=v2(unobserved=(uid("d-C9_A"),))))


def test_v2_fact_present_without_capability_fails_closed():
    profile = thaw(v2(unobserved=(D,)))
    profile["coverage"]["facts"][2]["capability_definition_ids"] = []
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=frozen(profile)))


V2_TAMPERS = {
    "fait en double": lambda p: p["coverage"]["facts"].append(dict(p["coverage"]["facts"][2])),
    "fact_codes v1 sous étiquette v2": lambda p: p["coverage"].update(fact_codes=[UNOBSERVED_CAPABILITIES_PRESENT]),
    "id non canonique": lambda p: p["coverage"]["facts"][2].update(capability_definition_ids=[str(D).upper()]),
    "id non str": lambda p: p["coverage"]["facts"][2].update(capability_definition_ids=[7]),
    "code hors dimension": lambda p: p["coverage"]["facts"][2].update(code="single_episode_only"),
    "dimension manquante": lambda p: p.pop("consistency"),
    "facts absent": lambda p: p["coverage"].pop("facts"),
}


@pytest.mark.parametrize("label", list(V2_TAMPERS))
def test_v2_corrupted_profile_fails_closed(label):
    profile = thaw(v2(unobserved=(D,)))
    V2_TAMPERS[label](profile)
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=frozen(profile)))


def test_contradicted_capability_without_positive_observation_is_not_excluded():
    """Sémantique Step 5 : aucune observation POSITIVE située. D porte une
    contradiction (tension localisée D au stade courant) : elle reste
    signalée, jamais exclue au motif qu'elle « a déjà été observée »."""
    snapshot = c7(profile=v2(unobserved=(D,)), tensions=(tension("application", "localized", D),))
    result = limits(snapshot)
    assert less(result) == [("C7_D", 1, "label C7_D")]
    assert texts(result) == [lim.LOCALIZED_TENSION_SINGLE_TEMPLATE.format(labels="label C7_D")]


def test_implied_current_claim_reads_its_own_profile():
    """Comprehension courante implied (Application fragilisée) : son propre
    profil, jamais celui de la claim source."""
    ladder = {"discovery": ((A, B, C), False), "comprehension": ((A, B, C), False),
              "application": ((A, B, C), False)}
    overrides = {"comprehension": {"basis_mode": "implied_by_higher_claim", "confidence": v2(unobserved=(D,))},
                 "application": {"confidence": v2(unobserved=())}}
    snapshot = snap("C7", "comprehension", ladder, claim_overrides=overrides)
    assert less(limits(snapshot)) == [("C7_D", 1, "label C7_D")]


# --------------------------------------------------------------------------
# 5. confidence-profile-v1 (historique)
# --------------------------------------------------------------------------

def test_v1_fact_absent_is_available_and_empty():
    result = limits(c7(profile=v1(A, B, C, coverage_codes=("localized_representative_scope",
                                                            "coverage_concentrated_on_claim_scope"))))
    assert (result.less_observed_status, result.less_observed_capabilities) == ("available", ())


def test_v1_fact_present_is_unavailable_and_projects_nothing():
    """Agrégat A/B/C/D : l'attribution au fait est perdue, rien n'est
    projeté (ni A/B/C/D, ni une soustraction)."""
    result = limits(c7(profile=v1(A, B, C, D)))
    assert (result.less_observed_status, result.less_observed_capabilities) == ("unavailable", ())
    assert not UUID_PATTERN.search(repr(result))


@pytest.mark.parametrize("aggregated", [(), (A,), (D,), (A, B, C, D), (B, D)])
def test_v1_aggregate_is_never_used_to_reconstruct_an_attribution(aggregated):
    result = limits(c7(profile=v1(*aggregated)))
    assert (result.less_observed_status, result.less_observed_capabilities) == ("unavailable", ())


def test_v1_never_reads_a_facts_key():
    profile = thaw(v1(A, B, C, D))
    profile["coverage"]["facts"] = [{"code": UNOBSERVED_CAPABILITIES_PRESENT, "capability_definition_ids": [str(D)]}]
    assert limits(c7(profile=frozen(profile))).less_observed_capabilities == ()


@pytest.mark.parametrize("fact_codes", [None, "unobserved_capabilities_present", [UNOBSERVED_CAPABILITIES_PRESENT] * 2,
                                        ["unknown_code"], ["single_episode_only"], [7]])
def test_v1_corrupted_fact_codes_fail_closed(fact_codes):
    profile = thaw(v1(A))
    if fact_codes is None:
        profile["coverage"].pop("fact_codes")
    else:
        profile["coverage"]["fact_codes"] = fact_codes
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=frozen(profile)))


# --------------------------------------------------------------------------
# 6. Version inconnue
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", ["confidence-profile-v3", "confidence-profile-v0", None, 2,
                                     "CONFIDENCE-PROFILE-V2"])
def test_unknown_confidence_profile_version_fails_closed(version):
    profile = thaw(v2())
    profile["schema_version"] = version
    with pytest.raises(UnsupportedCurrentProgressLimitationsVersion):
        limits(c7(profile=frozen(profile)))


def test_established_current_claim_without_structured_profile_fails_closed():
    forged = tuple(dataclasses.replace(c, confidence_profile="low") if c.stage == "application" else c
                   for c in c7().claims)
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(claim_tuple=forged))


# --------------------------------------------------------------------------
# 7. Tensions
# --------------------------------------------------------------------------

def test_localized_tension_on_the_current_stage_names_the_exact_capability():
    result = limits(c7(tensions=(tension("application", "localized", B),)))
    assert texts(result) == ["Des éléments observés restent contradictoires sur « label C7_B »."
                             " Oryx garde ce point comme une limite ouverte de son estimation."]
    assert not UUID_PATTERN.search(repr(result)) and str(B) not in repr(result)


def test_localized_tension_on_several_capabilities_uses_the_catalogue_order():
    result = limits(c7(tensions=(tension("application", "localized", B, D),)))
    assert texts(result) == ["Des éléments observés restent contradictoires sur « label C7_B », « label C7_D »."
                             " Oryx garde ce périmètre comme une limite ouverte de son estimation."]


def test_localized_tension_resolves_by_definition_id_never_by_code():
    revised = uid("d-C7_B-r2")
    capabilities = (cap("C7_A"), cap("C7_B", revised, revision=2, label="B révisée"), cap("C7_C"), cap("C7_D"))
    ladder = {"discovery": ((A, C), False), "comprehension": ((A, C), False), "application": ((A, C), False)}
    profile = v2(coverage=[("localized_representative_scope", (A, C)),
                           ("coverage_concentrated_on_claim_scope", (A, C))])
    result = limits(c7(profile=profile, ladder=ladder, capabilities=capabilities,
                       tensions=(tension("application", "localized", revised),)))
    assert texts(result) == [lim.LOCALIZED_TENSION_SINGLE_TEMPLATE.format(labels="B révisée")]
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(profile=profile, ladder=ladder, capabilities=capabilities,
                  tensions=(tension("application", "localized", B),)))


@pytest.mark.parametrize("scope", ["whole_competency", "competency_only"])
def test_unlocalized_tension_never_invents_a_capability(scope):
    result = limits(c7(tensions=(tension("application", scope),)))
    assert texts(result) == [COMPETENCY_TENSION_TEXT]
    assert COMPETENCY_TENSION_TEXT == ("Des éléments observés restent contradictoires à l’échelle de cette"
                                       " compétence. Oryx garde cette limite ouverte dans son estimation.")
    assert "C7_" not in COMPETENCY_TENSION_TEXT and "label" not in COMPETENCY_TENSION_TEXT


@pytest.mark.parametrize("fragilized", ["mastery", "comprehension", "discovery"])
def test_tension_on_another_stage_is_never_visible(fragilized):
    """Stade supérieur : jamais une checklist vers le stade suivant ; stade
    inférieur / ancien : hors du périmètre affiché."""
    result = limits(c7(tensions=(tension(fragilized, "localized", A),)))
    assert result.current_tensions == ()
    assert result.less_observed_status == "available"


def test_only_current_stage_tensions_are_kept_in_their_canonical_order():
    tensions = (tension("comprehension", "whole_competency"), tension("application", "whole_competency"),
                tension("application", "localized", A), tension("application", "localized", C, D),
                tension("mastery", "localized", D))
    result = limits(c7(tensions=tensions))
    assert texts(result) == [
        COMPETENCY_TENSION_TEXT,
        lim.LOCALIZED_TENSION_SINGLE_TEMPLATE.format(labels="label C7_A"),
        lim.LOCALIZED_TENSION_MULTIPLE_TEMPLATE.format(labels="« label C7_C », « label C7_D »")]


def test_revision_status_is_never_exposed():
    unresolved = limits(c7(tensions=(tension("application", "localized", A, status="unresolved"),)))
    revalidation = limits(c7(tensions=(tension("application", "localized", A, status="revalidation_needed"),)))
    assert unresolved.current_tensions == revalidation.current_tensions
    assert [f.name for f in dataclasses.fields(VisibleCurrentTension)] == ["text"]
    text = texts(revalidation)[0].lower()
    for word in ("revalid", "refaire", "travaille", "prochaine", "étape", "dois", "à valider"):
        assert word not in text, word


def test_one_text_per_structured_tension_never_merged():
    tensions = (tension("application", "localized", A, status="revalidation_needed"),
                tension("application", "localized", A, status="unresolved"))
    assert len(limits(c7(tensions=tensions)).current_tensions) == 2


TENSION_TAMPERS = {
    "stade inconnu": tension("expert", "whole_competency"),
    "scope inconnu": tension("application", "global"),
    "statut inconnu": tension("application", "whole_competency", status="critical"),
    "localized sans capacité": tension("application", "localized"),
    "whole avec capacité": tension("application", "whole_competency", A),
    "competency_only avec capacité": tension("application", "competency_only", A),
    "capacité en double": tension("application", "localized", A, A),
    "capacité inconnue": tension("application", "localized", uid("d-C9_A")),
    "id non uuid": AdaptationTension(fragilized_stage="application", scope_mode="localized",
                                     capability_definition_ids=(str(A),), revision_status="unresolved"),
}


@pytest.mark.parametrize("label", list(TENSION_TAMPERS))
def test_corrupted_tensions_fail_closed_even_on_another_stage(label):
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(c7(tensions=(TENSION_TAMPERS[label],)))


def test_tension_state_must_agree_with_tensions():
    snapshot = dataclasses.replace(c7(tensions=(tension("application", "whole_competency"),)), tension_state="none")
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(snapshot)
    with pytest.raises(InvalidCurrentProgressLimitationsState):
        limits(dataclasses.replace(c7(), tension_state="open"))


# --------------------------------------------------------------------------
# 8. Inertie : seules les sources autorisées décident
# --------------------------------------------------------------------------

def _rich(**changes):
    base = dict(profile=v2(unobserved=(D,)), tensions=(tension("application", "localized", D),))
    base.update(changes)
    return c7(**base)


def test_validation_needs_never_change_the_projection():
    needs = (AdaptationValidationNeed(intent="revalidation", target_stage="application", scope_mode="localized",
                                      capability_definition_ids=(D,), reason_codes=("unresolved_revision_motif",)),
             AdaptationValidationNeed(intent="confirmation", target_stage="mastery", scope_mode="whole_competency",
                                      capability_definition_ids=(), reason_codes=("x",)))
    assert limits(_rich(needs=needs)) == limits(_rich())
    assert need  # vocabulaire 6-1A : besoins latents, jamais une todo-list


def test_revision_context_never_changes_the_projection():
    context = MappingProxyType({"schema_version": "revision-context-v1", "motifs": ({"stage": "mastery"},)})
    assert limits(_rich(context=context)) == limits(_rich())


def test_mastery_assessment_limitations_never_become_a_checklist():
    ladder = {stage: ((A, B, C), False) for stage in STAGES}
    a = mastery_payload(A, B, C)
    b = mastery_payload(A, B, C, status="not_supported")
    b["limitations"] = ["independence_not_established", "variety_not_established"]
    one = snap("C7", "mastery", ladder, claim_overrides={
        "mastery": {"assessment": frozen(a), "confidence": v2(unobserved=(D,))}})
    two = snap("C7", "mastery", ladder, claim_overrides={
        "mastery": {"assessment": frozen(b), "confidence": v2(unobserved=(D,))}})
    assert limits(one) == limits(two)
    assert less(limits(one)) == [("C7_D", 1, "label C7_D")]


def test_other_claims_profiles_never_change_the_projection():
    overrides = {"application": {"confidence": v2(unobserved=(D,))},
                 "comprehension": {"confidence": v2(unobserved=(B, C, D))},
                 "discovery": {"confidence": v1(A, B, C, D)}}
    other = snap("C7", "application", LADDER, claim_overrides=overrides)
    assert limits(other) == limits(c7(profile=v2(unobserved=(D,))))


def test_positive_basis_limitations_are_never_exposed():
    profile = thaw(v2(unobserved=(D,)))
    for name in DIMENSIONS:
        profile[name]["limitations"] = ["dependency_limited_demonstration", "independence_not_established",
                                        "localized_scope_not_representative"]
    result = limits(c7(profile=frozen(profile)))
    assert result == limits(c7(profile=v2(unobserved=(D,))))
    for word in ("dependency", "independence", "representative"):
        assert word not in repr(result)


# --------------------------------------------------------------------------
# 9. Exclusions, pureté, contrat statique
# --------------------------------------------------------------------------

def test_no_uuid_no_identifier_no_internal_vocabulary_is_exposed():
    snapshot = _rich()
    result = limits(snapshot)
    rendered = repr(result)
    assert not UUID_PATTERN.search(rendered)
    for leaf in leaves(result):
        assert not isinstance(leaf, uuid.UUID)
    for word in ("fragilized", "scope_mode", "revision_status", "unresolved", "localized", "membership",
                 "definition_id", "release", "run"):
        assert word not in rendered, word


def test_no_weakness_score_severity_or_action_field():
    names = {f.name for cls in (LessObservedCapability, VisibleCurrentTension, CurrentProgressLimitations)
             for f in dataclasses.fields(cls)}
    for word in ("weak", "gap", "deficien", "missing", "priority", "needs", "improve", "severity", "warning",
                 "critical", "alert", "importance", "action", "next", "practice", "recommend", "score", "percent",
                 "count", "level", "rank"):
        assert not any(word in name for name in names), word


def test_same_input_same_output_and_inputs_untouched():
    snapshot = _rich()
    st = state(snapshot)
    progress = project_current_progress(state=st)
    before = (repr(st), repr(progress))
    first = project_current_progress_limitations(state=st, progress=progress, competency_code="C7")
    second = project_current_progress_limitations(state=st, progress=progress, competency_code="C7")
    assert first == second and (repr(st), repr(progress)) == before


def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    by_module = {}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                by_module.setdefault(alias.name, set())
    return by_module


def test_imports_are_public_contracts_and_vocabularies_only():
    assert _imports() == {
        "uuid": set(),
        "collections.abc": {"Mapping"},
        "dataclasses": {"dataclass"},
        "core.adaptation_state": {"COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot",
                                  "AdaptationTension", "CompetencyAdaptationSnapshot"},
        "core.inference_final_policies": {"InvalidConfidenceProfilePayload", "check_confidence_profile_v2"},
        "core.inference_service": {"CLAIM_STAGES", "ESTABLISHED", "LOCALIZED", "NOT_ESTABLISHED",
                                   "REVISION_STATUSES", "TENSION_OPEN", "TENSION_SCOPE_MODES", "TENSION_STATES"},
        "core.inference_state_policies": {"CONFIDENCE_PROFILE_SCHEMA_V1", "CONFIDENCE_PROFILE_SCHEMA_V2", "COVERAGE",
                                          "DIMENSION_FACT_CODES", "UNOBSERVED_CAPABILITIES_PRESENT"},
        "core.progress_projection": {"CURRENT_PROGRESS_POLICY_VERSION", "CURRENT_PROGRESS_SCHEMA_VERSION",
                                     "NON_ETABLI", "CompetencyCurrentProgress", "CurrentProgressProjection",
                                     "ProgressProjectionError", "project_current_progress"},
    }
    assert all(name.isupper() for name in _imports()["core.inference_service"])


def test_never_reads_forbidden_sources():
    """Sources interdites : validation_needs, unresolved_revision_context,
    mastery_assessment, périmètre représenté (aucune soustraction),
    l'agrégat v1, les observations, T5, T6."""
    attributes = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("validation_needs", "unresolved_revision_context", "mastery_assessment",
                      "represented_capability_definition_ids", "represented_capabilities",
                      "competency_only_basis", "basis_mode", "coverage_mode"):
        assert forbidden not in attributes, forbidden
    strings = {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "capability_definition_ids" in strings  # lu SEULEMENT sur le fait v2 exact
    v1_reader = ast.get_source_segment(_source(), next(
        n for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef) and n.name == "_less_observed_v1"))
    assert "capability_definition_ids" not in v1_reader.split('"""')[-1]
    assert '"facts"' not in v1_reader
    names = set(_code_tokens(_source()).split())
    for forbidden in ("difference", "symmetric_difference", "get_inference_tensions", "infer_competency",
                      "evaluate_positive_basis", "evaluate_inference_state", "load_adaptation_state",
                      "get_validated_user_competency_state"):
        assert forbidden not in names, forbidden


def test_no_database_no_session_no_write():
    names = set(_code_tokens(_source()).split()) | {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    for forbidden in ("db", "Session", "session", "execute", "select", "query", "add", "flush", "commit",
                      "rollback", "insert", "update", "delete", "no_autoflush"):
        assert forbidden not in names, forbidden
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "alembic", "psycopg2"} & set(_imports())


def test_no_llm_clock_randomness_or_support_trace():
    assert not {"anthropic", "openai", "requests", "httpx", "random", "secrets", "datetime", "time", "os",
                "json"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "backend", "renderer", "proposal",
                 "support_trace", "supporttrace", "environ", "uuid4", "uuid1", "complete("):
        assert word not in tokens, word
    assert not {"llm", "model", "now", "today"} & set(tokens.split())


def test_no_score_counter_percent_or_bound():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "percent", "ratio", "rank", "points", "xp", "streak", "severity", "priority",
                 "weak", "gap", "missing", "recommend", "next", "average", "overall"):
        assert word not in parts, word
    assert "MAX_" not in _source()


def test_not_wired_to_the_runtime():
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("project_current_progress_limitations", "CurrentProgressLimitations"):
        assert name not in api, name
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_limitations.py" and "progress_limitations" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    # Seul consommateur, lui-même non branché : le détail 6-4D.
    assert users == ["core/competency_progress_detail.py"]


def test_no_migration_nor_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0012_decryptage_cognitive_links.py" and len(versions) == 12
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "Limitation" not in models and "limitation" not in models.lower().replace("limitations", "")


# --------------------------------------------------------------------------
# 10. Intégration PostgreSQL
# --------------------------------------------------------------------------

def _load(Sessions):  # noqa: N803
    with Sessions() as session:
        snapshot = ad.load_adaptation_state(session, user_id="u")
        assert not session.new and not session.dirty and not session.deleted
    return snapshot


def test_pg_real_v2_chain_projects_exactly_the_unobserved_fact(Sessions):  # noqa: F811
    """T3 Application C7_A / C7_B / C7_C + contradiction sur C7_D -> T5 ->
    T6 (v2) -> 6-1A -> 6-4A -> limitations : C7_D, porteuse d'une
    contradiction mais d'aucune observation positive située, reste signalée ;
    aucune capacité représentée n'est projetée."""
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", "C7_C", evidence_strength="strong"))
    p.t3(p.contra("C7_D", contradiction_scope="application"))
    _, decision = p.infer()
    snapshot = _load(Sessions)
    (c7_state,) = snapshot.competencies
    assert c7_state.current_stage == decision.current_stage == "application"
    current = next(c for c in c7_state.claims if c.stage == "application")
    assert current.confidence_profile["schema_version"] == "confidence-profile-v2"
    progress = project_current_progress(state=snapshot)
    result = project_current_progress_limitations(state=snapshot, progress=progress, competency_code="C7")
    assert result.less_observed_status == "available"
    assert [c.capability_code for c in result.less_observed_capabilities] == ["C7_D"]
    represented = {c.capability_code for c in card(progress, "C7").represented_capabilities}
    assert represented == {"C7_A", "C7_B", "C7_C"}
    assert not represented & {c.capability_code for c in result.less_observed_capabilities}
    assert not UUID_PATTERN.search(repr(result))


def test_pg_historical_v1_run_is_unavailable_never_reconstructed(Sessions):  # noqa: F811
    from tests.test_adaptation_state import _historical_v1
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", "C7_C", evidence_strength="strong"))
    p.infer(rewrite=_historical_v1)
    snapshot = _load(Sessions)
    (c7_state,) = snapshot.competencies
    coverage = c7_state.claims[2].confidence_profile["coverage"]
    assert UNOBSERVED_CAPABILITIES_PRESENT in coverage["fact_codes"] and coverage["capability_definition_ids"]
    result = project_current_progress_limitations(state=snapshot, progress=project_current_progress(state=snapshot),
                                                  competency_code="C7")
    assert (result.less_observed_status, result.less_observed_capabilities) == ("unavailable", ())
