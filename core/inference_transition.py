"""TransitionCauseResolver (T6-C3) : quelle famille de faits causaux explique
RÉELLEMENT le nouveau snapshot décisionnel.

    InferenceContext + PositiveBasisAssessment (T6-C1) + T6C2Assessment
        -> resolve_transition_cause -> TransitionCauseAssessment (interne)

Vocabulaire : celui de T6-B, et lui seul (new_user_evidence,
evidence_integrity_change, pedagogical_reinterpretation). Première inférence
: None. Avec predecessor : toujours une cause, même pour un maintien (elle
explique le nouveau snapshot, pas seulement une progression).

Faits != décision. TransitionCausalityContext (T6-B.2) dit quels faits
existent dans la fenêtre causale ; plusieurs familles coexistent librement.
T6-C3 ne choisit jamais « la famille présente » (jamais « nouvel événement
=> new_user_evidence ») ni une priorité fixe entre familles (jamais
« intégrité > réinterprétation > nouvelle preuve ») : il cherche, dans un
ordre SÉMANTIQUE de niveaux d'explication, les faits qui expliquent la
décision, et échoue fermé si deux familles l'expliquent également.

    A. Résolution / supersession de motif portée par T6-C2 : pour un
       upgrade, les motifs franchis (le rebound qu'ils autorisent) ; pour un
       maintien, tout motif qui quitte le contexte de révision ; jamais pour
       une révision descendante (relâcher une contrainte ne fait pas
       descendre). resolved_supportively = démonstration d'un événement
       nouveau => new_user_evidence ; superseded : sources invalidées =>
       evidence_integrity_change, seulement réévaluées (aucune correction
       d'intégrité responsable) => pedagogical_reinterpretation.
    B. Sources qui expliquent directement un changement :
       B1 stade — upgrade : base du nouveau stade, sinon tensions bloquantes
          du predecessor dont les sources ont quitté le dossier ; révision :
          ce qui a fait tomber le stade précédent (contradictions devenues
          matérielles, ou base précédente sortie du dossier), sinon (stade
          précédent déjà tenu sous tension) la base du stade retenu ;
       B2 stade sans source attribuable : release / version T5 /
          spécification T6 différente, ou changement SÉMANTIQUE des
          relations T5 (relation_delta added / removed, jamais retained)
          qu'aucune autre famille n'explique => pedagogical_reinterpretation ;
       B3 tensions apparues (sources présentes) ou disparues (sources
          sorties) ; B4 idem B2 pour un changement de tensions.
    C. Sources SUBSTANTIELLES de la décision (positive_basis, mastery,
       tension ; et celles du predecessor sorties du dossier) liées à la
       fenêtre causale.
    D. Maintien sans changement matériel : D1 provenance descriptive
       (confidence, refs run-level du predecessor) ; D2 snapshot (nouvel
       événement présent, intégrité, réévaluation, changement de version).

À chaque niveau : aucune explication => niveau suivant ; une seule famille
=> cause ; plusieurs => AmbiguousTransitionCause (aucune priorité
arbitraire). Aucun niveau => UnattributableTransitionCause.

Attribution d'une source : observation PRÉSENTE (dossier courant) d'un
événement de new_user_event_ids => new_user_evidence ; présente et issue
d'une réévaluation (current_observation_ids) => pedagogical_reinterpretation ;
SORTIE du dossier et invalidée (integrity_changes) => evidence_integrity_change
(l'invalidation suffit à la retirer, même si l'événement a aussi été
réévalué) ; sortie et seulement réévaluée => pedagogical_reinterpretation.
Une relation T5 citée atteint un événement nouveau par ses extrémités.
Relations T5 (relation_delta) : interprétations structurées du dossier,
jamais une preuve utilisateur. Un ajout / retrait dont une extrémité est
une observation d'un événement nouveau, invalidée ou réévaluée appartient à
cette famille ; sinon il est une réinterprétation contrôlée
(t5_relation_change_changed_interpretation), au même rang que les
changements de release / versions : jamais new_user_evidence. Les refs
transition restent les seules sources présentes du dossier courant (aucun
UUID de relation n'est reconstruit depuis le payload sémantique).

Contrats T6-B respectés par construction : new_user_evidence n'est jamais la
cause d'un retour à non_etabli (une contradiction nouvelle ne remet jamais à
zéro : ces explications sont écartées) ; new_user_evidence cite toujours au
moins une source présente atteignant un événement nouveau (refs transition,
qui portent aussi l'anti-oscillation) ; upgrade / révision vers un stade
positif ont toujours au moins une ref transition (sources explicatives
présentes, sinon la base du stade retenu) ; aucune source absente du dossier
courant n'est jamais citée.

Pureté : aucune base, horloge, hasard ni texte relu. Le predecessor
(PredecessorDecisionContext) n'est lu que pour savoir quelles sources
fondaient l'ancienne décision : jamais une preuve.
"""
from dataclasses import dataclass

