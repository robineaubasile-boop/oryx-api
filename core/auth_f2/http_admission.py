"""Admission HTTP F2 (R1 PR 4, C.4.5 §2.2 et §2.3) — DORMANTE.

Frontière anti-abus PROPRE aux sept routes de PR 4, indépendante du moteur
métier (core.auth_f2.service) : la couche HTTP l'appelle APRÈS tous les
contrôles de transport et AVANT le moteur, pour toute requête, y compris
quand l'opération ou l'invitation désignée n'existe pas et quand le
moteur OTP refuserait ensuite d'emblée.

Budgets (fenêtres glissantes ]t - W, t], horloge PostgreSQL lue après les
verrous) sur le sujet IP de l'appelant, toutes versions de K_abuse
comptées (core.auth_f2.abuse) :

  - commun : 60 probes / 5 min, toutes actions PR 4 confondues ;
  - invitation_begin 20 / 1 h ; invitation_complete, invitation_resume et
    email_link_result 30 / 5 min (filtre http_action).

Protocole d'une admission :

  1. sujets IP manquants créés dans une transaction COURTE et committée
     (abuse.ensure_subjects) ;
  2. transaction d'admission : verrou consultatif PARTAGÉ de l'état de clé,
     sujets IP verrouillés (FOR UPDATE, ordre trié), heure PostgreSQL,
     budgets commun et propre à l'action, puis UN probe
     auth_http_probe_attempt (policy_version f2-pr4-http-v1, http_action)
     si et seulement si la demande est admise ;
  3. COMMIT ; admit() ne rend True qu'après un COMMIT confirmé.

Un refus de budget rend False : aucun probe, aucun marqueur. Une erreur
(état de clé absent ou incompatible, verrou ou instruction expirés,
écriture refusée, COMMIT en échec ou incertain) lève une exception :
l'appelant refuse (503) et n'appelle JAMAIS le moteur. Un probe
éventuellement committé reste compté ; aucune annulation rétroactive.

Aucun verrou n'est conservé au retour : la transaction métier qui suit est
distincte et n'est ouverte qu'après ce COMMIT. Aucun appel fournisseur ici.
"""
import logging

from core.auth_f2 import abuse
from core.auth_f2.abuse import IP, AbuseUnavailable, Scope, SubjectsMissing
from core.auth_f2.keys import Keyring
from core.auth_f2.normalize import InvalidClientIp, normalize_client_ip
from core.auth_f2.policy import (
    HTTP_ACTION_BUDGETS,
    HTTP_ACTIONS,
    HTTP_COMMON_LIMIT,
    HTTP_COMMON_WINDOW,
    HTTP_POLICY_VERSION,
)
from core.auth_f2.txn import pg_clock, transaction

logger = logging.getLogger("oryx.auth_f2.admission")

_RETRIES = 3


class AdmissionUnavailable(Exception):
    """Admission impossible (IP non normalisable) : refus fermé."""


class HttpAdmission:
    """engine : moteur SQLAlchemy PostgreSQL ; keyring : trousseau F2 (seul
    l'usage abuse est lu) ; clock : horloge PostgreSQL ; fault_hook : points
    d'interruption des tests de panne (aucun en production)."""

    def __init__(self, engine, keyring: Keyring, *, clock=pg_clock, fault_hook=None):
        if not isinstance(keyring, Keyring):
            raise TypeError("keyring : Keyring attendu")
        self._engine = engine
        self._keys = keyring
        self._clock = clock
        self._hook = fault_hook or (lambda point: None)

    def __repr__(self) -> str:
        return "HttpAdmission()"

    def admit(self, action: str, client_ip) -> bool:
        """True : probe enregistré ET committé ; False : budget épuisé, rien
        n'est écrit. Toute autre issue lève une exception."""
        if action not in HTTP_ACTIONS:
            raise ValueError("action HTTP inconnue")
        try:
            scope = Scope(IP, normalize_client_ip(client_ip))
        except InvalidClientIp:
            raise AdmissionUnavailable("IP client non normalisable") from None
        for _ in range(_RETRIES):
            abuse.ensure_subjects(self._engine, self._keys, [scope])
            try:
                with transaction(self._engine) as conn:
                    admitted = self._decide(conn, action, scope)
                    if admitted:
                        self._hook("admission.before_commit")
            except SubjectsMissing:
                continue
            logger.info("f2 admission action=%s admitted=%s", action, admitted)
            if admitted:
                self._hook("admission.committed")
            return admitted
        raise AbuseUnavailable("sujets anti-abus indisponibles")

    def _decide(self, conn, action: str, scope: Scope) -> bool:
        state = abuse.lock_key_state(conn, self._keys)
        self._hook("admission.state_locked")
        subjects = abuse.select_subjects(conn, self._keys, state, [scope], lock=True)
        at = self._clock(conn)  # après tous les verrous
        ids = subjects.ids(scope)
        if abuse.count_since(conn, ids, abuse.HTTP_PROBE, at - HTTP_COMMON_WINDOW) >= HTTP_COMMON_LIMIT:
            return False
        budget = HTTP_ACTION_BUDGETS.get(action)
        if budget is not None:
            limit, window = budget
            if abuse.count_since(conn, ids, abuse.HTTP_PROBE, at - window, http_action=action) >= limit:
                return False
        abuse.insert_event(conn, subject_id=subjects.recording_id(scope), kind=abuse.HTTP_PROBE, at=at,
                           policy_version=HTTP_POLICY_VERSION, http_action=action)
        return True
