"""Étape 6.1E : CONTEXTE PÉDAGOGIQUE minimal de la réponse
(PedagogicalResponseContext).

Question, et seulement elle : « une fois le focus de la demande validé et
les présupposés sûrs établis, quel contexte pédagogique minimal, immuable
et versionné les composants suivants reçoivent-ils ? ». 6-1E est un
ASSEMBLEUR PUR : il ne prend aucune nouvelle décision sur les compétences,
les acquis ou les besoins de validation.

    6-1B InteractionCompetencyFocus validé (de quoi parle la demande)
    + 6-1D SafeAssumptionPlan (ce qui peut être présupposé, contraintes)
        -> build_pedagogical_response_context -> PedagogicalResponseContext
        -> project_pedagogical_context -> PedagogicalProjection

Frontière constitutionnelle. Step 5 établit l'état épistémique ; Step 6
adapte l'interaction et ne modifie jamais Step 5. 6-1E ne lit aucun état
Step 5 (ni claims, ni tensions, ni contexte de révision, ni besoins bruts),
aucune base, aucun message utilisateur ; il ne crée aucun stade, ne remappe
aucune capacité, n'ajoute aucune compétence, n'active aucune contrainte,
ne choisit ni posture, ni mouvement, ni progression visible, ne génère
aucune réponse et n'est branché à aucun runtime. « Décrire, jamais
prescrire. »

Structure : UNE représentation canonique, UNE projection dérivée.

- PedagogicalResponseContext (canonique) =
    A. ENVELOPPE TECHNIQUE : ResponseContextEnvelope (versions du plan, du
       baseline et du focus, identité de taxonomie) et, par compétence,
       CompetencyStateProvenance (provenance Step 5 copiée du plan, None si
       aucun état Step 5) ;
    B. CONTENU PÉDAGOGIQUE : resolution_status et, par compétence,
       CompetencyPedagogicalContext (rôle, compétence, périmètre exact,
       présupposés sûrs, contraintes conditionnelles).
  Chaque compétence est un CompetencyResponseContext(provenance, pedagogy) :
  la partie pédagogique n'existe qu'UNE fois.
- PedagogicalProjection : vue minimale destinée aux composants suivants,
  obtenue UNIQUEMENT par project_pedagogical_context. Elle référence les
  MÊMES objets CompetencyPedagogicalContext que le contexte canonique (aucune
  copie, aucune divergence possible) et n'expose ni l'enveloppe ni la
  provenance : aucun identifiant de run, aucun state_generation, aucune
  release, aucune identité de personne, aucun diagnostic.

Invariants :

- Version explicite : PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION décrit le
  contrat (contexte et projection). Aucune version implicite.

- Compatibilité focus / plan STRICTE (jamais un rapprochement) : types
  exacts ; versions supportées du plan, du baseline et du focus ; versions
  de schéma et de policy du focus identiques à celles portées par le plan ;
  taxonomy_release_id et taxonomy_spec_fingerprint identiques ;
  resolution_status identique ; cible et supports appariés dans l'ordre
  (même nombre, même competency_code, même rôle, même scope_mode, mêmes
  capability_definition_ids exacts dans le même ordre). Aucun rapprochement
  par libellé, capability_code ou semantic_revision ; aucun remapping entre
  releases ; rien n'est réparé, réordonné ni ignoré.

- Compatibilité != authenticité : ces contrôles prouvent que le plan a été
  construit pour CE focus, pas que ce focus provient réellement du
  classificateur pour cette interaction, ni que l'état Step 5 du plan est
  le plus récent. Les contrats reçus ne permettent pas de le vérifier :
  l'authenticité du focus et la fraîcheur de l'état relèvent de la chaîne
  d'orchestration (6-1D lie déjà son plan à l'état fourni). 6-1E ne relit
  aucune base pour le simuler.

- Conservation exacte : SafeAssumptionCoverage et MinimalValidationConstraint
  de 6-1D sont repris tels quels (mêmes objets, même ordre, mêmes
  definition_id). Aucun périmètre fusionné, aucun localized élargi, aucun
  competency_only converti en whole_competency, aucune échelle comblée ;
  une capacité sans stade sûr reste représentée avec safe_stages ().

- Trois situations jamais fusionnées :
    A. focus non résolu (neutral / ambiguous / composite) : contexte neutre,
       statut exact conservé, ni cible, ni support, ni présupposé, ni
       contrainte ;
    B. focus résolu, aucun état Step 5 : focus conservé, provenance None,
       aucun stade sûr, aucune contrainte (jamais un non_etabli synthétisé) ;
    C. focus résolu, état Step 5 présent sans stade sûr : focus et
       périmètres conservés, provenance présente, contraintes du plan
       conservées, aucun présupposé inventé.
  L'absence de présupposé n'est jamais un diagnostic d'incapacité.

- Contraintes CONDITIONNELLES : chaque contrainte conserve exactement
  VALIDATION_ACTIVATION_RULE et VALIDATION_WORK_RULE ; 6-1E ne la convertit
  jamais en question, exercice ou obligation, ne la sélectionne ni ne
  l'active, et n'y ajoute aucun motif.

- Priorité de la demande : le contexte ne fixe aucune difficulté maximale.
  Les stades sûrs sont un PLANCHER de présupposés, jamais un plafond ; la
  demande de l'utilisateur reste une entrée indépendante de la génération ;
  un support n'est jamais un prérequis bloquant.

- Fail-closed : InvalidResponseContextArgument (types top-level),
  UnsupportedResponseContextVersion (version non supportée),
  IncompatibleResponseContextInputs (focus et plan non appariés),
  InvalidResponseContextContent (structure incohérente du focus, du plan ou
  d'un contexte fourni à la projection). Jamais de contexte partiel ; une
  corruption n'est jamais convertie en contexte neutre (le neutre est
  réservé à une résolution non concluante).

- Fonction PURE : aucune base, aucune horloge, aucun environnement, aucun
  hasard, aucun modèle de langage, aucun réseau, aucune persistance. Même
  focus + même plan => même contexte ; même contexte => même projection.

Ordres canoniques : cible puis supports (ordre C1 -> C12 du focus) ;
présupposés dans l'ordre des coverages du plan ; contraintes dans l'ordre du
plan. Jamais un tri.
"""
import uuid
from dataclasses import dataclass

