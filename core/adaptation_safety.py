"""Étape 6.1D : PRÉSUPPOSÉS SÛRS et contraintes minimales de validation
(SafeAssumptionPlan).

Deux questions, et seulement elles :
  A. parmi les étages positifs établis par 6-1C, lesquels restent réellement
     sûrs à présupposer sur le périmètre demandé, après prise en compte des
     fragilités Step 5 ?
  B. existe-t-il sur ce même périmètre un besoin latent de confirmation /
     revalidation pouvant devenir une contrainte minimale de génération,
     sans jamais prendre priorité sur la demande explicite ?
6-1D ne choisit ni posture, ni mouvement pédagogique, ne formule aucune
question, ne construit aucun contexte de génération (6-1E), ne branche
aucun runtime, ne crée aucune trace d'étayage réel et ne modifie jamais
Step 5.

    6-1A AdaptationStateSnapshot + 6-1C AssumptionBaseline (focus 6-1B validé)
        -> build_safe_assumption_plan -> SafeAssumptionPlan

Trois sources, trois rôles, jamais mélangés :
  - AssumptionBaseline : BASE POSITIVE (souveraine, jamais reconstruite) ;
  - tensions + unresolved_revision_context : FRAGILITÉS, qui ne peuvent que
    RÉDUIRE les présupposés et ne créent jamais à elles seules une
    contrainte de validation (Step 5 possède la décision de revalider) ;
  - validation_needs : OPPORTUNITÉS LATENTES, qui créent uniquement des
    contraintes CONDITIONNELLES et ne retirent jamais un stade.

Invariants :

- Version explicite : SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION. Aucune version
  implicite.

- Preflight canonique : 6-1D n'applique jamais un baseline construit sur un
  autre état (R42) à l'état courant (R43). Le focus est reconstruit
  UNIQUEMENT depuis le baseline (sans rien ajouter ni remapper), puis
  build_assumption_baseline(state, focus) doit redonner EXACTEMENT le
  baseline fourni ; sinon InvalidSafeAssumptionBaseline (erreur 6-1C
  chaînée). 6-1C reste souverain sur la base positive, la provenance, le
  plafond current_stage, les claims et la compatibilité entre releases :
  rien de tout cela n'est réimplémenté ici, et aucune claim n'est relue
  après le preflight. La sortie est construite depuis le baseline
  canonique. Le preflight lie le baseline à l'ÉTAT ; il n'authentifie pas
  le focus (seuls (baseline, state) sont reçus) : l'authenticité du focus
  validé reste la responsabilité de 6-1B / de l'orchestrateur.

- Focus non résolu (neutral / ambiguous / composite) : target None,
  supporting () ; aucune fragilité, aucun contexte de révision, aucun
  besoin n'est lu.

- Minimisation : seules la cible et les compétences de soutien du baseline
  sont traitées. Une compétence hors focus ne réduit rien et ne crée rien.
  Compétence sans état Step 5 (source_inference_run_id None) : coverages
  inchangés, aucune contrainte, aucune fragilité synthétisée.

- safe_stages = established_stages MOINS les étages rendus non sûrs. Une
  fragilité au stade S rend non sûrs S et tous les stades supérieurs, sur
  CE périmètre seulement ; un étage reste sûr s'il est strictement
  inférieur à TOUTES les fragilités applicables (le stade fragilisé le plus
  bas l'emporte, quel que soit l'ordre d'entrée). Aucun stade ajouté,
  restauré ni comblé.

- Portée d'une fragilité (matrice figée) :
      fragilité \\ focus     localized X                competency_only
      localized Y            oui ssi definition_id X=Y  non
      competency_only        non                        oui
      whole_competency       oui (toutes capacités)     oui
  competency_only n'est jamais assimilé à whole_competency. localized =
  definition_id EXACT, jamais un remapping par capability_code, libellé,
  semantic_revision, position ni membership.

- Tensions courantes : toute AdaptationTension structurellement valide est
  une fragilité, quel que soit revision_status (unresolved et
  revalidation_needed : aucune hiérarchie). Leurs capacités localized
  appartiennent à la release du snapshot courant (sinon fail closed).

- unresolved_revision_context : PREMIER composant Step 6 qui l'interprète.
  Relecture STRICTE de revision-context-v1 reproduisant le contrat du
  parser canonique de T6-C2 (sans l'importer) : clés exactes, version,
  resolution_status unresolved, reason_code du vocabulaire, UUID
  canoniques, motifs non vides, périmètre localized <-> capacités,
  sources non vides, reason_codes du vocabulaire des tensions, ordres
  canoniques ; un motif en double est refusé. Les capacités d'un motif
  peuvent appartenir à une release historique : elles ne sont jamais
  rejetées pour cette seule raison et n'agissent que sur un definition_id
  identique du focus. Les sources de contradiction et les codes de raison
  servent uniquement à valider le format : jamais relus, jamais
  transmis.

- validation_needs : validés structurellement (vocabulaires validation-need-v1,
  capacités localized de la release courante, reason_codes non vides,
  distincts, ordre du vocabulaire, aucun besoin en double) ET comme
  combinaison POSSIBLE de la policy T6-C3 V1 (confirmation : stade de
  confirmation_stages, jamais Mastery, jamais whole_competency, raisons de
  la seule famille confirmation ; revalidation : raisons de la seule
  famille revalidation ; familles jamais mêlées), sans jamais rechercher
  pourquoi le besoin existe, puis
  PROJETÉS sur le focus de l'interaction :
      besoin \\ focus        localized (ids F)          competency_only
      localized N            localized F ∩ N (ordre F)  aucun
      competency_only        aucun                      competency_only ()
      whole_competency       localized F                competency_only ()
  intersection vide => aucune contrainte. intent et target_stage
  préservés EXACTEMENT (target_stage n'est jamais aligné sur les étages
  sûrs restants). reason_codes jamais copiés. Deux besoins convergeant
  vers la même projection => une seule contrainte (première occurrence,
  ordre Step 5). whole_competency n'apparaît jamais en sortie.

- Contrainte CONDITIONNELLE : activation_rule (VALIDATION_ACTIVATION_RULE)
  et residual_work_rule (VALIDATION_WORK_RULE) sont des règles machine
  internes, jamais du texte utilisateur : ne jamais détourner une demande
  directe pour observer l'utilisateur, ne laisser de travail cognitif que
  s'il vient naturellement, et au strict minimum. 6-1D dit seulement que
  l'opportunité existe ; il ne décide pas de son activation.

- Cible / supports : même mécanique locale ; un support (fragile, vide ou
  absent) ne bloque, ne réduit, ne route et ne conditionne jamais la cible.

- Sortie minimale : aucun diagnostic (ni état de tension, ni stade
  fragilisé, ni statut de révision, ni raison), aucune identité de
  personne ; seulement ce qui peut être présupposé, les contraintes
  éventuelles et la provenance TECHNIQUE copiée du baseline.

- Fail-closed : InvalidSafeAssumptionArgument (types top-level),
  InvalidSafeAssumptionBaseline (schéma, baseline non canonique pour cet
  état), InvalidSafeAssumptionState (fragilité ou besoin incohérent sur une
  compétence pertinente), InvalidSafeAssumptionRevisionContext (hors
  revision-context-v1). Aucun repli neutre automatique.

- Fonction PURE : aucune base, aucune horloge, aucun environnement, aucun
  hasard, aucun modèle de langage, aucun réseau, aucune persistance. Même
  baseline + même état => même plan. Jamais de re-inférence Step 5.

Ordres canoniques : cible puis supports (ordre du baseline) ; coverages
dans l'ordre du baseline ; safe_stages filtrés depuis established_stages ;
contraintes dans l'ordre des validation_needs Step 5 ; capacités projetées
dans l'ordre du focus. Jamais un tri lexical d'UUID.
"""
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from core.adaptation_assumptions import (
    ASSUMPTION_BASELINE_SCHEMA_VERSION,
    ASSUMPTION_STAGE_ORDER,
    AssumptionBaseline,
    AssumptionBaselineError,
    CompetencyAssumptionBaseline,
    build_assumption_baseline,
)
from core.adaptation_focus import (
    COMPETENCY_ONLY,
    LOCALIZED,
    RESOLVED,
    CompetencyFocus,
    InteractionCompetencyFocus,
)
from core.adaptation_state import AdaptationStateSnapshot, AdaptationTension, AdaptationValidationNeed
from core.inference_final_policies import (
    CONFIRMATION,
    CURRENT_STAGE_UNDER_TENSION,
    FINAL_INFERENCE_V1_POLICY,
    MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM,
    REVALIDATION,
    UNRESOLVED_REVISION_MOTIF,
    VALIDATION_INTENTS,
    VALIDATION_REASON_CODES,
    VALIDATION_SCOPE_MODES,
)
from core.inference_service import (
    CLAIM_STAGES,
    REVISION_STATUSES,
    TENSION_NONE,
    TENSION_OPEN,
    TENSION_SCOPE_MODES,
    WHOLE_COMPETENCY,
)
from core.inference_state_policies import (
    REVISION_CONTEXT_SCHEMA_VERSION,
    REVISION_REASON_CODES,
    TENSION_REASON_CODES,
    UNRESOLVED,
)

SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION = "safe-assumption-plan-v1"

# Règles machine internes (jamais du texte utilisateur).
VALIDATION_ACTIVATION_RULE = (
    "only_if_naturally_relevant_and_not_direct_answer_or_explanation"
)
VALIDATION_WORK_RULE = (
    "preserve_minimal_residual_user_work"
)

# Format exact de revision-context-v1 (miroir de la sérialisation T6-C2).
_REVISION_CONTEXT_FIELDS = ("schema_version", "origin_inference_run_id", "reason_code", "resolution_status",
                            "motifs")
_REVISION_MOTIF_FIELDS = ("fragilized_stage", "scope_mode", "capability_definition_ids",
                          "source_contradiction_observation_ids", "reason_codes")

# Combinaisons qu'un besoin validation-need-v1 peut RÉELLEMENT porter selon
# la policy T6-C3 V1 (core/inference_validation.py), lues sur ses
# constantes propriétaires, jamais recalculées : une confirmation vient
# d'un confirmation_motif (stades confirmation_stages, jamais Mastery ; scope
# localized ou competency_only, jamais whole_competency) ; une revalidation
# vient d'un motif de révision ou d'une tension (tout stade, tout scope).
# intent fait partie de l'identité de fusion T6-C3 : jamais de familles
# mêlées.
_CONFIRMATION_REASON_CODES = frozenset(rule.reason_code for rule in FINAL_INFERENCE_V1_POLICY.confirmation_motifs)
_REVALIDATION_REASON_CODES = frozenset({UNRESOLVED_REVISION_MOTIF, CURRENT_STAGE_UNDER_TENSION,
                                        MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM})


