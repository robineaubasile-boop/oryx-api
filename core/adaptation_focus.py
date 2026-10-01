"""Étape 6.1B1 : contrats, catalogue sémantique courant et validateur
DÉTERMINISTE du focus de compétence d'une interaction.

Question de 6-1B, et seulement elle : « de quelle compétence parle
réellement la demande actuelle, sur quel périmètre sémantique, et quelles
autres compétences sont strictement nécessaires comme ponts conceptuels ? ».
6-1B1 n'en construit que la fondation déterministe ; il ne lit AUCUN message
utilisateur (aucun texte, aucun historique de conversation, aucun
classificateur, aucun modèle de langage : ce sera 6-1B2) :

    release active T4 + SPEC canonique supportée
        -> load_current_focus_taxonomy -> CurrentFocusTaxonomy
    FocusProposal (NON FIABLE, future sortie de 6-1B2) + CurrentFocusTaxonomy
        -> validate_focus_proposal -> InteractionCompetencyFocus (validé)

Frontière constitutionnelle. 6-1A répond « qu'est-ce que Step 5 sait de
l'utilisateur ? » ; 6-1B répond « de quoi parle la demande ? ». Ce module
n'importe aucun objet de 6-1A et ne connaît aucun utilisateur : aucun
identifiant d'utilisateur, aucun stade, aucun profil, aucune tension, aucun
besoin de validation, aucun compteur d'état. Même taxonomie + même
FocusProposal => même InteractionCompetencyFocus, quel que soit
l'utilisateur. Le routage produit (Education / Coach / Décrypte / Checklist)
est une autre question : il n'est ni lu ni modifié ici.

Invariants :

- Versions explicites : FOCUS_SCHEMA_VERSION décrit le contrat entrée /
  sortie ; FOCUS_POLICY_VERSION décrit les règles de résolution compatibles
  avec UNE identité de taxonomie. Aucune version implicite.

- Registre de policies FAIL-CLOSED : une policy est liée explicitement à un
  couple (version_key, spec_fingerprint) figé, et à la SPEC Python
  canonique qui décrit ce couple. Taxonomie inconnue, ou version connue
  avec un autre fingerprint => UnsupportedFocusPolicy. Jamais de repli par
  proximité, jamais « même capability_code donc probablement même sens ».
  Le fingerprint du registre est une constante figée (même doctrine que
  EXPECTED_V1_FINGERPRINT) : une SPEC modifiée, même sous la même
  version_key, n'hérite jamais silencieusement d'une policy existante.

- SPEC <-> PostgreSQL : la SPEC canonique supportée porte le sens
  (compétences, libellés, questions centrales, capacités, révisions,
  définitions, mapping_guidance) ; PostgreSQL porte l'identité (release_id,
  definition_id) et l'appartenance réelle à la release. Les memberships de
  la release active doivent correspondre EXACTEMENT à la SPEC : mêmes
  capability_code, une seule révision par code, semantic_revision,
  competency_code, label, definition et mapping_guidance identiques (types
  JSON exacts, ordre des listes conservé). Toute divergence, capacité
  manquante ou en trop => InvalidFocusTaxonomy. Rien n'est réparé, rien
  n'est remappé par capability_code.

- Lecture seule : uniquement get_active_release et get_release_capabilities
  (service propriétaire T4-B), sous db.no_autoflush, et la SPEC canonique
  supportée (load_taxonomy_v1). Jamais d'écriture, de verrou, de flush, de
  commit ni de rollback ; aucune migration, aucun modèle Step 6.

- Token sémantique : « <capability_code>@r<semantic_revision> » (C10_B@r1),
  seule forme par laquelle une proposition désigne une capacité (jamais un
  UUID). Résolution STRICTE dans LA taxonomie fournie : casse exacte, aucun
  zéro de tête, aucune révision omise ou absente de la release. Aucun
  repli, aucun parsing permissif.

- Validateur PUR : validate_focus_proposal ne touche ni base, ni horloge,
  ni environnement, ni hasard. Même entrée => même sortie.
  * frontière publique : CurrentFocusTaxonomy est une dataclass publique,
    donc constructible à la main. Avant toute résolution, le catalogue
    fourni est revérifié EXACTEMENT contre la SPEC canonique de la policy que
    désigne son identité (policy.load_spec, Python pur) : schéma, 12
    compétences C1 -> C12 (code, libellé, question centrale), 45 capacités
    dans l'ordre canonique (six champs sémantiques), taxonomy_release_id et
    definition_id uuid.UUID, definition_id uniques. Tout écart =>
    InvalidFocusTaxonomy : un focus n'est jamais lié au fingerprint officiel
    d'un catalogue qui ne le représente pas. Le lien definition_id <->
    PostgreSQL reste garanti par load_current_focus_taxonomy ;
  * resolved => exactement une cible, 0..N supports ;
    neutral / ambiguous / composite => aucune cible, aucun support (rien de
    partiel n'est conservé : 6-1C ne personnalise jamais depuis une
    compétence non résolue proprement) ;
  * scope localized => au moins un token, tous distincts, de la compétence,
    convertis en definition_id exacts de la release ; competency_only =>
    aucun token, aucun definition_id inventé ; tout autre scope (dont
    whole_competency, propre aux tensions / revalidations Step 5) refusé ;
  * la compétence cible n'apparaît jamais en support ; une compétence de
    support apparaît au plus une fois (ses capacités sont regroupées dans UN
    seul focus).
  Toute proposition invalide => InvalidFocusProposal ; aucune erreur n'est
  convertie en neutral (le repli neutre appartiendra à l'orchestrateur).

- Aucune résolution probabiliste : ni note, ni pondération, ni classement,
  ni degré de certitude. 6-1B produit une résolution symbolique.

Ordres canoniques : compétences C1 -> C12 ; capacités dans l'ordre naturel
de la SPEC / T4-B (C1_A ... C9_D, C10_A ... C12_D), jamais un tri lexical ;
definition_ids d'un focus dans l'ordre de la taxonomie ; supports C1 -> C12,
quel que soit l'ordre de la proposition.
"""
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NamedTuple

