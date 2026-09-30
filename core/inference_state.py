"""State Engine (T6-C2) : confiance, tensions, compatibilité et stade
défendable.

    InferenceContext + PositiveBasisAssessment
        -> T6C2PolicyResolver -> ClaimContextProjector
        -> ConfidenceProfileEngine -> Consistency / Tension Engine
        -> ClaimCompatibilityEngine -> CurrentStateEngine
        -> DownwardRevisionGuard -> AntiOscillationEngine
        -> T6C2Assessment (interne, jamais persisté ici)

Question : « quelle est la solidité descriptive de chacune des prétentions
établies par T6-C1, quelles contradictions les fragilisent réellement, et
quel stade reste aujourd'hui défendable ? ». T6-C2 ne choisit ni
transition_cause, ni validation_needs, ni confirmation / revalidation, ni
revision_status, ni résumé, ni membership de taxonomie, et ne construit
aucune InferenceDecision, StageClaimDecision, TensionDecision ni
BasisRefDecision : T6-C3.

Pureté : aucune base, Session, commit, modèle, réseau, horloge, hasard ni
texte relu. Même entrée => même T6C2Assessment (égalité de dataclasses),
quel que soit l'ordre des collections d'entrée.

Doctrine :

- Tension (TensionAssessment) : une contradiction T5 n'est une tension T6
  que si elle fragilise une claim positive ÉTABLIE (ou, dans un contexte de
  révision, une claim historiquement établie par le predecessor et portée
  par un motif non résolu). Sans prétention à fragiliser, elle reste dans
  l'histoire T5 : aucune tension, jamais un stade négatif. Une tension
  porte le plus petit périmètre ouvert qui touche le périmètre affirmé
  (capability_definition_id, jamais un membership : T6-C3), et
  compatibility_effect tensioned ou materially_incompatible.
- Matérialité : materially_incompatible exige une incompatibilité
  attribuable, diagnostique et représentative du périmètre affirmé. Voie A :
  UNE contradiction de niveau T3 déterminé qui atteint le stade, lecture
  diagnostique locale exceptionnelle (T3), non liée à une dépendance T5 sur
  le périmètre retenu, qui satisfait chaque relation représentative portée
  par la claim et touche ses capacités représentées. Voie B : plusieurs
  contradictions d'épisodes distincts dont T5 ÉTABLIT l'indépendance sur le
  périmètre apporté, qui ensemble le satisfont (en V1, T5 n'établit
  l'indépendance que des cibles supportive de transferts / revalidations :
  la voie B reste fermée, et un structural_recurrence_candidate n'est
  jamais une indépendance). Jamais « N contradictions => incompatible »,
  jamais « strong => incompatible », jamais d'error_type pondéré ;
  undetermined ne suffit jamais ; des contradictions dépendantes ne sont
  jamais plusieurs preuves. materially_incompatible n'efface pas la base
  positive historique de la claim.
- Stade (CurrentStateEngine) : la plus haute claim established non
  materially_incompatible (dossier_candidate_stage, avant stabilité). Une
  claim tensioned reste défendable. Une claim implied est une vraie claim :
  si seul le niveau supérieur est fragilisé, elle devient le candidat.
- DownwardRevisionGuard : aucune révision descendante vers un stade qui
  n'est pas positivement établi ET défendable. Sans stade défendable mais
  avec une claim positive : le stade précédent est maintenu s'il est encore
  établi (sinon la plus haute claim positive), sous tension ouverte et
  revision-context-v1 ; jamais non_etabli par élimination. non_etabli =
  aucune claim positive courante (ex. preuves invalidées / réinterprétées).
  Le predecessor n'est jamais une preuve : il ne sert qu'à stabiliser.
- AntiOscillationEngine : après une révision, les anciennes preuves du stade
  supérieur restent historiques mais ne provoquent jamais seules le
  rebound. Un motif est resolved_supportively seulement par une
  démonstration d'un CognitiveEvent NOUVEAU (transition_causality), de
  profondeur suffisante pour le stade fragilisé, couvrant exactement le
  périmètre du motif (une preuve C8_A ne résout pas C8_C), représentative,
  sans dépendance T5 sur ce périmètre, et dont l'indépendance est établie
  par T5 (transfert / revalidation) ou qui est une démonstration unique
  exceptionnellement diagnostique. Aucun compteur, aucun « deux
  confirmations ». superseded_by_reinterpretation_or_integrity_change :
  seulement si les contradictions du motif ont quitté le dossier par une
  invalidation ou une réévaluation T3 structurée (jamais « l'utilisateur a
  progressé »). Tant qu'un motif reste unresolved, aucun rebound vers son
  stade ou au-delà. Un motif resolved_supportively ne rend plus sa claim
  materially_incompatible (la contradiction reste une tension visible).

Ordres canoniques : claims / profils / compatibilités discovery -> mastery ;
capacités dans l'ordre naturel de la taxonomie résolue ; observations par
str(UUID) ; refs par (relation_kind, str(relation_id)) ; tensions et motifs
par (stade, scope_mode, capacités, contradictions) ; codes dans leur ordre
de déclaration.
"""
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.inference_confidence import (
    ClaimEvidenceContext,
    DossierIndex,
    claim_patterns,
    confidence_profile,
    index_dossier,
    project_claim_contexts,
    project_contradictions,
    read_contradictions,
    relation_refs,
    represents,
    single,
    sorted_ids,
    sorted_refs,
)
from core.inference_policies import InconsistentInferenceContext
from core.inference_positive_basis import (
    ANSWER_GIVEN,
    COMPETENCY_ONLY,
    COMPETENCY_UNIT,
    DEPENDENCY_LIMITING,
    INDEPENDENCE_EVIDENCE,
    LOCALIZED,
    PositiveBasisAssessment,
    evaluate_positive_basis,
    project_positive_evidence,
)
from core.inference_service import (
    CLAIM_STAGES,
    NON_ETABLI,
    STAGE_SEQUENCE,
    TENSION_SCOPE_MODES,
    PredecessorDecisionContext,
    PredecessorSnapshot,
    TransitionCausalityContext,
)
from core.inference_state_policies import (
    CLAIM_RELEVANT_CONTRADICTION,
    CLAIM_RELEVANT_TENSION,
    CLAIM_REPRESENTATIVE_CONTRADICTION,
    CLAIM_REPRESENTATIVE_INCOMPATIBILITY,
    COMPATIBILITY_REASON_CODES,
    COMPATIBLE,
    COMPETENCY_ONLY_CONTRADICTION,
    DEPENDENCY_LINKED_CONTRADICTION,
    DOWNWARD_REVISION_TO_DEFENSIBLE_LOWER_CLAIM,
    EXCEPTIONALLY_DIAGNOSTIC_DEMONSTRATION,
    FIRST_INFERENCE_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION,
    HIGHER_CLAIM_MATERIALLY_FRAGILIZED_DEFENSIBLE_LOWER_CLAIM_RETAINED,
    HIGHER_CLAIM_MATERIALLY_FRAGILIZED_LOWER_CLAIM_NOT_IDENTIFIED,
    HIGHEST_COMPATIBLE_POSITIVE_CLAIM,
    HIGHEST_POSITIVE_CLAIM_WITH_OPEN_TENSION,
    HIGHEST_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION,
    INDEPENDENT_COMPLEMENTARY_CONTRADICTIONS,
    MATERIALLY_INCOMPATIBLE,
    MOTIF_CONTRADICTIONS_INVALIDATED,
    MOTIF_CONTRADICTIONS_REEVALUATED,
    NEW_DEMONSTRATION_ON_MOTIF_SCOPE,
    NO_CURRENT_POSITIVE_CLAIM,
    NO_RELEVANT_OPEN_CONTRADICTION,
    NO_SUPPORTIVE_RESOLUTION_ON_MOTIF_SCOPE,
    PREDECESSOR_CLAIM_IN_REVISION_CONTEXT,
    PREVIOUS_STAGE_TEMPORARILY_HELD_UNDER_UNRESOLVED_TENSION,
    REBOUND_BLOCKED_BY_UNRESOLVED_REVISION_MOTIF,
    RELEVANT_CONTRADICTION_CLAIM_REMAINS_DEFENSIBLE,
    REPRESENTATIVE_CONTRADICTION_CLAIM_NOT_DEFENSIBLE,
    REPRESENTATIVE_SCOPE_WITHOUT_EXCEPTIONAL_DIAGNOSTICITY,
    RESOLUTION_REASON_CODES,
    RESOLVED_SUPPORTIVELY,
    REVISION_CONTEXT_SCHEMA_VERSION,
    REVISION_MOTIF_RESOLVED_SUPPORTIVELY,
    REVISION_MOTIF_RESOLVED_SUPPORTIVELY_STATE,
    REVISION_MOTIF_SUPERSEDED_BY_REINTERPRETATION,
    REVISION_REASON_CODES,
    STRUCTURAL_RECURRENCE_CANDIDATE,
    SUPERSEDED_BY_REINTERPRETATION_OR_INTEGRITY_CHANGE,
    T5_INDEPENDENCE_ON_MOTIF_SCOPE,
    TENSION_REASON_CODES,
    TENSIONED,
    UNDETERMINED,
    UNDETERMINED_CONTRADICTION_SCOPE,
    UNRESOLVED,
    InconsistentPositiveBasis,
    ResolvedStatePolicy,
    UnsupportedRevisionContext,
    resolve_state_decision_policy,
)

