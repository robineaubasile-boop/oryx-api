"""Tests de l'Étape 6.4B1 : acquisition des observations positives qui
expliquent la carte de progression actuelle (core/progress_evidence.py).

1. Contrats : versions, vocabulaires fermés, dataclasses immuables, API
   publique, hiérarchie d'erreurs, exclusions publiques.
2. Statuts purs (aucune lecture) : no_state, no_positive_basis,
   current_stage_not_established ; jamais d'emprunt à une claim inférieure.
3. Claim source : direct, implied (claim directe supérieure la plus proche),
   chaîne implied, implied sans source, Mastery implied.
4. Chaîne probante : positive_basis observation seulement, doublon, snapshot
   T5, compétence, polarité, intégrité, run T3, événement, texte,
   vocabulaires T3 ; stale vs invalid.
5. Revalidation Step 5 avant / après (aucun résultat partiel, aucun retry).
6. Périmètre : localized / competency_only / mixed, intersection, règle
   Mastery, definition_id exact, jetons de capacité.
7. Inertie : evidence_strength, horodatages, résumés d'audit ; ordre
   technique des refs ; déterminisme.
8. Compatibilité 6-4A : projection falsifiée => incompatible.
9. Contrat statique : imports, lecture seule, aucun LLM, aucune écriture,
   aucun max-3, aucune migration, aucun branchement runtime.
10. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
    SKIPPÉS) : T3 -> T5 -> T6 réels -> 6-1A -> 6-4A -> 6-4B1.

Couches 2 à 8 : le snapshot est produit par la projection 6-1A RÉELLE
(_project_competency) depuis des enregistrements construits à la main
(tests/test_adaptation_state.World), puis la carte par 6-4A réelle ; seuls
les services propriétaires relus par 6-4B1 sont simulés (Chain), cohérents
avec ces enregistrements sauf corruption explicite.
"""
import ast
import contextlib
import dataclasses
import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from core import adaptation_state as ad
from core import progress_evidence as pe
from core.adaptation_state import AdaptationStateSnapshot, load_adaptation_state
from core.inference_service import InvalidInferenceState, StaleInferenceChain
from core.observation_service import ELICITATION_MODES as T3_ELICITATION_MODES
from core.observation_service import SUPPORT_LEVELS as T3_SUPPORT_LEVELS
from core.observation_service import ObservationNotFound
from core.progress_evidence import (
    BASIS_DIRECT,
    BASIS_INHERITED_FROM_HIGHER_CLAIM,
    BASIS_ORIGINS,
    EVIDENCE_AVAILABLE,
    EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED,
    EVIDENCE_NO_POSITIVE_BASIS,
    EVIDENCE_NO_STATE,
    EVIDENCE_STATUSES,
    PROGRESS_EVIDENCE_POLICY_VERSION,
    PROGRESS_EVIDENCE_SCHEMA_VERSION,
    SCOPE_COMPETENCY_ONLY,
    SCOPE_LOCALIZED,
    SCOPE_MODES,
    IncompatibleProgressEvidenceInputs,
    InvalidProgressEvidenceArgument,
    InvalidProgressEvidenceState,
    ProgressEvidenceCandidate,
    ProgressEvidenceError,
    ProgressEvidenceSet,
    StaleProgressEvidence,
    acquire_progress_evidence,
)
from core.progress_projection import VisibleCapabilityProjection, project_current_progress
from tests.test_adaptation_state import World, mastery_payload
from tests.test_adaptation_state import uid as world_uid
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "progress_evidence.py"
NS = uuid.UUID("6a4b1000-0000-4000-8000-000000000000")
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


# --------------------------------------------------------------------------
# Services propriétaires simulés
# --------------------------------------------------------------------------

class FakeDB:
    """Session simulée : seul db.no_autoflush est permis."""

    def __init__(self):
        self.entered = 0

    @property
    def no_autoflush(self):
        @contextlib.contextmanager
        def block():
            self.entered += 1
            yield
        return block()

    def __getattr__(self, name):
        raise AssertionError(f"db.{name} : 6-4B1 ne touche la session que par no_autoflush")


class Chain:
    """Lignes relues par 6-4B1, dérivées des enregistrements d'un World 6-1A.
    Les dictionnaires d'overrides simulent une base modifiée APRÈS la
    capture du snapshot (corruption ou évolution amont)."""

    def __init__(self, w: World):
        self.w = w
        self.calls = []
        self.read_observations = []
        self.revalidations = []  # effet du n-ième appel : run_id, None ou exception
        self.run, self.parent = {}, {}
        self.observation, self.t3, self.event, self.mappings = {}, {}, {}, {}
        self.claims = self.refs = self.inputs = None
        self.texts = {}

    def text(self, observation_id):
        if observation_id not in self.texts:
            self.texts[observation_id] = f"Observation interne {len(self.texts) + 1}"
        return self.texts[observation_id]

    def state(self) -> AdaptationStateSnapshot:
        """Snapshot produit par la projection 6-1A réelle."""
        return AdaptationStateSnapshot(user_id="u", competencies=(self.w.project(),))

    def _observation_of(self, kind, value):
        for observation_id in self.w.observations:
            if uid(f"{kind}-{observation_id}") == value:
                return observation_id
        raise AssertionError(f"{kind} {value} inconnu")

    def install(self, monkeypatch):
        w = self.w

        def validated(db, *, user_id, competency_code):
            self.calls.append("get_validated_user_competency_state")
            n = self.calls.count("get_validated_user_competency_state") - 1
            effect = self.revalidations[n] if n < len(self.revalidations) else w.run_id
            if isinstance(effect, Exception):
                raise effect
            if effect is None:
                return None
            assert (user_id, competency_code) == ("u", w.code)
            return dataclasses.replace(w.validated(), active_inference_run_id=effect)

        def competency_inference(db, *, run_id):
            self.calls.append("get_competency_inference")
            assert run_id == w.run_id  # jamais l'active « courante »
            return SimpleNamespace(**{
                "id": run_id, "user_id": "u", "competency_code": w.code,
                "longitudinal_assessment_run_id": w.parent_id, "current_stage": w.current_stage,
                "tension_state": w.tension_state, "execution_status": "completed",
                "interpretation_status": "active", "state_decision_summary": "audit", **self.run})

        def stage_claims(db, *, run_id):
            self.calls.append("get_stage_claims")
            assert run_id == w.run_id
            if self.claims is not None:
                return list(self.claims)
            return [SimpleNamespace(id=c.id, inference_run_id=c.inference_run_id, stage=c.stage,
                                    positive_basis_status=c.positive_basis_status, basis_mode=c.basis_mode,
                                    basis_summary="audit", scope_summary="audit")
                    for c in w.acquired().claims]

        def basis_refs(db, *, run_id):
            self.calls.append("get_inference_basis_refs")
            assert run_id == w.run_id
            refs = self.refs if self.refs is not None else w.acquired().refs
            return [SimpleNamespace(**r._asdict()) for r in refs]

        def longitudinal_assessment(db, *, run_id):
            self.calls.append("get_longitudinal_assessment")
            assert run_id == w.parent_id
            return SimpleNamespace(**{"id": run_id, "user_id": "u", "competency_code": w.code,
                                      "execution_status": "completed", "interpretation_status": "active",
                                      **self.parent})

        def longitudinal_inputs(db, *, run_id):
            self.calls.append("get_longitudinal_inputs")
            ids = self.inputs if self.inputs is not None else w.snapshot
            return [SimpleNamespace(run_id=run_id, observation_id=i) for i in sorted(ids, key=str)]

        def observation(db, *, observation_id):
            self.calls.append("get_observation")
            self.read_observations.append(observation_id)
            record = w.observations.get(observation_id)
            if record is None:
                raise ObservationNotFound(str(observation_id))
            return SimpleNamespace(**{
                "id": observation_id, "evaluation_run_id": uid(f"t3-{observation_id}"), "ordinal": 0,
                "competency_code": record.competency_code, "observation_role": "primary",
                "polarity": record.polarity, "integrity_status": "valid",
                "capability_localization": record.capability_localization,
                "observation_text": self.text(observation_id), "elicitation_mode": "prompted",
                "support_level": "none", "evidence_strength": "medium", "local_stage": "application",
                "error_type": None, "residual_cognitive_work": {"description": "x"},
                "source_contribution_refs": [], "created_at": T0, **self.observation.get(observation_id, {})})

        def evaluation_run(db, *, run_id):
            self.calls.append("get_evaluation_run")
            observation_id = self._observation_of("t3", run_id)
            return SimpleNamespace(**{
                "id": run_id, "event_id": uid(f"event-{observation_id}"), "execution_status": "completed",
                "interpretation_status": "active",
                "pedagogical_taxonomy_release_id": w.observations[observation_id].source_taxonomy_release_id,
                "completed_at": T0, "created_at": T0, **self.t3.get(observation_id, {})})

        def event(db, *, event_id):
            self.calls.append("get_event")
            observation_id = self._observation_of("event", event_id)
            return SimpleNamespace(**{"id": event_id, "user_id": "u", "status": "finalized", "closed_at": T0,
                                      **self.event.get(observation_id, {})})

        def observation_capabilities(db, *, observation_id):
            self.calls.append("get_observation_capabilities")
            mappings = self.mappings.get(observation_id, w.observations[observation_id].mappings)
            return [(SimpleNamespace(observation_id=observation_id),
                     SimpleNamespace(taxonomy_release_id=m.taxonomy_release_id,
                                     capability_definition_id=m.definition_id),
                     SimpleNamespace(id=m.definition_id, competency_code=m.competency_code))
                    for m in mappings]

        for name, fake in (("get_validated_user_competency_state", validated),
                           ("get_competency_inference", competency_inference),
                           ("get_stage_claims", stage_claims),
                           ("get_inference_basis_refs", basis_refs),
                           ("get_longitudinal_assessment", longitudinal_assessment),
                           ("get_longitudinal_inputs", longitudinal_inputs),
                           ("get_observation", observation),
                           ("get_evaluation_run", evaluation_run),
                           ("get_event", event),
                           ("get_observation_capabilities", observation_capabilities)):
            monkeypatch.setattr(pe, name, fake)
        return self

    def acquire(self, state=None, progress=None, code=None, db=None):
        state = state if state is not None else self.state()
        progress = progress if progress is not None else project_current_progress(state=state)
        return acquire_progress_evidence(db or FakeDB(), state=state, progress=progress,
                                         competency_code=code or self.w.code)


