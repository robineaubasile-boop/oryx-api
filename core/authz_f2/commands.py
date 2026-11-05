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
- DECRYPTAGE_MUTATION (R1 PR 9-A) : users FOR NO KEY UPDATE -> oryx_sessions
                   FOR SHARE -> oryx_session_generations FOR SHARE ; aucun
                   verrou métier préparatoire ; READ COMMITTED vérifié.
                   Aucune commande du registre de production ne l'utilise.

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

R1 PR 8 : trois lectures paginées ou individuelles sous le plan READ
inchangé (ListAnalysisOverviewPage, ListThesesPage, ReadThesis), données
gelées sans user_id dont les limites, filtres et bornes de curseur sont
prévalidés ; ListTheses et ListThesesForTicker sont conservées telles
quelles. Vues fermées et validées : AnalysisOverviewEntry / Page (synthèse
par ticker, états de PRÉSENTATION in_progress / thesis_available, jamais
des stades cognitifs), ThesisListEntry / ThesesPage (sans thesis_text) et
ThesisDetailView (texte intégral borné, provenance relationnelle). Une
AnalysisSession, une InvestmentThesis et une preuve pédagogique restent
trois notions distinctes ; « verified » n'est jamais une preuve de
compétence.

R1 PR 9-B1 : deux commandes Décrypter sous le plan DECRYPTAGE_MUTATION
(inchangé), dormantes (aucune route ne les invoque) :

- ReserveDecryptageTurn : entrées métier du tour (conversation_key,
  client_turn_id, ticker_input, question, context, last_method_id),
  admises structurellement à la construction (core.authz_f2.
  decryptage_fingerprint), jamais un propriétaire, un epoch, un jeton, une
  échéance, un compteur, une empreinte, une version de clé, un état ni un
  motif de blocage ;
- RecordDecryptageAttemptFailure : capacité serveur (reservation_id,
  lease_epoch, jeton de bail brut LeaseToken) et classification FERMÉE de
  l'échec. Aucune commande force_block / reset / unlock / override.

R1 PR 9-B2 : UNE lecture Décrypter sous le plan READ inchangé (aucun plan
nouveau, aucun verrou métier, aucune écriture), dormante :

- ReadDecryptagePreparationContext : capacité serveur du bail B1
  (reservation_id, lease_epoch, LeaseToken brut), entrées ORIGINALES du tour
  telles que réservées (revérifiées par le HMAC d'intention B1 enregistré,
  trousseau Décrypter du dispatch existant Spec.decryptage_keyed) et ticker
  RÉSOLU (core.authz_f2.decryptage_context.RESOLVED_TICKER_RE). Jamais un
  user_id, une Session, une connexion ni un callable. Résultat fermé :
  DecryptagePreparationView ou DecryptagePreparationRefused
  (core.authz_f2.decryptage_context).

R1 PR 9-C1-B : dix-huitième commande, sous le plan DECRYPTAGE_MUTATION
inchangé (aucun plan nouveau), dormante :

- FinalizeDecryptageCandidate : porte EXCLUSIVEMENT le candidat B2 immuable
  (core.authz_f2.decryptage_candidate.GeneratedDecryptageCandidate, type
  exact, invariants revérifiés à la construction : version, sous-objets,
  empreinte financière, PreparationSnapshot et son empreinte, payload,
  cohérence du bail). Jamais un user_id, une Session, une connexion, un
  rappel, du SQL, un statut ou un motif forcé, une décision cognitive ni une
  option de contrôle. Ces validations ne sont PAS une autorisation : la
  réservation propre, le HMAC d'intention, l'epoch, le jeton et l'échéance
  sont revérifiés sous verrou par les adaptateurs SQL ; le propriétaire
  reste AuthorizedContext.principal.

R1 PR 9-C2 : deux commandes (vingt au total), aucun plan nouveau, dormantes,
SANS trousseau Décrypter (aucune empreinte HMAC B1 n'est recalculée : C2 ne
possède ni les entrées de génération ni le jeton, et n'en a pas besoin pour
restituer une réponse finalisée) :

