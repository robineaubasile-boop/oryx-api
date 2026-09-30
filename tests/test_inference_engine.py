"""Tests de T6-C3 : moteur final (core/inference_engine.py) et policies
finales (core/inference_final_policies.py).

1. Tests purs (toujours exécutés) : taxonomie courante (definition_id ->
   membership_id), assemblage (claims, tensions, refs, JSON, résumés),
   DecisionInvariantGuard, versions, déterminisme, pureté statique. La
   validation structurelle privée de T6-B (_validated_decision) n'y sert que
   d'ORACLE de test : le moteur ne l'appelle jamais.

2. Tests d'intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL,
   sinon SKIPPÉS ; jamais SQLite) : T3 / T5 réels -> start T6-B ->
   infer_competency -> complétion T6-B -> COMMIT, sans aucune décision
   construite à la main.
"""
import ast
import dataclasses
import inspect
import re
import uuid

import pytest

from core import inference_service as svc
from core.inference_engine import (
    _FORBIDDEN_PAYLOAD_KEYS,
    _guard,
    _membership_ids,
    infer_competency,
)
from core.inference_final_policies import (
    FINAL_INFERENCE_POLICIES,
    FINAL_INFERENCE_V1_POLICY,
    FinalInferenceError,
    FinalInferencePolicyResolver,
    InvalidAssembledDecision,
    InvalidFinalInferencePolicy,
    UnresolvableCapabilityMembership,
    UnsupportedFinalInferencePolicy,
    resolve_final_inference_policy,
    validate_final_inference_policy,
)
from core.inference_policies import UnsupportedInferenceContext, UnsupportedPositiveBasisPolicy
from core.inference_positive_basis import evaluate_positive_basis
from core.inference_service import BasisRefDecision, CurrentTaxonomyCapability, TensionDecision
from core.inference_state import evaluate_inference_state
from core.inference_state_policies import (
    DIMENSION_FACT_CODES,
    UnsupportedConfidenceProfilePolicy,
    UnsupportedStateDecisionPolicy,
)
from tests.test_inference_positive_basis import (
    NS,
    STAGES,
    VERSIONS,
    Dossier,
    _mastery_dossier,
    competency_codes,
    definition_id,
    taxonomy,
)
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_inference_state import _held
from tests.test_inference_transition import DecisionChain, revised
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

T6C3_PATHS = tuple(REPO_ROOT / "core" / name for name in (
    "inference_final_policies.py", "inference_transition.py", "inference_validation.py", "inference_engine.py"))
ENGINE_PATH = T6C3_PATHS[-1]
DIMENSIONS = ["diagnosticity", "coverage", "independence", "consistency", "temporal_validation"]


def _single():
    d = Dossier("C2")
    o42 = d.observe("e42", "C2_A", "C2_B", name="O42")
    return d, o42


def _tensioned():
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    k = d.contra("e2", "C2_B", name="k")
    return d, k


def refs(decision, role, **target):
    return [r for r in decision.basis_refs if r.ref_role == role
            and all(getattr(r, name) == value for name, value in target.items())]


def membership(code, context):
    return next(c.membership_id for c in context.current_taxonomy_context.capabilities if c.capability_code == code)


BUILDERS = {
    "empty": lambda: Dossier("C1"),
    "single": lambda: _single()[0],
    "tensioned": lambda: _tensioned()[0],
    "held": lambda: _held()[0],
    "mastery": lambda: _mastery_dossier()[0],
}


# --------------------------------------------------------------------------
# API, erreurs, versions
# --------------------------------------------------------------------------

def test_single_public_entry_point():
    assert list(inspect.signature(infer_competency).parameters) == ["context"]
    public = {}
    for path in T6C3_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        public[path.name] = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {
        "inference_engine.py": {"infer_competency"},
        "inference_transition.py": {"resolve_transition_cause", "relation_endpoints"},
        "inference_validation.py": {"evaluate_validation_needs", "tension_revision_status"},
        "inference_final_policies.py": {"validate_final_inference_policy", "resolve_final_inference_policy",
                                        "check_inference_assessments"},
    }


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidFinalInferencePolicy, UnsupportedFinalInferencePolicy, UnresolvableCapabilityMembership,
                InvalidAssembledDecision):
        assert issubclass(cls, FinalInferenceError)


