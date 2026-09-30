"""Confidence Profile Engine (T6-C2) : solidité DESCRIPTIVE de chaque
prétention positive établie par T6-C1.

    InferenceContext + PositiveBasisAssessment
        -> ClaimContextProjector (ClaimEvidenceContext)
        -> ContradictionProjector + lecture par claim (ContradictionClaimReading)
        -> ConfidenceProfileEngine (ConfidenceProfileAssessment)

Ce module est interne à T6-C2 : l'unique point d'entrée public est
core.inference_state.evaluate_inference_state. Il ne décide ni stade, ni
tension, ni compatibilité (core/inference_state.py), ni rien de T6-C3.

Doctrine :

- Une claim not_established n'a AUCUN profil (None), jamais « low ». Chaque
  claim established a exactement cinq dimensions SÉPARÉES (diagnosticity,
  coverage, independence, consistency, temporal_validation), chacune un
  ensemble de faits fermés et descriptifs avec leur provenance
  (observations, relations T5, capacités). Aucun score, niveau, ratio,
  compte, poids, moyenne ni agrégation entre dimensions ; aucun fait ne
  modifie la claim : coverage et independence décrivent le périmètre d'une
  base déjà établie par T6-C1 et ne peuvent jamais la rouvrir.
- Projection des claims (ClaimContextProjector) : une claim directe porte sa
  base T6-C1 ; une claim implied_by_higher_claim remonte à la claim directe
  désignée par implied_from_stage et réutilise son périmètre représentatif
  pour l'analyse (effective_basis_*), sans jamais devenir une seconde base
  positive : aucune basis_observation_ids nouvelle, T6-C1 inchangé. Son
  profil de confiance reste explicable : ses faits citent les observations
  et relations de la base supérieure qui les expliquent (rôle confidence,
  distinct de positive_basis : un même observation_id peut être base
  positive de la claim directe et provenance de confiance de la claim
  implied, sans devenir une seconde preuve).
- diagnosticity ne recalcule jamais T3 (support_level, residual work,
  evidence_strength, elicitation_mode, local_stage) : elle projette la
  nature de la base établie par T6-C1 (representativeness_reason).
  evidence_strength reste une propriété descriptive des observations.
- independence lit les classifications T5 des périmètres de la base
  (dependent, partially_dependent, independence_evidence, not_established) :
  absence de dépendance != indépendance.
- consistency est la SEULE dimension qui lit les contradictions ; elles ne
  modifient jamais rétroactivement les quatre autres (calculées sans elles).
  no_observed_current_tension signifie seulement qu'aucune tension
  pertinente n'est observée, jamais une « consistency élevée ». Une
  contradiction historiquement revalidée reste visible, sans être ouverte.
- temporal_validation ne lit que les faits T5 (épisodes distincts,
  remobilisations indépendantes, durability evidence, disponibilité d'un
  horodatage de démonstration) : aucun horodatage technique, aucune
  fraîcheur, aucun âge, aucune durée ; le temps qui passe ne change rien.
- Lecture d'une contradiction par claim (partagée avec le moteur de
  tension, une seule source) : le niveau T3 (contradiction_scope) dit quels
  stades elle PEUT concerner ; puis le scope sémantique (SemanticPattern de
  positive_basis-1 portés par la claim, capacités représentées),
  l'attribution (dépendances T5 du périmètre contradictoire) et les
  revalidations T5 décident : périphérique, pertinente (tension) ou
  représentative (incompatibilité matérielle possible). La représentativité
  est lue au niveau cognitif de LA claim testée (policy de son propre
  stade ; Mastery : Application), jamais à celui de la claim source d'une
  claim implied. Une contradiction
  competency_only reste competency_only (aucune capacité inventée) : elle
  n'est comparable qu'au niveau de la compétence ; une contradiction
  localisée n'est pas comparable à une claim purement competency_only.
"""
import uuid
from dataclasses import dataclass

