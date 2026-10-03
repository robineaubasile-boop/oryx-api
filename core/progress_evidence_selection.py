"""Étape 6.4B2 : sélection ÉDITORIALE de quelques exemples représentatifs
parmi les candidats sûrs de 6.4B1 — contrats, préflight, chemins
déterministes et validateur DÉTERMINISTE.

Question, et seulement elle : « parmi les observations déjà autorisées par
6.4B1, quel petit sous-ensemble rend le mieux compréhensible la carte
actuelle, sans redondance et sans juger la force probante des
observations ? ». Ce module en porte les contrats et les portes
déterministes ; la proposition sémantique vient du sélecteur 6-4B2
(core/progress_evidence_selector.py), qui dépend de ce module (jamais
l'inverse) :

    6-4A CompetencyCurrentProgress + 6-4B1 ProgressEvidenceSet
        -> prepare_progress_evidence_selection (préflight)
           -> ProgressEvidenceSelectionPlanning
    non available, ou available avec UNE candidate (aucune décision
    sémantique à prendre) :
        -> resolve_deterministic_progress_evidence_selection
           -> SelectedProgressEvidence (zéro appel modèle)
    available avec au moins deux candidates :
        ProgressEvidenceSelectionProposal (NON FIABLE, sortie du sélecteur)
        -> validate_progress_evidence_selection_proposal
           -> SelectedProgressEvidence

Frontière constitutionnelle. Step 5 possède l'état pédagogique ; 6-4A
décide de la carte affichée ; 6-4B1 est la SEULE frontière probante (elle
autorise les observations). 6-4B2 est ÉDITORIAL : il ne choisit, ne
modifie, ne confirme ni ne conteste aucun stade, ne décide pas si les
preuves suffisent, ne juge aucune force probante, ne recalcule aucune
confiance, ne classe aucune observation par qualité, n'utilise ni la
récence, ni le niveau d'aide, ni le mode d'élicitation, ne crée aucune
preuve, ne rédige aucun texte utilisateur et ne persiste rien.

selected_candidates = SOUS-ENSEMBLE ILLUSTRATIF, jamais un ensemble
causal complet : même si toutes les candidates sont retenues, la claim
affichée peut dépendre d'autres éléments (structure longitudinale de
Mastery, par exemple). Ce n'est ni un proof_set, ni une decision_basis, ni
l'ensemble des causes du stade : 6.4B3 pourra dire « parmi les éléments
sur lesquels Oryx s'appuie… », jamais « Oryx a décidé ce stade uniquement
parce que… ».

Invariants :

- Versions explicites : PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION décrit
  le contrat de sortie ; PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION décrit
  les règles de sélection. Cette policy n'est définie QUE pour les
  versions amont SUPPORTED_* (6-4A et 6-4B1) : toute autre version, du
  module amont ou de l'objet reçu, => UnsupportedProgressEvidenceSelectionVersion
  (jamais acceptée silencieusement). CompetencyCurrentProgress ne porte pas
  de version propre : celle de 6-4A est vérifiée au niveau de son module.

- MAX_SELECTED_EVIDENCE = 3 : nombre MAXIMUM d'exemples visibles, règle
  d'affichage uniquement. Jamais un minimum de preuves, un seuil de stade,
  une condition de confiance ni une métrique pédagogique. Aucune borne sur
  le nombre de candidates ni sur la longueur d'une observation.

- Préflight structurel strict (aucune réparation) : types exacts ; même
  compétence ; carte 6-4A bien formée (stade, libellé visible, couverture,
  capacités) ; statut B1 cohérent avec la carte ; hors available :
  basis_origin None, source_claim_stage None, aucune candidate ; available :
  basis_origin et source_claim_stage des vocabulaires B1 / Step 5 (direct
  => source = stade de la carte ; hérité => source strictement supérieure),
  au moins une candidate ; jetons exactement evidence_1 .. evidence_N ;
  chaque candidate : observation_text str non vide, encodable en UTF-8,
  sans NUL ; scope_mode localized (capacités non vides, distinctes, toutes
  représentées par la carte) ou competency_only (aucune capacité, carte
  competency_only ou mixed). 6-4B2 ne refait JAMAIS la provenance de
  6-4B1 (observation, positive_basis, T5, intégrité, T3, événement,
  release, Mastery) : il vérifie seulement que le contrat reçu est
  exploitable. Toute incohérence => InvalidProgressEvidenceSelectionInput.

- Chemins déterministes : non available => aucune sélection ; available
  avec une seule candidate => cette candidate ; aucun modèle n'est
  consulté quand aucune décision sémantique n'existe.

- Validateur : proposition de type exact ; versions exactes ; 1 à
  MAX_SELECTED_EVIDENCE jetons (aucun hors available) ; chacun connu,
  aucun doublon ; ordre = sous-séquence de l'ordre 6-4B1 (jamais retrié).
  Toute violation => InvalidProgressEvidenceSelectionProposal : jamais de
  troncature, de jeton ignoré, de tri ni de repli. Aucune couverture
  exhaustive, aucun quota par capacité ou par famille, aucune préférence
  localized / competency_only.

- Sortie minimale : provenance 6-4B1 (competency_code, evidence_status,
  basis_origin, source_claim_stage) propagée INCHANGÉE ; selected_candidates
  = les objets ProgressEvidenceCandidate ORIGINAUX de 6-4B1 (aucun texte ni
  candidat reconstruit). Aucun mode de sélection, aucun score, aucun rang,
  aucun texte, aucun UUID.

- Fonctions PURES : aucune base, aucune session, aucun service, aucun
  réseau, aucun modèle, aucune horloge, aucun hasard ; éphémère, aucune
  persistance, aucune SupportTrace, aucun branchement runtime.
"""
from dataclasses import dataclass

