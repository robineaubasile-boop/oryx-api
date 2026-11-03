"""Snapshot de préparation, validation de la réponse et candidat interne
Décrypter F2 (R1 PR 9-B2) — module pur, dormant.

1. PreparationSnapshot (oryx-decryptage-preparation-snapshot-v1) : le
   contexte pédagogique RÉELLEMENT injecté dans le prompt (pas toutes les
   lignes disponibles) : identité du tour, propriétaire autorisé, ticker
   saisi et résolu, (level, level_revision), méthode retenue et sa base,
   ancre conversationnelle, session active et étape ouverte, faits,
   contributions et thèse utilisés, avec leurs identifiants. Empreinte
   déterministe : SHA-256(préfixe de domaine || JSON canonique) — clés
   triées, séparateurs compacts, UTF-8 sans échappement ASCII, NaN interdit,
   UUID en minuscules avec tirets, flottants en binary64-decimal-v1
   (core.authz_f2.portfolio_values). Cette empreinte n'est NI le HMAC
   d'intention B1, NI le SHA-256 historique de la requête
   (assistant_deliveries.request_fingerprint) : aucune n'est substituable
   à une autre.

   Limite ABA : level_revision n'avance pas sur la route historique
   PUT /api/user/{user_id}/level ; (level, level_revision) est donc
   conservé, mais une modification suivie d'un retour à la valeur initiale
   reste indétectable sans versionnement plus fort. Le snapshot permet une
   revalidation par PR 9-C ; il ne prouve pas l'absence de modification
   concurrente.

2. validate_response : extraction du texte, bornes, raison d'arrêt,
   marqueurs privés <!--ORYX_STEP:...--> (sémantique de
   core.decryptage_progress.extract_step_marker, vocabulaire fermé ;
   marqueur inconnu ou contradictoire : aucun marqueur, jamais de transition
   implicite ; résidu de marqueur dans le texte visible : inexploitable),
   texte visible non vide. Sinon : ResponseUnusable (generation_unusable).

3. GeneratedDecryptageCandidate (oryx-decryptage-generated-candidate-v1) :
   type fermé, immuable (tuples et dataclasses gelées uniquement, aucun dict
   ni liste), validé à la construction, non sérialisable (pickle, copie,
   JSON), repr() sans jeton ni texte. Jamais retourné à un navigateur,
   jamais persisté, aucune AssistantDelivery : seule PR 9-C pourra le
   finaliser après revalidation sous verrous.
"""
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_fingerprint as dfp
from core.authz_f2 import decryptage_instrument as di
from core.authz_f2 import decryptage_market_data as dm
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2 import portfolio_values as pv
from core.decryptage_progress import DECRYPTAGE_STEP_MARKERS, STEP_MARKER_RE, extract_step_marker

SNAPSHOT_VERSION = "oryx-decryptage-preparation-snapshot-v1"
CANDIDATE_VERSION = "oryx-decryptage-generated-candidate-v1"
SNAPSHOT_DOMAIN = b"ORYX-DECRYPTAGE-PREPARATION-SNAPSHOT\x00"
MARKET_DOMAIN = b"ORYX-DECRYPTAGE-MARKET-DATA\x00"
# Copie de api.DECRYPTAGE_DISCLAIMER (la frontière n'importe pas api) ; égalité testée.
DISCLAIMER = "Analyse éducative uniquement. Ne constitue pas un conseil en investissement."
CONSTRUCTION_THESE = "construction_these"
# Base de la méthode retenue (règles historiques de /decryptage).
BASIS_ACTIVE_ANALYSIS = "active_analysis"  # session in_progress : construction_these forcée
BASIS_INITIAL_TURN = "initial_turn"        # contexte vide : construction_these forcée (+ thèse rappelée)
BASIS_LOOKUP = "lookup"                    # core.pedagogie_library.lookup_method (peut ne rien trouver)
METHOD_BASES = (BASIS_ACTIVE_ANALYSIS, BASIS_INITIAL_TURN, BASIS_LOOKUP)
# Faits injectés par core.decryptage_engine (ordre et clés de fact_labels).
PROMPT_FACT_TYPES = ("operating_margin", "roe", "roic", "gross_margin_latest", "fcf_per_share", "revenue_growth",
                     "net_cash", "eps")
