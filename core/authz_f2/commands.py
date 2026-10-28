"""Commandes typées de la frontière d'autorisation (BF1) et plans de verrous (BF2).

Une commande est une DONNÉE immuable, jamais du code : le runner
(core.authz_f2.runner) n'accepte que les types exacts du registre fermé
core.authz_f2._adapters.REGISTRY, chacun associé à un plan de verrouillage
et à des adaptateurs SQL privés audités. Aucune commande ne porte de
user_id : le propriétaire faisant autorité est toujours
AuthorizedPrincipal.user_id. Les identifiants de ressource (position_id,
ticker, assistant_turn_id, conversation_key) ne sont que des filtres
SUPPLÉMENTAIRES, toujours combinés à ce user_id.

Plans (ordre global F2 : ... -> users -> ... -> session -> génération ;
les tables métiers, absentes de l'ordre F2, viennent après) :

- READ           : users FOR KEY SHARE -> oryx_sessions FOR SHARE ->
                   oryx_session_generations FOR SHARE ; aucun verrou métier ;
- PROFILE_MUTATION (R1 PR 6) : users FOR UPDATE -> oryx_sessions FOR SHARE
                   -> oryx_session_generations FOR SHARE ; AUCUN verrou
                   métier (le profil est la ligne users elle-même) ;
- PORTFOLIO_MUTATION (R1 PR 7) : users FOR UPDATE -> oryx_sessions FOR
                   SHARE -> oryx_session_generations FOR SHARE -> reçu
                   du triplet (user_id, operation_type, operation_id) FOR
                   UPDATE -> si aucun reçu, position propre FOR UPDATE
                   (PUT / DELETE ; CREATE n'en verrouille aucune). Exige
                   READ COMMITTED, vérifié au début de la phase B.

PR 5 livrait des lectures et UNE mutation de démonstration interne
(UpdatePositionQuantity, sans CAS) : R1 PR 7 la RETIRE du registre fermé
(le runner la refuse : UnknownCommand) et retire avec elle le plan
USER_MUTATION, qu'elle seule utilisait. R1 PR 6 ajoute la mutation
versionnée du niveau déclaré (UpdateProfileLevel, CAS sur
users.level_revision) et la révision dans ProfileView. Le niveau déclaré
est une préférence de personnalisation, jamais un stade pédagogique.

R1 PR 7 : trois commandes de portefeuille versionnées et idempotentes
(CreatePortfolioPosition, ReplacePortfolioPosition,
DeletePortfolioPosition), données gelées sans user_id, dont les valeurs
financières sont des chaînes décimales canoniques validées
(core.authz_f2.portfolio_values) ; PositionView porte la révision, les
valeurs binary64-decimal-v1 et le diagnostic de lecture. Thèses,
Décrypter et ACK relèvent de PR 8 et PR 9.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from core.authz_f2 import portfolio_values as pv

MAX_POSITION_ID = 2**31 - 1
TICKER_MAX_CHARS = 32
CONVERSATION_KEY_MAX_CHARS = 200
# R1 PR 6 : niveaux déclarés admis et borne de users.level_revision (BIGINT).
LEVELS = ("debutant", "avance")
MAX_LEVEL_REVISION = 2**63 - 1
# R1 PR 7 : même borne pour portfolio_positions.position_revision (BIGINT).
MAX_POSITION_REVISION = MAX_LEVEL_REVISION
# Révision transportée en texte : "0" ou sans zéro initial, chiffres ASCII.
REVISION_TEXT_RE = re.compile(r"0|[1-9][0-9]{0,18}", re.ASCII)


@dataclass(frozen=True, slots=True)
class LockPlan:
    name: str
    users_lock: str  # mode du verrou de rang users, pris EN PREMIER en phase B
    business_locks: tuple  # tables métiers verrouillées après la génération, dans cet ordre
    # R1 PR 7 : isolation READ COMMITTED vérifiée au début de la phase B
    # (refus fermé sinon) ; les plans antérieurs ne la vérifient pas.
    requires_read_committed: bool = False


READ = LockPlan("read", "FOR KEY SHARE", ())
PROFILE_MUTATION = LockPlan("profile_mutation", "FOR UPDATE", ())
PORTFOLIO_MUTATION = LockPlan("portfolio_mutation", "FOR UPDATE",
                              ("portfolio_write_idempotency", "portfolio_positions"), True)
PLANS = (READ, PROFILE_MUTATION, PORTFOLIO_MUTATION)
SESSION_LOCK = "FOR SHARE"
GENERATION_LOCK = "FOR SHARE"


class Command:
    """Base des commandes du registre fermé (données pures)."""

    __slots__ = ()


def _position_id(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_POSITION_ID:
        raise ValueError("position_id : entier strictement positif attendu")
    return value


def _bounded_text(value, max_chars: int, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars or value != value.strip():
        raise ValueError(f"{label} : chaîne non vide, sans blanc de bord, {max_chars} caractères au plus")
    return value


def valid_level(value) -> bool:
    return type(value) is str and value in LEVELS


def valid_revision(value) -> bool:
    """Entier Python EXACT (ni booléen, ni sous-classe), 0 à 2^63 - 1."""
    return type(value) is int and 0 <= value <= MAX_LEVEL_REVISION


def replace_revisions(expected: int) -> tuple:
    """Révisions possibles après un remplacement réussi à la révision
    attendue : inchangée (aucune modification) ou suivante (modification
    effective ; aucune à la révision maximale)."""
    return (expected,) if expected == MAX_POSITION_REVISION else (expected, expected + 1)


def valid_position_id(value) -> bool:
    return type(value) is int and 1 <= value <= MAX_POSITION_ID


def next_revision(revision: int) -> int:
    """Révision suivante ; jamais de débordement, de recyclage ni de remise
    à zéro : la révision maximale n'a pas de suivante."""
    if not valid_revision(revision) or revision == MAX_LEVEL_REVISION:
        raise ValueError("level_revision : aucune révision suivante")
    return revision + 1