from core.inference_policies import InconsistentInferenceContext
from core.inference_positive_basis import (
    COMPETENCY_ONLY,
    COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION,
    COMPETENCY_UNIT,
    CONTRADICTORY,
    DEPENDENT,
    EVIDENCE_STRENGTHS,
    INDEPENDENCE_EVIDENCE,
    LOCALIZED,
    LONGITUDINAL_MASTERY_HISTORY,
    PARTIALLY_DEPENDENT,
    REVALIDATION,
    TRANSFER,
    PositiveBasisAssessment,
    StructuralRef,
)
from core.inference_positive_basis import COMPLEMENTARY_REPRESENTATIVE_HISTORY as T6C1_COMPLEMENTARY
from core.inference_positive_basis import SINGLE_REPRESENTATIVE_DEMONSTRATION as T6C1_SINGLE_DEMONSTRATION
from core.inference_positive_basis import SINGLE_REPRESENTATIVE_EPISODE as T6C1_SINGLE_EPISODE
from core.inference_service import CLAIM_STAGES, DIRECT, ESTABLISHED, IMPLIED_BY_HIGHER_CLAIM
from core.inference_state_policies import (
    ADDITIONAL_POSITIVE_SCOPE_PRESENT,
    CLAIM_RELEVANT_TENSION,
    CLAIM_REPRESENTATIVE_INCOMPATIBILITY,
    COMPETENCY_ONLY_REPRESENTATIVE_BASIS,
    COMPETENCY_ONLY_SCOPE,
    COMPLEMENTARY_REPRESENTATIVE_HISTORY,
    CONFIDENCE_PROFILE_SCHEMA_VERSION,
    CONSISTENCY,
    CONTRADICTION_OUTSIDE_CLAIM_SCOPE_PRESENT,
    CONTRADICTION_OUTSIDE_CLAIM_STAGE,
    CONTRADICTION_SCOPES,
    COVERAGE,
    COVERAGE_CONCENTRATED_ON_CLAIM_SCOPE,
    DEPENDENCY_PRESENT,
    DIAGNOSTICITY,
    DIMENSION_FACT_CODES,
    DIRECT_REPRESENTATIVE_BASIS,
    DISTINCT_POSITIVE_EPISODES_PRESENT,
    DURABILITY_EVIDENCE_PRESENT,
    EXACT_DEMONSTRATION_TIME_AVAILABLE,
    EXACT_DEMONSTRATION_TIME_UNAVAILABLE,
    HISTORICALLY_REVALIDATED_CONTRADICTION_PRESENT,
    HISTORICALLY_REVALIDATED_ONLY,
    IMPLIED_FROM_HIGHER_CLAIM,
    INDEPENDENCE,
    INDEPENDENCE_EVIDENCE_PRESENT,
    INDEPENDENCE_NOT_ESTABLISHED,
    INDEPENDENT_REMOBILIZATION_PRESENT,
    LOCALIZED_PERIPHERAL,
    LOCALIZED_REPRESENTATIVE_SCOPE,
    LONGITUDINAL_MASTERY_BASIS,
    MIXED_CONSISTENCY_HISTORY,
    MIXED_DEPENDENCY_STRUCTURE,
    NO_OBSERVED_CURRENT_TENSION,
    OPEN_COMPARABLE_CONTRADICTION_PRESENT,
    PARTIAL_DEPENDENCY_PRESENT,
    SINGLE_EPISODE_ONLY,
    SINGLE_REPRESENTATIVE_DEMONSTRATION,
    SINGLE_REPRESENTATIVE_EPISODE,
    STRUCTURAL_RECURRENCE_CANDIDATE_PRESENT,
    TEMPORAL_VALIDATION,
    UNOBSERVED_CAPABILITIES_PRESENT,
    InconsistentPositiveBasis,
    ResolvedStatePolicy,
)

# Vocabulaires T5-C (lus seulement ; recopiés comme en T6-C1 pour ne jamais
# importer la vue, qui porte l'accès à la base).
DEPENDENCY = "dependency"
NOT_ESTABLISHED_READING = "not_established"
SCOPE_CLASSIFICATIONS = (DEPENDENT, PARTIALLY_DEPENDENT, INDEPENDENCE_EVIDENCE, NOT_ESTABLISHED_READING)
DEPENDENCY_LIMITING = frozenset({DEPENDENT, PARTIALLY_DEPENDENT})
HISTORICALLY_REVALIDATED = "historically_revalidated"
TENSION_READING_STATUSES = frozenset({"open_historical_tension", HISTORICALLY_REVALIDATED,
                                      "historical_contradiction_without_comparable_positive"})
OBSERVED_POSITIVE = "observed_positive"
# Limitations T5-C héritées, par dimension (limites de représentation,
# jamais des faiblesses utilisateur).
DOSSIER_LIMITATIONS = {
    COVERAGE: ("cross_release_capability_mappings_not_retained",),
    INDEPENDENCE: ("relation_scope_not_localized_on_capabilities",),
    CONSISTENCY: ("semantic_contradiction_motif_not_persisted",),
}