def test_v1_supports_exactly_one_complete_combination():
    assert list(FINAL_INFERENCE_POLICIES) == [("positive_basis-1", "confidence_profile-1", "state_decision-1",
                                               "validation-1", "inference_schema-1", "evaluator-1")]
    validate_final_inference_policy(FINAL_INFERENCE_V1_POLICY)
    resolved = resolve_final_inference_policy(_single()[0].context())
    assert resolved.rules is FINAL_INFERENCE_V1_POLICY
    assert "mastery" not in FINAL_INFERENCE_V1_POLICY.confirmation_stages
    with pytest.raises(InvalidFinalInferencePolicy):
        validate_final_inference_policy(dataclasses.replace(FINAL_INFERENCE_V1_POLICY,
                                                            confirmation_stages=("mastery",)))


@pytest.mark.parametrize("field", ["validation_version", "inference_schema_version", "evaluator_version"])
@pytest.mark.parametrize("value", ["{}-2", "{}-0", "", None, 1, "{}-1 "])
def test_unknown_t6c3_versions_fail_closed_without_default(field, value):
    if isinstance(value, str) and "{}" in value:
        value = value.format(field.split("_version")[0])
    context = _single()[0].context(**{field: value})
    with pytest.raises(UnsupportedFinalInferencePolicy, match=field):
        infer_competency(context)


def test_t6c1_t6c2_guards_are_composed_not_duplicated():
    d = _single()[0]
    for overrides, error in (({"positive_basis_version": "positive_basis-2"}, UnsupportedPositiveBasisPolicy),
                             ({"confidence_profile_version": "confidence_profile-2"},
                              UnsupportedConfidenceProfilePolicy),
                             ({"state_decision_version": "state_decision-2"}, UnsupportedStateDecisionPolicy),
                             ({"input_schema_version": 1}, UnsupportedInferenceContext),
                             ({"model_id": "claude"}, UnsupportedInferenceContext),
                             ({"prompt_spec_version": "p1"}, UnsupportedInferenceContext)):
        with pytest.raises(error):
            infer_competency(d.context(**overrides))
    undeclared = FinalInferencePolicyResolver(registry={("positive_basis-1", "confidence_profile-1",
                                                         "state_decision-1", "validation-1", "inference_schema-1",
                                                         "evaluator-2"): FINAL_INFERENCE_V1_POLICY})
    with pytest.raises(UnsupportedFinalInferencePolicy, match="evaluator_version"):
        undeclared.resolve(d.context())


# --------------------------------------------------------------------------
# 31-34 : TaxonomyMembershipResolver
# --------------------------------------------------------------------------

def test_localized_tension_resolves_to_the_exact_current_membership():
    """31."""
    d, k = _tensioned()
    context = d.context()
    (tension,) = infer_competency(context).tensions
    assert tension.scope_mode == "localized"
    assert tension.capability_membership_ids == (membership("C2_B", context),)
    many = _membership_ids([definition_id("C2_C"), definition_id("C2_A")], context.current_taxonomy_context)
    assert many == (membership("C2_A", context), membership("C2_C", context))  # ordre naturel T4-B


def test_same_code_other_revision_is_never_remapped_by_code():
    """32 : même capability_code, autre definition_id / semantic_revision."""
    current = taxonomy("C8", revisions={"C8_C": 2})
    assert {c.capability_code for c in current.capabilities} >= {"C8_C"}
    with pytest.raises(UnresolvableCapabilityMembership):
        _membership_ids([definition_id("C8_C", 1)], current)
    assert _membership_ids([definition_id("C8_C", 2)], current) == (
        next(c.membership_id for c in current.capabilities if c.capability_code == "C8_C"),)


def test_absent_or_duplicated_definition_fails_closed():
    """33."""
    current = taxonomy("C8")
    with pytest.raises(UnresolvableCapabilityMembership):
        _membership_ids([definition_id("C9_A")], current)
    with pytest.raises(UnresolvableCapabilityMembership):
        _membership_ids([], current)
    duplicated = dataclasses.replace(current, capabilities=(*current.capabilities, CurrentTaxonomyCapability(
        membership_id=uuid.uuid5(NS, "autre-membership"), definition_id=definition_id("C8_A"),
        capability_code="C8_A", semantic_revision=1, label="doublon")))
    with pytest.raises(UnresolvableCapabilityMembership):
        _membership_ids([definition_id("C8_A")], duplicated)


def test_competency_only_tension_has_no_membership():
    """34."""
    d = Dossier("C10")
    d.observe("e1", "C10_B", "C10_A", name="a")
    k = d.contra("e2", scope="application", name="k")
    decision = infer_competency(d.context())
    (tension,) = decision.tensions
    assert (tension.scope_mode, tension.capability_membership_ids) == ("competency_only", ())
    assert tension.summary == "application:competency_only:claim_relevant_contradiction+competency_only_contradiction"
    assert [(r.source_kind, r.source_id) for r in refs(decision, "tension", tension_key="t0")] == [("observation", k)]


