"""Moteur d'inférence pédagogique T6-C V1 : point d'entrée public final.

    InferenceContext (T6-B start / get_inference_context)
        -> FinalInferencePolicyResolver      (combinaison complète des versions)
        -> evaluate_positive_basis           (T6-C1)
        -> evaluate_inference_state          (T6-C2)
        -> resolve_transition_cause          (T6-C3, core/inference_transition.py)
        -> evaluate_validation_needs         (T6-C3, core/inference_validation.py)
        -> TaxonomyMembershipResolver        (T6-C3, ici)
        -> DecisionAssembler                 (T6-C3, ici)
        -> DecisionInvariantGuard            (T6-C3, ici)
        -> InferenceDecision

T6-C ne persiste rien : l'appelant transmet la décision au service
transactionnel T6-B (complétion du run candidat), qui la revalide lui-même
de façon indépendante et génère seul les UUID des lignes persistées.

T6-C3 ne recalcule rien de T6-C1 (base positive, direct / implied,
Mastery) ni de T6-C2 (confiance, tensions, compatibilité, stade, revision
context, anti-oscillation) : il attribue la cause, exprime les besoins
latents, résout les memberships et SÉRIALISE.

- TaxonomyMembershipResolver : capability_definition_id -> membership_id de
  la release COURANTE (CurrentTaxonomyContext uniquement, ordre naturel de
  ses capacités). Même definition_id = même sens ; jamais de correspondance
  par capability_code (même code, autre révision = autre capacité).
  definition_id absent ou doublon => UnresolvableCapabilityMembership. La
  primitive (current_membership_ids, core/inference_final_policies.py) est
  UNIQUE : elle résout les tensions localized et vérifie aussi, avant
  l'assemblage, les capability_definition_ids des besoins de validation
  localized (qui restent sérialisés par definition_id, sans membership).
- StageClaimDecision : exactement quatre, discovery -> mastery ;
  basis_summary / scope_summary = gabarits fermés, AUDIT SEULEMENT (aucune
  logique ne les relit) ; confidence_profile = lecture qualitative
  (confidence-profile-v2 : cinq dimensions ; par dimension, chaque fait
  T6-C2 avec SES propres capability_definition_ids, exactement ceux de
  ConfidenceFact, sans union ni reconstruction ; et les limitations ;
  jamais d'observation, de relation ni de score : la provenance passe par
  les refs ; format vérifié par check_confidence_profile_v2 avant tout
  retour). confidence-profile-v1 (fact_codes + capacités agrégées par
  dimension) est le format historique, plus jamais écrit ; la policy
  pédagogique confidence_profile-1 et les six versions de spécification
  sont inchangées : seul le format sérialisé évolue. mastery_assessment sur
  la seule claim mastery (cinq propriétés qualitatives, jamais 4/5).
- TensionDecision : clés locales t0, t1... dans l'ordre T6-C2 (aucune
  sémantique) ; revision_status revalidation_needed seulement si un besoin
  de revalidation la couvre sémantiquement, sinon unresolved.
- BasisRefDecision : positive_basis (observations de base des seules claims
  directes : l'anti-double-comptage T6-C1 est conservé) ; confidence (par
  claim et dimension, provenance T6-C2 telle quelle, y compris celle des
  claims implied) ; mastery (propriétés Mastery, dédupliquées : une relation
  qui documente plusieurs propriétés reste une seule source) ; tension ;
  transition et validation (run-level). Une relation T5 n'est jamais
  positive_basis. Aucune source absente du dossier courant.
- unresolved_revision_context = state.revision_context.to_payload() (seul
  sérialiseur de revision-context-v1).
- DecisionInvariantGuard : garde PUR et propre à T6-C (jamais le helper
  privé de T6-B) avant tout retour.

Pureté : aucune base, Session, commit, modèle, réseau, horloge, hasard ni
génération d'UUID. Même InferenceContext => même InferenceDecision.
"""
from collections.abc import Mapping