# Nature de la base T6-C1 (representativeness_reason) -> fait diagnosticity.
BASIS_KIND_FACTS = {
    T6C1_SINGLE_DEMONSTRATION: SINGLE_REPRESENTATIVE_DEMONSTRATION,
    T6C1_SINGLE_EPISODE: SINGLE_REPRESENTATIVE_EPISODE,
    T6C1_COMPLEMENTARY: COMPLEMENTARY_REPRESENTATIVE_HISTORY,
    COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION: COMPETENCY_ONLY_REPRESENTATIVE_BASIS,
    LONGITUDINAL_MASTERY_HISTORY: LONGITUDINAL_MASTERY_BASIS,
}


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ClaimEvidenceContext:
    """Projection interne d'une claim ESTABLISHED de T6-C1. Pour une claim
    implied_by_higher_claim, effective_* désignent la claim directe source
    (implied_from_stage) : périmètre d'analyse, jamais une preuve recopiée.
    competency_only : périmètre purement competency_only (aucune capacité
    représentée)."""
    stage: str
    basis_mode: str
    implied_from_stage: str | None
    effective_basis_stage: str
    effective_basis_kind: str
    effective_basis_observation_ids: tuple
    effective_structural_refs: tuple
    represented_capability_definition_ids: tuple
    competency_only_observation_ids: tuple
    competency_only: bool
    representative_pattern_names: tuple
    positive_basis_limitations: tuple


@dataclass(frozen=True, kw_only=True)
class ContradictionEvidence:
    """Une contradiction du dossier T5 courant, projetée sur ses seules
    propriétés structurées (jamais observation_text). units : capacités
    (ordre naturel) ou (COMPETENCY_UNIT,) ; open / revalidated : lecture T5
    (tension_readings) ; dependency_limited / independent : lecture T5
    (dependency_profile) du périmètre contradictoire."""
    observation_id: uuid.UUID
    event_id: uuid.UUID
    contradiction_scope: str
    error_type: str | None
    evidence_strength: str
    capability_localization: str
    units: tuple
    open_units: frozenset
    revalidated_units: frozenset
    dependency_limited_units: frozenset
    independent_units: frozenset
    revalidation_refs: tuple
    dependency_refs: tuple
    recurrence_candidate: bool


@dataclass(frozen=True, kw_only=True)
class ContradictionClaimReading:
    """Lecture d'UNE contradiction par rapport à UNE claim established.
    relevant_units : plus petit périmètre ouvert de la contradiction qui
    touche le périmètre affirmé ; revalidated_units : périmètre pertinent
    historiquement revalidé. representative_scope : la contradiction
    attribuable satisfait chaque relation représentative de la claim et
    touche ses capacités représentées ; exceptionally_diagnostic : lecture
    diagnostique locale de T3 exigée d'une observation unique (jamais
    suffisante seule)."""
    stage: str
    contradiction: ContradictionEvidence
    reading: str
    relevant_units: tuple
    revalidated_units: tuple
    representative_scope: bool
    exceptionally_diagnostic: bool
    material_scope: bool
    dependency_linked: bool


@dataclass(frozen=True, kw_only=True)
class ConfidenceFact:
    """Un fait descriptif fermé et sa provenance."""
    code: str
    observation_ids: tuple = ()
    structural_refs: tuple = ()
    capability_definition_ids: tuple = ()


@dataclass(frozen=True, kw_only=True)
class ConfidenceDimensionAssessment:
    """Une dimension : faits (ordre de déclaration), provenance réunie
    (observations, relations T5, capacités) et limitations amont héritées
    (T6-C1 / T5-C). Aucun niveau, aucune note."""
    dimension: str
    fact_codes: tuple
    observation_ids: tuple
    structural_refs: tuple
    capability_definition_ids: tuple
    limitations: tuple
    facts: tuple


@dataclass(frozen=True, kw_only=True)
class ConfidenceProfileAssessment:
    """Cinq dimensions séparées d'une claim established ; jamais agrégées."""
    schema_version: str
    stage: str
    diagnosticity: ConfidenceDimensionAssessment
    coverage: ConfidenceDimensionAssessment
    independence: ConfidenceDimensionAssessment
    consistency: ConfidenceDimensionAssessment
    temporal_validation: ConfidenceDimensionAssessment


# --------------------------------------------------------------------------
# Utilitaires canoniques
# --------------------------------------------------------------------------

def sorted_ids(ids) -> tuple:
    return tuple(sorted(frozenset(ids), key=str))


def sorted_refs(refs) -> tuple:
    return tuple(sorted(frozenset(refs), key=lambda ref: (ref.relation_kind, str(ref.relation_id))))


def single(values) -> bool:
    """Exactement une valeur distincte (sans compter)."""
    items = frozenset(values)
    return bool(items) and not any(a != b for a in items for b in items)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InconsistentInferenceContext(message)


# --------------------------------------------------------------------------
# ClaimContextProjector
# --------------------------------------------------------------------------