# --------------------------------------------------------------------------
# 35-47 : claims, refs, JSON
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(BUILDERS))
def test_exactly_four_claims_and_t6b_structural_compatibility(name):
    """35 + 53 (oracle T6-B de TEST)."""
    context = BUILDERS[name]().context()
    decision = infer_competency(context)
    assert [c.stage for c in decision.claims] == list(STAGES)
    svc._validated_decision(decision)
    positive = evaluate_positive_basis(context)
    for claim, source in zip(decision.claims, positive.claims):
        assert (claim.positive_basis_status, claim.basis_mode) == (source.status, source.basis_mode)
        assert (claim.confidence_profile is None) == (source.status != "established")
        assert claim.mastery_assessment is None or claim.stage == "mastery"


def test_positive_basis_refs_only_for_direct_claims_never_for_implied():
    """36 + 37 + 38 : O42 est base de la seule Application ; la Comprehension
    implied garde sa provenance de confiance sans seconde base positive."""
    d, o42 = _single()
    decision = infer_competency(d.context())
    assert [(r.claim_stage, r.source_id) for r in refs(decision, "positive_basis")] == [("application", o42)]
    for stage in ("discovery", "comprehension"):
        assert not refs(decision, "positive_basis", claim_stage=stage)
        assert refs(decision, "confidence", claim_stage=stage, confidence_dimension="diagnosticity",
                    source_id=o42)
    assert all(r.source_kind == "observation" for r in refs(decision, "positive_basis"))


def test_mastery_refs_only_on_the_mastery_claim_and_deduplicated():
    """39 + 46 : une relation qui documente plusieurs propriétés reste une
    seule source ; aucun 5/5."""
    d, a, b, t = _mastery_dossier()
    context = d.context()
    decision = infer_competency(context)
    mastery = refs(decision, "mastery")
    assert mastery and {r.claim_stage for r in mastery} == {"mastery"}
    assert [(r.source_kind, r.source_id) for r in mastery] == sorted(
        {("observation", a), ("observation", b), ("transfer", t)}, key=lambda x: (x[0], str(x[1])))
    assert not [r for r in refs(decision, "positive_basis") if r.source_kind != "observation"]
    payload = decision.claims[3].mastery_assessment
    assert list(payload) == ["schema_version", "autonomy", "independent_repetition", "variety_transfer",
                             "longitudinality", "robustness_revision", "represented_capability_definition_ids",
                             "limitations"]
    assert all(set(payload[p]) == {"status", "reason_codes", "limitations"} and payload[p]["status"] == "supported"
               for p in ("autonomy", "independent_repetition", "variety_transfer", "longitudinality",
                         "robustness_revision"))
    text = repr(payload)
    for forbidden in ("score", "5/5", "4/5", "almost", "percentage", str(a), str(t)):
        assert forbidden not in text


def test_every_tension_has_a_tension_ref_and_every_need_a_validation_ref():
    """40 + 41."""
    for name in BUILDERS:
        decision = infer_competency(BUILDERS[name]().context())
        for tension in decision.tensions:
            assert refs(decision, "tension", tension_key=tension.tension_key)
        assert bool(decision.validation_needs) <= bool(refs(decision, "validation"))
    assert [t.tension_key for t in infer_competency(_held()[0].context()).tensions] == ["t0", "t1", "t2"]