# Relecture de revision-context-v1 : vocabulaire T6-B des tensions
# (TENSION_SCOPE_MODES : localized, competency_only, whole_competency),
# une seule source de vérité. Les tensions COURANTES restent dérivées en
# localized / competency_only (_claim_tensions) ; un motif historique
# whole_competency (aucune capacité) est relu, préservé et transmis tel
# quel, jamais converti ni doté d'une capacité inventée.
REVISION_CONTEXT_FIELDS = ("schema_version", "origin_inference_run_id", "reason_code", "resolution_status", "motifs")
REVISION_MOTIF_FIELDS = ("fragilized_stage", "scope_mode", "capability_definition_ids",
                         "source_contradiction_observation_ids", "reason_codes")


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class TensionAssessment:
    """Tension T6 interne : claim fragilisée, plus petit périmètre ouvert
    (capability_definition_id ; competency_only : aucune capacité),
    contradictions sources, relations T5 qui la documentent (dépendances).
    Aucun revision_status (T6-C3)."""
    fragilized_stage: str
    scope_mode: str
    capability_definition_ids: tuple
    contradiction_observation_ids: tuple
    structural_refs: tuple
    compatibility_effect: str
    reason_codes: tuple


@dataclass(frozen=True, kw_only=True)
class AffectedScope:
    capability_definition_ids: tuple
    competency_only: bool


