"""Étape 6.1C : SOCLE POSITIF LOCALISÉ des présupposés (AssumptionBaseline).

Question, et seulement elle : « parmi ce qui est directement pertinent pour
la demande courante, quels stades positifs Step 5 sont réellement établis
sur ce périmètre ? ». 6-1C ne répond PAS encore à « qu'est-ce qui reste sûr
malgré les fragilités ? » : cette réduction appartient à 6-1D. La sortie
n'est donc jamais un présupposé final ; c'est un BASELINE positif que 6-1D
pourra réduire localement sans relire Step 5.

    6-1A AdaptationStateSnapshot (ce que Step 5 affirme)
    + 6-1B InteractionCompetencyFocus validé (de quoi parle la demande)
        -> build_assumption_baseline -> AssumptionBaseline

Frontière constitutionnelle. 6-1C consomme UNIQUEMENT, pour la cible et les
supports du focus :
  - le focus validé (jamais une FocusProposal brute de 6-1B2) ;
  - les claims déjà reconstruites par 6-1A : status, stage,
    represented_capability_definition_ids, competency_only_basis ;
  - current_stage, uniquement comme PLAFOND ;
  - les definition_id exacts des capacités du snapshot ;
  - la provenance technique du snapshot (run T6, run T5, compteur d'état,
    release).
Il ne lit jamais les fragilités Step 5 (ni leur état, ni leur liste), le
contexte de révision non résolu, les besoins de validation, les codes de
raison, le profil de confiance, l'évaluation de maîtrise, le mode de base
d'une claim, les libellés, l'identité de la personne, aucune préférence,
aucun signal comportemental, aucune trace d'étayage réel. Modifier
uniquement l'un de ces champs ne change jamais la sortie.

Invariants :

- Version explicite : ASSUMPTION_BASELINE_SCHEMA_VERSION décrit le contrat
  de sortie. Aucune version implicite.

- Focus non résolu (neutral / ambiguous / composite) : target None,
  supporting () ; AUCUNE compétence du snapshot n'est inspectée. Pas de
  focus résolu => pas de personnalisation par compétence.

- Minimisation : resolved => seules la cible et les compétences de soutien
  du focus sont traitées ; jamais un parcours C1-C12 pour un profil
  complet. Hors focus, seul competency_code est lu (pour localiser les
  compétences pertinentes) : rien d'autre n'est exploité ni validé.

- Absence != non_etabli : compétence du focus absente du snapshot (aucun T6
  validé) => provenance None et échelles vides ; snapshot présent avec
  current_stage non_etabli => provenance copiée et échelles vides. Jamais
  de non_etabli synthétisé.

- Localized : un AssumptionCoverage par definition_id du focus, dans son
  ordre (taxonomique, validé par 6-1B1). Identité sémantique =
  definition_id EXACT : un stade n'est rattaché à une capacité que si la
  claim est established, sous le plafond current_stage, et que le
  definition_id figure EXPLICITEMENT dans represented_capability_definition_ids.
  La release du snapshot peut différer de celle du focus (une même
  définition appartient à plusieurs releases) ; jamais de remapping par
  capability_code, libellé ou semantic_revision. competency_only_basis ne
  localise jamais une capacité.

- Competency_only : UN AssumptionCoverage (capability_definition_id None).
  Un stade n'y figure que si la claim est established, sous le plafond, et
  porte competency_only_basis True. Les périmètres localized ne sont jamais
  agrégés en une carte blanche sur la compétence. Aucune identité
  sémantique de compétence ne prouve la compatibilité entre releases :
  release du snapshot != release du focus => échelle vide (fail-closed V1),
  même competency_code.

- current_stage = PLAFOND, jamais couverture : il n'autorise aucun stade
  par lui-même, il en interdit seulement au-dessus de lui. Une claim
  established au-dessus du plafond n'entre jamais dans le baseline.

- Échelle complète : established_stages conserve TOUS les stades positifs
  rattachables, dans ASSUMPTION_STAGE_ORDER, sans combler de trou (aucun
  stade inventé) : 6-1D pourra retirer un étage fragilisé et retrouver les
  étages inférieurs encore positifs. Aucun « stade le plus haut » ne
  remplace l'échelle ; aucun stade converti en nombre ou en note.

- direct / implied : 6-1A a déjà reconstruit les périmètres ; 6-1C ne relit
  aucune observation, aucune ref de base positive, aucune claim source.

- Provenance TECHNIQUE (source_*) : copiée telle quelle du snapshot, jamais
  générée ni interprétée comme signal pédagogique ; elle permettra à 6-1D
  de vérifier qu'il applique les fragilités du MÊME état Step 5.

- Fail-closed sur un objet public forgé : InvalidAssumptionBaselineArgument
  (types top-level), InvalidAssumptionFocus (focus incohérent),
  InvalidAssumptionBaselineState (partie pertinente du snapshot
  incohérente, compétence pertinente en double). Seul ce qui sert à 6-1C
  est vérifié ; 6-1A reste le propriétaire du reste. Aucune erreur n'est
  convertie en baseline neutre : le repli appartiendra à l'orchestrateur.

- Fonction PURE : aucune base, aucune horloge, aucun environnement, aucun
  hasard, aucun modèle de langage. Même snapshot + même focus => même
  baseline. Éphémère : aucune persistance.

Ordres canoniques : cible puis supports (ordre C1 -> C12 déjà fourni par le
focus) ; coverages localized dans l'ordre des definition_id du focus ;
established_stages dans ASSUMPTION_STAGE_ORDER. Jamais un tri lexical
d'UUID, jamais un ordre SQL.
"""
import uuid
from dataclasses import dataclass

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
from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateSnapshot,
    CapabilitySemanticRef,
    CompetencyAdaptationSnapshot,
)