def project_claim_contexts(positive_basis: PositiveBasisAssessment) -> dict:
    """{stage: ClaimEvidenceContext} des claims established (ordre
    discovery -> mastery). Une claim implied remonte à sa claim directe
    source via implied_from_stage (qui doit être established et direct)."""
    claims = {c.stage: c for c in positive_basis.claims}
    contexts = {}
    for stage in CLAIM_STAGES:
        claim = claims[stage]
        if claim.status != ESTABLISHED:
            continue
        source = claim
        if claim.basis_mode == IMPLIED_BY_HIGHER_CLAIM:
            source = claims.get(claim.implied_from_stage)
            if source is None or source.status != ESTABLISHED or source.basis_mode != DIRECT or \
                    CLAIM_STAGES.index(source.stage) <= CLAIM_STAGES.index(stage):
                raise InconsistentPositiveBasis(f"{stage} implied depuis {claim.implied_from_stage!r} : aucune claim"
                                                " directe supérieure établie")
        elif claim.basis_mode != DIRECT or claim.implied_from_stage is not None:
            raise InconsistentPositiveBasis(f"{stage} established : basis_mode {claim.basis_mode!r} invalide")
        if source.representativeness_reason not in BASIS_KIND_FACTS or not source.basis_observation_ids:
            raise InconsistentPositiveBasis(f"{stage} : base directe {source.representativeness_reason!r} invalide")
        contexts[stage] = ClaimEvidenceContext(
            stage=stage,
            basis_mode=claim.basis_mode,
            implied_from_stage=claim.implied_from_stage,
            effective_basis_stage=source.stage,
            effective_basis_kind=source.representativeness_reason,
            effective_basis_observation_ids=source.basis_observation_ids,
            effective_structural_refs=source.structural_basis_refs,
            represented_capability_definition_ids=claim.represented_capability_definition_ids,
            competency_only_observation_ids=claim.competency_only_observation_ids,
            competency_only=bool(claim.competency_only_observation_ids)
            and not claim.represented_capability_definition_ids,
            representative_pattern_names=claim.representative_pattern_names,
            positive_basis_limitations=tuple(sorted(frozenset(item.code for item in source.limitations))),
        )
    return contexts


def claim_patterns(context: ClaimEvidenceContext, policy: ResolvedStatePolicy) -> tuple:
    """SemanticPattern résolus (positive_basis-1) qui portent le périmètre
    affirmé par la claim, lus au niveau cognitif de LA CLAIM TESTÉE : policy
    de représentativité de son propre stade (Mastery : Application).
    Claim directe : les patterns qui l'ont établie. Claim implied : les
    patterns de SON stade que porte le périmètre hérité de la claim source
    (effective_basis_* ne sert qu'à la provenance, jamais à choisir le
    niveau cognitif) ; aucun si ce périmètre n'en porte aucun."""
    stage_policy = policy.positive_basis.stage_policies[policy.rules.pattern_stage[context.stage]]
    if context.basis_mode == IMPLIED_BY_HIGHER_CLAIM:
        return tuple(p for p in stage_policy.representative_patterns
                     if p.satisfied_by(context.represented_capability_definition_ids))
    by_name = {p.name: p for p in stage_policy.representative_patterns}
    missing = [name for name in context.representative_pattern_names if name not in by_name]
    if missing:
        raise InconsistentPositiveBasis(f"{context.stage} : patterns {missing} absents de positive_basis-1")
    return tuple(by_name[name] for name in context.representative_pattern_names)


def claim_units(context: ClaimEvidenceContext) -> frozenset:
    """Périmètre affirmé : capacités représentées, et la compétence entière
    si la claim repose (aussi) sur une preuve competency_only."""
    units = frozenset(context.represented_capability_definition_ids)
    return units | frozenset({COMPETENCY_UNIT}) if context.competency_only_observation_ids else units


# --------------------------------------------------------------------------
# Index du dossier T5 (lecture seule)
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class DossierIndex:
    """Accès déterministe aux faits T5-C dont T6-C2 a besoin."""
    observations: dict
    scope_readings: dict
    relation_kinds: dict


def index_dossier(dossier) -> DossierIndex:
    kinds = {}
    for dependency in dossier.dependency_profile.dependencies:
        kinds[dependency.relation_id] = DEPENDENCY
    for transfer in dossier.transfer_profile.transfers:
        kinds[transfer.relation_id] = TRANSFER
    for revalidation in dossier.consistency_profile.historical_revalidations:
        kinds[revalidation.relation_id] = REVALIDATION
    readings = {}
    for reading in dossier.dependency_profile.scope_readings:
        _require(reading.classification in SCOPE_CLASSIFICATIONS,
                 f"classification T5 {reading.classification!r} inconnue")
        unit = COMPETENCY_UNIT if reading.unit.capability is None else reading.unit.capability.definition_id
        readings[(reading.observation_id, unit)] = reading
    return DossierIndex(observations={o.observation_id: o for o in dossier.active_history.observations},
                        scope_readings=readings, relation_kinds=kinds)