from core.adaptation_assumptions import ASSUMPTION_BASELINE_SCHEMA_VERSION, ASSUMPTION_STAGE_ORDER, SUPPORTING, TARGET
from core.adaptation_focus import (
    COMPETENCY_ONLY,
    FOCUS_POLICY_VERSION,
    FOCUS_SCHEMA_VERSION,
    LOCALIZED,
    RESOLUTION_STATUSES,
    RESOLVED,
    SCOPE_MODES,
    CompetencyFocus,
    InteractionCompetencyFocus,
)
from core.adaptation_safety import (
    SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION,
    VALIDATION_ACTIVATION_RULE,
    VALIDATION_WORK_RULE,
    CompetencySafeAssumptions,
    MinimalValidationConstraint,
    SafeAssumptionCoverage,
    SafeAssumptionPlan,
)
from core.adaptation_state import COMPETENCY_ORDER
from core.inference_final_policies import VALIDATION_INTENTS

PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION = "pedagogical-response-context-v1"


class PedagogicalResponseContextError(Exception):
    """Erreur métier de 6-1E : jamais un contexte partiel, jamais un repli
    neutre implicite."""


class InvalidResponseContextArgument(PedagogicalResponseContextError):
    """Argument top-level d'un type inattendu (focus, plan, contexte)."""


class UnsupportedResponseContextVersion(PedagogicalResponseContextError):
    """Version de schéma ou de policy non supportée par ce contrat."""


class IncompatibleResponseContextInputs(PedagogicalResponseContextError):
    """Focus et plan non appariés (versions, taxonomie, statut, compétences,
    rôles, périmètres, definition_id) : jamais rapprochés."""