@dataclass(frozen=True, slots=True)
class ReadProfile(Command):
    pass


@dataclass(frozen=True, slots=True)
class ListPositions(Command):
    pass


@dataclass(frozen=True, slots=True)
class ReadPosition(Command):
    position_id: int

    def __post_init__(self):
        _position_id(self.position_id)


@dataclass(frozen=True, slots=True)
class ListTheses(Command):
    pass


@dataclass(frozen=True, slots=True)
class ListThesesForTicker(Command):
    ticker: str

    def __post_init__(self):
        _bounded_text(self.ticker, TICKER_MAX_CHARS, "ticker")


@dataclass(frozen=True, slots=True)
class ReadDelivery(Command):
    assistant_turn_id: uuid.UUID

    def __post_init__(self):
        if not isinstance(self.assistant_turn_id, uuid.UUID):
            raise ValueError("assistant_turn_id : uuid.UUID attendu")


@dataclass(frozen=True, slots=True)
class ListConversationDeliveries(Command):
    conversation_key: str

    def __post_init__(self):
        _bounded_text(self.conversation_key, CONVERSATION_KEY_MAX_CHARS, "conversation_key")


@dataclass(frozen=True, slots=True)
class UpdatePositionQuantity(Command):
    """Ancienne mutation de DÉMONSTRATION de PR 5 (sans CAS). RETIRÉE du
    registre fermé par R1 PR 7 : conservée comme donnée inerte pour prouver
    que le runner la refuse (UnknownCommand, avant toute transaction)."""
    position_id: int
    quantity: float


@dataclass(frozen=True, slots=True)
class UpdateProfileLevel(Command):
    """R1 PR 6 : niveau déclaré si la révision courante vaut EXACTEMENT
    expected_revision (CAS). Révision périmée : conflit, rien n'est écrit ;
    niveau inchangé : succès sans écriture ; sinon niveau écrit et révision
    + 1 dans une seule instruction SQL. Aucun user_id : le profil est celui
    du principal autorisé."""
    level: str
    expected_revision: int

    def __post_init__(self):
        if not valid_level(self.level):
            raise ValueError("level : debutant ou avance attendu")
        if not valid_revision(self.expected_revision):
            raise ValueError("expected_revision : entier de 0 à 2^63 - 1 attendu")


def _operation_id(value) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise ValueError("operation_id : uuid.UUID attendu")
    return value


def _business_values(command) -> None:
    """Cinq champs métier d'une nouvelle écriture (chaînes canoniques)."""
    try:
        for check, value in zip(pv.CHECKS, (command.ticker, command.quantity, command.purchase_price,
                                            command.target_percent, command.envelope)):
            check(value)
    except pv.ValueRejected as exc:
        raise ValueError(str(exc)) from None


def _expected_revision(value) -> int:
    if not valid_revision(value):
        raise ValueError("expected_revision : entier de 0 à 2^63 - 1 attendu")
    return value


