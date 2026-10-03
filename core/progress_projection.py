"""Étape 6.4A : projection DESCRIPTIVE de l'état pédagogique actuel
(CurrentProgressProjection).

Question, et seulement elle : « quel état actuel et quel périmètre
positivement établi peuvent être exposés à l'utilisateur sans ambiguïté,
sans créer de nouvelle inférence, de score, de checklist ni de second moteur
de progression ? ». Montrer des capacités, pas attribuer une note à la
personne : la vue Progression est un miroir, jamais une checklist. Absence
de preuve = limite d'observation d'Oryx, jamais faiblesse de l'utilisateur.

    6-1A AdaptationStateSnapshot (ce que Step 5 affirme, déjà validé)
    + SPEC canonique V1 (libellés des compétences, load_taxonomy_v1)
        -> project_current_progress -> CurrentProgressProjection

Frontière constitutionnelle. Step 5 possède EXCLUSIVEMENT l'état
pédagogique ; 6-4A le LIT à travers la seule frontière sûre de 6-1A et ne
relit jamais la base (ni cache d'état, ni run T6, ni claim, ni tension, ni
run T5). 6-4A est une PROJECTION, jamais une inférence : il ne choisit, ne
maintient, ne dégrade ni n'augmente jamais un stade ; il ne calcule aucune
confiance ; il ne décide jamais qu'une capacité est faible ou manquante ; il
ne recommande rien, ne choisit aucune prochaine compétence, n'active aucune
validation, ne crée ni observation ni preuve. Il est INDÉPENDANT des
décisions interactionnelles 6.1-6.3 (tâche, posture, support, mouvement) :
aucun objet de ces étapes n'est importé ni lu ; 6-4A projette l'état
durable actuel, pas la question courante.

Invariants :

- Versions explicites : CURRENT_PROGRESS_SCHEMA_VERSION décrit le contrat de
  sortie ; CURRENT_PROGRESS_POLICY_VERSION décrit les règles de projection
  (libellés visibles, sélection de la couverture). Aucune version implicite.

- Douze cartes, toujours : C1 -> C12, dans l'ordre conceptuel (jamais un tri
  lexical, jamais l'ordre d'entrée), quel que soit le nombre de compétences
  présentes dans le snapshot (un snapshot vide est VALIDE : douze cartes sans
  état).

- Absence != non_etabli. Compétence absente du snapshot (aucun état Step 5
  validé) : state_present False, stage_code None, NO_STATE_LABEL, coverage
  none, aucune capacité. Compétence présente avec current_stage non_etabli
  (Step 5 a évalué le dossier, aucune claim positive actuelle) :
  state_present True, stage_code non_etabli, son libellé visible propre,
  coverage none. Jamais confondus ; une corruption n'est JAMAIS convertie en
  NO_STATE_LABEL (ce libellé désigne une vraie absence d'état).

- Stade visible = EXACTEMENT current_stage décidé par Step 5, même sous
  tension / révision ouverte, même si la claim de ce stade n'est plus
  established. Libellé : VISIBLE_STAGE_LABELS, table pure et fermée ; aucun
  « Débutant / Intermédiaire / Avancé / Expert », aucun « niveau n ».

- Stade et couverture sont DISTINCTS. La couverture ne lit que la claim
  dont stage == current_stage, telle que reconstruite par 6-1A :
  * current_stage non_etabli => none (aucune claim non_etabli cherchée) ;
  * claim courante not_established => none, aucune capacité : JAMAIS de
    repli sur une claim inférieure établie (son périmètre ne serait pas
    celui du stade affiché) ;
  * claim courante established => SON périmètre, tel quel :
      capacités non vides, competency_only_basis False => localized ;
      capacités vides,     competency_only_basis True  => competency_only ;
      capacités non vides, competency_only_basis True  => mixed ;
      capacités vides,     competency_only_basis False => état incohérent,
      InvalidProgressProjectionState (jamais converti en none).
  basis_mode n'est jamais lu : une claim implied_by_higher_claim porte déjà
  le périmètre reconstruit par 6-1A (aucune recherche de claim source) ; une
  claim mastery porte déjà le périmètre canonique de son mastery_assessment
  (jamais « toutes les capacités de la compétence »).

- Ce qui est représenté, rien d'autre. represented_capabilities = les seules
  capacités EXPLICITEMENT localisées par la claim courante (en mixed, la
  part competency-wide ne localise rien). Aucune capacité « moins
  observée », « manquante », « à travailler » n'est calculée : une capacité
  absente de ce périmètre a pu être observée autrement ; ce constat
  appartient à 6-4B, après lecture des preuves actives.

- Capacités : chaque definition_id représenté est résolu EXACTEMENT dans
  CompetencyAdaptationSnapshot.capabilities (catalogue validé de la release
  du snapshot), jamais par capability_code ; inconnu, en double, capacité
  d'une autre compétence ou hors de l'ordre canonique =>
  InvalidProgressProjectionState. La sortie retire ensuite toute identité
  technique : capability_code, semantic_revision et label seulement, dans
  l'ordre de ce catalogue.

- Libellés des compétences : SPEC canonique V1 validée et fingerprintée
  (load_taxonomy_v1), jamais recopiés ici ; SPEC invalide ou modifiée sans
  décision explicite => UnsupportedProgressProjectionVersion.

- Exclusions : aucun identifiant d'utilisateur, aucun UUID, aucun
  identifiant de run / release / définition / membership, aucun compteur
  d'état technique ; ni tension, ni contexte de révision, ni besoin de
  validation, ni profil de confiance, ni évaluation de maîtrise ne sont lus
  ou exposés (modifier uniquement l'un d'eux ne change jamais la sortie).
  Aucun score, pourcentage, moyenne, compteur, XP, rang, comparaison entre
  compétences, recommandation, prochaine étape, historique, explication ni
  exemple de preuve (6-4B / 6-4C).

- Validation minimale fail-closed : seuls les invariants nécessaires à
  6-4A sont revérifiés sur l'objet public (type exact, compétence connue et
  unique, current_stage du vocabulaire Step 5 ; hors non_etabli : quatre
  claims discovery -> mastery, contenu de la seule claim courante, catalogue
  de capacités) ; 6-1A reste le propriétaire du reste. Aucune réparation,
  aucune projection partielle.

- Fonction PURE : aucune base, aucune session, aucun réseau, aucun modèle,
  aucune horloge, aucun hasard, aucun environnement ; éphémère, aucune
  persistance, aucun branchement runtime. Même snapshot => même projection.
"""
import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType

from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateSnapshot,
    CapabilitySemanticRef,
    CompetencyAdaptationSnapshot,
)
from core.pedagogy.taxonomy_v1 import TaxonomySpecError, load_taxonomy_v1

CURRENT_PROGRESS_SCHEMA_VERSION = "current-progress-projection-v1"
CURRENT_PROGRESS_POLICY_VERSION = "current-progress-policy-1"

# Vocabulaire Step 5 (miroir figé de core.inference_service, vérifié par les
# tests) : current_stage sans claim positive, puis stades des claims dans
# leur ordre conceptuel (jamais un nombre).
NON_ETABLI = "non_etabli"
CLAIM_STAGE_ORDER = ("discovery", "comprehension", "application", "mastery")
_ESTABLISHED = "established"
_NOT_ESTABLISHED = "not_established"

# Libellé d'une compétence SANS état Step 5 validé (jamais un stade).
NO_STATE_LABEL = "Pas encore observé par Oryx"

# Libellés visibles V1 de current_stage : table pure, fermée, sans variante.
VISIBLE_STAGE_LABELS = MappingProxyType({
    NON_ETABLI: "Encore peu observé par Oryx",
    "discovery": "Notion reconnue",
    "comprehension": "Mécanisme compris",
    "application": "Utilisé en situation",
    "mastery": "Raisonnement solide dans des contextes variés",
})

