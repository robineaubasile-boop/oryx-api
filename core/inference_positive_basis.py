"""Positive Basis Engine (T6-C1) : première brique du moteur d'inférence
pédagogique T6-C.

    InferenceContext (T6-B / T6-C0) -> evaluate_positive_basis ->
    PositiveBasisAssessment (interne, jamais persisté ici)

Question, et seulement elle : « en regardant uniquement les preuves
positives du dossier longitudinal courant, quelles prétentions Discovery,
Comprehension, Application et Mastery possèdent actuellement une base
positive suffisamment établie ? ». T6-C1 ne décide ni current_stage, ni
confidence_profile, ni tension, ni compatibilité avec une contradiction, ni
révision, anti-oscillation, transition, validation_needs ou résumé : T6-C2 /
T6-C3. Aucune InferenceDecision n'est construite, aucun service n'est appelé.

Pureté : aucune base, Session, commit, modèle, réseau, horloge, hasard ni
état global mutable. Même InferenceContext => même PositiveBasisAssessment
(égalité de dataclasses), quel que soit l'ordre des collections d'entrée.

Doctrine (pourquoi aucun score) :

- capability = périmètre sémantique ; observation = preuve locale ;
  local_stage = profondeur LOCALE ; evidence_strength = qualité diagnostique
  locale ; T5 = structure de l'histoire. Aucun compteur, ratio, moyenne,
  poids, points ni « N capacités => stade ».
- Entrée probante : les seules observations du dossier T5 courant
  (active_history). supportive + local_stage discovery / comprehension /
  application = démonstration positive candidate ; local_stage none ne
  construit jamais rien ; contradictory est TOTALEMENT ignorée (ajouter des
  contradictions laisse le résultat identique : T6-C2). Le predecessor et la
  causalité de transition sont historiques : jamais lus ici. Aucun texte
  (observation_text, primary_user_action, residual_cognitive_work,
  source_contribution_refs) n'est relu : seules les propriétés structurées.
- Profondeur (local_stage) : discovery soutient Discovery ; comprehension
  soutient Discovery ou Comprehension ; application les trois. Chaque claim
  directe est évaluée pour elle-même : l'échec d'une claim supérieure
  n'établit jamais une claim inférieure.
- evidence_strength (qualité diagnostique LOCALE, jamais un stade) reste une
  propriété descriptive, projetée telle quelle pour la confiance future
  (T6-C2) : T6-C1 n'en fait JAMAIS une condition d'éligibilité (ni « weak =>
  incapable seule », ni « medium / strong => capable seule »). Ce qui fait
  une base positive est la profondeur locale et la représentativité
  sémantique du périmètre démontré. Aucune accumulation : répéter un même
  micro-savoir n'étend jamais le périmètre, donc ne crée aucun stade.
- Représentativité (core/inference_policies.py) : un SemanticPattern est
  une relation booléenne nommée entre dimensions de la compétence.
  A. Démonstration ou épisode directement représentatif : UNE observation
     (single_representative_demonstration), les observations sœurs d'UN
     CognitiveEvent qui élargissent la couverture
     (single_representative_episode), ou une observation competency_only
     (competency_only_representative_demonstration). Une dépendance T5 n'y
     est jamais une seconde barrière : elle est seulement signalée
     (dependency_limited_demonstration) ; support_level n'y est ni un
     coefficient ni un plafond (T3 l'a déjà pris en compte).
  B. Sinon, histoire complémentaire (complementary_representative_history,
     patterns relationnels seulement) : des épisodes DISTINCTS couvrent
     chacun une dimension différente de la relation, aucun n'y suffisant
     seul (répétition redondante d'un même micro-savoir => rien). Parce
     que plusieurs épisodes deviennent NÉCESSAIRES, chaque contribution doit
     être indépendante selon T5 sur le périmètre qu'elle apporte
     (independence_evidence) ; not_established (absence d'arête),
     dependent et partially_dependent ne suffisent jamais. Complémentarité
     réelle sans indépendance établie : claim non établie par cette voie,
     raison independence_not_established (jamais une fraction). Une
     première démonstration représentative unique (A) n'exige, elle, aucune
     indépendance ni répétition.
  competency_only est légitime (R6) : jamais transformée en capacités
  fictives, jamais accumulée (aucune dimension => jamais complémentaire).
- Mastery est exclusivement longitudinale et qualitative : Application
  représentative établie ET cinq propriétés soutenues (autonomy,
  independent_repetition, variety_transfer, longitudinality,
  robustness_revision) ET périmètre remobilisé représentatif. Lues sur les
  seules relations T5 établies (transferts, revalidations, durability
  evidence) dont la cible est une démonstration positive d'Application,
  jamais sur un horodatage, un âge, un nombre d'observations ni une absence
  d'arête. autonomy lit les faits structurés du périmètre remobilisé :
  dépendances T5 des extrémités positives de la relation sur ce périmètre,
  et answer_given sur la cible (qui ne peut, seule, prouver que le
  raisonnement essentiel appartient à l'utilisateur) ; guided / hinted ne
  sont jamais un plafond. Une même relation peut documenter plusieurs
  propriétés sans devenir plusieurs preuves. Aucun 5/5, aucun « presque
  Mastery ».
- Implication et anti-double-comptage : Mastery => Application =>
  Comprehension => Discovery. Allocation descendante (Mastery, Application,
  Comprehension, Discovery) : une observation n'est base positive DIRECTE
  que du plus haut stade qu'elle établit ; une claim inférieure reste direct
  seulement avec une base distincte, sinon implied_by_higher_claim (aucune
  ref recopiée). Compatible avec _validated_refs de T6-B (une observation =
  une claim). Une claim implied conserve le PÉRIMÈTRE de la claim directe
  la plus proche au-dessus d'elle (implied_from_stage) : mêmes
  represented_capability_definition_ids, competency_only_observation_ids
  et representative_pattern_names (ceux de la claim source), mais
  basis_observation_ids et structural_basis_refs vides : le périmètre est
  impliqué, aucune preuve n'est dupliquée.

Ordres canoniques : claims discovery -> mastery ; capacités dans l'ordre
pédagogique naturel de la policy résolue (C10_A < C10_B ...) ; identifiants
d'observation et d'événement triés par leur forme canonique str(UUID) ;
refs structurelles triées par (relation_kind, str(relation_id)) ;
limitations triées par code. Jamais l'ordre d'un set, d'un dict, du SQL ni
d'un horodatage.
"""
import uuid
from dataclasses import dataclass

