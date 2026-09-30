"""ValidationNeedEngine (T6-C3, 5.4) : besoins LATENTS de confirmation ou de
revalidation, jamais un plan de validation actif.

    InferenceContext + PositiveBasisAssessment (T6-C1) + T6C2Assessment
        -> evaluate_validation_needs -> ValidationNeedAssessment (interne)

Un besoin dit seulement « cette information serait utile si une occasion
pertinente se présente ». Il n'est ni une preuve, ni une condition du stade,
ni une claim, ni une tâche : aucune question, surface, tâche ciblée ou
opportuniste, aucun appel à l'action (l'Étape 6 en décidera). « none » =
aucun besoin (tuple vide). 5.4 n'est jamais obligatoire.

Revalidation (intent revalidation) — toujours le PLUS PETIT périmètre
fragilisé (stade fragilisé, scope_mode, capability_definition_ids), jamais
« toute la compétence » ni « revalider la Mastery » :
- motif unresolved du revision_context retenu par T6-C2 (motif C8_C =>
  revalidation C8_C, jamais C8_A) ;
- tension du current_stage (stade défendable sous tension ; Mastery sous
  tension C8_C => revalidation C8_C, jamais la Mastery entière) ;
- tension materially_incompatible d'une claim supérieure établie qui l'a
  empêchée d'être le current_stage.
Toute autre tension (périphérique, sur un stade inférieur) reste visible,
revision_status unresolved, sans besoin.

Confirmation (intent confirmation, policy V1 conservatrice figée dans
core/inference_final_policies.py) : seulement sur le current_stage retenu
(jamais les claims inférieures), Discovery / Comprehension / Application
(jamais Mastery : conclusion longitudinale), compatible (une claim sous
tension relève de la revalidation), et seulement pour un motif V1 lu sur les
fact_codes STRUCTURÉS du profil de confiance T6-C2 : épisode représentatif
unique sans indépendance T5, indépendance non établie, base competency_only.
Une capacité jamais observée, l'âge d'une preuve ou le temps qui passe ne
créent jamais de besoin ; aucune checklist.

Provenance : chaque besoin cite des sources COURANTES (contradictions et
relations du motif / de la tension ; faits de confiance qui déclenchent la
confirmation). Aucune source => MissingValidationProvenance (jamais
fabriquée). Deux sources qui justifient le même besoin (identité intent,
target_stage, scope_mode, capability_definition_ids) => un seul besoin,
raisons et provenance réunies. Ordre canonique : intent, stade, scope_mode,
capacités (ordre naturel de la taxonomie).

Scopes (vocabulaire T6-B des tensions, VALIDATION_SCOPE_MODES) : localized
(capability_definition_ids non vides, TOUS présents exactement dans la
taxonomie courante : même primitive que les memberships des tensions, aucune
correspondance par code), competency_only et whole_competency (aucune
capacité). Le scope d'un motif / d'une tension est conservé tel quel,
jamais converti ; une confirmation n'est jamais whole_competency. Toute
incohérence => InvalidValidationNeedScope ou UnresolvableCapabilityMembership,
avant tout ordre ou assemblage.
"""
from dataclasses import dataclass

from core.inference_confidence import DEPENDENCY, sorted_ids, sorted_refs
from core.inference_final_policies import (
    CONFIRMATION,
    CURRENT_STAGE_UNDER_TENSION,
    MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM,
    REVALIDATION,
    UNRESOLVED_REVISION_MOTIF,
    VALIDATION_INTENTS,
    VALIDATION_REASON_CODES,
    VALIDATION_SCOPE_MODES,
    MissingValidationProvenance,
    check_inference_assessments,
    check_validation_scope,
    resolve_final_inference_policy,
)
from core.inference_positive_basis import REVALIDATION as REVALIDATION_RELATION
from core.inference_positive_basis import TRANSFER
from core.inference_service import CLAIM_STAGES, COMPETENCY_ONLY, LOCALIZED, STAGE_SEQUENCE
from core.inference_state_policies import COMPATIBLE, MATERIALLY_INCOMPATIBLE

REVALIDATION_NEEDED = "revalidation_needed"
UNRESOLVED_STATUS = "unresolved"


@dataclass(frozen=True, kw_only=True)
class ValidationNeedAssessment:
    """Besoin latent. observation_ids / structural_refs : provenance
    courante (refs validation run-level), jamais sérialisée dans le
    payload. Aucun score, aucune priorité, aucune tâche."""
    schema_version: str
    intent: str
    target_stage: str
    scope_mode: str
    capability_definition_ids: tuple
    reason_codes: tuple
    observation_ids: tuple
    structural_refs: tuple

    def to_payload(self) -> dict:
        """Format canonique validation-need-v1 de InferenceDecision.validation_needs."""
        return {
            "schema_version": self.schema_version,
            "intent": self.intent,
            "target_stage": self.target_stage,
            "scope_mode": self.scope_mode,
            "capability_definition_ids": [str(i) for i in self.capability_definition_ids],
            "reason_codes": list(self.reason_codes),
        }


def _above(first: str, second: str) -> bool:
    return STAGE_SEQUENCE.index(first) > STAGE_SEQUENCE.index(second)


def _candidate(intent, stage, mode, caps, reason, observations, refs) -> tuple:
    return (intent, stage, mode, tuple(caps)), reason, tuple(observations), tuple(refs)