from core.inference_confidence import DEPENDENCY, sorted_ids, sorted_refs
from core.inference_final_policies import (
    FIRST_INFERENCE,
    INTEGRITY_CHANGE_IN_CAUSAL_WINDOW,
    INTEGRITY_CHANGE_REMOVED_PREVIOUS_BASIS,
    INTEGRITY_CHANGE_REMOVED_PREVIOUS_CONTRADICTION,
    INTEGRITY_CHANGE_REMOVED_PREVIOUS_SOURCE,
    INTEGRITY_CHANGE_SUPERSEDED_REVISION_MOTIF,
    MAINTAINED_AFTER_CONTROLLED_REINTERPRETATION,
    MAINTAINED_AFTER_INTEGRITY_CHANGE,
    MAINTAINED_AFTER_NEW_USER_EVIDENCE,
    NEW_CONTRADICTION_DRIVES_REVISION,
    NEW_USER_EVIDENCE_CHANGES_TENSION,
    NEW_USER_EVIDENCE_IN_CURRENT_SNAPSHOT,
    NEW_USER_EVIDENCE_RESOLVES_REVISION_MOTIF,
    NEW_USER_EVIDENCE_SUPPORTS_CURRENT_DECISION,
    REEVALUATION_CHANGED_INTERPRETATION,
    T5_RELATION_CHANGE_CHANGED_INTERPRETATION,
    T5_VERSION_CHANGE_CHANGED_INTERPRETATION,
    T6_SPECIFICATION_CHANGE_CHANGED_INTERPRETATION,
    TAXONOMY_CHANGE_CHANGED_INTERPRETATION,
    TRANSITION_REASON_CODES,
    AmbiguousTransitionCause,
    InconsistentInferenceAssessment,
    UnattributableTransitionCause,
    check_inference_assessments,
)
from core.inference_positive_basis import REVALIDATION, TRANSFER
from core.inference_service import (
    CLAIM_STAGES,
    CONFIDENCE,
    DIRECT,
    ESTABLISHED,
    EVIDENCE_INTEGRITY_CHANGE,
    IMPLIED_BY_HIGHER_CLAIM,
    MAINTAINED,
    MASTERY_REF,
    NEW_USER_EVIDENCE,
    NON_ETABLI,
    PEDAGOGICAL_REINTERPRETATION,
    POSITIVE_BASIS,
    REVISED_DOWN,
    SOURCE_OBSERVATION,
    STAGE_SEQUENCE,
    TENSION,
    TRANSITION,
    UPGRADED,
    VALIDATION,
)
from core.inference_state_policies import MATERIALLY_INCOMPATIBLE, RESOLVED_SUPPORTIVELY, UNRESOLVED

# Nature de l'explication (interne ; mappée vers TRANSITION_REASON_CODES).
RESOLUTION = "resolution"
SUPERSESSION = "supersession"
BASIS = "basis"
CONTRADICTION = "contradiction"
LOST_BASIS = "lost_basis"
LOST_CONTRADICTION = "lost_contradiction"
CITED = "cited"
LOST_SOURCE = "lost_source"
SNAPSHOT = "snapshot"
TAXONOMY = "taxonomy"
T5_VERSIONS = "t5_versions"
RELATION_DELTA = "relation_delta"
T6_SPECIFICATION = "t6_specification"
SUBSTANTIVE_ROLES = frozenset({POSITIVE_BASIS, MASTERY_REF, TENSION})
DESCRIPTIVE_ROLES = frozenset({CONFIDENCE, TRANSITION, VALIDATION})