def positive_refs(w, stage, *observation_ids):
    return tuple(ad._RefRecord(w.run_id, world_uid(f"claim-{stage}"), "positive_basis", "observation", i)
                 for i in observation_ids)


def with_claim(state, stage, **changes):
    """Snapshot public modifié à la main (état impossible en amont)."""
    (snapshot,) = state.competencies
    claims = tuple(dataclasses.replace(c, **changes) if c.stage == stage else c for c in snapshot.claims)
    return AdaptationStateSnapshot(user_id=state.user_id,
                                   competencies=(dataclasses.replace(snapshot, claims=claims),))


def tokens(result):
    return [(c.evidence_token, c.scope_mode, c.capability_tokens) for c in result.candidates]


# --------------------------------------------------------------------------
# Mondes (compétence C8 du World 6-1A : catalogue C8_A..C8_D, révision 1)
# --------------------------------------------------------------------------

def application_world(current="application"):
    """Application directe localized (a : C8_C / C8_A ; b : C8_B),
    Comprehension / Discovery implied, Mastery not_established."""
    w = World()
    a = w.obs("a", "C", "A")
    b = w.obs("b", "B")
    w.direct("application", a, b)
    w.implied("comprehension")
    w.implied("discovery")
    w.current_stage = current
    return w, a, b


def mastery_world(represented_letters, current="mastery"):
    """Mastery directe (a : C8_A / C8_B ; c : C8_C), périmètre canonique =
    represented_letters (sous-ensemble), stades inférieurs implied."""
    w = World()
    a = w.obs("a", "A", "B")
    c = w.obs("c", "C")
    w.direct("mastery", a, c, assessment=mastery_payload(*(w.d(x) for x in represented_letters)))
    for stage in ("application", "comprehension", "discovery"):
        w.implied(stage)
    w.current_stage = current
    return w, a, c


@pytest.fixture
def chain(monkeypatch):
    def build(w):
        return Chain(w).install(monkeypatch)
    return build


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_versions_and_closed_vocabularies_are_frozen():
    assert PROGRESS_EVIDENCE_SCHEMA_VERSION == "progress-evidence-set-v1"
    assert PROGRESS_EVIDENCE_POLICY_VERSION == "progress-evidence-policy-1"
    assert EVIDENCE_STATUSES == ("no_state", "no_positive_basis", "current_stage_not_established", "available")
    assert (EVIDENCE_NO_STATE, EVIDENCE_NO_POSITIVE_BASIS, EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED,
            EVIDENCE_AVAILABLE) == EVIDENCE_STATUSES
    assert BASIS_ORIGINS == ("direct", "inherited_from_higher_claim")
    assert (BASIS_DIRECT, BASIS_INHERITED_FROM_HIGHER_CLAIM) == BASIS_ORIGINS
    assert SCOPE_MODES == ("localized", "competency_only") == (SCOPE_LOCALIZED, SCOPE_COMPETENCY_ONLY)


def test_structures_are_frozen_keyword_only_dataclasses_with_exactly_these_fields():
    expected = {
        ProgressEvidenceCandidate: ["evidence_token", "observation_text", "scope_mode", "capability_tokens",
                                    "elicitation_mode", "support_level"],
        ProgressEvidenceSet: ["schema_version", "policy_version", "competency_code", "evidence_status",
                              "basis_origin", "source_claim_stage", "candidates"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls))
    candidate = ProgressEvidenceCandidate(evidence_token="evidence_1", observation_text="t", scope_mode="localized",
                                          capability_tokens=("C8_A@r1",), elicitation_mode="prompted",
                                          support_level="none")
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.observation_text = "autre"
    with pytest.raises(TypeError):
        ProgressEvidenceCandidate("evidence_1", "t", "localized", (), "prompted", "none")  # noqa


def test_public_contracts_exclude_identifiers_scores_and_timestamps():
    fields = {f.name for cls in (ProgressEvidenceCandidate, ProgressEvidenceSet) for f in dataclasses.fields(cls)}
    for forbidden in ("user_id", "observation_id", "event_id", "run_id", "inference_run_id", "uuid", "id",
                      "taxonomy_release_id", "definition_id", "membership_id", "evidence_strength", "confidence",
                      "score", "weight", "timestamp", "created_at", "local_stage", "observation_role",
                      "residual_cognitive_work", "ordinal", "error_type", "source_contribution_refs",
                      "rank", "count", "priority", "quality", "percent", "percentage", "summary", "why_text",
                      "explanation", "visible_text", "user_message", "bullet_text", "label"):
        assert forbidden not in fields, forbidden
    parts = {part for name in fields for part in name.split("_")}
    assert not {"score", "weight", "confidence", "strength", "count", "rank", "priority", "id", "at"} & parts


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidProgressEvidenceArgument, IncompatibleProgressEvidenceInputs, InvalidProgressEvidenceState,
                StaleProgressEvidence):
        assert issubclass(cls, ProgressEvidenceError)
    assert not issubclass(StaleProgressEvidence, InvalidProgressEvidenceState)
    assert not issubclass(ProgressEvidenceError, (ad.AdaptationStateError, StaleInferenceChain))


def test_public_api_is_exactly_acquire_progress_evidence():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"acquire_progress_evidence"}
    params = list(inspect.signature(acquire_progress_evidence).parameters.values())
    assert [p.name for p in params] == ["db", "state", "progress", "competency_code"]
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params[1:])


def test_t3_vocabularies_are_reused_not_copied():
    assert T3_SUPPORT_LEVELS == frozenset({"none", "hinted", "guided", "answer_given"})
    assert T3_ELICITATION_MODES == frozenset({"prompted", "spontaneous"})


# --------------------------------------------------------------------------
# 2. Arguments
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("state", None), ("state", {"user_id": "u"}), ("progress", None), ("progress", ()),
    ("competency_code", "C13"), ("competency_code", "c8"), ("competency_code", 8), ("competency_code", None),
    ("competency_code", "C0"), ("competency_code", " C8"),
])
def test_bad_arguments_are_rejected(chain, field, value):
    c = chain(application_world()[0])
    state = c.state()
    kwargs = {"state": state, "progress": project_current_progress(state=state), "competency_code": "C8"}
    kwargs[field] = value
    with pytest.raises(InvalidProgressEvidenceArgument):
        acquire_progress_evidence(FakeDB(), **kwargs)
    assert c.calls == []


def test_subclass_instances_are_not_accepted(chain):
    c = chain(application_world()[0])
    state = c.state()

    class Snapshot(AdaptationStateSnapshot):
        pass

    forged = Snapshot(user_id="u", competencies=state.competencies)
    with pytest.raises(InvalidProgressEvidenceArgument):
        c.acquire(state=forged, progress=project_current_progress(state=state))


@pytest.mark.parametrize("user_id", ["", "   ", "u\x00", 7])
def test_blank_user_identity_is_rejected(chain, user_id):
    c = chain(application_world()[0])
    state = dataclasses.replace(c.state(), user_id=user_id)
    with pytest.raises(InvalidProgressEvidenceArgument):
        c.acquire(state=state, progress=project_current_progress(state=c.state()))


# --------------------------------------------------------------------------
# 3. Statuts purs : aucune lecture
# --------------------------------------------------------------------------