- ReadCanonicalDecryptageDelivery (plan READ) et
  AcknowledgeDecryptageDelivery (plan DECRYPTAGE_MUTATION) : champs EXACTS
  reservation_id (uuid.UUID), conversation_key (validateur B1 existant, sans
  normalisation) et assistant_turn_id (uuid.UUID). Jamais un user_id, un
  jeton, une clé, un candidat, des données de génération, un payload, un
  statut imposé, une décision cognitive, une Session, une connexion, du SQL
  ni un rappel ;
- CanonicalDecryptageDelivery : vue fermée du replay (octets UTF-8 exacts du
  texte JSONB public validé, repr sans contenu). L'ACK rend la vue
  historique DeliveryAcknowledged du pont (type exact réutilisé).
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_fingerprint as dfp
from core.authz_f2 import decryptage_reservation as dr
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
# R1 PR 9-A (A-01) : futures mutations Décrypter F2, jamais les lectures.
# users FOR NO KEY UPDATE sérialise deux mutations Décrypter d'un même
# compte, reste compatible avec le FOR KEY SHARE des contrôles de FK et
# entre en conflit avec FOR UPDATE (mutations de profil et de portefeuille).
# Aucun verrou métier préparatoire : les verrous d'exécution sont ceux des
# services métier, pris après T_authorization (core.authz_f2._decryptage_orm_bridge).
DECRYPTAGE_MUTATION = LockPlan("decryptage_mutation", "FOR NO KEY UPDATE", (), True)
PLANS = (READ, PROFILE_MUTATION, PORTFOLIO_MUTATION, DECRYPTAGE_MUTATION)
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


# --------------------------------------------------------------------------
# R1 PR 8 : lectures des analyses et des thèses (plan READ, aucune écriture)
# --------------------------------------------------------------------------

MAX_THESIS_ID = 2**31 - 1  # investment_theses.id (INTEGER)
PAGE_LIMIT_MAX = 50
TICKER_MAX_BYTES = 512  # tout ticker RESTITUÉ (octets UTF-8) ; au-delà : 503, jamais tronqué
THESIS_TEXT_MAX_BYTES = 131072  # texte brut restituable (le JSON final est revérifié ensuite)
# Borne de transport d'un ticker de curseur (le curseur encodé fait au plus
# 1024 caractères) : jamais la validation des nouveaux filtres.
CURSOR_TICKER_MAX_BYTES = 1024
# Étapes reconnues de construction_these (core.decryptage_progress, recopiées
# ici : aucun import applicatif ; égalité vérifiée par les tests).
ANALYSIS_STEPS = ("business", "moat", "chiffres", "valorisation", "risques", "swot_final")
IN_PROGRESS = "in_progress"
THESIS_AVAILABLE = "thesis_available"
OVERVIEW_STATES = (IN_PROGRESS, THESIS_AVAILABLE)  # présentation, jamais un stade cognitif
MULTIPLE_ACTIVE_SESSIONS = "multiple_active_sessions"
INVALID_CURRENT_STEP = "invalid_current_step"
DIAGNOSTICS = (MULTIPLE_ACTIVE_SESSIONS, INVALID_CURRENT_STEP)  # ordre fermé
VERIFIED = "verified"
UNVERIFIED = "unverified"


def valid_page_limit(value) -> bool:
    return type(value) is int and 1 <= value <= PAGE_LIMIT_MAX


def page_fetch_size(limit: int) -> int:
    """limit + 1 lignes lues : la ligne supplémentaire dit seulement qu'une
    page suivante existe ; elle n'est jamais rendue ni utilisée pour le
    curseur."""
    if not valid_page_limit(limit):
        raise ValueError("limit : entier de 1 à 50 attendu")
    return limit + 1


def valid_thesis_id(value) -> bool:
    return type(value) is int and 1 <= value <= MAX_THESIS_ID