from core.inference_policies import (
    APPLICATION,
    COMPREHENSION,
    DISCOVERY,
    MASTERY,
    InconsistentInferenceContext,
    ResolvedPositiveBasisPolicy,
    resolve_positive_basis_policy,
)
from core.inference_service import (
    BASIS_MODE_NONE,
    CLAIM_STAGES,
    DIRECT,
    ESTABLISHED,
    IMPLIED_BY_HIGHER_CLAIM,
    NOT_ESTABLISHED,
)

# Vocabulaires T3 / T5 (lus seulement).
SUPPORTIVE = "supportive"
CONTRADICTORY = "contradictory"
LOCAL_STAGE_NONE = "none"
LOCALIZED = "localized"
COMPETENCY_ONLY = "competency_only"
EVIDENCE_STRENGTHS = frozenset({"weak", "medium", "strong"})
SUPPORT_LEVELS = frozenset({"none", "hinted", "guided", "answer_given"})
ELICITATION_MODES = frozenset({"prompted", "spontaneous"})
DEPENDENT = "dependent"
PARTIALLY_DEPENDENT = "partially_dependent"
INDEPENDENCE_EVIDENCE = "independence_evidence"
TRANSFER = "transfer"
REVALIDATION = "revalidation"

# Profondeur locale maximale (§ local_stage) : claims directes qu'une
# démonstration de cette profondeur PEUT soutenir. Jamais un rang numérique.
SUPPORTABLE_CLAIMS = {
    DISCOVERY: (DISCOVERY,),
    COMPREHENSION: (DISCOVERY, COMPREHENSION),
    APPLICATION: (DISCOVERY, COMPREHENSION, APPLICATION),
}
POSITIVE_LOCAL_STAGES = frozenset(SUPPORTABLE_CLAIMS)
# Une dépendance T5 (même partielle) sur un périmètre : la démonstration
# n'est pas une contribution autonome de ce périmètre.
DEPENDENCY_LIMITING = frozenset({DEPENDENT, PARTIALLY_DEPENDENT})
# Autonomie (Mastery seulement) : une démonstration où la réponse
# essentielle a été fournie ne prouve pas, seule, que le raisonnement
# appartient à l'utilisateur. Aucun autre support_level n'est exclu :
# support_level n'est ni un coefficient ni un plafond (contrat T3).
ANSWER_GIVEN = "answer_given"

# Statuts / modes (vocabulaire T6-B).
CLAIM_STATUSES = frozenset({ESTABLISHED, NOT_ESTABLISHED})
BASIS_MODES = frozenset({DIRECT, IMPLIED_BY_HIGHER_CLAIM, BASIS_MODE_NONE})

# basis_kind (fermé).
SINGLE_REPRESENTATIVE_DEMONSTRATION = "single_representative_demonstration"
SINGLE_REPRESENTATIVE_EPISODE = "single_representative_episode"
COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION = "competency_only_representative_demonstration"
COMPLEMENTARY_REPRESENTATIVE_HISTORY = "complementary_representative_history"
LONGITUDINAL_MASTERY_HISTORY = "longitudinal_mastery_history"
BASIS_KIND_NONE = "none"
BASIS_KINDS = (SINGLE_REPRESENTATIVE_DEMONSTRATION, SINGLE_REPRESENTATIVE_EPISODE,
               COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION, COMPLEMENTARY_REPRESENTATIVE_HISTORY,
               LONGITUDINAL_MASTERY_HISTORY, BASIS_KIND_NONE)

# representativeness_reason (fermé).
IMPLIED_REASON = IMPLIED_BY_HIGHER_CLAIM
INSUFFICIENT_POSITIVE_BASIS = "insufficient_positive_basis"
LOCALIZED_SCOPE_NOT_REPRESENTATIVE = "localized_scope_not_representative"
INSUFFICIENT_LONGITUDINAL_STRUCTURE = "insufficient_longitudinal_structure"
REPRESENTATIVE_APPLICATION_NOT_ESTABLISHED = "representative_application_not_established"
LONGITUDINAL_SCOPE_NOT_REPRESENTATIVE = "longitudinal_scope_not_representative"
INDEPENDENCE_NOT_ESTABLISHED = "independence_not_established"
UNSUPPORTED_TAXONOMY_SEMANTICS = "unsupported_taxonomy_semantics"
REASON_CODES = frozenset({SINGLE_REPRESENTATIVE_DEMONSTRATION, SINGLE_REPRESENTATIVE_EPISODE,
                          COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION, COMPLEMENTARY_REPRESENTATIVE_HISTORY,
                          LONGITUDINAL_MASTERY_HISTORY, IMPLIED_REASON, INSUFFICIENT_POSITIVE_BASIS,
                          LOCALIZED_SCOPE_NOT_REPRESENTATIVE, INSUFFICIENT_LONGITUDINAL_STRUCTURE,
                          REPRESENTATIVE_APPLICATION_NOT_ESTABLISHED, LONGITUDINAL_SCOPE_NOT_REPRESENTATIVE,
                          INDEPENDENCE_NOT_ESTABLISHED})

