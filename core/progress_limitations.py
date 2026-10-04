"""Étape 6.4D (préalable) : projection des limitations ACTUELLES déjà
établies par Step 5 (CurrentProgressLimitations).

Question, et seulement elle : « quelles limitations actuelles déjà établies
par Step 5 peuvent être projetées sans inventer de faiblesse, de checklist
ni de nouveau moteur ? ».

    6-1A AdaptationStateSnapshot + 6-4A CurrentProgressProjection
        -> project_current_progress_limitations -> CurrentProgressLimitations

Frontière constitutionnelle. Step 5 possède EXCLUSIVEMENT l'état
pédagogique ; 6-4A décide de la carte affichée. Ce module LIT deux faits
déjà décidés par Step 5 pour la claim du stade courant, et rien d'autre :
le fait de couverture unobserved_capabilities_present de son profil de
confiance, et les tensions du run capturé. Il ne choisit, ne maintient ni
ne dégrade aucun stade, ne recalcule aucune claim, confiance ni tension, ne
relance jamais T6, ne déduit aucune faiblesse, ne produit ni score, ni
gravité, ni priorité, ni action, ni prochaine étape.

Invariants :

- Versions explicites : CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION décrit
  le contrat de sortie ; CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION les
  règles de projection (statut less_observed, filtre et gabarits des
  tensions). Policy définie QUE pour la projection 6-4A SUPPORTED_* : toute
  autre version => UnsupportedCurrentProgressLimitationsVersion.

- Carte canonique : progress doit être EXACTEMENT
  project_current_progress(state=state) (égalité structurelle complète des
  douze cartes) ; les règles 6-4A ne sont jamais recopiées. Projection
  falsifiée, carte absente ou dupliquée =>
  IncompatibleCurrentProgressLimitationsInputs ; aucune réparation.

- less_observed_status (vocabulaire fermé) :
    * not_applicable : aucune claim courante POSITIVE à laquelle appliquer
      cette lecture : compétence sans état Step 5, current_stage non_etabli,
      ou claim du stade courant not_established (état Step 5 réel : maintien
      du stade précédent sous tension ouverte, accepté par 6-1A / 6-4A /
      6-4B1) ; toujours () ;
    * available : la lecture est structurellement disponible ; () signifie
      « aucune capacité actuellement signalée par
      unobserved_capabilities_present », jamais « information perdue » ;
    * unavailable : le profil historique confidence-profile-v1 porte le
      fait unobserved_capabilities_present, mais ses capacités y sont
      AGRÉGÉES par dimension : l'attribution précise est perdue ; toujours
      (). Aucun texte utilisateur n'est fabriqué pour ce statut.

- Source UNIQUE de less_observed : confidence_profile de la claim COURANTE,
  format choisi par son schema_version (jamais déduit de la forme) :
    * confidence-profile-v2 : validé par check_confidence_profile_v2
      (définition unique du format) contre le catalogue du snapshot ; puis
      coverage -> facts -> fait EXACT unobserved_capabilities_present ->
      SES capability_definition_ids, et rien d'autre (jamais les capacités
      d'un autre fait, jamais un agrégat). Fait absent => available, () ;
      fait présent sans capacité => état incohérent (Step 5 ne l'écrit
      jamais), InvalidCurrentProgressLimitationsState ;
    * confidence-profile-v1 : SEULE la clé coverage.fact_codes est lue
      (présence / absence du fait, conservée fiablement) ; jamais la clé
      facts, jamais coverage.capability_definition_ids (agrégés) ; fait
      absent => available, () ; fait présent => unavailable, () ;
    * tout autre schema_version => UnsupportedCurrentProgressLimitationsVersion.
  JAMAIS « toutes les capacités - capacités représentées » : aucune
  soustraction, aucune lecture du périmètre 6-4A, des observations ni de T5.

- Sémantique du fait (inchangée) : aucune observation POSITIVE située
  actuellement enregistrée dans le dossier courant sur cette capacité. Ce
  n'est jamais « jamais observée » : une capacité portant seulement des
  observations contradictoires, ou supportive sans profondeur exploitable,
  en fait partie et n'est jamais exclue ici. Ce n'est jamais une faiblesse :
  côté utilisateur, « cette dimension est encore peu observée par Oryx ».

- Identité des capacités : chaque capability_definition_id (str canonique)
  est résolu EXACTEMENT par definition_id dans
  CompetencyAdaptationSnapshot.capabilities, jamais par capability_code ;
  inconnu ou en double => InvalidCurrentProgressLimitationsState. Sortie :
  capability_code, semantic_revision, label, dans l'ordre de ce catalogue.

- Tensions : source UNIQUE snapshot.tensions (jamais les observations, le
  profil de confiance, validation_needs ni unresolved_revision_context).
  Visible seulement si current_stage != non_etabli ET fragilized_stage ==
  current_stage : une tension d'une claim supérieure ou inférieure n'est
  jamais projetée (6-4D n'est pas une checklist vers le stade suivant).
  Ordre canonique de snapshot.tensions conservé ; un texte par tension
  visible, aucune fusion, aucun tri par gravité. Gabarits fixes :
  localized => libellés EXACTS des capacités (résolues par definition_id
  dans le catalogue, ordre du catalogue) ; whole_competency /
  competency_only => formulation à l'échelle de la compétence, aucune
  capacité inventée. revision_status est validé mais jamais exposé :
  revalidation_needed ne devient ni « à revalider » ni une action.

- Exclusions : ni validation_needs, ni unresolved_revision_context, ni
  limitations internes de mastery_assessment ou de la base positive, ni
  fragilized_stage, scope_mode, revision_status, identifiant, UUID, gravité,
  priorité, compteur, score, pourcentage, action ou prochaine étape.

- Fonction PURE : aucune base, aucune session, aucun réseau, aucun modèle,
  aucune horloge, aucun hasard ; éphémère, aucune persistance, aucun
  branchement runtime. Même entrée => même sortie.
"""
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateSnapshot,
    AdaptationTension,
    CompetencyAdaptationSnapshot,
)
from core.inference_final_policies import InvalidConfidenceProfilePayload, check_confidence_profile_v2
from core.inference_service import (
    CLAIM_STAGES,
    ESTABLISHED,
    LOCALIZED,
    NOT_ESTABLISHED,
    REVISION_STATUSES,
    TENSION_OPEN,
    TENSION_SCOPE_MODES,
    TENSION_STATES,
)
from core.inference_state_policies import (
    CONFIDENCE_PROFILE_SCHEMA_V1,
    CONFIDENCE_PROFILE_SCHEMA_V2,
    COVERAGE,
    DIMENSION_FACT_CODES,
    UNOBSERVED_CAPABILITIES_PRESENT,
)
from core.progress_projection import (
    CURRENT_PROGRESS_POLICY_VERSION,
    CURRENT_PROGRESS_SCHEMA_VERSION,
    NON_ETABLI,
    CompetencyCurrentProgress,
    CurrentProgressProjection,
    ProgressProjectionError,
    project_current_progress,
)

CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION = "current-progress-limitations-v1"
CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION = "current-progress-limitations-policy-1"

# Version amont (6-4A) pour laquelle cette policy est définie.
SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION = "current-progress-projection-v1"
SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"

# CurrentProgressLimitations.less_observed_status : vocabulaire fermé V1.
LESS_OBSERVED_AVAILABLE = "available"
LESS_OBSERVED_NOT_APPLICABLE = "not_applicable"
LESS_OBSERVED_UNAVAILABLE = "unavailable"
LESS_OBSERVED_STATUSES = (
    LESS_OBSERVED_AVAILABLE,
    LESS_OBSERVED_NOT_APPLICABLE,
    LESS_OBSERVED_UNAVAILABLE,
)

# Gabarits fixes des tensions visibles (aucune gravité, aucune action).
LOCALIZED_TENSION_SINGLE_TEMPLATE = (
    "Des éléments observés restent contradictoires sur « {labels} »."
    " Oryx garde ce point comme une limite ouverte de son estimation.")
LOCALIZED_TENSION_MULTIPLE_TEMPLATE = (
    "Des éléments observés restent contradictoires sur {labels}."
    " Oryx garde ce périmètre comme une limite ouverte de son estimation.")
COMPETENCY_TENSION_TEXT = (
    "Des éléments observés restent contradictoires à l’échelle de cette compétence."
    " Oryx garde cette limite ouverte dans son estimation.")


class CurrentProgressLimitationsError(Exception):
    """Erreur métier de la projection des limitations : jamais une
    projection partielle, jamais un statut de repli."""


