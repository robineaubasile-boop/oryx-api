"""Policies versionnées de la base positive (T6-C1, positive_basis-1) et leur
résolution contre la taxonomie COURANTE du contexte d'inférence.

Frontière. T6-C1 répond à UNE question : « en regardant uniquement les
preuves positives du dossier longitudinal courant, quelles prétentions
Discovery / Comprehension / Application / Mastery possèdent une base positive
suffisamment établie ? ». Ce module ne porte que les RÈGLES (registry C1-C12)
et leur résolution ; le moteur pur vit dans core/inference_positive_basis.py.
Aucune base, aucune Session, aucun modèle, aucun réseau, aucune horloge.

Pourquoi aucun score. La représentativité d'un périmètre démontré n'est
jamais un nombre de capacités, un ratio, une moyenne ni un poids : chaque
règle est un SemanticPattern NOMMÉ, booléen et auditable, qui exprime une
RELATION sémantique entre dimensions de la compétence :

    satisfait  <=>  le périmètre démontré touche une capacité du noyau
                    (core) ET, si la relation en exige une, une capacité
                    reliée (related).

« C10_B reliée à C10_A | C10_C | C10_D » n'est pas « 2 capacités sur 4 » :
C10_A + C10_C (deux capacités) ne satisfont pas ce pattern, C10_B seule
(une capacité) satisfait celui de Comprehension. Les narrow_scope_patterns
décrivent des périmètres connus comme trop locaux (jamais une pénalité :
seulement l'explication d'une non-généralisation) ; generalization_limits
nomme la limite doctrinale correspondante ; competency_only_allowed dit si
une preuve competency_only (légitime : R6) peut soutenir la claim.

Identité sémantique. capability_code DÉCLARE la policy (lisibilité, parité
avec core/pedagogy/taxonomy_v1.py, source canonique des 45 capacités V1) ;
le raisonnement sur le contexte courant se fait par capability_definition_id.
Le resolver confronte CurrentTaxonomyContext (T6-C0) aux attentes de
positive_basis-1 : chaque capacité attendue présente, de la bonne compétence
et en semantic_revision 1 (UNIQUE(capability_code, semantic_revision) en base
: le couple (code, 1) désigne exactement LA définition V1, donc une release
future qui réutilise les mêmes definition_id V1 reste compatible). Même
capability_code sous une autre révision = autre sens : fail closed, jamais un
remapping par code. Version de policy inconnue, contexte hors V1
(input_schema_version != 2, model_id ou prompt_spec_version renseigné) :
fail closed, aucune policy par défaut.
"""
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from core.inference_service import CLAIM_STAGES, CurrentTaxonomyCapability, InferenceContext
from core.pedagogy.taxonomy_v1 import CAPABILITY_CODES, COMPETENCY_CODES, V1_SEMANTIC_REVISION

POSITIVE_BASIS_V1 = "positive_basis-1"
# Format d'entrée exigé par positive_basis-1 : input_fingerprint V2 (T6-B.2).
# Volontairement recopié (et non importé) : un futur format T6-B ne doit
# jamais être accepté implicitement par cette policy.
SUPPORTED_INPUT_SCHEMA_VERSION = 2
SUPPORTED_SEMANTIC_REVISION = V1_SEMANTIC_REVISION

DISCOVERY, COMPREHENSION, APPLICATION, MASTERY = CLAIM_STAGES
# Claims évaluées directement sur des démonstrations (Mastery est
# exclusivement longitudinale : jamais un local_stage).
DIRECT_STAGES = (DISCOVERY, COMPREHENSION, APPLICATION)


class PositiveBasisError(Exception):
    """Erreur métier T6-C1 : le moteur échoue fermé, jamais un repli."""


class InvalidPositiveBasisPolicy(PositiveBasisError):
    """Registry de policy structurellement invalide (code inconnu, pattern
    vide, dimension croisée, compétence manquante)."""


class UnsupportedPositiveBasisPolicy(PositiveBasisError):
    """positive_basis_version inconnue : aucune policy par défaut."""


