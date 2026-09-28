"""Bootstrap, vérification et activation contrôlée de la taxonomie Oryx V1
(T4-C), au-dessus du service transactionnel T4-B (core/taxonomy_service.py).

Trois opérations internes, sans route ni LLM ni inférence :

- bootstrap_taxonomy_v1(db) : crée (ou retrouve à l'identique) la release
  candidate « oryx-v1 », ses 45 définitions r1 et ses 45 memberships ;
- verify_taxonomy_v1(db) : compare strictement la base à la SPEC locale
  (core/pedagogy/taxonomy_v1.py), sans rien modifier ;
- activate_taxonomy_v1(db) : n'appelle activate_release (T4-B) qu'après une
  vérification 45/45 stricte.

Transactions : aucune fonction de ce module ne fait commit() ni
rollback() ; toutes les écritures passent par les primitives T4-B, qui
flushent. L'appelant possède la transaction : bootstrap -> verify -> commit,
ou rollback de tout le lot si une exception est levée (rien n'est alors
persisté). Seule la CLI scripts/bootstrap_pedagogical_taxonomy_v1.py,
propriétaire de sa Session, commite.

Bootstrap != activation. Le bootstrap ne produit qu'une release candidate
(activated_at NULL) : aucun run ne l'utilise tant qu'elle n'est pas active.
L'activation est une action distincte et explicite (jamais au deploy, jamais
dans la CLI de bootstrap) ; activate_release de T4-B accepte volontairement
une candidate vide, ce module ajoute le contrat de contenu « V1 complète et
conforme » sans modifier T4-B.

Idempotence. Chaque définition (capability_code, semantic_revision) absente
est créée par create_capability_definition ; présente et STRICTEMENT
identique (code, révision, compétence, libellé, définition,
mapping_guidance, types JSON exacts), elle est réutilisée (même UUID) ;
présente mais différente => TaxonomyBootstrapConflict : la ligne n'est
jamais réécrite ni « réparée », et une autre r1 n'est jamais créée (un
nouveau sens = une semantic_revision suivante, dans une future release).
Toutes les définitions existantes sont comparées AVANT la première
écriture. Un second bootstrap identique est un NO-OP sémantique : 1 release,
45 définitions, 45 memberships.

Release oryx-v1 existante : son spec_fingerprint doit être exactement celui
de la SPEC (sinon TaxonomyBootstrapConflict ; il n'est jamais modifié), puis
son contenu est vérifié strictement, quel que soit son statut. candidate,
active ou retired conforme => NO-OP (jamais réactivée, jamais modifiée).
Une V1 persistée partielle ou divergente (membership manquant ou en trop,
mauvaise révision, définition ou mapping_guidance différent) =>
TaxonomyVerificationError : elle n'est JAMAIS réparée silencieusement (le
bootstrap ne devine pas l'intention). Seule la transaction qui crée la
release y attache les 45 memberships ; une V1 commitée par ce module est
donc toujours complète.

Vérification : fingerprint ET contenu. spec_fingerprint est une chaîne
stockée, jamais recalculée depuis les tables ; il couvre en outre les 12
compétences et les 8 règles globales, qui n'ont pas de table. verify exige
donc les DEUX : (1) release.spec_fingerprint == fingerprint recalculé de la
SPEC locale (lui-même égal à EXPECTED_V1_FINGERPRINT) ; (2) les 45
memberships / définitions persistés == la partie persistable de la SPEC.
Les compétences et règles ne peuvent pas être relues : elles sont garanties
par (1).

Concurrence (PostgreSQL READ COMMITTED, défaut de SessionLocal). Aucun
verrou supplémentaire : ceux de T4-B suffisent et l'ordre global reste le
sien. Deux bootstraps simultanés sur une base vide :
- le premier (A) insère C1_A r1 ... C12_D r1 puis la release et ses
  memberships, sans commiter ;
- le second (B) ne voit rien de non commité, tente d'insérer C1_A r1 et
  attend sur l'index unique uq_core_capability_definitions_code_revision
  (savepoint T4-B) ;
- si A commite : T4-B traduit la violation en DuplicateCapabilityDefinition,
  B relit la ligne commitée (nouvel instantané de requête en READ
  COMMITTED), la compare strictement et la réutilise ; il en va de même
  pour chaque définition, puis pour la release (DuplicateTaxonomyRelease =>
  relecture de la V1 commitée par A, vérification stricte) : B termine en
  NO-OP, sans erreur ;
- si A annule : l'INSERT de B aboutit et B construit V1 lui-même.
Il n'y a aucune boucle de retry : au plus UNE relecture par clé, après un
doublon signalé par T4-B. Un conflit de contenu (définition ou release
concurrente différente) reste une erreur forte. Les deux transactions
insèrent les mêmes clés dans le même ordre naturel (C1_A -> C12_D, puis la
release) : le perdant attend toujours le gagnant sur la première clé
commune, jamais l'inverse, donc aucun cycle d'attente. Le bootstrap ne prend
ni le verrou consultatif d'activation ni aucun verrou de run / événement /
observation : aucun cycle release <-> définition <-> activation.

Activation : activate_taxonomy_v1 vérifie V1 (gate AVANT T4-B), puis appelle
activate_release, propriétaire du verrou consultatif, des verrous de
release, de la retraite atomique de l'ancienne active et de la concurrence
d'activation ; puis revérifie V1 sous le verrou de release que T4-B vient
de prendre (aucun attach ne peut plus s'intercaler). Déjà active et
conforme => résultat idempotent (activated=False), sans appel à T4-B, dont
le comportement générique (refus de réactiver une active) est inchangé ;
une activation concurrente perdue (T4-B lève InvalidTaxonomyState après
avoir rechargé V1 active sous verrou) aboutit au même résultat idempotent.
retired => InvalidTaxonomyState (exception T4-B réutilisée, levée avant
tout appel à activate_release) : jamais réactivée.

V2 future (non implémentée) : oryx-v2 sera une autre release candidate qui
réutilisera les définitions r1 inchangées (mêmes UUID) et n'ajoutera une r2
que pour les capacités dont le sens change ; oryx-v1 reste intacte.
"""
import json
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select

