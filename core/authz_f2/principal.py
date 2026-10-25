"""Identité serveur autorisée (C.5.2 §4.1).

AuthorizedPrincipal n'est construit que par le runner, à partir des lignes
oryx_sessions / oryx_session_generations verrouillées et jugées à
T_authorization. Il ne porte aucun secret et ne vient jamais du client
(sélecteur X-Oryx-Generation-Id non secret, user_id jamais accepté).

AuthorizedContext est ce que voient les adaptateurs après la décision : le
principal et l'instant T_authorization, rien d'autre (aucune connexion,
aucune méthode transactionnelle : BF1).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AuthorizedPrincipal:
    user_id: str
    session_id: uuid.UUID
    generation_id: uuid.UUID
    auth_method: str

    def __post_init__(self):
        if not isinstance(self.user_id, str) or not self.user_id:
            raise TypeError("principal : user_id serveur attendu")
        if not isinstance(self.session_id, uuid.UUID) or not isinstance(self.generation_id, uuid.UUID):
            raise TypeError("principal : identifiants de session UUID attendus")
        if not isinstance(self.auth_method, str):
            raise TypeError("principal : méthode d'authentification attendue")

    def __reduce__(self):
        raise TypeError("AuthorizedPrincipal n'est pas sérialisable")


@dataclass(frozen=True, slots=True)
class AuthorizedContext:
    principal: AuthorizedPrincipal
    authorized_at: datetime  # T_authorization : clock_timestamp() lu après les verrous de décision

    def __post_init__(self):
        if not isinstance(self.principal, AuthorizedPrincipal):
            raise TypeError("contexte : AuthorizedPrincipal attendu")
        if not isinstance(self.authorized_at, datetime) or self.authorized_at.tzinfo is None:
            raise TypeError("contexte : T_authorization (datetime avec fuseau) attendu")

    def __reduce__(self):
        raise TypeError("AuthorizedContext n'est pas sérialisable")