# CompetencyCurrentProgress.coverage_mode : descriptif, ni ordonné ni score.
COVERAGE_NONE = "none"
COVERAGE_LOCALIZED = "localized"
COVERAGE_COMPETENCY_ONLY = "competency_only"
COVERAGE_MIXED = "mixed"
COVERAGE_MODES = (
    COVERAGE_NONE,
    COVERAGE_LOCALIZED,
    COVERAGE_COMPETENCY_ONLY,
    COVERAGE_MIXED,
)

_CAPABILITY_CODE = re.compile(r"(C[1-9][0-9]*)_[A-Z]")


class ProgressProjectionError(Exception):
    """Erreur métier de 6-4A : jamais une projection partielle, jamais une
    carte « sans état » de repli."""


class InvalidProgressProjectionArgument(ProgressProjectionError):
    """Argument top-level d'un type inattendu."""


class InvalidProgressProjectionState(ProgressProjectionError):
    """Partie de l'AdaptationStateSnapshot nécessaire à 6-4A incohérente."""


class UnsupportedProgressProjectionVersion(ProgressProjectionError):
    """Source canonique des libellés (SPEC V1) invalide ou non supportée."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class VisibleCapabilityProjection:
    """Capacité positivement représentée par la claim du stade courant.
    Descriptive : ni stade, ni confiance, ni identifiant technique."""
    capability_code: str
    semantic_revision: int
    label: str


@dataclass(frozen=True, kw_only=True)
class CompetencyCurrentProgress:
    """Carte d'UNE compétence. state_present False <=> aucun état Step 5
    validé (stage_code None) ; stage_code = current_stage Step 5 tel quel ;
    represented_capabilities : uniquement le périmètre localisé de la claim
    courante établie, ordre du catalogue de la release."""
    competency_code: str
    competency_label: str

    state_present: bool

    stage_code: str | None
    stage_label: str

    coverage_mode: str

    represented_capabilities: tuple[VisibleCapabilityProjection, ...]


@dataclass(frozen=True, kw_only=True)
class CurrentProgressProjection:
    """Vue descriptive actuelle : exactement douze cartes, C1 -> C12. Aucune
    identité de personne."""
    schema_version: str
    policy_version: str
    competencies: tuple[CompetencyCurrentProgress, ...]


# --------------------------------------------------------------------------
# Libellés canoniques des compétences (SPEC V1, Python pur)
# --------------------------------------------------------------------------

def _competency_labels() -> dict:
    """competency_code -> libellé canonique de la SPEC V1 validée et
    fingerprintée ; jamais une copie locale des douze libellés."""
    try:
        spec, _ = load_taxonomy_v1()
    except TaxonomySpecError as exc:
        raise UnsupportedProgressProjectionVersion(f"SPEC canonique V1 non supportée : {exc}") from exc
    labels = {competency["competency_code"]: competency["label"] for competency in spec["competencies"]}
    if sorted(labels) != sorted(COMPETENCY_ORDER):
        raise UnsupportedProgressProjectionVersion(f"SPEC canonique : compétences {sorted(labels)} != C1..C12")
    return labels


# --------------------------------------------------------------------------
# Validation structurelle minimale (pure)
# --------------------------------------------------------------------------

def _state_fail(competency_code: str, message: str) -> InvalidProgressProjectionState:
    return InvalidProgressProjectionState(f"{competency_code} : {message}")


def _present_competencies(state: AdaptationStateSnapshot) -> dict:
    """competency_code -> snapshot ; une compétence en double n'est jamais
    départagée, l'ordre d'entrée n'est jamais conservé."""
    if type(state.competencies) is not tuple:
        raise InvalidProgressProjectionState("competencies : tuple attendu")
    present = {}
    for snapshot in state.competencies:
        if type(snapshot) is not CompetencyAdaptationSnapshot:
            raise InvalidProgressProjectionState(f"compétence de type {type(snapshot).__name__}")
        code = snapshot.competency_code
        if type(code) is not str or code not in COMPETENCY_ORDER:
            raise InvalidProgressProjectionState(f"competency_code {code!r} hors de C1..C12")
        if code in present:
            raise InvalidProgressProjectionState(f"{code} : état en double (jamais départagé)")
        present[code] = snapshot
    return present