class SafeAssumptionPlanError(Exception):
    """Erreur métier de 6-1D : jamais un plan partiel, jamais un repli
    neutre implicite."""


class InvalidSafeAssumptionArgument(SafeAssumptionPlanError):
    """Argument top-level d'un type inattendu (baseline, state)."""


class InvalidSafeAssumptionBaseline(SafeAssumptionPlanError):
    """Baseline hors schéma, ou non reproductible canoniquement par 6-1C
    sur l'état fourni."""


class InvalidSafeAssumptionState(SafeAssumptionPlanError):
    """Fragilité ou besoin de validation d'une compétence pertinente
    structurellement incohérent."""


class InvalidSafeAssumptionRevisionContext(SafeAssumptionPlanError):
    """unresolved_revision_context hors revision-context-v1 canonique."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class SafeAssumptionCoverage:
    """Étages sûrs à présupposer sur UN périmètre : definition_id exact du
    focus (localized) ou None (competency_only). safe_stages : sous-ensemble
    ordonné des established_stages du baseline, jamais un stade ajouté."""
    capability_definition_id: uuid.UUID | None
    safe_stages: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class MinimalValidationConstraint:
    """Opportunité latente de Step 5 projetée sur le périmètre demandé.
    Conditionnelle (activation_rule) et minimale (residual_work_rule) :
    jamais une question, une tâche ni une priorité."""
    intent: str
    target_stage: str

    scope_mode: str
    capability_definition_ids: tuple[uuid.UUID, ...]

    activation_rule: str
    residual_work_rule: str


@dataclass(frozen=True, kw_only=True)
class CompetencySafeAssumptions:
    """Présupposés sûrs d'UNE compétence du focus. source_* : provenance
    TECHNIQUE copiée du baseline, jamais un signal pédagogique."""
    role: str
    competency_code: str

    focus_scope_mode: str
    focus_capability_definition_ids: tuple[uuid.UUID, ...]

    source_inference_run_id: uuid.UUID | None
    source_longitudinal_assessment_run_id: uuid.UUID | None
    source_state_generation: int | None
    source_taxonomy_release_id: uuid.UUID | None

    coverages: tuple[SafeAssumptionCoverage, ...]

    validation_constraints: tuple[MinimalValidationConstraint, ...]


@dataclass(frozen=True, kw_only=True)
class SafeAssumptionPlan:
    """Présupposés sûrs de l'interaction, liés au baseline et au focus
    validé. Aucune identité de personne, aucun diagnostic."""
    schema_version: str

    baseline_schema_version: str

    focus_schema_version: str
    focus_policy_version: str
    focus_taxonomy_release_id: uuid.UUID
    focus_taxonomy_spec_fingerprint: str

    resolution_status: str

    target: CompetencySafeAssumptions | None
    supporting: tuple[CompetencySafeAssumptions, ...]


class _Fragility(NamedTuple):
    """Fragilité applicable au cutoff (tension courante ou motif non résolu)."""
    stage: str
    scope_mode: str
    definition_ids: frozenset


# --------------------------------------------------------------------------
# Preflight canonique du baseline (6-1C souverain)
# --------------------------------------------------------------------------

def _competency_focus(part: CompetencyAssumptionBaseline) -> CompetencyFocus:
    return CompetencyFocus(competency_code=part.competency_code, scope_mode=part.focus_scope_mode,
                           capability_definition_ids=part.focus_capability_definition_ids)


def _canonical_baseline(baseline: AssumptionBaseline, state: AdaptationStateSnapshot) -> AssumptionBaseline:
    """Focus reconstruit UNIQUEMENT depuis le baseline, puis baseline
    recalculé par 6-1C sur l'état fourni : il doit être identique."""
    target, supporting = baseline.target, baseline.supporting
    if target is not None and type(target) is not CompetencyAssumptionBaseline:
        raise InvalidSafeAssumptionBaseline(f"target de type {type(target).__name__}")
    if type(supporting) is not tuple or any(type(p) is not CompetencyAssumptionBaseline for p in supporting):
        raise InvalidSafeAssumptionBaseline("supporting : tuple de CompetencyAssumptionBaseline attendu")
    focus = InteractionCompetencyFocus(
        schema_version=baseline.focus_schema_version,
        policy_version=baseline.focus_policy_version,
        taxonomy_release_id=baseline.focus_taxonomy_release_id,
        taxonomy_spec_fingerprint=baseline.focus_taxonomy_spec_fingerprint,
        resolution_status=baseline.resolution_status,
        target=None if target is None else _competency_focus(target),
        supporting=tuple(_competency_focus(part) for part in supporting),
    )
    try:
        canonical = build_assumption_baseline(state=state, focus=focus)
    except AssumptionBaselineError as exc:
        raise InvalidSafeAssumptionBaseline(f"baseline non reproductible par 6-1C : {exc}") from exc
    if canonical != baseline:
        raise InvalidSafeAssumptionBaseline(
            "baseline différent du baseline canonique de cet état (autre état Step 5, provenance, périmètre"
            " ou échelle falsifiés)")
    return canonical