from core.adaptation_state import COMPETENCY_ORDER
from core.progress_evidence import (
    BASIS_DIRECT,
    BASIS_INHERITED_FROM_HIGHER_CLAIM,
    BASIS_ORIGINS,
    EVIDENCE_AVAILABLE,
    EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED,
    EVIDENCE_NO_POSITIVE_BASIS,
    EVIDENCE_NO_STATE,
    EVIDENCE_STATUSES,
    PROGRESS_EVIDENCE_POLICY_VERSION,
    PROGRESS_EVIDENCE_SCHEMA_VERSION,
    SCOPE_COMPETENCY_ONLY,
    SCOPE_LOCALIZED,
    SCOPE_MODES,
    ProgressEvidenceCandidate,
    ProgressEvidenceSet,
)
from core.progress_projection import (
    CLAIM_STAGE_ORDER,
    COVERAGE_COMPETENCY_ONLY,
    COVERAGE_LOCALIZED,
    COVERAGE_MIXED,
    COVERAGE_MODES,
    COVERAGE_NONE,
    CURRENT_PROGRESS_POLICY_VERSION,
    CURRENT_PROGRESS_SCHEMA_VERSION,
    NO_STATE_LABEL,
    NON_ETABLI,
    VISIBLE_STAGE_LABELS,
    CompetencyCurrentProgress,
    VisibleCapabilityProjection,
)

PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION = "progress-evidence-selection-v1"
PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION = "progress-evidence-selection-policy-1"

# Versions amont pour lesquelles cette policy est définie.
SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION = "current-progress-projection-v1"
SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"
SUPPORTED_PROGRESS_EVIDENCE_SCHEMA_VERSION = "progress-evidence-set-v1"
SUPPORTED_PROGRESS_EVIDENCE_POLICY_VERSION = "progress-evidence-policy-1"

# Nombre MAXIMUM d'exemples visibles : règle d'affichage, jamais un minimum
# de preuves, un seuil de stade ni une condition de confiance.
MAX_SELECTED_EVIDENCE = 3

_EVIDENCE_TOKEN_PREFIX = "evidence_"


class ProgressEvidenceSelectionError(Exception):
    """Erreur métier de 6-4B2 : jamais une sélection partielle, jamais un
    repli."""


class InvalidProgressEvidenceSelectionInput(ProgressEvidenceSelectionError):
    """Carte 6-4A ou ensemble 6-4B1 de type inattendu, mal formé ou
    incohérent entre eux (jamais réparé)."""


class UnsupportedProgressEvidenceSelectionVersion(ProgressEvidenceSelectionError):
    """Version amont (6-4A ou 6-4B1) pour laquelle cette policy n'est pas
    définie."""