def test_no_state_is_deterministic_and_reads_nothing(chain):
    c = chain(application_world()[0])
    db = FakeDB()
    result = c.acquire(code="C7", db=db)
    assert result == ProgressEvidenceSet(
        schema_version="progress-evidence-set-v1", policy_version="progress-evidence-policy-1",
        competency_code="C7", evidence_status="no_state", basis_origin=None, source_claim_stage=None,
        candidates=())
    assert c.calls == [] and db.entered == 0
    empty = AdaptationStateSnapshot(user_id="u", competencies=())
    assert c.acquire(state=empty, code="C8") == dataclasses.replace(result, competency_code="C8")
    assert c.calls == []


def test_non_etabli_is_no_positive_basis_and_never_looks_for_negative_evidence(chain):
    w = World()
    contra = w.obs("contra", "A", polarity="contradictory")
    assert contra  # observation présente au dossier : jamais cherchée
    c = chain(w)
    db = FakeDB()
    result = c.acquire(db=db)
    assert (result.evidence_status, result.basis_origin, result.source_claim_stage, result.candidates) == (
        "no_positive_basis", None, None, ())
    assert c.calls == [] and db.entered == 0
    assert result != c.acquire(code="C7")  # no_positive_basis != no_state


def test_current_stage_not_established_never_borrows_a_lower_claim(chain):
    w = World()
    comprehension = w.obs("comp", "A", "B")
    w.direct("comprehension", comprehension)
    w.implied("discovery")
    w.current_stage = "application"  # Application not_established, Comprehension établie
    c = chain(w)
    state = c.state()
    (snapshot,) = state.competencies
    assert [(cl.stage, cl.status) for cl in snapshot.claims][:3] == [
        ("discovery", "established"), ("comprehension", "established"), ("application", "not_established")]
    db = FakeDB()
    result = c.acquire(state=state, db=db)
    assert (result.evidence_status, result.basis_origin, result.source_claim_stage, result.candidates) == (
        "current_stage_not_established", None, None, ())
    assert c.calls == [] and db.entered == 0


# --------------------------------------------------------------------------
# 4. Claim source
# --------------------------------------------------------------------------

def test_direct_claim_exposes_its_own_positive_basis(chain):
    w, a, b = application_world()
    c = chain(w)
    result = c.acquire()
    assert (result.schema_version, result.policy_version, result.competency_code) == (
        "progress-evidence-set-v1", "progress-evidence-policy-1", "C8")
    assert (result.evidence_status, result.basis_origin, result.source_claim_stage) == (
        "available", "direct", "application")
    assert result.candidates == (
        ProgressEvidenceCandidate(evidence_token="evidence_1", observation_text=c.text(a), scope_mode="localized",
                                  capability_tokens=("C8_A@r1", "C8_C@r1"), elicitation_mode="prompted",
                                  support_level="none"),
        ProgressEvidenceCandidate(evidence_token="evidence_2", observation_text=c.text(b), scope_mode="localized",
                                  capability_tokens=("C8_B@r1",), elicitation_mode="prompted", support_level="none"))
    assert type(result.candidates) is tuple and all(type(x.capability_tokens) is tuple for x in result.candidates)
    assert c.read_observations == [a, b]


def test_reads_are_bound_to_the_captured_run_and_revalidated_before_and_after(chain):
    c = chain(application_world()[0])
    db = FakeDB()
    c.acquire(db=db)
    assert db.entered == 1
    assert c.calls[0] == c.calls[-1] == "get_validated_user_competency_state"
    assert c.calls.count("get_validated_user_competency_state") == 2
    assert c.calls[1:4] == ["get_competency_inference", "get_stage_claims", "get_inference_basis_refs"]
    assert c.calls[4:6] == ["get_longitudinal_assessment", "get_longitudinal_inputs"]
    assert c.calls[6:10] == ["get_observation", "get_evaluation_run", "get_event", "get_observation_capabilities"]


def test_implied_claim_resolves_the_nearest_higher_direct_claim(chain):
    w, a, b = application_world(current="comprehension")
    c = chain(w)
    result = c.acquire()
    assert (result.evidence_status, result.basis_origin, result.source_claim_stage) == (
        "available", "inherited_from_higher_claim", "application")
    assert [x.observation_text for x in result.candidates] == [c.text(a), c.text(b)]
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r1", "C8_C@r1")),
                              ("evidence_2", "localized", ("C8_B@r1",))]


def test_implied_chain_skips_implied_claims_to_the_nearest_direct_one(chain):
    w, a, b = application_world(current="discovery")
    c = chain(w)
    result = c.acquire()
    assert (result.basis_origin, result.source_claim_stage) == ("inherited_from_higher_claim", "application")
    assert c.read_observations == [a, b]


def test_nearest_higher_direct_wins_and_lower_direct_claims_are_never_read(chain):
    """Comprehension directe (c) SOUS le stade courant ; Application implied
    par Mastery directe : seule la base Mastery est lue."""
    w = World()
    low = w.obs("low", "D")
    a = w.obs("a", "A", "B")
    w.direct("comprehension", low)
    w.implied("discovery")
    w.implied("application")
    w.direct("mastery", a, assessment=mastery_payload(w.d("A"), w.d("B")))
    w.current_stage = "application"
    c = chain(w)
    result = c.acquire()
    assert (result.basis_origin, result.source_claim_stage) == ("inherited_from_higher_claim", "mastery")
    assert c.read_observations == [a] and low not in c.read_observations
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r1", "C8_B@r1"))]


def test_implied_without_any_higher_direct_claim_fails_closed(chain):
    c = chain(application_world()[0])
    state = with_claim(c.state(), "application", basis_mode="implied_by_higher_claim")
    with pytest.raises(InvalidProgressEvidenceState, match="sans claim directe"):
        c.acquire(state=state)
    assert c.calls == []


def test_implied_mastery_fails_closed(chain):
    c = chain(mastery_world("AB")[0])
    state = with_claim(c.state(), "mastery", basis_mode="implied_by_higher_claim")
    with pytest.raises(InvalidProgressEvidenceState, match="aucun stade supérieur"):
        c.acquire(state=state)
    assert c.calls == []


def test_inconsistent_status_and_basis_mode_of_the_current_claim_fails_closed(chain):
    c = chain(application_world()[0])
    state = with_claim(c.state(), "application", basis_mode="none")
    with pytest.raises(InvalidProgressEvidenceState):
        c.acquire(state=state)


# --------------------------------------------------------------------------
# 5. Refs positive_basis et claims T6 relues
# --------------------------------------------------------------------------

def test_direct_claim_without_positive_basis_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.refs = tuple(r for r in w.acquired().refs if r.ref_role != "positive_basis")
    with pytest.raises(InvalidProgressEvidenceState, match="sans ref positive_basis"):
        c.acquire(state=state)


@pytest.mark.parametrize("source_kind,observation_id", [
    ("dependency", None), ("transfer", None), ("revalidation", None), ("observation", None)])
def test_positive_basis_must_be_an_observation(chain, source_kind, observation_id):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.refs = (*positive_refs(w, "application", a),
              ad._RefRecord(w.run_id, world_uid("claim-application"), "positive_basis", source_kind, observation_id))
    with pytest.raises(InvalidProgressEvidenceState, match="observation requise"):
        c.acquire(state=state)


def test_duplicate_positive_ref_is_refused_never_deduplicated(chain):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.refs = positive_refs(w, "application", a, b, a)
    with pytest.raises(InvalidProgressEvidenceState, match="en double"):
        c.acquire(state=state)


def test_non_positive_refs_of_the_run_are_ignored(chain):
    w, a, b = application_world()
    c = chain(w)
    reference = c.acquire()
    c.refs = (ad._RefRecord(w.run_id, world_uid("claim-application"), "confidence", "dependency", None),
              *positive_refs(w, "application", a, b),
              ad._RefRecord(w.run_id, None, "transition", "observation", uid("other")),
              ad._RefRecord(w.run_id, world_uid("claim-mastery"), "mastery", "transfer", None))
    assert c.acquire() == reference


def test_own_positive_ref_on_an_implied_claim_fails_closed(chain):
    w, a, b = application_world(current="comprehension")
    c = chain(w)
    state = c.state()
    c.refs = (*positive_refs(w, "application", a, b), *positive_refs(w, "comprehension", a))
    with pytest.raises(InvalidProgressEvidenceState, match="sans base directe"):
        c.acquire(state=state)


def test_ref_of_another_run_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.refs = (*positive_refs(w, "application", a, b),
              ad._RefRecord(uid("other-run"), world_uid("claim-application"), "positive_basis", "observation", a))
    with pytest.raises(InvalidProgressEvidenceState, match="autre run"):
        c.acquire(state=state)


