"""AuthorizedTransactionRunner (C.5.2 §5, BF1, BF2).

Unique propriétaire des transactions de la frontière d'autorisation : il
ouvre chaque transaction (core.auth_f2.txn : lock_timeout et
statement_timeout en SET LOCAL), prend les verrous du plan de la commande,
lit T_authorization, décide, fait exécuter la commande par son adaptateur
privé, puis COMMIT ou ROLLBACK intégral. Il n'accepte que des commandes
typées du registre fermé (core.authz_f2._adapters.REGISTRY) : jamais une
fonction, jamais un user_id client. Aucun adaptateur ne voit de méthode
transactionnelle ; aucun appel réseau n'a lieu dans ses transactions.

Phase A — pré-validation (transaction courte, sans verrou ni écriture) :
justificatif désigné relu (core.auth_f2.sessions.read_session), jugé par
le validateur F2 (session_valid) et livraison 'confirmed' exigée ; rend
candidate_user_id, puis la transaction se termine.

Phase B — autorisation définitive (nouvelle transaction) :

  1. users <plan> (FOR KEY SHARE en lecture, FOR UPDATE en mutation),
     filtre candidate_user_id ;
  2. oryx_sessions FOR SHARE puis oryx_session_generations FOR SHARE
     (core.auth_f2.sessions.lock_session) ;
  3. verrous métiers de la commande (lignes filtrées par id ET user_id) ;
  4. T_authorization = clock_timestamp(), lu APRÈS ces verrous ;
  5. session, génération, révocation, expiration (T_authorization <
     absolute_expires_at, l'égalité refuse), secret et livraison confirmée
     revérifiés ; session.user_id = candidate_user_id ; profil présent ;
  6. appartenance de la ressource ;
  7. exécution par l'adaptateur, puis COMMIT ; toute erreur : ROLLBACK.

Ordre compatible avec l'ordre global F2 (... -> users -> ... -> session ->
génération -> ...) : aucun rang n'est remonté. Une révocation committée
avant l'étape 2 est vue (FOR SHARE relit la dernière version) : refus. Une
révocation commencée après l'étape 2 attend le COMMIT (son FOR UPDATE sur
oryx_sessions attend notre FOR SHARE). Une expiration survenue pendant une
attente de verrou est jugée à T_authorization : refus, aucune écriture.
Une expiration postérieure à T_authorization n'annule pas la transaction
autorisée.

Classification (C.5.2-A) : SESSION_INVALID (401) pour tout justificatif
non admissible, y compris une version de K_active inconnue (aucune
substitution) ; UNAVAILABLE (503) pour toute panne (PostgreSQL, délai de
verrou ou d'instruction, COMMIT en échec, erreur imprévue) et pour un
profil incohérent avec une session pourtant valide ; NOT_FOUND pour une
ressource étrangère ou inexistante (même résultat, rien n'est écrit).

R1 PR 6 : REVISION_CONFLICT, résultat métier fermé distinct des quatre
autres, rendu seulement APRÈS l'autorisation définitive quand la révision
attendue d'UpdateProfileLevel est périmée : ROLLBACK, aucune écriture, ni
profil courant ni principal transportés. Le plan PROFILE_MUTATION suit les
étapes ci-dessus (users FOR UPDATE, session, génération, aucun verrou
métier). Un changement demandé à la révision maximale rend UNAVAILABLE et
journalise une alerte interne, sans détail. Un COMMIT en échec ou
incertain rend UNAVAILABLE : le runner ne prétend jamais que la
transaction a été annulée et ne rejoue rien.

R1 PR 7 (plan PORTFOLIO_MUTATION) : le runner reçoit un trousseau
portefeuille DÉDIÉ (PortfolioKeyring, facultatif ; absent : toute commande
portefeuille rend UNAVAILABLE avant toute transaction). Phase B : isolation
READ COMMITTED vérifiée en premier (_adapters.require_read_committed,
refus fermé), puis users FOR UPDATE -> session -> génération -> reçu du
triplet FOR UPDATE -> (sans reçu, PUT / DELETE) position propre FOR UPDATE
-> T_authorization -> revalidation intégrale -> exécution (reçu restitué,
conflit, ou mutation + reçu) -> COMMIT. L'état verrouillé est d'un type
EXACT du registre (Spec.states) ; l'ancien refus d'ABSENT avant
l'exécution est remplacé par cet état fermé : une position absente n'est
jugée 404 qu'après l'autorisation et l'examen du reçu. Résultat fermé
supplémentaire IDEMPOTENCY_CONFLICT (même triplet, autre intention),
rendu après l'autorisation par ROLLBACK ; REVISION_CONFLICT sert aussi au
CAS des positions. Un rejeu est soumis exactement aux mêmes contrôles
qu'une première exécution. Points d'interruption supplémentaires (tests) :
position_written, receipt_ready, receipt_written.

R1 PR 9-A (plan DECRYPTAGE_MUTATION) : même séquence de phase B, inchangée
(READ COMMITTED vérifié, users FOR NO KEY UPDATE, session, génération,
aucun verrou métier préparatoire, T_authorization, décision) ; le plan est
seulement transmis à require_read_committed pour l'alerte. Aucune commande
du registre de production n'utilise ce plan : il n'est exercé que par le
banc d'essai isolé des tests (registre fermé de test composé et restauré
par une fixture, pont ORM privé core.authz_f2._decryptage_orm_bridge).

R1 PR 9-B1 : deux commandes du registre de production utilisent
DECRYPTAGE_MUTATION (ReserveDecryptageTurn, RecordDecryptageAttemptFailure),
séquence de phase B INCHANGÉE (spec.lock = _no_business_lock : aucun
préverrouillage ; leurs verrous métier sont pris par l'adaptateur
d'exécution, après la décision). Dépendance facultative DÉDIÉE
decryptage_keyring (DecryptageKeyring, type exact ; aucune de ses clés ne
peut être une clé F2 ou portefeuille) : Spec.keyed désigne toujours et
seulement le trousseau portefeuille, Spec.decryptage_keyed le trousseau
Décrypter ; les deux à la fois sont refusés à la construction de Spec et,
par défense, ici (UNAVAILABLE, alerte key_families_conflict, avant toute
transaction). Absente : les deux commandes Décrypter rendent
UNAVAILABLE avant toute transaction ; les autres commandes sont
inchangées. Un résultat AUTHORIZED de ces commandes n'autorise aucun appel
externe : seul un LeaseGranted (capacité rendue après COMMIT confirmé) le
permettra à PR 9-B2. Un blocage durable est une vue normale, committée
(jamais une sentinelle de ROLLBACK).
"""
import logging
from dataclasses import dataclass