class InvalidCurrentProgressLimitationsArgument(CurrentProgressLimitationsError):
    """Argument top-level d'un type inattendu ou compétence inconnue."""


class IncompatibleCurrentProgressLimitationsInputs(CurrentProgressLimitationsError):
    """La projection fournie n'est pas EXACTEMENT la projection canonique
    6-4A du snapshot fourni."""


class UnsupportedCurrentProgressLimitationsVersion(CurrentProgressLimitationsError):
    """Version amont (6-4A, profil de confiance) pour laquelle cette policy
    n'est pas définie."""


class InvalidCurrentProgressLimitationsState(CurrentProgressLimitationsError):
    """Partie du snapshot nécessaire à cette projection incohérente : jamais
    réparée."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class LessObservedCapability:
    """Capacité sans observation positive située actuellement enregistrée
    dans le dossier courant (fait Step 5 unobserved_capabilities_present de
    la claim courante). Jamais une faiblesse. Même identité visible que
    VisibleCapabilityProjection (6-4A), aucun identifiant technique."""
    capability_code: str
    semantic_revision: int
    label: str


@dataclass(frozen=True, kw_only=True)
class VisibleCurrentTension:
    """Tension Step 5 fragilisant le stade courant, formulée par gabarit
    fixe ; ni stade, ni scope, ni statut de révision, ni gravité."""
    text: str


@dataclass(frozen=True, kw_only=True)
class CurrentProgressLimitations:
    """Limitations actuelles d'UNE compétence. less_observed_capabilities
    vide si less_observed_status est not_applicable ou unavailable."""
    schema_version: str
    policy_version: str

    competency_code: str

    less_observed_status: str
    less_observed_capabilities: tuple[LessObservedCapability, ...]

    current_tensions: tuple[VisibleCurrentTension, ...]

    def __post_init__(self):
        if self.less_observed_status not in LESS_OBSERVED_STATUSES:
            raise InvalidCurrentProgressLimitationsState(
                f"less_observed_status {self.less_observed_status!r} hors vocabulaire {LESS_OBSERVED_STATUSES}")
        if type(self.less_observed_capabilities) is not tuple:
            raise InvalidCurrentProgressLimitationsState("less_observed_capabilities : tuple attendu")
        if self.less_observed_status != LESS_OBSERVED_AVAILABLE and self.less_observed_capabilities:
            raise InvalidCurrentProgressLimitationsState(
                f"{self.less_observed_status} : aucune capacité attendue")


# --------------------------------------------------------------------------
# Validation des entrées (pure)
# --------------------------------------------------------------------------

def _invalid(competency_code: str, detail: str) -> InvalidCurrentProgressLimitationsState:
    return InvalidCurrentProgressLimitationsState(f"{competency_code} : {detail}")


def _check_arguments(state, progress, competency_code) -> None:
    if type(state) is not AdaptationStateSnapshot:
        raise InvalidCurrentProgressLimitationsArgument(
            f"AdaptationStateSnapshot attendu, reçu {type(state).__name__}")
    if type(progress) is not CurrentProgressProjection:
        raise InvalidCurrentProgressLimitationsArgument(
            f"CurrentProgressProjection attendue, reçu {type(progress).__name__}")
    if type(competency_code) is not str or competency_code not in COMPETENCY_ORDER:
        raise InvalidCurrentProgressLimitationsArgument(f"competency_code {competency_code!r} hors de C1..C12")


def _check_versions(progress: CurrentProgressProjection) -> None:
    supported = (SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION)
    module = (CURRENT_PROGRESS_SCHEMA_VERSION, CURRENT_PROGRESS_POLICY_VERSION)
    received = (progress.schema_version, progress.policy_version)
    if module != supported or received != supported:
        raise UnsupportedCurrentProgressLimitationsVersion(
            f"{CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION} n'est pas définie pour la projection 6-4A"
            f" {received!r} (module {module!r})")


def _canonical_card(state: AdaptationStateSnapshot, progress: CurrentProgressProjection,
                    competency_code: str) -> CompetencyCurrentProgress:
    """La carte fournie, seulement si la projection fournie est EXACTEMENT la
    projection canonique 6-4A de ce snapshot (règles jamais recopiées)."""
    cards = progress.competencies
    if type(cards) is not tuple or any(type(card) is not CompetencyCurrentProgress for card in cards):
        raise IncompatibleCurrentProgressLimitationsInputs(
            "progress.competencies : tuple de CompetencyCurrentProgress attendu")
    matching = [card for card in cards if card.competency_code == competency_code]
    if len(matching) != 1:
        raise IncompatibleCurrentProgressLimitationsInputs(
            f"{competency_code} : {len(matching)} carte(s), exactement une attendue")
    try:
        expected = project_current_progress(state=state)
    except ProgressProjectionError as exc:
        raise InvalidCurrentProgressLimitationsState(f"snapshot non projetable par 6-4A : {exc!r}") from exc
    if progress != expected:
        raise IncompatibleCurrentProgressLimitationsInputs(
            f"{competency_code} : projection fournie différente de project_current_progress(state)")
    return matching[0]


def _competency_snapshot(state: AdaptationStateSnapshot, competency_code: str) -> CompetencyAdaptationSnapshot:
    matching = [snapshot for snapshot in state.competencies if snapshot.competency_code == competency_code]
    if len(matching) != 1:
        raise _invalid(competency_code, f"{len(matching)} état(s) Step 5 dans le snapshot pour une carte présente")
    return matching[0]


def _current_claim(snapshot: CompetencyAdaptationSnapshot) -> AdaptationStageClaim:
    """Claim dont stage == current_stage (current_stage positif). Aucune
    autre claim n'est lue."""
    code = snapshot.competency_code
    claims = snapshot.claims
    if type(claims) is not tuple or any(type(claim) is not AdaptationStageClaim for claim in claims):
        raise _invalid(code, "claims : tuple d'AdaptationStageClaim attendu")
    if tuple(claim.stage for claim in claims) != CLAIM_STAGES:
        raise _invalid(code, f"claims : exactement {CLAIM_STAGES} attendues")
    claim = claims[CLAIM_STAGES.index(snapshot.current_stage)]
    if type(claim.status) is not str or claim.status not in (ESTABLISHED, NOT_ESTABLISHED):
        raise _invalid(code, f"{claim.stage} : status {claim.status!r} hors vocabulaire")
    return claim