@pytest.mark.parametrize("change", [
    {"basis_mode": "implied_by_higher_claim"}, {"positive_basis_status": "not_established", "basis_mode": "none"}])
def test_t6_claims_must_match_the_snapshot(chain, change):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.claims = [SimpleNamespace(id=x.id, inference_run_id=x.inference_run_id, stage=x.stage,
                                positive_basis_status=x.positive_basis_status, basis_mode=x.basis_mode)
                for x in w.acquired().claims]
    c.claims[2] = SimpleNamespace(**{**vars(c.claims[2]), **change})
    with pytest.raises(InvalidProgressEvidenceState, match="divergente"):
        c.acquire(state=state)


@pytest.mark.parametrize("mutate", ["missing", "duplicate", "other_run"])
def test_t6_claim_structure_is_checked(chain, mutate):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    rows = [SimpleNamespace(id=x.id, inference_run_id=x.inference_run_id, stage=x.stage,
                            positive_basis_status=x.positive_basis_status, basis_mode=x.basis_mode)
            for x in w.acquired().claims]
    if mutate == "missing":
        rows = rows[:3]
    elif mutate == "duplicate":
        rows.append(rows[2])
    else:
        rows[0] = SimpleNamespace(**{**vars(rows[0]), "inference_run_id": uid("x")})
    c.claims = rows
    with pytest.raises(InvalidProgressEvidenceState):
        c.acquire(state=state)


def test_audit_summaries_are_never_read(chain):
    w, a, b = application_world()
    c = chain(w)
    reference = c.acquire()
    c.claims = [SimpleNamespace(id=x.id, inference_run_id=x.inference_run_id, stage=x.stage,
                                positive_basis_status=x.positive_basis_status, basis_mode=x.basis_mode,
                                basis_summary="implied_by_higher_claim:mastery",
                                scope_summary="localized:C8_D@r1;competency_only")
                for x in w.acquired().claims]
    c.run = {"state_decision_summary": "current_stage=mastery"}
    assert c.acquire() == reference


@pytest.mark.parametrize("field,value,error", [
    ("interpretation_status", "superseded", StaleProgressEvidence),
    ("interpretation_status", "candidate", InvalidProgressEvidenceState),
    ("interpretation_status", "obsolete", InvalidProgressEvidenceState),
    ("execution_status", "running", InvalidProgressEvidenceState),
    ("user_id", "u2", InvalidProgressEvidenceState),
    ("competency_code", "C9", InvalidProgressEvidenceState),
    ("current_stage", "mastery", InvalidProgressEvidenceState),
    ("longitudinal_assessment_run_id", uid("other-t5"), InvalidProgressEvidenceState),
])
def test_reread_t6_run_must_be_the_captured_one(chain, field, value, error):
    c = chain(application_world()[0])
    c.run = {field: value}
    with pytest.raises(error):
        c.acquire()


# --------------------------------------------------------------------------
# 6. Snapshot T5 et provenance T3 / T2
# --------------------------------------------------------------------------

def test_positive_observation_outside_the_consumed_t5_snapshot_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.inputs = frozenset(w.snapshot) - {b}
    with pytest.raises(InvalidProgressEvidenceState, match="hors du snapshot T5"):
        c.acquire(state=state)
    assert c.read_observations == []  # appartenance vérifiée avant toute lecture T3


@pytest.mark.parametrize("field,value,error", [
    ("interpretation_status", "superseded", StaleProgressEvidence),
    ("interpretation_status", "candidate", InvalidProgressEvidenceState),
    ("execution_status", "running", InvalidProgressEvidenceState),
    ("user_id", "u2", InvalidProgressEvidenceState),
    ("competency_code", "C7", InvalidProgressEvidenceState),
])
def test_t5_parent_is_the_captured_current_one(chain, field, value, error):
    c = chain(application_world()[0])
    c.parent = {field: value}
    with pytest.raises(error):
        c.acquire()


def test_observation_of_another_competency_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    c.observation[b] = {"competency_code": "C9"}
    with pytest.raises(InvalidProgressEvidenceState, match="de C9"):
        c.acquire()


def test_contradictory_positive_basis_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    c.observation[a] = {"polarity": "contradictory"}
    with pytest.raises(InvalidProgressEvidenceState, match="contradictory"):
        c.acquire()


def test_invalidated_observation_is_stale(chain):
    w, a, b = application_world()
    c = chain(w)
    c.observation[b] = {"integrity_status": "invalidated"}
    with pytest.raises(StaleProgressEvidence, match="invalidated"):
        c.acquire()
    assert c.calls.count("get_validated_user_competency_state") == 1  # aucun résultat partiel


@pytest.mark.parametrize("execution,interpretation,error", [
    ("completed", "superseded", StaleProgressEvidence),
    ("completed", "candidate", InvalidProgressEvidenceState),
    ("completed", "obsolete", InvalidProgressEvidenceState),
    ("running", "candidate", InvalidProgressEvidenceState),
    ("failed", "obsolete", InvalidProgressEvidenceState),
])
def test_t3_run_must_be_completed_and_active(chain, execution, interpretation, error):
    w, a, b = application_world()
    c = chain(w)
    c.t3[a] = {"execution_status": execution, "interpretation_status": interpretation}
    with pytest.raises(error):
        c.acquire()


@pytest.mark.parametrize("status", ["open", "abandoned"])
def test_non_finalized_event_fails_closed(chain, status):
    w, a, b = application_world()
    c = chain(w)
    c.event[b] = {"status": status}
    with pytest.raises(InvalidProgressEvidenceState, match=status):
        c.acquire()


def test_event_of_another_user_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    c.event[a] = {"user_id": "u2"}
    with pytest.raises(InvalidProgressEvidenceState, match="autre utilisateur"):
        c.acquire()


def test_missing_upstream_row_is_invalid_not_skipped(chain, monkeypatch):
    w, a, b = application_world()
    c = chain(w)
    real = pe.get_observation

    def missing(db, *, observation_id):
        if observation_id == b:
            raise ObservationNotFound(str(b))
        return real(db, observation_id=observation_id)

    monkeypatch.setattr(pe, "get_observation", missing)
    with pytest.raises(InvalidProgressEvidenceState, match="lecture impossible"):
        c.acquire()


@pytest.mark.parametrize("text", ["", "   \n", "texte\x00nul", None, 42])
def test_unusable_observation_text_fails_closed(chain, text):
    w, a, b = application_world()
    c = chain(w)
    c.observation[a] = {"observation_text": text}
    with pytest.raises(InvalidProgressEvidenceState, match="observation_text"):
        c.acquire()


def test_observation_text_is_kept_verbatim(chain):
    w, a, b = application_world()
    c = chain(w)
    text = "  A expliqué : « le ROE rapporte le résultat aux capitaux propres »…\n" + "détail " * 2000
    c.observation[a] = {"observation_text": text}
    result = c.acquire()
    assert result.candidates[0].observation_text == text  # ni tronqué, ni nettoyé, ni reformulé


@pytest.mark.parametrize("field,value", [
    ("elicitation_mode", "forced"), ("elicitation_mode", None), ("support_level", "partial"),
    ("support_level", "NONE")])
def test_t3_vocabularies_are_validated(chain, field, value):
    w, a, b = application_world()
    c = chain(w)
    c.observation[b] = {field: value}
    with pytest.raises(InvalidProgressEvidenceState, match=field):
        c.acquire()


def test_support_levels_are_preserved_without_filter_or_sort(chain):
    w = World()
    levels = ("answer_given", "none", "guided", "hinted")
    observations = [w.obs(f"o{i}", "A") for i in range(len(levels))]
    w.direct("application", *observations)
    w.current_stage = "application"
    c = chain(w)
    for observation_id, level in zip(observations, levels):
        c.observation[observation_id] = {"support_level": level}
    result = c.acquire()
    assert [x.support_level for x in result.candidates] == list(levels)  # answer_given compris
    assert [x.evidence_token for x in result.candidates] == ["evidence_1", "evidence_2", "evidence_3", "evidence_4"]


def test_elicitation_modes_are_preserved_without_scoring(chain):
    w = World()
    observations = [w.obs(f"o{i}", "A") for i in range(3)]
    w.direct("application", *observations)
    w.current_stage = "application"
    c = chain(w)
    modes = ("spontaneous", "prompted", "spontaneous")
    for observation_id, mode in zip(observations, modes):
        c.observation[observation_id] = {"elicitation_mode": mode}
    assert [x.elicitation_mode for x in c.acquire().candidates] == list(modes)


# --------------------------------------------------------------------------
# 7. Revalidation Step 5
# --------------------------------------------------------------------------

def test_stale_before_acquisition_reads_no_evidence(chain):
    c = chain(application_world()[0])
    c.revalidations = [uid("R43")]
    with pytest.raises(StaleProgressEvidence, match="run T6 courant"):
        c.acquire()
    assert c.calls == ["get_validated_user_competency_state"]