def relation_refs(ids, index: DossierIndex) -> tuple:
    refs = []
    for relation_id in ids:
        _require(relation_id in index.relation_kinds, f"relation T5 {relation_id} hors du dossier courant")
        refs.append(StructuralRef(relation_kind=index.relation_kinds[relation_id], relation_id=relation_id))
    return sorted_refs(refs)


def observation_units(observation) -> tuple:
    if observation.capability_localization == LOCALIZED:
        return tuple(c.definition_id for c in observation.compatible_capabilities)
    return (COMPETENCY_UNIT,)


# --------------------------------------------------------------------------
# ContradictionProjector + lecture par claim
# --------------------------------------------------------------------------

def project_contradictions(dossier, policy: ResolvedStatePolicy, index: DossierIndex) -> tuple:
    """Contradictions du dossier courant (ordre : str(observation_id))."""
    positive = policy.positive_basis
    known = {c.definition_id: (c.capability_code, c.semantic_revision) for c in positive.capabilities}
    statuses = {}
    for reading in dossier.consistency_profile.tension_readings:
        _require(reading.status in TENSION_READING_STATUSES, f"statut T5 {reading.status!r} inconnu")
        unit = COMPETENCY_UNIT if reading.unit.capability is None else reading.unit.capability.definition_id
        statuses[(reading.contradiction_observation_id, unit)] = reading
    recurrent = frozenset(i for candidate in dossier.consistency_profile.structural_recurrence_candidates
                          for i in candidate.observation_ids)
    projected = []
    for record in dossier.consistency_profile.historical_contradictions:
        observation = index.observations.get(record.observation_id)
        _require(observation is not None and observation.polarity == CONTRADICTORY,
                 f"contradiction {record.observation_id} hors de l'historique actif")
        _require(record.contradiction_scope in CONTRADICTION_SCOPES and record.evidence_strength in EVIDENCE_STRENGTHS,
                 f"contradiction {record.observation_id} : vocabulaire T3 inconnu")
        if record.capability_localization == LOCALIZED:
            _require(bool(record.capabilities), f"contradiction {record.observation_id} localized sans capacité")
            for ref in record.capabilities:
                _require(known.get(ref.definition_id) == (ref.capability_code, ref.semantic_revision),
                         f"contradiction {record.observation_id} : capacité {ref.capability_code} hors de la"
                         " taxonomie résolue")
            units = positive.ordered(ref.definition_id for ref in record.capabilities)
        else:
            _require(record.capability_localization == COMPETENCY_ONLY and not record.capabilities,
                     f"contradiction {record.observation_id} : localisation incohérente")
            units = (COMPETENCY_UNIT,)
        tension = [statuses.get((record.observation_id, unit)) for unit in units]
        scope = [index.scope_readings.get((record.observation_id, unit)) for unit in units]
        _require(all(item is not None for item in (*tension, *scope)),
                 f"contradiction {record.observation_id} : lecture T5 de périmètre absente")
        revalidated = frozenset(u for u, r in zip(units, tension) if r.status == HISTORICALLY_REVALIDATED)
        projected.append(ContradictionEvidence(
            observation_id=record.observation_id,
            event_id=record.event_id,
            contradiction_scope=record.contradiction_scope,
            error_type=record.error_type,
            evidence_strength=record.evidence_strength,
            capability_localization=record.capability_localization,
            units=units,
            open_units=frozenset(units) - revalidated,
            revalidated_units=revalidated,
            dependency_limited_units=frozenset(u for u, r in zip(units, scope)
                                               if r.classification in DEPENDENCY_LIMITING),
            independent_units=frozenset(u for u, r in zip(units, scope) if r.classification == INDEPENDENCE_EVIDENCE),
            revalidation_refs=relation_refs((i for r in tension for i in r.revalidation_ids), index),
            dependency_refs=relation_refs((i for r in scope for i in r.dependency_relation_ids), index),
            recurrence_candidate=record.observation_id in recurrent,
        ))
    return tuple(sorted(projected, key=lambda c: str(c.observation_id)))


def represents(units: frozenset, context: ClaimEvidenceContext, patterns: tuple) -> bool:
    """Le périmètre (contradictoire) satisfait CHAQUE relation représentative
    portée par la claim et touche ses capacités représentées ; claim
    purement competency_only : comparaison au seul niveau de la compétence."""
    if context.competency_only:
        return COMPETENCY_UNIT in units
    return bool(patterns) and all(p.satisfied_by(units) for p in patterns) and \
        not units.isdisjoint(context.represented_capability_definition_ids)