def _revalidations(state) -> list:
    candidates = []
    tensions = {(t.fragilized_stage, t.scope_mode, t.capability_definition_ids): t for t in state.tensions}
    if state.revision_context is not None:
        for motif in state.revision_context.motifs:
            identity = (motif.fragilized_stage, motif.scope_mode, motif.capability_definition_ids)
            tension = tensions.get(identity)
            candidates.append(_candidate(REVALIDATION, *identity, UNRESOLVED_REVISION_MOTIF,
                                         motif.source_contradiction_observation_ids,
                                         () if tension is None else tension.structural_refs))
    compatibilities = {c.stage: c for c in state.claim_compatibilities if c is not None}
    for tension in state.tensions:
        identity = (tension.fragilized_stage, tension.scope_mode, tension.capability_definition_ids)
        if tension.fragilized_stage == state.current_stage:
            reason = CURRENT_STAGE_UNDER_TENSION
        elif tension.compatibility_effect == MATERIALLY_INCOMPATIBLE and \
                _above(tension.fragilized_stage, state.current_stage) and \
                tension.fragilized_stage in compatibilities and \
                compatibilities[tension.fragilized_stage].status == MATERIALLY_INCOMPATIBLE:
            reason = MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM
        else:
            continue
        candidates.append(_candidate(REVALIDATION, *identity, reason, tension.contradiction_observation_ids,
                                     tension.structural_refs))
    return candidates


def _confirmations(positive_basis, state, rules) -> list:
    current = state.current_stage
    if current not in rules.confirmation_stages:
        return []
    index = CLAIM_STAGES.index(current)
    profile, compatibility = state.confidence_profiles[index], state.claim_compatibilities[index]
    if profile is None or compatibility is None or compatibility.status != COMPATIBLE:
        return []
    claim = positive_basis.claims[index]
    dimensions = {d.dimension: d for d in (profile.diagnosticity, profile.coverage, profile.independence,
                                           profile.consistency, profile.temporal_validation)}
    caps = claim.represented_capability_definition_ids
    candidates = []
    for rule in rules.confirmation_motifs:
        triggering = [f for f in dimensions[rule.dimension].facts if f.code in rule.any_of]
        if not triggering or any(code in dimensions[name].fact_codes for name, code in rule.none_of):
            continue
        required = [f for name, code in rule.all_of for f in dimensions[name].facts if f.code == code]
        if {code for _, code in rule.all_of} - {f.code for f in required}:
            continue
        facts = [*triggering, *required]
        candidates.append(_candidate(CONFIRMATION, current, LOCALIZED if caps else COMPETENCY_ONLY, caps,
                                     rule.reason_code, [i for f in facts for i in f.observation_ids],
                                     [r for f in facts for r in f.structural_refs]))
    return candidates


def _order_key(policy):
    known = policy.state.positive_basis.definition_ids

    def key(need):
        caps = tuple((d not in known, known.index(d) if d in known else str(d))
                     for d in need.capability_definition_ids)
        return (VALIDATION_INTENTS.index(need.intent), CLAIM_STAGES.index(need.target_stage),
                VALIDATION_SCOPE_MODES.index(need.scope_mode), caps)
    return key


def evaluate_validation_needs(context, positive_basis, state) -> tuple:
    """Besoins latents de validation (fonction pure). Voir la docstring du
    module. Retourne un tuple (vide = aucun besoin) dans l'ordre canonique."""
    policy = resolve_final_inference_policy(context)
    check_inference_assessments(context, positive_basis, state)
    dossier = context.longitudinal_dossier
    present = frozenset(o.observation_id for o in dossier.active_history.observations)
    relations = frozenset({*((DEPENDENCY, r.relation_id) for r in dossier.dependency_profile.dependencies),
                           *((TRANSFER, r.relation_id) for r in dossier.transfer_profile.transfers),
                           *((REVALIDATION_RELATION, r.relation_id)
                             for r in dossier.consistency_profile.historical_revalidations)})
    merged = {}
    for identity, reason, observations, refs in [*_revalidations(state),
                                                 *_confirmations(positive_basis, state, policy.rules)]:
        reasons, sources, links = merged.setdefault(identity, (set(), set(), set()))
        reasons.add(reason)
        sources.update(i for i in observations if i in present)
        links.update(r for r in refs if (r.relation_kind, r.relation_id) in relations)
    needs = []
    for (intent, stage, mode, caps), (reasons, sources, links) in merged.items():
        # Scope et taxonomie courante vérifiés AVANT tout ordre / assemblage.
        check_validation_scope(mode, caps, context.current_taxonomy_context)
        if not sources and not links:
            raise MissingValidationProvenance(f"besoin {intent} {stage} {mode} sans aucune source courante légale")
        needs.append(ValidationNeedAssessment(
            schema_version=policy.rules.validation_need_schema_version, intent=intent, target_stage=stage,
            scope_mode=mode, capability_definition_ids=caps,
            reason_codes=tuple(code for code in VALIDATION_REASON_CODES if code in reasons),
            observation_ids=sorted_ids(sources), structural_refs=sorted_refs(links)))
    return tuple(sorted(needs, key=_order_key(policy)))


def tension_revision_status(tension, needs) -> str:
    """revision_status final d'une TensionAssessment : revalidation_needed
    si un besoin de revalidation couvre SÉMANTIQUEMENT la tension (même
    stade fragilisé, même scope_mode, mêmes capability_definition_ids),
    sinon unresolved. Jamais resolved / passed / failed / confirmed."""
    identity = (tension.fragilized_stage, tension.scope_mode, tension.capability_definition_ids)
    if any(n.intent == REVALIDATION and (n.target_stage, n.scope_mode, n.capability_definition_ids) == identity
           for n in needs):
        return REVALIDATION_NEEDED
    return UNRESOLVED_STATUS
