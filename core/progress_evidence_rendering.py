"""Étape 6.4B3 : rendu utilisateur FIDÈLE des exemples illustratifs déjà
sélectionnés par 6.4B2 — contrats, préflight, chemin déterministe et
validateur DÉTERMINISTE.

Question, et seulement elle : « comment transformer les exemples
illustratifs déjà sélectionnés par 6.4B2 en formulations utilisateur
fidèles, compréhensibles et non exagérées, sans ajouter de raisonnement, de
causalité ou d'évaluation qui n'existe pas dans les observations
sources ? ». Ce module en porte les contrats et les portes déterministes ;
la proposition de texte vient du renderer 6-4B3
(core/progress_evidence_renderer.py), qui dépend de ce module (jamais
l'inverse) :

    6-4A CompetencyCurrentProgress + 6-4B2 SelectedProgressEvidence
        -> prepare_progress_evidence_rendering (préflight)
           -> ProgressEvidenceRenderingPlanning
    non available (aucun exemple, aucun texte à rédiger) :
        -> resolve_deterministic_progress_evidence_rendering
           -> ProgressWhyRendering vide (zéro appel modèle)
    available (1 à MAX_SELECTED_EVIDENCE exemples, même un seul) :
        ProgressEvidenceRenderingProposal (NON FIABLE, sortie du renderer)
        -> validate_progress_evidence_rendering_proposal
           -> ProgressWhyRendering

Frontière constitutionnelle. Step 5 possède l'état pédagogique ; 6-4A
décide de la carte affichée ; 6-4B1 autorise les observations ; 6-4B2
choisit les exemples illustratifs. 6-4B3 REFORMULE, et seulement cela : il
ne choisit, ne confirme ni ne conteste aucun stade, ne sélectionne,
n'ajoute, ne retire, ne fusionne ni ne réordonne aucun exemple, ne
recalcule aucune confiance, ne juge aucune force probante, ne crée aucune
preuve, ne produit ni score, ni recommandation, ni prochaine étape, ni
résumé global, ni dimension « peu observée », ni tension, ni historique,
et n'écrit aucune trace de support. « Généré » n'est pas « montré » : une
future couche runtime / UI devra confirmer qu'un texte a réellement été
affiché avant de le tracer comme support cognitif.

Les exemples restent un SOUS-ENSEMBLE ILLUSTRATIF, jamais l'ensemble
causal du stade : 6-4B3 décrit ce que chaque exemple montre, jamais
pourquoi Oryx affiche ce stade. Une future introduction fixe (« Parmi les
éléments sur lesquels Oryx s'appuie : ») appartient au conteneur UI, pas à
6-4B3.

Invariants :

- Versions explicites : PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION décrit le
  contrat de sortie ; PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION décrit les
  règles de rendu. Cette policy n'est définie QUE pour les versions amont
  SUPPORTED_* (6-4A et 6-4B2) : toute autre version, du module amont ou de
  l'objet reçu, => UnsupportedProgressEvidenceRenderingVersion (jamais
  acceptée silencieusement).

- Entrées métier : la carte 6-4A et la sélection 6-4B2, rien d'autre (ni
  snapshot Step 5, ni ensemble 6-4B1 complet, ni base).

- Préflight structurel strict (aucune réparation) : types exacts ; même
  compétence ; carte 6-4A bien formée ; statut cohérent avec la carte ;
  hors available : basis_origin None, source_claim_stage None, aucun
  exemple ; available : basis_origin et source_claim_stage cohérents avec
  le stade affiché, 1 à MAX_SELECTED_EVIDENCE exemples (borne importée de
  6-4B2, jamais recopiée), jetons evidence_k distincts dans l'ordre 6-4B1 ;
  chaque exemple : ProgressEvidenceCandidate exacte, observation_text str
  non vide, encodable en UTF-8, sans NUL ; scope_mode, support_level et
  elicitation_mode des vocabulaires 6-4B1 / T3 ; capability_tokens tuple
  de str cohérent avec la carte. 6-4B3 ne refait JAMAIS la sélection
  (pertinence, non-redondance, couverture) ni la provenance 6-4B1. Toute
  incohérence => InvalidProgressEvidenceRenderingInput.

- Chemin déterministe : non available => ProgressWhyRendering sans
  exemple ; aucun modèle consulté. Il n'existe AUCUN chemin déterministe
  pour available, même avec un seul exemple : observation_text est une
  matière INTERNE, jamais un texte utilisateur ni un repli.

- Validateur : proposition de type exact ; versions exactes ; mapping 1:1
  STRICT : exactement un exemple rendu par exemple sélectionné, mêmes
  evidence_token dans le MÊME ordre (ni manquant, ni en trop, ni doublon,
  ni réordonné, ni fusionné) ; chaque text : str exacte, non vide après
  strip, encodable en UTF-8, sans NUL, sur UNE seule ligne (aucun \\n, \\r
  ni autre fin de ligne Unicode). Aucune limite de longueur par exemple
  (la seule garde est celle, globale, du transport du renderer). Toute
  violation => InvalidProgressEvidenceRenderingProposal : jamais de
  troncature, de jeton ignoré, de tri, de nettoyage ni de repli.

- Sortie minimale : competency_code, evidence_status et, par exemple,
  evidence_token (provenance INTERNE, jamais affichée) et text (SEUL
  contenu destiné à l'affichage). Ni stade, ni libellé, ni basis_origin,
  ni source_claim_stage, ni support_level, ni elicitation_mode, ni score,
  ni UUID : le stade reste la propriété de 6-4A.

- Fonctions PURES : aucune base, aucune session, aucun service, aucun
  réseau, aucun modèle, aucune horloge, aucun hasard ; éphémère, aucune
  persistance, aucun branchement runtime.
"""
from dataclasses import dataclass