def read_contradiction(context: ClaimEvidenceContext, contradiction: ContradictionEvidence,
                       policy: ResolvedStatePolicy) -> ContradictionClaimReading:
    """Lecture qualitative (voir la docstring du module). Aucune règle de
    comptage : une contradiction est lue pour elle-même."""
    rules = policy.rules
    patterns = claim_patterns(context, policy)
    if contradiction.capability_localization == COMPETENCY_ONLY:
        touched = frozenset({COMPETENCY_UNIT})
    elif context.competency_only:
        touched = frozenset()
    else:
        dimensions = frozenset(context.represented_capability_definition_ids).union(
            *(p.core_definition_ids | p.related_definition_ids for p in patterns))
        touched = frozenset(contradiction.units) & dimensions
    relevant = touched & contradiction.open_units
    attributable = contradiction.open_units - contradiction.dependency_limited_units
    representative = bool(relevant) and represents(attributable, context, patterns)
    diagnostic = contradiction.evidence_strength in rules.exceptional_diagnostic_strengths
    material = contradiction.contradiction_scope in rules.material_contradiction_scopes
    if context.stage not in rules.contradiction_scope_reach[contradiction.contradiction_scope]:
        reading = CONTRADICTION_OUTSIDE_CLAIM_STAGE
    elif not touched:
        reading = LOCALIZED_PERIPHERAL
    elif not relevant:
        reading = HISTORICALLY_REVALIDATED_ONLY
    elif representative and diagnostic and material:
        reading = CLAIM_REPRESENTATIVE_INCOMPATIBILITY
    else:
        reading = CLAIM_RELEVANT_TENSION
    reached = reading not in (CONTRADICTION_OUTSIDE_CLAIM_STAGE, LOCALIZED_PERIPHERAL)
    ordered = policy.positive_basis.ordered
    return ContradictionClaimReading(
        stage=context.stage,
        contradiction=contradiction,
        reading=reading,
        relevant_units=_units_tuple(relevant, ordered) if reached else (),
        revalidated_units=_units_tuple(touched & contradiction.revalidated_units, ordered) if reached else (),
        representative_scope=representative,
        exceptionally_diagnostic=diagnostic,
        material_scope=material,
        dependency_linked=not relevant.isdisjoint(contradiction.dependency_limited_units),
    )


def _units_tuple(units, ordered) -> tuple:
    """Capacités dans l'ordre naturel, puis la compétence entière."""
    return (*ordered(units), *((COMPETENCY_UNIT,) if COMPETENCY_UNIT in units else ()))


def read_contradictions(contexts: dict, contradictions: tuple, policy: ResolvedStatePolicy) -> dict:
    """{stage: lectures de toutes les contradictions} pour chaque claim
    established : unique lecture partagée par consistency et le moteur de
    tension."""
    return {stage: tuple(read_contradiction(context, c, policy) for c in contradictions)
            for stage, context in contexts.items()}


# --------------------------------------------------------------------------
# ConfidenceProfileEngine
# --------------------------------------------------------------------------

def _dimension(name: str, facts, limitations, policy: ResolvedStatePolicy) -> ConfidenceDimensionAssessment:
    declared = DIMENSION_FACT_CODES[name]
    facts = sorted(facts, key=lambda fact: declared.index(fact.code))
    return ConfidenceDimensionAssessment(
        dimension=name,
        fact_codes=tuple(fact.code for fact in facts),
        observation_ids=sorted_ids(i for fact in facts for i in fact.observation_ids),
        structural_refs=sorted_refs(r for fact in facts for r in fact.structural_refs),
        capability_definition_ids=policy.positive_basis.ordered(i for fact in facts
                                                                for i in fact.capability_definition_ids),
        limitations=tuple(sorted(frozenset(limitations))),
        facts=tuple(facts),
    )


def _fact(code, observation_ids=(), structural_refs=(), capability_definition_ids=(), *, policy) -> ConfidenceFact:
    """Fait canonique et sa provenance DESCRIPTIVE. Sur une claim implied,
    les observations / relations de la base directe supérieure qui
    expliquent le fait restent citées : c'est une provenance de confiance
    (rôle confidence), jamais une seconde base positive (positive_basis
    reste portée par la seule claim directe, T6-C1 inchangé)."""
    return ConfidenceFact(
        code=code,
        observation_ids=sorted_ids(observation_ids),
        structural_refs=sorted_refs(structural_refs),
        capability_definition_ids=policy.positive_basis.ordered(capability_definition_ids),
    )


def _inherited(dimension: str, dossier) -> tuple:
    present = frozenset(item.code for item in dossier.limitations)
    return tuple(code for code in DOSSIER_LIMITATIONS.get(dimension, ()) if code in present)