def test_transition_and_validation_refs_are_run_level():
    """42 + 43 + 44."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    decision = chain.previous[2]
    assert refs(decision, "transition") and refs(decision, "validation")
    for role in ("transition", "validation"):
        for ref in refs(decision, role):
            assert (ref.claim_stage, ref.tension_key, ref.confidence_dimension) == (None, None, None)
    for ref in refs(decision, "confidence"):
        assert ref.claim_stage in STAGES and ref.confidence_dimension in DIMENSIONS and ref.tension_key is None


def test_confidence_json_is_the_qualitative_reading_only():
    """45."""
    d, k = _tensioned()
    decision = infer_competency(d.context())
    observation_ids = {str(o.observation_id) for o in d.observations}
    for claim in decision.claims[:3]:
        profile = claim.confidence_profile
        assert list(profile) == ["schema_version", *DIMENSIONS]
        assert profile["schema_version"] == "confidence-profile-v1"
        for name in DIMENSIONS:
            assert list(profile[name]) == ["fact_codes", "capability_definition_ids", "limitations"]
            assert set(profile[name]["fact_codes"]) <= set(DIMENSION_FACT_CODES[name])
            assert all(isinstance(v, str) for v in (*profile[name]["fact_codes"], *profile[name]["limitations"]))
        assert not any(i in repr(profile) for i in observation_ids)
    assert decision.claims[3].confidence_profile is None


def test_revision_context_is_exactly_the_t6c2_payload():
    """47."""
    context = _held()[0].context()
    state = evaluate_inference_state(context, evaluate_positive_basis(context))
    decision = infer_competency(context)
    assert decision.unresolved_revision_context == state.revision_context.to_payload()
    assert infer_competency(_single()[0].context()).unresolved_revision_context is None


# --------------------------------------------------------------------------
# 48-50 : résumés d'audit (gabarits fermés)
# --------------------------------------------------------------------------

def test_basis_scope_and_state_summaries_are_deterministic_templates():
    """48 + 49 + 50."""
    d, k = _tensioned()
    decision = infer_competency(d.context())
    assert [(c.basis_summary, c.scope_summary) for c in decision.claims] == [
        ("implied_by_higher_claim:application", "localized:C2_A@r1,C2_B@r1"),
        ("implied_by_higher_claim:application", "localized:C2_A@r1,C2_B@r1"),
        ("direct:single_representative_demonstration", "localized:C2_A@r1,C2_B@r1"),
        ("not_established:insufficient_longitudinal_structure", "none")]
    assert decision.state_decision_summary == ("current_stage=application;state_reason="
                                               "highest_positive_claim_with_open_tension;transition_cause=none;"
                                               "validation=revalidation")
    assert decision.tensions[0].summary == "application:localized:claim_relevant_contradiction:C2_B@r1"
    first = infer_competency(_single()[0].context())
    assert first.state_decision_summary == ("current_stage=application;state_reason=highest_compatible_positive_claim;"
                                            "transition_cause=none;validation=confirmation")
    mastery = infer_competency(_mastery_dossier()[0].context())
    assert mastery.claims[3].basis_summary == "direct:longitudinal_mastery_history"
    competency_only = Dossier("C10")
    competency_only.observe("e1", name="a")
    assert infer_competency(competency_only.context()).claims[2].scope_summary == "competency_only"
    empty = infer_competency(Dossier("C1").context())
    assert empty.state_decision_summary == ("current_stage=non_etabli;state_reason=no_current_positive_claim;"
                                            "transition_cause=none;validation=none")
    chain = DecisionChain()
    revised(chain)
    assert chain.previous[2].state_decision_summary == (
        "current_stage=comprehension;state_reason=downward_revision_to_defensible_lower_claim;"
        "transition_cause=new_user_evidence;validation=confirmation+revalidation")


# --------------------------------------------------------------------------
# 51-55 : déterminisme, aucun UUID généré
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(BUILDERS))
def test_same_context_same_decision_whatever_the_collection_order(name):
    """51 + 52."""
    d = BUILDERS[name]()
    assert infer_competency(d.context()) == infer_competency(d.context()) == infer_competency(d.context(reverse=True))


def test_capability_source_order_does_not_change_the_decision():
    """53."""
    first, second = Dossier("C2"), Dossier("C2")
    first.observe("e1", "C2_A", "C2_B", name="a")
    second.observe("e1", "C2_B", "C2_A", name="a")
    first.contra("e2", "C2_C", "C2_B", name="k")
    second.contra("e2", "C2_B", "C2_C", name="k")
    assert infer_competency(first.context()) == infer_competency(second.context())


def _uuids(value):
    if isinstance(value, uuid.UUID):
        yield value
    elif isinstance(value, str):
        for match in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
            yield uuid.UUID(match)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _uuids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _uuids(item)
    elif dataclasses.is_dataclass(value):
        for f in dataclasses.fields(value):
            yield from _uuids(getattr(value, f.name))


def test_no_uuid_is_generated_every_identifier_comes_from_the_context():
    """54."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    d.observe("e3", "C8_B", "C8_C", name="n")
    context, decision = chain.step(d, new=["e3"])
    for context, decision in ((context, decision), (_held()[0].context(), infer_competency(_held()[0].context()))):
        known = set(_uuids(context))
        assert set(_uuids(decision)) <= known


# --------------------------------------------------------------------------
# DecisionInvariantGuard
# --------------------------------------------------------------------------