def _catalogue(snapshot: CompetencyAdaptationSnapshot) -> dict:
    """definition_id -> position dans le catalogue validé du snapshot
    (6-4A a déjà vérifié sa structure en projetant la carte)."""
    return {capability.definition_id: index for index, capability in enumerate(snapshot.capabilities)}


# --------------------------------------------------------------------------
# less_observed : fait exact de la claim courante (pure)
# --------------------------------------------------------------------------

def _definition(code: str, value, catalogue: dict, path: str) -> uuid.UUID:
    """capability_definition_id sérialisé (str canonique) résolu EXACTEMENT
    dans le catalogue du snapshot ; jamais par capability_code."""
    if type(value) is not str:
        raise _invalid(code, f"{path} : UUID sérialisé attendu, reçu {value!r}")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise _invalid(code, f"{path} : UUID invalide {value!r}") from exc
    if str(parsed) != value or parsed not in catalogue:
        raise _invalid(code, f"{path} : {value!r} absent du catalogue du snapshot (aucune résolution par code)")
    return parsed


def _less_observed_v2(snapshot: CompetencyAdaptationSnapshot, profile: Mapping) -> tuple:
    code = snapshot.competency_code
    try:
        check_confidence_profile_v2(profile, tuple(str(c.definition_id) for c in snapshot.capabilities))
    except InvalidConfidenceProfilePayload as exc:
        raise _invalid(code, f"confidence_profile {CONFIDENCE_PROFILE_SCHEMA_V2} invalide ({exc})") from exc
    facts = [fact for fact in profile[COVERAGE]["facts"] if fact["code"] == UNOBSERVED_CAPABILITIES_PRESENT]
    if not facts:
        return LESS_OBSERVED_AVAILABLE, ()
    (fact,) = facts  # unicité garantie par check_confidence_profile_v2
    path = f"{COVERAGE}.{UNOBSERVED_CAPABILITIES_PRESENT}"
    catalogue = _catalogue(snapshot)
    ids = [_definition(code, value, catalogue, f"{path}[{i}]")
           for i, value in enumerate(fact["capability_definition_ids"])]
    if not ids:
        raise _invalid(code, f"{path} présent sans aucune capacité (jamais écrit par Step 5)")
    if len(set(ids)) != len(ids):
        raise _invalid(code, f"{path} : capacité en double")
    return LESS_OBSERVED_AVAILABLE, tuple(LessObservedCapability(
        capability_code=capability.capability_code,
        semantic_revision=capability.semantic_revision,
        label=capability.label,
    ) for capability in snapshot.capabilities if capability.definition_id in ids)