# Limitations (fermé) : limites de la base, jamais des faiblesses utilisateur.
DEPENDENCY_LIMITED_DEMONSTRATION = "dependency_limited_demonstration"
COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE = "competency_only_scope_not_localizable"
LOCALIZED_SCOPE_LIMITATION = LOCALIZED_SCOPE_NOT_REPRESENTATIVE
RELATION_SCOPE_NOT_ATTACHED = "relation_scope_not_attached_to_positive_scope"
LIMITATION_CODES = frozenset({INDEPENDENCE_NOT_ESTABLISHED, DEPENDENCY_LIMITED_DEMONSTRATION,
                              COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE, LOCALIZED_SCOPE_LIMITATION,
                              RELATION_SCOPE_NOT_ATTACHED,
                              LONGITUDINAL_SCOPE_NOT_REPRESENTATIVE})

# Mastery.
MASTERY_ASSESSMENT_SCHEMA_VERSION = "mastery-assessment-v1"
SUPPORTED = "supported"
NOT_DEMONSTRATED = "not_demonstrated"
MASTERY_PROPERTIES = ("autonomy", "independent_repetition", "variety_transfer", "longitudinality",
                      "robustness_revision")
T5_TRANSFER_REMOBILIZATION = "t5_transfer_remobilization"
T5_REVALIDATION_REMOBILIZATION = "t5_revalidation_remobilization"
T5_DEMONSTRATED_COGNITIVE_VARIATION = "t5_demonstrated_cognitive_variation"
T5_DURABILITY_EVIDENCE = "t5_durability_evidence"
T5_REVALIDATION = "t5_revalidation"
T5_TRANSFER_ADAPTATION = "t5_transfer_adaptation"
THESIS_REVISION_DEMONSTRATED = "thesis_revision_demonstrated"
AUTONOMOUS_REMOBILIZED_REASONING = "autonomous_remobilized_reasoning"
NO_T5_INDEPENDENT_REMOBILIZATION = "no_t5_independent_remobilization"
NO_T5_COGNITIVE_VARIATION = "no_t5_demonstrated_cognitive_variation"
NO_T5_DURABILITY_EVIDENCE = "no_t5_durability_evidence"
NO_REVISION_OR_ADAPTATION = "no_revision_or_adaptation_structure"
AUTONOMY_NOT_ON_REPRESENTATIVE_SCOPE = "autonomy_not_demonstrated_on_representative_scope"
MASTERY_REASON_CODES = frozenset({
    T5_TRANSFER_REMOBILIZATION, T5_REVALIDATION_REMOBILIZATION, T5_DEMONSTRATED_COGNITIVE_VARIATION,
    T5_DURABILITY_EVIDENCE, T5_REVALIDATION, T5_TRANSFER_ADAPTATION, THESIS_REVISION_DEMONSTRATED,
    AUTONOMOUS_REMOBILIZED_REASONING, NO_T5_INDEPENDENT_REMOBILIZATION, NO_T5_COGNITIVE_VARIATION,
    NO_T5_DURABILITY_EVIDENCE, NO_REVISION_OR_ADAPTATION, AUTONOMY_NOT_ON_REPRESENTATIVE_SCOPE})

# Unité de périmètre « compétence entière » (preuve competency_only).
COMPETENCY_UNIT = None


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class DemonstrationScope:
    """Lecture T5 (dependency_profile) d'UN périmètre d'une démonstration :
    une capacité (definition_id) ou la compétence entière (None)."""
    capability_definition_id: uuid.UUID | None
    classification: str


@dataclass(frozen=True, kw_only=True)
class PositiveDemonstration:
    """Une observation supportive de profondeur positive, projetée depuis le
    dossier T5 courant : ses seules propriétés structurées. Une observation
    multi-capacités reste UNE démonstration."""
    observation_id: uuid.UUID
    event_id: uuid.UUID
    local_stage: str
    evidence_strength: str
    elicitation_mode: str
    support_level: str
    capability_localization: str
    capability_definition_ids: tuple
    scope_readings: tuple


@dataclass(frozen=True, kw_only=True)
class PositiveEpisode:
    """Démonstrations d'UN CognitiveEvent : elles peuvent élargir la
    couverture, jamais créer entre elles répétition indépendante, transfert,
    variété ni durabilité."""
    event_id: uuid.UUID
    demonstrations: tuple
    capability_definition_ids: tuple


@dataclass(frozen=True, kw_only=True)
class PositiveEvidence:
    demonstrations: tuple
    episodes: tuple


@dataclass(frozen=True, kw_only=True)
class StructuralRef:
    """Relation T5 citée comme structure (jamais une preuve +1)."""
    relation_kind: str
    relation_id: uuid.UUID


@dataclass(frozen=True, kw_only=True)
class ClaimLimitation:
    """Limite de la base d'une claim (code fermé), avec les observations,
    patterns et limites doctrinales concernés."""
    code: str
    observation_ids: tuple = ()
    pattern_names: tuple = ()
    generalization_limits: tuple = ()


@dataclass(frozen=True, kw_only=True)
class DirectClaimAssessment:
    """Évaluation DIRECTE d'une claim (avant implication), sur les
    démonstrations non allouées à une claim supérieure."""
    stage: str
    status: str
    basis_kind: str
    observation_ids: tuple
    capability_definition_ids: tuple
    competency_only_observation_ids: tuple
    structural_refs: tuple
    representative_pattern_names: tuple
    representativeness_reason: str
    limitations: tuple


@dataclass(frozen=True, kw_only=True)
class MasteryPropertyAssessment:
    status: str
    observation_ids: tuple
    structural_refs: tuple
    reason_codes: tuple
    limitations: tuple


@dataclass(frozen=True, kw_only=True)
class MasteryAssessment:
    """Cinq propriétés qualitatives, jamais agrégées en compte, score ni
    niveau. Mastery exige les cinq ; aucune n'est « presque »."""
    schema_version: str
    autonomy: MasteryPropertyAssessment
    independent_repetition: MasteryPropertyAssessment
    variety_transfer: MasteryPropertyAssessment
    longitudinality: MasteryPropertyAssessment
    robustness_revision: MasteryPropertyAssessment
    represented_capability_definition_ids: tuple
    limitations: tuple