class UnsupportedInferenceContext(PositiveBasisError):
    """Contexte hors du périmètre V1 : type, input_schema_version != 2,
    model_id ou prompt_spec_version renseigné."""


class UnsupportedTaxonomySemantics(PositiveBasisError):
    """Taxonomie courante incompatible avec la policy : capacité attendue
    absente, capacité inattendue, doublon ou semantic_revision non supportée
    (jamais une correspondance par capability_code)."""


class InconsistentInferenceContext(PositiveBasisError):
    """Contexte incohérent : compétence, release ou dossier divergents,
    observation d'une autre compétence ou localisée hors de la taxonomie
    résolue, vocabulaire inconnu."""


# --------------------------------------------------------------------------
# Structures de policy (immuables, déclarées par capability_code)
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class SemanticPattern:
    """Règle qualitative booléenne nommée : core (au moins une de ces
    capacités) reliée, si related est non vide, à au moins une capacité de
    related. Jamais une note, un compte ni un seuil. core et related sont
    disjoints : la relation lie deux dimensions sémantiques distinctes."""
    name: str
    core_capability_codes: tuple
    related_capability_codes: tuple = ()


@dataclass(frozen=True, kw_only=True)
class StageRepresentativityPolicy:
    """Représentativité d'UNE claim directe (discovery, comprehension ou
    application) au niveau de la compétence."""
    stage: str
    representative_patterns: tuple
    narrow_scope_patterns: tuple
    generalization_limits: tuple
    competency_only_allowed: bool


@dataclass(frozen=True, kw_only=True)
class MasteryPolicy:
    """Spécificités qualitatives de Mastery pour une compétence.
    revision_capability_codes : capacités dont une démonstration
    d'Application documente directement une révision rationnelle (C11_D
    pour C11) et peut donc contribuer à robustness_revision."""
    revision_capability_codes: tuple = ()


@dataclass(frozen=True, kw_only=True)
class CompetencyPositiveBasisPolicy:
    competency_code: str
    capability_codes: tuple
    discovery_policy: StageRepresentativityPolicy
    comprehension_policy: StageRepresentativityPolicy
    application_policy: StageRepresentativityPolicy
    mastery_policy: MasteryPolicy

    def stage_policy(self, stage: str) -> StageRepresentativityPolicy:
        return {DISCOVERY: self.discovery_policy, COMPREHENSION: self.comprehension_policy,
                APPLICATION: self.application_policy}[stage]


@dataclass(frozen=True, kw_only=True)
class PositiveBasisPolicy:
    version: str
    competencies: tuple


# --------------------------------------------------------------------------
# Structures résolues (par capability_definition_id)
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ResolvedPattern:
    """SemanticPattern traduit en capability_definition_id de la release
    courante (ensembles internes, jamais un ordre de sortie)."""
    name: str
    core_definition_ids: frozenset
    related_definition_ids: frozenset

    def satisfied_by(self, definition_ids) -> bool:
        """Relation sémantique booléenne (voir la docstring du module)."""
        touched = frozenset(definition_ids)
        if self.core_definition_ids.isdisjoint(touched):
            return False
        return not self.related_definition_ids or not self.related_definition_ids.isdisjoint(touched)

    def touched_by(self, definition_ids) -> bool:
        return not (self.core_definition_ids | self.related_definition_ids).isdisjoint(frozenset(definition_ids))


@dataclass(frozen=True, kw_only=True)
class ResolvedStagePolicy:
    stage: str
    representative_patterns: tuple
    narrow_scope_patterns: tuple
    generalization_limits: tuple
    competency_only_allowed: bool


@dataclass(frozen=True, kw_only=True)
class ResolvedPositiveBasisPolicy:
    """Policy d'UNE compétence, confrontée à la taxonomie courante.
    capabilities / definition_ids : ordre pédagogique naturel (C10_A <
    C10_B ...), seul ordre de sortie des capacités."""
    policy_version: str
    competency_code: str
    release_id: uuid.UUID
    capabilities: tuple
    definition_ids: tuple
    stage_policies: Mapping
    revision_definition_ids: frozenset

    def ordered(self, definition_ids) -> tuple:
        """Sous-ensemble dans l'ordre naturel (jamais l'ordre d'un set)."""
        wanted = frozenset(definition_ids)
        return tuple(d for d in self.definition_ids if d in wanted)