class InvalidProgressEvidenceSelectionProposal(ProgressEvidenceSelectionError):
    """Proposition de sélection hors contrat (jamais corrigée)."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceSelectionCapability:
    """Capacité représentée par la carte, telle que visible du sélecteur :
    token "{capability_code}@r{semantic_revision}" et libellé seulement."""
    capability_token: str
    label: str


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceSelectionPlanning:
    """Entrée préparée de 6-4B2 : la carte et l'ensemble 6-4B1 ORIGINAUX,
    revérifiés, les capacités représentées par la carte et le seul fait
    technique « une décision sémantique existe » (available, au moins deux
    candidates). Objet interne : jamais exposé à 6-4B3."""
    schema_version: str
    policy_version: str
    card: CompetencyCurrentProgress
    evidence: ProgressEvidenceSet
    represented_capabilities: tuple[ProgressEvidenceSelectionCapability, ...]
    model_selection_required: bool


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceSelectionProposal:
    """Proposition NON FIABLE : uniquement des evidence_token, dans l'ordre
    6-4B1. Aucune raison, aucun score, aucun rang, aucun texte."""
    schema_version: str
    policy_version: str
    selected_evidence_tokens: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class SelectedProgressEvidence:
    """Exemples ILLUSTRATIFS d'UNE carte (jamais l'ensemble causal du
    stade). Provenance 6-4B1 inchangée ; selected_candidates : objets
    6-4B1 originaux, dans l'ordre 6-4B1, au plus MAX_SELECTED_EVIDENCE."""
    schema_version: str
    policy_version: str

    competency_code: str
    evidence_status: str

    basis_origin: str | None
    source_claim_stage: str | None

    selected_candidates: tuple[ProgressEvidenceCandidate, ...]


# --------------------------------------------------------------------------
# Préflight (pur)
# --------------------------------------------------------------------------

def _input_fail(path: str, detail: str) -> InvalidProgressEvidenceSelectionInput:
    return InvalidProgressEvidenceSelectionInput(f"{path} : {detail}")


def _check_versions(evidence: ProgressEvidenceSet) -> None:
    projection = (CURRENT_PROGRESS_SCHEMA_VERSION, CURRENT_PROGRESS_POLICY_VERSION)
    if projection != (SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION):
        raise UnsupportedProgressEvidenceSelectionVersion(
            f"{PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION} n'est pas définie pour la projection 6-4A {projection!r}")
    supported = (SUPPORTED_PROGRESS_EVIDENCE_SCHEMA_VERSION, SUPPORTED_PROGRESS_EVIDENCE_POLICY_VERSION)
    module = (PROGRESS_EVIDENCE_SCHEMA_VERSION, PROGRESS_EVIDENCE_POLICY_VERSION)
    received = (evidence.schema_version, evidence.policy_version)
    if module != supported or received != supported:
        raise UnsupportedProgressEvidenceSelectionVersion(
            f"{PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION} n'est pas définie pour les preuves 6-4B1 {received!r}"
            f" (module {module!r})")


def _non_empty_str(value) -> bool:
    return type(value) is str and bool(value.strip())


def _card_capabilities(card: CompetencyCurrentProgress) -> tuple:
    """Carte 6-4A bien formée et cohérente ; capacités représentées (token,
    libellé) dans l'ordre de la carte."""
    code = card.competency_code
    if type(code) is not str or code not in COMPETENCY_ORDER:
        raise _input_fail("card.competency_code", f"{code!r} hors de C1..C12")
    if not _non_empty_str(card.competency_label):
        raise _input_fail(f"{code} card.competency_label", "texte vide ou non str")
    if type(card.state_present) is not bool:
        raise _input_fail(f"{code} card.state_present", "bool attendu")
    stage = card.stage_code
    if card.state_present:
        if type(stage) is not str or stage not in VISIBLE_STAGE_LABELS:
            raise _input_fail(f"{code} card.stage_code", f"{stage!r} hors vocabulaire 6-4A")
        expected_label = VISIBLE_STAGE_LABELS[stage]
    else:
        if stage is not None:
            raise _input_fail(f"{code} card.stage_code", f"{stage!r} pour une carte sans état")
        expected_label = NO_STATE_LABEL
    if card.stage_label != expected_label:
        raise _input_fail(f"{code} card.stage_label", f"{card.stage_label!r} au lieu de {expected_label!r}")
    mode = card.coverage_mode
    if type(mode) is not str or mode not in COVERAGE_MODES:
        raise _input_fail(f"{code} card.coverage_mode", f"{mode!r} hors vocabulaire 6-4A")
    if stage not in CLAIM_STAGE_ORDER and mode != COVERAGE_NONE:
        raise _input_fail(f"{code} card.coverage_mode", f"{mode!r} pour le stade {stage!r}")
    capabilities = card.represented_capabilities
    if type(capabilities) is not tuple:
        raise _input_fail(f"{code} card.represented_capabilities", "tuple attendu")
    if (mode in (COVERAGE_LOCALIZED, COVERAGE_MIXED)) != bool(capabilities):
        raise _input_fail(f"{code} card.represented_capabilities",
                          f"{len(capabilities)} capacité(s) pour la couverture {mode!r}")
    visible = []
    for index, capability in enumerate(capabilities):
        path = f"{code} card.represented_capabilities[{index}]"
        if type(capability) is not VisibleCapabilityProjection:
            raise _input_fail(path, f"VisibleCapabilityProjection attendue, reçu {type(capability).__name__}")
        capability_code, revision = capability.capability_code, capability.semantic_revision
        if type(capability_code) is not str or not capability_code.startswith(f"{code}_"):
            raise _input_fail(path, f"capability_code {capability_code!r} hors de la compétence")
        if type(revision) is not int or revision < 1:
            raise _input_fail(path, f"semantic_revision {revision!r} invalide")
        if not _non_empty_str(capability.label):
            raise _input_fail(path, "libellé vide ou non str")
        visible.append(ProgressEvidenceSelectionCapability(
            capability_token=f"{capability_code}@r{revision}", label=capability.label))
    tokens = [capability.capability_token for capability in visible]
    if len(set(tokens)) != len(tokens):
        raise _input_fail(f"{code} card.represented_capabilities", "capacité en double")
    return tuple(visible)