@dataclass(frozen=True, kw_only=True)
class PositiveBasisClaim:
    """implied_from_stage : pour une claim implied_by_higher_claim, la claim
    DIRECTE la plus proche au-dessus d'elle, dont elle reprend le périmètre
    représenté (capacités, competency_only, patterns) sans aucune
    observation de base ni ref structurelle ; None sinon."""
    stage: str
    status: str
    basis_mode: str
    implied_from_stage: str | None
    basis_observation_ids: tuple
    represented_capability_definition_ids: tuple
    competency_only_observation_ids: tuple
    structural_basis_refs: tuple
    representative_pattern_names: tuple
    representativeness_reason: str
    limitations: tuple
    mastery_assessment: MasteryAssessment | None


@dataclass(frozen=True, kw_only=True)
class PositiveBasisAssessment:
    """Sortie interne de T6-C1 : quatre claims (discovery -> mastery) ;
    highest_established_stage None si aucune (non_etabli n'est jamais une
    claim). Aucun current_stage : T6-C2."""
    policy_version: str
    competency_code: str
    claims: tuple
    highest_established_stage: str | None


# --------------------------------------------------------------------------
# Ordres canoniques
# --------------------------------------------------------------------------

def _sorted_ids(ids) -> tuple:
    return tuple(sorted(frozenset(ids), key=str))


def _sorted_refs(refs) -> tuple:
    return tuple(sorted(frozenset(refs), key=lambda ref: (ref.relation_kind, str(ref.relation_id))))


def _sorted_limitations(limitations) -> tuple:
    return tuple(sorted(frozenset(limitations), key=lambda item: (item.code, [str(i) for i in item.observation_ids],
                                                                 item.pattern_names)))


# --------------------------------------------------------------------------
# PositiveEvidenceProjector
# --------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InconsistentInferenceContext(message)


def project_positive_evidence(context, policy: ResolvedPositiveBasisPolicy) -> PositiveEvidence:
    """Projection pure du dossier T5 courant vers les démonstrations
    positives candidates, groupées par CognitiveEvent. contradictory est
    ignorée AVANT toute lecture ; supportive none n'est jamais projetée.
    Une démonstration localisée hors de la taxonomie résolue, d'une autre
    compétence ou de vocabulaire inconnu => InconsistentInferenceContext."""
    dossier = context.longitudinal_dossier
    known = frozenset(policy.definition_ids)
    codes = {c.definition_id: (c.capability_code, c.semantic_revision) for c in policy.capabilities}
    readings = {}
    for reading in dossier.dependency_profile.scope_readings:
        unit = COMPETENCY_UNIT if reading.unit.capability is None else reading.unit.capability.definition_id
        readings.setdefault(reading.observation_id, {})[unit] = reading.classification
    demonstrations = []
    for observation in dossier.active_history.observations:
        if observation.polarity == CONTRADICTORY:
            continue
        _require(observation.polarity == SUPPORTIVE, f"polarité {observation.polarity!r} inconnue")
        _require(observation.competency_code == policy.competency_code,
                 f"observation {observation.observation_id} de {observation.competency_code} dans un dossier"
                 f" {policy.competency_code} (jamais de traversée de compétence)")
        if observation.local_stage == LOCAL_STAGE_NONE:
            continue
        _require(observation.local_stage in POSITIVE_LOCAL_STAGES,
                 f"local_stage {observation.local_stage!r} inconnu (mastery n'est jamais local)")
        _require(observation.evidence_strength in EVIDENCE_STRENGTHS and observation.support_level in SUPPORT_LEVELS
                 and observation.elicitation_mode in ELICITATION_MODES,
                 f"observation {observation.observation_id} : vocabulaire T3 inconnu")
        refs = observation.compatible_capabilities
        if observation.capability_localization == LOCALIZED:
            _require(bool(refs), f"observation {observation.observation_id} localized sans capacité")
            for ref in refs:
                _require(ref.definition_id in known and codes[ref.definition_id] == (ref.capability_code,
                                                                                    ref.semantic_revision),
                         f"observation {observation.observation_id} : capacité {ref.capability_code} hors de la"
                         " taxonomie résolue")
            units = [ref.definition_id for ref in refs]
        else:
            _require(observation.capability_localization == COMPETENCY_ONLY and not refs,
                     f"observation {observation.observation_id} : localisation incohérente")
            units = [COMPETENCY_UNIT]
        observed = readings.get(observation.observation_id, {})
        _require(all(unit in observed for unit in units),
                 f"observation {observation.observation_id} : lecture T5 de périmètre absente")
        demonstrations.append(PositiveDemonstration(
            observation_id=observation.observation_id,
            event_id=observation.event_id,
            local_stage=observation.local_stage,
            evidence_strength=observation.evidence_strength,
            elicitation_mode=observation.elicitation_mode,
            support_level=observation.support_level,
            capability_localization=observation.capability_localization,
            capability_definition_ids=policy.ordered(r.definition_id for r in refs),
            scope_readings=tuple(DemonstrationScope(capability_definition_id=unit,
                                                    classification=observed[unit])
                                 for unit in (policy.ordered(units) if refs else units)),
        ))
    demonstrations.sort(key=lambda d: str(d.observation_id))
    grouped = {}
    for demonstration in demonstrations:
        grouped.setdefault(demonstration.event_id, []).append(demonstration)
    episodes = tuple(PositiveEpisode(
        event_id=event_id, demonstrations=tuple(grouped[event_id]),
        capability_definition_ids=policy.ordered(i for item in grouped[event_id]
                                                 for i in item.capability_definition_ids))
        for event_id in sorted(grouped, key=str))
    return PositiveEvidence(demonstrations=tuple(demonstrations), episodes=episodes)