from core.inference_confidence import ConfidenceDimensionAssessment, ConfidenceFact, ConfidenceProfileAssessment
from core.inference_final_policies import (
    VALIDATION_INTENTS,
    FinalInferenceError,
    InvalidAssembledDecision,
    UnresolvableCapabilityMembership,
    check_confidence_profile_v2,
    check_validation_scope,
    current_capabilities,
    resolve_final_inference_policy,
)
from core.inference_final_policies import current_membership_ids as _membership_ids
from core.inference_positive_basis import MASTERY_PROPERTIES, ClaimLimitation, evaluate_positive_basis
from core.inference_service import (
    BASIS_MODE_NONE,
    BASIS_MODES,
    CLAIM_STAGES,
    CONFIDENCE,
    CONFIDENCE_DIMENSIONS,
    DIRECT,
    ESTABLISHED,
    EVIDENCE_INTEGRITY_CHANGE,
    IMPLIED_BY_HIGHER_CLAIM,
    LOCALIZED,
    MASTERY,
    MASTERY_REF,
    NEW_USER_EVIDENCE,
    NON_ETABLI,
    NOT_ESTABLISHED,
    PEDAGOGICAL_REINTERPRETATION,
    POSITIVE_BASIS,
    REF_ATTACHMENTS,
    REVISED_DOWN,
    REVISION_STATUSES,
    SOURCE_KINDS,
    SOURCE_OBSERVATION,
    STAGE_SEQUENCE,
    TENSION,
    TENSION_OPEN,
    TRANSITION,
    TRANSITION_CAUSES,
    VALIDATION,
    BasisRefDecision,
    InferenceDecision,
    StageClaimDecision,
    TensionDecision,
)
from core.inference_state import evaluate_inference_state
from core.inference_state_policies import CONFIDENCE_DIMENSIONS as DIMENSION_ORDER
from core.inference_state_policies import CONFIDENCE_PROFILE_SCHEMA_V2
from core.inference_transition import relation_endpoints, resolve_transition_cause
from core.inference_validation import evaluate_validation_needs, tension_revision_status

SUPPORTIVE = "supportive"
NONE_LABEL = "none"
COMPETENCY_ONLY_LABEL = "competency_only"
# Clés interdites dans tout payload JSON produit (aucun score, niveau,
# priorité, tâche ni provenance brute ; audit du DecisionInvariantGuard).
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "score", "confidence_score", "competency_score", "mastery_score", "level", "confidence_level",
    "overall_confidence", "percentage", "percent", "ratio", "average", "weight", "points", "probability",
    "priority", "high", "medium", "low", "passed", "failed", "question", "surface", "task", "tension_key",
    "observation_ids", "relation_ids", "source_ids", "structural_refs"})
_VALIDATION_NEED_KEYS = frozenset({"schema_version", "intent", "target_stage", "scope_mode",
                                   "capability_definition_ids", "reason_codes"})


# --------------------------------------------------------------------------
# TaxonomyMembershipResolver (primitive : current_membership_ids)
# --------------------------------------------------------------------------

def _scope_labels(definition_ids, taxonomy) -> str:
    """Libellé d'AUDIT « C10_B@r1,C10_D@r1 » (jamais relu par une logique)."""
    known = current_capabilities(taxonomy)
    missing = [str(d) for d in definition_ids if d not in known]
    if missing:
        raise UnresolvableCapabilityMembership(f"capability_definition_ids {missing} absents de la release courante")
    wanted = frozenset(definition_ids)
    return ",".join(f"{c.capability_code}@r{c.semantic_revision}" for c in taxonomy.capabilities
                    if c.definition_id in wanted)


# --------------------------------------------------------------------------
# DecisionAssembler
# --------------------------------------------------------------------------

def _basis_summary(claim) -> str:
    if claim.status != ESTABLISHED:
        return f"{NOT_ESTABLISHED}:{claim.representativeness_reason}"
    if claim.basis_mode == IMPLIED_BY_HIGHER_CLAIM:
        return f"{IMPLIED_BY_HIGHER_CLAIM}:{claim.implied_from_stage}"
    return f"{DIRECT}:{claim.representativeness_reason}"