def _less_observed_v1(snapshot: CompetencyAdaptationSnapshot, profile: Mapping) -> tuple:
    """v1 historique : seule coverage.fact_codes est lue. Les capacités y
    sont agrégées par dimension : aucune attribution au fait n'est
    reconstruite."""
    code = snapshot.competency_code
    coverage = profile.get(COVERAGE)
    if not isinstance(coverage, Mapping) or "fact_codes" not in coverage:
        raise _invalid(code, f"confidence_profile {CONFIDENCE_PROFILE_SCHEMA_V1} : {COVERAGE}.fact_codes attendu")
    fact_codes = coverage["fact_codes"]
    if not isinstance(fact_codes, (list, tuple)) or any(
            type(fact) is not str or fact not in DIMENSION_FACT_CODES[COVERAGE] for fact in fact_codes) \
            or len(set(fact_codes)) != len(fact_codes):
        raise _invalid(code, f"confidence_profile {CONFIDENCE_PROFILE_SCHEMA_V1} : {COVERAGE}.fact_codes"
                             " hors vocabulaire ou en double")
    if UNOBSERVED_CAPABILITIES_PRESENT in fact_codes:
        return LESS_OBSERVED_UNAVAILABLE, ()
    return LESS_OBSERVED_AVAILABLE, ()


def _less_observed(snapshot: CompetencyAdaptationSnapshot, claim: AdaptationStageClaim) -> tuple:
    """(less_observed_status, capacités) de la claim du stade courant."""
    code = snapshot.competency_code
    profile = claim.confidence_profile
    if claim.status == NOT_ESTABLISHED:
        if profile is not None:
            raise _invalid(code, f"{claim.stage} {NOT_ESTABLISHED} avec un confidence_profile")
        return LESS_OBSERVED_NOT_APPLICABLE, ()
    if not isinstance(profile, Mapping):
        raise _invalid(code, f"{claim.stage} {ESTABLISHED} sans confidence_profile structuré")
    version = profile.get("schema_version")
    if type(version) is str and version == CONFIDENCE_PROFILE_SCHEMA_V2:
        return _less_observed_v2(snapshot, profile)
    if type(version) is str and version == CONFIDENCE_PROFILE_SCHEMA_V1:
        return _less_observed_v1(snapshot, profile)
    raise UnsupportedCurrentProgressLimitationsVersion(
        f"{code} : confidence_profile {version!r} non supporté"
        f" ({CONFIDENCE_PROFILE_SCHEMA_V1} / {CONFIDENCE_PROFILE_SCHEMA_V2})")


# --------------------------------------------------------------------------
# Tensions visibles (pure)
# --------------------------------------------------------------------------

def _tension_text(snapshot: CompetencyAdaptationSnapshot, tension: AdaptationTension) -> str:
    if tension.scope_mode != LOCALIZED:
        return COMPETENCY_TENSION_TEXT
    ids = frozenset(tension.capability_definition_ids)
    labels = [capability.label for capability in snapshot.capabilities if capability.definition_id in ids]
    if len(labels) == 1:
        return LOCALIZED_TENSION_SINGLE_TEMPLATE.format(labels=labels[0])
    return LOCALIZED_TENSION_MULTIPLE_TEMPLATE.format(labels=", ".join(f"« {label} »" for label in labels))