# --------------------------------------------------------------------------
# Direct Claim Evaluator
# --------------------------------------------------------------------------

def _independent_ids(demonstration: PositiveDemonstration) -> frozenset:
    """Capacités sur lesquelles T5 ÉTABLIT l'indépendance de la
    démonstration (independence_evidence : cible d'un transfert ou d'une
    revalidation sur ce périmètre). not_established n'en fait jamais partie :
    une absence d'arête n'est pas une indépendance ; dependent /
    partially_dependent non plus."""
    return frozenset(s.capability_definition_id for s in demonstration.scope_readings
                     if s.capability_definition_id is not None and s.classification == INDEPENDENCE_EVIDENCE)


def _units_of(demonstration: PositiveDemonstration) -> frozenset:
    """Périmètre positif d'une démonstration : ses capacités, ou la
    compétence entière (competency_only)."""
    return frozenset(demonstration.capability_definition_ids) or frozenset({COMPETENCY_UNIT})


def _codes(*pairs) -> list:
    """Codes fermés dont la condition qualitative est vraie."""
    return [code for code, present in pairs if present]


def _dependency_limited(demonstration: PositiveDemonstration) -> bool:
    return any(s.classification in DEPENDENCY_LIMITING for s in demonstration.scope_readings)


def _limited_on(demonstration: PositiveDemonstration, unit) -> bool:
    """Dépendance T5 (même partielle) sur CE périmètre de la démonstration."""
    return any(s.capability_definition_id == unit and s.classification in DEPENDENCY_LIMITING
               for s in demonstration.scope_readings)


def _touched(pattern, demonstration: PositiveDemonstration) -> frozenset:
    """Capacités de la démonstration qui participent à la relation."""
    return frozenset(demonstration.capability_definition_ids) & (
        pattern.core_definition_ids | pattern.related_definition_ids)


def _complementary(pattern, localized, covered_ids):
    """{event_id: démonstrations contributives} des épisodes distincts qui
    touchent la relation sans la couvrir seuls (covered_ids : périmètre
    retenu par démonstration). Répétition redondante d'une même dimension :
    jamais une complémentarité."""
    per_event = {}
    for demonstration in localized:
        per_event.setdefault(demonstration.event_id, []).append(demonstration)
    partial = {}
    for event_id, items in per_event.items():
        retained = frozenset(i for item in items for i in covered_ids(item))
        if not pattern.satisfied_by(retained) and pattern.touched_by(retained):
            partial[event_id] = [item for item in items if pattern.touched_by(covered_ids(item))]
    covered = frozenset(i for items in partial.values() for item in items for i in covered_ids(item))
    return partial if pattern.satisfied_by(covered) else {}


def _direct_claim(stage: str, evidence: PositiveEvidence, policy: ResolvedPositiveBasisPolicy,
                  allocated: frozenset) -> DirectClaimAssessment:
    """Claim directe évaluée pour elle-même sur les démonstrations de
    profondeur suffisante non allouées à une claim supérieure : A
    (démonstration / épisode / competency_only directement représentatif),
    sinon B (histoire complémentaire à indépendance établie par T5). Aucune
    condition sur evidence_strength. Voir la docstring du module."""
    stage_policy = policy.stage_policies[stage]
    eligible = [d for d in evidence.demonstrations
                if d.observation_id not in allocated and stage in SUPPORTABLE_CLAIMS[d.local_stage]]
    localized = [d for d in eligible if d.capability_localization == LOCALIZED]
    contributions = {kind: set() for kind in BASIS_KINDS}
    patterns = set()

    # A.1 — une démonstration directement représentative.
    for pattern in stage_policy.representative_patterns:
        for demonstration in localized:
            if pattern.satisfied_by(demonstration.capability_definition_ids):
                contributions[SINGLE_REPRESENTATIVE_DEMONSTRATION].add(demonstration.observation_id)
                patterns.add(pattern.name)
    # A.2 — observations sœurs d'un même épisode qui, ensemble, couvrent la
    # relation (aucune n'y suffisant seule).
    by_event = {}
    for demonstration in localized:
        by_event.setdefault(demonstration.event_id, []).append(demonstration)
    for items in by_event.values():
        covered = frozenset(i for item in items for i in item.capability_definition_ids)
        for pattern in stage_policy.representative_patterns:
            if pattern.satisfied_by(covered) and not any(pattern.satisfied_by(item.capability_definition_ids)
                                                         for item in items):
                contributions[SINGLE_REPRESENTATIVE_EPISODE].update(
                    item.observation_id for item in items if pattern.touched_by(item.capability_definition_ids))
                patterns.add(pattern.name)
    # A.3 — competency_only (jamais de capacités inventées, jamais
    # d'accumulation : aucune dimension, donc jamais complémentaire).
    if stage_policy.competency_only_allowed:
        contributions[COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION].update(
            d.observation_id for d in eligible if d.capability_localization == COMPETENCY_ONLY)

    direct = any(contributions[kind] for kind in (SINGLE_REPRESENTATIVE_DEMONSTRATION, SINGLE_REPRESENTATIVE_EPISODE,
                                                  COMPETENCY_ONLY_REPRESENTATIVE_DEMONSTRATION))
    # B — histoire complémentaire : relation couverte par des épisodes
    # distincts dont aucun ne la couvre seul, chaque contribution étant
    # INDÉPENDANTE selon T5 sur le périmètre qu'elle apporte.
    if not direct:
        for pattern in stage_policy.representative_patterns:
            if not pattern.related_definition_ids:
                continue
            partial = _complementary(pattern, localized, _independent_ids)
            if partial:
                contributions[COMPLEMENTARY_REPRESENTATIVE_HISTORY].update(
                    item.observation_id for items in partial.values() for item in items)
                patterns.add(pattern.name)

    kind = next((k for k in BASIS_KINDS if contributions[k]), BASIS_KIND_NONE)
    if kind == BASIS_KIND_NONE:
        return _not_established(stage, stage_policy, eligible, localized)
    basis = frozenset(i for k in BASIS_KINDS for i in contributions[k])
    used = [d for d in eligible if d.observation_id in basis]
    limitations = []
    competency_only_ids = [d.observation_id for d in used if d.capability_localization == COMPETENCY_ONLY]
    if competency_only_ids:
        limitations.append(ClaimLimitation(code=COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE,
                                           observation_ids=_sorted_ids(competency_only_ids)))
    limited = [d.observation_id for d in used if _dependency_limited(d)]
    if limited:
        limitations.append(ClaimLimitation(code=DEPENDENCY_LIMITED_DEMONSTRATION, observation_ids=_sorted_ids(limited)))
    return DirectClaimAssessment(
        stage=stage,
        status=ESTABLISHED,
        basis_kind=kind,
        observation_ids=_sorted_ids(basis),
        capability_definition_ids=policy.ordered(i for d in used for i in d.capability_definition_ids),
        competency_only_observation_ids=_sorted_ids(competency_only_ids),
        structural_refs=(),
        representative_pattern_names=tuple(sorted(patterns)),
        representativeness_reason=kind,
        limitations=_sorted_limitations(limitations),
    )