ASSUMPTION_BASELINE_SCHEMA_VERSION = "assumption-baseline-v1"

# Ordre conceptuel des stades positifs (jamais un nombre persisté).
ASSUMPTION_STAGE_ORDER = (
    "discovery",
    "comprehension",
    "application",
    "mastery",
)
# current_stage sans claim positive.
NON_ETABLI = "non_etabli"
_CURRENT_STAGES = (NON_ETABLI, *ASSUMPTION_STAGE_ORDER)

# Statuts de claim reconstruits par 6-1A.
_ESTABLISHED = "established"
_CLAIM_STATUSES = (_ESTABLISHED, "not_established")

# Rôle d'une compétence dans le focus.
TARGET = "target"
SUPPORTING = "supporting"
ASSUMPTION_ROLES = (TARGET, SUPPORTING)


class AssumptionBaselineError(Exception):
    """Erreur métier de 6-1C : jamais un baseline partiel, jamais un repli
    neutre implicite."""


class InvalidAssumptionBaselineArgument(AssumptionBaselineError):
    """Argument top-level d'un type inattendu (snapshot, focus)."""


class InvalidAssumptionFocus(AssumptionBaselineError):
    """InteractionCompetencyFocus forgé ou incohérent."""


class InvalidAssumptionBaselineState(AssumptionBaselineError):
    """Partie pertinente de l'AdaptationStateSnapshot incohérente."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class AssumptionCoverage:
    """Échelle positive établie sur UN périmètre : definition_id exact du
    focus (localized) ou None (competency_only). established_stages : stades
    positifs Step 5 rattachables à ce périmètre, ordre conceptuel, sans trou
    comblé. Jamais un présupposé final (6-1D)."""
    capability_definition_id: uuid.UUID | None
    established_stages: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CompetencyAssumptionBaseline:
    """Baseline d'UNE compétence du focus. source_* : provenance TECHNIQUE
    du snapshot Step 5 (None si aucun état validé), jamais un signal
    pédagogique."""
    role: str
    competency_code: str

    focus_scope_mode: str
    focus_capability_definition_ids: tuple[uuid.UUID, ...]

    source_inference_run_id: uuid.UUID | None
    source_longitudinal_assessment_run_id: uuid.UUID | None
    source_state_generation: int | None
    source_taxonomy_release_id: uuid.UUID | None

    coverages: tuple[AssumptionCoverage, ...]


@dataclass(frozen=True, kw_only=True)
class AssumptionBaseline:
    """Socle positif localisé de l'interaction, lié au focus validé. Aucune
    identité de personne."""
    schema_version: str

    focus_schema_version: str
    focus_policy_version: str
    focus_taxonomy_release_id: uuid.UUID
    focus_taxonomy_spec_fingerprint: str

    resolution_status: str

    target: CompetencyAssumptionBaseline | None
    supporting: tuple[CompetencyAssumptionBaseline, ...]


# --------------------------------------------------------------------------
# Validation structurelle du focus (pure)
# --------------------------------------------------------------------------

def _focus_choice(value, name: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise InvalidAssumptionFocus(f"{name} : valeur {value!r} hors vocabulaire {list(allowed)}")
    return value


def _check_competency_focus(focus, path: str) -> None:
    if type(focus) is not CompetencyFocus:
        raise InvalidAssumptionFocus(f"{path} : CompetencyFocus attendu, reçu {type(focus).__name__}")
    _focus_choice(focus.competency_code, f"{path}.competency_code", COMPETENCY_ORDER)
    scope_mode = _focus_choice(focus.scope_mode, f"{path}.scope_mode", SCOPE_MODES)
    ids = focus.capability_definition_ids
    if type(ids) is not tuple:
        raise InvalidAssumptionFocus(f"{path}.capability_definition_ids : tuple attendu")
    if scope_mode == COMPETENCY_ONLY:
        if ids:
            raise InvalidAssumptionFocus(f"{path} : {COMPETENCY_ONLY} avec {len(ids)} definition_id(s)")
        return
    if not ids:
        raise InvalidAssumptionFocus(f"{path} : {LOCALIZED} sans definition_id")
    if any(type(i) is not uuid.UUID for i in ids):
        raise InvalidAssumptionFocus(f"{path} : definition_id non uuid.UUID")
    if len(set(ids)) != len(ids):
        raise InvalidAssumptionFocus(f"{path} : definition_id en double")


def _check_focus(focus: InteractionCompetencyFocus) -> None:
    """Défense de la frontière publique : le focus validé par 6-1B1 reste
    une dataclass constructible à la main. Rien n'est remappé ni réparé."""
    _focus_choice(focus.schema_version, "schema_version", (FOCUS_SCHEMA_VERSION,))
    _focus_choice(focus.policy_version, "policy_version", (FOCUS_POLICY_VERSION,))
    if type(focus.taxonomy_release_id) is not uuid.UUID:
        raise InvalidAssumptionFocus(f"taxonomy_release_id {focus.taxonomy_release_id!r} non uuid.UUID")
    fingerprint = focus.taxonomy_spec_fingerprint
    if type(fingerprint) is not str or not fingerprint.strip():
        raise InvalidAssumptionFocus("taxonomy_spec_fingerprint : chaîne non vide attendue")
    status = _focus_choice(focus.resolution_status, "resolution_status", RESOLUTION_STATUSES)
    if type(focus.supporting) is not tuple:
        raise InvalidAssumptionFocus("supporting : tuple attendu")
    if status != RESOLVED:
        if focus.target is not None or focus.supporting:
            raise InvalidAssumptionFocus(f"{status} : ni cible ni support attendus")
        return
    if focus.target is None:
        raise InvalidAssumptionFocus(f"{RESOLVED} sans cible")
    _check_competency_focus(focus.target, "target")
    codes = []
    for index, support in enumerate(focus.supporting):
        _check_competency_focus(support, f"supporting[{index}]")
        if support.competency_code == focus.target.competency_code:
            raise InvalidAssumptionFocus(f"supporting[{index}] : {support.competency_code} est déjà la cible")
        if support.competency_code in codes:
            raise InvalidAssumptionFocus(f"supporting[{index}] : {support.competency_code} en double")
        codes.append(support.competency_code)
    if codes != [code for code in COMPETENCY_ORDER if code in codes]:
        raise InvalidAssumptionFocus(f"supporting {codes} hors de l'ordre C1 -> C12")