_REASONS = {
    (NEW_USER_EVIDENCE, RESOLUTION): NEW_USER_EVIDENCE_RESOLVES_REVISION_MOTIF,
    (NEW_USER_EVIDENCE, BASIS): NEW_USER_EVIDENCE_SUPPORTS_CURRENT_DECISION,
    (NEW_USER_EVIDENCE, CITED): NEW_USER_EVIDENCE_SUPPORTS_CURRENT_DECISION,
    (NEW_USER_EVIDENCE, CONTRADICTION): NEW_USER_EVIDENCE_CHANGES_TENSION,
    (NEW_USER_EVIDENCE, SNAPSHOT): NEW_USER_EVIDENCE_IN_CURRENT_SNAPSHOT,
    (EVIDENCE_INTEGRITY_CHANGE, SUPERSESSION): INTEGRITY_CHANGE_SUPERSEDED_REVISION_MOTIF,
    (EVIDENCE_INTEGRITY_CHANGE, LOST_BASIS): INTEGRITY_CHANGE_REMOVED_PREVIOUS_BASIS,
    (EVIDENCE_INTEGRITY_CHANGE, LOST_CONTRADICTION): INTEGRITY_CHANGE_REMOVED_PREVIOUS_CONTRADICTION,
    (EVIDENCE_INTEGRITY_CHANGE, LOST_SOURCE): INTEGRITY_CHANGE_REMOVED_PREVIOUS_SOURCE,
    (EVIDENCE_INTEGRITY_CHANGE, SNAPSHOT): INTEGRITY_CHANGE_IN_CAUSAL_WINDOW,
    (PEDAGOGICAL_REINTERPRETATION, TAXONOMY): TAXONOMY_CHANGE_CHANGED_INTERPRETATION,
    (PEDAGOGICAL_REINTERPRETATION, T5_VERSIONS): T5_VERSION_CHANGE_CHANGED_INTERPRETATION,
    (PEDAGOGICAL_REINTERPRETATION, RELATION_DELTA): T5_RELATION_CHANGE_CHANGED_INTERPRETATION,
    (PEDAGOGICAL_REINTERPRETATION, T6_SPECIFICATION): T6_SPECIFICATION_CHANGE_CHANGED_INTERPRETATION,
}
_MAINTAINED = {
    NEW_USER_EVIDENCE: MAINTAINED_AFTER_NEW_USER_EVIDENCE,
    EVIDENCE_INTEGRITY_CHANGE: MAINTAINED_AFTER_INTEGRITY_CHANGE,
    PEDAGOGICAL_REINTERPRETATION: MAINTAINED_AFTER_CONTROLLED_REINTERPRETATION,
}


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class TransitionCauseAssessment:
    """Cause attribuée (None : première inférence) et sources PRÉSENTES qui
    la documentent (refs transition, run-level) ; reason_codes : vocabulaire
    fermé TRANSITION_REASON_CODES, ordre de déclaration. Jamais de prose."""
    cause: str | None
    observation_ids: tuple
    structural_refs: tuple
    reason_codes: tuple


@dataclass(frozen=True, kw_only=True)
class _Explanation:
    family: str
    kind: str
    observation_ids: tuple = ()
    structural_refs: tuple = ()


@dataclass(frozen=True, kw_only=True)
class _Facts:
    """Faits structurés du contexte, indexés (lecture seule)."""
    observations: dict
    endpoints: dict
    new_events: frozenset
    invalidated: frozenset
    reevaluated_previous: frozenset
    reevaluated_current: frozenset
    global_kinds: tuple
    predecessor_claims: dict
    predecessor_tensions: tuple
    predecessor_run_refs: tuple


# --------------------------------------------------------------------------
# Index des faits
# --------------------------------------------------------------------------

def _above(first: str, second: str) -> bool:
    return STAGE_SEQUENCE.index(first) > STAGE_SEQUENCE.index(second)