def test_stale_during_acquisition_returns_no_partial_result(chain):
    w = application_world()[0]
    c = chain(w)
    c.revalidations = [w.run_id, uid("R43")]
    with pytest.raises(StaleProgressEvidence, match="run T6 courant"):
        c.acquire()
    assert c.calls.count("get_validated_user_competency_state") == 2  # aucun retry
    assert c.calls[-1] == "get_validated_user_competency_state"


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("effect,error", [
    (None, StaleProgressEvidence),
    (StaleInferenceChain("observation invalidated"), StaleProgressEvidence),
    (InvalidInferenceState("cache divergent"), InvalidProgressEvidenceState),
])
def test_revalidation_outcomes(chain, position, effect, error):
    w = application_world()[0]
    c = chain(w)
    c.revalidations = [w.run_id] * position + [effect]
    with pytest.raises(error) as info:
        c.acquire()
    if isinstance(effect, Exception):
        assert info.value.__cause__ is effect


def test_revalidation_with_the_same_run_but_another_parent_is_invalid(chain, monkeypatch):
    w = application_world()[0]
    c = chain(w)
    real = pe.get_validated_user_competency_state

    def forged(db, *, user_id, competency_code):
        return dataclasses.replace(real(db, user_id=user_id, competency_code=competency_code),
                                   longitudinal_assessment_run_id=uid("other-t5"))

    monkeypatch.setattr(pe, "get_validated_user_competency_state", forged)
    with pytest.raises(InvalidProgressEvidenceState, match="parent divergent"):
        c.acquire()


# --------------------------------------------------------------------------
# 8. Périmètre
# --------------------------------------------------------------------------

def test_localized_tokens_follow_the_snapshot_catalogue_order(chain):
    w = World()
    o = w.obs("o", "D", "B", "A")  # ordre des mappings != ordre du catalogue
    w.direct("application", o)
    w.current_stage = "application"
    c = chain(w)
    c.mappings[o] = tuple(reversed(w.observations[o].mappings))
    result = c.acquire()
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r1", "C8_B@r1", "C8_D@r1"))]


def test_localized_card_with_a_competency_only_basis_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    assert project_current_progress(state=state).competencies[7].coverage_mode == "localized"
    c.observation[b] = {"capability_localization": "competency_only"}
    c.mappings[b] = ()
    with pytest.raises(InvalidProgressEvidenceState, match="competency_only sur une carte localized"):
        c.acquire(state=state)


def competency_only_world(current="application"):
    w = World()
    o1 = w.obs("o1", localization="competency_only")
    o2 = w.obs("o2", localization="competency_only")
    w.direct("application", o1, o2)
    w.implied("comprehension")
    w.implied("discovery")
    w.current_stage = current
    return w, o1, o2


def test_competency_only_card_exposes_competency_only_candidates_without_capability(chain):
    w, o1, o2 = competency_only_world()
    c = chain(w)
    state = c.state()
    card = project_current_progress(state=state).competencies[7]
    assert (card.coverage_mode, card.represented_capabilities) == ("competency_only", ())
    result = c.acquire(state=state)
    assert (result.evidence_status, result.basis_origin) == ("available", "direct")
    assert tokens(result) == [("evidence_1", "competency_only", ()), ("evidence_2", "competency_only", ())]
    assert c.calls.count("get_observation_capabilities") == 2  # aucun mapping vérifié


def test_competency_only_card_with_a_localized_basis_fails_closed(chain):
    w, o1, o2 = competency_only_world()
    c = chain(w)
    state = c.state()
    c.observation[o2] = {"capability_localization": "localized"}
    c.mappings[o2] = (ad._MappingRecord(w.release, w.d("A"), "C8"),)
    with pytest.raises(InvalidProgressEvidenceState, match="hors du périmètre de la carte competency_only"):
        c.acquire(state=state)


def test_competency_only_observation_with_a_mapping_fails_closed(chain):
    w, o1, o2 = competency_only_world()
    c = chain(w)
    c.mappings[o1] = (ad._MappingRecord(w.release, w.d("A"), "C8"),)
    with pytest.raises(InvalidProgressEvidenceState, match="mapping"):
        c.acquire()


def mixed_world():
    w = World()
    a = w.obs("a", "B", "A")
    o = w.obs("o", localization="competency_only")
    w.direct("application", a, o)
    w.current_stage = "application"
    return w, a, o


def test_mixed_card_exposes_both_families(chain):
    w, a, o = mixed_world()
    c = chain(w)
    state = c.state()
    card = project_current_progress(state=state).competencies[7]
    assert card.coverage_mode == "mixed"
    assert [x.capability_code for x in card.represented_capabilities] == ["C8_A", "C8_B"]
    result = c.acquire(state=state)
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r1", "C8_B@r1")),
                              ("evidence_2", "competency_only", ())]


@pytest.mark.parametrize("missing", ["competency_only", "localized"])
def test_mixed_card_without_both_families_fails_closed(chain, missing):
    w, a, o = mixed_world()
    c = chain(w)
    state = c.state()
    if missing == "competency_only":
        c.observation[o] = {"capability_localization": "localized"}
        c.mappings[o] = (ad._MappingRecord(w.release, w.d("A"), "C8"),)
    else:
        c.observation[a] = {"capability_localization": "competency_only"}
        c.mappings[a] = ()
    with pytest.raises(InvalidProgressEvidenceState, match="chaque famille"):
        c.acquire(state=state)


def test_empty_intersection_for_a_non_mastery_source_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    c.mappings[b] = (ad._MappingRecord(w.release, w.d("D"), "C8"),)  # C8_D : hors carte (A, B, C)
    with pytest.raises(InvalidProgressEvidenceState, match="hors du périmètre de la carte localized"):
        c.acquire(state=state)


@pytest.mark.parametrize("mapping", [
    ("other-release", "A", "C8"),   # mapping d'une autre release que celle du run T3
    ("release", "A", "C9"),         # mapping d'une autre compétence
    ("release", "Z", "C8"),         # definition_id absent du catalogue du snapshot
])
def test_localized_observation_without_compatible_mapping_fails_closed(chain, mapping):
    w, a, b = application_world()
    c = chain(w)
    state = c.state()
    release = world_uid(mapping[0])
    c.mappings[b] = (ad._MappingRecord(release, w.d(mapping[1]), mapping[2]),)
    with pytest.raises(InvalidProgressEvidenceState, match="sans capacité compatible"):
        c.acquire(state=state)


def test_localized_observation_of_a_t3_run_without_release_fails_closed(chain):
    w, a, b = application_world()
    c = chain(w)
    c.t3[a] = {"pedagogical_taxonomy_release_id": None}
    with pytest.raises(InvalidProgressEvidenceState, match="sans release"):
        c.acquire()


def test_mastery_basis_outside_the_canonical_scope_is_valid_but_not_a_candidate(chain):
    w, a, c_obs = mastery_world("AB")
    c = chain(w)
    state = c.state()
    card = project_current_progress(state=state).competencies[7]
    assert [x.capability_code for x in card.represented_capabilities] == ["C8_A", "C8_B"]
    result = c.acquire(state=state)
    assert (result.evidence_status, result.basis_origin, result.source_claim_stage) == (
        "available", "direct", "mastery")
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r1", "C8_B@r1"))]
    assert c.read_observations == [a, c_obs]  # provenance de c validée, mais pas candidate


def test_mastery_out_of_scope_basis_is_still_provenance_checked(chain):
    w, a, c_obs = mastery_world("AB")
    c = chain(w)
    c.observation[c_obs] = {"integrity_status": "invalidated"}
    with pytest.raises(StaleProgressEvidence):
        c.acquire()


def test_mastery_with_zero_visible_candidate_fails_closed(chain):
    w, a, c_obs = mastery_world("A")
    c = chain(w)
    state = c.state()
    c.mappings[a] = (ad._MappingRecord(w.release, w.d("C"), "C8"),)
    with pytest.raises(InvalidProgressEvidenceState, match="aucune candidate"):
        c.acquire(state=state)


def test_implied_claim_sourced_by_mastery_applies_the_mastery_rule(chain):
    w, a, c_obs = mastery_world("A", current="application")
    c = chain(w)
    result = c.acquire()
    assert (result.basis_origin, result.source_claim_stage) == ("inherited_from_higher_claim", "mastery")
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r1",))]


def test_mastery_competency_only_card_with_a_localized_basis_is_the_documented_nuance(chain):
    """Cas réel de T6-C1 (_mastery) : source d'un transfert localisée, cible
    competency_only => périmètre canonique vide, competency_only_basis True,
    carte competency_only ; la base localisée reste une provenance valide
    mais n'est jamais exposée."""
    w = World()
    local = w.obs("local", "A")
    only = w.obs("only", localization="competency_only")
    w.direct("mastery", local, only, assessment=mastery_payload())
    for stage in ("application", "comprehension", "discovery"):
        w.implied(stage)
    w.current_stage = "mastery"
    c = chain(w)
    state = c.state()
    assert project_current_progress(state=state).competencies[7].coverage_mode == "competency_only"
    result = c.acquire(state=state)
    assert tokens(result) == [("evidence_1", "competency_only", ())]
    assert c.read_observations == [local, only]