# --------------------------------------------------------------------------
# Validation structurelle de la partie pertinente du snapshot (pure)
# --------------------------------------------------------------------------

def _state_fail(competency_code: str, message: str) -> InvalidAssumptionBaselineState:
    return InvalidAssumptionBaselineState(f"{competency_code} : {message}")


def _check_snapshot(snapshot: CompetencyAdaptationSnapshot, competency_code: str) -> None:
    """Seuls les champs utilisés par 6-1C sont vérifiés (6-1A reste le
    propriétaire du reste : ni confiance, ni maîtrise, ni fragilités, ni
    besoins, ni contexte de révision ne sont relus)."""
    if snapshot.competency_code != competency_code:
        raise _state_fail(competency_code, f"snapshot de {snapshot.competency_code!r}")
    if type(snapshot.current_stage) is not str or snapshot.current_stage not in _CURRENT_STAGES:
        raise _state_fail(competency_code, f"current_stage {snapshot.current_stage!r} hors vocabulaire")
    for name in ("active_inference_run_id", "longitudinal_assessment_run_id", "taxonomy_release_id"):
        if type(getattr(snapshot, name)) is not uuid.UUID:
            raise _state_fail(competency_code, f"{name} {getattr(snapshot, name)!r} non uuid.UUID")
    if type(snapshot.state_generation) is not int or snapshot.state_generation < 1:
        raise _state_fail(competency_code, f"state_generation {snapshot.state_generation!r} incohérent")

    if type(snapshot.capabilities) is not tuple:
        raise _state_fail(competency_code, "capabilities : tuple attendu")
    definitions = set()
    for capability in snapshot.capabilities:
        if type(capability) is not CapabilitySemanticRef:
            raise _state_fail(competency_code, f"capacité de type {type(capability).__name__}")
        if type(capability.definition_id) is not uuid.UUID:
            raise _state_fail(competency_code, f"definition_id {capability.definition_id!r} non uuid.UUID")
        if capability.definition_id in definitions:
            raise _state_fail(competency_code, f"definition_id {capability.definition_id} en double")
        definitions.add(capability.definition_id)

    if type(snapshot.claims) is not tuple:
        raise _state_fail(competency_code, "claims : tuple attendu")
    if any(type(claim) is not AdaptationStageClaim for claim in snapshot.claims):
        raise _state_fail(competency_code, "claim d'un type inattendu")
    stages = tuple(claim.stage for claim in snapshot.claims)
    if stages != ASSUMPTION_STAGE_ORDER:
        raise _state_fail(competency_code, f"claims {stages!r} : exactement {ASSUMPTION_STAGE_ORDER} attendues")
    for claim in snapshot.claims:
        if type(claim.status) is not str or claim.status not in _CLAIM_STATUSES:
            raise _state_fail(competency_code, f"{claim.stage} : status {claim.status!r} hors vocabulaire")
        represented = claim.represented_capability_definition_ids
        if type(represented) is not tuple:
            raise _state_fail(competency_code, f"{claim.stage} : represented_capability_definition_ids non tuple")
        if any(type(i) is not uuid.UUID for i in represented):
            raise _state_fail(competency_code, f"{claim.stage} : definition_id représenté non uuid.UUID")
        if len(set(represented)) != len(represented):
            raise _state_fail(competency_code, f"{claim.stage} : definition_id représenté en double")
        if not set(represented) <= definitions:
            raise _state_fail(competency_code, f"{claim.stage} : definition_id représenté hors des capacités")
        if type(claim.competency_only_basis) is not bool:
            raise _state_fail(competency_code, f"{claim.stage} : competency_only_basis non bool")