from core.pedagogy.taxonomy_v1 import (
    CAPABILITY_CODES,
    COMPETENCY_CODES,
    TaxonomySpecError,
    load_taxonomy_v1,
)
from core.taxonomy_service import TaxonomyServiceError, get_active_release, get_release_capabilities

FOCUS_SCHEMA_VERSION = "interaction-focus-v1"
FOCUS_POLICY_VERSION = "focus-policy-1"

# FocusProposal.resolution_status / InteractionCompetencyFocus.resolution_status
RESOLVED = "resolved"
NEUTRAL = "neutral"
AMBIGUOUS = "ambiguous"
COMPOSITE = "composite"
RESOLUTION_STATUSES = (RESOLVED, NEUTRAL, AMBIGUOUS, COMPOSITE)

# ProposedCompetencyFocus.scope_mode / CompetencyFocus.scope_mode
# (whole_competency appartient aux tensions / revalidations Step 5 : refusé).
LOCALIZED = "localized"
COMPETENCY_ONLY = "competency_only"
SCOPE_MODES = (LOCALIZED, COMPETENCY_ONLY)

# Forme bien formée d'un token (casse exacte, aucun zéro de tête) ; un token
# bien formé doit en outre exister EXACTEMENT dans la taxonomie.
_TOKEN = re.compile(r"(C[1-9][0-9]*_[A-Z])@r([1-9][0-9]*)")


class FocusError(Exception):
    """Erreur métier de 6-1B : jamais un focus partiel, jamais un repli
    neutre implicite."""


class InvalidFocusArgument(FocusError):
    """Argument d'un type inattendu (proposition, taxonomie)."""


class FocusTaxonomyUnavailable(FocusError):
    """Aucune release de taxonomie active."""


class UnsupportedFocusPolicy(FocusError):
    """Release active valide, mais couple (version_key, spec_fingerprint)
    absent du registre des policies."""


class InvalidFocusTaxonomy(FocusError):
    """Policy censée supportée, mais contenu PostgreSQL <-> SPEC canonique
    incohérent (ou catalogue fourni incohérent) : jamais réparé."""


