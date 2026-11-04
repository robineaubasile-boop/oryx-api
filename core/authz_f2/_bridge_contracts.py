"""Contrats communs PURS entre les adaptateurs SQL et le pont ORM privé
(R1 PR 9-C1-B, F-01) — dormant.

PR 9-C1-B exige que core/authz_f2/_adapters.py appelle le pont ORM privé
(core/authz_f2/_decryptage_orm_bridge.py) ; le pont importait jusque-là
_adapters (exceptions, sentinelles, sonde d'isolation). Pour qu'aucun cycle
_adapters <-> pont n'existe, ce module porte SEULEMENT :

- les sentinelles fermées ABSENT, CONFLICT, IDEMPOTENCY_CONFLICT et
  l'exception racine AdapterInvariantBroken, DÉFINIES ICI une seule fois et
  réexportées par _adapters (mêmes objets : `_adapters.ABSENT is
  _bridge_contracts.ABSENT`, aucune seconde classe indépendante) ;
- les exceptions de transaction partagées (TransactionLost,
  CapabilityRefused) ;
- les contrats SCELLÉS des deux capacités privées :

  | Contrat | Implémentation (seul module autorisé) | Capacité |
  |---|---|---|
  | TransactionProbe | core.authz_f2._adapters | vérifier réellement la transaction du runner (identité, activité, sonde PostgreSQL READ COMMITTED) |
  | FinalizationReadPort (étend TransactionProbe) | core.authz_f2._adapters | en plus : UNE relecture de préparation B2 (read_preparation) liée au SAVEPOINT C1 courant |

  Une implémentation n'est admise que si elle est définie dans
  core.authz_f2._adapters, et au plus une par contrat (__init_subclass__) :
  après l'import de _adapters, toute autre sous-classe est refusée. Le pont
  n'accepte que la classe EXACTE enregistrée (implementation()), jamais une
  sous-classe ni un objet qui en imiterait l'interface.

Aucun SQL, aucun Engine, aucune Session, aucune connexion, aucun réseau,
aucune variable d'environnement ; n'importe aucun module applicatif.
"""

IMPLEMENTING_MODULE = "core.authz_f2._adapters"


class _Absent:
    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()


class _Conflict:
    __slots__ = ()

    def __repr__(self) -> str:
        return "CONFLICT"


CONFLICT = _Conflict()  # révision ou capacité périmée : aucune écriture (ROLLBACK du runner)


class _IdempotencyConflict:
    __slots__ = ()

    def __repr__(self) -> str:
        return "IDEMPOTENCY_CONFLICT"


IDEMPOTENCY_CONFLICT = _IdempotencyConflict()  # même identité, autre intention : aucune écriture


class AdapterInvariantBroken(RuntimeError):
    """État impossible constaté sous verrou : la transaction est annulée."""

    alert = None  # nom d'alerte interne journalisé par le runner (sans détail)


class TransactionLost(AdapterInvariantBroken):
    """La transaction externe n'est plus la transaction active du runner :
    aucun succès ne peut être rendu."""

    alert = "decryptage_bridge_transaction_lost"


class CapabilityRefused(AdapterInvariantBroken):
    """Capacité privée utilisée hors de son contrat : autre connexion, autre
    transaction, autre principal, opération close, lecture déjà consommée,
    hors du SAVEPOINT C1 courant, capacité non admise pour l'opération."""

    alert = "decryptage_bridge_capability_refused"


_IMPLEMENTATIONS = {}


class TransactionProbe:
    """Contrat SCELLÉ : capacité de vérifier la transaction du runner pour
    UNE opération du pont (connexion exacte, transaction externe active,
    principal autorisé). Aucune autre capacité."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__module__ == __name__:
            return  # contrat dérivé défini ici (FinalizationReadPort) : jamais une implémentation
        if cls.__module__ != IMPLEMENTING_MODULE:
            raise TypeError("capacité privée : implémentation réservée aux adaptateurs SQL")
        contract = FinalizationReadPort if issubclass(cls, FinalizationReadPort) else TransactionProbe
        if contract in _IMPLEMENTATIONS:
            raise TypeError("capacité privée : une seule implémentation par contrat")
        _IMPLEMENTATIONS[contract] = cls

    @property
    def user_id(self) -> str:
        raise NotImplementedError

    def bound_to(self, conn, user_id: str) -> bool:
        raise NotImplementedError

    def require_live(self) -> None:
        raise NotImplementedError


class FinalizationReadPort(TransactionProbe):
    """Contrat SCELLÉ, réservé à l'opération de finalisation C1-B :
    TransactionProbe + réservation B1 verrouillée, identité complète du
    tour, epoch, capacité de bail, ticker résolu et principal, figés à la
    construction par les adaptateurs. Seule lecture métier :
    read_preparation(db, scope), consommable une fois, dans le SAVEPOINT C1
    lié. Aucune méthode d'exécution SQL ni de rappel."""

    __slots__ = ()

    def read_preparation(self, db, scope):
        raise NotImplementedError


def implementation(contract):
    """Classe EXACTE admise pour un contrat, ou None (adaptateurs non chargés)."""
    if contract not in (TransactionProbe, FinalizationReadPort):
        raise TypeError("contrat de capacité inconnu")
    return _IMPLEMENTATIONS.get(contract)