def _relevant_snapshots(state: AdaptationStateSnapshot, codes: tuple) -> dict:
    """competency_code -> snapshot, pour les seules compétences du focus.
    Hors focus, seul competency_code est lu ; une compétence pertinente en
    double n'est jamais départagée."""
    if type(state.competencies) is not tuple:
        raise InvalidAssumptionBaselineState("competencies : tuple attendu")
    relevant = {}
    for snapshot in state.competencies:
        if type(snapshot) is not CompetencyAdaptationSnapshot:
            raise InvalidAssumptionBaselineState(f"compétence de type {type(snapshot).__name__}")
        code = snapshot.competency_code
        if type(code) is not str or code not in COMPETENCY_ORDER:
            raise InvalidAssumptionBaselineState(f"competency_code {code!r} hors de C1..C12")
        if code not in codes:
            continue
        if code in relevant:
            raise InvalidAssumptionBaselineState(f"{code} : snapshot en double (jamais départagé)")
        relevant[code] = snapshot
    for code, snapshot in relevant.items():
        _check_snapshot(snapshot, code)
    return relevant


# --------------------------------------------------------------------------
# Projection (pure)
# --------------------------------------------------------------------------

def _below_ceiling(current_stage: str) -> tuple:
    """Stades autorisés par le PLAFOND current_stage (jamais une couverture)."""
    if current_stage == NON_ETABLI:
        return ()
    return ASSUMPTION_STAGE_ORDER[:ASSUMPTION_STAGE_ORDER.index(current_stage) + 1]