def test_capability_identity_is_the_exact_definition_never_the_code(chain):
    """Catalogue du snapshot : C8_A révision 2 (nouvelle définition).
    Observation d'une ANCIENNE release mappée sur C8_A révision 1 et C8_B :
    seule C8_B est compatible ; jamais C8_A@r2 par code."""
    w = World()
    w.capabilities[0] = ad._CapabilityRecord(world_uid("m-C8_A-r2"), w.release, world_uid("d-C8_A-r2"), "C8_A", 2,
                                             "label C8_A r2", "C8")
    old = world_uid("old-release")
    o = w.obs("o", source_release=old, mappings=(ad._MappingRecord(old, w.d("A"), "C8"),
                                                 ad._MappingRecord(old, w.d("B"), "C8")))
    w.direct("application", o)
    w.current_stage = "application"
    c = chain(w)
    state = c.state()
    card = project_current_progress(state=state).competencies[7]
    assert [(x.capability_code, x.semantic_revision) for x in card.represented_capabilities] == [("C8_B", 1)]
    assert tokens(c.acquire(state=state)) == [("evidence_1", "localized", ("C8_B@r1",))]


def test_capability_token_uses_the_snapshot_semantic_revision(chain):
    w = World()
    w.capabilities[0] = ad._CapabilityRecord(world_uid("m-C8_A-r2"), w.release, world_uid("d-C8_A-r2"), "C8_A", 2,
                                             "label C8_A r2", "C8")
    o = w.obs("o", mappings=(ad._MappingRecord(w.release, world_uid("d-C8_A-r2"), "C8"),
                             ad._MappingRecord(w.release, w.d("C"), "C8")))
    w.direct("application", o)
    w.current_stage = "application"
    result = chain(w).acquire()
    assert tokens(result) == [("evidence_1", "localized", ("C8_A@r2", "C8_C@r1"))]


# --------------------------------------------------------------------------
# 9. Inertie, ordre, déterminisme
# --------------------------------------------------------------------------

def test_evidence_strength_is_inert(chain):
    w, a, b = application_world()
    c = chain(w)
    reference = c.acquire()
    for strength in ("weak", "strong"):
        c.observation = {a: {"evidence_strength": strength}, b: {"evidence_strength": "weak"}}
        assert c.acquire() == reference


def test_timestamps_are_inert_and_never_order_the_candidates(chain):
    w, a, b = application_world()
    c = chain(w)
    reference = c.acquire()
    c.observation = {a: {"created_at": T0 + timedelta(days=400)}, b: {"created_at": T0 - timedelta(days=400)}}
    c.t3 = {a: {"completed_at": T0 + timedelta(days=1)}, b: {"completed_at": T0 - timedelta(days=9)}}
    c.event = {a: {"closed_at": T0 + timedelta(days=3)}}
    assert c.acquire() == reference


def test_other_t3_fields_are_inert(chain):
    w, a, b = application_world()
    c = chain(w)
    reference = c.acquire()
    c.observation = {a: {"observation_role": "secondary", "local_stage": "discovery", "ordinal": 9,
                         "residual_cognitive_work": {"description": "autre"}, "source_contribution_refs": [{}]}}
    assert c.acquire() == reference


def test_candidates_follow_the_technical_order_of_the_refs(chain):
    w, a, b = application_world()
    c = chain(w)
    forward = c.acquire()
    c.refs = positive_refs(w, "application", b, a)
    backward = c.acquire()
    assert [x.observation_text for x in backward.candidates] == [c.text(b), c.text(a)]
    assert [x.evidence_token for x in backward.candidates] == ["evidence_1", "evidence_2"]
    assert [x.observation_text for x in forward.candidates] == [c.text(a), c.text(b)]


def test_all_safe_candidates_are_returned_never_a_top_n(chain):
    w = World()
    observations = [w.obs(f"o{i}", "ABCD"[i % 4]) for i in range(15)]
    w.direct("application", *observations)
    w.current_stage = "application"
    result = chain(w).acquire()
    assert len(result.candidates) == 15
    assert [x.evidence_token for x in result.candidates] == [f"evidence_{i}" for i in range(1, 16)]


def test_same_inputs_same_evidence_set(chain):
    c = chain(application_world()[0])
    state = c.state()
    progress = project_current_progress(state=state)
    assert c.acquire(state=state, progress=progress) == c.acquire(state=state, progress=progress)


def test_output_carries_no_uuid_and_no_technical_identity(chain):
    w, a, b = application_world()
    result = chain(w).acquire()
    assert not UUID_PATTERN.search(repr(result))
    text = repr(result)
    for value in (w.run_id, w.parent_id, a, b, w.release, w.d("A")):
        assert str(value) not in text


# --------------------------------------------------------------------------
# 10. Compatibilité 6-4A
# --------------------------------------------------------------------------

def _replace_card(progress, code, **changes):
    return dataclasses.replace(progress, competencies=tuple(
        dataclasses.replace(card, **changes) if card.competency_code == code else card
        for card in progress.competencies))


@pytest.mark.parametrize("mutation", [
    "stage", "label", "competency_label", "coverage", "capability_missing", "capability_added",
    "capability_revision", "state_present", "schema", "other_card", "missing_card", "duplicate_card", "list",
    "foreign_card_type",
])
def test_falsified_projection_is_incompatible(chain, mutation):
    c = chain(application_world()[0])
    state = c.state()
    progress = project_current_progress(state=state)
    card = progress.competencies[7]
    extra = VisibleCapabilityProjection(capability_code="C8_D", semantic_revision=1, label="label C8_D")
    forged = {
        "stage": lambda: _replace_card(progress, "C8", stage_code="mastery"),
        "label": lambda: _replace_card(progress, "C8", stage_label="Expert"),
        "competency_label": lambda: _replace_card(progress, "C8", competency_label="Autre"),
        "coverage": lambda: _replace_card(progress, "C8", coverage_mode="mixed"),
        "capability_missing": lambda: _replace_card(progress, "C8",
                                                    represented_capabilities=card.represented_capabilities[:-1]),
        "capability_added": lambda: _replace_card(progress, "C8",
                                                  represented_capabilities=(*card.represented_capabilities, extra)),
        "capability_revision": lambda: _replace_card(progress, "C8", represented_capabilities=tuple(
            dataclasses.replace(x, semantic_revision=2) for x in card.represented_capabilities)),
        "state_present": lambda: _replace_card(progress, "C8", state_present=False),
        "schema": lambda: dataclasses.replace(progress, schema_version="current-progress-projection-v2"),
        "other_card": lambda: _replace_card(progress, "C2", stage_label="Notion reconnue"),
        "missing_card": lambda: dataclasses.replace(progress, competencies=progress.competencies[:7]
                                                    + progress.competencies[8:]),
        "duplicate_card": lambda: dataclasses.replace(progress, competencies=(*progress.competencies, card)),
        "list": lambda: dataclasses.replace(progress, competencies=list(progress.competencies)),
        "foreign_card_type": lambda: dataclasses.replace(progress, competencies=(
            *progress.competencies[:7], SimpleNamespace(**vars(card)), *progress.competencies[8:])),
    }[mutation]()
    with pytest.raises(IncompatibleProgressEvidenceInputs):
        c.acquire(state=state, progress=forged)
    assert c.calls == []


def test_projection_of_another_state_is_incompatible(chain):
    c = chain(application_world()[0])
    other = Chain(competency_only_world()[0]).state()
    with pytest.raises(IncompatibleProgressEvidenceInputs):
        c.acquire(progress=project_current_progress(state=other))


def test_snapshot_rejected_by_6_4a_is_an_invalid_state(chain):
    c = chain(application_world()[0])
    state = c.state()
    progress = project_current_progress(state=state)
    (snapshot,) = state.competencies
    broken = AdaptationStateSnapshot(user_id="u", competencies=(snapshot, snapshot))
    with pytest.raises(InvalidProgressEvidenceState, match="6-4A"):
        c.acquire(state=broken, progress=progress)


# --------------------------------------------------------------------------
# 11. Contrat statique
# --------------------------------------------------------------------------

def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    by_module = {}
    for node in _tree().body:
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                by_module.setdefault(alias.name, set())
    return by_module