ABSENT_LABEL = "N/D"  # convention d'absence de core.decryptage_engine
RESPONSE_MAX_BYTES = 64 * 1024
ACCEPTED_STOP_REASONS = ("end_turn", "stop_sequence")
UNKNOWN_MARKER = "unknown_marker"
CONTRADICTORY_MARKERS = "contradictory_markers"
RESPONSE_DIAGNOSTICS = (UNKNOWN_MARKER, CONTRADICTORY_MARKERS)
MODEL_MAX_CHARS = 128


class ResponseUnusable(ValueError):
    """Réponse vide, uniquement composée de marqueurs ou structurellement
    inexploitable : generation_unusable."""


# --------------------------------------------------------------------------
# Encodage canonique
# --------------------------------------------------------------------------

def _plain(value):
    """Arbre canonique : str, int, bool, None, listes, dicts à clés str ;
    UUID -> str ; float -> binary64-decimal-v1 (jamais NaN ni infini)."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("valeur non finie dans un snapshot")
        return {"binary64": pv.render_binary64_decimal_v1(value)}
    if type(value) is uuid.UUID:
        return str(value)
    if type(value) in (list, tuple):
        return [_plain(v) for v in value]
    if type(value) is dict:
        if any(type(k) is not str for k in value):
            raise TypeError("clé canonique non textuelle")
        return {k: _plain(v) for k, v in value.items()}
    raise TypeError(f"type non canonique : {type(value).__name__}")


def canonical_bytes(value) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def market_fingerprint(snapshot: dm.FinancialSnapshot) -> str:
    return hashlib.sha256(MARKET_DOMAIN + canonical_bytes(snapshot.canonical())).hexdigest()


# --------------------------------------------------------------------------
# Snapshot de préparation (contexte réellement utilisé)
# --------------------------------------------------------------------------

SNAPSHOT_FIELDS = ("version", "reservation_id", "lease_epoch", "conversation_key", "client_turn_id", "owner_user_id",
                   "ticker_input", "ticker", "level", "level_revision", "method_id", "method_basis", "anchor",
                   "active_session", "facts_used", "statements_used", "thesis_used")


def snapshot_canonical(*, version, reservation_id, lease_epoch, conversation_key, client_turn_id, owner_user_id,
                       ticker_input, ticker, level, level_revision, method_id, method_basis, anchor, active_session,
                       facts_used, statements_used, thesis_used) -> dict:
    """Contenu canonique du snapshot (entrée de l'empreinte)."""
    return {
        "version": version,
        "turn": {"reservation_id": reservation_id, "lease_epoch": lease_epoch, "conversation_key": conversation_key,
                 "client_turn_id": client_turn_id},
        "owner_user_id": owner_user_id,
        "ticker_input": ticker_input, "ticker": ticker,
        "level": level, "level_revision": level_revision,
        "method": {"id": method_id, "basis": method_basis},
        "anchor": {"state": anchor.state, "event_id": anchor.event_id,
                   "analysis_session_id": anchor.analysis_session_id},
        "active_session": None if active_session is None else {"id": active_session.analysis_session_id,
                                                               "current_step": active_session.current_step},
        "facts": [{"id": f.fact_id, "type": f.fact_type, "value": f.value} for f in facts_used],
        "statements": [{"id": x.statement_id, "step": x.step, "text": x.text} for x in statements_used],
        "thesis": None if thesis_used is None else {"id": thesis_used.thesis_id, "text": thesis_used.text,
                                                    "date": thesis_used.date_label},
    }


def snapshot_fingerprint(canonical: dict) -> str:
    return hashlib.sha256(SNAPSHOT_DOMAIN + canonical_bytes(canonical)).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparationSnapshot:
    version: str
    reservation_id: uuid.UUID
    lease_epoch: int
    conversation_key: str
    client_turn_id: uuid.UUID
    owner_user_id: str
    ticker_input: str
    ticker: str
    level: str
    level_revision: int
    method_id: object      # str ou None (aucune méthode)
    method_basis: str
    anchor: dc.PreparationAnchor
    active_session: object  # dc.PreparationActiveSession ou None
    facts_used: tuple       # dc.PreparationFact
    statements_used: tuple  # dc.PreparationStatement
    thesis_used: object     # dc.PreparationThesis ou None
    fingerprint: str

    def __post_init__(self):
        if self.version != SNAPSHOT_VERSION or self.method_basis not in METHOD_BASES:
            raise ValueError("snapshot : version ou base hors contrat")
        if self.method_id is not None and (type(self.method_id) is not str or not self.method_id):
            raise ValueError("snapshot : méthode hors contrat")
        if self.method_basis != BASIS_LOOKUP and self.method_id != CONSTRUCTION_THESE:
            raise ValueError("snapshot : méthode forcée incohérente")
        if (self.active_session is not None) != (self.method_basis == BASIS_ACTIVE_ANALYSIS):
            raise ValueError("snapshot : la session active est prioritaire")
        if self.thesis_used is not None and self.method_basis != BASIS_INITIAL_TURN:
            raise ValueError("snapshot : thèse rappelée hors tour initial")
        if self.active_session is None and (self.facts_used or self.statements_used):
            raise ValueError("snapshot : faits ou contributions sans session active")
        if type(self.anchor) is not dc.PreparationAnchor or not dc.valid_resolved_ticker(self.ticker):
            raise ValueError("snapshot : ancre ou ticker hors contrat")
        if type(self.facts_used) is not tuple or any(type(f) is not dc.PreparationFact for f in self.facts_used) \
                or type(self.statements_used) is not tuple \
                or any(type(s) is not dc.PreparationStatement for s in self.statements_used):
            raise ValueError("snapshot : éléments hors contrat")
        if type(self.level) is not str or self.level not in dc.LEVELS or type(self.level_revision) is not int:
            raise ValueError("snapshot : niveau hors contrat")
        if self.fingerprint != self.compute_fingerprint():
            raise ValueError("snapshot : empreinte incohérente")

    def canonical(self) -> dict:
        return snapshot_canonical(**{name: getattr(self, name) for name in SNAPSHOT_FIELDS})

    def compute_fingerprint(self) -> str:
        return snapshot_fingerprint(self.canonical())

    def in_progress_analysis(self):
        """Argument in_progress_analysis de build_system_prompt (nouveau dict)."""
        if self.active_session is None:
            return None
        return {"current_step": self.active_session.current_step,
                "statements": [{"step": s.step, "text": s.text} for s in self.statements_used],
                # Dernière valeur NULL : « N/D » (convention d'absence du moteur), jamais
                # une valeur antérieure ni un zéro ; la valeur None reste dans le snapshot.
                "facts": {f.fact_type: ABSENT_LABEL if f.value is None else f.value for f in self.facts_used}}

    def existing_thesis(self):
        """Argument existing_thesis de build_system_prompt (nouveau dict)."""
        if self.thesis_used is None:
            return None
        return {"text": self.thesis_used.text, "date": self.thesis_used.date_label or "date non enregistrée"}

    def __reduce__(self):
        raise TypeError("PreparationSnapshot n'est pas sérialisable")


def used_facts(facts: tuple) -> tuple:
    """Règle historique (dict {type: valeur} construit dans l'ordre fact_date
    ASC, id ASC) : la DERNIÈRE ligne d'un type l'emporte, MÊME si sa valeur
    est NULL — jamais une valeur antérieure réutilisée parce qu'elle est
    connue (audit P1-02). Restreinte aux types injectés dans le prompt, dans
    l'ordre de lecture."""
    last = {}
    for fact in facts:
        if fact.fact_type in PROMPT_FACT_TYPES:
            last[fact.fact_type] = fact
    chosen = {id(f) for f in last.values()}
    return tuple(f for f in facts if id(f) in chosen)


def used_statements(statements: tuple) -> tuple:
    """Règle historique : étape renseignée et texte non vide après strip()."""
    return tuple(s for s in statements if s.step and s.text.strip())


def build_snapshot(view: dc.DecryptagePreparationView, ticker_input: str, method_id, method_basis: str
                   ) -> PreparationSnapshot:
    active = view.active_session
    facts = used_facts(view.facts) if active is not None else ()
    statements = used_statements(view.statements) if active is not None else ()
    thesis = view.latest_thesis if method_basis == BASIS_INITIAL_TURN else None
    fields = dict(version=SNAPSHOT_VERSION, reservation_id=view.reservation_id, lease_epoch=view.lease_epoch,
                  conversation_key=view.conversation_key, client_turn_id=view.client_turn_id,
                  owner_user_id=view.owner_user_id, ticker_input=ticker_input, ticker=view.ticker, level=view.level,
                  level_revision=view.level_revision, method_id=method_id, method_basis=method_basis,
                  anchor=view.anchor, active_session=active, facts_used=facts, statements_used=statements,
                  thesis_used=thesis)
    return PreparationSnapshot(**fields, fingerprint=snapshot_fingerprint(snapshot_canonical(**fields)))


# --------------------------------------------------------------------------
# Validation de la réponse Claude
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ValidatedResponse:
    visible_text: str
    step_marker: object   # marqueur du vocabulaire fermé ou None
    diagnostics: tuple    # sous-ensemble ordonné de RESPONSE_DIAGNOSTICS
    stop_reason: str


def validate_response(raw) -> ValidatedResponse:
    """Réponse brute du transport : {"stop_reason": str, "content": [blocs]}.
    Exactement UN bloc texte (les blocs non textuels sont ignorés)."""
    if type(raw) is not dict or type(raw.get("content")) is not list:
        raise ResponseUnusable("structure de réponse inexploitable")
    stop_reason = raw.get("stop_reason")
    if type(stop_reason) is not str or stop_reason not in ACCEPTED_STOP_REASONS:
        raise ResponseUnusable("réponse interrompue ou raison d'arrêt inconnue")
    texts = [b for b in raw["content"] if type(b) is dict and b.get("type") == "text"]
    if len(texts) != 1 or type(texts[0].get("text")) is not str:
        raise ResponseUnusable("un seul bloc texte attendu")
    text = texts[0]["text"]
    length = dc.utf8_length(text)
    if length is None or length > RESPONSE_MAX_BYTES or "\x00" in text:
        raise ResponseUnusable("texte hors bornes")
    found = STEP_MARKER_RE.findall(text)
    diagnostics = []
    if any(m not in DECRYPTAGE_STEP_MARKERS for m in found):
        diagnostics.append(UNKNOWN_MARKER)
    if len({m for m in found if m in DECRYPTAGE_STEP_MARKERS}) > 1:
        diagnostics.append(CONTRADICTORY_MARKERS)
    visible, marker = extract_step_marker(text)
    if diagnostics:
        marker = None  # jamais de transition implicite
    if "ORYX_STEP" in visible or "<!--" in visible:
        raise ResponseUnusable("marqueur privé résiduel dans le texte visible")
    if not visible.strip():
        raise ResponseUnusable("texte visible vide")
    return ValidatedResponse(visible, marker, tuple(diagnostics), stop_reason)


# --------------------------------------------------------------------------
# Candidat interne
# --------------------------------------------------------------------------

def _finite_or_none(value) -> bool:
    return value is None or (type(value) is float and math.isfinite(value))


@dataclass(frozen=True, slots=True)
class CandidatePayload:
    """Payload public candidat, forme de /decryptage (success, ticker, name,
    method_used, analysis, price, currency, disclaimer)."""
    ticker: str
    name: str
    method_used: object
    analysis: str
    price: object
    currency: str
    disclaimer: str = DISCLAIMER
    success: bool = True

    def __post_init__(self):
        if self.success is not True or self.disclaimer != DISCLAIMER:
            raise ValueError("payload : succès et avertissement figés")
        if not dc.valid_resolved_ticker(self.ticker) or type(self.name) is not str or not self.name:
            raise ValueError("payload : instrument hors contrat")
        if self.method_used is not None and type(self.method_used) is not str:
            raise ValueError("payload : méthode hors contrat")
        if type(self.analysis) is not str or not self.analysis.strip():
            raise ValueError("payload : texte visible vide")
        if not _finite_or_none(self.price) or type(self.currency) is not str or not self.currency:
            raise ValueError("payload : prix ou devise hors contrat")

    def as_dict(self) -> dict:
        """NOUVEAU dict à chaque appel (jamais une vue mutable du candidat)."""
        return {"success": True, "ticker": self.ticker, "name": self.name, "method_used": self.method_used,
                "analysis": self.analysis, "price": self.price, "currency": self.currency,
                "disclaimer": self.disclaimer}


@dataclass(frozen=True, slots=True)
class TurnInputs:
    """Entrées originales du tour, normalisées exactement comme la trame B1
    (ticker strip().upper(), question / contexte strip(), last_method_id
    inchangé). Le niveau n'en fait pas partie : il vient du serveur."""
    conversation_key: str
    client_turn_id: uuid.UUID
    ticker_input: str
    question: str
    context: str
    last_method_id: object

    def __post_init__(self):
        try:
            dfp.admit_conversation_key(self.conversation_key)
            if type(self.client_turn_id) is not uuid.UUID:
                raise dfp.TurnRejected("tour")
            if dfp.normalize_ticker(self.ticker_input) != self.ticker_input:
                raise dfp.TurnRejected("ticker non normalisé")
            if dfp.normalize_texts(self.question, self.context) != (self.question, self.context):
                raise dfp.TurnRejected("textes non normalisés")
            dfp.admit_last_method_id(self.last_method_id)
        except dfp.TurnRejected as exc:
            raise ValueError(f"entrées du tour hors contrat : {exc}") from None

    @classmethod
    def from_reservation(cls, command) -> "TurnInputs":
        question, context = command.texts
        return cls(command.conversation_key, command.client_turn_id, command.ticker, question, context,
                   command.last_method_id)


@dataclass(frozen=True, slots=True, repr=False)
class GeneratedDecryptageCandidate(dr._Unserializable):
    contract_version: str
    turn: TurnInputs
    lease: dr.DecryptageLease
    owner_user_id: str
    instrument: di.InstrumentResolution
    market: dm.FinancialSnapshot
    market_fingerprint: str
    preparation: PreparationSnapshot
    prepared_read_at: datetime          # clock_timestamp() PostgreSQL de la seconde lecture
    prepared_lease_expires_at: datetime  # échéance enregistrée lue à ce moment (indicative)
    payload: CandidatePayload
    step_marker: object
    response_diagnostics: tuple
    model: str

    def __post_init__(self):
        if self.contract_version != CANDIDATE_VERSION:
            raise ValueError("candidat : version hors contrat")
        if type(self.turn) is not TurnInputs or type(self.lease) is not dr.DecryptageLease \
                or type(self.instrument) is not di.InstrumentResolution or not self.instrument.identified \
                or type(self.market) is not dm.FinancialSnapshot or type(self.preparation) is not PreparationSnapshot \
                or type(self.payload) is not CandidatePayload:
            raise ValueError("candidat : sous-objet hors contrat")
        lease, prep = self.lease, self.preparation
        if (lease.conversation_key, lease.source_user_turn_id) != (self.turn.conversation_key,
                                                                   self.turn.client_turn_id):
            raise ValueError("candidat : bail d'un autre tour")
        if (prep.reservation_id, prep.lease_epoch, prep.conversation_key, prep.client_turn_id) != (
                lease.reservation_id, lease.lease_epoch, lease.conversation_key, lease.source_user_turn_id):
            raise ValueError("candidat : snapshot d'un autre bail")
        if type(self.owner_user_id) is not str or self.owner_user_id != prep.owner_user_id:
            raise ValueError("candidat : propriétaire incohérent")
        if not (self.instrument.ticker == self.market.ticker == prep.ticker == self.payload.ticker) \
                or prep.ticker_input != self.turn.ticker_input:
            raise ValueError("candidat : instrument incohérent")
        if self.market_fingerprint != market_fingerprint(self.market):
            raise ValueError("candidat : empreinte financière incohérente")
        if self.payload.method_used != prep.method_id or self.payload.currency != self.market.currency \
                or self.payload.price != self.market.public_price():
            raise ValueError("candidat : payload incohérent")
        if self.step_marker is not None and self.step_marker not in DECRYPTAGE_STEP_MARKERS:
            raise ValueError("candidat : marqueur hors vocabulaire")
        if type(self.response_diagnostics) is not tuple or any(d not in RESPONSE_DIAGNOSTICS
                                                                for d in self.response_diagnostics):
            raise ValueError("candidat : diagnostics hors contrat")
        if self.response_diagnostics and self.step_marker is not None:
            raise ValueError("candidat : marqueur malgré un diagnostic")
        for instant in (self.prepared_read_at, self.prepared_lease_expires_at):
            if type(instant) is not datetime or instant.tzinfo is None:
                raise ValueError("candidat : instants PostgreSQL attendus")
        if self.prepared_lease_expires_at != lease.lease_expires_at:
            raise ValueError("candidat : échéance incohérente avec le bail")
        if type(self.model) is not str or not 0 < len(self.model) <= MODEL_MAX_CHARS:
            raise ValueError("candidat : modèle hors contrat")

    @property
    def visible_text(self) -> str:
        return self.payload.analysis

    def delivery_fingerprint_inputs(self) -> dict:
        """Valeurs dont PR 9-C aura besoin pour la future empreinte de
        livraison historique (core.assistant_delivery.
        decryptage_request_fingerprint) : identité SERVEUR, entrées
        réservées, niveau lu par le serveur. Aucune empreinte calculée ici."""
        return {"user_id": self.owner_user_id, "conversation_key": self.turn.conversation_key,
                "client_turn_id": self.turn.client_turn_id, "ticker_input": self.turn.ticker_input,
                "question": self.turn.question, "context": self.turn.context,
                "last_method_id": self.turn.last_method_id, "level": self.preparation.level}

    def redacted(self) -> dict:
        """Vue de diagnostic expurgée : ni jeton, ni texte, ni propriétaire."""
        return {"contract_version": self.contract_version, "reservation_id": str(self.lease.reservation_id),
                "lease_epoch": self.lease.lease_epoch, "ticker": self.instrument.ticker,
                "instrument_provenance": self.instrument.provenance, "market_source": self.market.primary_source,
                "market_fingerprint": self.market_fingerprint, "method_id": self.preparation.method_id,
                "preparation_version": self.preparation.version,
                "preparation_fingerprint": self.preparation.fingerprint, "step_marker": self.step_marker,
                "response_diagnostics": list(self.response_diagnostics)}

    def __repr__(self) -> str:
        return (f"GeneratedDecryptageCandidate(reservation_id={self.lease.reservation_id!r}, "
                f"lease_epoch={self.lease.lease_epoch!r}, ticker={self.instrument.ticker!r}, token=<secret>)")

    __str__ = __repr__