def utf8_length(value):
    """Longueur UTF-8 d'une chaîne, ou None si elle n'est pas encodable
    (substitut isolé)."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _storable_text(value) -> bool:
    """Chaîne Python exacte qu'une colonne text PostgreSQL peut contenir
    (UTF-8 encodable, sans NUL)."""
    return type(value) is str and "\x00" not in value and utf8_length(value) is not None


def valid_ticker_filter(value) -> bool:
    """Filtre ticker d'une nouvelle requête : non vide, 32 caractères au
    plus, sans blanc de bord ; jamais corrigé (ni upper ni strip)."""
    return _storable_text(value) and 0 < len(value) <= TICKER_MAX_CHARS and value == value.strip()


def valid_cursor_ticker(value) -> bool:
    """Dernier ticker d'un curseur de synthèse : chaîne historique EXACTE,
    éventuellement vide, soumise aux seules bornes de transport."""
    return _storable_text(value) and utf8_length(value) <= CURSOR_TICKER_MAX_BYTES


def restorable_ticker(value) -> bool:
    return type(value) is str and utf8_length(value) is not None and utf8_length(value) <= TICKER_MAX_BYTES


def historical_timestamp(value) -> bool:
    """investment_theses.created_at : TIMESTAMP WITHOUT TIME ZONE (UTC
    historique), datetime naïf exact."""
    return type(value) is datetime and value.tzinfo is None


def session_timestamp(value) -> bool:
    """analysis_sessions.* : TIMESTAMPTZ, datetime avec fuseau."""
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def thesis_precedes(created_at, thesis_id, other_created_at, other_id) -> bool:
    """Ordre total des thèses : created_at DESC NULLS LAST, id DESC. True si
    (created_at, thesis_id) vient STRICTEMENT avant (other_created_at,
    other_id)."""
    if created_at is None:
        return other_created_at is None and thesis_id > other_id
    if other_created_at is None:
        return True
    return created_at > other_created_at or (created_at == other_created_at and thesis_id > other_id)


def overview_diagnostics(active_sessions, current_step) -> tuple:
    """Diagnostics fermés d'une session active retenue, dans l'ordre fermé ;
    calculés à la lecture, jamais enregistrés ni réparés."""
    if type(active_sessions) is not int or active_sessions < 1:
        raise ValueError("nombre de sessions actives hors contrat")
    if current_step is not None and current_step not in ANALYSIS_STEPS:
        raise ValueError("étape hors contrat")
    present = {MULTIPLE_ACTIVE_SESSIONS: active_sessions > 1, INVALID_CURRENT_STEP: current_step is None}
    return tuple(code for code in DIAGNOSTICS if present[code])


@dataclass(frozen=True, slots=True)
class ListAnalysisOverviewPage(Command):
    """Une page de la synthèse par ticker du principal (une instruction SQL).
    after_ticker : None (première page) ou dernier ticker rendu, chaîne
    historique exacte (éventuellement vide)."""
    limit: int
    after_ticker: object

    def __post_init__(self):
        if not valid_page_limit(self.limit):
            raise ValueError("limit : entier de 1 à 50 attendu")
        if self.after_ticker is not None and not valid_cursor_ticker(self.after_ticker):
            raise ValueError("after_ticker : chaîne exacte attendue")


@dataclass(frozen=True, slots=True)
class ListThesesPage(Command):
    """Une page des InvestmentThesis du principal (created_at DESC NULLS
    LAST, id DESC). ticker : filtre exact ou None. Curseur : after_thesis_id
    None (première page) ou dernier identifiant rendu, avec after_created_at
    (datetime naïf exact ou None, alors dernière date nulle)."""
    limit: int
    ticker: object
    after_thesis_id: object
    after_created_at: object

    def __post_init__(self):
        if not valid_page_limit(self.limit):
            raise ValueError("limit : entier de 1 à 50 attendu")
        if self.ticker is not None and not valid_ticker_filter(self.ticker):
            raise ValueError("ticker : filtre exact attendu")
        if self.after_thesis_id is None:
            if self.after_created_at is not None:
                raise ValueError("curseur : date sans identifiant")
        elif not valid_thesis_id(self.after_thesis_id):
            raise ValueError("after_thesis_id : entier de 1 à 2^31 - 1 attendu")
        if self.after_created_at is not None and not historical_timestamp(self.after_created_at):
            raise ValueError("after_created_at : TIMESTAMP sans fuseau attendu")


@dataclass(frozen=True, slots=True)
class ReadThesis(Command):
    """Une InvestmentThesis du principal (id ET user_id) ; étrangère ou
    inexistante : même ABSENT."""
    thesis_id: int

    def __post_init__(self):
        if not valid_thesis_id(self.thesis_id):
            raise ValueError("thesis_id : entier de 1 à 2^31 - 1 attendu")


@dataclass(frozen=True, slots=True)
class AnalysisOverviewEntry:
    """Entrée fermée de la synthèse : session active retenue (prioritaire)
    ou, sans session active, dernière thèse enregistrée. Une vue incohérente
    ne peut pas être construite."""
    ticker: str
    state: str
    analysis_session_id: object  # uuid.UUID (in_progress) ou None
    current_step: object         # étape reconnue ou None
    updated_at: object           # TIMESTAMPTZ de la session, created_at naïf de la thèse, ou None
    latest_thesis_id: object     # entier (thesis_available) ou None
    diagnostics: tuple

    def __post_init__(self):
        if not restorable_ticker(self.ticker):
            raise ValueError("synthèse : ticker non restituable")
        if type(self.diagnostics) is not tuple or any(type(d) is not str for d in self.diagnostics):
            raise ValueError("synthèse : diagnostics hors contrat")
        if self.state == IN_PROGRESS:
            if (type(self.analysis_session_id) is not uuid.UUID or self.latest_thesis_id is not None
                    or not session_timestamp(self.updated_at)
                    or (self.current_step is not None and self.current_step not in ANALYSIS_STEPS)):
                raise ValueError("synthèse : session active hors contrat")
            ordered = tuple(d for d in DIAGNOSTICS if d in self.diagnostics)
            if self.diagnostics != ordered or (INVALID_CURRENT_STEP in self.diagnostics) != (self.current_step is None):
                raise ValueError("synthèse : diagnostics incohérents")
        elif self.state == THESIS_AVAILABLE:
            if (self.analysis_session_id is not None or self.current_step is not None or self.diagnostics != ()
                    or not valid_thesis_id(self.latest_thesis_id)
                    or (self.updated_at is not None and not historical_timestamp(self.updated_at))):
                raise ValueError("synthèse : thèse hors contrat")
        else:
            raise ValueError("synthèse : état hors contrat")


@dataclass(frozen=True, slots=True)
class AnalysisOverviewPage:
    """entries : au plus limit entrées, tickers strictement croissants
    (collation "C" : ordre des points de code) ; has_more : une ligne
    supplémentaire a été lue."""
    entries: tuple
    has_more: bool

    def __post_init__(self):
        if type(self.entries) is not tuple or type(self.has_more) is not bool \
                or any(type(e) is not AnalysisOverviewEntry for e in self.entries):
            raise ValueError("synthèse : page hors contrat")
        tickers = [e.ticker for e in self.entries]
        if any(a >= b for a, b in zip(tickers, tickers[1:])):
            raise ValueError("synthèse : ordre des tickers rompu")


@dataclass(frozen=True, slots=True)
class ThesisListEntry:
    """Entrée de l'historique : jamais de thesis_text ni d'extrait."""
    thesis_id: int
    ticker: str
    created_at: object  # datetime naïf (UTC historique) ou None
    provenance_status: str

    def __post_init__(self):
        if not valid_thesis_id(self.thesis_id) or not restorable_ticker(self.ticker):
            raise ValueError("thèse : identifiant ou ticker hors contrat")
        if self.created_at is not None and not historical_timestamp(self.created_at):
            raise ValueError("thèse : date hors contrat")
        if self.provenance_status not in (VERIFIED, UNVERIFIED) or type(self.provenance_status) is not str:
            raise ValueError("thèse : provenance hors contrat")


@dataclass(frozen=True, slots=True)
class ThesesPage:
    """entries : au plus limit thèses, dans l'ordre total created_at DESC
    NULLS LAST, id DESC ; has_more : une ligne supplémentaire a été lue."""
    entries: tuple
    has_more: bool

    def __post_init__(self):
        if type(self.entries) is not tuple or type(self.has_more) is not bool \
                or any(type(e) is not ThesisListEntry for e in self.entries):
            raise ValueError("thèses : page hors contrat")
        for a, b in zip(self.entries, self.entries[1:]):
            if not thesis_precedes(a.created_at, a.thesis_id, b.created_at, b.thesis_id):
                raise ValueError("thèses : ordre rompu")


@dataclass(frozen=True, slots=True)
class ThesisDetailView:
    """Thèse complète : texte intégral (jamais tronqué), provenance
    relationnelle. analysis_session_id n'est porté que si le rattachement
    est VÉRIFIÉ (même id, même utilisateur, même ticker) ; sinon None, sans
    raison ni identifiant incohérent."""
    thesis_id: int
    ticker: str
    thesis_text: str
    created_at: object
    analysis_session_id: object  # uuid.UUID vérifié ou None

    def __post_init__(self):
        if not valid_thesis_id(self.thesis_id) or not restorable_ticker(self.ticker):
            raise ValueError("thèse : identifiant ou ticker hors contrat")
        length = utf8_length(self.thesis_text) if type(self.thesis_text) is str else None
        if length is None or length > THESIS_TEXT_MAX_BYTES:
            raise ValueError("thèse : texte non restituable")
        if self.created_at is not None and not historical_timestamp(self.created_at):
            raise ValueError("thèse : date hors contrat")
        if self.analysis_session_id is not None and type(self.analysis_session_id) is not uuid.UUID:
            raise ValueError("thèse : provenance hors contrat")

    @property
    def provenance_status(self) -> str:
        return UNVERIFIED if self.analysis_session_id is None else VERIFIED


# --------------------------------------------------------------------------
# R1 PR 9-B1 : réservation durable des tours Décrypter (plan DECRYPTAGE_MUTATION)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReserveDecryptageTurn(Command):
    """Réserve UN tour Décrypter du principal avant tout appel externe.
    Identité logique : (principal, conversation_key, 'decryptage',
    client_turn_id, 1). Admission structurelle complète ICI, avant tout
    accès SQL ; aucune valeur n'est tronquée ni corrigée (seules les
    normalisations locales figées de la trame : strip().upper() du ticker,
    strip() de la question et du contexte)."""
    conversation_key: str
    client_turn_id: uuid.UUID
    ticker_input: str
    question: str
    context: str
    last_method_id: object  # str ou None (None distinct de "")

    def __post_init__(self):
        try:
            dfp.admit_conversation_key(self.conversation_key)
            if type(self.client_turn_id) is not uuid.UUID:
                raise dfp.TurnRejected("client_turn_id : uuid.UUID attendu")
            dfp.normalize_ticker(self.ticker_input)
            dfp.normalize_texts(self.question, self.context)
            dfp.admit_last_method_id(self.last_method_id)
        except dfp.TurnRejected as exc:
            raise ValueError(str(exc)) from None

    @property
    def ticker(self) -> str:
        """Ticker saisi, normalisé localement (jamais résolu par EODHD)."""
        return dfp.normalize_ticker(self.ticker_input)

    @property
    def texts(self) -> tuple:
        """(question, contexte) strippés, tels que signés."""
        return dfp.normalize_texts(self.question, self.context)


@dataclass(frozen=True, slots=True)
class RecordDecryptageAttemptFailure(Command):
    """Déclare l'échec connu d'une attribution par son détenteur : exige une
    nouvelle autorisation F2 complète ET la capacité exacte (réservation
    propre, epoch, jeton, bail non expiré). repr() ne révèle jamais le
    jeton."""
    reservation_id: uuid.UUID
    lease_epoch: int
    lease_token: dr.LeaseToken
    failure: str

    def __post_init__(self):
        if type(self.reservation_id) is not uuid.UUID:
            raise ValueError("reservation_id : uuid.UUID attendu")
        if type(self.lease_epoch) is not int or not 1 <= self.lease_epoch <= dr.MAX_EPOCH:
            raise ValueError("lease_epoch : entier de 1 à 2^63 - 1 attendu")
        if type(self.lease_token) is not dr.LeaseToken:
            raise ValueError("lease_token : jeton de bail serveur attendu")
        if type(self.failure) is not str or self.failure not in dr.FAILURE_CLASSES:
            raise ValueError("failure : classification fermée attendue")

    @classmethod
    def from_lease(cls, lease: dr.DecryptageLease, failure: str) -> "RecordDecryptageAttemptFailure":
        if type(lease) is not dr.DecryptageLease:
            raise ValueError("capacité de bail attendue")
        return cls(lease.reservation_id, lease.lease_epoch, lease.token, failure)


# --------------------------------------------------------------------------
# R1 PR 9-B2 : lecture de préparation d'un tour Décrypter (plan READ)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReadDecryptagePreparationContext(Command):
    """Lit, en UNE instruction et sans écriture, le contexte d'un tour
    Décrypter du principal détenteur d'un bail B1 : réservation propre (id ET
    user_id), état reserved, epoch exact, jeton exact (temps constant) et bail
    non échu à l'instant de la lecture ; identité du tour et intention
    revérifiées par le HMAC B1 enregistré ; puis profil (level ET
    level_revision), ancre conversationnelle R1-C4, AnalysisSession active du
    ticker RÉSOLU, faits, contributions et thèse historique, bornés.

    Admission structurelle complète ICI, avant tout accès SQL (mêmes règles
    que ReserveDecryptageTurn pour les entrées ; jamais de troncature).
    repr() ne révèle jamais le jeton (LeaseToken masqué)."""
    reservation_id: uuid.UUID
    lease_epoch: int
    lease_token: dr.LeaseToken
    conversation_key: str
    client_turn_id: uuid.UUID
    ticker_input: str
    question: str
    context: str
    last_method_id: object  # str ou None (None distinct de "")
    resolved_ticker: str

    def __post_init__(self):
        if type(self.reservation_id) is not uuid.UUID:
            raise ValueError("reservation_id : uuid.UUID attendu")
        if type(self.lease_epoch) is not int or not 1 <= self.lease_epoch <= dr.MAX_EPOCH:
            raise ValueError("lease_epoch : entier de 1 à 2^63 - 1 attendu")
        if type(self.lease_token) is not dr.LeaseToken:
            raise ValueError("lease_token : jeton de bail serveur attendu")
        try:
            dfp.admit_conversation_key(self.conversation_key)
            if type(self.client_turn_id) is not uuid.UUID:
                raise dfp.TurnRejected("client_turn_id : uuid.UUID attendu")
            dfp.normalize_ticker(self.ticker_input)
            dfp.normalize_texts(self.question, self.context)
            dfp.admit_last_method_id(self.last_method_id)
        except dfp.TurnRejected as exc:
            raise ValueError(str(exc)) from None
        if not dc.valid_resolved_ticker(self.resolved_ticker):
            raise ValueError("resolved_ticker : ticker résolu canonique attendu")

    @property
    def ticker(self) -> str:
        """Ticker SAISI normalisé (trame HMAC B1), jamais le ticker résolu."""
        return dfp.normalize_ticker(self.ticker_input)

    @property
    def texts(self) -> tuple:
        return dfp.normalize_texts(self.question, self.context)

    @classmethod
    def from_lease(cls, lease: dr.DecryptageLease, reservation: ReserveDecryptageTurn,
                   resolved_ticker: str) -> "ReadDecryptagePreparationContext":
        """Capacité B1 + entrées ORIGINALES de la réservation (même tour)."""
        if type(lease) is not dr.DecryptageLease or type(reservation) is not ReserveDecryptageTurn:
            raise ValueError("capacité de bail et réservation originale attendues")
        if (lease.conversation_key, lease.source_user_turn_id) != (reservation.conversation_key,
                                                                   reservation.client_turn_id):
            raise ValueError("le bail ne correspond pas au tour réservé")
        return cls(lease.reservation_id, lease.lease_epoch, lease.token, reservation.conversation_key,
                   reservation.client_turn_id, reservation.ticker_input, reservation.question, reservation.context,
                   reservation.last_method_id, resolved_ticker)


# --------------------------------------------------------------------------
# R1 PR 9-C1-B : finalisation d'un candidat Décrypter (plan DECRYPTAGE_MUTATION)
# --------------------------------------------------------------------------

def _revalidate_candidate(candidate) -> None:
    """Revérifie, sur l'objet REÇU, tous les invariants structurels du
    candidat et de ses sous-objets (un objet gelé peut avoir été altéré par
    object.__setattr__) : version, types exacts, bail, tour, instrument,
    empreinte financière, snapshot et son empreinte, payload."""
    if type(candidate) is not dcand.GeneratedDecryptageCandidate:
        raise ValueError("candidate : GeneratedDecryptageCandidate (type exact) attendu")
    dcand.GeneratedDecryptageCandidate.__post_init__(candidate)
    for part in (candidate.turn, candidate.lease, candidate.market, candidate.preparation, candidate.payload):
        type(part).__post_init__(part)
    if candidate.preparation.compute_fingerprint() != candidate.preparation.fingerprint:
        raise ValueError("candidate : empreinte de snapshot incohérente")
    if candidate.market_fingerprint != dcand.market_fingerprint(candidate.market):
        raise ValueError("candidate : empreinte financière incohérente")


@dataclass(frozen=True, slots=True, repr=False)
class FinalizeDecryptageCandidate(Command):
    """Finalise UN candidat B2 en livraison canonique F2 (une transaction :
    livraison, capture cognitive, progression, réservation finalized).
    repr() ne révèle ni jeton ni texte."""
    candidate: dcand.GeneratedDecryptageCandidate

    def __post_init__(self):
        _revalidate_candidate(self.candidate)

    def __repr__(self) -> str:
        return f"FinalizeDecryptageCandidate({self.candidate!r})"

    __str__ = __repr__


# --------------------------------------------------------------------------
# R1 PR 9-C2 : replay canonique (plan READ) et ACK F2 (plan DECRYPTAGE_MUTATION)
# --------------------------------------------------------------------------

# Statuts de livraison restituables (core.assistant_delivery.PENDING /
# DELIVERED, recopiés : la frontière des commandes n'importe pas les services ;
# égalité vérifiée par les tests).
CANONICAL_DELIVERY_STATUSES = ("pending", "delivered")


def _admit_canonical_delivery(command) -> None:
    """Admission structurelle COMPLÈTE de l'identité demandée (avant tout SQL) :
    UUID de types exacts, conversation au validateur B1 existant ; aucune
    normalisation, aucune valeur corrigée. Jamais une autorisation : la
    réservation propre, son état finalized, la livraison liée et sa provenance
    V2 sont prouvés par les adaptateurs SQL ; le propriétaire reste
    AuthorizedContext.principal."""
    if type(command.reservation_id) is not uuid.UUID:
        raise ValueError("reservation_id : uuid.UUID attendu")
    if type(command.assistant_turn_id) is not uuid.UUID:
        raise ValueError("assistant_turn_id : uuid.UUID attendu")
    try:
        dfp.admit_conversation_key(command.conversation_key)
    except dfp.TurnRejected as exc:
        raise ValueError(str(exc)) from None


@dataclass(frozen=True, slots=True)
class ReadCanonicalDecryptageDelivery(Command):
    """Restitue le contenu public CANONIQUE d'une livraison Décrypter
    finalisée par C1-B (réservation B1 propre finalized -> livraison liée ->
    lien cognitif V2), sans écriture, sans verrou métier, sans regénération.
    Ni user_id, ni jeton, ni clé, ni candidat, ni payload, ni statut imposé."""
    reservation_id: uuid.UUID
    conversation_key: str
    assistant_turn_id: uuid.UUID

    def __post_init__(self):
        _admit_canonical_delivery(self)


@dataclass(frozen=True, slots=True)
class AcknowledgeDecryptageDelivery(Command):
    """Confirme la PRÉSENTATION d'une livraison canonique C1-B (insertion dans
    l'interface first-party : jamais une preuve de lecture ni de
    compréhension). Capture cognitive historique et pending -> delivered
    atomiques ; ACK rejoué : no-op. Mêmes champs, même admission que le replay."""
    reservation_id: uuid.UUID
    conversation_key: str
    assistant_turn_id: uuid.UUID

    def __post_init__(self):
        _admit_canonical_delivery(self)


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalDecryptageDelivery:
    """Vue fermée du replay : octets UTF-8 EXACTS du texte JSONB public validé
    (assistant_deliveries.response_payload::text), jamais resérialisés.
    Aucune métadonnée privée, aucune empreinte, aucun identifiant cognitif.
    repr() / str() ne révèlent jamais le contenu."""
    reservation_id: uuid.UUID
    assistant_turn_id: uuid.UUID
    delivery_status: str
    payload_json: bytes

    def __post_init__(self):
        if type(self.reservation_id) is not uuid.UUID or type(self.assistant_turn_id) is not uuid.UUID:
            raise ValueError("livraison canonique : identifiants hors contrat")
        if type(self.delivery_status) is not str or self.delivery_status not in CANONICAL_DELIVERY_STATUSES:
            raise ValueError("livraison canonique : statut hors contrat")
        if type(self.payload_json) is not bytes or not self.payload_json:
            raise ValueError("livraison canonique : contenu hors contrat")

    def __repr__(self) -> str:
        return (f"CanonicalDecryptageDelivery(reservation_id={self.reservation_id}, "
                f"assistant_turn_id={self.assistant_turn_id}, delivery_status={self.delivery_status}, "
                "payload_json=<private>)")

    __str__ = __repr__