def _valid():
    d, k = _tensioned()
    context = d.context()
    state = evaluate_inference_state(context, evaluate_positive_basis(context))
    return context, state, infer_competency(context)


def _mutations(decision):
    claims = decision.claims
    tension = decision.tensions[0]
    ref = decision.basis_refs[0]
    o = next(r.source_id for r in decision.basis_refs if r.ref_role == "positive_basis")
    yield "trois claims", dataclasses.replace(decision, claims=claims[:3])
    yield "claims désordonnées", dataclasses.replace(decision, claims=(claims[1], claims[0], *claims[2:]))
    forged = BasisRefDecision(ref_role="positive_basis", source_kind="observation", source_id=o,
                              claim_stage="comprehension")
    yield "positive_basis sur implied", dataclasses.replace(decision, basis_refs=(*decision.basis_refs, forged))
    yield "tension sans ref", dataclasses.replace(decision, basis_refs=tuple(
        r for r in decision.basis_refs if r.ref_role != "tension"))
    yield "besoin sans ref validation", dataclasses.replace(decision, basis_refs=tuple(
        r for r in decision.basis_refs if r.ref_role != "validation"))
    yield "score", dataclasses.replace(decision, claims=(dataclasses.replace(
        claims[0], confidence_profile={**claims[0].confidence_profile, "score": "3"}), *claims[1:]))
    yield "localized sans membership", dataclasses.replace(decision, tensions=(
        dataclasses.replace(tension, capability_membership_ids=()),))
    yield "membership inventé", dataclasses.replace(decision, tensions=(
        dataclasses.replace(tension, capability_membership_ids=(uuid.uuid5(NS, "x"),)),))
    yield "competency_only avec membership", dataclasses.replace(decision, tensions=(
        dataclasses.replace(tension, scope_mode="competency_only"),))
    yield "cause en première inférence", dataclasses.replace(decision, transition_cause="new_user_evidence")
    yield "ref en double", dataclasses.replace(decision, basis_refs=(*decision.basis_refs, ref))
    yield "observation absente", dataclasses.replace(decision, basis_refs=(*decision.basis_refs, BasisRefDecision(
        ref_role="transition", source_kind="observation", source_id=uuid.uuid5(NS, "absente"))))
    yield "mastery_assessment hors mastery", dataclasses.replace(decision, claims=(
        *claims[:2], dataclasses.replace(claims[2], mastery_assessment={"schema_version": "x"}), claims[3]))
    yield "revision_status inventé", dataclasses.replace(decision, tensions=(
        dataclasses.replace(tension, revision_status="resolved"),))
    yield "non_etabli avec claim", dataclasses.replace(decision, current_stage="non_etabli")
    yield "contexte de révision parallèle", dataclasses.replace(decision, unresolved_revision_context={"x": "y"})
    yield "besoin hors format", dataclasses.replace(decision, validation_needs=(
        {**decision.validation_needs[0], "priority": "1"},))
    yield "tension inconnue", dataclasses.replace(decision, tensions=(TensionDecision(
        tension_key="t9", fragilized_stage="application", scope_mode="competency_only", summary="x",
        revision_status="unresolved"),))


def test_invariant_guard_accepts_the_assembled_decision_and_rejects_violations():
    context, state, decision = _valid()
    _guard(context, state, decision)
    for label, mutated in _mutations(decision):
        with pytest.raises(InvalidAssembledDecision):
            _guard(context, state, mutated)
            pytest.fail(label)


def test_forbidden_payload_keys_cover_scores_and_raw_provenance():
    for key in ("score", "level", "percentage", "ratio", "average", "priority", "observation_ids", "relation_ids",
                "source_ids", "question", "surface", "task", "passed", "failed"):
        assert key in _FORBIDDEN_PAYLOAD_KEYS


# --------------------------------------------------------------------------
# Pureté statique
# --------------------------------------------------------------------------