# --------------------------------------------------------------------------
# Validation structurelle de la compétence pertinente (pure)
# --------------------------------------------------------------------------

def _state_fail(competency_code: str, message: str) -> InvalidSafeAssumptionState:
    return InvalidSafeAssumptionState(f"{competency_code} : {message}")


def _state_choice(value, competency_code: str, name: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise _state_fail(competency_code, f"{name} : valeur {value!r} hors vocabulaire")
    return value


def _state_ids(ids, mode: str, known: frozenset, competency_code: str, name: str) -> tuple:
    """capability_definition_ids d'un objet COURANT : localized <-> non vide,
    uuid.UUID exacts, distincts, de la release du snapshot."""
    if type(ids) is not tuple:
        raise _state_fail(competency_code, f"{name}.capability_definition_ids : tuple attendu")
    if any(type(i) is not uuid.UUID for i in ids):
        raise _state_fail(competency_code, f"{name} : definition_id non uuid.UUID")
    if len(set(ids)) != len(ids):
        raise _state_fail(competency_code, f"{name} : definition_id en double")
    if (mode == LOCALIZED) != bool(ids):
        raise _state_fail(competency_code, f"{name} : {mode} et {len(ids)} definition_id(s) incohérents")
    if not set(ids) <= known:
        raise _state_fail(competency_code, f"{name} : definition_id absent des capacités du snapshot"
                                           " (identité exacte, aucun remapping)")
    return ids


def _current_fragilities(snapshot, known: frozenset) -> tuple:
    code = snapshot.competency_code
    state = _state_choice(snapshot.tension_state, code, "tension_state", (TENSION_NONE, TENSION_OPEN))
    tensions = snapshot.tensions
    if type(tensions) is not tuple:
        raise _state_fail(code, "tensions : tuple attendu")
    if bool(tensions) != (state == TENSION_OPEN):
        raise _state_fail(code, f"{len(tensions)} tension(s) pour tension_state {state}")
    fragilities = []
    for index, tension in enumerate(tensions):
        name = f"tensions[{index}]"
        if type(tension) is not AdaptationTension:
            raise _state_fail(code, f"{name} de type {type(tension).__name__}")
        stage = _state_choice(tension.fragilized_stage, code, f"{name}.fragilized_stage", ASSUMPTION_STAGE_ORDER)
        mode = _state_choice(tension.scope_mode, code, f"{name}.scope_mode", TENSION_SCOPE_MODES)
        _state_choice(tension.revision_status, code, f"{name}.revision_status", REVISION_STATUSES)
        ids = _state_ids(tension.capability_definition_ids, mode, known, code, name)
        fragilities.append(_Fragility(stage, mode, frozenset(ids)))
    return tuple(fragilities)


def _check_possible_need(intent: str, stage: str, mode: str, reasons: tuple, code: str, name: str) -> None:
    """La COMBINAISON doit être une sortie possible de la policy T6-C3 V1 ;
    jamais convertie en une autre combinaison."""
    if intent == CONFIRMATION:
        if stage not in FINAL_INFERENCE_V1_POLICY.confirmation_stages:
            raise _state_fail(code, f"{name} : {CONFIRMATION} {stage} impossible (confirmation_stages V1)")
        if mode == WHOLE_COMPETENCY:
            raise _state_fail(code, f"{name} : {CONFIRMATION} {WHOLE_COMPETENCY} impossible")
        family = _CONFIRMATION_REASON_CODES
    elif intent == REVALIDATION:
        family = _REVALIDATION_REASON_CODES
    else:
        raise _state_fail(code, f"{name}.intent : valeur {intent!r} hors vocabulaire")
    if not set(reasons) <= family:
        raise _state_fail(code, f"{name}.reason_codes : hors de la famille {intent} (familles jamais mêlées)")


def _validation_needs(snapshot, known: frozenset) -> tuple:
    code = snapshot.competency_code
    needs = snapshot.validation_needs
    if type(needs) is not tuple:
        raise _state_fail(code, "validation_needs : tuple attendu")
    identities = set()
    for index, need in enumerate(needs):
        name = f"validation_needs[{index}]"
        if type(need) is not AdaptationValidationNeed:
            raise _state_fail(code, f"{name} de type {type(need).__name__}")
        intent = _state_choice(need.intent, code, f"{name}.intent", VALIDATION_INTENTS)
        stage = _state_choice(need.target_stage, code, f"{name}.target_stage", ASSUMPTION_STAGE_ORDER)
        mode = _state_choice(need.scope_mode, code, f"{name}.scope_mode", VALIDATION_SCOPE_MODES)
        ids = _state_ids(need.capability_definition_ids, mode, known, code, name)
        reasons = need.reason_codes
        if type(reasons) is not tuple or not reasons:
            raise _state_fail(code, f"{name}.reason_codes : tuple non vide attendu")
        for reason in reasons:
            _state_choice(reason, code, f"{name}.reason_codes", VALIDATION_REASON_CODES)
        if reasons != tuple(r for r in VALIDATION_REASON_CODES if r in reasons):
            raise _state_fail(code, f"{name}.reason_codes : distincts, ordre du vocabulaire attendu")
        _check_possible_need(intent, stage, mode, reasons, code, name)
        identity = (intent, stage, mode, frozenset(ids))
        if identity in identities:
            raise _state_fail(code, f"{name} : besoin {intent} {stage} {mode} en double")
        identities.add(identity)
    return needs


# --------------------------------------------------------------------------
# Relecture stricte de revision-context-v1 (pure, sans le moteur T6)
# --------------------------------------------------------------------------

def _context_fail(message: str) -> InvalidSafeAssumptionRevisionContext:
    return InvalidSafeAssumptionRevisionContext(f"unresolved_revision_context : {message}")


def _context_mapping(value, fields, name: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise _context_fail(f"{name} : champs {sorted(fields)} exactement")
    return value


def _context_sequence(value, name: str) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _context_fail(f"{name} : liste attendue")
    return tuple(value)


def _context_choice(value, name: str, allowed) -> str:
    if type(value) is not str or value not in allowed:
        raise _context_fail(f"{name} : valeur {value!r} hors vocabulaire")
    return value


def _context_uuid(value, name: str) -> uuid.UUID:
    """UUID sérialisé par T6-C2 : str canonique, jamais deviné."""
    if type(value) is not str:
        raise _context_fail(f"{name} : UUID canonique attendu")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise _context_fail(f"{name} : UUID invalide {value!r}") from None
    if str(parsed) != value:
        raise _context_fail(f"{name} : forme canonique attendue")
    return parsed


def _revision_fragilities(raw, position: Mapping) -> tuple:
    """revision-context-v1 -> fragilités des motifs non résolus. position :
    definition_id -> rang dans la release du snapshot (ordre canonique de
    T6-C2 : capacités connues dans l'ordre de la taxonomie, puis capacités
    historiques par str(UUID) ; motifs par stade, scope, capacités,
    sources). Une capacité historique n'est jamais rejetée pour sa seule
    absence de la release courante."""
    if raw is None:
        return ()
    data = _context_mapping(raw, _REVISION_CONTEXT_FIELDS, "contexte")
    _context_choice(data["schema_version"], "schema_version", (REVISION_CONTEXT_SCHEMA_VERSION,))
    _context_uuid(data["origin_inference_run_id"], "origin_inference_run_id")
    _context_choice(data["reason_code"], "reason_code", REVISION_REASON_CODES)
    _context_choice(data["resolution_status"], "resolution_status", (UNRESOLVED,))
    items = _context_sequence(data["motifs"], "motifs")
    if not items:
        raise _context_fail("motifs vides")
    keys, identities, fragilities = [], set(), []
    for index, item in enumerate(items):
        name = f"motifs[{index}]"
        motif = _context_mapping(item, _REVISION_MOTIF_FIELDS, name)
        stage = _context_choice(motif["fragilized_stage"], f"{name}.fragilized_stage", CLAIM_STAGES)
        mode = _context_choice(motif["scope_mode"], f"{name}.scope_mode", TENSION_SCOPE_MODES)
        caps = tuple(_context_uuid(v, f"{name}.capability_definition_ids") for v in _context_sequence(
            motif["capability_definition_ids"], f"{name}.capability_definition_ids"))
        sources = tuple(_context_uuid(v, f"{name}.source_contradiction_observation_ids") for v in _context_sequence(
            motif["source_contradiction_observation_ids"], f"{name}.source_contradiction_observation_ids"))
        reasons = tuple(_context_choice(v, f"{name}.reason_codes", TENSION_REASON_CODES)
                        for v in _context_sequence(motif["reason_codes"], f"{name}.reason_codes"))
        if (mode == LOCALIZED) != bool(caps):
            raise _context_fail(f"{name} : {mode} et {len(caps)} capacité(s) incohérents")
        if not sources:
            raise _context_fail(f"{name} : source_contradiction_observation_ids vide")
        known = tuple(i for i in sorted(set(caps) & set(position), key=position.__getitem__))
        historical = tuple(sorted(set(caps) - set(position), key=str))
        if caps != (*known, *historical) or sources != tuple(sorted(set(sources), key=str)) or \
                reasons != tuple(r for r in TENSION_REASON_CODES if r in reasons):
            raise _context_fail(f"{name} : doublon ou ordre non canonique")
        identity = (stage, mode, caps, sources)
        if identity in identities:
            raise _context_fail(f"{name} : motif en double")
        identities.add(identity)
        keys.append((CLAIM_STAGES.index(stage), mode,
                     tuple((i not in position, position[i] if i in position else str(i)) for i in caps),
                     tuple(str(i) for i in sources)))
        fragilities.append(_Fragility(stage, mode, frozenset(caps)))
    if keys != sorted(keys):
        raise _context_fail("motifs hors ordre canonique")
    return tuple(fragilities)


# --------------------------------------------------------------------------
# Réduction locale et projection (pures)
# --------------------------------------------------------------------------

def _applies(fragility: _Fragility, focus_scope_mode: str, definition_id: uuid.UUID | None) -> bool:
    """Matrice figée : whole_competency agit sur tout coverage ; localized
    seulement sur le definition_id exact d'un focus localized ;
    competency_only seulement sur un focus competency_only."""
    if fragility.scope_mode == WHOLE_COMPETENCY:
        return True
    if focus_scope_mode == LOCALIZED:
        return fragility.scope_mode == LOCALIZED and definition_id in fragility.definition_ids
    return fragility.scope_mode == COMPETENCY_ONLY


def _below(stage: str, fragilized_stage: str) -> bool:
    return ASSUMPTION_STAGE_ORDER.index(stage) < ASSUMPTION_STAGE_ORDER.index(fragilized_stage)


def _safe_coverage(coverage, focus_scope_mode: str, fragilities: tuple) -> SafeAssumptionCoverage:
    applicable = [f for f in fragilities if _applies(f, focus_scope_mode, coverage.capability_definition_id)]
    return SafeAssumptionCoverage(
        capability_definition_id=coverage.capability_definition_id,
        safe_stages=tuple(stage for stage in coverage.established_stages
                          if all(_below(stage, f.stage) for f in applicable)),
    )


def _constraints(needs: tuple, focus_scope_mode: str, focus_ids: tuple) -> tuple:
    """Projection éphémère des besoins latents sur le focus : localized ->
    intersection exacte dans l'ordre du focus ; whole_competency -> tout le
    focus ; granularité incompatible -> rien. Déduplication stable."""
    constraints = []
    for need in needs:
        if focus_scope_mode == LOCALIZED:
            if need.scope_mode == LOCALIZED:
                ids = tuple(i for i in focus_ids if i in need.capability_definition_ids)
            elif need.scope_mode == WHOLE_COMPETENCY:
                ids = focus_ids
            else:
                ids = ()
            if not ids:
                continue
        elif need.scope_mode == LOCALIZED:
            continue
        else:
            ids = ()
        constraint = MinimalValidationConstraint(
            intent=need.intent, target_stage=need.target_stage,
            scope_mode=focus_scope_mode, capability_definition_ids=ids,
            activation_rule=VALIDATION_ACTIVATION_RULE, residual_work_rule=VALIDATION_WORK_RULE)
        if constraint not in constraints:
            constraints.append(constraint)
    return tuple(constraints)


def _competency_plan(part: CompetencyAssumptionBaseline, snapshot) -> CompetencySafeAssumptions:
    fragilities, constraints = (), ()
    if snapshot is not None:
        known = frozenset(capability.definition_id for capability in snapshot.capabilities)
        position = {capability.definition_id: index for index, capability in enumerate(snapshot.capabilities)}
        fragilities = (*_current_fragilities(snapshot, known),
                       *_revision_fragilities(snapshot.unresolved_revision_context, position))
        constraints = _constraints(_validation_needs(snapshot, known), part.focus_scope_mode,
                                   part.focus_capability_definition_ids)
    return CompetencySafeAssumptions(
        role=part.role,
        competency_code=part.competency_code,
        focus_scope_mode=part.focus_scope_mode,
        focus_capability_definition_ids=part.focus_capability_definition_ids,
        source_inference_run_id=part.source_inference_run_id,
        source_longitudinal_assessment_run_id=part.source_longitudinal_assessment_run_id,
        source_state_generation=part.source_state_generation,
        source_taxonomy_release_id=part.source_taxonomy_release_id,
        coverages=tuple(_safe_coverage(coverage, part.focus_scope_mode, fragilities) for coverage in part.coverages),
        validation_constraints=constraints,
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def build_safe_assumption_plan(
    *,
    baseline: AssumptionBaseline,
    state: AdaptationStateSnapshot,
) -> SafeAssumptionPlan:
    """Présupposés sûrs + contraintes minimales de validation, pour la cible
    et les supports du baseline 6-1C, sur l'état Step 5 qui l'a produit.
    Fonction PURE (aucune base, horloge, environnement, hasard ni modèle) :
    même entrée => même sortie.

    Erreurs : InvalidSafeAssumptionArgument, InvalidSafeAssumptionBaseline,
    InvalidSafeAssumptionState, InvalidSafeAssumptionRevisionContext ;
    jamais de plan partiel ou neutre implicite."""
    if type(baseline) is not AssumptionBaseline:
        raise InvalidSafeAssumptionArgument(f"AssumptionBaseline attendu, reçu {type(baseline).__name__}")
    if type(state) is not AdaptationStateSnapshot:
        raise InvalidSafeAssumptionArgument(f"AdaptationStateSnapshot attendu, reçu {type(state).__name__}")
    if type(baseline.schema_version) is not str or baseline.schema_version != ASSUMPTION_BASELINE_SCHEMA_VERSION:
        raise InvalidSafeAssumptionBaseline(f"schema_version {baseline.schema_version!r} non supportée")
    canonical = _canonical_baseline(baseline, state)

    target, supporting = None, ()
    if canonical.resolution_status == RESOLVED:
        parts = (canonical.target, *canonical.supporting)
        # Le preflight garantit une seule compétence pertinente par code et
        # sa provenance exacte ; hors focus, seul competency_code est lu.
        codes = {part.competency_code for part in parts if part.source_inference_run_id is not None}
        snapshots = {snapshot.competency_code: snapshot for snapshot in state.competencies
                     if snapshot.competency_code in codes}
        target = _competency_plan(canonical.target, snapshots.get(canonical.target.competency_code))
        supporting = tuple(_competency_plan(part, snapshots.get(part.competency_code))
                           for part in canonical.supporting)

    return SafeAssumptionPlan(
        schema_version=SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION,
        baseline_schema_version=canonical.schema_version,
        focus_schema_version=canonical.focus_schema_version,
        focus_policy_version=canonical.focus_policy_version,
        focus_taxonomy_release_id=canonical.focus_taxonomy_release_id,
        focus_taxonomy_spec_fingerprint=canonical.focus_taxonomy_spec_fingerprint,
        resolution_status=canonical.resolution_status,
        target=target,
        supporting=supporting,
    )