def _diagnosticity(context, policy):
    fact = lambda code: _fact(code, context.effective_basis_observation_ids,  # noqa: E731
                              context.effective_structural_refs, context.represented_capability_definition_ids,
                              policy=policy)
    implied = context.basis_mode == IMPLIED_BY_HIGHER_CLAIM
    facts = [fact(IMPLIED_FROM_HIGHER_CLAIM if implied else DIRECT_REPRESENTATIVE_BASIS),
             fact(BASIS_KIND_FACTS[context.effective_basis_kind])]
    return _dimension(DIAGNOSTICITY, facts, context.positive_basis_limitations, policy)


def _coverage(context, dossier, policy):
    represented = frozenset(context.represented_capability_definition_ids)
    observed = [entry.capability.definition_id for entry in dossier.coverage_profile.capabilities
                if entry.positive_observation_status == OBSERVED_POSITIVE]
    unobserved = [entry.capability.definition_id for entry in dossier.coverage_profile.capabilities
                  if entry.positive_observation_status != OBSERVED_POSITIVE]
    additional = [d for d in policy.positive_basis.ordered(observed) if d not in represented]
    facts = []
    if represented:
        facts.append(_fact(LOCALIZED_REPRESENTATIVE_SCOPE, capability_definition_ids=represented, policy=policy))
    if context.competency_only_observation_ids:
        facts.append(_fact(COMPETENCY_ONLY_SCOPE, context.competency_only_observation_ids, policy=policy))
    facts.append(_fact(ADDITIONAL_POSITIVE_SCOPE_PRESENT, capability_definition_ids=additional, policy=policy)
                 if additional else _fact(COVERAGE_CONCENTRATED_ON_CLAIM_SCOPE, policy=policy,
                                          capability_definition_ids=represented))
    if policy.positive_basis.ordered(unobserved):
        facts.append(_fact(UNOBSERVED_CAPABILITIES_PRESENT, capability_definition_ids=unobserved, policy=policy))
    return _dimension(COVERAGE, facts, _inherited(COVERAGE, dossier), policy)


def _basis_readings(context, index):
    """Lectures T5 (dependency_profile) des observations de la base sur le
    périmètre affirmé de la claim."""
    units = claim_units(context)
    readings = []
    for observation_id in context.effective_basis_observation_ids:
        observation = index.observations.get(observation_id)
        _require(observation is not None, f"base {observation_id} hors du dossier courant")
        for unit in observation_units(observation):
            if unit in units:
                reading = index.scope_readings.get((observation_id, unit))
                _require(reading is not None, f"observation {observation_id} : lecture T5 de périmètre absente")
                readings.append((unit, reading))
    return readings


def _remobilizations(context, dossier):
    """Remobilisations indépendantes T5 (transfert / revalidation) dont la
    cible appartient à la base de la claim."""
    basis = frozenset(context.effective_basis_observation_ids)
    return [r for r in dossier.temporal_validation_profile.evidence_of_independent_remobilization
            if r.target_observation_id in basis]


def _remobilization_fact(code, context, relations, policy):
    basis = frozenset(context.effective_basis_observation_ids)
    return _fact(code, [i for r in relations for i in (r.source_observation_id, r.target_observation_id)
                        if i in basis],
                 [StructuralRef(relation_kind=r.relation_kind, relation_id=r.relation_id) for r in relations],
                 [c.definition_id for r in relations for c in r.scope.capabilities], policy=policy)


def _independence(context, dossier, index, policy):
    readings = _basis_readings(context, index)
    by_class = {c: [(u, r) for u, r in readings if r.classification == c] for c in SCOPE_CLASSIFICATIONS}

    def reading_fact(code, classification, relation_ids):
        items = by_class[classification]
        return _fact(code, [r.observation_id for _, r in items], relation_refs(
            (i for _, r in items for i in relation_ids(r)), index), [u for u, _ in items], policy=policy)

    facts = []
    if single(index.observations[i].event_id for i in context.effective_basis_observation_ids):
        facts.append(_fact(SINGLE_EPISODE_ONLY, context.effective_basis_observation_ids, policy=policy))
    if by_class[INDEPENDENCE_EVIDENCE]:
        facts.append(reading_fact(INDEPENDENCE_EVIDENCE_PRESENT, INDEPENDENCE_EVIDENCE,
                                  lambda r: r.independence_evidence_relation_ids))
    relations = _remobilizations(context, dossier)
    if relations:
        facts.append(_remobilization_fact(INDEPENDENT_REMOBILIZATION_PRESENT, context, relations, policy))
    if by_class[DEPENDENT]:
        facts.append(reading_fact(DEPENDENCY_PRESENT, DEPENDENT, lambda r: r.dependency_relation_ids))
    if by_class[PARTIALLY_DEPENDENT]:
        facts.append(reading_fact(PARTIAL_DEPENDENCY_PRESENT, PARTIALLY_DEPENDENT, lambda r: r.dependency_relation_ids))
    if by_class[NOT_ESTABLISHED_READING]:
        facts.append(reading_fact(INDEPENDENCE_NOT_ESTABLISHED, NOT_ESTABLISHED_READING, lambda r: ()))
    present = [c for c in SCOPE_CLASSIFICATIONS if by_class[c]]
    if present and not single(present):
        facts.append(_fact(MIXED_DEPENDENCY_STRUCTURE, policy=policy))
    return _dimension(INDEPENDENCE, facts, _inherited(INDEPENDENCE, dossier), policy)