def test_imports_are_exactly_the_owner_services_and_public_contracts():
    assert _imports() == {
        "uuid": set(),
        "dataclasses": {"dataclass"},
        "typing": {"NamedTuple"},
        "core.adaptation_state": {"COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot",
                                  "CompetencyAdaptationSnapshot"},
        "core.cognitive_capture": {"FINALIZED", "CognitiveCaptureError", "get_event"},
        "core.inference_service": {
            "ACTIVE", "BASIS_MODE_NONE", "CLAIM_STAGES", "COMPETENCY_ONLY", "COMPLETED", "DIRECT", "ESTABLISHED",
            "IMPLIED_BY_HIGHER_CLAIM", "LOCALIZED", "MASTERY", "NON_ETABLI", "NOT_ESTABLISHED", "POSITIVE_BASIS",
            "SOURCE_OBSERVATION", "SUPERSEDED", "InferenceServiceError", "StaleInferenceChain",
            "get_competency_inference", "get_inference_basis_refs", "get_stage_claims",
            "get_validated_user_competency_state"},
        "core.longitudinal_service": {"LongitudinalServiceError", "get_longitudinal_assessment",
                                      "get_longitudinal_inputs"},
        "core.observation_service": {"CONTRADICTORY", "ELICITATION_MODES", "INVALIDATED", "SUPPORT_LEVELS",
                                     "SUPPORTIVE", "VALID", "ObservationServiceError", "get_evaluation_run",
                                     "get_observation"},
        "core.progress_projection": {"COVERAGE_COMPETENCY_ONLY", "COVERAGE_LOCALIZED", "COVERAGE_MIXED",
                                     "CompetencyCurrentProgress", "CurrentProgressProjection",
                                     "ProgressProjectionError", "project_current_progress"},
        "core.taxonomy_service": {"TaxonomyServiceError", "get_observation_capabilities"},
    }
    imported = set().union(*_imports().values())
    assert not any(name.startswith("_") for name in imported)


def test_no_database_write_no_sql_no_lock_and_no_orm_model():
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    for forbidden in ("add_all", "flush", "commit", "rollback", "delete", "merge", "execute", "insert",
                      "update", "begin_nested", "with_for_update", "select", "text", "query", "refresh",
                      "expire", "pg_advisory_xact_lock", "FOR UPDATE", "add_support_trace"):
        assert forbidden not in names, forbidden
    used = {node.attr for node in ast.walk(_tree())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "db"}
    assert used == {"no_autoflush"}
    # Le seul .add est celui d'un set local (familles de candidates).
    adds = sorted(n.func.value.id for n in ast.walk(_tree()) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "add")
    assert adds == ["families"]
    assert not {"sqlalchemy", "sqlalchemy.orm", "core.models", "core.db", "alembic", "psycopg2"} & set(_imports())


def test_no_reinference_no_active_api_and_no_global_taxonomy():
    tokens = set(_code_tokens(_source()).split())
    for forbidden in ("get_active_competency_inference", "get_user_competency_state", "get_active_release",
                      "get_release_capabilities", "load_adaptation_state", "infer_competency",
                      "evaluate_positive_basis", "evaluate_inference_state", "build_longitudinal_dossier",
                      "get_inference_tensions", "get_dependencies", "get_transfers", "get_revalidations",
                      "load_taxonomy_v1", "get_support_traces", "get_observations"):
        assert forbidden not in tokens, forbidden


def test_audit_summaries_and_inert_t3_fields_are_never_read():
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    assert not [name for name in names if "summary" in name.lower()]
    for forbidden in ("evidence_strength", "created_at", "completed_at", "closed_at", "started_at", "ordinal",
                      "observation_role", "local_stage", "error_type", "residual_cognitive_work",
                      "source_contribution_refs", "confidence_profile", "mastery_assessment", "tensions",
                      "validation_needs", "state_generation", "primary_user_action", "stimulus_snapshot",
                      "user_work_snapshot"):
        assert forbidden not in names, forbidden


def test_no_llm_network_clock_or_randomness():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "random", "secrets", "datetime",
                "time", "os", "json", "logging"} & set(_imports())
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "classif", "backend", "complete(", "environ",
                 "getenv", "uuid4", "proposal"):
        assert word not in tokens, word
    assert not {"llm", "model", "now", "today"} & set(tokens.split())


def test_no_score_selection_bound_or_user_facing_text():
    tokens = _code_tokens(_source()).lower()
    parts = {part for token in tokens.split() for part in re.split(r"[^a-z0-9]+", token)}
    for word in ("score", "weight", "confidence", "percent", "rank", "priority", "quality", "best", "top",
                 "max", "limit", "sorted", "sort", "representative", "why", "explanation", "summary", "bullet",
                 "message", "render", "recommend", "weak", "strength"):
        assert word not in parts, word
    assert "MAX_" not in _source()
    integers = {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is int}
    assert integers <= {0, 1}, integers  # aucune borne pédagogique (aucun « 3 »)
    calls = {n.func.id for n in ast.walk(_tree()) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {"sorted", "max", "min", "sum", "reversed"} & calls


def test_no_migration_no_db_model_and_not_wired_to_the_runtime():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0011_assistant_deliveries.py" and len(versions) == 11
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "ProgressEvidence" not in models and "progress_evidence" not in models
    # Étape 6.4B2 : seuls consommateurs, eux-mêmes non branchés ; ils lisent
    # les contrats publics de 6-4B1, jamais acquire_progress_evidence (voir
    # tests/test_progress_evidence_selection.py et
    # tests/test_progress_evidence_selector.py). Étape 6.4B3 : la frontière
    # de rendu lit les mêmes contrats publics, plus les vocabulaires T3
    # support_level / elicitation_mode tels que 6-4B1 les applique ; son
    # renderer n'importe rien de 6-4B1 (voir
    # tests/test_progress_evidence_rendering.py et
    # tests/test_progress_evidence_renderer.py).
    step6_consumers = {
        "core/progress_evidence_selection.py": {
            "BASIS_DIRECT", "BASIS_INHERITED_FROM_HIGHER_CLAIM", "BASIS_ORIGINS", "EVIDENCE_AVAILABLE",
            "EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED", "EVIDENCE_NO_POSITIVE_BASIS", "EVIDENCE_NO_STATE",
            "EVIDENCE_STATUSES", "PROGRESS_EVIDENCE_POLICY_VERSION", "PROGRESS_EVIDENCE_SCHEMA_VERSION",
            "SCOPE_COMPETENCY_ONLY", "SCOPE_LOCALIZED", "SCOPE_MODES", "ProgressEvidenceCandidate",
            "ProgressEvidenceSet"},
        "core/progress_evidence_selector.py": {"ProgressEvidenceSet"},
        "core/progress_evidence_rendering.py": {
            "BASIS_DIRECT", "BASIS_INHERITED_FROM_HIGHER_CLAIM", "BASIS_ORIGINS", "ELICITATION_MODES",
            "EVIDENCE_AVAILABLE", "EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED", "EVIDENCE_NO_POSITIVE_BASIS",
            "EVIDENCE_NO_STATE", "EVIDENCE_STATUSES", "SCOPE_COMPETENCY_ONLY", "SCOPE_LOCALIZED", "SCOPE_MODES",
            "SUPPORT_LEVELS", "ProgressEvidenceCandidate"},
        "core/progress_evidence_renderer.py": set(),
        # Étape 6.4D : le détail lit seulement le contrat de sortie de 6-4B3,
        # rien de 6-4B1 (tests/test_competency_progress_detail.py).
        "core/competency_progress_detail.py": set(),
    }
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if getattr(n, "module", None) == "core.progress_evidence"
                    or a.name == "core.progress_evidence"]
        assert sorted(imported) == sorted(("core.progress_evidence", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/progress_evidence.py" and "progress_evidence" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    assert sorted(users) == sorted(step6_consumers)
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("acquire_progress_evidence", "ProgressEvidenceSet", "ProgressEvidenceCandidate"):
        assert name not in api, name
    tokens = _code_tokens(_source()).lower()
    for word in ("web_chat", "fastapi", "route", "endpoint", "coach", "education", "decrypt", "portfolio",
                 "rallye", "academy", "__tablename__", "column"):
        assert word not in tokens, word


# --------------------------------------------------------------------------
# 12. Intégration PostgreSQL : T3 / T5 / T6 réels -> 6-1A -> 6-4A -> 6-4B1
# --------------------------------------------------------------------------

UPSTREAM_TABLES = ("cognitive_events", "support_traces", "observation_evaluation_runs", "pedagogical_observations",
                   "observation_capabilities", "longitudinal_assessment_runs", "longitudinal_assessment_inputs",
                   "competency_inference_runs", "competency_stage_claims", "competency_inference_tensions",
                   "competency_inference_basis_refs", "user_competency_states")


def _dump(engine):  # noqa: F811
    with engine.connect() as conn:
        return {table: sorted(map(repr, conn.execute(sa.text(f"SELECT * FROM {table}")).all()))
                for table in UPSTREAM_TABLES}


def _sql(engine, statement, **params):  # noqa: F811
    with engine.begin() as conn:
        conn.execute(sa.text(statement), params)


def _views(Sessions):  # noqa: N803
    with Sessions() as session:
        state = load_adaptation_state(session, user_id="u")
    return state, project_current_progress(state=state)


def _acquire(Sessions, state, progress, code="C7"):  # noqa: N803
    with Sessions() as session:
        result = acquire_progress_evidence(session, state=state, progress=progress, competency_code=code)
        assert not session.new and not session.dirty and not session.deleted
        return result


def _recorded(engine, fn):  # noqa: F811
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        return fn(), statements
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def test_pg_direct_application_evidence_from_the_real_chain(Sessions, engine):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_B", "C7_A", evidence_strength="strong"))
    p.infer()
    state, progress = _views(Sessions)
    before = _dump(engine)
    result, statements = _recorded(engine, lambda: _acquire(Sessions, state, progress))
    assert _dump(engine) == before
    assert statements and all(" ".join(s.split()).startswith("SELECT") and "FOR " not in s for s in statements)
    assert result == ProgressEvidenceSet(
        schema_version="progress-evidence-set-v1", policy_version="progress-evidence-policy-1",
        competency_code="C7", evidence_status="available", basis_origin="direct",
        source_claim_stage="application",
        candidates=(ProgressEvidenceCandidate(
            evidence_token="evidence_1", observation_text="Explique correctement ce que mesure le ROE.",
            scope_mode="localized", capability_tokens=("C7_A@r1", "C7_B@r1"), elicitation_mode="prompted",
            support_level="hinted"),))
    assert not UUID_PATTERN.search(repr(result))
    assert _acquire(Sessions, state, progress) == result


def test_pg_pure_statuses_issue_no_statement(Sessions, engine):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_D", local_stage="none"))
    p.infer()
    state, progress = _views(Sessions)
    assert progress.competencies[6].stage_code == "non_etabli"
    for code, status in (("C7", "no_positive_basis"), ("C8", "no_state")):
        result, statements = _recorded(engine, lambda: _acquire(Sessions, state, progress, code=code))
        assert (result.evidence_status, result.candidates) == (status, ())
        assert statements == []


def test_pg_competency_only_then_mixed(Sessions):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    from tests.test_longitudinal_service import only
    p = Pipeline(Sessions)
    p.t3(only(p.app(evidence_strength="strong", observation_text="Applique le ROE sans capacité localisée.")))
    p.infer()
    state, progress = _views(Sessions)
    result = _acquire(Sessions, state, progress)
    assert progress.competencies[6].coverage_mode == "competency_only"
    assert tokens(result) == [("evidence_1", "competency_only", ())]
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong", support_level="answer_given",
               elicitation_mode="spontaneous"))
    p.infer()
    state, progress = _views(Sessions)
    assert progress.competencies[6].coverage_mode == "mixed"
    result = _acquire(Sessions, state, progress)
    # Ordre technique des refs (UUID des observations) : aucune valeur probante.
    assert sorted((x.scope_mode, x.capability_tokens) for x in result.candidates) == [
        ("competency_only", ()), ("localized", ("C7_A@r1", "C7_B@r1"))]
    assert [x.evidence_token for x in result.candidates] == ["evidence_1", "evidence_2"]
    localized = next(x for x in result.candidates if x.scope_mode == "localized")
    assert (localized.support_level, localized.elicitation_mode) == ("answer_given", "spontaneous")