def _expected_status(card: CompetencyCurrentProgress) -> tuple:
    """Statuts 6-4B1 compatibles avec la carte (jamais un statut déduit)."""
    if not card.state_present:
        return (EVIDENCE_NO_STATE,)
    if card.stage_code == NON_ETABLI:
        return (EVIDENCE_NO_POSITIVE_BASIS,)
    if card.coverage_mode == COVERAGE_NONE:
        return (EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED,)
    return (EVIDENCE_AVAILABLE,)


def _check_provenance(card: CompetencyCurrentProgress, evidence: ProgressEvidenceSet) -> None:
    code = card.competency_code
    status = evidence.evidence_status
    if type(status) is not str or status not in EVIDENCE_STATUSES:
        raise _input_fail(f"{code} evidence.evidence_status", f"{status!r} hors vocabulaire 6-4B1")
    if status not in _expected_status(card):
        raise _input_fail(f"{code} evidence.evidence_status",
                          f"{status!r} incompatible avec la carte ({card.stage_code!r}, {card.coverage_mode!r})")
    origin, source = evidence.basis_origin, evidence.source_claim_stage
    if status != EVIDENCE_AVAILABLE:
        if origin is not None or source is not None or evidence.candidates != ():
            raise _input_fail(f"{code} evidence", f"{status} avec une provenance ou des candidates")
        return
    if type(origin) is not str or origin not in BASIS_ORIGINS:
        raise _input_fail(f"{code} evidence.basis_origin", f"{origin!r} hors vocabulaire 6-4B1")
    if type(source) is not str or source not in CLAIM_STAGE_ORDER:
        raise _input_fail(f"{code} evidence.source_claim_stage", f"{source!r} hors des stades de claim")
    shown, origin_stage = CLAIM_STAGE_ORDER.index(card.stage_code), CLAIM_STAGE_ORDER.index(source)
    if (origin == BASIS_DIRECT and origin_stage != shown) or (
            origin == BASIS_INHERITED_FROM_HIGHER_CLAIM and origin_stage <= shown):
        raise _input_fail(f"{code} evidence", f"{origin} depuis {source} incompatible avec le stade {card.stage_code}")