def _scope_summary(claim, taxonomy) -> str:
    if claim.status != ESTABLISHED:
        return NONE_LABEL
    parts = []
    if claim.represented_capability_definition_ids:
        parts.append(f"{LOCALIZED}:{_scope_labels(claim.represented_capability_definition_ids, taxonomy)}")
    if claim.competency_only_observation_ids:
        parts.append(COMPETENCY_ONLY_LABEL)
    return ";".join(parts) or NONE_LABEL


def _limitation_codes(items) -> list:
    codes = []
    for item in items:
        code = item.code if type(item) is ClaimLimitation else item
        if type(code) is not str:
            raise InvalidAssembledDecision("limitation hors vocabulaire (code attendu)")
        if code not in codes:
            codes.append(code)
    return codes


def _check_profile(payload, taxonomy, path: str) -> None:
    """Format confidence-profile-v2 (définition unique, partagée avec 6-1A)
    contre la taxonomie courante : fail closed."""
    try:
        check_confidence_profile_v2(payload, tuple(str(d) for d in current_capabilities(taxonomy)))
    except FinalInferenceError as exc:
        raise InvalidAssembledDecision(f"{path} : {exc}") from exc


def _profile_payload(profile, taxonomy) -> dict:
    """confidence-profile-v2 persisté : la LECTURE qualitative seulement.
    Chaque fait T6-C2 reste distinct, avec EXACTEMENT ses propres
    capability_definition_ids (ConfidenceFact), dans l'ordre de sa
    dimension ; aucune union par dimension, aucune observation ni relation
    (provenance : refs confidence). Fail closed avant tout retour."""
    _fail(type(profile) is not ConfidenceProfileAssessment or profile.schema_version != CONFIDENCE_PROFILE_SCHEMA_V2,
          f"profil de confiance {CONFIDENCE_PROFILE_SCHEMA_V2} attendu")
    known = current_capabilities(taxonomy)
    payload = {"schema_version": profile.schema_version}
    for name in DIMENSION_ORDER:
        dimension = getattr(profile, name)
        _fail(type(dimension) is not ConfidenceDimensionAssessment or dimension.dimension != name,
              f"{profile.stage}.{name} : dimension inattendue")
        facts = dimension.facts
        _fail(type(facts) is not tuple or any(type(fact) is not ConfidenceFact for fact in facts),
              f"{profile.stage}.{name} : ConfidenceFact attendus")
        _fail(dimension.fact_codes != tuple(fact.code for fact in facts),
              f"{profile.stage}.{name} : fact_codes incohérents avec les faits")
        _fail(any(type(fact.capability_definition_ids) is not tuple or
                  any(i not in known for i in fact.capability_definition_ids) for fact in facts),
              f"{profile.stage}.{name} : capacité d'un fait hors de la taxonomie courante")
        payload[name] = {"facts": [{"code": fact.code,
                                    "capability_definition_ids": [str(i) for i in fact.capability_definition_ids]}
                                   for fact in facts],
                         "limitations": _limitation_codes(dimension.limitations)}
    _check_profile(payload, taxonomy, f"{profile.stage}.confidence_profile")
    return payload


def _mastery_payload(assessment) -> dict:
    payload = {"schema_version": assessment.schema_version}
    for name in MASTERY_PROPERTIES:
        item = getattr(assessment, name)
        payload[name] = {"status": item.status, "reason_codes": list(item.reason_codes),
                         "limitations": _limitation_codes(item.limitations)}
    payload["represented_capability_definition_ids"] = [str(i) for i in
                                                        assessment.represented_capability_definition_ids]
    payload["limitations"] = _limitation_codes(assessment.limitations)
    return payload


def _refs(role, observation_ids=(), structural_refs=(), **target) -> list:
    return [*(BasisRefDecision(ref_role=role, source_kind=SOURCE_OBSERVATION, source_id=i, **target)
              for i in observation_ids),
            *(BasisRefDecision(ref_role=role, source_kind=r.relation_kind, source_id=r.relation_id, **target)
              for r in structural_refs)]