import sqlalchemy as sa

from core.auth_f2 import sessions
from core.auth_f2.keys import PURPOSES, Keyring
from core.auth_f2.txn import pg_clock, transaction
from core.authz_f2 import _adapters
from core.authz_f2.commands import GENERATION_LOCK
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.portfolio_fingerprint import PortfolioKeyring
from core.authz_f2.principal import AuthorizedContext, AuthorizedPrincipal

logger = logging.getLogger("oryx.authz_f2")

AUTHORIZED = "authorized"
SESSION_INVALID = "session_invalid"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
REVISION_CONFLICT = "revision_conflict"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
STATUSES = (AUTHORIZED, SESSION_INVALID, NOT_FOUND, UNAVAILABLE, REVISION_CONFLICT, IDEMPOTENCY_CONFLICT)

# Points d'interruption des tests (aucun en production).
HOOK_POINTS = ("prevalidated", "locked", "authorized", "position_written", "receipt_ready", "receipt_written",
               "executed")


class AuthorizationConfigError(ValueError):
    """Runner impossible à construire : aucune autorisation ne sera rendue."""


class UnknownCommand(TypeError):
    """Commande hors du registre fermé (ou sous-classe) : refus avant toute transaction."""


@dataclass(frozen=True, slots=True)
class AuthorizationOutcome:
    status: str
    value: object = None
    principal: object = None