def _check_tension(snapshot: CompetencyAdaptationSnapshot, tension, catalogue: dict) -> None:
    code = snapshot.competency_code
    if type(tension) is not AdaptationTension:
        raise _invalid(code, f"tension de type {type(tension).__name__}")
    if type(tension.fragilized_stage) is not str or tension.fragilized_stage not in CLAIM_STAGES:
        raise _invalid(code, f"tension : fragilized_stage {tension.fragilized_stage!r} hors vocabulaire")
    if type(tension.scope_mode) is not str or tension.scope_mode not in TENSION_SCOPE_MODES:
        raise _invalid(code, f"tension : scope_mode {tension.scope_mode!r} hors vocabulaire")
    if type(tension.revision_status) is not str or tension.revision_status not in REVISION_STATUSES:
        raise _invalid(code, f"tension : revision_status {tension.revision_status!r} hors vocabulaire")
    ids = tension.capability_definition_ids
    if type(ids) is not tuple or any(type(i) is not uuid.UUID for i in ids):
        raise _invalid(code, "tension : capability_definition_ids tuple d'uuid.UUID attendu")
    if len(set(ids)) != len(ids):
        raise _invalid(code, "tension : capacité en double")
    if (tension.scope_mode == LOCALIZED) != bool(ids):
        raise _invalid(code, f"tension {tension.scope_mode} avec {len(ids)} capacité(s)"
                             " (aucun scope converti en un autre)")
    unknown = [str(i) for i in ids if i not in catalogue]
    if unknown:
        raise _invalid(code, f"tension : definition_id {unknown} absents du catalogue (aucune résolution par code)")


def _current_tensions(snapshot: CompetencyAdaptationSnapshot) -> tuple:
    """Tensions fragilisant EXACTEMENT le stade courant, ordre canonique de
    snapshot.tensions, un texte par tension."""
    code = snapshot.competency_code
    tensions = snapshot.tensions
    if type(tensions) is not tuple:
        raise _invalid(code, "tensions : tuple attendu")
    if type(snapshot.tension_state) is not str or snapshot.tension_state not in TENSION_STATES:
        raise _invalid(code, f"tension_state {snapshot.tension_state!r} hors vocabulaire")
    if bool(tensions) != (snapshot.tension_state == TENSION_OPEN):
        raise _invalid(code, f"{len(tensions)} tension(s) pour tension_state {snapshot.tension_state}")
    catalogue = _catalogue(snapshot)
    for tension in tensions:
        _check_tension(snapshot, tension, catalogue)
    return tuple(VisibleCurrentTension(text=_tension_text(snapshot, tension)) for tension in tensions
                 if tension.fragilized_stage == snapshot.current_stage)


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def _limitations(competency_code: str, status: str, capabilities: tuple, tensions: tuple) -> CurrentProgressLimitations:
    return CurrentProgressLimitations(
        schema_version=CURRENT_PROGRESS_LIMITATIONS_SCHEMA_VERSION,
        policy_version=CURRENT_PROGRESS_LIMITATIONS_POLICY_VERSION,
        competency_code=competency_code,
        less_observed_status=status,
        less_observed_capabilities=capabilities,
        current_tensions=tensions,
    )


def project_current_progress_limitations(
    *,
    state: AdaptationStateSnapshot,
    progress: CurrentProgressProjection,
    competency_code: str,
) -> CurrentProgressLimitations:
    """Limitations actuelles de la carte 6-4A de competency_code : capacités
    signalées par le fait Step 5 unobserved_capabilities_present de la claim
    courante, et tensions fragilisant le stade courant.

    progress doit être EXACTEMENT project_current_progress(state=state).
    Aucune state / non_etabli => not_applicable, aucune capacité, aucune
    tension. Fonction PURE : même entrée => même sortie.

    Erreurs : InvalidCurrentProgressLimitationsArgument,
    IncompatibleCurrentProgressLimitationsInputs,
    UnsupportedCurrentProgressLimitationsVersion,
    InvalidCurrentProgressLimitationsState ; jamais de résultat partiel."""
    _check_arguments(state, progress, competency_code)
    _check_versions(progress)
    card = _canonical_card(state, progress, competency_code)
    if not card.state_present:
        return _limitations(competency_code, LESS_OBSERVED_NOT_APPLICABLE, (), ())
    snapshot = _competency_snapshot(state, competency_code)
    if snapshot.current_stage != card.stage_code:
        raise _invalid(competency_code, f"current_stage {snapshot.current_stage!r} != stade de la carte"
                                        f" {card.stage_code!r}")
    if snapshot.current_stage == NON_ETABLI:
        return _limitations(competency_code, LESS_OBSERVED_NOT_APPLICABLE, (), ())
    status, capabilities = _less_observed(snapshot, _current_claim(snapshot))
    return _limitations(competency_code, status, capabilities, _current_tensions(snapshot))