# --------------------------------------------------------------------------
# Registry positive_basis-1 (spécification T6-C1 V1)
# --------------------------------------------------------------------------

def _pattern(name, core, related=()) -> SemanticPattern:
    return SemanticPattern(name=name, core_capability_codes=tuple(core), related_capability_codes=tuple(related))


def _stage(stage, representative, narrow=(), limits=(), *, competency_only_allowed=True):
    return StageRepresentativityPolicy(stage=stage, representative_patterns=tuple(representative),
                                       narrow_scope_patterns=tuple(narrow), generalization_limits=tuple(limits),
                                       competency_only_allowed=competency_only_allowed)


def _competency(code, *, comprehension, application, revision=()) -> CompetencyPositiveBasisPolicy:
    """Discovery (règle commune) : une vraie reconnaissance / discrimination
    sur n'importe quelle capacité noyau de la compétence ; jamais relationnel
    (aucune accumulation de démonstrations ne fabrique Discovery)."""
    capabilities = tuple(c for c in CAPABILITY_CODES if c.startswith(f"{code}_"))
    discovery = _stage(DISCOVERY, [_pattern(f"{code.lower()}_core_capability_recognition", capabilities)])
    return CompetencyPositiveBasisPolicy(
        competency_code=code, capability_codes=capabilities, discovery_policy=discovery,
        comprehension_policy=_stage(COMPREHENSION, *comprehension),
        application_policy=_stage(APPLICATION, *application),
        mastery_policy=MasteryPolicy(revision_capability_codes=tuple(revision)))


_C1_NARROW = [_pattern("c1_holding_frame_only", ["C1_B"])]
_C1_LIMITS = ["c1_asset_wrapper_intermediary_alone_does_not_represent_foundations"]
_C2_NARROW = [_pattern("c2_horizon_only", ["C2_B"]), _pattern("c2_tolerance_vs_capacity_only", ["C2_C"]),
              _pattern("c2_risk_nature_only", ["C2_A"])]
_C2_LIMITS = ["c2_isolated_horizon_or_tolerance_does_not_represent_risk_reasoning"]
_C3_NARROW = [_pattern("c3_concentration_only", ["C3_A"]), _pattern("c3_allocation_mechanics_only", ["C3_C"])]
_C3_LIMITS = ["c3_concentration_or_allocation_mechanics_alone_remain_narrower"]
_C4_NARROW = [_pattern("c4_value_proposition_only", ["C4_A"]), _pattern("c4_revenue_engine_only", ["C4_B"]),
              _pattern("c4_dependencies_only", ["C4_C"]), _pattern("c4_moat_only", ["C4_D"])]
_C4_LIMITS = ["c4_isolated_moat_dependency_or_product_description_does_not_represent_business"]
_C5_LIMITS = ["c5_isolated_rate_normalisation_or_declared_quality_does_not_represent_growth"]
_C6_LIMITS = ["c6_margin_statement_leverage_or_normalisation_alone_does_not_generalize"]
_C7_NARROW = [_pattern("c7_working_capital_only", ["C7_B"]), _pattern("c7_capex_only", ["C7_C"]),
              _pattern("c7_cash_flow_quality_only", ["C7_D"])]
_C7_LIMITS = ["c7_isolated_working_capital_or_capex_mechanism_does_not_generalize"]
_C8_NARROW = [_pattern("c8_historical_return_on_capital_only", ["C8_A"]),
              _pattern("c8_capital_allocation_only", ["C8_D"])]
_C8_LIMITS = ["c8_isolated_historical_roic_or_allocation_decision_does_not_generalize"]
_C9_NARROW = [_pattern("c9_cash_and_debt_structure_only", ["C9_A"]), _pattern("c9_dilution_only", ["C9_D"])]
_C9_LIMITS = ["c9_isolated_cash_debt_or_dilution_does_not_represent_financial_strength"]
_C10_NARROW = [_pattern("c10_multiple_only", ["C10_A"]), _pattern("c10_implied_assumptions_only", ["C10_B"]),
               _pattern("c10_method_or_calculation_only", ["C10_C"]), _pattern("c10_scenarios_only", ["C10_D"])]