class InvalidFocusProposal(FocusError):
    """Proposition structurellement ou sémantiquement invalide."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

def _frozen_json(value, path: str):
    """Copie profonde immuable d'une valeur JSON (objet -> mappingproxy,
    tableau -> tuple). Tout autre type => InvalidFocusTaxonomy."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Mapping):
        copy = {}
        for key, item in value.items():
            if type(key) is not str:
                raise InvalidFocusTaxonomy(f"{path} : clé {key!r} non str")
            copy[key] = _frozen_json(item, f"{path}.{key}")
        return MappingProxyType(copy)
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_json(item, f"{path}[{i}]") for i, item in enumerate(value))
    raise InvalidFocusTaxonomy(f"{path} : type {type(value).__name__} non JSON")


@dataclass(frozen=True, kw_only=True)
class FocusCompetencyDefinition:
    """Une compétence de la SPEC canonique (C1..C12)."""
    competency_code: str
    label: str
    central_question: str


@dataclass(frozen=True, kw_only=True)
class FocusCapabilityDefinition:
    """Une capacité de la release active. definition_id (PostgreSQL) =
    identité sémantique ; le sens vient de la SPEC canonique. mapping_guidance
    est copié en profondeur et figé à la construction : muter la source
    ensuite ne modifie jamais cette définition."""
    definition_id: uuid.UUID

    capability_code: str
    semantic_revision: int
    competency_code: str

    label: str
    definition: str
    mapping_guidance: Mapping

    def __post_init__(self):
        object.__setattr__(self, "mapping_guidance", _frozen_json(self.mapping_guidance, "mapping_guidance"))


@dataclass(frozen=True, kw_only=True)
class CurrentFocusTaxonomy:
    """Catalogue sémantique courant de 6-1B : release active, identité de
    taxonomie, policy, 12 compétences (C1 -> C12) et capacités (ordre
    naturel)."""
    taxonomy_release_id: uuid.UUID
    taxonomy_schema_version: int
    taxonomy_version_key: str
    taxonomy_spec_fingerprint: str
    focus_policy_version: str

    competencies: tuple[FocusCompetencyDefinition, ...]
    capabilities: tuple[FocusCapabilityDefinition, ...]

    def __post_init__(self):
        object.__setattr__(self, "competencies", tuple(self.competencies))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True, kw_only=True)
class ProposedCompetencyFocus:
    """Focus proposé (NON FIABLE) : capacités désignées par token sémantique."""
    competency_code: str
    scope_mode: str
    capability_tokens: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class FocusProposal:
    """Proposition NON FIABLE (future sortie de 6-1B2) : inutilisable avant
    validate_focus_proposal."""
    schema_version: str
    policy_version: str

    resolution_status: str

    target: ProposedCompetencyFocus | None
    supporting: tuple[ProposedCompetencyFocus, ...]


@dataclass(frozen=True, kw_only=True)
class CompetencyFocus:
    """Focus validé : definition_id exacts de la release, ordre taxonomique."""
    competency_code: str
    scope_mode: str
    capability_definition_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, kw_only=True)
class InteractionCompetencyFocus:
    """Focus validé, lié à UNE release (taxonomy_release_id) et à UNE
    identité sémantique (taxonomy_spec_fingerprint)."""
    schema_version: str
    policy_version: str

    taxonomy_release_id: uuid.UUID
    taxonomy_spec_fingerprint: str

    resolution_status: str

    target: CompetencyFocus | None
    supporting: tuple[CompetencyFocus, ...]


# --------------------------------------------------------------------------
# Registre des policies (interne, fail-closed)
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class _FocusPolicy:
    """Policy de focus liée à UNE identité de taxonomie et à la SPEC
    canonique qui la décrit ((spec, fingerprint) validés)."""
    policy_version: str
    taxonomy_version_key: str
    taxonomy_spec_fingerprint: str
    load_spec: Callable[[], tuple[dict, str]]
    competency_order: tuple[str, ...]
    capability_order: tuple[str, ...]