def relation_endpoints(dossier) -> dict:
    """{(relation_kind, relation_id): observations extrémités} des relations
    T5 du dossier courant (mêmes extrémités que T6-B)."""
    endpoints = {}
    for dependency in dossier.dependency_profile.dependencies:
        endpoints[(DEPENDENCY, dependency.relation_id)] = frozenset(
            {dependency.target_observation_id, dependency.source_observation_id} - {None})
    for transfer in dossier.transfer_profile.transfers:
        endpoints[(TRANSFER, transfer.relation_id)] = frozenset({transfer.source_observation_id,
                                                                  transfer.target_observation_id})
    for revalidation in dossier.consistency_profile.historical_revalidations:
        endpoints[(REVALIDATION, revalidation.relation_id)] = frozenset({
            revalidation.source_contradiction_observation_id, revalidation.target_supportive_observation_id})
    return endpoints


def _autonomous_relation_change(causality, attributed: frozenset) -> bool:
    """Changement SÉMANTIQUE des relations T5 (added / removed ; retained
    n'est jamais un changement) dont AUCUNE extrémité n'est une observation
    déjà attribuée à une autre famille (événement nouveau, invalidation,
    réévaluation) : une relation créée autour d'une preuve nouvelle ou
    retirée avec une observation invalidée relève de cette famille, jamais
    d'une réinterprétation. Seules les extrémités *_observation_id du
    payload sémantique sont lues (aucun UUID de relation : le contrat n'en
    fournit pas)."""
    delta = causality.relation_delta
    for family in (delta.dependencies, delta.transfers, delta.revalidations):
        for item in (*family.added, *family.removed):
            ends = {value for key, value in item.items() if key.endswith("_observation_id") and value is not None}
            if ends.isdisjoint(attributed):
                return True
    return False


def _facts(context) -> _Facts:
    dossier, causality = context.longitudinal_dossier, context.transition_causality
    endpoints = relation_endpoints(dossier)
    observations = {o.observation_id: o for o in dossier.active_history.observations}
    new_events = frozenset(causality.new_user_event_ids)
    invalidated = frozenset(c.observation_id for c in causality.integrity_changes)
    reevaluated_previous = frozenset(i for r in causality.reevaluations for i in r.previous_observation_ids)
    reevaluated_current = frozenset(i for r in causality.reevaluations for i in r.current_observation_ids)
    attributed = frozenset(str(i) for i in (*invalidated, *reevaluated_previous, *reevaluated_current,
                                            *(o for o, item in observations.items() if item.event_id in new_events)))
    global_kinds = tuple(kind for kind, present in (
        (TAXONOMY, causality.taxonomy_release_changed), (T5_VERSIONS, bool(causality.t5_version_changes)),
        (RELATION_DELTA, _autonomous_relation_change(causality, attributed)),
        (T6_SPECIFICATION, bool(causality.t6_specification_changes))) if present)
    decision = context.predecessor_decision_context
    return _Facts(
        observations=observations,
        endpoints=endpoints,
        new_events=new_events,
        invalidated=invalidated,
        reevaluated_previous=reevaluated_previous,
        reevaluated_current=reevaluated_current,
        global_kinds=global_kinds,
        predecessor_claims={c.stage: c for c in decision.claims},
        predecessor_tensions=tuple(decision.tensions),
        predecessor_run_refs=tuple(decision.run_basis_refs),
    )


def _present(observation_ids, structural_refs, kind: str, facts: _Facts) -> list:
    """Sources PRÉSENTES attribuables : observation d'un événement nouveau
    (ou relation qui en atteint une) => new_user_evidence ; observation
    issue d'une réévaluation => pedagogical_reinterpretation."""
    new, reinterpreted, new_refs = set(), set(), set()
    for observation_id in observation_ids:
        observation = facts.observations.get(observation_id)
        if observation is None:
            continue
        if observation.event_id in facts.new_events:
            new.add(observation_id)
        elif observation_id in facts.reevaluated_current:
            reinterpreted.add(observation_id)
    for ref in structural_refs:
        ends = facts.endpoints.get((ref.relation_kind, ref.relation_id), frozenset())
        if any(facts.observations[e].event_id in facts.new_events for e in ends if e in facts.observations):
            new_refs.add(ref)
    explanations = []
    if new or new_refs:
        explanations.append(_Explanation(family=NEW_USER_EVIDENCE, kind=kind, observation_ids=sorted_ids(new),
                                         structural_refs=sorted_refs(new_refs)))
    if reinterpreted:
        explanations.append(_Explanation(family=PEDAGOGICAL_REINTERPRETATION, kind=kind,
                                         observation_ids=sorted_ids(reinterpreted)))
    return explanations