_C10_LIMITS = ["c10_local_valuation_step_does_not_generalize_valuation"]
_C11_NARROW = [_pattern("c11_thesis_without_falsifiability", ["C11_A"]),
               _pattern("c11_signal_vs_noise_only", ["C11_C"])]
_C11_LIMITS = ["c11_narration_or_price_reaction_is_not_thesis_reasoning"]
_C12_NARROW = [_pattern("c12_position_role_only", ["C12_A"]),
               _pattern("c12_mechanical_position_change_only", ["C12_D"])]
_C12_LIMITS = ["c12_isolated_position_role_or_sizing_does_not_generalize_portfolio_reasoning"]

POSITIVE_BASIS_V1_POLICY = PositiveBasisPolicy(version=POSITIVE_BASIS_V1, competencies=(
    _competency(
        "C1",
        comprehension=([_pattern("c1_economic_nature_of_investing", ["C1_A"]),
                        _pattern("c1_fundamentals_vs_price_movement", ["C1_C"])], _C1_NARROW, _C1_LIMITS),
        application=([_pattern("c1_economic_nature_of_investing", ["C1_A"]),
                      _pattern("c1_fundamentals_vs_price_movement", ["C1_C"])], _C1_NARROW, _C1_LIMITS)),
    _competency(
        "C2",
        comprehension=([_pattern("c2_risk_nature_related_to_horizon_or_capacity", ["C2_A"], ["C2_B", "C2_C"])],
                       _C2_NARROW, _C2_LIMITS),
        application=([_pattern("c2_risk_nature_related_to_horizon_or_capacity", ["C2_A"], ["C2_B", "C2_C"])],
                     _C2_NARROW, _C2_LIMITS)),
    _competency(
        "C3",
        comprehension=([_pattern("c3_real_exposure_diversification", ["C3_B"])], _C3_NARROW, _C3_LIMITS),
        application=([_pattern("c3_real_exposure_diversification", ["C3_B"])], _C3_NARROW, _C3_LIMITS)),
    _competency(
        "C4",
        comprehension=([_pattern("c4_value_proposition_related_to_revenue_engine", ["C4_A"], ["C4_B"])],
                       _C4_NARROW, _C4_LIMITS),
        application=([_pattern("c4_revenue_engine_related_to_structural_dimension", ["C4_B"],
                               ["C4_A", "C4_C", "C4_D"])], _C4_NARROW, _C4_LIMITS)),
    _competency(
        "C5",
        comprehension=([_pattern("c5_growth_decomposition", ["C5_A"])],
                       [_pattern("c5_drivers_without_decomposition", ["C5_B"]),
                        _pattern("c5_normalisation_only", ["C5_C"]), _pattern("c5_growth_quality_only", ["C5_D"])],
                       _C5_LIMITS),
        application=([_pattern("c5_drivers_related_to_growth_dimension", ["C5_B"], ["C5_A", "C5_C", "C5_D"])],
                     [_pattern("c5_historical_growth_only", ["C5_A"]), _pattern("c5_normalisation_only", ["C5_C"]),
                      _pattern("c5_growth_quality_only", ["C5_D"])], _C5_LIMITS)),
    _competency(
        "C6",
        comprehension=([_pattern("c6_profitability_structure", ["C6_A"])],
                       [_pattern("c6_margin_drivers_only", ["C6_B"]), _pattern("c6_operating_leverage_only", ["C6_C"]),
                        _pattern("c6_normalisation_only", ["C6_D"])], _C6_LIMITS),
        application=([_pattern("c6_causal_margin_drivers", ["C6_B"])],
                     [_pattern("c6_margin_level_statement_only", ["C6_A"]),
                      _pattern("c6_operating_leverage_only", ["C6_C"]), _pattern("c6_normalisation_only", ["C6_D"])],
                     _C6_LIMITS)),
    _competency(
        "C7",
        comprehension=([_pattern("c7_earnings_vs_cash_related_to_conversion", ["C7_A"], ["C7_C", "C7_B"])],
                       _C7_NARROW, _C7_LIMITS),
        application=([_pattern("c7_earnings_vs_cash_related_to_cash_dimension", ["C7_A"], ["C7_B", "C7_C", "C7_D"])],
                     _C7_NARROW, _C7_LIMITS)),
    _competency(
        "C8",
        comprehension=([_pattern("c8_return_on_capital_related_to_intensity", ["C8_A"], ["C8_B"])],
                       _C8_NARROW, _C8_LIMITS),
        application=([_pattern("c8_reinvestment_related_to_returns", ["C8_B"], ["C8_A", "C8_C"])],
                     _C8_NARROW, _C8_LIMITS)),
    _competency(
        "C9",
        comprehension=([_pattern("c9_financial_structure_related_to_obligations", ["C9_A"], ["C9_B"])],
                       _C9_NARROW, _C9_LIMITS),
        application=([_pattern("c9_obligations_related_to_resilience", ["C9_B"], ["C9_C"])],
                     _C9_NARROW, _C9_LIMITS)),
    _competency(
        "C10",
        comprehension=([_pattern("c10_price_depends_on_assumptions", ["C10_B"])],
                       [_pattern("c10_multiple_only", ["C10_A"]), _pattern("c10_method_or_calculation_only", ["C10_C"]),
                        _pattern("c10_scenarios_only", ["C10_D"])], _C10_LIMITS),
        application=([_pattern("c10_implied_assumptions_related_to_valuation_dimension", ["C10_B"],
                               ["C10_A", "C10_C", "C10_D"])], _C10_NARROW, _C10_LIMITS)),
    _competency(
        "C11",
        comprehension=([_pattern("c11_testable_thesis_related_to_critical_risks", ["C11_A"], ["C11_B"])],
                       _C11_NARROW, _C11_LIMITS),
        application=([_pattern("c11_testable_thesis_related_to_critical_risks", ["C11_A"], ["C11_B"]),
                      _pattern("c11_rational_thesis_revision", ["C11_D"])], _C11_NARROW, _C11_LIMITS),
        revision=["C11_D"]),
    _competency(
        "C12",
        comprehension=([_pattern("c12_shared_dependencies", ["C12_B"])],
                       [*_C12_NARROW, _pattern("c12_stress_test_only", ["C12_C"])], _C12_LIMITS),
        application=([_pattern("c12_position_roles_related_to_dependencies", ["C12_A"], ["C12_B"]),
                      _pattern("c12_dependencies_related_to_stress_test", ["C12_B"], ["C12_C"]),
                      _pattern("c12_stress_test_related_to_portfolio_change", ["C12_C"], ["C12_D"])],
                     _C12_NARROW, _C12_LIMITS)),
))