from core import taxonomy_service as tax
from core.models import CoreCapabilityDefinition, PedagogicalTaxonomyRelease
from core.pedagogy.taxonomy_v1 import (
    CAPABILITY_CODES,
    PERSISTED_CAPABILITY_FIELDS,
    VERSION_KEY,
    load_taxonomy_v1,
)

_RELEASE_STATUSES = (tax.RELEASE_CANDIDATE, tax.RELEASE_ACTIVE, tax.RELEASE_RETIRED)


class TaxonomyBootstrapError(Exception):
    """Erreur métier de l'orchestration T4-C en base."""


class TaxonomyBootstrapConflict(TaxonomyBootstrapError):
    """Un contenu déjà persisté contredit la SPEC V1 : définition
    (capability_code, révision 1) différente, release oryx-v1 d'un autre
    fingerprint, ou fingerprint V1 porté par une autre version_key. Rien
    n'est modifié."""


class TaxonomyVerificationError(TaxonomyBootstrapError):
    """La release oryx-v1 persistée ne correspond pas strictement à la
    SPEC V1 (fingerprint, memberships, révisions, contenus). Rien n'est
    réparé."""


@dataclass(frozen=True)
class TaxonomyVerificationResult:
    """État vérifié, conforme à la SPEC, de la release oryx-v1."""
    release_id: uuid.UUID
    version_key: str
    spec_fingerprint: str
    status: str
    capability_count: int


@dataclass(frozen=True)
class TaxonomyBootstrapResult(TaxonomyVerificationResult):
    """État vérifié + ce que CE bootstrap a écrit (tout à zéro / False pour
    un NO-OP)."""
    created_release: bool
    created_definitions: int
    reused_definitions: int
    created_memberships: int


@dataclass(frozen=True)
class TaxonomyActivationResult(TaxonomyVerificationResult):
    """État vérifié après activation ; activated=False si V1 était déjà
    active (résultat idempotent, aucune écriture)."""
    activated: bool


# --------------------------------------------------------------------------
# Lectures et comparaisons
# --------------------------------------------------------------------------

