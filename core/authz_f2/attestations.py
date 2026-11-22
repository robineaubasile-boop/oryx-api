"""Attestations métier internes F2 (V0-T08 Lot 2B-2, PR 0) — pur, DORMANT.

Une attestation est une information INTERNE sur la décision métier déjà prise
par un adaptateur F2 : elle distingue une opération réellement nouvelle et
effective d'un rejeu ou d'une opération sans effet. Elle ne décide rien, ne
modifie aucun résultat public, aucun reçu, aucune réponse HTTP, et n'est
jamais une condition de réussite de l'opération.

Contrat (quatre champs exacts, immuable, vocabulaire fermé) :

  owner_user_id : user_id SERVEUR du principal F2 autorisé (jamais un
                  user_id venu du client) ;
  operation     : nature exacte de l'opération (OPERATIONS) ;
  operation_id  : identifiant canonique de l'opération (uuid.UUID) :
                  - ACK Décrypter : assistant_turn_id de la livraison
                    (une livraison n'est confirmée qu'une fois) ;
                  - portefeuille : operation_id du reçu, dont l'identité
                    durable est le triplet (owner_user_id, operation,
                    operation_id) de portfolio_write_idempotency ;
  qualification : ce que la branche métier EXÉCUTÉE a réellement fait
                  (QUALIFICATIONS, couples admis par ADMISSIBLE).

Aucune donnée financière, aucun contenu IA, aucune donnée
d'authentification (ni session, ni génération, ni secret), aucune date.

Production : seuls l'adaptateur ACK du pont Décrypter (transition observée
sous le verrou FOR UPDATE de la livraison) et l'adaptateur portefeuille
(branche exécutée de _write_portfolio) en construisent, par les fabriques
TOTALES ci-dessous : une entrée hors contrat rend None (statistique perdue),
jamais une exception. Le transport jusqu'au runner est une enveloppe de
données (Attested), jamais un rappel : le runner la retire AVANT ses
contrôles historiques, n'expose l'attestation qu'après un COMMIT confirmé
(AuthorizedTransactionRunner._run_attested) et la rejette si elle n'est pas
liée au principal et à la commande exécutés.

Ce module n'importe rien du projet, n'ouvre aucune connexion, ne lit aucune
configuration et n'appelle aucun réseau ; aucun module analytics n'est
importé ni raccordé.
"""
import uuid
from dataclasses import dataclass
from types import MappingProxyType

# Nature exacte de l'opération (chaînes des reçus portefeuille réutilisées
# telles quelles : portfolio_fingerprint.OPERATION_TYPES, vérifié par les tests).
DECRYPTAGE_DELIVERY_ACK = "decryptage_delivery_ack"
PORTFOLIO_CREATE = "portfolio_position_create"
PORTFOLIO_UPDATE = "portfolio_position_update"
PORTFOLIO_DELETE = "portfolio_position_delete"
OPERATIONS = (DECRYPTAGE_DELIVERY_ACK, PORTFOLIO_CREATE, PORTFOLIO_UPDATE, PORTFOLIO_DELETE)

# Qualification de la branche réellement exécutée.
DELIVERY_FIRST_ACK = "delivery_first_ack"   # pending -> delivered effectuée par CETTE transaction
DELIVERY_REPLAY = "delivery_replay"         # ACK valide sur une livraison déjà delivered
PORTFOLIO_CREATED = "portfolio_created"     # INSERT de la position effectué
PORTFOLIO_UPDATED = "portfolio_updated"     # UPDATE conditionnel effectué (révision incrémentée)
PORTFOLIO_DELETED = "portfolio_deleted"     # DELETE conditionnel effectué
PORTFOLIO_NOOP = "portfolio_noop"           # PUT accepté à valeurs identiques : aucun UPDATE
PORTFOLIO_REPLAY = "portfolio_replay"       # reçu déjà committé restitué, rien n'est réexécuté
QUALIFICATIONS = (DELIVERY_FIRST_ACK, DELIVERY_REPLAY, PORTFOLIO_CREATED, PORTFOLIO_UPDATED, PORTFOLIO_DELETED,
                  PORTFOLIO_NOOP, PORTFOLIO_REPLAY)