@dataclass(frozen=True, kw_only=True)
class ClaimCompatibilityAssessment:
    """Compatibilité d'une claim established avec le dossier négatif
    courant. tensions : les TensionAssessment (identiques à celles de
    T6C2Assessment.tensions) qui la concernent."""
    stage: str
    status: str
    affected_scope: AffectedScope
    tensions: tuple
    reason_codes: tuple


@dataclass(frozen=True, kw_only=True)
class RevisionMotif:
    fragilized_stage: str
    scope_mode: str
    capability_definition_ids: tuple
    source_contradiction_observation_ids: tuple
    reason_codes: tuple


@dataclass(frozen=True, kw_only=True)
class RevisionContextAssessment:
    """Contexte de révision canonique (revision-context-v1). Aucun texte
    libre n'est lu par la logique. to_payload() est l'unique format JSON de
    ce contexte : c'est lui que T6-C2 relit dans
    predecessor.unresolved_revision_context (fail closed sinon) ; T6-C3
    décidera de le persister."""
    schema_version: str
    origin_inference_run_id: uuid.UUID
    reason_code: str
    resolution_status: str
    motifs: tuple

    def to_payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "origin_inference_run_id": str(self.origin_inference_run_id),
            "reason_code": self.reason_code,
            "resolution_status": self.resolution_status,
            "motifs": [{
                "fragilized_stage": m.fragilized_stage,
                "scope_mode": m.scope_mode,
                "capability_definition_ids": [str(i) for i in m.capability_definition_ids],
                "source_contradiction_observation_ids": [str(i) for i in m.source_contradiction_observation_ids],
                "reason_codes": list(m.reason_codes),
            } for m in self.motifs],
        }


@dataclass(frozen=True, kw_only=True)
class RevisionMotifResolution:
    """Lecture, sur le dossier courant, d'un motif du contexte de révision
    du predecessor. observation_ids : démonstrations qui le résolvent, ou
    contradictions sorties du dossier (supersession)."""
    motif: RevisionMotif
    resolution_status: str
    observation_ids: tuple
    structural_refs: tuple
    reason_codes: tuple


@dataclass(frozen=True, kw_only=True)
class T6C2Assessment:
    """Sortie interne de T6-C2. confidence_profiles et claim_compatibilities
    : une entrée par claim (discovery -> mastery), None si la claim n'est
    pas established. dossier_candidate_stage : meilleur stade suggéré par le
    dossier courant AVANT stabilité / anti-oscillation (None : aucun).
    current_stage : stade défendable retenu (non_etabli = aucune claim
    positive). revision_context : contexte non résolu applicable à cette
    décision (None si aucun)."""
    positive_basis_version: str
    confidence_profile_version: str
    state_decision_version: str
    competency_code: str
    confidence_profiles: tuple
    claim_compatibilities: tuple
    tensions: tuple
    dossier_candidate_stage: str | None
    current_stage: str
    revision_context: RevisionContextAssessment | None
    revision_resolutions: tuple
    state_reason_code: str


# --------------------------------------------------------------------------
# Ordres canoniques
# --------------------------------------------------------------------------

def _above(first: str, second: str) -> bool:
    return STAGE_SEQUENCE.index(first) > STAGE_SEQUENCE.index(second)


def _ordered_caps(ids, policy: ResolvedStatePolicy) -> tuple:
    """Ordre naturel de la taxonomie résolue ; une capacité hors de la
    release courante (motif historique) suit, par str(UUID)."""
    ids = frozenset(i for i in ids if i is not COMPETENCY_UNIT)
    known = frozenset(policy.positive_basis.definition_ids)
    return (*policy.positive_basis.ordered(ids), *sorted(ids - known, key=str))


def _caps_key(ids, policy: ResolvedStatePolicy) -> tuple:
    known = policy.positive_basis.definition_ids
    return tuple((d not in known, known.index(d) if d in known else str(d)) for d in ids)


def _scoped_key(stage, mode, caps, observations, policy) -> tuple:
    return (CLAIM_STAGES.index(stage), mode, _caps_key(caps, policy), tuple(str(i) for i in observations))