def _catalogue(snapshot: CompetencyAdaptationSnapshot) -> tuple:
    """Catalogue de capacités du snapshot, vérifié : identités uniques, toutes
    de CETTE compétence, champs exposables bien formés. Ordre conservé."""
    code = snapshot.competency_code
    if type(snapshot.capabilities) is not tuple:
        raise _state_fail(code, "capabilities : tuple attendu")
    definitions, codes = set(), set()
    for capability in snapshot.capabilities:
        if type(capability) is not CapabilitySemanticRef:
            raise _state_fail(code, f"capacité de type {type(capability).__name__}")
        if type(capability.definition_id) is not uuid.UUID:
            raise _state_fail(code, f"definition_id {capability.definition_id!r} non uuid.UUID")
        if capability.definition_id in definitions:
            raise _state_fail(code, f"definition_id {capability.definition_id} en double")
        definitions.add(capability.definition_id)
        capability_code = capability.capability_code
        match = _CAPABILITY_CODE.fullmatch(capability_code) if type(capability_code) is str else None
        if match is None or match.group(1) != code:
            raise _state_fail(code, f"capacité {capability_code!r} hors de la compétence")
        if capability_code in codes:
            raise _state_fail(code, f"capability_code {capability_code} en double")
        codes.add(capability_code)
        revision = capability.semantic_revision
        if type(revision) is not int or revision < 1:
            raise _state_fail(code, f"{capability_code} : semantic_revision {revision!r} incohérente")
        if type(capability.label) is not str or not capability.label.strip():
            raise _state_fail(code, f"{capability_code} : libellé vide ou non str")
    return snapshot.capabilities


def _current_claim(snapshot: CompetencyAdaptationSnapshot) -> AdaptationStageClaim:
    """Claim dont stage == current_stage, parmi exactement quatre claims
    discovery -> mastery. Seule la claim courante est lue au-delà de sa
    structure : aucune claim inférieure ni supérieure n'est consultée."""
    code = snapshot.competency_code
    claims = snapshot.claims
    if type(claims) is not tuple or any(type(claim) is not AdaptationStageClaim for claim in claims):
        raise _state_fail(code, "claims : tuple d'AdaptationStageClaim attendu")
    stages = tuple(claim.stage for claim in claims)
    if any(type(stage) is not str for stage in stages) or stages != CLAIM_STAGE_ORDER:
        raise _state_fail(code, f"claims {stages!r} : exactement {CLAIM_STAGE_ORDER} attendues")
    return claims[CLAIM_STAGE_ORDER.index(snapshot.current_stage)]


# --------------------------------------------------------------------------
# Projection (pure)
# --------------------------------------------------------------------------