# Couples FERMÉS (opération -> qualifications possibles).
ADMISSIBLE = MappingProxyType({
    DECRYPTAGE_DELIVERY_ACK: frozenset({DELIVERY_FIRST_ACK, DELIVERY_REPLAY}),
    PORTFOLIO_CREATE: frozenset({PORTFOLIO_CREATED, PORTFOLIO_REPLAY}),
    PORTFOLIO_UPDATE: frozenset({PORTFOLIO_UPDATED, PORTFOLIO_NOOP, PORTFOLIO_REPLAY}),
    PORTFOLIO_DELETE: frozenset({PORTFOLIO_DELETED, PORTFOLIO_REPLAY}),
})

# Seules qualifications qui constatent un effet métier NOUVEAU ; les no-op et
# les rejeux n'en sont jamais.
NEW_EFFECTS = frozenset({DELIVERY_FIRST_ACK, PORTFOLIO_CREATED, PORTFOLIO_UPDATED, PORTFOLIO_DELETED})

MAX_OWNER_CHARS = 255


class AttestationInvalid(ValueError):
    """Attestation hors contrat (jamais propagée hors de ce module par les
    fabriques : elles rendent None)."""


@dataclass(frozen=True, slots=True, repr=False)
class BusinessAttestation:
    owner_user_id: str
    operation: str
    operation_id: uuid.UUID
    qualification: str

    def __post_init__(self):
        if type(self.owner_user_id) is not str or not 1 <= len(self.owner_user_id) <= MAX_OWNER_CHARS:
            raise AttestationInvalid("propriétaire serveur attendu")
        if type(self.operation) is not str or self.operation not in ADMISSIBLE:
            raise AttestationInvalid("opération hors vocabulaire")
        if type(self.operation_id) is not uuid.UUID:
            raise AttestationInvalid("identifiant d'opération uuid.UUID attendu")
        if type(self.qualification) is not str or self.qualification not in ADMISSIBLE[self.operation]:
            raise AttestationInvalid("qualification hors vocabulaire ou incompatible avec l'opération")

    @property
    def new_effect(self) -> bool:
        """True seulement pour un effet métier nouveau (premier ACK, création,
        modification effective, suppression)."""
        return self.qualification in NEW_EFFECTS

    def __repr__(self) -> str:  # jamais le propriétaire ni l'identifiant dans un journal
        return f"BusinessAttestation({self.operation}, {self.qualification})"


@dataclass(frozen=True, slots=True, repr=False)
class Attested:
    """Enveloppe interne adaptateur -> runner : la valeur métier INCHANGÉE et
    son attestation. Jamais rendue par le runner (retirée avant ses contrôles),
    jamais transmise à une route. Construction sans validation : elle ne peut
    pas échouer."""
    value: object
    attestation: object

    def __repr__(self) -> str:
        return "Attested(<private>)"


def _build(owner_user_id, operation, operation_id, qualification):
    try:
        return BusinessAttestation(owner_user_id, operation, operation_id, qualification)
    except Exception:  # fabrique TOTALE : statistique perdue, jamais une opération en échec
        return None


def delivery_ack(owner_user_id, assistant_turn_id, *, first) -> object:
    """ACK Décrypter : first est la transition pending -> delivered OBSERVÉE
    sous le verrou de la livraison avant toute mutation ; None si hors
    contrat (first doit être un booléen exact)."""
    if type(first) is not bool:
        return None
    return _build(owner_user_id, DECRYPTAGE_DELIVERY_ACK, assistant_turn_id,
                  DELIVERY_FIRST_ACK if first else DELIVERY_REPLAY)


def portfolio(owner_user_id, operation, operation_id, qualification) -> object:
    """Écriture portefeuille : qualification de la branche exécutée ; None si
    hors contrat."""
    return _build(owner_user_id, operation, operation_id, qualification)


def attach(value, attestation):
    """value telle quelle si aucune attestation, sinon l'enveloppe interne."""
    return value if attestation is None else Attested(value, attestation)


def unwrap(value) -> tuple:
    """(valeur métier, attestation brute ou None) : retire l'enveloppe sans
    rien examiner d'autre."""
    if type(value) is Attested:
        return value.value, value.attestation
    return value, None


def bound(attestation, *, owner_user_id, operation, operation_id) -> bool:
    """Liaison exacte au principal et à la commande EXÉCUTÉS (jamais
    interchangeable entre deux opérations ou deux utilisateurs)."""
    return (type(attestation) is BusinessAttestation and attestation.owner_user_id == owner_user_id
            and attestation.operation == operation and attestation.operation_id == operation_id)