@dataclass(frozen=True, slots=True)
class CreatePortfolioPosition(Command):
    """R1 PR 7 : crée une position du principal (révision 0) et son reçu
    d'idempotence, atomiquement. Aucun user_id, aucun position_id."""
    operation_id: uuid.UUID
    ticker: str
    quantity: str
    purchase_price: object  # chaîne décimale canonique ou None
    target_percent: object  # chaîne décimale canonique ou None
    envelope: object        # "PEA", "CTO" ou None

    def __post_init__(self):
        _operation_id(self.operation_id)
        _business_values(self)


@dataclass(frozen=True, slots=True)
class ReplacePortfolioPosition(Command):
    """R1 PR 7 : remplacement COMPLET des cinq champs métier si la révision
    courante vaut EXACTEMENT expected_revision (CAS) ; valeurs identiques :
    aucun UPDATE (reçu seulement)."""
    operation_id: uuid.UUID
    position_id: int
    expected_revision: int
    ticker: str
    quantity: str
    purchase_price: object
    target_percent: object
    envelope: object

    def __post_init__(self):
        _operation_id(self.operation_id)
        _position_id(self.position_id)
        _expected_revision(self.expected_revision)
        _business_values(self)


@dataclass(frozen=True, slots=True)
class DeletePortfolioPosition(Command):
    """R1 PR 7 : suppression si la révision courante vaut EXACTEMENT
    expected_revision (même maximale) ; les anciens champs financiers n'ont
    pas à être valides."""
    operation_id: uuid.UUID
    position_id: int
    expected_revision: int

    def __post_init__(self):
        _operation_id(self.operation_id)
        _position_id(self.position_id)
        _expected_revision(self.expected_revision)


# --------------------------------------------------------------------------
# Vues rendues (aucune métadonnée privée, aucun secret)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProfileView:
    """level et level_revision lus ENSEMBLE (une requête) ; une vue
    incohérente ne peut pas exister (refus fermé, jamais de correction)."""
    user_id: str
    level: str
    level_revision: int

    def __post_init__(self):
        if not isinstance(self.user_id, str) or not self.user_id:
            raise ValueError("profil : user_id serveur attendu")
        if not valid_level(self.level) or not valid_revision(self.level_revision):
            raise ValueError("profil : niveau ou révision hors contrat")


@dataclass(frozen=True, slots=True)
class PositionView:
    """R1 PR 7 : vue publique fermée d'une position (aucune date). Nombres
    rendus en binary64-decimal-v1 (chaînes, valeurs spéciales comprises) ;
    diagnostic calculé à la lecture, jamais enregistré ni corrigé. Une vue
    incohérente ne peut pas être construite."""
    position_id: int
    position_revision: int
    ticker: str
    quantity: str
    purchase_price: object  # chaîne binary64-decimal-v1 ou None
    target_percent: object  # chaîne binary64-decimal-v1 ou None
    envelope: object        # chaîne historique ou None
    data_status: str
    validation_issues: tuple

    def __post_init__(self):
        if not valid_position_id(self.position_id) or not valid_revision(self.position_revision):
            raise ValueError("position : identifiant ou révision hors contrat")
        if type(self.ticker) is not str or not pv.is_rendered_number(self.quantity):
            raise ValueError("position : ticker ou quantité hors contrat")
        for optional in (self.purchase_price, self.target_percent):
            if optional is not None and not pv.is_rendered_number(optional):
                raise ValueError("position : nombre hors contrat")
        if self.envelope is not None and type(self.envelope) is not str:
            raise ValueError("position : enveloppe hors contrat")
        issues = pv.diagnose(self.ticker, self.quantity, self.purchase_price, self.target_percent, self.envelope)
        status = pv.LEGACY_INVALID if issues else pv.VALID
        if type(self.validation_issues) is not tuple or self.validation_issues != issues \
                or self.data_status != status:
            raise ValueError("position : diagnostic incohérent")


def position_view(position_id, position_revision, ticker, quantity, purchase_price, target_percent,
                  envelope) -> PositionView:
    """Vue d'une ligne lue (floats binary64 exacts) ; diagnostic calculé ici."""
    rendered = [None if value is None else pv.render_binary64_decimal_v1(value)
                for value in (quantity, purchase_price, target_percent)]
    if rendered[0] is None:
        raise ValueError("position : quantité absente")
    issues = pv.diagnose(ticker, *rendered, envelope)
    return PositionView(position_id, position_revision, ticker, *rendered, envelope,
                        pv.LEGACY_INVALID if issues else pv.VALID, issues)


@dataclass(frozen=True, slots=True)
class ThesisView:
    thesis_id: int
    ticker: str
    thesis_text: str
    created_at: object


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """Sans private_metadata, request_fingerprint ni response_payload."""
    assistant_turn_id: uuid.UUID
    conversation_key: str
    surface: str
    status: str
    generated_at: datetime