def _coverage(snapshot: CompetencyAdaptationSnapshot, claim: AdaptationStageClaim) -> tuple:
    """(coverage_mode, capacités visibles) de la claim du stade courant, telle
    que reconstruite par 6-1A ; jamais un autre périmètre."""
    code = snapshot.competency_code
    catalogue = _catalogue(snapshot)
    represented = claim.represented_capability_definition_ids
    only = claim.competency_only_basis
    if type(represented) is not tuple or any(type(i) is not uuid.UUID for i in represented):
        raise _state_fail(code, f"{claim.stage} : represented_capability_definition_ids non tuple d'uuid.UUID")
    if type(only) is not bool:
        raise _state_fail(code, f"{claim.stage} : competency_only_basis non bool")
    if type(claim.status) is not str or claim.status not in (_ESTABLISHED, _NOT_ESTABLISHED):
        raise _state_fail(code, f"{claim.stage} : status {claim.status!r} hors vocabulaire")

    if claim.status == _NOT_ESTABLISHED:
        if represented or only:
            raise _state_fail(code, f"{claim.stage} {_NOT_ESTABLISHED} avec un périmètre positif")
        return COVERAGE_NONE, ()

    if len(set(represented)) != len(represented):
        raise _state_fail(code, f"{claim.stage} : definition_id représenté en double")
    known = {capability.definition_id for capability in catalogue}
    unknown = [str(i) for i in represented if i not in known]
    if unknown:
        raise _state_fail(code, f"{claim.stage} : definition_id {unknown} absents du catalogue"
                                " (aucune résolution par capability_code)")
    visible = tuple(capability for capability in catalogue if capability.definition_id in represented)
    if tuple(capability.definition_id for capability in visible) != represented:
        raise _state_fail(code, f"{claim.stage} : definition_id représentés hors de l'ordre du catalogue")

    if represented and not only:
        mode = COVERAGE_LOCALIZED
    elif only and not represented:
        mode = COVERAGE_COMPETENCY_ONLY
    elif represented and only:
        mode = COVERAGE_MIXED
    else:
        raise _state_fail(code, f"{claim.stage} {_ESTABLISHED} sans aucune base (ni capacité, ni competency_only)")
    return mode, tuple(VisibleCapabilityProjection(
        capability_code=capability.capability_code,
        semantic_revision=capability.semantic_revision,
        label=capability.label,
    ) for capability in visible)


def _present_card(code: str, label: str, snapshot: CompetencyAdaptationSnapshot) -> CompetencyCurrentProgress:
    stage = snapshot.current_stage
    if type(stage) is not str or stage not in VISIBLE_STAGE_LABELS:
        raise _state_fail(code, f"current_stage {stage!r} hors vocabulaire Step 5")
    if stage == NON_ETABLI:
        mode, capabilities = COVERAGE_NONE, ()
    else:
        mode, capabilities = _coverage(snapshot, _current_claim(snapshot))
    return CompetencyCurrentProgress(
        competency_code=code,
        competency_label=label,
        state_present=True,
        stage_code=stage,
        stage_label=VISIBLE_STAGE_LABELS[stage],
        coverage_mode=mode,
        represented_capabilities=capabilities,
    )


def _absent_card(code: str, label: str) -> CompetencyCurrentProgress:
    return CompetencyCurrentProgress(
        competency_code=code,
        competency_label=label,
        state_present=False,
        stage_code=None,
        stage_label=NO_STATE_LABEL,
        coverage_mode=COVERAGE_NONE,
        represented_capabilities=(),
    )


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def project_current_progress(*, state: AdaptationStateSnapshot) -> CurrentProgressProjection:
    """Projection descriptive de l'état Step 5 actuel : douze cartes C1 ->
    C12, stade visible = current_stage, couverture = périmètre de la seule
    claim du stade courant. Fonction PURE : même snapshot => même projection.

    Erreurs : InvalidProgressProjectionArgument, InvalidProgressProjectionState,
    UnsupportedProgressProjectionVersion ; jamais de projection partielle ni
    de carte « sans état » de repli."""
    if type(state) is not AdaptationStateSnapshot:
        raise InvalidProgressProjectionArgument(f"AdaptationStateSnapshot attendu, reçu {type(state).__name__}")
    labels = _competency_labels()
    present = _present_competencies(state)
    return CurrentProgressProjection(
        schema_version=CURRENT_PROGRESS_SCHEMA_VERSION,
        policy_version=CURRENT_PROGRESS_POLICY_VERSION,
        competencies=tuple(
            _present_card(code, labels[code], present[code]) if code in present else _absent_card(code, labels[code])
            for code in COMPETENCY_ORDER),
    )