def _not_established(stage, stage_policy, eligible, localized) -> DirectClaimAssessment:
    """Explication fermée d'une claim directe non établie : aucune
    démonstration de profondeur suffisante ; complémentarité sémantique
    réelle entre épisodes distincts mais indépendance non établie par T5
    (independence_not_established : jamais une fraction) ; ou périmètre trop
    local (patterns étroits et limites doctrinales de la policy)."""
    limitations, reason = [], INSUFFICIENT_POSITIVE_BASIS
    if eligible:
        complementary = [(p, _complementary(p, localized, lambda d: frozenset(d.capability_definition_ids)))
                         for p in stage_policy.representative_patterns if p.related_definition_ids]
        complementary = [(p, partial) for p, partial in complementary if partial]
        if complementary:
            reason = INDEPENDENCE_NOT_ESTABLISHED
            limitations.append(ClaimLimitation(
                code=INDEPENDENCE_NOT_ESTABLISHED,
                observation_ids=_sorted_ids(
                    item.observation_id for p, partial in complementary for items in partial.values()
                    for item in items if not _touched(p, item) <= _independent_ids(item)),
                pattern_names=tuple(sorted(p.name for p, _ in complementary))))
        else:
            covered = frozenset(i for d in localized for i in d.capability_definition_ids)
            reason = LOCALIZED_SCOPE_NOT_REPRESENTATIVE
            limitations.append(ClaimLimitation(
                code=LOCALIZED_SCOPE_LIMITATION,
                observation_ids=_sorted_ids(d.observation_id for d in localized),
                pattern_names=tuple(sorted(p.name for p in stage_policy.narrow_scope_patterns
                                           if p.satisfied_by(covered))),
                generalization_limits=tuple(stage_policy.generalization_limits)))
    return DirectClaimAssessment(
        stage=stage, status=NOT_ESTABLISHED, basis_kind=BASIS_KIND_NONE, observation_ids=(),
        capability_definition_ids=(), competency_only_observation_ids=(), structural_refs=(),
        representative_pattern_names=(), representativeness_reason=reason,
        limitations=_sorted_limitations(limitations))


# --------------------------------------------------------------------------
# Mastery Engine
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class _Remobilization:
    """Relation T5 de remobilisation (transfert ou revalidation) dont la
    cible est une démonstration positive d'Application, rattachée à son
    périmètre positif (units : definition_id, ou COMPETENCY_UNIT)."""
    ref: StructuralRef
    source_observation_id: uuid.UUID | None
    target: PositiveDemonstration
    units: frozenset


def _relation_units(scope, target: PositiveDemonstration, policy: ResolvedPositiveBasisPolicy) -> frozenset:
    known = frozenset(policy.definition_ids)
    scoped = frozenset(ref.definition_id for ref in scope.capabilities)
    _require(scoped <= known, "relation T5 localisée hors de la taxonomie résolue")
    if target.capability_localization == COMPETENCY_ONLY:
        return frozenset({COMPETENCY_UNIT})
    return scoped & frozenset(target.capability_definition_ids)


def _represents(units: frozenset, policy: ResolvedPositiveBasisPolicy) -> bool:
    application = policy.stage_policies[APPLICATION]
    return any(p.satisfied_by(units) for p in application.representative_patterns) or (
        application.competency_only_allowed and COMPETENCY_UNIT in units)


def _property(supported: bool, observation_ids, refs, reasons, failure: str) -> MasteryPropertyAssessment:
    return MasteryPropertyAssessment(
        status=SUPPORTED if supported else NOT_DEMONSTRATED,
        observation_ids=_sorted_ids(observation_ids) if supported else (),
        structural_refs=_sorted_refs(refs) if supported else (),
        reason_codes=tuple(sorted(frozenset(reasons))) if supported else (failure,),
        limitations=())