def _claims_and_refs(positive_basis, state, taxonomy) -> tuple:
    claims, refs = [], []
    for claim, profile in zip(positive_basis.claims, state.confidence_profiles):
        claims.append(StageClaimDecision(
            stage=claim.stage, positive_basis_status=claim.status, basis_mode=claim.basis_mode,
            basis_summary=_basis_summary(claim), scope_summary=_scope_summary(claim, taxonomy),
            confidence_profile=None if claim.status != ESTABLISHED else _profile_payload(profile, taxonomy),
            mastery_assessment=None if claim.stage != MASTERY or claim.mastery_assessment is None
            else _mastery_payload(claim.mastery_assessment)))
        if claim.status == ESTABLISHED and claim.basis_mode == DIRECT:
            # Observations seulement : une relation T5 n'est jamais une preuve.
            refs += _refs(POSITIVE_BASIS, claim.basis_observation_ids, claim_stage=claim.stage)
        if claim.status == ESTABLISHED:
            for name in DIMENSION_ORDER:
                dimension = getattr(profile, name)
                refs += _refs(CONFIDENCE, dimension.observation_ids, dimension.structural_refs,
                              claim_stage=claim.stage, confidence_dimension=name)
        if claim.stage == MASTERY and claim.mastery_assessment is not None:
            items = [getattr(claim.mastery_assessment, name) for name in MASTERY_PROPERTIES]
            refs += _refs(MASTERY_REF, [i for item in items for i in item.observation_ids],
                          [r for item in items for r in item.structural_refs], claim_stage=MASTERY)
    return tuple(claims), refs


def _tensions_and_refs(state, needs, taxonomy) -> tuple:
    tensions, refs = [], []
    for index, tension in enumerate(state.tensions):
        key = f"t{index}"
        localized = tension.scope_mode == LOCALIZED
        summary = f"{tension.fragilized_stage}:{tension.scope_mode}:{'+'.join(tension.reason_codes)}"
        if tension.capability_definition_ids:
            summary += f":{_scope_labels(tension.capability_definition_ids, taxonomy)}"
        tensions.append(TensionDecision(
            tension_key=key, fragilized_stage=tension.fragilized_stage, scope_mode=tension.scope_mode,
            summary=summary, revision_status=tension_revision_status(tension, needs),
            capability_membership_ids=_membership_ids(tension.capability_definition_ids, taxonomy)
            if localized else ()))
        refs += _refs(TENSION, tension.contradiction_observation_ids, tension.structural_refs, tension_key=key)
    return tuple(tensions), refs


def _ref_order(tension_keys: dict):
    def key(ref):
        return (ref.ref_role, ref.claim_stage is not None,
                None if ref.claim_stage is None else CLAIM_STAGES.index(ref.claim_stage),
                ref.tension_key is not None, tension_keys.get(ref.tension_key), ref.confidence_dimension or "",
                ref.source_kind, str(ref.source_id))
    return key


def _state_summary(state, transition, needs) -> str:
    intents = [intent for intent in VALIDATION_INTENTS if any(n.intent == intent for n in needs)]
    return ";".join((f"current_stage={state.current_stage}", f"state_reason={state.state_reason_code}",
                     f"transition_cause={transition.cause or NONE_LABEL}",
                     f"validation={'+'.join(intents) or NONE_LABEL}"))


def _assemble(context, positive_basis, state, transition, needs) -> InferenceDecision:
    taxonomy = context.current_taxonomy_context
    claims, refs = _claims_and_refs(positive_basis, state, taxonomy)
    tensions, tension_refs = _tensions_and_refs(state, needs, taxonomy)
    refs += tension_refs
    refs += _refs(TRANSITION, transition.observation_ids, transition.structural_refs)
    refs += _refs(VALIDATION, [i for n in needs for i in n.observation_ids],
                  [r for n in needs for r in n.structural_refs])
    unique = list(dict.fromkeys(refs))  # dédoublonnage par cible EXACTE
    keys = {t.tension_key: index for index, t in enumerate(tensions)}
    return InferenceDecision(
        current_stage=state.current_stage,
        transition_cause=transition.cause,
        unresolved_revision_context=None if state.revision_context is None else state.revision_context.to_payload(),
        validation_needs=tuple(n.to_payload() for n in needs),
        state_decision_summary=_state_summary(state, transition, needs),
        claims=claims,
        tensions=tensions,
        basis_refs=tuple(sorted(unique, key=_ref_order(keys))),
    )


