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
"""
import logging
from dataclasses import dataclass

import sqlalchemy as sa

from core.auth_f2 import sessions
from core.auth_f2.keys import Keyring
from core.auth_f2.txn import pg_clock, transaction
from core.authz_f2 import _adapters
from core.authz_f2.commands import GENERATION_LOCK
from core.authz_f2.principal import AuthorizedContext, AuthorizedPrincipal

logger = logging.getLogger("oryx.authz_f2")

AUTHORIZED = "authorized"
SESSION_INVALID = "session_invalid"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
REVISION_CONFLICT = "revision_conflict"
STATUSES = (AUTHORIZED, SESSION_INVALID, NOT_FOUND, UNAVAILABLE, REVISION_CONFLICT)

# Points d'interruption des tests (aucun en production).
HOOK_POINTS = ("prevalidated", "locked", "authorized", "executed")


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
    même que le moteur F2) ; clock : horloge PostgreSQL (pg_clock),
    remplaçable par les tests de frontières ; fault_hook : points
    d'interruption des tests (HOOK_POINTS)."""

    __slots__ = ("_engine", "_keys", "_clock", "_hook")

    def __init__(self, engine, keyring, *, clock=pg_clock, fault_hook=None):
        if not isinstance(engine, sa.engine.Engine) or engine.dialect.name != "postgresql":
            raise AuthorizationConfigError("moteur PostgreSQL attendu")
        if not isinstance(keyring, Keyring):
            raise AuthorizationConfigError("trousseau F2 attendu")
        if not callable(clock) or (fault_hook is not None and not callable(fault_hook)):
            raise AuthorizationConfigError("horloge et point d'interruption appelables attendus")
        self._engine = engine
        self._keys = keyring
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
        try:
            outcome = self._run(credential, command, spec)
        except Exception as exc:  # refus fermé : aucun détail, aucun secret
            if isinstance(exc, _adapters.RevisionExhausted):
                logger.error("authz_f2 %s status=%s alert=level_revision_exhausted", name, UNAVAILABLE)
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
                profile_locked = _adapters.lock_profile(conn, candidate_user_id, spec.plan)
                locked = sessions.lock_session(conn, credential, generation_lock=GENERATION_LOCK)
                business = spec.lock(conn, candidate_user_id, command) if profile_locked else _adapters.ABSENT
                self._hook("locked")
                at = self._clock(conn)  # T_authorization : après tous les verrous de la décision
                principal = self._decide(profile_locked, locked, at, candidate_user_id)
                if principal is None:
                    raise _Refused(SESSION_INVALID)
                if principal is _INCOHERENT:
                    raise _Refused(UNAVAILABLE)
                if business is _adapters.ABSENT:
                    raise _Refused(NOT_FOUND)
                context = AuthorizedContext(principal, at)
                self._hook("authorized")
                value = spec.execute(conn, context, command, business)
                if value is _adapters.ABSENT:
                    raise _Refused(NOT_FOUND)
                if value is _adapters.CONFLICT:
                    raise _Refused(REVISION_CONFLICT)  # après la décision ; rien n'a été écrit
                self._hook("executed")
        except _Refused as refused:
            return AuthorizationOutcome(refused.status)
        # Rendu seulement après un COMMIT confirmé.
        return AuthorizationOutcome(AUTHORIZED, value, principal)