def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def _identifiers(path):
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.alias):
            names.append(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names.append(node.arg)
    return names


def test_no_database_llm_network_clock_randomness_or_uuid_generation():
    """55 + pureté."""
    allowed = {"dataclasses", "collections.abc", "types", "core.inference_service", "core.inference_policies",
               "core.inference_positive_basis", "core.inference_state_policies", "core.inference_state",
               "core.inference_confidence", "core.inference_final_policies", "core.inference_transition",
               "core.inference_validation"}
    for path in T6C3_PATHS:
        assert _imports(path) <= allowed, (path.name, _imports(path) - allowed)
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower().split("\n")
        for word in ("sqlalchemy", "session", "select", "execute", "commit", "rollback", "flush", "db", "models",
                     "anthropic", "openai", "claude", "llm", "requests", "httpx", "urllib", "socket", "random",
                     "datetime", "now", "utcnow", "time", "uuid4", "uuid1", "uuid", "sleep", "getenv", "environ",
                     "open", "_validated_decision", "complete_competency_inference", "start_competency_inference",
                     "get_inference_context"):
            assert word not in tokens, (path.name, word)


def test_no_text_reinterpretation_of_opaque_payloads():
    for path in T6C3_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")
        for field in ("observation_text", "primary_user_action", "contributive_user_actions", "residual_cognitive_work",
                      "source_contribution_refs", "event_started_at_technical", "event_closed_at_technical",
                      "demonstration_time", "observation_created_at_inference", "current_integrity_status",
                      "current_evaluation_run_interpretation_status", "run_completed_at_technical"):
            assert field not in tokens, (path.name, field)


def test_no_score_counter_or_numeric_rule():
    for path in T6C3_PATHS:
        words = set(re.split(r"[^a-z0-9]+", "\n".join(_identifiers(path)).lower()))
        for word in ("score", "scores", "scoring", "percent", "percentage", "ratio", "average", "mean", "points",
                     "weight", "weighted", "coefficient", "threshold", "xp", "decay", "probability", "penalty",
                     "balance", "almost", "sum", "count", "counter", "min", "max", "days", "freshness"):
            assert word not in words, (path.name, word)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) in (int, float)], \
            path.name
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "len"], \
            path.name
    for compound in ("competency_score", "evidence_balance", "contradiction_penalty", "decay_rate", "mastery_points",
                     "overall_user_level", "stage_probability", "coverage_percent", "weighted_evidence",
                     "negative_points", "validation_priority_score", "validation_score", "capability_mastered",
                     "capability_stage"):
        for path in T6C3_PATHS:
            assert compound not in _identifiers(path), (path.name, compound)


def test_no_active_validation_plan_and_no_step6_behavior():
    """47 (spéc.) : la seule sortie 5.4 persistée est validation_needs."""
    for path in T6C3_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower()
        words = set(re.split(r"[^a-z0-9_]+", tokens)) | set(re.split(r"[^a-z0-9]+", tokens))
        for word in ("active_validation_plan", "validation_task", "question_to_ask", "target_surface",
                     "surface_recommendation", "learning_path", "cta", "next_question", "prompt", "academy",
                     "rallye", "coach", "decryptage", "portfolio", "gamification", "posture", "targeted",
                     "opportunistic"):
            assert word not in words, (path.name, word)


def test_production_engine_never_calls_the_private_t6b_validator():
    source = ENGINE_PATH.read_text(encoding="utf-8")
    assert "_validated_decision" not in _code_tokens(source)
    assert "_validated_decision" not in "".join(_identifiers(ENGINE_PATH))


# --------------------------------------------------------------------------
# 2. Intégration PostgreSQL : T3 / T5 réels -> T6-B -> T6-C -> T6-B
# --------------------------------------------------------------------------

T6_VERSIONS = {name: f"{name.split('_version')[0]}-1" for name in VERSIONS}