def _mastery(evidence: PositiveEvidence, dossier, policy: ResolvedPositiveBasisPolicy, application_established: bool):
    """(DirectClaimAssessment de Mastery, MasteryAssessment). Lit
    uniquement les relations T5 établies ; jamais un horodatage, un nombre
    d'observations ni une absence d'arête. Base (si établie) : extrémités
    positives des relations citées et démonstrations de révision, jamais la
    contradiction source d'une revalidation."""
    positives = {d.observation_id: d for d in evidence.demonstrations}
    remobilizations, detached = [], []

    def attach(kind, relation_id, source_id, target_id, scope):
        target = positives.get(target_id)
        if target is None or target.local_stage != APPLICATION:
            return
        if kind == TRANSFER and source_id not in positives:
            return
        units = _relation_units(scope, target, policy)
        ref = StructuralRef(relation_kind=kind, relation_id=relation_id)
        if not units:
            detached.append(ref)
            return
        remobilizations.append(_Remobilization(ref=ref, source_observation_id=source_id, target=target, units=units))

    for t in dossier.transfer_profile.transfers:
        attach(TRANSFER, t.relation_id, t.source_observation_id, t.target_observation_id, t.scope)
    for r in dossier.consistency_profile.historical_revalidations:
        attach(REVALIDATION, r.relation_id, None, r.target_supportive_observation_id, r.scope)

    def endpoints(items):
        return [i for item in items for i in (item.source_observation_id, item.target.observation_id)
                if i is not None]

    transfers = [m for m in remobilizations if m.ref.relation_kind == TRANSFER]
    revalidations = [m for m in remobilizations if m.ref.relation_kind == REVALIDATION]

    # independent_repetition : remobilisation T5 d'une démonstration positive
    # dans un épisode distinct (transfert), ou revalidation dont la cible
    # (remobilisation indépendante établie par T5) porte un périmètre aussi
    # démontré en Application dans un AUTRE épisode. Jamais le même
    # événement, jamais une absence d'arête, aucun ordre temporel.
    def other_episode_application(m):
        return [d.observation_id for d in evidence.demonstrations if d.local_stage == APPLICATION
                and d.event_id != m.target.event_id and not m.units.isdisjoint(_units_of(d))]

    repeated = [m for m in revalidations if other_episode_application(m)]
    repetition = _property(
        bool(transfers or repeated),
        [*endpoints(transfers), *endpoints(repeated), *(i for m in repeated for i in other_episode_application(m))],
        [m.ref for m in (*transfers, *repeated)],
        _codes((T5_TRANSFER_REMOBILIZATION, transfers), (T5_REVALIDATION_REMOBILIZATION, repeated)),
        NO_T5_INDEPENDENT_REMOBILIZATION)

    # variety_transfer : variation cognitive positivement établie par T5
    # (descripteurs nominaux jamais suffisants).
    variations = frozenset(v.transfer_id for v in dossier.variety_profile.demonstrated_cognitive_variation)
    varied = [m for m in transfers if m.ref.relation_id in variations]
    variety = _property(bool(varied), endpoints(varied), [m.ref for m in varied],
                        [T5_DEMONSTRATED_COGNITIVE_VARIATION], NO_T5_COGNITIVE_VARIATION)

    # longitudinality : remobilisation entre épisodes distincts attestée par
    # la durability evidence T5 (aucune durée, aucun âge, aucun horodatage).
    durable_refs = frozenset((e.relation_kind, e.relation_id)
                             for e in dossier.temporal_validation_profile.durability_evidence_events
                             if e.source_event_id != e.target_event_id)
    durable = [m for m in remobilizations if (m.ref.relation_kind, m.ref.relation_id) in durable_refs]
    longitudinality = _property(bool(durable), endpoints(durable), [m.ref for m in durable],
                                [T5_DURABILITY_EVIDENCE], NO_T5_DURABILITY_EVIDENCE)

    # robustness_revision : revalidation, adaptation (transfert) ou révision
    # rationnelle démontrée sur une capacité de révision de la policy (C11_D).
    # Aucune contradiction préalable exigée.
    revisions = [d for d in evidence.demonstrations
                 if d.local_stage == APPLICATION
                 and not policy.revision_definition_ids.isdisjoint(d.capability_definition_ids)]
    robustness = _property(
        bool(revalidations or transfers or revisions),
        [*endpoints(revalidations), *endpoints(transfers), *(d.observation_id for d in revisions)],
        [m.ref for m in (*revalidations, *transfers)],
        _codes((T5_REVALIDATION, revalidations), (T5_TRANSFER_ADAPTATION, transfers),
               (THESIS_REVISION_DEMONSTRATED, revisions)),
        NO_REVISION_OR_ADAPTATION)

    # autonomy : le périmètre remobilisé REPRÉSENTATIF appartient à
    # l'utilisateur d'après les faits structurés : aucune dépendance T5 (même
    # partielle) d'une extrémité positive de la relation sur ce périmètre, et
    # une cible qui n'est pas answer_given (une réponse fournie ne prouve
    # pas, seule, l'autonomie). guided / hinted ne sont jamais un plafond.
    def autonomous_units(m):
        if m.target.support_level == ANSWER_GIVEN:
            return frozenset()
        ends = [positives[i] for i in (m.source_observation_id, m.target.observation_id) if i is not None]
        return frozenset(u for u in m.units if not any(_limited_on(end, u) for end in ends))

    autonomous = [m for m in remobilizations if autonomous_units(m)]
    autonomy = _property(_represents(frozenset(u for m in autonomous for u in autonomous_units(m)), policy),
                         endpoints(autonomous), [m.ref for m in autonomous], [AUTONOMOUS_REMOBILIZED_REASONING],
                         AUTONOMY_NOT_ON_REPRESENTATIVE_SCOPE)

    units = frozenset(u for m in remobilizations for u in m.units)
    properties = (autonomy, repetition, variety, longitudinality, robustness)
    limitations = []
    if detached:
        limitations.append(ClaimLimitation(code=RELATION_SCOPE_NOT_ATTACHED))
    if COMPETENCY_UNIT in units:
        limitations.append(ClaimLimitation(code=COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE, observation_ids=_sorted_ids(
            m.target.observation_id for m in remobilizations if COMPETENCY_UNIT in m.units)))
    representative = _represents(units, policy)
    if remobilizations and not representative:
        limitations.append(ClaimLimitation(code=LONGITUDINAL_SCOPE_NOT_REPRESENTATIVE))
    assessment = MasteryAssessment(
        schema_version=MASTERY_ASSESSMENT_SCHEMA_VERSION,
        autonomy=autonomy,
        independent_repetition=repetition,
        variety_transfer=variety,
        longitudinality=longitudinality,
        robustness_revision=robustness,
        represented_capability_definition_ids=policy.ordered(u for u in units if u is not COMPETENCY_UNIT),
        limitations=_sorted_limitations(limitations),
    )
    all_supported = all(p.status == SUPPORTED for p in properties)
    established = application_established and all_supported and representative
    if established:
        reason = LONGITUDINAL_MASTERY_HISTORY
    elif not application_established:
        reason = REPRESENTATIVE_APPLICATION_NOT_ESTABLISHED
    elif remobilizations and not representative:
        reason = LONGITUDINAL_SCOPE_NOT_REPRESENTATIVE
    else:
        reason = INSUFFICIENT_LONGITUDINAL_STRUCTURE
    basis = frozenset(i for p in properties for i in p.observation_ids) if established else frozenset()
    refs = frozenset(r for p in properties for r in p.structural_refs) if established else frozenset()
    competency_only = [i for i in basis if positives[i].capability_localization == COMPETENCY_ONLY]
    return DirectClaimAssessment(
        stage=MASTERY,
        status=ESTABLISHED if established else NOT_ESTABLISHED,
        basis_kind=LONGITUDINAL_MASTERY_HISTORY if established else BASIS_KIND_NONE,
        observation_ids=_sorted_ids(basis),
        capability_definition_ids=assessment.represented_capability_definition_ids if established else (),
        competency_only_observation_ids=_sorted_ids(competency_only),
        structural_refs=_sorted_refs(refs),
        representative_pattern_names=tuple(sorted(
            p.name for p in policy.stage_policies[APPLICATION].representative_patterns if p.satisfied_by(units)))
        if established else (),
        representativeness_reason=reason,
        limitations=assessment.limitations,
    ), assessment