def _tension_key(policy):
    return lambda t: (*_scoped_key(t.fragilized_stage, t.scope_mode, t.capability_definition_ids,
                                   t.contradiction_observation_ids, policy), t.compatibility_effect)


def _motif_key(policy):
    return lambda m: _scoped_key(m.fragilized_stage, m.scope_mode, m.capability_definition_ids,
                                 m.source_contradiction_observation_ids, policy)


def _codes(declared, present) -> tuple:
    present = frozenset(present)
    return tuple(code for code in declared if code in present)


# --------------------------------------------------------------------------
# Entrées : base positive et predecessor
# --------------------------------------------------------------------------

def _check_positive_basis(context, positive_basis) -> None:
    if type(positive_basis) is not PositiveBasisAssessment:
        raise InconsistentPositiveBasis("PositiveBasisAssessment attendu")
    if positive_basis.policy_version != context.positive_basis_version:
        raise InconsistentPositiveBasis(f"positive_basis {positive_basis.policy_version!r} != contexte"
                                        f" {context.positive_basis_version!r}")
    if positive_basis.competency_code != context.competency_code:
        raise InconsistentPositiveBasis(f"compétence {positive_basis.competency_code!r} != contexte"
                                        f" {context.competency_code!r}")
    if positive_basis != evaluate_positive_basis(context):
        raise InconsistentPositiveBasis("PositiveBasisAssessment différent de la base T6-C1 du dossier courant")


def _uuid(value, name: str) -> uuid.UUID:
    if type(value) is not str:
        raise UnsupportedRevisionContext(f"{name} : UUID canonique attendu")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise UnsupportedRevisionContext(f"{name} : UUID invalide") from None
    if str(parsed) != value:
        raise UnsupportedRevisionContext(f"{name} : forme canonique attendue")
    return parsed


def _sequence(value, name: str) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise UnsupportedRevisionContext(f"{name} : liste attendue")
    return tuple(value)