def _canonical(value) -> str:
    """Forme de comparaison stricte : distingue les types JSON (1, 1.0,
    True, "1") et ignore l'ordre des clés, pas celui des listes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _differing_fields(definition, capability: dict) -> list[str]:
    return [field for field in PERSISTED_CAPABILITY_FIELDS
            if _canonical(getattr(definition, field)) != _canonical(capability[field])]


def _find_release(db, condition) -> PedagogicalTaxonomyRelease | None:
    """Lecture sans verrou, rechargée depuis la base (un objet déjà présent
    dans la Session reflète l'état le plus récent visible). version_key et
    spec_fingerprint sont UNIQUE : au plus une ligne."""
    return db.execute(
        select(PedagogicalTaxonomyRelease)
        .where(condition)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _find_v1(db) -> PedagogicalTaxonomyRelease | None:
    return _find_release(db, PedagogicalTaxonomyRelease.version_key == VERSION_KEY)


def _find_definition(db, capability: dict) -> CoreCapabilityDefinition | None:
    return db.execute(
        select(CoreCapabilityDefinition)
        .where(CoreCapabilityDefinition.capability_code == capability["capability_code"],
               CoreCapabilityDefinition.semantic_revision == capability["semantic_revision"])
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _require_identical(definition, capability: dict) -> None:
    differing = _differing_fields(definition, capability)
    if differing:
        raise TaxonomyBootstrapConflict(
            f"{capability['capability_code']} révision {capability['semantic_revision']} déjà persistée"
            f" ({definition.id}) avec {', '.join(differing)} différents de la SPEC V1 : jamais réécrite"
            " (un nouveau sens exige une semantic_revision suivante)")


def _verify_rows(release, rows, spec: dict, fingerprint: str) -> TaxonomyVerificationResult:
    """Comparaison pure release + [(membership, définition)] <-> SPEC ;
    TaxonomyVerificationError au premier écart."""
    if release.version_key != VERSION_KEY:
        raise TaxonomyVerificationError(f"version_key {release.version_key!r} ({VERSION_KEY!r} attendue)")
    if release.spec_fingerprint != fingerprint:
        raise TaxonomyVerificationError(
            f"{VERSION_KEY} : spec_fingerprint persisté {release.spec_fingerprint} différent de la SPEC"
            f" {fingerprint}")
    if release.status not in _RELEASE_STATUSES:
        raise TaxonomyVerificationError(f"{VERSION_KEY} : statut {release.status!r} inconnu")

    expected = {capability["capability_code"]: capability for capability in spec["capabilities"]}
    persisted = {}
    for _membership, definition in rows:
        persisted.setdefault(definition.capability_code, []).append(definition)
    missing = [code for code in CAPABILITY_CODES if code in expected and code not in persisted]
    extra = sorted(code for code in persisted if code not in expected)
    doubled = [code for code in CAPABILITY_CODES if len(persisted.get(code, ())) > 1]
    if missing or extra or doubled or len(rows) != len(expected):
        raise TaxonomyVerificationError(
            f"{VERSION_KEY} : {len(rows)} memberships pour {len(expected)} capacités attendues"
            f" (manquantes {missing}, hors SPEC {extra}, plusieurs révisions {doubled})")
    for code in CAPABILITY_CODES:
        (definition,) = persisted[code]
        differing = _differing_fields(definition, expected[code])
        if differing:
            raise TaxonomyVerificationError(
                f"{VERSION_KEY} : {code} ({definition.id}) diffère de la SPEC sur {', '.join(differing)}")
    return TaxonomyVerificationResult(release_id=release.id, version_key=release.version_key,
                                      spec_fingerprint=release.spec_fingerprint, status=release.status,
                                      capability_count=len(rows))


def _verify_release(db, release, spec: dict, fingerprint: str) -> TaxonomyVerificationResult:
    return _verify_rows(release, tax.get_release_capabilities(db, release_id=release.id), spec, fingerprint)


def _unchanged(db, release, spec: dict, fingerprint: str) -> TaxonomyBootstrapResult:
    """oryx-v1 déjà persistée : conflit si autre fingerprint, sinon
    vérification stricte ; aucune écriture, quel que soit le statut."""
    if release.spec_fingerprint != fingerprint:
        raise TaxonomyBootstrapConflict(
            f"{VERSION_KEY} existe ({release.id}, {release.status}) avec le fingerprint"
            f" {release.spec_fingerprint}, différent de la SPEC {fingerprint} : jamais modifié")
    verified = _verify_release(db, release, spec, fingerprint)
    return TaxonomyBootstrapResult(**asdict(verified), created_release=False, created_definitions=0,
                                   reused_definitions=0, created_memberships=0)


def _create_or_reuse(db, capability: dict) -> tuple[CoreCapabilityDefinition, bool]:
    """Création via T4-B ; si une transaction concurrente a créé la même
    clé entre notre lecture et notre INSERT (DuplicateCapabilityDefinition,
    savepoint T4-B déjà annulé), UNE relecture stricte, sans retry."""
    try:
        return tax.create_capability_definition(
            db, **{field: capability[field] for field in PERSISTED_CAPABILITY_FIELDS}), True
    except tax.DuplicateCapabilityDefinition:
        definition = _find_definition(db, capability)
        if definition is None:
            raise TaxonomyBootstrapError(
                f"{capability['capability_code']} révision {capability['semantic_revision']} signalée en"
                " doublon mais introuvable") from None
        _require_identical(definition, capability)
        return definition, False


# --------------------------------------------------------------------------
# API interne
# --------------------------------------------------------------------------

def bootstrap_taxonomy_v1(db) -> TaxonomyBootstrapResult:
    """Assure l'existence de la release candidate oryx-v1 conforme à la
    SPEC (voir la docstring du module) : validation de la SPEC et du
    fingerprint, inspection en lecture seule, création / réutilisation des
    45 définitions, création de la release, 45 attachements, vérification
    finale. Jamais d'activation, de commit ni de rollback."""
    spec, fingerprint = load_taxonomy_v1()

    release = _find_v1(db)
    if release is not None:
        return _unchanged(db, release, spec, fingerprint)
    homonym = _find_release(db, PedagogicalTaxonomyRelease.spec_fingerprint == fingerprint)
    if homonym is not None:
        raise TaxonomyBootstrapConflict(
            f"le fingerprint {fingerprint} de {VERSION_KEY} est déjà porté par {homonym.version_key!r}")

    # Inspection complète avant la première écriture : un conflit laisse la
    # transaction de l'appelant intacte.
    plan = []
    for capability in spec["capabilities"]:
        existing = _find_definition(db, capability)
        if existing is not None:
            _require_identical(existing, capability)
        plan.append((capability, existing))

    definitions, created = [], 0
    for capability, existing in plan:
        if existing is None:
            existing, was_created = _create_or_reuse(db, capability)
            created += was_created
        definitions.append(existing)

    try:
        release = tax.create_candidate_release(db, version_key=VERSION_KEY, spec_fingerprint=fingerprint)
    except tax.DuplicateTaxonomyRelease:
        # Un bootstrap concurrent a commité V1 entre notre lecture et notre
        # INSERT : UNE relecture stricte (jamais de réparation ni de retry).
        release = _find_v1(db)
        if release is None:
            raise TaxonomyBootstrapConflict(
                f"le fingerprint {fingerprint} de {VERSION_KEY} est déjà porté par une autre release") from None
        return _unchanged(db, release, spec, fingerprint)

    for definition in definitions:
        tax.attach_capability_to_release(db, release_id=release.id, capability_definition_id=definition.id)
    verified = _verify_release(db, release, spec, fingerprint)
    return TaxonomyBootstrapResult(**asdict(verified), created_release=True, created_definitions=created,
                                   reused_definitions=len(definitions) - created,
                                   created_memberships=len(definitions))


def verify_taxonomy_v1(db) -> TaxonomyVerificationResult:
    """Vérification stricte, en lecture seule, de oryx-v1 contre la SPEC :
    fingerprint recalculé ET 45 memberships / définitions persistés. Tout
    statut est vérifiable (candidate, active, retired). Absente =>
    TaxonomyReleaseNotFound (T4-B) ; divergente => TaxonomyVerificationError.
    Aucune écriture, aucun verrou, aucun flush, ni commit ni rollback."""
    spec, fingerprint = load_taxonomy_v1()
    release = _find_v1(db)
    if release is None:
        raise tax.TaxonomyReleaseNotFound(VERSION_KEY)
    return _verify_release(db, release, spec, fingerprint)


def activate_taxonomy_v1(db) -> TaxonomyActivationResult:
    """candidate -> active pour oryx-v1, uniquement si elle est complète
    (45/45) et strictement conforme : vérification AVANT T4-B, activation
    par activate_release (verrous, retraite atomique de l'ancienne active),
    puis revérification sous le verrou de release pris par T4-B (une
    divergence lève TaxonomyVerificationError : l'appelant annule, donc
    l'activation aussi). Déjà active et conforme => activated=False, aucune
    écriture. retired => InvalidTaxonomyState, jamais réactivée."""
    spec, fingerprint = load_taxonomy_v1()
    release = _find_v1(db)
    if release is None:
        raise tax.TaxonomyReleaseNotFound(VERSION_KEY)
    verified = _verify_release(db, release, spec, fingerprint)
    if verified.status == tax.RELEASE_ACTIVE:
        return TaxonomyActivationResult(**asdict(verified), activated=False)
    if verified.status != tax.RELEASE_CANDIDATE:
        raise tax.InvalidTaxonomyState(f"{VERSION_KEY} est {verified.status} : jamais réactivée")

    try:
        tax.activate_release(db, release_id=release.id)
    except tax.InvalidTaxonomyState:
        # Activation concurrente commitée avant la nôtre : T4-B a rechargé
        # la release sous son verrou (populate_existing). Active => même
        # résultat idempotent ; tout autre état est une vraie erreur.
        if release.status != tax.RELEASE_ACTIVE:
            raise
        return TaxonomyActivationResult(**asdict(_verify_release(db, release, spec, fingerprint)),
                                        activated=False)
    return TaxonomyActivationResult(**asdict(_verify_release(db, release, spec, fingerprint)), activated=True)