class _Refused(Exception):
    """Sortie de la transaction de phase B par ROLLBACK, sans exécution."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


_INCOHERENT = object()


class AuthorizedTransactionRunner:
    """engine : moteur SQLAlchemy PostgreSQL ; keyring : trousseau F2 (le
    même que le moteur F2) ; portfolio_keyring : trousseau portefeuille
    dédié (R1 PR 7, facultatif) ; decryptage_keyring : trousseau Décrypter
    dédié (R1 PR 9-B1, facultatif) ; clock : horloge PostgreSQL (pg_clock),
    remplaçable par les tests de frontières ; fault_hook : points
    d'interruption des tests (HOOK_POINTS)."""

    __slots__ = ("_engine", "_keys", "_portfolio", "_decryptage", "_clock", "_hook")

    def __init__(self, engine, keyring, *, portfolio_keyring=None, decryptage_keyring=None, clock=pg_clock,
                 fault_hook=None):
        if not isinstance(engine, sa.engine.Engine) or engine.dialect.name != "postgresql":
            raise AuthorizationConfigError("moteur PostgreSQL attendu")
        if not isinstance(keyring, Keyring):
            raise AuthorizationConfigError("trousseau F2 attendu")
        if portfolio_keyring is not None and type(portfolio_keyring) is not PortfolioKeyring:
            raise AuthorizationConfigError("trousseau portefeuille attendu")
        if decryptage_keyring is not None:
            if type(decryptage_keyring) is not DecryptageKeyring:
                raise AuthorizationConfigError("trousseau Décrypter attendu")
            # Indépendance des trousseaux : aucun repli, aucune clé partagée.
            for purpose in PURPOSES:
                for version in keyring.versions(purpose):
                    if decryptage_keyring.has_key_value(keyring.key(purpose, version)):
                        raise AuthorizationConfigError("clé Décrypter identique à une clé F2")
            if portfolio_keyring is not None and any(
                    portfolio_keyring.has_key_value(decryptage_keyring.key(v)) for v in decryptage_keyring.versions()):
                raise AuthorizationConfigError("clé Décrypter identique à une clé portefeuille")
        if not callable(clock) or (fault_hook is not None and not callable(fault_hook)):
            raise AuthorizationConfigError("horloge et point d'interruption appelables attendus")
        self._engine = engine
        self._keys = keyring
        self._portfolio = portfolio_keyring
        self._decryptage = decryptage_keyring
        self._clock = clock
        self._hook = fault_hook or (lambda point: None)

    def __repr__(self) -> str:
        return "AuthorizedTransactionRunner()"

    def __reduce__(self):
        raise TypeError("AuthorizedTransactionRunner n'est pas sérialisable")

    def run(self, credential, command) -> AuthorizationOutcome:
        spec = _adapters.REGISTRY.get(type(command))
        if spec is None:
            raise UnknownCommand("commande hors du registre fermé")
        name = type(command).__name__
        if spec.keyed and spec.decryptage_keyed:  # jamais deux familles de clés (défense : Spec le refuse déjà)
            logger.error("authz_f2 %s status=%s alert=key_families_conflict", name, UNAVAILABLE)
            return AuthorizationOutcome(UNAVAILABLE)
        if spec.keyed and self._portfolio is None:
            logger.warning("authz_f2 %s status=%s error=portfolio_keyring_missing", name, UNAVAILABLE)
            return AuthorizationOutcome(UNAVAILABLE)
        if spec.decryptage_keyed and self._decryptage is None:
            logger.warning("authz_f2 %s status=%s error=decryptage_keyring_missing", name, UNAVAILABLE)
            return AuthorizationOutcome(UNAVAILABLE)
        try:
            outcome = self._run(credential, command, spec)
        except Exception as exc:  # refus fermé : aucun détail, aucun secret
            alert = getattr(exc, "alert", None) if isinstance(exc, _adapters.AdapterInvariantBroken) else None
            if alert is not None:
                logger.error("authz_f2 %s status=%s alert=%s", name, UNAVAILABLE, alert)
            else:
                logger.warning("authz_f2 %s status=%s error=%s", name, UNAVAILABLE, type(exc).__name__)
            return AuthorizationOutcome(UNAVAILABLE)
        logger.info("authz_f2 %s status=%s", name, outcome.status)
        return outcome

    # ------------------------------------------------------------------

    def _valid(self, locked, at) -> bool:
        """Validateur F2 (core.auth_f2.sessions) + livraison confirmée par
        /session/resolve : jamais de confirmation implicite ici."""
        return sessions.session_valid(self._keys, locked, at) and locked[1]["delivery_status"] == "confirmed"

    def _prevalidate(self, credential):
        """Phase A : candidate_user_id ou None ; aucune écriture, aucun verrou."""
        with transaction(self._engine) as conn:
            locked = sessions.read_session(conn, credential)
            if locked is None or not self._valid(locked, self._clock(conn)):
                return None
            return locked[0]["user_id"]

    def _decide(self, profile_locked: bool, locked, at, candidate_user_id):
        """Décision à T_authorization sur des lignes déjà verrouillées."""
        if not self._valid(locked, at):
            return None
        session, generation, _ = locked
        if session["user_id"] != candidate_user_id or not profile_locked:
            return _INCOHERENT  # session valide, profil incohérent : 503, rien n'est créé
        return AuthorizedPrincipal(session["user_id"], session["id"], generation["id"], session["auth_method"])

    def _run(self, credential, command, spec) -> AuthorizationOutcome:
        candidate_user_id = self._prevalidate(credential)
        if candidate_user_id is None:
            return AuthorizationOutcome(SESSION_INVALID)
        self._hook("prevalidated")
        try:
            with transaction(self._engine) as conn:
                if spec.plan.requires_read_committed:
                    _adapters.require_read_committed(conn, spec.plan)  # isolation réellement active, avant tout verrou
                profile_locked = _adapters.lock_profile(conn, candidate_user_id, spec.plan)
                locked = sessions.lock_session(conn, credential, generation_lock=GENERATION_LOCK)
                business = spec.lock(conn, candidate_user_id, command) if profile_locked else None
                self._hook("locked")
                at = self._clock(conn)  # T_authorization : après tous les verrous de la décision
                principal = self._decide(profile_locked, locked, at, candidate_user_id)
                if principal is None:
                    raise _Refused(SESSION_INVALID)
                if principal is _INCOHERENT:
                    raise _Refused(UNAVAILABLE)
                if type(business) not in spec.states:  # état verrouillé hors du registre fermé
                    raise _adapters.AdapterInvariantBroken("état verrouillé hors contrat")
                context = AuthorizedContext(principal, at)
                self._hook("authorized")
                if spec.keyed:
                    value = spec.execute(conn, context, command, business, self._portfolio, self._hook)
                elif spec.decryptage_keyed:
                    value = spec.execute(conn, context, command, business, self._decryptage)
                else:
                    value = spec.execute(conn, context, command, business)
                if value is _adapters.ABSENT:
                    raise _Refused(NOT_FOUND)  # après la décision ; rien n'a été écrit
                if value is _adapters.CONFLICT:
                    raise _Refused(REVISION_CONFLICT)  # après la décision ; rien n'a été écrit
                if value is _adapters.IDEMPOTENCY_CONFLICT:
                    raise _Refused(IDEMPOTENCY_CONFLICT)  # après la décision ; rien n'a été écrit
                self._hook("executed")
        except _Refused as refused:
            return AuthorizationOutcome(refused.status)
        # Rendu seulement après un COMMIT confirmé.
        return AuthorizationOutcome(AUTHORIZED, value, principal)