# Identité de la taxonomie V1 FIGÉE ici (tests/test_adaptation_focus.py
# garantit l'égalité avec VERSION_KEY / EXPECTED_V1_FINGERPRINT) : une autre
# SPEC, même sous la même version_key, n'est jamais supportée implicitement.
_FOCUS_POLICIES = MappingProxyType({
    ("oryx-v1", "50b690b9e5ddd6812b625b02037b6f29b8b6cd553c3cf7d4069a634f7df5e2f7"): _FocusPolicy(
        policy_version=FOCUS_POLICY_VERSION,
        taxonomy_version_key="oryx-v1",
        taxonomy_spec_fingerprint="50b690b9e5ddd6812b625b02037b6f29b8b6cd553c3cf7d4069a634f7df5e2f7",
        load_spec=load_taxonomy_v1,
        competency_order=COMPETENCY_CODES,
        capability_order=CAPABILITY_CODES,
    ),
})
_SUPPORTED_POLICY_VERSIONS = frozenset(policy.policy_version for policy in _FOCUS_POLICIES.values())


def _resolve_policy(version_key, spec_fingerprint) -> _FocusPolicy:
    """Correspondance EXACTE (str, str) ; aucune proximité."""
    if type(version_key) is not str or type(spec_fingerprint) is not str:
        raise UnsupportedFocusPolicy(f"identité de taxonomie ({version_key!r}, {spec_fingerprint!r}) non textuelle")
    policy = _FOCUS_POLICIES.get((version_key, spec_fingerprint))
    if policy is None:
        raise UnsupportedFocusPolicy(
            f"taxonomie {version_key!r} / {spec_fingerprint} : aucune policy de focus (aucun repli)")
    return policy


# --------------------------------------------------------------------------
# Catalogue : acquisition (lectures T4-B) puis projection pure SPEC <-> DB
# --------------------------------------------------------------------------

class _ReleaseRecord(NamedTuple):
    id: uuid.UUID
    version_key: str
    spec_fingerprint: str


class _CapabilityRow(NamedTuple):
    """(membership, définition) lus, copiés ; mapping_guidance sous forme
    canonique (jamais une référence vers l'objet ORM)."""
    membership_id: uuid.UUID
    taxonomy_release_id: uuid.UUID
    membership_definition_id: uuid.UUID
    definition_id: uuid.UUID
    capability_code: object
    semantic_revision: object
    competency_code: object
    label: object
    definition: object
    mapping_guidance: str


_COMPARED_FIELDS = ("capability_code", "semantic_revision", "competency_code", "label", "definition",
                    "mapping_guidance")


