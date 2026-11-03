"""Vue de préparation d'un tour Décrypter F2 (R1 PR 9-B2) — module PUR, dormant.

Résultat fermé de la commande READ ReadDecryptagePreparationContext
(core.authz_f2.commands), construit par l'adaptateur SQL privé
core.authz_f2._adapters._read_preparation_context à partir d'UNE SEULE
instruction PostgreSQL (un seul instantané d'instruction sous READ
COMMITTED : réservation, profil, ancre conversationnelle, AnalysisSession
active, faits, contributions et thèse sont lus ensemble ; jamais une
succession d'instructions indépendantes présentée comme un instantané).

La vue ne porte AUCUN secret (ni jeton de bail, ni empreinte HMAC, ni
empreinte du jeton) et aucun effet : une lecture n'écrit rien. Elle porte
tout ce qu'une génération B2 utilise et que PR 9-C devra revalider ; elle
n'est jamais une autorisation de finalisation.

Deux issues fermées :

- DecryptagePreparationView : contexte cohérent, borné, propriétaire ;
- DecryptagePreparationRefused(reason) : refus fermé d'un contexte
  métier inutilisable (conversation obsolète, sessions actives multiples,
  étape ouverte invalide, contenu hors bornes, valeur numérique non finie).

Les pannes SQL, lignes incohérentes, contenus d'autrui ou versions de clé
introuvables ne deviennent JAMAIS une vue ni un refus métier : l'adaptateur
lève AdapterInvariantBroken (UNAVAILABLE), et une panne n'est jamais une
« absence d'analyse ».

Bornes (aucune troncature : hors bornes = refus context_out_of_bounds) :
64 faits et 64 contributions au plus par AnalysisSession (65 lignes lues
pour détecter le dépassement), texte de contribution <= 16 Kio, total des
contributions <= 64 Kio, texte de thèse <= 32 Kio, étape <= 32 octets, type
de fait <= 64 octets (UTF-8).

Ne lit aucune variable d'environnement, n'ouvre aucune connexion.
"""
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

CONTRACT_VERSION = "oryx-decryptage-preparation-read-v1"

# Étapes TÂCHES de construction_these (core.decryptage_progress, recopiées
# ici : aucun import applicatif ; égalité vérifiée par les tests). Une
# AnalysisSession in_progress dont l'étape ouverte n'en fait pas partie est
# refusée (jamais de retour implicite au Business).
TASK_STEPS = ("business", "moat", "chiffres", "valorisation", "risques")
LEVELS = ("debutant", "avance")  # préférence produit déclarée, jamais un stade pédagogique

MAX_FACTS = 64
MAX_STATEMENTS = 64
STATEMENT_TEXT_MAX_BYTES = 16 * 1024
STATEMENTS_TOTAL_MAX_BYTES = 64 * 1024
THESIS_TEXT_MAX_BYTES = 32 * 1024
STEP_MAX_BYTES = 32
FACT_TYPE_MAX_BYTES = 64

# Ticker résolu (format historique de core.ticker_resolver : code nu pour
# une cotation US, CODE.PLACE sinon). Jamais corrigé.
RESOLVED_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,31}", re.ASCII)

# État de l'ancre conversationnelle R1-C4 au moment de la lecture.
ANCHOR_NONE = "none"                  # aucune livraison V2 delivered + captured ancrante
ANCHOR_UNBOUND = "unbound"            # event d'ancrage sans AnalysisSession
ANCHOR_OTHER_TICKER = "other_ticker"  # ancrée à une autre entreprise : navigation, jamais stale
ANCHOR_CURRENT = "current"            # ancrée au MÊME ticker, état cognitif encore valide
ANCHOR_STATES = (ANCHOR_NONE, ANCHOR_UNBOUND, ANCHOR_OTHER_TICKER, ANCHOR_CURRENT)

STALE_CONVERSATION_CONTEXT = "stale_conversation_context"
MULTIPLE_ACTIVE_SESSIONS = "multiple_active_sessions"
ACTIVE_SESSION_STEP_INVALID = "active_session_step_invalid"
CONTEXT_OUT_OF_BOUNDS = "context_out_of_bounds"
CONTEXT_VALUE_INVALID = "context_value_invalid"
REFUSAL_REASONS = (STALE_CONVERSATION_CONTEXT, MULTIPLE_ACTIVE_SESSIONS, ACTIVE_SESSION_STEP_INVALID,
                   CONTEXT_OUT_OF_BOUNDS, CONTEXT_VALUE_INVALID)