class InvalidResponseContextContent(PedagogicalResponseContextError):
    """Structure incohérente du focus, du plan ou d'un contexte fourni."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ResponseContextEnvelope:
    """Enveloppe TECHNIQUE : versions et identité de taxonomie vérifiées.
    Jamais transmise par la projection."""
    plan_schema_version: str
    baseline_schema_version: str
    focus_schema_version: str
    focus_policy_version: str
    taxonomy_release_id: uuid.UUID
    taxonomy_spec_fingerprint: str


@dataclass(frozen=True, kw_only=True)
class CompetencyStateProvenance:
    """Provenance TECHNIQUE de l'état Step 5 d'une compétence, copiée du
    plan. Jamais un signal pédagogique, jamais transmise par la projection."""
    inference_run_id: uuid.UUID
    longitudinal_assessment_run_id: uuid.UUID
    state_generation: int
    taxonomy_release_id: uuid.UUID


@dataclass(frozen=True, kw_only=True)
class CompetencyPedagogicalContext:
    """Contenu pédagogique d'UNE compétence du focus : périmètre exact,
    présupposés sûrs (plancher, jamais plafond) et contraintes
    conditionnelles de 6-1D, conservés tels quels. Aucun diagnostic."""
    role: str
    competency_code: str

    scope_mode: str
    capability_definition_ids: tuple[uuid.UUID, ...]

    safe_assumptions: tuple[SafeAssumptionCoverage, ...]
    validation_constraints: tuple[MinimalValidationConstraint, ...]


@dataclass(frozen=True, kw_only=True)
class CompetencyResponseContext:
    """Une compétence du contexte canonique : provenance technique (None si
    aucun état Step 5) + contenu pédagogique (unique)."""
    provenance: CompetencyStateProvenance | None
    pedagogy: CompetencyPedagogicalContext


@dataclass(frozen=True, kw_only=True)
class PedagogicalResponseContext:
    """Contexte CANONIQUE de 6-1E : enveloppe technique + contenu
    pédagogique. Aucune identité de personne."""
    schema_version: str
    envelope: ResponseContextEnvelope

    resolution_status: str

    target: CompetencyResponseContext | None
    supporting: tuple[CompetencyResponseContext, ...]


@dataclass(frozen=True, kw_only=True)
class PedagogicalProjection:
    """Projection pédagogique minimale (project_pedagogical_context) : focus
    pertinent, présupposés sûrs localisés, contraintes conditionnelles.
    Aucune provenance, aucune version technique, aucun diagnostic."""
    schema_version: str

    resolution_status: str

    target: CompetencyPedagogicalContext | None
    supporting: tuple[CompetencyPedagogicalContext, ...]


# --------------------------------------------------------------------------
# Versions et compatibilité focus / plan (pures)
# --------------------------------------------------------------------------

def _supported(value, name: str, expected: str) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value != expected:
        raise UnsupportedResponseContextVersion(f"{name} {value!r} non supportée ({expected!r} attendue)")
    return value


def _same(focus_value, plan_value, name: str) -> None:
    if type(plan_value) is not type(focus_value) or plan_value != focus_value:
        raise IncompatibleResponseContextInputs(
            f"{name} : focus {focus_value!r}, plan {plan_value!r} (aucun rapprochement)")


def _check_versions(focus: InteractionCompetencyFocus, plan: SafeAssumptionPlan) -> None:
    _supported(plan.schema_version, "plan.schema_version", SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION)
    _supported(plan.baseline_schema_version, "plan.baseline_schema_version", ASSUMPTION_BASELINE_SCHEMA_VERSION)
    _supported(focus.schema_version, "focus.schema_version", FOCUS_SCHEMA_VERSION)
    _supported(focus.policy_version, "focus.policy_version", FOCUS_POLICY_VERSION)
    _same(focus.schema_version, plan.focus_schema_version, "schema_version du focus")
    _same(focus.policy_version, plan.focus_policy_version, "policy_version du focus")


def _check_taxonomy(focus: InteractionCompetencyFocus, plan: SafeAssumptionPlan) -> None:
    if type(focus.taxonomy_release_id) is not uuid.UUID:
        raise InvalidResponseContextContent(f"focus.taxonomy_release_id {focus.taxonomy_release_id!r} non uuid.UUID")
    fingerprint = focus.taxonomy_spec_fingerprint
    if type(fingerprint) is not str or not fingerprint.strip():
        raise InvalidResponseContextContent("focus.taxonomy_spec_fingerprint : chaîne non vide attendue")
    _same(focus.taxonomy_release_id, plan.focus_taxonomy_release_id, "taxonomy_release_id")
    _same(fingerprint, plan.focus_taxonomy_spec_fingerprint, "taxonomy_spec_fingerprint (aucun remapping)")


def _parts(target, supporting, part_type, owner: str) -> tuple:
    """(cible, *supports) d'un focus ou d'un plan, types exacts."""
    if target is not None and type(target) is not part_type:
        raise InvalidResponseContextContent(f"{owner}.target : {part_type.__name__} attendu")
    if type(supporting) is not tuple or any(type(part) is not part_type for part in supporting):
        raise InvalidResponseContextContent(f"{owner}.supporting : tuple de {part_type.__name__} attendu")
    return (target, *supporting)


def _paired(focus: InteractionCompetencyFocus, plan: SafeAssumptionPlan) -> tuple:
    """Appariement EXACT, dans l'ordre, de la cible et des supports."""
    focused = _parts(focus.target, focus.supporting, CompetencyFocus, "focus")
    planned = _parts(plan.target, plan.supporting, CompetencySafeAssumptions, "plan")
    if focus.resolution_status != RESOLVED:
        if focused != (None,) or planned != (None,):
            raise InvalidResponseContextContent(
                f"{focus.resolution_status} : ni cible ni support attendus (jamais un contexte neutre implicite)")
        return ()
    if focus.target is None or plan.target is None:
        raise InvalidResponseContextContent(f"{RESOLVED} sans cible ({'focus' if focus.target is None else 'plan'})")
    focus_codes = [part.competency_code for part in focus.supporting]
    plan_codes = [part.competency_code for part in plan.supporting]
    if focus_codes != plan_codes:
        missing = [code for code in focus_codes if code not in plan_codes]
        extra = [code for code in plan_codes if code not in focus_codes]
        detail = (f"manquantes {missing}, en trop {extra}" if missing or extra
                  else f"ordre {plan_codes} au lieu de {focus_codes}")
        raise IncompatibleResponseContextInputs(f"compétences de soutien : {detail}")
    pairs = []
    for index, (wanted, planned_part) in enumerate(zip(focused, planned)):
        name = "target" if index == 0 else f"supporting[{index - 1}]"
        role = TARGET if index == 0 else SUPPORTING
        if type(planned_part.role) is not str or planned_part.role != role:
            raise InvalidResponseContextContent(f"plan.{name}.role {planned_part.role!r} ({role!r} attendu)")
        _same(wanted.competency_code, planned_part.competency_code, f"{name}.competency_code")
        _same(wanted.scope_mode, planned_part.focus_scope_mode, f"{name}.scope_mode")
        _same(wanted.capability_definition_ids, planned_part.focus_capability_definition_ids,
              f"{name}.capability_definition_ids (identité exacte, ordre conservé)")
        pairs.append((role, wanted, planned_part, name))
    return tuple(pairs)