def _consistency(readings, dossier, policy):
    open_ = [r for r in readings if r.reading in (CLAIM_RELEVANT_TENSION, CLAIM_REPRESENTATIVE_INCOMPATIBILITY)]
    revalidated = [r for r in readings if r.revalidated_units]
    outside = [r for r in readings if r.reading in (CONTRADICTION_OUTSIDE_CLAIM_STAGE, LOCALIZED_PERIPHERAL)
               and r.contradiction.open_units]
    facts = []
    if open_:
        facts.append(_fact(OPEN_COMPARABLE_CONTRADICTION_PRESENT, [r.contradiction.observation_id for r in open_],
                           [ref for r in open_ for ref in r.contradiction.dependency_refs],
                           [u for r in open_ for u in r.relevant_units], policy=policy))
    else:
        facts.append(_fact(NO_OBSERVED_CURRENT_TENSION, policy=policy))
    if revalidated:
        facts.append(_fact(HISTORICALLY_REVALIDATED_CONTRADICTION_PRESENT,
                           [r.contradiction.observation_id for r in revalidated],
                           [ref for r in revalidated for ref in r.contradiction.revalidation_refs],
                           [u for r in revalidated for u in r.revalidated_units], policy=policy))
    open_ids = frozenset(r.contradiction.observation_id for r in open_)
    candidates = [c for c in dossier.consistency_profile.structural_recurrence_candidates
                  if not open_ids.isdisjoint(c.observation_ids)]
    if candidates:
        facts.append(_fact(STRUCTURAL_RECURRENCE_CANDIDATE_PRESENT, [i for c in candidates for i in c.observation_ids],
                           capability_definition_ids=[c.common_scope.capability.definition_id for c in candidates
                                                      if c.common_scope.capability is not None],
                           policy=policy))
    if outside:
        facts.append(_fact(CONTRADICTION_OUTSIDE_CLAIM_SCOPE_PRESENT, [r.contradiction.observation_id for r in outside],
                           capability_definition_ids=[u for r in outside for u in r.contradiction.open_units],
                           policy=policy))
    if open_ and revalidated:
        facts.append(_fact(MIXED_CONSISTENCY_HISTORY, policy=policy))
    limitations = _inherited(CONSISTENCY, dossier) if open_ or revalidated else ()
    return _dimension(CONSISTENCY, facts, limitations, policy)


def _temporal_validation(context, dossier, index, policy):
    profile = dossier.temporal_validation_profile
    basis = context.effective_basis_observation_ids
    facts = [_fact(SINGLE_EPISODE_ONLY if single(index.observations[i].event_id for i in basis)
                   else DISTINCT_POSITIVE_EPISODES_PRESENT, basis, policy=policy)]
    relations = _remobilizations(context, dossier)
    if relations:
        facts.append(_remobilization_fact(INDEPENDENT_REMOBILIZATION_PRESENT, context, relations, policy))
    durable = frozenset((e.relation_kind, e.relation_id) for e in profile.durability_evidence_events)
    lasting = [r for r in relations if (r.relation_kind, r.relation_id) in durable]
    if lasting:
        facts.append(_remobilization_fact(DURABILITY_EVIDENCE_PRESENT, context, lasting, policy))
    facts.append(_fact(EXACT_DEMONSTRATION_TIME_AVAILABLE if profile.exact_demonstration_time_available is True
                       else EXACT_DEMONSTRATION_TIME_UNAVAILABLE, policy=policy))
    return _dimension(TEMPORAL_VALIDATION, facts, (), policy)


def confidence_profile(context: ClaimEvidenceContext, readings: tuple, dossier, index: DossierIndex,
                       policy: ResolvedStatePolicy) -> ConfidenceProfileAssessment:
    """Profil de confiance d'UNE claim established. diagnosticity, coverage,
    independence et temporal_validation ne lisent aucune contradiction."""
    return ConfidenceProfileAssessment(
        schema_version=CONFIDENCE_PROFILE_SCHEMA_VERSION,
        stage=context.stage,
        diagnosticity=_diagnosticity(context, policy),
        coverage=_coverage(context, dossier, policy),
        independence=_independence(context, dossier, index, policy),
        consistency=_consistency(readings, dossier, policy),
        temporal_validation=_temporal_validation(context, dossier, index, policy),
    )