def _lost(observation_ids, kind: str, facts: _Facts) -> list:
    """Sources SORTIES du dossier courant : invalidées => intégrité ;
    seulement réévaluées => réinterprétation. Jamais citées (absentes)."""
    gone = frozenset(observation_ids) - frozenset(facts.observations)
    explanations = []
    if gone & facts.invalidated:
        explanations.append(_Explanation(family=EVIDENCE_INTEGRITY_CHANGE, kind=kind))
    if (gone - facts.invalidated) & facts.reevaluated_previous:
        explanations.append(_Explanation(family=PEDAGOGICAL_REINTERPRETATION, kind=kind))
    return explanations


def _global(facts: _Facts) -> list:
    return [_Explanation(family=PEDAGOGICAL_REINTERPRETATION, kind=kind) for kind in facts.global_kinds]


def _observation_refs(refs, roles) -> list:
    return [r.source_id for r in refs if r.ref_role in roles and r.source_kind == SOURCE_OBSERVATION]


def _effective_basis(claims: dict, stage: str) -> tuple:
    """(observations, relations) de la base directe qui porte la claim
    (claim implied : sa claim directe source, T6-C1)."""
    claim = claims[stage]
    if claim.basis_mode == IMPLIED_BY_HIGHER_CLAIM:
        claim = claims[claim.implied_from_stage]
    return claim.basis_observation_ids, claim.structural_basis_refs


def _predecessor_basis(facts: _Facts, stage: str) -> list:
    """Observations qui fondaient la claim `stage` du predecessor : ses
    refs positive_basis / mastery, ou celles de la claim directe supérieure
    dont elle était implied (T6-B ne persiste aucune base recopiée)."""
    for higher in CLAIM_STAGES[CLAIM_STAGES.index(stage):]:
        claim = facts.predecessor_claims.get(higher)
        if claim is not None and claim.positive_basis_status == ESTABLISHED and claim.basis_mode == DIRECT:
            return _observation_refs(claim.basis_refs, {POSITIVE_BASIS, MASTERY_REF})
    return []


def _tension_identity(stage, mode, definition_ids) -> tuple:
    return stage, mode, frozenset(definition_ids)


# --------------------------------------------------------------------------
# Niveaux d'explication (ordre sémantique)
# --------------------------------------------------------------------------

def _level_a(state, transition, previous, current, facts: _Facts) -> list:
    if transition == REVISED_DOWN:
        return []
    explanations = []
    for resolution in state.revision_resolutions:
        stage = resolution.motif.fragilized_stage
        if resolution.resolution_status == UNRESOLVED:
            continue
        if transition == UPGRADED and not (_above(stage, previous) and not _above(stage, current)):
            continue
        if resolution.resolution_status == RESOLVED_SUPPORTIVELY:
            found = _present(resolution.observation_ids, resolution.structural_refs, RESOLUTION, facts)
            if [(e.family, e.observation_ids) for e in found] != [(NEW_USER_EVIDENCE, resolution.observation_ids)]:
                raise InconsistentInferenceAssessment("motif resolved_supportively sans démonstration présente d'un"
                                                      " événement nouveau (contrat T6-C2)")
            explanations.extend(found)
            continue
        # Même attribution par source que partout ailleurs : sources
        # invalidées => intégrité ; seulement réévaluées => réinterprétation ;
        # les deux (sources distinctes) => ambiguïté au même niveau.
        found = _lost(resolution.motif.source_contradiction_observation_ids, SUPERSESSION, facts)
        if not found:
            raise InconsistentInferenceAssessment("motif superseded sans invalidation ni réévaluation de ses"
                                                  " sources (contrat T6-C2)")
        explanations.extend(found)
    return explanations