POSITIVE_BASIS_POLICIES = MappingProxyType({POSITIVE_BASIS_V1: POSITIVE_BASIS_V1_POLICY})


def validate_positive_basis_policy(policy) -> None:
    """Validation statique stricte d'une policy (pure) : exactement C1-C12
    dans l'ordre, capacités = celles de taxonomy_v1 pour la compétence,
    patterns non vides, noms uniques par stade, core / related disjoints et
    dans la compétence, Discovery jamais relationnelle."""
    if type(policy) is not PositiveBasisPolicy or type(policy.version) is not str or not policy.version:
        raise InvalidPositiveBasisPolicy("PositiveBasisPolicy attendue")
    codes = tuple(getattr(c, "competency_code", None) for c in policy.competencies)
    if codes != COMPETENCY_CODES:
        raise InvalidPositiveBasisPolicy(f"compétences {codes} (C1-C12 attendues, ordre naturel)")
    for competency in policy.competencies:
        if type(competency) is not CompetencyPositiveBasisPolicy:
            raise InvalidPositiveBasisPolicy("CompetencyPositiveBasisPolicy attendue")
        code = competency.competency_code
        expected = tuple(c for c in CAPABILITY_CODES if c.startswith(f"{code}_"))
        if competency.capability_codes != expected:
            raise InvalidPositiveBasisPolicy(f"{code} : capacités {competency.capability_codes} != {expected}")
        allowed = frozenset(expected)
        for stage in DIRECT_STAGES:
            stage_policy = competency.stage_policy(stage)
            if type(stage_policy) is not StageRepresentativityPolicy or stage_policy.stage != stage:
                raise InvalidPositiveBasisPolicy(f"{code} : policy {stage} invalide")
            if not stage_policy.representative_patterns:
                raise InvalidPositiveBasisPolicy(f"{code} {stage} : aucun pattern représentatif")
            if type(stage_policy.competency_only_allowed) is not bool:
                raise InvalidPositiveBasisPolicy(f"{code} {stage} : competency_only_allowed booléen attendu")
            for group in (stage_policy.representative_patterns, stage_policy.narrow_scope_patterns):
                names = set()
                for pattern in group:
                    _validate_pattern(pattern, allowed, f"{code} {stage}")
                    if pattern.name in names:
                        raise InvalidPositiveBasisPolicy(f"{code} {stage} : pattern {pattern.name} en double")
                    names.add(pattern.name)
            if stage == DISCOVERY and any(p.related_capability_codes for p in stage_policy.representative_patterns):
                raise InvalidPositiveBasisPolicy(f"{code} : Discovery n'est jamais relationnelle")
            if not all(type(limit) is str and limit for limit in stage_policy.generalization_limits):
                raise InvalidPositiveBasisPolicy(f"{code} {stage} : generalization_limits invalides")
        mastery = competency.mastery_policy
        if type(mastery) is not MasteryPolicy or not set(mastery.revision_capability_codes) <= allowed:
            raise InvalidPositiveBasisPolicy(f"{code} : mastery_policy invalide")