from core.adaptation_state import COMPETENCY_ORDER
from core.progress_evidence import (
    BASIS_DIRECT,
    BASIS_INHERITED_FROM_HIGHER_CLAIM,
    BASIS_ORIGINS,
    ELICITATION_MODES,
    EVIDENCE_AVAILABLE,
    EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED,
    EVIDENCE_NO_POSITIVE_BASIS,
    EVIDENCE_NO_STATE,
    EVIDENCE_STATUSES,
    SCOPE_COMPETENCY_ONLY,
    SCOPE_LOCALIZED,
    SCOPE_MODES,
    SUPPORT_LEVELS,
    ProgressEvidenceCandidate,
)
from core.progress_evidence_selection import (
    MAX_SELECTED_EVIDENCE,
    PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION,
    PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
    SelectedProgressEvidence,
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

PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION = "progress-evidence-rendering-v1"
PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION = "progress-evidence-rendering-policy-1"

# Versions amont pour lesquelles cette policy est définie.
SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION = "current-progress-projection-v1"
SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"
SUPPORTED_PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION = "progress-evidence-selection-v1"
SUPPORTED_PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION = "progress-evidence-selection-policy-1"

_EVIDENCE_TOKEN_PREFIX = "evidence_"


class ProgressEvidenceRenderingError(Exception):
    """Erreur métier de 6-4B3 : jamais un rendu partiel, jamais un repli sur
    observation_text."""


class InvalidProgressEvidenceRenderingInput(ProgressEvidenceRenderingError):
    """Carte 6-4A ou sélection 6-4B2 de type inattendu, mal formée ou
    incohérente entre elles (jamais réparée)."""


class UnsupportedProgressEvidenceRenderingVersion(ProgressEvidenceRenderingError):
    """Version amont (6-4A ou 6-4B2) pour laquelle cette policy n'est pas
    définie."""


class InvalidProgressEvidenceRenderingProposal(ProgressEvidenceRenderingError):
    """Proposition de rendu hors contrat (jamais corrigée)."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class RenderedProgressEvidenceExample:
    """UN exemple rendu. evidence_token : provenance INTERNE (relie le texte
    à la candidate 6-4B1 sélectionnée), JAMAIS affichée ; text : SEUL
    contenu destiné à l'affichage, une ligne."""
    evidence_token: str
    text: str


@dataclass(frozen=True, kw_only=True)
class ProgressWhyRendering:
    """Formulations fidèles des exemples illustratifs d'UNE carte, dans
    l'ordre de la sélection 6-4B2, une par exemple sélectionné. Ni stade,
    ni libellé, ni métadonnée de provenance, ni UUID."""
    schema_version: str
    policy_version: str

    competency_code: str
    evidence_status: str

    examples: tuple[RenderedProgressEvidenceExample, ...]


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceRenderingProposalExample:
    """Exemple proposé (NON FIABLE) : jeton et texte, rien d'autre."""
    evidence_token: str
    text: str


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceRenderingProposal:
    """Proposition NON FIABLE du renderer : versions et exemples, rien
    d'autre (aucun résumé, aucune introduction, aucun score)."""
    schema_version: str
    policy_version: str
    examples: tuple[ProgressEvidenceRenderingProposalExample, ...]


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceRenderingCapability:
    """Capacité représentée par la carte, telle que visible du renderer :
    token "{capability_code}@r{semantic_revision}" et libellé seulement."""
    capability_token: str
    label: str


@dataclass(frozen=True, kw_only=True)
class ProgressEvidenceRenderingPlanning:
    """Entrée préparée de 6-4B3 : la carte et la sélection 6-4B2 ORIGINALES,
    revérifiées, les capacités représentées par la carte et le seul fait
    technique « un texte doit être rédigé » (available). Objet interne."""
    schema_version: str
    policy_version: str
    card: CompetencyCurrentProgress
    selection: SelectedProgressEvidence
    represented_capabilities: tuple[ProgressEvidenceRenderingCapability, ...]
    model_rendering_required: bool


# --------------------------------------------------------------------------
# Préflight (pur)
# --------------------------------------------------------------------------

def _input_fail(path: str, detail: str) -> InvalidProgressEvidenceRenderingInput:
    return InvalidProgressEvidenceRenderingInput(f"{path} : {detail}")


def _check_versions(selection: SelectedProgressEvidence) -> None:
    projection = (CURRENT_PROGRESS_SCHEMA_VERSION, CURRENT_PROGRESS_POLICY_VERSION)
    if projection != (SUPPORTED_CURRENT_PROGRESS_SCHEMA_VERSION, SUPPORTED_CURRENT_PROGRESS_POLICY_VERSION):
        raise UnsupportedProgressEvidenceRenderingVersion(
            f"{PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION} n'est pas définie pour la projection 6-4A {projection!r}")
    supported = (SUPPORTED_PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION,
                 SUPPORTED_PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION)
    module = (PROGRESS_EVIDENCE_SELECTION_SCHEMA_VERSION, PROGRESS_EVIDENCE_SELECTION_POLICY_VERSION)
    received = (selection.schema_version, selection.policy_version)
    if module != supported or received != supported:
        raise UnsupportedProgressEvidenceRenderingVersion(
            f"{PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION} n'est pas définie pour la sélection 6-4B2 {received!r}"
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
        visible.append(ProgressEvidenceRenderingCapability(
            capability_token=f"{capability_code}@r{revision}", label=capability.label))
    tokens = [capability.capability_token for capability in visible]
    if len(set(tokens)) != len(tokens):
        raise _input_fail(f"{code} card.represented_capabilities", "capacité en double")
    return tuple(visible)


def _expected_status(card: CompetencyCurrentProgress) -> str:
    """Statut 6-4B1 / 6-4B2 compatible avec la carte (jamais un statut déduit
    pour réparer la sélection)."""
    if not card.state_present:
        return EVIDENCE_NO_STATE
    if card.stage_code == NON_ETABLI:
        return EVIDENCE_NO_POSITIVE_BASIS
    if card.coverage_mode == COVERAGE_NONE:
        return EVIDENCE_CURRENT_STAGE_NOT_ESTABLISHED
    return EVIDENCE_AVAILABLE


def _check_provenance(card: CompetencyCurrentProgress, selection: SelectedProgressEvidence) -> None:
    code = card.competency_code
    status = selection.evidence_status
    if type(status) is not str or status not in EVIDENCE_STATUSES:
        raise _input_fail(f"{code} selection.evidence_status", f"{status!r} hors vocabulaire 6-4B1")
    if status != _expected_status(card):
        raise _input_fail(f"{code} selection.evidence_status",
                          f"{status!r} incompatible avec la carte ({card.stage_code!r}, {card.coverage_mode!r})")
    origin, source = selection.basis_origin, selection.source_claim_stage
    if status != EVIDENCE_AVAILABLE:
        if origin is not None or source is not None or selection.selected_candidates != ():
            raise _input_fail(f"{code} selection", f"{status} avec une provenance ou des exemples")
        return
    if type(origin) is not str or origin not in BASIS_ORIGINS:
        raise _input_fail(f"{code} selection.basis_origin", f"{origin!r} hors vocabulaire 6-4B1")
    if type(source) is not str or source not in CLAIM_STAGE_ORDER:
        raise _input_fail(f"{code} selection.source_claim_stage", f"{source!r} hors des stades de claim")
    shown, origin_stage = CLAIM_STAGE_ORDER.index(card.stage_code), CLAIM_STAGE_ORDER.index(source)
    if (origin == BASIS_DIRECT and origin_stage != shown) or (
            origin == BASIS_INHERITED_FROM_HIGHER_CLAIM and origin_stage <= shown):
        raise _input_fail(f"{code} selection", f"{origin} depuis {source} incompatible avec le stade {card.stage_code}")


def _check_text(value, path: str, fail) -> None:
    """str exacte, non vide après strip, sans NUL, encodable en UTF-8."""
    if type(value) is not str:
        raise fail(path, f"str attendu, reçu {type(value).__name__}")
    if not value.strip():
        raise fail(path, "texte vide")
    if "\x00" in value:
        raise fail(path, "caractère NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise fail(path, "texte non encodable en UTF-8") from exc


def _token_index(token) -> int | None:
    """k pour un jeton 6-4B1 "evidence_k" canonique (k >= 1), sinon None."""
    if type(token) is not str or not token.startswith(_EVIDENCE_TOKEN_PREFIX):
        return None
    digits = token.removeprefix(_EVIDENCE_TOKEN_PREFIX)
    if not digits.isascii() or not digits.isdigit() or digits.startswith("0"):
        return None
    return int(digits)


def _check_selected(card: CompetencyCurrentProgress, selection: SelectedProgressEvidence, visible: tuple) -> None:
    """Sélection 6-4B2 exploitable : nombre, jetons, texte, vocabulaires,
    portée. Jamais la pertinence de la sélection ni la provenance 6-4B1."""
    code = card.competency_code
    selected = selection.selected_candidates
    if type(selected) is not tuple:
        raise _input_fail(f"{code} selection.selected_candidates", "tuple attendu")
    if selection.evidence_status == EVIDENCE_AVAILABLE and not 1 <= len(selected) <= MAX_SELECTED_EVIDENCE:
        raise _input_fail(f"{code} selection.selected_candidates",
                          f"{len(selected)} exemple(s), 1 à {MAX_SELECTED_EVIDENCE} attendus"
                          f" pour {EVIDENCE_AVAILABLE}")
    represented = frozenset(capability.capability_token for capability in visible)
    previous = 0
    for index, candidate in enumerate(selected):
        path = f"{code} selection.selected_candidates[{index}]"
        if type(candidate) is not ProgressEvidenceCandidate:
            raise _input_fail(path, f"ProgressEvidenceCandidate attendue, reçu {type(candidate).__name__}")
        position = _token_index(candidate.evidence_token)
        if position is None:
            raise _input_fail(f"{path}.evidence_token", f"{candidate.evidence_token!r} hors du format evidence_k")
        if position <= previous:
            raise _input_fail(f"{path}.evidence_token",
                              f"{candidate.evidence_token!r} en double ou hors de l'ordre 6-4B1 (jamais retrié)")
        previous = position
        _check_text(candidate.observation_text, f"{path}.observation_text", _input_fail)
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
        if type(candidate.support_level) is not str or candidate.support_level not in SUPPORT_LEVELS:
            raise _input_fail(f"{path}.support_level", f"{candidate.support_level!r} hors vocabulaire 6-4B1 / T3")
        if type(candidate.elicitation_mode) is not str or candidate.elicitation_mode not in ELICITATION_MODES:
            raise _input_fail(f"{path}.elicitation_mode",
                              f"{candidate.elicitation_mode!r} hors vocabulaire 6-4B1 / T3")


def _prepared(card, selection) -> ProgressEvidenceRenderingPlanning:
    if type(card) is not CompetencyCurrentProgress:
        raise InvalidProgressEvidenceRenderingInput(
            f"CompetencyCurrentProgress attendue, reçu {type(card).__name__}")
    if type(selection) is not SelectedProgressEvidence:
        raise InvalidProgressEvidenceRenderingInput(
            f"SelectedProgressEvidence attendue, reçu {type(selection).__name__}")
    _check_versions(selection)
    visible = _card_capabilities(card)
    if selection.competency_code != card.competency_code:
        raise _input_fail("selection.competency_code",
                          f"{selection.competency_code!r} != carte {card.competency_code!r}")
    _check_provenance(card, selection)
    _check_selected(card, selection, visible)
    return ProgressEvidenceRenderingPlanning(
        schema_version=PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
        card=card,
        selection=selection,
        represented_capabilities=visible,
        model_rendering_required=selection.evidence_status == EVIDENCE_AVAILABLE,
    )


def _replayed(planning) -> ProgressEvidenceRenderingPlanning:
    """Planning reçu = EXACTEMENT le préflight rejoué sur sa carte et sa
    sélection (un planning forgé n'est jamais cru)."""
    if type(planning) is not ProgressEvidenceRenderingPlanning:
        raise InvalidProgressEvidenceRenderingInput(
            f"ProgressEvidenceRenderingPlanning attendu, reçu {type(planning).__name__}")
    expected = _prepared(planning.card, planning.selection)
    if planning != expected:
        raise InvalidProgressEvidenceRenderingInput("planning différent du préflight rejoué sur ses entrées")
    return expected


# --------------------------------------------------------------------------
# Validation d'une proposition (pure)
# --------------------------------------------------------------------------

def _proposal_fail(path: str, detail: str) -> InvalidProgressEvidenceRenderingProposal:
    return InvalidProgressEvidenceRenderingProposal(f"{path} : {detail} (jamais corrigé)")


def _check_rendered_text(value, path: str) -> None:
    """Texte rendu : contrôles structurels seulement (aucune sémantique,
    aucun comptage de phrases, aucune limite de longueur) ; une seule
    ligne."""
    _check_text(value, path, _proposal_fail)
    if value.splitlines() != [value]:
        raise _proposal_fail(path, "saut de ligne (\\n, \\r ou autre fin de ligne) : une seule ligne attendue")


def _check_mapping(tokens: tuple, expected: tuple) -> None:
    """Mapping 1:1 STRICT : mêmes jetons, même nombre, même ordre."""
    if tokens == expected:
        return
    if len(set(tokens)) != len(tokens):
        raise _proposal_fail("examples", f"{list(tokens)} : jeton en double")
    missing = [token for token in expected if token not in tokens]
    extra = [token for token in tokens if token not in expected]
    if missing or extra:
        raise _proposal_fail("examples", f"{list(tokens)} au lieu de {list(expected)}"
                                         f" (manquants {missing}, en trop {extra} ; aucune fusion, aucun ajout)")
    raise _proposal_fail("examples", f"{list(tokens)} hors de l'ordre de la sélection {list(expected)}"
                                     " (jamais retrié)")


def _rendered(planning: ProgressEvidenceRenderingPlanning, proposal) -> ProgressWhyRendering:
    if type(proposal) is not ProgressEvidenceRenderingProposal:
        raise InvalidProgressEvidenceRenderingProposal(
            f"ProgressEvidenceRenderingProposal attendue, reçu {type(proposal).__name__}")
    versions = (proposal.schema_version, proposal.policy_version)
    if versions != (PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION, PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION):
        raise InvalidProgressEvidenceRenderingProposal(f"versions {versions!r} non supportées")
    examples = proposal.examples
    if type(examples) is not tuple:
        raise _proposal_fail("examples", "tuple attendu")
    for index, example in enumerate(examples):
        if type(example) is not ProgressEvidenceRenderingProposalExample:
            raise _proposal_fail(f"examples[{index}]",
                                 f"ProgressEvidenceRenderingProposalExample attendu, reçu {type(example).__name__}")
        if type(example.evidence_token) is not str:
            raise _proposal_fail(f"examples[{index}].evidence_token",
                                 f"str attendu, reçu {type(example.evidence_token).__name__}")
    selected = planning.selection.selected_candidates
    _check_mapping(tuple(example.evidence_token for example in examples),
                   tuple(candidate.evidence_token for candidate in selected))
    for index, example in enumerate(examples):
        _check_rendered_text(example.text, f"examples[{index}].text")
    return ProgressWhyRendering(
        schema_version=PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
        competency_code=planning.selection.competency_code,
        evidence_status=planning.selection.evidence_status,
        examples=tuple(RenderedProgressEvidenceExample(evidence_token=candidate.evidence_token, text=example.text)
                       for candidate, example in zip(selected, examples)),
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def prepare_progress_evidence_rendering(
    *,
    card: CompetencyCurrentProgress,
    selection: SelectedProgressEvidence,
) -> ProgressEvidenceRenderingPlanning:
    """Préflight DÉTERMINISTE de 6-4B3 : versions amont supportées, carte et
    sélection 6-4B2 structurellement exploitables et cohérentes entre elles.
    Fonction PURE ; aucun appel modèle ; rien n'est corrigé.

    Erreurs : InvalidProgressEvidenceRenderingInput,
    UnsupportedProgressEvidenceRenderingVersion."""
    return _prepared(card, selection)


def resolve_deterministic_progress_evidence_rendering(
    *,
    planning: ProgressEvidenceRenderingPlanning,
) -> ProgressWhyRendering:
    """Rendu sans rédaction : non available => aucun exemple. Passe par le
    MÊME validateur. Planning available (au moins un exemple à rédiger) =>
    InvalidProgressEvidenceRenderingInput : jamais observation_text affiché
    en repli."""
    planning = _replayed(planning)
    if planning.model_rendering_required:
        raise InvalidProgressEvidenceRenderingInput(
            f"{len(planning.selection.selected_candidates)} exemple(s) {EVIDENCE_AVAILABLE} :"
            " rédaction requise, aucun repli sur le texte interne")
    return _rendered(planning, ProgressEvidenceRenderingProposal(
        schema_version=PROGRESS_EVIDENCE_RENDERING_SCHEMA_VERSION,
        policy_version=PROGRESS_EVIDENCE_RENDERING_POLICY_VERSION,
        examples=(),
    ))


def validate_progress_evidence_rendering_proposal(
    *,
    planning: ProgressEvidenceRenderingPlanning,
    proposal: ProgressEvidenceRenderingProposal,
) -> ProgressWhyRendering:
    """ProgressEvidenceRenderingProposal NON FIABLE -> ProgressWhyRendering.
    Fonction PURE : même planning + même proposition => même rendu.

    Le préflight est rejoué sur la carte et la sélection du planning. Puis :
    versions exactes ; exactement un exemple par exemple sélectionné, mêmes
    evidence_token dans le même ordre ; chaque text non vide, UTF-8, sans
    NUL, sur une seule ligne. Toute violation =>
    InvalidProgressEvidenceRenderingProposal ; jamais de jeton ignoré, de
    troncature, de tri, de nettoyage ni de repli."""
    return _rendered(_replayed(planning), proposal)