def _level_b_stage(positive_basis, state, transition, previous, current, facts: _Facts) -> list:
    claims = {c.stage: c for c in positive_basis.claims}
    if transition == UPGRADED:
        found = _present(*_effective_basis(claims, current), BASIS, facts)
        if found:
            return found
        blockers = [t for t in facts.predecessor_tensions
                    if _above(t.fragilized_stage, previous) and not _above(t.fragilized_stage, current)]
        return _lost([i for t in blockers for i in _observation_refs(t.basis_refs, {TENSION})],
                     LOST_CONTRADICTION, facts)
    if transition == REVISED_DOWN:
        if previous in claims and claims[previous].status == ESTABLISHED:
            fragilizing = [t for t in state.tensions if t.compatibility_effect == MATERIALLY_INCOMPATIBLE
                           and _above(t.fragilized_stage, current) and not _above(t.fragilized_stage, previous)]
            found = _present([i for t in fragilizing for i in t.contradiction_observation_ids],
                             [r for t in fragilizing for r in t.structural_refs], CONTRADICTION, facts)
        else:
            found = _lost(_predecessor_basis(facts, previous), LOST_BASIS, facts)
        if found or current == NON_ETABLI:
            return found
        return _present(*_effective_basis(claims, current), BASIS, facts)
    return []


def _level_b_tensions(state, facts: _Facts) -> tuple:
    """(explications, changement de tensions ?) : tensions apparues (sources
    présentes) ou disparues (sources sorties du dossier)."""
    current = {_tension_identity(t.fragilized_stage, t.scope_mode, t.capability_definition_ids): t
               for t in state.tensions}
    previous = {_tension_identity(t.fragilized_stage, t.scope_mode, t.capability_definition_ids): t
                for t in facts.predecessor_tensions}
    appeared = [current[key] for key in current if key not in previous]
    vanished = [previous[key] for key in previous if key not in current]
    explanations = _present([i for t in appeared for i in t.contradiction_observation_ids],
                            [r for t in appeared for r in t.structural_refs], CONTRADICTION, facts)
    explanations += _lost([i for t in vanished for i in _observation_refs(t.basis_refs, {TENSION})],
                          LOST_CONTRADICTION, facts)
    return explanations, bool(appeared or vanished)


def _current_sources(positive_basis, state, *, descriptive: bool) -> tuple:
    """Sources citées par la décision courante : substantielles
    (positive_basis directe, Mastery, tensions) ou descriptives (confiance)."""
    observations, refs = [], []
    if descriptive:
        for profile in state.confidence_profiles:
            for dimension in () if profile is None else (profile.diagnosticity, profile.coverage,
                                                          profile.independence, profile.consistency,
                                                          profile.temporal_validation):
                observations.extend(dimension.observation_ids)
                refs.extend(dimension.structural_refs)
        return observations, refs
    for claim in positive_basis.claims:
        if claim.status == ESTABLISHED and claim.basis_mode == DIRECT:
            observations.extend(claim.basis_observation_ids)
        if claim.mastery_assessment is not None:
            assessment = claim.mastery_assessment
            for item in (assessment.autonomy, assessment.independent_repetition, assessment.variety_transfer,
                         assessment.longitudinality, assessment.robustness_revision):
                observations.extend(item.observation_ids)
                refs.extend(item.structural_refs)
    for tension in state.tensions:
        observations.extend(tension.contradiction_observation_ids)
        refs.extend(tension.structural_refs)
    return observations, refs


def _predecessor_sources(facts: _Facts, *, descriptive: bool) -> list:
    if descriptive:
        refs = [r for c in facts.predecessor_claims.values() for r in c.basis_refs]
        return _observation_refs([*refs, *facts.predecessor_run_refs], DESCRIPTIVE_ROLES)
    refs = [*(r for c in facts.predecessor_claims.values() for r in c.basis_refs),
            *(r for t in facts.predecessor_tensions for r in t.basis_refs)]
    return _observation_refs(refs, SUBSTANTIVE_ROLES)


def _level_snapshot(context, facts: _Facts) -> list:
    causality = context.transition_causality
    explanations = _present(facts.observations, (), SNAPSHOT, facts)
    if causality.integrity_changes:
        explanations.append(_Explanation(family=EVIDENCE_INTEGRITY_CHANGE, kind=SNAPSHOT))
    if causality.reevaluations and not any(e.family == PEDAGOGICAL_REINTERPRETATION for e in explanations):
        explanations.append(_Explanation(family=PEDAGOGICAL_REINTERPRETATION, kind=SNAPSHOT))
    return explanations + _global(facts)