def test_pg_stale_before_a_newer_inference(Sessions):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    state, progress = _views(Sessions)
    p.t3(p.app("C7_A", "C7_C", evidence_strength="strong"))
    p.infer()
    with pytest.raises(StaleProgressEvidence):
        _acquire(Sessions, state, progress)
    fresh_state, fresh_progress = _views(Sessions)
    assert _acquire(Sessions, fresh_state, fresh_progress).evidence_status == "available"


def _activate_r2(Sessions, parent):  # noqa: N803
    """Active un nouveau run T6 (R2) dans une AUTRE session, commité."""
    from core import inference_service as svc
    from core import longitudinal_service as t5
    from core.inference_engine import infer_competency
    from tests.test_inference_engine import T6_VERSIONS
    with Sessions() as other:
        t5.complete_longitudinal_assessment(other, run_id=parent)
        other.commit()
    with Sessions() as other:
        context = svc.start_competency_inference(other, longitudinal_assessment_run_id=parent,
                                                 trigger="longitudinal_completed", **T6_VERSIONS)
        other.commit()
    with Sessions() as other:
        svc.complete_competency_inference(other, run_id=context.run_id, decision=infer_competency(context))
        other.commit()
    return context.run_id


@pytest.mark.parametrize("moment", ["after_first_revalidation", "after_last_read"])
def test_pg_stale_when_a_newer_inference_is_activated_during_acquisition(Sessions, monkeypatch, moment):  # noqa: F811
    """R1 capturé et revalidé ; R2 activé et COMMITÉ par une autre session
    pendant l'acquisition : soit le run T6 relu est déjà superseded, soit la
    revalidation APRÈS désigne R2. Jamais de résultat, jamais de retry."""
    from tests.test_inference_engine import Pipeline
    from tests.test_longitudinal_service import _start as start_t5
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    first, _ = p.infer()
    state, progress = _views(Sessions)
    p.t3(p.app("C7_A", "C7_C", evidence_strength="strong"))
    parent = start_t5(Sessions, p.release_id)
    activated, validations = [], []
    real_validated, real_capabilities = pe.get_validated_user_competency_state, pe.get_observation_capabilities

    def validated(db, *, user_id, competency_code):
        result = real_validated(db, user_id=user_id, competency_code=competency_code)
        validations.append(result.active_inference_run_id)
        if moment == "after_first_revalidation" and not activated:
            activated.append(_activate_r2(Sessions, parent))
        return result

    def capabilities(db, *, observation_id):
        rows = real_capabilities(db, observation_id=observation_id)
        if moment == "after_last_read" and not activated:
            activated.append(_activate_r2(Sessions, parent))
        return rows

    monkeypatch.setattr(pe, "get_validated_user_competency_state", validated)
    monkeypatch.setattr(pe, "get_observation_capabilities", capabilities)
    with pytest.raises(StaleProgressEvidence) as info:
        _acquire(Sessions, state, progress)
    if moment == "after_first_revalidation":
        assert validations == [first.run_id] and "superseded" in str(info.value)
    else:
        assert validations == [first.run_id, activated[0]] and "run T6 courant" in str(info.value)


@pytest.mark.parametrize("case", ["observation_invalidated", "t3_superseded", "event_reopened"])
def test_pg_upstream_change_after_capture_is_stale(Sessions, engine, case):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    from tests.test_longitudinal_service import _invalidate, _t3
    p = Pipeline(Sessions)
    first = p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    state, progress = _views(Sessions)
    if case == "observation_invalidated":
        _invalidate(Sessions, first.id)
    elif case == "t3_superseded":
        _t3(Sessions, p.release_id, p.app("C7_A"), event_id=first.event)
    else:
        _sql(engine, "UPDATE cognitive_events SET status = 'open', closed_at = NULL WHERE id = :e", e=first.event)
    with pytest.raises(StaleProgressEvidence) as info:
        _acquire(Sessions, state, progress)
    assert isinstance(info.value.__cause__, StaleInferenceChain)


def test_pg_duplicate_positive_ref_is_refused_where_6_1a_still_loads(Sessions, engine):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    context, _ = p.infer()
    _sql(engine, "INSERT INTO competency_inference_basis_refs (id, inference_run_id, stage_claim_id, ref_role,"
                 " source_kind, source_observation_id, created_at)"
                 " SELECT :new, inference_run_id, stage_claim_id, ref_role, source_kind, source_observation_id,"
                 " created_at FROM competency_inference_basis_refs WHERE inference_run_id = :r"
                 " AND ref_role = 'positive_basis' LIMIT 1", new=uuid.uuid4(), r=context.run_id)
    state, progress = _views(Sessions)  # 6-1A : union identique, snapshot chargé
    with pytest.raises(InvalidProgressEvidenceState, match="en double"):
        _acquire(Sessions, state, progress)


def test_pg_falsified_audit_summaries_change_nothing(Sessions, engine):  # noqa: F811
    from tests.test_inference_engine import Pipeline
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    context, _ = p.infer()
    state, progress = _views(Sessions)
    reference = _acquire(Sessions, state, progress)
    _sql(engine, "UPDATE competency_stage_claims SET basis_summary = 'implied_by_higher_claim:mastery',"
                 " scope_summary = 'localized:C7_D@r1;competency_only' WHERE inference_run_id = :r", r=context.run_id)
    _sql(engine, "UPDATE pedagogical_observations SET evidence_strength = 'weak'")
    assert _acquire(Sessions, state, progress) == reference