def _mapping(value, fields, name: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise UnsupportedRevisionContext(f"{name} : champs {sorted(fields)} exactement")
    return value


def _parse_revision_context(raw, policy: ResolvedStatePolicy) -> RevisionContextAssessment:
    """Relecture STRICTE de revision-context-v1 (format de to_payload) :
    jamais de conversion, jamais de repli."""
    data = _mapping(raw, REVISION_CONTEXT_FIELDS, "unresolved_revision_context")
    if data["schema_version"] != REVISION_CONTEXT_SCHEMA_VERSION:
        raise UnsupportedRevisionContext(f"schema_version {data['schema_version']!r} inconnue")
    if data["reason_code"] not in REVISION_REASON_CODES or data["resolution_status"] != UNRESOLVED:
        raise UnsupportedRevisionContext("reason_code / resolution_status hors vocabulaire")
    motifs = []
    for index, item in enumerate(_sequence(data["motifs"], "motifs")):
        motif = _mapping(item, REVISION_MOTIF_FIELDS, f"motifs[{index}]")
        caps = tuple(_uuid(v, "capability_definition_ids")
                     for v in _sequence(motif["capability_definition_ids"], "capability_definition_ids"))
        sources = tuple(_uuid(v, "source_contradiction_observation_ids") for v in _sequence(
            motif["source_contradiction_observation_ids"], "source_contradiction_observation_ids"))
        reasons = _sequence(motif["reason_codes"], "reason_codes")
        if motif["fragilized_stage"] not in CLAIM_STAGES or motif["scope_mode"] not in TENSION_SCOPE_MODES:
            raise UnsupportedRevisionContext(f"motifs[{index}] : stade / scope_mode hors vocabulaire")
        if (motif["scope_mode"] == LOCALIZED) != bool(caps) or not sources or \
                not set(reasons) <= set(TENSION_REASON_CODES):
            raise UnsupportedRevisionContext(f"motifs[{index}] : périmètre, sources ou raisons invalides")
        parsed = RevisionMotif(fragilized_stage=motif["fragilized_stage"], scope_mode=motif["scope_mode"],
                               capability_definition_ids=_ordered_caps(caps, policy),
                               source_contradiction_observation_ids=sorted_ids(sources),
                               reason_codes=_codes(TENSION_REASON_CODES, reasons))
        if parsed.capability_definition_ids != caps or parsed.source_contradiction_observation_ids != sources or \
                parsed.reason_codes != tuple(reasons):
            raise UnsupportedRevisionContext(f"motifs[{index}] : ordre non canonique")
        motifs.append(parsed)
    if not motifs or tuple(sorted(motifs, key=_motif_key(policy))) != tuple(motifs):
        raise UnsupportedRevisionContext("motifs vides ou hors ordre canonique")
    return RevisionContextAssessment(
        schema_version=REVISION_CONTEXT_SCHEMA_VERSION,
        origin_inference_run_id=_uuid(data["origin_inference_run_id"], "origin_inference_run_id"),
        reason_code=data["reason_code"], resolution_status=UNRESOLVED, motifs=tuple(motifs))


def _predecessor(context, policy: ResolvedStatePolicy) -> tuple:
    """(previous_stage | None, RevisionContextAssessment | None). Lecture
    HISTORIQUE uniquement (stabilité, motifs) : jamais une preuve."""
    snapshot, decision = context.predecessor, context.predecessor_decision_context
    if snapshot is None:
        if decision is not None or context.transition_causality is not None:
            raise InconsistentInferenceContext("première inférence : aucun contexte predecessor ni causalité")
        return None, None
    if type(snapshot) is not PredecessorSnapshot or type(decision) is not PredecessorDecisionContext or \
            decision.inference_run_id != snapshot.inference_run_id:
        raise InconsistentInferenceContext("predecessor et predecessor_decision_context incohérents")
    if type(context.transition_causality) is not TransitionCausalityContext:
        raise InconsistentInferenceContext("predecessor sans transition_causality (V2 exigé)")
    if snapshot.current_stage not in STAGE_SEQUENCE:
        raise InconsistentInferenceContext(f"predecessor current_stage {snapshot.current_stage!r} inconnu")
    raw = snapshot.unresolved_revision_context
    return snapshot.current_stage, None if raw is None else _parse_revision_context(raw, policy)


# --------------------------------------------------------------------------
# Résolution des motifs du predecessor (AntiOscillationEngine, en amont)
# --------------------------------------------------------------------------

def _resolve_motif(motif: RevisionMotif, context, evidence, index: DossierIndex,
                   policy: ResolvedStatePolicy) -> RevisionMotifResolution:
    causality = context.transition_causality
    sources = frozenset(motif.source_contradiction_observation_ids)
    invalidated = frozenset(c.observation_id for c in causality.integrity_changes) & sources
    reevaluated = frozenset(i for r in causality.reevaluations for i in r.previous_observation_ids) & sources
    if sources.isdisjoint(index.observations) and sources <= invalidated | reevaluated:
        return RevisionMotifResolution(
            motif=motif, resolution_status=SUPERSEDED_BY_REINTERPRETATION_OR_INTEGRITY_CHANGE,
            observation_ids=sorted_ids(sources), structural_refs=(),
            reason_codes=_codes(RESOLUTION_REASON_CODES, [
                *([MOTIF_CONTRADICTIONS_INVALIDATED] if invalidated else []),
                *([MOTIF_CONTRADICTIONS_REEVALUATED] if reevaluated else [])]))
    rules, positive = policy.rules, policy.positive_basis
    stage_policy = positive.stage_policies[rules.pattern_stage[motif.fragilized_stage]]
    new_events = frozenset(causality.new_user_event_ids)
    scope = frozenset(motif.capability_definition_ids)
    resolving, refs, reasons = [], [], set()
    for demonstration in evidence.demonstrations:
        if demonstration.event_id not in new_events or \
                demonstration.local_stage not in rules.resolution_depths[motif.fragilized_stage]:
            continue
        if not scope <= frozenset(demonstration.capability_definition_ids):
            continue
        localized = demonstration.capability_localization == LOCALIZED
        if not (any(p.satisfied_by(demonstration.capability_definition_ids)
                    for p in stage_policy.representative_patterns)
                or (not localized and stage_policy.competency_only_allowed)):
            continue
        units = scope or frozenset(s.capability_definition_id for s in demonstration.scope_readings)
        readings = [s for s in demonstration.scope_readings if s.capability_definition_id in units]
        if any(s.classification in DEPENDENCY_LIMITING for s in readings):
            continue
        independent = bool(readings) and all(s.classification == INDEPENDENCE_EVIDENCE for s in readings)
        exceptional = demonstration.evidence_strength in rules.exceptional_diagnostic_strengths and \
            demonstration.support_level != ANSWER_GIVEN
        if not (independent or exceptional):
            continue
        resolving.append(demonstration.observation_id)
        reasons.add(NEW_DEMONSTRATION_ON_MOTIF_SCOPE)
        if independent:
            reasons.add(T5_INDEPENDENCE_ON_MOTIF_SCOPE)
            refs.extend(relation_refs((i for s in readings for i in index.scope_readings[
                (demonstration.observation_id, s.capability_definition_id)].independence_evidence_relation_ids),
                index))
        if exceptional:
            reasons.add(EXCEPTIONALLY_DIAGNOSTIC_DEMONSTRATION)
    if not resolving:
        return RevisionMotifResolution(motif=motif, resolution_status=UNRESOLVED, observation_ids=(),
                                       structural_refs=(), reason_codes=(NO_SUPPORTIVE_RESOLUTION_ON_MOTIF_SCOPE,))
    return RevisionMotifResolution(motif=motif, resolution_status=RESOLVED_SUPPORTIVELY,
                                   observation_ids=sorted_ids(resolving), structural_refs=sorted_refs(refs),
                                   reason_codes=_codes(RESOLUTION_REASON_CODES, reasons))


# --------------------------------------------------------------------------
# Consistency / Tension Engine
# --------------------------------------------------------------------------

def _resolved_contradictions(resolutions, stage: str) -> frozenset:
    """Contradictions dont un motif couvrant ce stade (ou un stade
    supérieur) a été résolu supportivement : elles restent des tensions
    visibles, jamais une incompatibilité matérielle."""
    return frozenset(i for r in resolutions if r.resolution_status == RESOLVED_SUPPORTIVELY
                     and not _above(stage, r.motif.fragilized_stage)
                     for i in r.motif.source_contradiction_observation_ids)


def _tension(stage, mode, caps, readings, effect, reasons, policy) -> TensionAssessment:
    return TensionAssessment(
        fragilized_stage=stage, scope_mode=mode, capability_definition_ids=_ordered_caps(caps, policy),
        contradiction_observation_ids=sorted_ids(r.contradiction.observation_id for r in readings),
        structural_refs=sorted_refs(ref for r in readings for ref in r.contradiction.dependency_refs),
        compatibility_effect=effect, reason_codes=_codes(TENSION_REASON_CODES, reasons))


def _claim_tensions(stage: str, context: ClaimEvidenceContext, readings: tuple, resolved: frozenset,
                    policy: ResolvedStatePolicy) -> list:
    groups = {}
    for reading in readings:
        if reading.reading not in (CLAIM_RELEVANT_TENSION, CLAIM_REPRESENTATIVE_INCOMPATIBILITY):
            continue
        contradiction = reading.contradiction
        mode = COMPETENCY_ONLY if contradiction.capability_localization == COMPETENCY_ONLY else LOCALIZED
        groups.setdefault((mode, _ordered_caps(reading.relevant_units, policy)), []).append(reading)
    tensions = []
    for (mode, caps), items in groups.items():
        reasons, material = set(), False
        for r in items:
            c = r.contradiction
            downgraded = r.reading == CLAIM_REPRESENTATIVE_INCOMPATIBILITY and c.observation_id in resolved
            if r.reading == CLAIM_REPRESENTATIVE_INCOMPATIBILITY and not downgraded:
                material = True
                reasons.add(CLAIM_REPRESENTATIVE_CONTRADICTION)
            else:
                reasons.add(CLAIM_RELEVANT_CONTRADICTION)
            reasons.update(code for code, present in (
                (REVISION_MOTIF_RESOLVED_SUPPORTIVELY, downgraded),
                (REPRESENTATIVE_SCOPE_WITHOUT_EXCEPTIONAL_DIAGNOSTICITY,
                 r.representative_scope and r.material_scope and not r.exceptionally_diagnostic),
                (UNDETERMINED_CONTRADICTION_SCOPE, c.contradiction_scope == UNDETERMINED),
                (COMPETENCY_ONLY_CONTRADICTION, mode == COMPETENCY_ONLY),
                (DEPENDENCY_LINKED_CONTRADICTION, r.dependency_linked),
                (STRUCTURAL_RECURRENCE_CANDIDATE, c.recurrence_candidate)) if present)
        tensions.append(_tension(stage, mode, caps, items, MATERIALLY_INCOMPATIBLE if material else TENSIONED,
                                 reasons, policy))
    complementary = _independent_complementary(context, readings, resolved, policy)
    if complementary:
        tensions.append(_tension(stage, LOCALIZED, [u for r in complementary for u in r.relevant_units],
                                 complementary, MATERIALLY_INCOMPATIBLE, [INDEPENDENT_COMPLEMENTARY_CONTRADICTIONS],
                                 policy))
    return tensions


def _independent_complementary(context, readings, resolved, policy) -> list:
    """Voie B : contradictions pertinentes d'épisodes distincts, de niveau
    déterminé, dont T5 établit l'indépendance sur le périmètre apporté, qui
    ENSEMBLE satisfont le périmètre affirmé. Aucun compte : la relation
    sémantique décide, et chaque apport doit être indépendant."""
    contributions = {}
    for r in readings:
        c = r.contradiction
        if r.reading != CLAIM_RELEVANT_TENSION or not r.material_scope or c.capability_localization != LOCALIZED \
                or c.observation_id in resolved:
            continue
        units = (c.independent_units - c.dependency_limited_units) & c.open_units
        if units:
            contributions[c.observation_id] = (r, units)
    if not contributions or single(r.contradiction.event_id for r, _ in contributions.values()):
        return []
    union = frozenset().union(*(units for _, units in contributions.values()))
    if not represents(union, context, claim_patterns(context, policy)):
        return []
    return [r for r, _ in contributions.values()]


def _historical_tensions(context, resolutions, contexts: dict, contradictions: tuple,
                         policy: ResolvedStatePolicy) -> list:
    """Claim établie par le predecessor, non établie aujourd'hui, portée par
    un motif non résolu dont les contradictions restent ouvertes : tension
    dans le contexte de révision (jamais une incompatibilité)."""
    snapshot = context.predecessor
    by_id = {c.observation_id: c for c in contradictions}
    tensions = []
    for resolution in resolutions:
        motif = resolution.motif
        if resolution.resolution_status != UNRESOLVED or motif.fragilized_stage in contexts or \
                motif.fragilized_stage not in snapshot.established_claim_stages:
            continue
        present = [by_id[i] for i in motif.source_contradiction_observation_ids
                   if i in by_id and by_id[i].open_units]
        scope = frozenset(motif.capability_definition_ids)
        caps = [u for c in present for u in c.open_units if u in scope]
        if not present or (motif.scope_mode == LOCALIZED and not caps):
            continue
        tensions.append(TensionAssessment(
            fragilized_stage=motif.fragilized_stage, scope_mode=motif.scope_mode,
            capability_definition_ids=_ordered_caps(caps, policy),
            contradiction_observation_ids=sorted_ids(c.observation_id for c in present),
            structural_refs=sorted_refs(ref for c in present for ref in c.dependency_refs),
            compatibility_effect=TENSIONED, reason_codes=(PREDECESSOR_CLAIM_IN_REVISION_CONTEXT,)))
    return tensions


# --------------------------------------------------------------------------
# ClaimCompatibilityEngine
# --------------------------------------------------------------------------

def _compatibility(stage: str, tensions: tuple, policy: ResolvedStatePolicy) -> ClaimCompatibilityAssessment:
    mine = tuple(t for t in tensions if t.fragilized_stage == stage)
    if any(t.compatibility_effect == MATERIALLY_INCOMPATIBLE for t in mine):
        status, reason = MATERIALLY_INCOMPATIBLE, REPRESENTATIVE_CONTRADICTION_CLAIM_NOT_DEFENSIBLE
    elif mine:
        status, reason = TENSIONED, RELEVANT_CONTRADICTION_CLAIM_REMAINS_DEFENSIBLE
    else:
        status, reason = COMPATIBLE, NO_RELEVANT_OPEN_CONTRADICTION
    return ClaimCompatibilityAssessment(
        stage=stage, status=status,
        affected_scope=AffectedScope(
            capability_definition_ids=_ordered_caps((i for t in mine for i in t.capability_definition_ids), policy),
            competency_only=any(t.scope_mode == COMPETENCY_ONLY for t in mine)),
        tensions=mine, reason_codes=_codes(COMPATIBILITY_REASON_CODES, [reason]))


# --------------------------------------------------------------------------
# CurrentStateEngine + DownwardRevisionGuard + AntiOscillationEngine
# --------------------------------------------------------------------------

def _highest(stages) -> str | None:
    stages = frozenset(stages)
    return next((s for s in reversed(CLAIM_STAGES) if s in stages), None)


def _guarded_stage(established, defensible, compatibilities, previous) -> tuple:
    """DownwardRevisionGuard : (stade, raison). Jamais une descente vers un
    stade non établi et défendable ; jamais non_etabli par élimination."""
    candidate = _highest(defensible)
    if not established:
        return NON_ETABLI, NO_CURRENT_POSITIVE_CLAIM
    if candidate is None:
        if previous is None:
            return _highest(established), FIRST_INFERENCE_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION
        if previous in established:
            return previous, PREVIOUS_STAGE_TEMPORARILY_HELD_UNDER_UNRESOLVED_TENSION
        return _highest(established), HIGHEST_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION
    if previous is not None and _above(previous, candidate):
        return candidate, DOWNWARD_REVISION_TO_DEFENSIBLE_LOWER_CLAIM
    if compatibilities[candidate].status == TENSIONED:
        return candidate, HIGHEST_POSITIVE_CLAIM_WITH_OPEN_TENSION
    return candidate, HIGHEST_COMPATIBLE_POSITIVE_CLAIM


def _anti_oscillation(stage, reason, established, defensible, previous, resolutions) -> tuple:
    """AntiOscillationEngine : aucun rebound vers (ou au-delà) du stade d'un
    motif non résolu ; les anciennes preuves seules ne remontent jamais."""
    if previous is None or not _above(stage, previous):
        return stage, reason
    crossed = [r for r in resolutions if _above(r.motif.fragilized_stage, previous)
               and not _above(r.motif.fragilized_stage, stage)]
    blocking = [r.motif.fragilized_stage for r in crossed if r.resolution_status == UNRESOLVED]
    if blocking:
        cap = next(s for s in CLAIM_STAGES if s in blocking)
        below = [s for s in established if _above(cap, s)]
        if below:
            kept = _highest(s for s in defensible if s in below) or \
                (previous if previous in below else _highest(below))
            return kept, REBOUND_BLOCKED_BY_UNRESOLVED_REVISION_MOTIF
        return stage, reason
    if any(r.resolution_status == RESOLVED_SUPPORTIVELY for r in crossed):
        return stage, REVISION_MOTIF_RESOLVED_SUPPORTIVELY_STATE
    if crossed:
        return stage, REVISION_MOTIF_SUPERSEDED_BY_REINTERPRETATION
    return stage, reason


def _revision_context(context, stage, compatibilities, tensions, carried, resolutions,
                      policy: ResolvedStatePolicy) -> RevisionContextAssessment | None:
    """Motifs non résolus du predecessor + motifs nouveaux (tensions
    materially_incompatible des claims au niveau du stade retenu ou
    au-dessus)."""
    held = stage in compatibilities and compatibilities[stage].status == MATERIALLY_INCOMPATIBLE
    new = [RevisionMotif(fragilized_stage=t.fragilized_stage, scope_mode=t.scope_mode,
                         capability_definition_ids=t.capability_definition_ids,
                         source_contradiction_observation_ids=t.contradiction_observation_ids,
                         reason_codes=t.reason_codes)
           for t in tensions if t.compatibility_effect == MATERIALLY_INCOMPATIBLE
           and stage != NON_ETABLI and not _above(stage, t.fragilized_stage)]
    kept = [r.motif for r in resolutions if r.resolution_status == UNRESOLVED]
    identity = lambda m: (m.fragilized_stage, m.scope_mode, m.capability_definition_ids,  # noqa: E731
                          m.source_contradiction_observation_ids)
    motifs = {identity(m): m for m in kept}
    for motif in new:
        motifs.setdefault(identity(motif), motif)
    if not motifs:
        return None
    if held:
        reason = HIGHER_CLAIM_MATERIALLY_FRAGILIZED_LOWER_CLAIM_NOT_IDENTIFIED
    elif new:
        reason = HIGHER_CLAIM_MATERIALLY_FRAGILIZED_DEFENSIBLE_LOWER_CLAIM_RETAINED
    else:
        reason = carried.reason_code
    return RevisionContextAssessment(
        schema_version=REVISION_CONTEXT_SCHEMA_VERSION,
        origin_inference_run_id=carried.origin_inference_run_id if kept else context.run_id,
        reason_code=reason, resolution_status=UNRESOLVED,
        motifs=tuple(sorted(motifs.values(), key=_motif_key(policy))))


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------

def evaluate_inference_state(context, positive_basis) -> T6C2Assessment:
    """T6-C2 (fonction pure) : confiance, tensions, compatibilité et stade
    défendable de la base positive T6-C1 du contexte.

    1. résolution des versions (gardes T6-C1 + combinaison T6-C2 : fail
       closed) ; 2. base positive = celle de T6-C1 pour ce contexte ;
    3. predecessor (historique : stade précédent, revision-context-v1) ;
    4. projection des claims et des contradictions, lecture partagée ;
    5. profils de confiance ; 6. résolution des motifs du predecessor ;
    7. tensions, compatibilités ; 8. dossier_candidate_stage ;
    9. DownwardRevisionGuard ; 10. AntiOscillationEngine ;
    11. contexte de révision."""
    policy = resolve_state_decision_policy(context)
    _check_positive_basis(context, positive_basis)
    previous, carried = _predecessor(context, policy)
    dossier = context.longitudinal_dossier
    index = index_dossier(dossier)
    contexts = project_claim_contexts(positive_basis)
    contradictions = project_contradictions(dossier, policy, index)
    readings = read_contradictions(contexts, contradictions, policy)
    profiles = {stage: confidence_profile(item, readings[stage], dossier, index, policy)
                for stage, item in contexts.items()}

    evidence = project_positive_evidence(context, policy.positive_basis)
    resolutions = tuple(sorted((_resolve_motif(m, context, evidence, index, policy) for m in carried.motifs),
                               key=lambda r: _motif_key(policy)(r.motif))) if carried else ()

    tensions = [t for stage, item in contexts.items()
                for t in _claim_tensions(stage, item, readings[stage], _resolved_contradictions(resolutions, stage),
                                         policy)]
    if carried:
        tensions.extend(_historical_tensions(context, resolutions, contexts, contradictions, policy))
    tensions = tuple(sorted(tensions, key=_tension_key(policy)))
    compatibilities = {stage: _compatibility(stage, tensions, policy) for stage in contexts}

    established = [s for s in CLAIM_STAGES if s in contexts]
    defensible = [s for s in established if compatibilities[s].status != MATERIALLY_INCOMPATIBLE]
    stage, reason = _guarded_stage(established, defensible, compatibilities, previous)
    stage, reason = _anti_oscillation(stage, reason, established, defensible, previous, resolutions)
    rules = policy.rules
    return T6C2Assessment(
        positive_basis_version=rules.positive_basis_version,
        confidence_profile_version=rules.confidence_profile_version,
        state_decision_version=rules.state_decision_version,
        competency_code=policy.positive_basis.competency_code,
        confidence_profiles=tuple(profiles.get(s) for s in CLAIM_STAGES),
        claim_compatibilities=tuple(compatibilities.get(s) for s in CLAIM_STAGES),
        tensions=tensions,
        dossier_candidate_stage=_highest(defensible),
        current_stage=stage,
        revision_context=_revision_context(context, stage, compatibilities, tensions, carried, resolutions, policy),
        revision_resolutions=resolutions,
        state_reason_code=reason,
    )