# --------------------------------------------------------------------------
# Claim Implication Resolver + PositiveBasisRefAllocator
# --------------------------------------------------------------------------

def _claim(assessment: DirectClaimAssessment, mode: str, mastery: MasteryAssessment | None,
           source: DirectClaimAssessment | None = None) -> PositiveBasisClaim:
    """Claim de sortie. implied_by_higher_claim : le périmètre représenté de
    `source` (claim directe la plus proche au-dessus) est repris tel quel ;
    basis_observation_ids et structural_basis_refs restent vides (aucune
    preuve dupliquée, aucune ref positive_basis recopiée)."""
    if mode == IMPLIED_BY_HIGHER_CLAIM:
        return PositiveBasisClaim(
            stage=assessment.stage, status=ESTABLISHED, basis_mode=IMPLIED_BY_HIGHER_CLAIM,
            implied_from_stage=source.stage, basis_observation_ids=(),
            represented_capability_definition_ids=source.capability_definition_ids,
            competency_only_observation_ids=source.competency_only_observation_ids, structural_basis_refs=(),
            representative_pattern_names=source.representative_pattern_names,
            representativeness_reason=IMPLIED_REASON, limitations=(), mastery_assessment=mastery)
    return PositiveBasisClaim(
        stage=assessment.stage,
        status=assessment.status,
        basis_mode=mode,
        implied_from_stage=None,
        basis_observation_ids=assessment.observation_ids,
        represented_capability_definition_ids=assessment.capability_definition_ids,
        competency_only_observation_ids=assessment.competency_only_observation_ids,
        structural_basis_refs=assessment.structural_refs,
        representative_pattern_names=assessment.representative_pattern_names,
        representativeness_reason=assessment.representativeness_reason,
        limitations=assessment.limitations,
        mastery_assessment=mastery,
    )


def evaluate_positive_basis(context) -> PositiveBasisAssessment:
    """Base positive des quatre claims pour le contexte (fonction pure).

    1. résolution de la policy (version, format V1, taxonomie courante :
       fail closed) ; 2. projection des démonstrations positives ;
    3. Application directe sur toutes les démonstrations, puis Mastery
       (exige une Application représentative établie) ;
    4. allocation descendante : les observations de base de Mastery, puis
       d'Application, puis de Comprehension sont retirées des claims
       inférieures, réévaluées directement sur le reste (base distincte =>
       direct) ; 5. implication : une claim sans base directe distincte sous
       une claim supérieure établie est implied_by_higher_claim et reprend
       le périmètre de la claim directe la plus proche au-dessus d'elle."""
    policy = resolve_positive_basis_policy(context)
    evidence = project_positive_evidence(context, policy)
    application = _direct_claim(APPLICATION, evidence, policy, frozenset())
    mastery_direct, mastery = _mastery(evidence, context.longitudinal_dossier, policy,
                                       application.status == ESTABLISHED)
    direct = {MASTERY: mastery_direct}
    allocated = frozenset(mastery_direct.observation_ids)
    for stage in (APPLICATION, COMPREHENSION, DISCOVERY):
        direct[stage] = _direct_claim(stage, evidence, policy, allocated)
        allocated = allocated | frozenset(direct[stage].observation_ids)
    claims, source = {}, None
    for stage in reversed(CLAIM_STAGES):
        assessment = direct[stage]
        attached = mastery if stage == MASTERY else None
        if assessment.status == ESTABLISHED:
            claims[stage] = _claim(assessment, DIRECT, attached)
            source = assessment
        elif source is not None:
            claims[stage] = _claim(assessment, IMPLIED_BY_HIGHER_CLAIM, attached, source)
        else:
            claims[stage] = _claim(assessment, BASIS_MODE_NONE, attached)
    ordered = tuple(claims[stage] for stage in CLAIM_STAGES)
    return PositiveBasisAssessment(
        policy_version=policy.policy_version,
        competency_code=policy.competency_code,
        claims=ordered,
        highest_established_stage=next((c.stage for c in reversed(ordered) if c.status == ESTABLISHED), None),
    )