def utf8_length(value):
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def valid_resolved_ticker(value) -> bool:
    return type(value) is str and RESOLVED_TICKER_RE.fullmatch(value) is not None


def _uuid(value) -> bool:
    return type(value) is uuid.UUID


def _aware(value) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _positive_int(value) -> bool:
    return type(value) is int and value >= 1


def _text(value, max_bytes) -> bool:
    length = utf8_length(value) if type(value) is str else None
    return length is not None and length <= max_bytes and "\x00" not in value


@dataclass(frozen=True, slots=True)
class PreparationAnchor:
    """Ancre conversationnelle (dernière livraison V2 delivered + captured de
    la conversation, jamais plus loin) : identifiants techniques seulement."""
    state: str
    event_id: object             # uuid.UUID ou None (ANCHOR_NONE)
    analysis_session_id: object  # uuid.UUID (other_ticker / current) ou None

    def __post_init__(self):
        if self.state not in ANCHOR_STATES or type(self.state) is not str:
            raise ValueError("ancre : état hors contrat")
        if self.state == ANCHOR_NONE:
            if self.event_id is not None or self.analysis_session_id is not None:
                raise ValueError("ancre absente : aucun identifiant")
        elif not _uuid(self.event_id):
            raise ValueError("ancre : event attendu")
        elif (self.state == ANCHOR_UNBOUND) != (self.analysis_session_id is None) or (
                self.analysis_session_id is not None and not _uuid(self.analysis_session_id)):
            raise ValueError("ancre : AnalysisSession incohérente")


@dataclass(frozen=True, slots=True)
class PreparationActiveSession:
    """L'UNIQUE AnalysisSession in_progress du principal pour ce ticker ;
    current_step = étape ACTUELLEMENT OUVERTE (attend la réponse)."""
    analysis_session_id: uuid.UUID
    current_step: str

    def __post_init__(self):
        if not _uuid(self.analysis_session_id) or type(self.current_step) is not str \
                or self.current_step not in TASK_STEPS:
            raise ValueError("session active hors contrat")


@dataclass(frozen=True, slots=True)
class PreparationFact:
    """AnalysisFact de la session active (propriétaire vérifié), valeur
    binary64 exacte (float8send) ou None ; jamais NaN ni infini."""
    fact_id: int
    fact_type: str
    value: object  # float fini ou None

    def __post_init__(self):
        if not _positive_int(self.fact_id) or not _text(self.fact_type, FACT_TYPE_MAX_BYTES):
            raise ValueError("fait hors contrat")
        if self.value is not None and (type(self.value) is not float or not math.isfinite(self.value)):
            raise ValueError("fait : valeur non finie")


@dataclass(frozen=True, slots=True)
class PreparationStatement:
    """UserStatement de la session active (propriétaire vérifié), texte intégral."""
    statement_id: int
    step: object  # str ou None
    text: str

    def __post_init__(self):
        if not _positive_int(self.statement_id) or not _text(self.text, STATEMENT_TEXT_MAX_BYTES):
            raise ValueError("contribution hors contrat")
        if self.step is not None and not _text(self.step, STEP_MAX_BYTES):
            raise ValueError("contribution : étape hors contrat")