class Pipeline:
    """Chaîne réelle d'un couple (utilisateur de test, C7) : T3 commité,
    T5 activé, start T6-B, infer_competency, complétion T6-B + COMMIT."""

    def __init__(self, Sessions):  # noqa: N803
        from core import models
        from core import taxonomy_service as tax
        from tests.test_taxonomy_service import _def_kwargs

        self.Sessions = Sessions
        with Sessions() as session:
            release = tax.create_candidate_release(session, version_key=f"t6c3-{uuid.uuid4()}",
                                                   spec_fingerprint=f"sha256:{uuid.uuid4()}")
            self.m = {}
            for code in competency_codes("C7"):
                existing = session.query(models.CoreCapabilityDefinition).filter_by(
                    capability_code=code, semantic_revision=1).one_or_none()
                definition = existing or tax.create_capability_definition(session, **_def_kwargs(code, 1))
                self.m[code] = tax.attach_capability_to_release(session, release_id=release.id,
                                                                capability_definition_id=definition.id).id
            tax.activate_release(session, release_id=release.id)
            session.commit()
            self.release_id = release.id

    def t3(self, *specs):
        from tests.test_longitudinal_service import _t3
        return _t3(self.Sessions, self.release_id, *specs)

    def app(self, *codes, **overrides):
        from tests.test_longitudinal_service import app
        return app(*(self.m[c] for c in codes), **overrides)

    def contra(self, *codes, **overrides):
        from tests.test_longitudinal_service import contra
        return contra(*(self.m[c] for c in codes), **overrides)

    def infer(self):
        from core import longitudinal_service as t5
        from tests.test_longitudinal_service import _start as start_t5
        parent = start_t5(self.Sessions, self.release_id)
        with self.Sessions() as session:
            t5.complete_longitudinal_assessment(session, run_id=parent)
            session.commit()
        with self.Sessions() as session:
            context = svc.start_competency_inference(session, longitudinal_assessment_run_id=parent,
                                                     trigger="longitudinal_completed", **T6_VERSIONS)
            session.commit()
        with self.Sessions() as session:
            assert svc.get_inference_context(session, run_id=context.run_id) == context
        decision = infer_competency(context)
        assert decision == infer_competency(context)
        with self.Sessions() as session:
            run = svc.complete_competency_inference(session, run_id=context.run_id, decision=decision)
            session.commit()
            assert (run.execution_status, run.interpretation_status) == ("completed", "active")
        return context, decision

    def state(self, context):
        with self.Sessions() as session:
            return svc.get_validated_user_competency_state(session, user_id=context.user_id, competency_code="C7")

    def run(self, run_id):
        with self.Sessions() as session:
            return svc.get_competency_inference(session, run_id=run_id)


def test_pg_scenario_a_first_real_inference_is_completed_and_cached(Sessions):  # noqa: F811
    """A : T5 réel -> start -> infer_competency -> complete -> COMMIT."""
    p = Pipeline(Sessions)
    a = p.t3(p.app("C7_A", "C7_B", evidence_strength="strong")).id
    context, decision = p.infer()
    assert context.predecessor is None
    assert (decision.current_stage, decision.transition_cause) == ("application", None)
    assert [r.source_id for r in refs(decision, "positive_basis")] == [a]
    state = p.state(context)
    assert (state.current_stage, state.tension_state, state.state_generation,
            state.active_inference_run_id) == ("application", "none", 1, context.run_id)
    run = p.run(context.run_id)
    assert run.validation_needs == [n for n in decision.validation_needs]
    assert run.state_decision_summary == decision.state_decision_summary
    with Sessions() as session:
        claims = svc.get_stage_claims(session, run_id=context.run_id)
        assert [(c.stage, c.positive_basis_status, c.basis_mode) for c in claims] == [
            ("discovery", "established", "implied_by_higher_claim"),
            ("comprehension", "established", "implied_by_higher_claim"),
            ("application", "established", "direct"), ("mastery", "not_established", "none")]
        assert claims[2].confidence_profile == decision.claims[2].confidence_profile
        stored = {(r.ref_role, r.source_kind) for r in svc.get_inference_basis_refs(session, run_id=context.run_id)}
        assert ("positive_basis", "observation") in stored and ("validation", "observation") in stored


def test_pg_scenario_b_predecessor_is_superseded_and_cache_updated(Sessions):  # noqa: F811
    """B : premier run actif -> nouveau T5 -> start avec predecessor réel ->
    infer -> complete ; predecessor superseded, nouveau run active, cache."""
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    first, _ = p.infer()
    n = p.t3(p.app("C7_A", "C7_C", evidence_strength="strong")).id
    second, decision = p.infer()
    assert second.predecessor.inference_run_id == first.run_id
    assert second.transition_causality.new_user_event_ids
    assert (decision.current_stage, decision.transition_cause) == ("application", "new_user_evidence")
    assert n in {r.source_id for r in refs(decision, "transition")}
    assert p.run(first.run_id).interpretation_status == "superseded"
    run = p.run(second.run_id)
    assert (run.interpretation_status, run.previous_stage, run.transition, run.transition_cause) == (
        "active", "application", "maintained", "new_user_evidence")
    state = p.state(second)
    assert (state.active_inference_run_id, state.current_stage, state.state_generation) == (
        second.run_id, "application", 2)