# --------------------------------------------------------------------------
# DecisionInvariantGuard
# --------------------------------------------------------------------------

def _fail(condition: bool, message: str) -> None:
    if condition:
        raise InvalidAssembledDecision(message)


def _check_payload(value, path: str) -> None:
    """JSON strict (mapping / liste / scalaire), sans clé de score, de
    niveau, de tâche ni de provenance brute."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            _fail(type(key) is not str or key in _FORBIDDEN_PAYLOAD_KEYS, f"{path} : clé {key!r} interdite")
            _check_payload(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_payload(item, path)
    else:
        _fail(value is not None and type(value) not in (str, bool), f"{path} : valeur {value!r} non qualitative")


def _highest(claims) -> str:
    return next((c.stage for c in reversed(claims) if c.positive_basis_status == ESTABLISHED), NON_ETABLI)


def _guard_claims(context, state, decision, refs) -> None:
    claims = decision.claims
    _fail(type(claims) is not tuple or tuple(getattr(c, "stage", None) for c in claims) != CLAIM_STAGES,
          "exactement quatre StageClaimDecision attendues (discovery -> mastery)")
    for claim in claims:
        _fail(type(claim) is not StageClaimDecision or claim.basis_mode not in BASIS_MODES,
              f"{claim.stage} : claim invalide")
        own = [r for r in refs if r.claim_stage == claim.stage]
        positive = [r for r in own if r.ref_role == POSITIVE_BASIS]
        if claim.positive_basis_status == NOT_ESTABLISHED:
            _fail(claim.basis_mode != BASIS_MODE_NONE or claim.confidence_profile is not None,
                  f"{claim.stage} not_established : basis_mode none et aucun confidence_profile")
            _fail(any(r.ref_role in (POSITIVE_BASIS, CONFIDENCE) for r in own),
                  f"{claim.stage} not_established : aucune ref positive_basis / confidence")
        else:
            _fail(claim.positive_basis_status != ESTABLISHED or claim.basis_mode == BASIS_MODE_NONE,
                  f"{claim.stage} : statut / mode incohérents")
            profile = claim.confidence_profile
            _fail(not isinstance(profile, Mapping) or set(profile) != {"schema_version", *CONFIDENCE_DIMENSIONS},
                  f"{claim.stage} : confidence_profile = schema_version + cinq dimensions exactement")
            _check_payload(profile, f"{claim.stage}.confidence_profile")
            _check_profile(profile, context.current_taxonomy_context, f"{claim.stage}.confidence_profile")
            _fail(claim.basis_mode == DIRECT and not positive, f"{claim.stage} direct sans ref positive_basis")
            _fail(claim.basis_mode == IMPLIED_BY_HIGHER_CLAIM and bool(positive),
                  f"{claim.stage} implied : aucune ref positive_basis (aucune preuve recopiée)")
        if claim.mastery_assessment is not None:
            _fail(claim.stage != MASTERY, f"{claim.stage} : mastery_assessment réservé à mastery")
            _check_payload(claim.mastery_assessment, "mastery.mastery_assessment")
        for text in (claim.basis_summary, claim.scope_summary):
            _fail(type(text) is not str or not text, f"{claim.stage} : résumés d'audit attendus")
    established = [c.positive_basis_status == ESTABLISHED for c in claims]
    _fail(any(e and not all(established[:i]) for i, e in enumerate(established)), "base positive non monotone")
    # Fidélité : chaque profil sérialisé est EXACTEMENT celui des faits T6-C2.
    for claim, profile in zip(claims, state.confidence_profiles):
        _fail(claim.confidence_profile != (None if profile is None else _profile_payload(
            profile, context.current_taxonomy_context)), f"{claim.stage} : confidence_profile != faits T6-C2")


def _guard_refs(context, decision) -> None:
    dossier = context.longitudinal_dossier
    observations = {o.observation_id: o for o in dossier.active_history.observations}
    relations = relation_endpoints(dossier)
    claims = {c.stage: c for c in decision.claims}
    keys = {t.tension_key for t in decision.tensions}
    _fail(list(dict.fromkeys(decision.basis_refs)) != list(decision.basis_refs), "ref en double (même cible exacte)")
    owners = {}
    for ref in decision.basis_refs:
        _fail(type(ref) is not BasisRefDecision or ref.ref_role not in REF_ATTACHMENTS or
              ref.source_kind not in SOURCE_KINDS, "ref invalide")
        attached = (ref.claim_stage is not None, ref.tension_key is not None, ref.confidence_dimension is not None)
        _fail(attached != REF_ATTACHMENTS[ref.ref_role], f"ref {ref.ref_role} : rattachement incohérent")
        _fail(ref.tension_key is not None and ref.tension_key not in keys, f"ref {ref.ref_role} : tension inconnue")
        if ref.source_kind == SOURCE_OBSERVATION:
            _fail(ref.source_id not in observations, f"ref {ref.ref_role} : observation hors du dossier courant")
        else:
            _fail((ref.source_kind, ref.source_id) not in relations,
                  f"ref {ref.ref_role} : relation hors du dossier courant")
        if ref.ref_role == POSITIVE_BASIS:
            claim = claims[ref.claim_stage]
            _fail(ref.source_kind != SOURCE_OBSERVATION or observations[ref.source_id].polarity != SUPPORTIVE,
                  "positive_basis : observation supportive seulement (une relation n'est jamais une preuve)")
            _fail((claim.positive_basis_status, claim.basis_mode) != (ESTABLISHED, DIRECT),
                  f"positive_basis vers {ref.claim_stage} non direct")
            _fail(owners.setdefault(ref.source_id, ref.claim_stage) != ref.claim_stage,
                  "une observation n'est positive_basis que d'une claim")
        _fail(ref.ref_role == CONFIDENCE and claims[ref.claim_stage].positive_basis_status != ESTABLISHED,
              "confidence vers une claim non établie")
        _fail(ref.ref_role == MASTERY_REF and ref.claim_stage != MASTERY, "ref mastery hors de la claim mastery")


def _guard_tensions(context, decision) -> None:
    memberships = {c.membership_id for c in context.current_taxonomy_context.capabilities}
    for tension in decision.tensions:
        _fail(type(tension) is not TensionDecision or tension.revision_status not in REVISION_STATUSES,
              "tension invalide")
        _fail(not any(r.ref_role == TENSION and r.tension_key == tension.tension_key for r in decision.basis_refs),
              f"{tension.tension_key} sans ref tension")
        if tension.scope_mode == LOCALIZED:
            _fail(not tension.capability_membership_ids or not set(tension.capability_membership_ids) <= memberships,
                  f"{tension.tension_key} localized : memberships de la release courante requis")
        else:
            _fail(bool(tension.capability_membership_ids), f"{tension.tension_key} {tension.scope_mode} : aucun"
                                                           " membership")


def _guard_state(context, state, decision) -> None:
    refs = decision.basis_refs
    current, cause = decision.current_stage, decision.transition_cause
    _fail(current not in STAGE_SEQUENCE, "current_stage inconnu")
    _fail(decision.unresolved_revision_context != (None if state.revision_context is None
                                                   else state.revision_context.to_payload()),
          "unresolved_revision_context != revision-context-v1 de T6-C2")
    if decision.unresolved_revision_context is not None:
        _check_payload(decision.unresolved_revision_context, "unresolved_revision_context")
    for need in decision.validation_needs:
        _fail(not isinstance(need, Mapping) or set(need) != _VALIDATION_NEED_KEYS, "validation_need hors format")
        _check_payload(need, "validation_need")
        definitions = {str(d): d for d in current_capabilities(context.current_taxonomy_context)}
        try:
            check_validation_scope(need["scope_mode"],
                                   tuple(definitions.get(d, d) for d in need["capability_definition_ids"]),
                                   context.current_taxonomy_context)
        except FinalInferenceError as exc:
            raise InvalidAssembledDecision(f"validation_need : {exc}") from exc
    _fail(bool(decision.validation_needs) and not any(r.ref_role == VALIDATION for r in refs),
          "validation_needs sans ref validation")
    _fail(type(decision.state_decision_summary) is not str or not decision.state_decision_summary,
          "state_decision_summary attendu")
    highest = _highest(decision.claims)
    _fail(current == NON_ETABLI and highest != NON_ETABLI, "non_etabli avec une claim établie")
    predecessor = context.predecessor
    if predecessor is None:
        _fail(cause is not None, "première inférence : transition_cause None")
        _fail(current != NON_ETABLI and STAGE_SEQUENCE.index(current) > STAGE_SEQUENCE.index(highest),
              "première inférence : stade au-dessus de la plus haute claim établie")
        return
    _fail(cause not in TRANSITION_CAUSES, "predecessor : transition_cause obligatoire")
    previous = predecessor.current_stage
    rank = STAGE_SEQUENCE.index
    held = current == previous and bool(decision.tensions) and bool(decision.unresolved_revision_context)
    _fail(current != NON_ETABLI and rank(current) > rank(highest) and not held,
          "stade au-dessus de la plus haute claim établie hors maintien sous tension")
    moved = current != previous
    reset = moved and current == NON_ETABLI and highest == NON_ETABLI and cause in (EVIDENCE_INTEGRITY_CHANGE,
                                                                                    PEDAGOGICAL_REINTERPRETATION)
    _fail(current == NON_ETABLI and moved and cause == NEW_USER_EVIDENCE,
          "retour à non_etabli par new_user_evidence interdit")
    _fail(moved and not reset and not any(r.ref_role == TRANSITION for r in refs), "transition sans ref transition")
    if cause == NEW_USER_EVIDENCE:
        dossier = context.longitudinal_dossier
        new_events = frozenset(context.transition_causality.new_user_event_ids)
        events = {o.observation_id: o.event_id for o in dossier.active_history.observations}
        endpoints = relation_endpoints(dossier)
        reached = {events.get(i) for r in refs
                   for i in ({r.source_id} if r.source_kind == SOURCE_OBSERVATION
                             else endpoints[(r.source_kind, r.source_id)])}
        _fail(reached.isdisjoint(new_events), "new_user_evidence sans ref atteignant un événement nouveau")
        unresolved = predecessor.unresolved_revision_context is not None or predecessor.tension_state == TENSION_OPEN \
            or predecessor.transition == REVISED_DOWN
        rebound = {events.get(i) for r in refs if r.ref_role in (POSITIVE_BASIS, TRANSITION)
                   for i in ({r.source_id} if r.source_kind == SOURCE_OBSERVATION
                             else endpoints[(r.source_kind, r.source_id)])}
        _fail(unresolved and rank(current) > rank(previous) and rebound.isdisjoint(new_events),
              "remontée après une révision non résolue sans ref positive_basis / transition nouvelle")


def _guard(context, state, decision) -> None:
    """DecisionInvariantGuard (pur, propre à T6-C)."""
    _fail(type(decision) is not InferenceDecision, "InferenceDecision attendue")
    _guard_claims(context, state, decision, decision.basis_refs)
    _guard_refs(context, decision)
    _guard_tensions(context, decision)
    _guard_state(context, state, decision)


# --------------------------------------------------------------------------
# Point d'entrée public
# --------------------------------------------------------------------------

def infer_competency(context) -> InferenceDecision:
    """Unique point d'entrée public du moteur T6-C V1 (fonction pure) :
    InferenceContext -> InferenceDecision, prête pour la complétion
    transactionnelle T6-B (aucune persistance ici)."""
    resolve_final_inference_policy(context)
    positive_basis = evaluate_positive_basis(context)
    state = evaluate_inference_state(context, positive_basis)
    transition = resolve_transition_cause(context, positive_basis, state)
    needs = evaluate_validation_needs(context, positive_basis, state)
    decision = _assemble(context, positive_basis, state, transition, needs)
    _guard(context, state, decision)
    return decision