@dataclass(frozen=True, slots=True)
class PreparationThesis:
    """Dernière InvestmentThesis du ticker (created_at DESC NULLS LAST, id
    DESC), lue seulement sans AnalysisSession active. date_label : JJ/MM/AAAA
    ou None (date non enregistrée)."""
    thesis_id: int
    text: str
    date_label: object

    def __post_init__(self):
        if not _positive_int(self.thesis_id) or not _text(self.text, THESIS_TEXT_MAX_BYTES):
            raise ValueError("thèse hors contrat")
        if self.date_label is not None and (type(self.date_label) is not str
                                            or re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", self.date_label) is None):
            raise ValueError("thèse : date hors contrat")


@dataclass(frozen=True, slots=True)
class DecryptagePreparationView:
    """Contexte de préparation lu en UNE instruction (instantané unique).
    read_at = clock_timestamp() de cette instruction ; lease_expires_at =
    échéance enregistrée (horloge PostgreSQL faisant autorité au moment de la
    lecture, jamais un droit de finalisation)."""
    contract_version: str
    reservation_id: uuid.UUID
    lease_epoch: int
    attempts_used: int
    lease_expires_at: datetime
    read_at: datetime
    owner_user_id: str
    conversation_key: str
    client_turn_id: uuid.UUID
    ticker: str
    level: str
    level_revision: int
    anchor: PreparationAnchor
    active_session: object  # PreparationActiveSession ou None
    facts: tuple            # PreparationFact, fact_date ASC, id ASC
    statements: tuple       # PreparationStatement, statement_date ASC, id ASC
    latest_thesis: object   # PreparationThesis ou None (toujours None avec une session active)

    def __post_init__(self):
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("préparation : version hors contrat")
        if not _uuid(self.reservation_id) or not _uuid(self.client_turn_id):
            raise ValueError("préparation : identifiants hors contrat")
        if not _positive_int(self.lease_epoch) or not _positive_int(self.attempts_used):
            raise ValueError("préparation : bail hors contrat")
        if not _aware(self.lease_expires_at) or not _aware(self.read_at) or not self.read_at < self.lease_expires_at:
            raise ValueError("préparation : bail non actif à la lecture")
        if type(self.owner_user_id) is not str or not self.owner_user_id:
            raise ValueError("préparation : propriétaire serveur attendu")
        if type(self.conversation_key) is not str or not valid_resolved_ticker(self.ticker):
            raise ValueError("préparation : conversation ou ticker hors contrat")
        if type(self.level) is not str or self.level not in LEVELS or type(self.level_revision) is not int \
                or not 0 <= self.level_revision <= 2**63 - 1:
            raise ValueError("préparation : niveau ou révision hors contrat")
        if type(self.anchor) is not PreparationAnchor:
            raise ValueError("préparation : ancre hors contrat")
        if self.active_session is not None and type(self.active_session) is not PreparationActiveSession:
            raise ValueError("préparation : session active hors contrat")
        if type(self.facts) is not tuple or any(type(f) is not PreparationFact for f in self.facts) \
                or len(self.facts) > MAX_FACTS:
            raise ValueError("préparation : faits hors contrat")
        if type(self.statements) is not tuple or any(type(s) is not PreparationStatement for s in self.statements) \
                or len(self.statements) > MAX_STATEMENTS \
                or sum(utf8_length(s.text) for s in self.statements) > STATEMENTS_TOTAL_MAX_BYTES:
            raise ValueError("préparation : contributions hors contrat")
        if self.active_session is None:
            if self.facts or self.statements:
                raise ValueError("préparation : faits ou contributions sans session active")
        elif self.latest_thesis is not None:
            raise ValueError("préparation : thèse historique avec une session active")
        if self.latest_thesis is not None and type(self.latest_thesis) is not PreparationThesis:
            raise ValueError("préparation : thèse hors contrat")
        if self.anchor.state == ANCHOR_CURRENT and (
                self.active_session is None
                or self.active_session.analysis_session_id != self.anchor.analysis_session_id):
            raise ValueError("préparation : ancre courante sans la session active")

    def context_key(self) -> tuple:
        """Tout le contexte lu SAUF l'instant de lecture : deux lectures dont
        les clés diffèrent ne décrivent pas le même contexte."""
        return (self.contract_version, self.reservation_id, self.lease_epoch, self.attempts_used,
                self.lease_expires_at, self.owner_user_id, self.conversation_key, self.client_turn_id, self.ticker,
                self.level, self.level_revision, self.anchor, self.active_session, self.facts, self.statements,
                self.latest_thesis)

    def __reduce__(self):
        raise TypeError("DecryptagePreparationView n'est pas sérialisable")


@dataclass(frozen=True, slots=True)
class DecryptagePreparationRefused:
    """Contexte métier inutilisable : refus fermé, aucune génération."""
    reason: str

    def __post_init__(self):
        if type(self.reason) is not str or self.reason not in REFUSAL_REASONS:
            raise ValueError("refus de préparation hors contrat")


PREPARATION_RESULTS = (DecryptagePreparationView, DecryptagePreparationRefused)