def test_pg_scenario_c_new_contradiction_revises_down_and_persists_the_revision(Sessions):  # noqa: F811
    """C : Application -> contradiction locale (tension, revalidation du
    périmètre exact) -> contradiction représentative et diagnostique :
    revised_down, motif persisté, revision_status revalidation_needed."""
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    local = p.t3(p.contra("C7_A", contradiction_scope="application")).id
    second, tensioned = p.infer()
    assert (tensioned.current_stage, tensioned.transition_cause) == ("application", "new_user_evidence")
    (tension,) = tensioned.tensions
    assert (tension.fragilized_stage, tension.capability_membership_ids, tension.revision_status) == (
        "application", (p.m["C7_A"],), "revalidation_needed")
    assert [r.source_id for r in refs(tensioned, "tension")] == [local]
    material = p.t3(p.contra("C7_A", "C7_B", contradiction_scope="application", evidence_strength="strong")).id
    third, revised_decision = p.infer()
    assert (revised_decision.current_stage, revised_decision.transition_cause) == ("comprehension",
                                                                                  "new_user_evidence")
    assert material in {r.source_id for r in refs(revised_decision, "transition")}
    motif = revised_decision.unresolved_revision_context["motifs"][0]
    assert motif["source_contradiction_observation_ids"] == [str(material)]
    run = p.run(third.run_id)
    assert (run.previous_stage, run.transition, run.tension_state) == ("application", "revised_down", "open")
    assert run.unresolved_revision_context == revised_decision.unresolved_revision_context
    with Sessions() as session:
        stored = svc.get_inference_tensions(session, run_id=third.run_id)
        by_scope = {tuple(sorted(map(str, memberships))): t.revision_status for t, memberships in stored
                    if t.fragilized_stage == "application"}
    # Le motif exact (C7_A + C7_B) est à revalider ; la tension locale C7_A,
    # plus décisionnelle, reste visible et unresolved (aucun besoin inventé).
    assert by_scope == {tuple(sorted((str(p.m["C7_A"]), str(p.m["C7_B"])))): "revalidation_needed",
                        (str(p.m["C7_A"]),): "unresolved"}
    assert (p.state(third).current_stage, p.state(third).tension_state) == ("comprehension", "open")


def test_pg_scenario_d_anti_oscillation_old_evidence_never_rebounds_new_exact_evidence_does(Sessions):  # noqa: F811
    """D : troisième run -> seules des preuves sans rapport : aucun rebound ;
    quatrième run -> démonstration nouvelle sur le périmètre exact du motif
    : motif résolu, upgrade accepté par l'anti-oscillation de T6-B."""
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", "C7_B", contradiction_scope="application", evidence_strength="strong"))
    _, revised_decision = p.infer()
    assert revised_decision.current_stage == "comprehension"
    p.t3(p.app("C7_D", local_stage="discovery"))
    third, blocked = p.infer()
    assert third.predecessor.unresolved_revision_context["schema_version"] == "revision-context-v1"
    assert (blocked.current_stage, blocked.transition_cause) == ("comprehension", "new_user_evidence")
    assert blocked.unresolved_revision_context["origin_inference_run_id"] == \
        revised_decision.unresolved_revision_context["origin_inference_run_id"]
    n = p.t3(p.app("C7_A", "C7_B", evidence_strength="strong", support_level="none")).id
    fourth, rebound = p.infer()
    assert (rebound.current_stage, rebound.transition_cause) == ("application", "new_user_evidence")
    assert rebound.unresolved_revision_context is None
    assert "state_reason=revision_motif_resolved_supportively" in rebound.state_decision_summary
    assert n in {r.source_id for r in refs(rebound, "transition")}
    run = p.run(fourth.run_id)
    assert (run.previous_stage, run.transition, run.transition_cause) == ("comprehension", "upgraded",
                                                                          "new_user_evidence")
    assert p.state(fourth).current_stage == "application"


def test_pg_scenario_e_invalidation_of_the_only_evidence_is_an_integrity_reset(Sessions):  # noqa: F811
    """E : l'unique preuve est invalidée (T3-B) : non_etabli par
    evidence_integrity_change, sans ref transition, accepté par T6-B."""
    from tests.test_longitudinal_service import _invalidate
    p = Pipeline(Sessions)
    a = p.t3(p.app("C7_A", "C7_B", evidence_strength="strong")).id
    p.t3(p.app("C7_D", local_stage="none"))
    p.infer()
    _invalidate(Sessions, a)
    context, decision = p.infer()
    assert [c.observation_id for c in context.transition_causality.integrity_changes] == [a]
    assert (decision.current_stage, decision.transition_cause) == ("non_etabli", "evidence_integrity_change")
    assert not refs(decision, "transition") and decision.validation_needs == ()
    run = p.run(context.run_id)
    assert (run.previous_stage, run.transition) == ("application", "revised_down")
    assert p.state(context).current_stage == "non_etabli"