def _localized_stages(snapshot: CompetencyAdaptationSnapshot, definition_id: uuid.UUID) -> tuple:
    """definition_id EXACT, explicitement représenté par une claim établie
    sous le plafond ; competency_only_basis ne localise jamais."""
    if definition_id not in {capability.definition_id for capability in snapshot.capabilities}:
        return ()
    allowed = _below_ceiling(snapshot.current_stage)
    return tuple(claim.stage for claim in snapshot.claims
                 if claim.status == _ESTABLISHED and claim.stage in allowed
                 and definition_id in claim.represented_capability_definition_ids)


def _competency_only_stages(snapshot: CompetencyAdaptationSnapshot, focus_release_id: uuid.UUID) -> tuple:
    """Base competency_only explicite uniquement, même release que le focus
    (aucune identité sémantique de compétence entre releases)."""
    if snapshot.taxonomy_release_id != focus_release_id:
        return ()
    allowed = _below_ceiling(snapshot.current_stage)
    return tuple(claim.stage for claim in snapshot.claims
                 if claim.status == _ESTABLISHED and claim.stage in allowed and claim.competency_only_basis is True)


def _competency_baseline(role: str, focus: CompetencyFocus, snapshot: CompetencyAdaptationSnapshot | None,
                         focus_release_id: uuid.UUID) -> CompetencyAssumptionBaseline:
    if focus.scope_mode == LOCALIZED:
        coverages = tuple(AssumptionCoverage(
            capability_definition_id=definition_id,
            established_stages=() if snapshot is None else _localized_stages(snapshot, definition_id),
        ) for definition_id in focus.capability_definition_ids)
    else:
        coverages = (AssumptionCoverage(
            capability_definition_id=None,
            established_stages=() if snapshot is None else _competency_only_stages(snapshot, focus_release_id),
        ),)
    return CompetencyAssumptionBaseline(
        role=role,
        competency_code=focus.competency_code,
        focus_scope_mode=focus.scope_mode,
        focus_capability_definition_ids=focus.capability_definition_ids,
        source_inference_run_id=None if snapshot is None else snapshot.active_inference_run_id,
        source_longitudinal_assessment_run_id=None if snapshot is None else snapshot.longitudinal_assessment_run_id,
        source_state_generation=None if snapshot is None else snapshot.state_generation,
        source_taxonomy_release_id=None if snapshot is None else snapshot.taxonomy_release_id,
        coverages=coverages,
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def build_assumption_baseline(
    *,
    state: AdaptationStateSnapshot,
    focus: InteractionCompetencyFocus,
) -> AssumptionBaseline:
    """Socle positif localisé : pour la cible et les supports du focus
    validé, échelle des stades Step 5 établis sur chaque périmètre demandé,
    sous le plafond current_stage. Fonction PURE (aucune base, horloge,
    environnement, hasard ni modèle) : même entrée => même sortie.

    Focus non résolu => ni cible ni support, aucune compétence inspectée.
    Erreurs : InvalidAssumptionBaselineArgument, InvalidAssumptionFocus,
    InvalidAssumptionBaselineState ; jamais de baseline partiel ou neutre
    implicite."""
    if type(state) is not AdaptationStateSnapshot:
        raise InvalidAssumptionBaselineArgument(f"AdaptationStateSnapshot attendu, reçu {type(state).__name__}")
    if type(focus) is not InteractionCompetencyFocus:
        raise InvalidAssumptionBaselineArgument(
            f"InteractionCompetencyFocus attendu, reçu {type(focus).__name__}")
    _check_focus(focus)

    target, supporting = None, ()
    if focus.resolution_status == RESOLVED:
        focused = (focus.target, *focus.supporting)
        snapshots = _relevant_snapshots(state, tuple(f.competency_code for f in focused))
        target = _competency_baseline(TARGET, focus.target, snapshots.get(focus.target.competency_code),
                                      focus.taxonomy_release_id)
        supporting = tuple(_competency_baseline(SUPPORTING, support, snapshots.get(support.competency_code),
                                                focus.taxonomy_release_id) for support in focus.supporting)

    return AssumptionBaseline(
        schema_version=ASSUMPTION_BASELINE_SCHEMA_VERSION,
        focus_schema_version=focus.schema_version,
        focus_policy_version=focus.policy_version,
        focus_taxonomy_release_id=focus.taxonomy_release_id,
        focus_taxonomy_spec_fingerprint=focus.taxonomy_spec_fingerprint,
        resolution_status=focus.resolution_status,
        target=target,
        supporting=supporting,
    )