def _canonical(value, path: str) -> str:
    """Forme de comparaison stricte : distingue les types JSON (1, 1.0,
    True, "1"), ignore l'ordre des clés, jamais celui des listes."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidFocusTaxonomy(f"{path} : valeur non JSON ({exc})") from exc


def _canonical_spec(policy: _FocusPolicy) -> dict:
    """SPEC canonique de la policy (Python pur, déterministe), validée et
    de la MÊME identité que la policy (version_key, fingerprint recalculé).
    Partagée par le chargement et par le validateur."""
    try:
        spec, fingerprint = policy.load_spec()
    except TaxonomySpecError as exc:
        raise InvalidFocusTaxonomy(f"SPEC canonique de {policy.policy_version} invalide : {exc}") from exc
    if (spec["version_key"], fingerprint) != (policy.taxonomy_version_key, policy.taxonomy_spec_fingerprint):
        raise InvalidFocusTaxonomy(
            f"SPEC canonique {spec['version_key']!r} / {fingerprint} différente de l'identité de"
            f" {policy.policy_version} {policy.taxonomy_version_key!r} / {policy.taxonomy_spec_fingerprint}")
    return spec


def _policy_spec(policy: _FocusPolicy, release: _ReleaseRecord) -> dict:
    """SPEC canonique de la policy, de la MÊME identité que la release."""
    spec = _canonical_spec(policy)
    if (policy.taxonomy_version_key, policy.taxonomy_spec_fingerprint) != (release.version_key,
                                                                           release.spec_fingerprint):
        raise InvalidFocusTaxonomy(
            f"release {release.version_key!r} / {release.spec_fingerprint} hors de l'identité de"
            f" {policy.policy_version}")
    return spec


def _focus_taxonomy(release: _ReleaseRecord, policy: _FocusPolicy, spec: dict, rows) -> CurrentFocusTaxonomy:
    """Projection PURE : release + SPEC + memberships lus -> catalogue.
    Exactement une ligne par capacité de la SPEC, champs sémantiques
    identiques ; definition_id de PostgreSQL, sens de la SPEC."""
    label = f"release {release.id} ({release.version_key})"
    expected = {capability["capability_code"]: capability for capability in spec["capabilities"]}
    persisted = {}
    for row in rows:
        if row.taxonomy_release_id != release.id:
            raise InvalidFocusTaxonomy(f"{label} : membership {row.membership_id} d'une autre release")
        if row.membership_definition_id != row.definition_id:
            raise InvalidFocusTaxonomy(f"{label} : membership {row.membership_id} incohérent")
        persisted.setdefault(row.capability_code, []).append(row)
    if len({row.definition_id for row in rows}) != len(rows):
        raise InvalidFocusTaxonomy(f"{label} : définition en double")

    missing = [code for code in policy.capability_order if code in expected and code not in persisted]
    extra = [str(code) for code in persisted if code not in expected]
    doubled = [code for code in policy.capability_order if len(persisted.get(code, ())) > 1]
    if missing or extra or doubled or len(rows) != len(expected) or len(expected) != len(policy.capability_order):
        raise InvalidFocusTaxonomy(
            f"{label} : {len(rows)} capacité(s) pour {len(expected)} attendues par la SPEC"
            f" (manquantes {missing}, hors SPEC {extra}, plusieurs révisions {doubled})")

    capabilities = []
    for code in policy.capability_order:
        (row,) = persisted[code]
        canonical = expected[code]
        differing = [name for name in _COMPARED_FIELDS
                     if (row.mapping_guidance if name == "mapping_guidance"
                         else _canonical(getattr(row, name), f"{code}.{name}"))
                     != _canonical(canonical[name], f"SPEC {code}.{name}")]
        if differing:
            raise InvalidFocusTaxonomy(
                f"{label} : {code} ({row.definition_id}) diffère de la SPEC sur {', '.join(differing)}"
                " (jamais réparé, aucun remapping par capability_code)")
        capabilities.append(FocusCapabilityDefinition(
            definition_id=row.definition_id,
            capability_code=canonical["capability_code"],
            semantic_revision=canonical["semantic_revision"],
            competency_code=canonical["competency_code"],
            label=canonical["label"],
            definition=canonical["definition"],
            mapping_guidance=canonical["mapping_guidance"],
        ))

    competencies = {competency["competency_code"]: competency for competency in spec["competencies"]}
    if set(competencies) != set(policy.competency_order) or len(spec["competencies"]) != len(competencies):
        raise InvalidFocusTaxonomy(f"SPEC {release.version_key} : compétences hors de {policy.competency_order}")
    return CurrentFocusTaxonomy(
        taxonomy_release_id=release.id,
        taxonomy_schema_version=spec["taxonomy_schema_version"],
        taxonomy_version_key=release.version_key,
        taxonomy_spec_fingerprint=release.spec_fingerprint,
        focus_policy_version=policy.policy_version,
        competencies=tuple(FocusCompetencyDefinition(
            competency_code=code,
            label=competencies[code]["label"],
            central_question=competencies[code]["central_question"],
        ) for code in policy.competency_order),
        capabilities=tuple(capabilities),
    )


def _acquire_rows(db, release_id: uuid.UUID) -> tuple:
    try:
        pairs = get_release_capabilities(db, release_id=release_id)
    except TaxonomyServiceError as exc:
        raise InvalidFocusTaxonomy(f"release {release_id} : lecture impossible ({exc!r})") from exc
    return tuple(
        _CapabilityRow(m.id, m.taxonomy_release_id, m.capability_definition_id, d.id, d.capability_code,
                       d.semantic_revision, d.competency_code, d.label, d.definition,
                       _canonical(d.mapping_guidance, f"{d.capability_code}.mapping_guidance"))
        for m, d in pairs)


# --------------------------------------------------------------------------
# Validation de la proposition (pure)
# --------------------------------------------------------------------------

class _Index(NamedTuple):
    competencies: Mapping
    by_token: Mapping
    position: Mapping


def _token(capability: FocusCapabilityDefinition) -> str:
    return f"{capability.capability_code}@r{capability.semantic_revision}"


def _thawed(value):
    """Valeur figée (mappingproxy / tuple) -> JSON (dict / list), pour la
    comparaison canonique avec la SPEC."""
    if isinstance(value, Mapping):
        return {key: _thawed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thawed(item) for item in value]
    return value


def _index(taxonomy: CurrentFocusTaxonomy) -> _Index:
    """Défense PURE de la frontière publique : le catalogue fourni doit être
    EXACTEMENT celui que son identité (version_key, spec_fingerprint)
    désigne. Policy résolue dans le registre, SPEC canonique rechargée
    (policy.load_spec, Python pur), puis comparaison complète : schéma,
    12 compétences (C1 -> C12, une fois chacune, libellé et question
    centrale exacts), 45 capacités (ordre canonique de la policy, aucune
    absente / en trop / dupliquée, six champs sémantiques exacts),
    definition_id uuid.UUID uniques. Aucune base, aucune reconstruction,
    aucun remapping par capability_code. Toute divergence =>
    InvalidFocusTaxonomy (identité non enregistrée => UnsupportedFocusPolicy).
    Ne prouve pas que les definition_id viennent de PostgreSQL (garanti par
    load_current_focus_taxonomy)."""
    if type(taxonomy.taxonomy_release_id) is not uuid.UUID:
        raise InvalidFocusTaxonomy(f"taxonomy_release_id {taxonomy.taxonomy_release_id!r} non uuid.UUID")
    policy = _resolve_policy(taxonomy.taxonomy_version_key, taxonomy.taxonomy_spec_fingerprint)
    if type(taxonomy.focus_policy_version) is not str or taxonomy.focus_policy_version != policy.policy_version:
        raise InvalidFocusTaxonomy(
            f"catalogue {taxonomy.taxonomy_version_key} annoncé sous {taxonomy.focus_policy_version!r},"
            f" policy enregistrée {policy.policy_version!r}")
    spec = _canonical_spec(policy)
    label = f"catalogue {taxonomy.taxonomy_version_key}"
    if _canonical(taxonomy.taxonomy_schema_version, "taxonomy_schema_version") != _canonical(
            spec["taxonomy_schema_version"], "SPEC taxonomy_schema_version"):
        raise InvalidFocusTaxonomy(
            f"{label} : taxonomy_schema_version {taxonomy.taxonomy_schema_version!r},"
            f" SPEC {spec['taxonomy_schema_version']!r}")

    spec_competencies = {c["competency_code"]: c for c in spec["competencies"]}
    if len(taxonomy.competencies) != len(policy.competency_order):
        raise InvalidFocusTaxonomy(
            f"{label} : {len(taxonomy.competencies)} compétence(s), {len(policy.competency_order)} attendues")
    competencies = {}
    for position, (code, competency) in enumerate(zip(policy.competency_order, taxonomy.competencies)):
        if type(competency) is not FocusCompetencyDefinition:
            raise InvalidFocusTaxonomy(f"{label} : compétence {position} de type {type(competency).__name__}")
        canonical = spec_competencies[code]
        differing = [name for name in ("competency_code", "label", "central_question")
                     if type(getattr(competency, name)) is not str or getattr(competency, name) != canonical[name]]
        if differing:
            raise InvalidFocusTaxonomy(
                f"{label} : compétence {position} ({competency.competency_code!r}) diffère de la SPEC {code}"
                f" sur {', '.join(differing)} (ordre C1 -> C12, une fois chacune)")
        competencies[code] = position

    spec_capabilities = {c["capability_code"]: c for c in spec["capabilities"]}
    if len(taxonomy.capabilities) != len(policy.capability_order):
        raise InvalidFocusTaxonomy(
            f"{label} : {len(taxonomy.capabilities)} capacité(s), {len(policy.capability_order)} attendues")
    by_token, position = {}, {}
    for index, (code, capability) in enumerate(zip(policy.capability_order, taxonomy.capabilities)):
        if type(capability) is not FocusCapabilityDefinition:
            raise InvalidFocusTaxonomy(f"{label} : capacité {index} de type {type(capability).__name__}")
        canonical = spec_capabilities[code]
        differing = [name for name in _COMPARED_FIELDS
                     if _canonical(_thawed(getattr(capability, name)), f"{code}.{name}")
                     != _canonical(canonical[name], f"SPEC {code}.{name}")]
        if (type(capability.capability_code) is not str or type(capability.competency_code) is not str
                or type(capability.label) is not str or type(capability.definition) is not str):
            differing.append("type")
        if differing:
            raise InvalidFocusTaxonomy(
                f"{label} : capacité {index} ({capability.capability_code!r}) diffère de la SPEC {code}"
                f" sur {', '.join(differing)} (ordre canonique, aucun remapping par capability_code)")
        if type(capability.definition_id) is not uuid.UUID:
            raise InvalidFocusTaxonomy(f"{label} : {code} definition_id {capability.definition_id!r} non uuid.UUID")
        if capability.definition_id in position:
            raise InvalidFocusTaxonomy(f"{label} : definition_id {capability.definition_id} en double")
        by_token[_token(capability)] = capability
        position[capability.definition_id] = len(position)
    return _Index(MappingProxyType(competencies), MappingProxyType(by_token), MappingProxyType(position))


def _choice(value, name: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise InvalidFocusProposal(f"{name} : valeur {value!r} hors vocabulaire {list(allowed)}")
    return value


def _resolve_token(token, competency_code: str, index: _Index, path: str) -> uuid.UUID:
    """Token strict -> definition_id exact de CETTE taxonomie ; aucun repli."""
    if type(token) is not str or _TOKEN.fullmatch(token) is None:
        raise InvalidFocusProposal(f"{path} : token {token!r} hors format <capability_code>@r<semantic_revision>")
    capability = index.by_token.get(token)
    if capability is None:
        raise InvalidFocusProposal(
            f"{path} : {token} absent de la taxonomie (aucun remapping par capability_code)")
    if capability.competency_code != competency_code:
        raise InvalidFocusProposal(f"{path} : {token} appartient à {capability.competency_code}, pas à {competency_code}")
    return capability.definition_id


def _competency_focus(proposed, index: _Index, path: str) -> CompetencyFocus:
    if type(proposed) is not ProposedCompetencyFocus:
        raise InvalidFocusProposal(f"{path} : ProposedCompetencyFocus attendu, reçu {type(proposed).__name__}")
    competency_code = _choice(proposed.competency_code, f"{path}.competency_code", tuple(index.competencies))
    scope_mode = _choice(proposed.scope_mode, f"{path}.scope_mode", SCOPE_MODES)
    tokens = proposed.capability_tokens
    if type(tokens) is not tuple:
        raise InvalidFocusProposal(f"{path}.capability_tokens : tuple attendu")
    if scope_mode == COMPETENCY_ONLY:
        if tokens:
            raise InvalidFocusProposal(f"{path} : {COMPETENCY_ONLY} avec {len(tokens)} token(s)")
        return CompetencyFocus(competency_code=competency_code, scope_mode=scope_mode, capability_definition_ids=())
    if not tokens:
        raise InvalidFocusProposal(f"{path} : {LOCALIZED} sans capacité")
    ids = [_resolve_token(token, competency_code, index, f"{path}.capability_tokens[{i}]")
           for i, token in enumerate(tokens)]
    if len(set(ids)) != len(ids):
        raise InvalidFocusProposal(f"{path} : token en double")
    return CompetencyFocus(competency_code=competency_code, scope_mode=scope_mode,
                           capability_definition_ids=tuple(sorted(ids, key=index.position.__getitem__)))


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def load_current_focus_taxonomy(db) -> CurrentFocusTaxonomy:
    """Catalogue sémantique courant : release active (T4-B) -> policy du
    registre (fail-closed) -> SPEC canonique supportée -> memberships de la
    release -> validation stricte SPEC <-> PostgreSQL.

    Aucune release active => FocusTaxonomyUnavailable ; identité non
    enregistrée => UnsupportedFocusPolicy ; contenu divergent (ou plusieurs
    releases actives) => InvalidFocusTaxonomy. Lecture seule : rien n'est
    créé, activé ni réparé ; aucune transaction possédée."""
    with db.no_autoflush:
        try:
            release = get_active_release(db)
        except TaxonomyServiceError as exc:
            raise InvalidFocusTaxonomy(f"release active illisible ({exc!r})") from exc
        if release is None:
            raise FocusTaxonomyUnavailable("aucune release de taxonomie active")
        record = _ReleaseRecord(release.id, release.version_key, release.spec_fingerprint)
        policy = _resolve_policy(record.version_key, record.spec_fingerprint)
        spec = _policy_spec(policy, record)
        rows = _acquire_rows(db, record.id)
    return _focus_taxonomy(record, policy, spec, rows)


def validate_focus_proposal(
    proposal: FocusProposal,
    taxonomy: CurrentFocusTaxonomy,
) -> InteractionCompetencyFocus:
    """FocusProposal NON FIABLE -> InteractionCompetencyFocus validé, lié à
    la release et au fingerprint du catalogue. Fonction PURE (aucune base,
    horloge, environnement ni hasard) : même entrée => même sortie.

    Versions : schema_version == FOCUS_SCHEMA_VERSION, policy_version ==
    taxonomy.focus_policy_version (et enregistrée). resolved => exactement
    une cible, supports 0..N (compétences distinctes, hors cible, ordonnés
    C1 -> C12) ; neutral / ambiguous / composite => ni cible ni support.
    Toute violation => InvalidFocusProposal (jamais un repli neutre)."""
    if type(proposal) is not FocusProposal:
        raise InvalidFocusArgument(f"FocusProposal attendue, reçu {type(proposal).__name__}")
    if type(taxonomy) is not CurrentFocusTaxonomy:
        raise InvalidFocusArgument(f"CurrentFocusTaxonomy attendue, reçu {type(taxonomy).__name__}")
    index = _index(taxonomy)

    _choice(proposal.schema_version, "schema_version", (FOCUS_SCHEMA_VERSION,))
    policy_version = _choice(proposal.policy_version, "policy_version", tuple(sorted(_SUPPORTED_POLICY_VERSIONS)))
    if policy_version != taxonomy.focus_policy_version:
        raise InvalidFocusProposal(
            f"policy_version {policy_version!r} différente de celle du catalogue {taxonomy.focus_policy_version!r}")
    status = _choice(proposal.resolution_status, "resolution_status", RESOLUTION_STATUSES)
    if type(proposal.supporting) is not tuple:
        raise InvalidFocusProposal("supporting : tuple attendu")

    target, supporting = None, ()
    if status != RESOLVED:
        if proposal.target is not None or proposal.supporting:
            raise InvalidFocusProposal(f"{status} : ni cible ni support attendus")
    else:
        if proposal.target is None:
            raise InvalidFocusProposal(f"{RESOLVED} sans cible")
        target = _competency_focus(proposal.target, index, "target")
        supports = {}
        for i, proposed in enumerate(proposal.supporting):
            focus = _competency_focus(proposed, index, f"supporting[{i}]")
            if focus.competency_code == target.competency_code:
                raise InvalidFocusProposal(f"supporting[{i}] : {focus.competency_code} est déjà la cible")
            if focus.competency_code in supports:
                raise InvalidFocusProposal(f"supporting[{i}] : {focus.competency_code} en double"
                                           " (capacités d'une compétence regroupées dans UN focus)")
            supports[focus.competency_code] = focus
        supporting = tuple(supports[code] for code in sorted(supports, key=index.competencies.__getitem__))

    return InteractionCompetencyFocus(
        schema_version=FOCUS_SCHEMA_VERSION,
        policy_version=policy_version,
        taxonomy_release_id=taxonomy.taxonomy_release_id,
        taxonomy_spec_fingerprint=taxonomy.taxonomy_spec_fingerprint,
        resolution_status=status,
        target=target,
        supporting=supporting,
    )