# --------------------------------------------------------------------------
# Conclusion d'un niveau
# --------------------------------------------------------------------------

def _reason(family: str, kind: str, transition: str) -> str:
    if family == PEDAGOGICAL_REINTERPRETATION and kind not in (TAXONOMY, T5_VERSIONS, RELATION_DELTA,
                                                               T6_SPECIFICATION):
        return REEVALUATION_CHANGED_INTERPRETATION
    if (family, kind) == (NEW_USER_EVIDENCE, CONTRADICTION) and transition == REVISED_DOWN:
        return NEW_CONTRADICTION_DRIVES_REVISION
    return _REASONS[(family, kind)]


def _conclude(explanations, transition, current, positive_basis) -> TransitionCauseAssessment | None:
    if transition == REVISED_DOWN and current == NON_ETABLI:
        # Une contradiction nouvelle ne remet jamais une compétence à zéro.
        explanations = [e for e in explanations if e.family != NEW_USER_EVIDENCE]
    if not explanations:
        return None
    family, *others = sorted({e.family for e in explanations})
    if others:
        raise AmbiguousTransitionCause(f"familles {[family, *others]} également explicatives au même niveau :"
                                       " aucune priorité arbitraire")
    observations = sorted_ids(i for e in explanations for i in e.observation_ids)
    refs = sorted_refs(r for e in explanations for r in e.structural_refs)
    if not observations and not refs and transition in (UPGRADED, REVISED_DOWN) and current != NON_ETABLI:
        # Aucune source explicative présente (intégrité, réinterprétation
        # globale) : le stade retenu est documenté par sa propre base.
        basis, basis_refs = _effective_basis({c.stage: c for c in positive_basis.claims}, current)
        observations, refs = sorted_ids(basis), sorted_refs(basis_refs)
    reasons = {_reason(e.family, e.kind, transition) for e in explanations}
    if transition == MAINTAINED:
        reasons.add(_MAINTAINED[family])
    return TransitionCauseAssessment(cause=family, observation_ids=observations, structural_refs=refs,
                                     reason_codes=tuple(c for c in TRANSITION_REASON_CODES if c in reasons))


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------

def resolve_transition_cause(context, positive_basis, state) -> TransitionCauseAssessment:
    """Cause de transition (fonction pure). Voir la docstring du module."""
    check_inference_assessments(context, positive_basis, state)
    if context.predecessor is None:
        return TransitionCauseAssessment(cause=None, observation_ids=(), structural_refs=(),
                                         reason_codes=(FIRST_INFERENCE,))
    facts = _facts(context)
    previous, current = context.predecessor.current_stage, state.current_stage
    transition = MAINTAINED if current == previous else UPGRADED if _above(current, previous) else REVISED_DOWN
    stage_changed = transition != MAINTAINED
    tension_explanations, tensions_changed = _level_b_tensions(state, facts)
    levels = (
        lambda: _level_a(state, transition, previous, current, facts),
        lambda: _level_b_stage(positive_basis, state, transition, previous, current, facts),
        lambda: _global(facts) if stage_changed else [],
        lambda: tension_explanations,
        lambda: _global(facts) if tensions_changed else [],
        lambda: _present(*_current_sources(positive_basis, state, descriptive=False), CITED, facts)
        + _lost(_predecessor_sources(facts, descriptive=False), LOST_SOURCE, facts),
        lambda: _present(*_current_sources(positive_basis, state, descriptive=True), CITED, facts)
        + _lost(_predecessor_sources(facts, descriptive=True), LOST_SOURCE, facts),
        lambda: _level_snapshot(context, facts),
    )
    for level in levels:
        assessment = _conclude(level(), transition, current, positive_basis)
        if assessment is not None:
            return assessment
    raise UnattributableTransitionCause(f"predecessor {context.predecessor.inference_run_id} : aucune famille"
                                        " causale structurellement attribuable à la décision")

