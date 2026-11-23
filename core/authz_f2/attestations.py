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
données (Attested, tuple natif), jamais un rappel ; attach et unwrap sont
TOTAUX (correctif d'audit PR 0 : toute panne de construction ou de transport
abandonne SEULEMENT l'attestation et rend la valeur métier d'origine) ; le
runner la retire AVANT ses contrôles historiques, n'expose l'attestation qu'après un COMMIT confirmé
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


class Attested(tuple):
    """Enveloppe interne adaptateur -> runner : la valeur métier INCHANGÉE et
    son attestation, stockées dans un tuple NATIF (valeur, attestation).
    Jamais rendue par le runner (retirée avant ses contrôles), jamais transmise
    à une route. Construction et extraction n'utilisent que les opérations
    natives de tuple (tuple.__new__, tuple.__len__, tuple.__getitem__) : aucune
    méthode redéfinissable, aucune validation, aucune exception possible pour
    une enveloppe construite ici (attach)."""
    __slots__ = ()

    def __new__(cls, value, attestation):
        return tuple.__new__(cls, (value, attestation))

    @property
    def value(self):
        return tuple.__getitem__(self, 0)

    @property
    def attestation(self):
        return tuple.__getitem__(self, 1)

    def __repr__(self) -> str:
        return "Attested(<private>)"


# Alias privé du transport (attach / unwrap) ; ENVELOPE_SIZE : (valeur, attestation).
_ENVELOPE = Attested
ENVELOPE_SIZE = 2


def _build(owner_user_id, operation, operation_id, qualification):
    try:
        return BusinessAttestation(owner_user_id, operation, operation_id, qualification)
    except Exception:  # fabrique TOTALE : statistique perdue, jamais une opération en échec
        return None


def delivery_ack(owner_user_id, assistant_turn_id, *, first) -> object:
    """ACK Décrypter : first est la transition pending -> delivered OBSERVÉE
    sous le verrou de la livraison avant toute mutation ; None si hors
    contrat (first doit être un booléen exact). Ne lève jamais."""
    try:
        if type(first) is not bool:
            return None
        return _build(owner_user_id, DECRYPTAGE_DELIVERY_ACK, assistant_turn_id,
                      DELIVERY_FIRST_ACK if first else DELIVERY_REPLAY)
    except Exception:  # fabrique TOTALE
        return None


def portfolio(owner_user_id, operation, operation_id, qualification) -> object:
    """Écriture portefeuille : qualification de la branche exécutée ; None si
    hors contrat. Ne lève jamais."""
    try:
        return _build(owner_user_id, operation, operation_id, qualification)
    except Exception:  # fabrique TOTALE
        return None


def attach(value, attestation):
    """TOTAL : value telle quelle si aucune attestation ou si l'enveloppe ne
    peut pas être construite ; sinon l'enveloppe interne, VÉRIFIÉE (type exact,
    taille, valeur et attestation restituées à l'identique) avant d'être
    rendue. Une panne n'abandonne que l'attestation : la valeur métier
    d'origine est toujours rendue, jamais une autre."""
    if attestation is None:
        return value
    try:
        envelope = tuple.__new__(_ENVELOPE, (value, attestation))
        if (type(envelope) is _ENVELOPE and tuple.__len__(envelope) == ENVELOPE_SIZE
                and tuple.__getitem__(envelope, 0) is value and tuple.__getitem__(envelope, 1) is attestation):
            return envelope
    except Exception:  # transport TOTAL : attestation abandonnée, valeur intacte
        pass
    return value


def unwrap(value) -> tuple:
    """TOTAL pour toute valeur métier : (valeur, None) si ce n'est pas une
    enveloppe ; (valeur d'origine, attestation brute) sinon, par les seules
    opérations natives de tuple. Une enveloppe de taille inattendue ne peut pas
    venir d'attach (taille vérifiée à la construction) : sa valeur métier
    n'étant pas restituable, refus fermé (AttestationInvalid, donc ROLLBACK et
    UNAVAILABLE), jamais une enveloppe ni une valeur substituée rendue comme
    résultat."""
    if type(value) is not _ENVELOPE:
        return value, None
    if tuple.__len__(value) != ENVELOPE_SIZE:
        raise AttestationInvalid("enveloppe interne malformée")
    return tuple.__getitem__(value, 0), tuple.__getitem__(value, 1)


def bound(attestation, *, owner_user_id, operation, operation_id) -> bool:
    """Liaison exacte au principal et à la commande EXÉCUTÉS (jamais
    interchangeable entre deux opérations ou deux utilisateurs). Ne lève
    jamais : toute anomalie vaut False (attestation abandonnée)."""
    try:
        return (type(attestation) is BusinessAttestation and attestation.owner_user_id == owner_user_id
                and attestation.operation == operation and attestation.operation_id == operation_id)
    except Exception:
        return False