# --------------------------------------------------------------------------
# Structure du contenu (pure ; partagée par l'assemblage et la projection)
# --------------------------------------------------------------------------

def _content_fail(path: str, message: str) -> InvalidResponseContextContent:
    return InvalidResponseContextContent(f"{path} : {message}")


def _choice(value, path: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise _content_fail(path, f"valeur {value!r} hors vocabulaire {list(allowed)}")
    return value


def _check_scope(scope_mode: str, ids, path: str) -> None:
    """localized <-> au moins un definition_id uuid.UUID, tous distincts ;
    competency_only <-> aucun."""
    if type(ids) is not tuple:
        raise _content_fail(path, "capability_definition_ids : tuple attendu")
    if any(type(i) is not uuid.UUID for i in ids):
        raise _content_fail(path, "definition_id non uuid.UUID")
    if len(set(ids)) != len(ids):
        raise _content_fail(path, "definition_id en double")
    if (scope_mode == LOCALIZED) != bool(ids):
        raise _content_fail(path, f"{scope_mode} et {len(ids)} definition_id(s) incohérents")


def _check_safe_assumptions(part: CompetencyPedagogicalContext, path: str) -> None:
    """Un coverage par definition_id du périmètre (localized, même ordre) ou
    un seul coverage None (competency_only) ; stades sûrs du vocabulaire,
    distincts, ordre conceptuel, jamais comblés."""
    coverages = part.safe_assumptions
    if type(coverages) is not tuple or any(type(c) is not SafeAssumptionCoverage for c in coverages):
        raise _content_fail(path, "safe_assumptions : tuple de SafeAssumptionCoverage attendu")
    expected = part.capability_definition_ids if part.scope_mode == LOCALIZED else (None,)
    if tuple(c.capability_definition_id for c in coverages) != expected:
        raise _content_fail(path, "safe_assumptions hors du périmètre exact (aucun périmètre fusionné ni élargi)")
    for coverage in coverages:
        stages = coverage.safe_stages
        if type(stages) is not tuple or any(type(s) is not str or s not in ASSUMPTION_STAGE_ORDER for s in stages):
            raise _content_fail(path, f"safe_stages {stages!r} hors vocabulaire")
        if stages != tuple(s for s in ASSUMPTION_STAGE_ORDER if s in stages):
            raise _content_fail(path, f"safe_stages {stages!r} : distincts, ordre conceptuel attendu")


def _check_constraints(part: CompetencyPedagogicalContext, path: str) -> None:
    """Contraintes de 6-1D sur le périmètre exact, CONDITIONNELLES (règles
    machine exactes), sans doublon ; jamais activées ni sélectionnées."""
    constraints = part.validation_constraints
    if type(constraints) is not tuple or any(type(c) is not MinimalValidationConstraint for c in constraints):
        raise _content_fail(path, "validation_constraints : tuple de MinimalValidationConstraint attendu")
    for index, constraint in enumerate(constraints):
        name = f"{path}.validation_constraints[{index}]"
        _choice(constraint.intent, f"{name}.intent", VALIDATION_INTENTS)
        _choice(constraint.target_stage, f"{name}.target_stage", ASSUMPTION_STAGE_ORDER)
        if constraint.scope_mode != part.scope_mode:
            raise _content_fail(name, f"scope_mode {constraint.scope_mode!r} hors du périmètre {part.scope_mode}")
        ids = constraint.capability_definition_ids
        _check_scope(part.scope_mode, ids, name)
        if ids != tuple(i for i in part.capability_definition_ids if i in ids):
            raise _content_fail(name, "definition_id hors du périmètre exact ou hors de son ordre")
        if (constraint.activation_rule, constraint.residual_work_rule) != (VALIDATION_ACTIVATION_RULE,
                                                                            VALIDATION_WORK_RULE):
            raise _content_fail(name, "règles conditionnelles modifiées (jamais une obligation)")
    if len(set(constraints)) != len(constraints):  # champs validés : tous hachables
        raise _content_fail(path, "contrainte en double")


def _check_pedagogy(part, role: str, path: str) -> None:
    if type(part) is not CompetencyPedagogicalContext:
        raise _content_fail(path, f"CompetencyPedagogicalContext attendu, reçu {type(part).__name__}")
    if type(part.role) is not str or part.role != role:
        raise _content_fail(path, f"role {part.role!r} ({role!r} attendu)")
    _choice(part.competency_code, f"{path}.competency_code", COMPETENCY_ORDER)
    scope_mode = _choice(part.scope_mode, f"{path}.scope_mode", SCOPE_MODES)
    _check_scope(scope_mode, part.capability_definition_ids, path)
    _check_safe_assumptions(part, path)
    _check_constraints(part, path)


def _check_provenance(provenance, part: CompetencyPedagogicalContext, path: str) -> None:
    """None (aucun état Step 5) => aucun stade sûr, aucune contrainte ;
    sinon provenance technique complète."""
    if provenance is None:
        if any(c.safe_stages for c in part.safe_assumptions) or part.validation_constraints:
            raise _content_fail(path, "présupposé ou contrainte sans état Step 5 (jamais un acquis créé)")
        return
    if type(provenance) is not CompetencyStateProvenance:
        raise _content_fail(path, f"CompetencyStateProvenance attendue, reçu {type(provenance).__name__}")
    for name in ("inference_run_id", "longitudinal_assessment_run_id", "taxonomy_release_id"):
        if type(getattr(provenance, name)) is not uuid.UUID:
            raise _content_fail(path, f"provenance.{name} non uuid.UUID")
    if type(provenance.state_generation) is not int or provenance.state_generation < 1:
        raise _content_fail(path, f"provenance.state_generation {provenance.state_generation!r} incohérent")


def _check_competencies(context: PedagogicalResponseContext) -> tuple:
    """Statut, rôles, unicité et ordre C1 -> C12 des supports ; contenu de
    chaque compétence. Retourne les CompetencyResponseContext."""
    status = _choice(context.resolution_status, "resolution_status", RESOLUTION_STATUSES)
    target, supporting = context.target, context.supporting
    if type(supporting) is not tuple:
        raise _content_fail("supporting", "tuple attendu")
    if status != RESOLVED:
        if target is not None or supporting:
            raise _content_fail(status, "ni cible ni support attendus")
        return ()
    if target is None:
        raise _content_fail(RESOLVED, "cible attendue")
    parts = (target, *supporting)
    for index, part in enumerate(parts):
        path = "target" if index == 0 else f"supporting[{index - 1}]"
        if type(part) is not CompetencyResponseContext:
            raise _content_fail(path, f"CompetencyResponseContext attendu, reçu {type(part).__name__}")
        _check_pedagogy(part.pedagogy, TARGET if index == 0 else SUPPORTING, path)
        _check_provenance(part.provenance, part.pedagogy, path)
    codes = [part.pedagogy.competency_code for part in parts]
    if len(set(codes)) != len(codes):
        raise _content_fail("supporting", "compétence en double ou cible reprise en support")
    if codes[1:] != [code for code in COMPETENCY_ORDER if code in codes[1:]]:
        raise _content_fail("supporting", f"{codes[1:]} hors de l'ordre C1 -> C12")
    return parts


def _check_context(context: PedagogicalResponseContext) -> tuple:
    _supported(context.schema_version, "schema_version", PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION)
    envelope = context.envelope
    if type(envelope) is not ResponseContextEnvelope:
        raise _content_fail("envelope", f"ResponseContextEnvelope attendue, reçu {type(envelope).__name__}")
    _supported(envelope.plan_schema_version, "envelope.plan_schema_version", SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION)
    _supported(envelope.baseline_schema_version, "envelope.baseline_schema_version",
               ASSUMPTION_BASELINE_SCHEMA_VERSION)
    _supported(envelope.focus_schema_version, "envelope.focus_schema_version", FOCUS_SCHEMA_VERSION)
    _supported(envelope.focus_policy_version, "envelope.focus_policy_version", FOCUS_POLICY_VERSION)
    if type(envelope.taxonomy_release_id) is not uuid.UUID:
        raise _content_fail("envelope", "taxonomy_release_id non uuid.UUID")
    if type(envelope.taxonomy_spec_fingerprint) is not str or not envelope.taxonomy_spec_fingerprint.strip():
        raise _content_fail("envelope", "taxonomy_spec_fingerprint : chaîne non vide attendue")
    return _check_competencies(context)


# --------------------------------------------------------------------------
# Assemblage (pur)
# --------------------------------------------------------------------------

def _provenance(part: CompetencySafeAssumptions, path: str) -> CompetencyStateProvenance | None:
    """Copie de la provenance du plan : tout None (aucun état Step 5) ou
    tout renseigné ; jamais complétée."""
    values = (part.source_inference_run_id, part.source_longitudinal_assessment_run_id,
              part.source_state_generation, part.source_taxonomy_release_id)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise _content_fail(path, "provenance Step 5 partielle")
    return CompetencyStateProvenance(
        inference_run_id=part.source_inference_run_id,
        longitudinal_assessment_run_id=part.source_longitudinal_assessment_run_id,
        state_generation=part.source_state_generation,
        taxonomy_release_id=part.source_taxonomy_release_id,
    )


def _competency(role: str, wanted: CompetencyFocus, planned: CompetencySafeAssumptions,
                path: str) -> CompetencyResponseContext:
    return CompetencyResponseContext(
        provenance=_provenance(planned, path),
        pedagogy=CompetencyPedagogicalContext(
            role=role,
            competency_code=wanted.competency_code,
            scope_mode=wanted.scope_mode,
            capability_definition_ids=wanted.capability_definition_ids,
            safe_assumptions=planned.coverages,
            validation_constraints=planned.validation_constraints,
        ),
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def build_pedagogical_response_context(
    *,
    focus: InteractionCompetencyFocus,
    plan: SafeAssumptionPlan,
) -> PedagogicalResponseContext:
    """Contexte pédagogique canonique, assemblé EXCLUSIVEMENT depuis le focus
    validé et le plan de présupposés sûrs construit pour ce focus. Fonction
    PURE (aucune base, horloge, environnement, hasard ni modèle) : même
    entrée => même sortie.

    Focus non résolu => contexte neutre (statut exact, ni cible ni support).
    Erreurs : InvalidResponseContextArgument, UnsupportedResponseContextVersion,
    IncompatibleResponseContextInputs, InvalidResponseContextContent ; jamais
    de contexte partiel ou neutre implicite. Compatibilité vérifiée,
    authenticité du focus et fraîcheur de l'état NON vérifiables ici
    (orchestrateur)."""
    if type(focus) is not InteractionCompetencyFocus:
        raise InvalidResponseContextArgument(f"InteractionCompetencyFocus attendu, reçu {type(focus).__name__}")
    if type(plan) is not SafeAssumptionPlan:
        raise InvalidResponseContextArgument(f"SafeAssumptionPlan attendu, reçu {type(plan).__name__}")
    _check_versions(focus, plan)
    _check_taxonomy(focus, plan)
    status = _choice(focus.resolution_status, "focus.resolution_status", RESOLUTION_STATUSES)
    _same(status, plan.resolution_status, "resolution_status")

    assembled = tuple(_competency(role, wanted, planned, name)
                      for role, wanted, planned, name in _paired(focus, plan))
    context = PedagogicalResponseContext(
        schema_version=PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
        envelope=ResponseContextEnvelope(
            plan_schema_version=plan.schema_version,
            baseline_schema_version=plan.baseline_schema_version,
            focus_schema_version=focus.schema_version,
            focus_policy_version=focus.policy_version,
            taxonomy_release_id=focus.taxonomy_release_id,
            taxonomy_spec_fingerprint=focus.taxonomy_spec_fingerprint,
        ),
        resolution_status=status,
        target=assembled[0] if assembled else None,
        supporting=assembled[1:],
    )
    _check_context(context)
    return context


def project_pedagogical_context(context: PedagogicalResponseContext) -> PedagogicalProjection:
    """Projection pédagogique minimale d'un contexte canonique (revérifié) :
    mêmes objets CompetencyPedagogicalContext, sans enveloppe ni provenance.
    Fonction PURE et déterministe ; aucune sélection, aucune activation."""
    if type(context) is not PedagogicalResponseContext:
        raise InvalidResponseContextArgument(f"PedagogicalResponseContext attendu, reçu {type(context).__name__}")
    parts = _check_context(context)
    return PedagogicalProjection(
        schema_version=context.schema_version,
        resolution_status=context.resolution_status,
        target=parts[0].pedagogy if parts else None,
        supporting=tuple(part.pedagogy for part in parts[1:]),
    )