def _validate_pattern(pattern, allowed: frozenset, where: str) -> None:
    if type(pattern) is not SemanticPattern or type(pattern.name) is not str or not pattern.name:
        raise InvalidPositiveBasisPolicy(f"{where} : SemanticPattern nommé attendu")
    core, related = pattern.core_capability_codes, pattern.related_capability_codes
    if type(core) is not tuple or type(related) is not tuple or not core:
        raise InvalidPositiveBasisPolicy(f"{where} {pattern.name} : core non vide (tuple) attendu")
    if not set(core) | set(related) <= allowed:
        raise InvalidPositiveBasisPolicy(f"{where} {pattern.name} : capacité hors de la compétence")
    if set(core) & set(related):
        raise InvalidPositiveBasisPolicy(f"{where} {pattern.name} : core et related doivent être disjoints")


# --------------------------------------------------------------------------
# Résolution
# --------------------------------------------------------------------------

def _resolve_pattern(pattern: SemanticPattern, ids_by_code: Mapping) -> ResolvedPattern:
    return ResolvedPattern(name=pattern.name,
                           core_definition_ids=frozenset(ids_by_code[c] for c in pattern.core_capability_codes),
                           related_definition_ids=frozenset(ids_by_code[c] for c in pattern.related_capability_codes))