def _check_text(value, path: str) -> None:
    if type(value) is not str:
        raise _input_fail(path, f"str attendu, reçu {type(value).__name__}")
    if not value.strip():
        raise _input_fail(path, "texte vide")
    if "\x00" in value:
        raise _input_fail(path, "caractère NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _input_fail(path, "texte non encodable en UTF-8") from exc


def _check_candidates(card: CompetencyCurrentProgress, evidence: ProgressEvidenceSet, visible: tuple) -> None:
    """Contrat 6-4B1 exploitable : jetons séquentiels, texte, portée. Jamais
    la provenance (propriété de 6-4B1)."""
    code = card.competency_code
    candidates = evidence.candidates
    if type(candidates) is not tuple:
        raise _input_fail(f"{code} evidence.candidates", "tuple attendu")
    if evidence.evidence_status == EVIDENCE_AVAILABLE and not candidates:
        raise _input_fail(f"{code} evidence.candidates", f"{EVIDENCE_AVAILABLE} sans aucune candidate")
    represented = frozenset(capability.capability_token for capability in visible)
    for index, candidate in enumerate(candidates):
        path = f"{code} evidence.candidates[{index}]"
        if type(candidate) is not ProgressEvidenceCandidate:
            raise _input_fail(path, f"ProgressEvidenceCandidate attendue, reçu {type(candidate).__name__}")
        expected = f"{_EVIDENCE_TOKEN_PREFIX}{index + 1}"
        if candidate.evidence_token != expected:
            raise _input_fail(f"{path}.evidence_token", f"{candidate.evidence_token!r} au lieu de {expected!r}")
        _check_text(candidate.observation_text, f"{path}.observation_text")
        scope, tokens = candidate.scope_mode, candidate.capability_tokens
        if type(scope) is not str or scope not in SCOPE_MODES:
            raise _input_fail(f"{path}.scope_mode", f"{scope!r} hors vocabulaire 6-4B1")
        if type(tokens) is not tuple or any(type(token) is not str for token in tokens):
            raise _input_fail(f"{path}.capability_tokens", "tuple de str attendu")
        if scope == SCOPE_COMPETENCY_ONLY:
            if tokens:
                raise _input_fail(f"{path}.capability_tokens", f"{SCOPE_COMPETENCY_ONLY} avec des capacités")
            if card.coverage_mode not in (COVERAGE_COMPETENCY_ONLY, COVERAGE_MIXED):
                raise _input_fail(path, f"{SCOPE_COMPETENCY_ONLY} sur une carte {card.coverage_mode}")
        elif scope == SCOPE_LOCALIZED:
            if not tokens:
                raise _input_fail(f"{path}.capability_tokens", f"{SCOPE_LOCALIZED} sans capacité")
            if len(set(tokens)) != len(tokens):
                raise _input_fail(f"{path}.capability_tokens", "capacité en double")
            outside = [token for token in tokens if token not in represented]
            if outside:
                raise _input_fail(f"{path}.capability_tokens", f"{outside} hors des capacités de la carte")


def _prepared(card, evidence) -> ProgressEvidenceSelectionPlanning:
    if type(card) is not CompetencyCurrentProgress:
        raise InvalidProgressEvidenceSelectionInput(
            f"CompetencyCurrentProgress attendue, reçu {type(card).__name__}")
    if type(evidence) is not ProgressEvidenceSet:
        raise InvalidProgressEvidenceSelectionInput(f"ProgressEvidenceSet attendu, reçu {type(evidence).__name__}")
    _check_versions(evidence)
    visible = _card_capabilities(card)
    if evidence.competency_code != card.competency_code:
        raise _input_fail("evidence.competency_code",
                          f"{evidence.competency_code!r} != carte {card.competency_code!r}")
    _check_provenance(card, evidence)
    _check_candidates(card, evidence, visible)
    return ProgressEvidenceSelectionPlanning(
        schema_version=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
        card=card,
        evidence=evidence,
        represented_capabilities=visible,
        model_selection_required=evidence.evidence_status == EVIDENCE_AVAILABLE and len(evidence.candidates) > 1,
    )


def _replayed(planning) -> ProgressEvidenceSelectionPlanning:
    """Planning reçu = EXACTEMENT le préflight rejoué sur sa carte et son
    ensemble 6-4B1 (un planning forgé n'est jamais cru)."""
    if type(planning) is not ProgressEvidenceSelectionPlanning:
        raise InvalidProgressEvidenceSelectionInput(
            f"ProgressEvidenceSelectionPlanning attendu, reçu {type(planning).__name__}")
    expected = _prepared(planning.card, planning.evidence)
    if planning != expected:
        raise InvalidProgressEvidenceSelectionInput("planning différent du préflight rejoué sur ses entrées")
    return expected


# --------------------------------------------------------------------------
# Validation d'une proposition (pure)
# --------------------------------------------------------------------------

def _proposal_fail(detail: str) -> InvalidProgressEvidenceSelectionProposal:
    return InvalidProgressEvidenceSelectionProposal(f"selected_evidence_tokens : {detail} (jamais corrigé)")


def _selected_indices(tokens, candidates: tuple) -> tuple:
    """Positions 6-4B1 des jetons proposés : 1 à MAX_SELECTED_EVIDENCE (aucun
    sans candidate), connus, distincts, sous-séquence de l'ordre 6-4B1."""
    if type(tokens) is not tuple or any(type(token) is not str for token in tokens):
        raise _proposal_fail("tuple de str attendu")
    if not candidates:
        if tokens:
            raise _proposal_fail(f"{list(tokens)} sans aucune candidate")
        return ()
    if not 1 <= len(tokens) <= MAX_SELECTED_EVIDENCE:
        raise _proposal_fail(f"{len(tokens)} jeton(s), 1 à {MAX_SELECTED_EVIDENCE} attendus")
    if len(set(tokens)) != len(tokens):
        raise _proposal_fail(f"{list(tokens)} : jeton en double")
    positions = {candidate.evidence_token: index for index, candidate in enumerate(candidates)}
    unknown = [token for token in tokens if token not in positions]
    if unknown:
        raise _proposal_fail(f"{unknown} absents des candidates")
    indices = tuple(positions[token] for token in tokens)
    if any(indices[k] >= indices[k + 1] for k in range(len(indices) - 1)):
        raise _proposal_fail(f"{list(tokens)} hors de l'ordre d'entrée (jamais retrié)")
    return indices


def _selected(planning: ProgressEvidenceSelectionPlanning, proposal) -> SelectedProgressEvidence:
    if type(proposal) is not ProgressEvidenceSelectionProposal:
        raise InvalidProgressEvidenceSelectionProposal(
            f"ProgressEvidenceSelectionProposal attendue, reçu {type(proposal).__name__}")
    versions = (proposal.schema_version, proposal.policy_version)
    if versions != (PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION, PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION):
        raise InvalidProgressEvidenceSelectionProposal(f"versions {versions!r} non supportées")
    evidence = planning.evidence
    candidates = evidence.candidates
    indices = _selected_indices(proposal.selected_evidence_tokens, candidates)
    return SelectedProgressEvidence(
        schema_version=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
        competency_code=evidence.competency_code,
        evidence_status=evidence.evidence_status,
        basis_origin=evidence.basis_origin,
        source_claim_stage=evidence.source_claim_stage,
        selected_candidates=tuple(candidates[index] for index in indices),
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def prepare_progress_evidence_selection(
    *,
    card: CompetencyCurrentProgress,
    evidence: ProgressEvidenceSet,
) -> ProgressEvidenceSelectionPlanning:
    """Préflight DÉTERMINISTE de 6-4B2 : versions amont supportées, carte et
    ensemble 6-4B1 structurellement exploitables et cohérents entre eux.
    Fonction PURE ; aucun appel modèle ; rien n'est corrigé.

    Erreurs : InvalidProgressEvidenceSelectionInput,
    UnsupportedProgressEvidenceSelectionVersion."""
    return _prepared(card, evidence)


def resolve_deterministic_progress_evidence_selection(
    *,
    planning: ProgressEvidenceSelectionPlanning,
) -> SelectedProgressEvidence:
    """Sélection sans décision sémantique : non available => aucune
    candidate ; available avec une seule candidate => cette candidate.
    Passe par le MÊME validateur. Planning exigeant une décision sémantique
    (au moins deux candidates) => InvalidProgressEvidenceSelectionInput :
    jamais une sélection arbitraire de repli."""
    planning = _replayed(planning)
    if planning.model_selection_required:
        raise InvalidProgressEvidenceSelectionInput(
            f"{len(planning.evidence.candidates)} candidates : sélection sémantique requise, aucun repli")
    return _selected(planning, ProgressEvidenceSelectionProposal(
        schema_version=PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
        selected_evidence_tokens=tuple(candidate.evidence_token for candidate in planning.evidence.candidates),
    ))


def validate_progress_evidence_selection_proposal(
    *,
    planning: ProgressEvidenceSelectionPlanning,
    proposal: ProgressEvidenceSelectionProposal,
) -> SelectedProgressEvidence:
    """ProgressEvidenceSelectionProposal NON FIABLE -> SelectedProgressEvidence.
    Fonction PURE : même planning + même proposition => même sélection.

    Le préflight est rejoué sur la carte et l'ensemble du planning. Puis :
    versions exactes ; 1 à MAX_SELECTED_EVIDENCE jetons (aucun sans
    candidate), connus, distincts, dans l'ordre 6-4B1. Toute violation =>
    InvalidProgressEvidenceSelectionProposal ; jamais de jeton ignoré, de
    troncature, de tri ni de repli. Aucune couverture ni quota exigés."""
    return _selected(_replayed(planning), proposal)