@dataclass(frozen=True, kw_only=True)
class PositiveBasisPolicyResolver:
    """Choisit la policy par positive_basis_version (acceptée explicitement,
    jamais de défaut) et la confronte au contexte : format V1, compétence,
    release, taxonomie courante. Pur et sans état mutable."""
    registry: Mapping = field(default_factory=lambda: POSITIVE_BASIS_POLICIES)

    def resolve(self, context) -> ResolvedPositiveBasisPolicy:
        if type(context) is not InferenceContext:
            raise UnsupportedInferenceContext("InferenceContext attendu")
        if type(context.input_schema_version) is not int or \
                context.input_schema_version != SUPPORTED_INPUT_SCHEMA_VERSION:
            raise UnsupportedInferenceContext(f"input_schema_version {context.input_schema_version!r}"
                                              f" ({SUPPORTED_INPUT_SCHEMA_VERSION} exigé par T6-C1 V1)")
        if context.model_id is not None or context.prompt_spec_version is not None:
            raise UnsupportedInferenceContext("T6-C1 V1 est déterministe : model_id et prompt_spec_version"
                                              " doivent être None")
        version = context.positive_basis_version
        policy = self.registry.get(version) if type(version) is str else None
        if policy is None:
            raise UnsupportedPositiveBasisPolicy(f"positive_basis_version {version!r} inconnue")
        validate_positive_basis_policy(policy)
        competency = next((c for c in policy.competencies if c.competency_code == context.competency_code), None)
        if competency is None:
            raise InconsistentInferenceContext(f"compétence {context.competency_code!r} hors C1-C12")
        taxonomy, dossier = context.current_taxonomy_context, context.longitudinal_dossier
        if {taxonomy.competency_code, dossier.competency_code} != {context.competency_code}:
            raise InconsistentInferenceContext("compétence divergente entre contexte, dossier T5 et taxonomie")
        if {taxonomy.release_id, dossier.pedagogical_taxonomy_release_id} != {context.pedagogical_taxonomy_release_id}:
            raise InconsistentInferenceContext("release divergente entre contexte, dossier T5 et taxonomie")
        ids_by_code = self._definitions(competency, taxonomy.capabilities)
        stage_policies = {}
        for stage in DIRECT_STAGES:
            declared = competency.stage_policy(stage)
            stage_policies[stage] = ResolvedStagePolicy(
                stage=stage,
                representative_patterns=tuple(_resolve_pattern(p, ids_by_code)
                                              for p in declared.representative_patterns),
                narrow_scope_patterns=tuple(_resolve_pattern(p, ids_by_code) for p in declared.narrow_scope_patterns),
                generalization_limits=declared.generalization_limits,
                competency_only_allowed=declared.competency_only_allowed)
        by_code = {c.capability_code: c for c in taxonomy.capabilities}
        return ResolvedPositiveBasisPolicy(
            policy_version=policy.version,
            competency_code=competency.competency_code,
            release_id=taxonomy.release_id,
            capabilities=tuple(by_code[code] for code in competency.capability_codes),
            definition_ids=tuple(ids_by_code[code] for code in competency.capability_codes),
            stage_policies=MappingProxyType(stage_policies),
            revision_definition_ids=frozenset(ids_by_code[code]
                                              for code in competency.mastery_policy.revision_capability_codes),
        )

    @staticmethod
    def _definitions(competency: CompetencyPositiveBasisPolicy, capabilities) -> dict:
        """{capability_code: definition_id} de la taxonomie courante, après
        vérification : exactement les capacités attendues, chacune en
        semantic_revision supportée, sans doublon. Aucun repli par code."""
        expected = frozenset(competency.capability_codes)
        found, definitions = {}, set()
        for capability in capabilities:
            if type(capability) is not CurrentTaxonomyCapability:
                raise UnsupportedTaxonomySemantics("CurrentTaxonomyCapability attendue")
            code = capability.capability_code
            if code not in expected:
                raise UnsupportedTaxonomySemantics(f"{code!r} inattendue pour {competency.competency_code}"
                                                   " dans positive_basis-1")
            if type(capability.semantic_revision) is not int or \
                    capability.semantic_revision != SUPPORTED_SEMANTIC_REVISION:
                raise UnsupportedTaxonomySemantics(
                    f"{code} semantic_revision {capability.semantic_revision!r} : positive_basis-1 ne connaît que"
                    f" la révision {SUPPORTED_SEMANTIC_REVISION} (même code != même sens)")
            if code in found or capability.definition_id in definitions:
                raise UnsupportedTaxonomySemantics(f"{code} en double dans la taxonomie courante")
            if not isinstance(capability.definition_id, uuid.UUID):
                raise UnsupportedTaxonomySemantics(f"{code} : definition_id invalide")
            found[code] = capability.definition_id
            definitions.add(capability.definition_id)
        missing = [code for code in competency.capability_codes if code not in found]
        if missing:
            raise UnsupportedTaxonomySemantics(f"capacités attendues absentes de la taxonomie courante : {missing}")
        return found


def resolve_positive_basis_policy(context) -> ResolvedPositiveBasisPolicy:
    """Résolution avec le registry versionné par défaut (positive_basis-1)."""
    return PositiveBasisPolicyResolver().resolve(context)
