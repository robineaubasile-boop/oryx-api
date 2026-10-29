"""Adaptateurs SQL PRIVÉS et audités de la frontière d'autorisation (BF1).

Seul module de core/authz_f2 qui exécute du SQL. Chaque adaptateur reçoit
la connexion du runner pour UNE opération prévue, écrite ici en constante ;
aucun ne fait commit(), rollback(), begin() ni SET, aucun ne compose du SQL
à partir d'une donnée, aucun n'appelle le réseau. Le propriétaire est
toujours un user_id serveur : celui de la pré-validation pour les verrous
pris AVANT T_authorization (confirmé ensuite par la décision), celui du
principal autorisé après.

Inventaire (vérifié par tests/test_authz_f2_boundary.py) :

| Opération | Table | Filtre | Verrou |
|---|---|---|---|
| lock_profile | users | id | plan (FOR KEY SHARE / FOR UPDATE) |
| _read_profile | users | id (level ET level_revision, une requête) | - |
| _list_positions | portfolio_positions | user_id | - |
| _read_position | portfolio_positions | id ET user_id | - |
| _list_theses | investment_theses | user_id | - |
| _list_theses_for_ticker | investment_theses | user_id ET ticker | - |
| _read_delivery | assistant_deliveries | id ET user_id | - |
| _list_conversation_deliveries | assistant_deliveries | user_id ET conversation_key | - |
| _update_profile_level | users | id ET level_revision attendue | (ligne déjà verrouillée, plan PROFILE_MUTATION) |
| require_read_committed | - | current_setting('transaction_isolation') | - (plan PORTFOLIO_MUTATION) |
| _lock_portfolio (reçu) | portfolio_write_idempotency | user_id ET operation_type ET operation_id | FOR UPDATE |
| _lock_portfolio (position, PUT / DELETE sans reçu) | portfolio_positions | id ET user_id | FOR UPDATE |
| _create_position | portfolio_positions | INSERT, user_id serveur | (users déjà FOR UPDATE) |
| _replace_position | portfolio_positions | id ET user_id ET position_revision attendue | (ligne déjà verrouillée) |
| _delete_position | portfolio_positions | id ET user_id ET position_revision attendue | (ligne déjà verrouillée) |
| _insert_receipt | portfolio_write_idempotency | INSERT, user_id serveur | (users déjà FOR UPDATE) |
| _analysis_overview_page (R1 PR 8) | analysis_sessions + investment_theses | user_id (chaque sous-requête), keyset ticker | - |
| _theses_page (R1 PR 8) | investment_theses (+ analysis_sessions pour la provenance) | user_id [ET ticker], keyset | - |
| _read_thesis (R1 PR 8) | investment_theses (+ analysis_sessions pour la provenance) | id ET user_id | - |

Une ressource étrangère et une ressource inexistante rendent le même
ABSENT : le filtre SQL porte simultanément l'identifiant et le user_id, la
ligne d'autrui n'est ni lue ni verrouillée.

R1 PR 6 (CAS sur users.level_revision, ligne users déjà verrouillée FOR
UPDATE par le plan PROFILE_MUTATION) : profil relu (niveau ET révision,
une requête) et jugé cohérent, révision comparée AVANT le niveau ;
révision différente : CONFLICT, rien n'est écrit ; niveau identique :
profil rendu tel quel, sans UPDATE ; sinon un seul UPDATE conditionnel
(id ET révision attendue) qui écrit le niveau et incrémente la révision,
valeurs relues par RETURNING. Aucune ligne retournée après ces contrôles
sous verrou : invariant rompu (rollback, 503), jamais un faux conflit. À
la révision maximale, un changement réel est refusé (RevisionExhausted :
503 et alerte interne), sans débordement ni remise à zéro.

R1 PR 7 (plan PORTFOLIO_MUTATION, READ COMMITTED vérifié par
require_read_committed au début de la phase B) : la mutation de
démonstration UpdatePositionQuantity et son verrou sont retirés.
_lock_portfolio rend un ÉTAT FERMÉ, avant T_authorization et sans rien
conclure : ReceiptFound (reçu du triplet, FOR UPDATE), CreateReady
(CREATE sans reçu : aucune position verrouillée), PositionFound (PUT /
DELETE sans reçu : position propre FOR UPDATE) ou PositionMissing. Le reçu
est TOUJOURS cherché avant la position : une position absente ne devient
404 qu'en l'absence de reçu. L'absence d'un reçu n'est jamais un verrou :
deux opérations du même compte sont sérialisées par users FOR UPDATE.

Après la décision, _write_portfolio (trousseau portefeuille et points
d'interruption transmis par le runner) :

- reçu : structure, versions, statut et payload validés
  (core.authz_f2.portfolio_results), empreinte recalculée avec la version
  de canonicalisation et la version de clé ENREGISTRÉES ; identique :
  résultat d'origine restitué sans écriture ; différente :
  IDEMPOTENCY_CONFLICT ; reçu, version ou clé invalide :
  PortfolioUnavailable (503, aucune réexécution) ;
- position absente ou étrangère : ABSENT (404 indiscernable) ;
- révision différente : CONFLICT (409, rien n'est écrit) — avant toute
  comparaison de valeurs ;
- PUT identique (binary64-decimal-v1) : aucun UPDATE, révision et
  updated_at inchangés ; PUT effectif : UN UPDATE conditionnel (id,
  user_id, révision attendue) qui incrémente la révision, relu par
  RETURNING ; révision maximale : RevisionExhausted (503, alerte) ;
- DELETE : UN DELETE conditionnel (id, user_id, révision attendue), même
  à la révision maximale ;
- résultat construit, validé et sérialisé, puis reçu inséré dans la même
  transaction ; aucune ligne retournée après des contrôles sous verrou :
  invariant rompu (rollback, 503), jamais un faux conflit.

Les flottants sont lus par float8send (8 octets IEEE 754), jamais par la
sortie texte de PostgreSQL (indépendant de extra_float_digits).

R1 PR 8 (plan READ inchangé, aucune écriture, aucun verrou métier) : une
SEULE instruction SQL métier par page ou par thèse, exécutée après
l'autorisation définitive ; sous READ COMMITTED, chaque instruction voit
un instantané cohérent pris à son début (aucune garantie entre deux
pages). Égalités, unicité, partitions, jointures, keyset et tris des
tickers sous COLLATE "C" (chaînes historiques exactes, jamais
normalisées). Bornes de restitution dans la projection : ticker rendu
seulement s'il fait au plus 512 octets, texte de thèse seulement s'il fait
au plus 131072 octets (longueur toujours projetée : valeur admissible et
valeur hors borne explicitement distinguées, jamais d'extrait) ; date
rendue seulement si elle tient dans les années 0001 à 9999 UTC, avec un
indicateur explicite (psycopg2 convertirait silencieusement ±infinity en
datetime.max / datetime.min et échouerait sur une année hors bornes, même
pour la ligne supplémentaire d'une page) ; TIMESTAMPTZ projeté en UTC par
PostgreSQL (AT TIME ZONE 'UTC'), TIMESTAMP historique tel quel. thesis_text
n'est jamais lu par la synthèse ni par la liste. Provenance : sous-requête
corrélée sur analysis_sessions (id = analysis_session_id ET même user_id
ET même ticker sous "C" ET user_id serveur) ; l'identifiant de session
n'est projeté que vérifié, jamais l'identifiant brut de la thèse. Une
valeur non restituable lève ConsultationUnrepresentable (503 et alerte
interne sans détail) ; aucune correction, aucune réparation.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import portfolio_fingerprint as fp
from core.authz_f2 import portfolio_results as results
from core.authz_f2 import portfolio_values as pv


class _Absent:
    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()


class _Conflict:
    __slots__ = ()

    def __repr__(self) -> str:
        return "CONFLICT"


CONFLICT = _Conflict()  # révision attendue périmée : aucune écriture


class _IdempotencyConflict:
    __slots__ = ()

    def __repr__(self) -> str:
        return "IDEMPOTENCY_CONFLICT"


IDEMPOTENCY_CONFLICT = _IdempotencyConflict()  # même triplet, autre intention : aucune écriture


class AdapterInvariantBroken(RuntimeError):
    """État impossible constaté sous verrou : la transaction est annulée."""

    alert = None  # nom d'alerte interne journalisé par le runner (sans détail)


class RevisionExhausted(AdapterInvariantBroken):
    """Changement demandé à la révision maximale : refus (503) et alerte."""

    def __init__(self, message: str, alert: str = "level_revision_exhausted"):
        super().__init__(message)
        self.alert = alert


class PortfolioUnavailable(AdapterInvariantBroken):
    """Reçu illisible ou incohérent, version ou clé portefeuille
    introuvable, isolation non conforme : 503, aucune réexécution."""

    def __init__(self, message: str, alert: str):
        super().__init__(message)
        self.alert = alert


class ConsultationUnrepresentable(AdapterInvariantBroken):
    """R1 PR 8 : valeur historique non restituable dans la vue fermée
    (ticker > 512 octets, texte > 131072 octets, date infinie ou hors
    représentation) : 503 pour la requête, aucune troncature, aucune
    correction, donnée intacte."""

    def __init__(self, message: str):
        super().__init__(message)
        self.alert = "consultation_value_unrepresentable"


# États FERMÉS rendus par _lock_portfolio (avant T_authorization).
@dataclass(frozen=True, slots=True)
class ReceiptFound:
    row: dict


@dataclass(frozen=True, slots=True)
class PositionFound:
    row: dict


@dataclass(frozen=True, slots=True)
class PositionMissing:
    pass


@dataclass(frozen=True, slots=True)
class CreateReady:
    pass


PORTFOLIO_STATES = (ReceiptFound, PositionFound, PositionMissing, CreateReady)
NO_BUSINESS_STATE = (type(None),)


_LOCK_PROFILE = {
    "FOR KEY SHARE": "SELECT id FROM users WHERE id = :u FOR KEY SHARE",
    "FOR UPDATE": "SELECT id FROM users WHERE id = :u FOR UPDATE",
}
_READ_PROFILE = "SELECT id, level, level_revision FROM users WHERE id = :u"
# Flottants lus en octets IEEE 754 exacts (float8send), jamais en texte.
_POSITION_COLUMNS = ("SELECT id, position_revision, ticker, float8send(quantity) AS quantity, "
                     "float8send(purchase_price) AS purchase_price, float8send(target_percent) AS target_percent, "
                     "envelope FROM portfolio_positions ")
_POSITION_RETURNING = ("RETURNING id, position_revision, ticker, float8send(quantity) AS quantity, "
                       "float8send(purchase_price) AS purchase_price, float8send(target_percent) AS target_percent, "
                       "envelope")
_LIST_POSITIONS = _POSITION_COLUMNS + "WHERE user_id = :u ORDER BY id"
_READ_POSITION = _POSITION_COLUMNS + "WHERE id = :id AND user_id = :u"
_THESIS_COLUMNS = "SELECT id, ticker, thesis_text, created_at FROM investment_theses "
_LIST_THESES = _THESIS_COLUMNS + "WHERE user_id = :u ORDER BY id"
_LIST_THESES_FOR_TICKER = _THESIS_COLUMNS + "WHERE user_id = :u AND ticker = :t ORDER BY id"
_DELIVERY_COLUMNS = "SELECT id, conversation_key, surface, status, generated_at FROM assistant_deliveries "
_READ_DELIVERY = _DELIVERY_COLUMNS + "WHERE id = :id AND user_id = :u"
_LIST_CONVERSATION_DELIVERIES = _DELIVERY_COLUMNS + "WHERE user_id = :u AND conversation_key = :k ORDER BY generated_at, id"
_TRANSACTION_ISOLATION = "SELECT current_setting('transaction_isolation')"
_LOCK_RECEIPT = ("SELECT user_id, operation_type, operation_id, fingerprint_version, fingerprint_key_version, "
                 "request_fingerprint, result_version, result_status, result_payload::text AS result_payload, "
                 "position_id FROM portfolio_write_idempotency "
                 "WHERE user_id = :u AND operation_type = :t AND operation_id = :o FOR UPDATE")
_LOCK_POSITION = _POSITION_COLUMNS + "WHERE id = :id AND user_id = :u FOR UPDATE"
_CREATE_POSITION = ("INSERT INTO portfolio_positions (user_id, ticker, quantity, purchase_price, target_percent, "
                    "envelope, created_at, updated_at) VALUES (:u, :ticker, :q, :p, :tp, :e, :at, :at) "
                    + _POSITION_RETURNING)
_REPLACE_POSITION = ("UPDATE portfolio_positions SET ticker = :ticker, quantity = :q, purchase_price = :p, "
                     "target_percent = :tp, envelope = :e, updated_at = :at, "
                     "position_revision = position_revision + 1 "
                     "WHERE id = :id AND user_id = :u AND position_revision = :r " + _POSITION_RETURNING)
_DELETE_POSITION = ("DELETE FROM portfolio_positions WHERE id = :id AND user_id = :u AND position_revision = :r "
                    "RETURNING id, position_revision")
_INSERT_RECEIPT = ("INSERT INTO portfolio_write_idempotency (user_id, operation_type, operation_id, "
                   "fingerprint_version, fingerprint_key_version, request_fingerprint, result_version, "
                   "result_status, result_payload, position_id, created_at) VALUES (:u, :t, :o, :fv, :kv, :f, "
                   ":rv, :s, CAST(:payload AS JSONB), :pid, :at)")
_UPDATE_PROFILE_LEVEL = ("UPDATE users SET level = :l, level_revision = level_revision + 1 "
                         "WHERE id = :u AND level_revision = :r RETURNING id, level, level_revision")

# R1 PR 8. Synthèse par ticker : une instruction. k = union des tickers des
# sessions in_progress et des thèses du principal ; s = session active
# retenue (started_at DESC, updated_at DESC, id DESC) et nombre de sessions
# actives du ticker ; t = dernière thèse (created_at DESC NULLS LAST, id
# DESC), sans thesis_text. Keyset appliqué APRÈS l'agrégation.
_ANALYSIS_OVERVIEW_COLUMNS = (
    "SELECT CASE WHEN octet_length(k.ticker) <= 512 THEN k.ticker END AS ticker, "
    "octet_length(k.ticker) AS ticker_bytes, s.id AS session_id, "
    "CASE WHEN octet_length(s.current_step) <= 32 THEN s.current_step END AS current_step, "
    "CASE WHEN s.updated_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00' "
    "AND s.updated_at < TIMESTAMPTZ '10000-01-01 00:00:00+00' THEN s.updated_at AT TIME ZONE 'UTC' END "
    "AS session_updated_at, (s.updated_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00' "
    "AND s.updated_at < TIMESTAMPTZ '10000-01-01 00:00:00+00') AS session_updated_at_representable, "
    "s.active_sessions, t.id AS thesis_id, "
    "CASE WHEN t.created_at >= TIMESTAMP '0001-01-01 00:00:00' AND t.created_at < TIMESTAMP '10000-01-01 00:00:00' "
    "THEN t.created_at END AS thesis_created_at, (t.created_at IS NULL OR (t.created_at >= TIMESTAMP "
    "'0001-01-01 00:00:00' AND t.created_at < TIMESTAMP '10000-01-01 00:00:00')) AS thesis_created_at_representable "
    "FROM (SELECT ticker COLLATE \"C\" AS ticker FROM analysis_sessions "
    "WHERE user_id = :u AND status = 'in_progress' "
    "UNION SELECT ticker COLLATE \"C\" AS ticker FROM investment_theses WHERE user_id = :u) k "
    "LEFT JOIN (SELECT ticker COLLATE \"C\" AS ticker, id, current_step, updated_at, "
    "count(*) OVER (PARTITION BY ticker COLLATE \"C\") AS active_sessions, "
    "row_number() OVER (PARTITION BY ticker COLLATE \"C\" ORDER BY started_at DESC, updated_at DESC, id DESC) "
    "AS ordinal FROM analysis_sessions WHERE user_id = :u AND status = 'in_progress') s "
    "ON s.ticker COLLATE \"C\" = k.ticker COLLATE \"C\" AND s.ordinal = 1 "
    "LEFT JOIN (SELECT ticker COLLATE \"C\" AS ticker, id, created_at, "
    "row_number() OVER (PARTITION BY ticker COLLATE \"C\" ORDER BY created_at DESC NULLS LAST, id DESC) "
    "AS ordinal FROM investment_theses WHERE user_id = :u) t "
    "ON t.ticker COLLATE \"C\" = k.ticker COLLATE \"C\" AND t.ordinal = 1 ")
_ANALYSIS_OVERVIEW_PAGE = {
    "first": _ANALYSIS_OVERVIEW_COLUMNS + "ORDER BY k.ticker COLLATE \"C\" ASC LIMIT :n",
    "after": _ANALYSIS_OVERVIEW_COLUMNS + "WHERE k.ticker COLLATE \"C\" > CAST(:a AS text) COLLATE \"C\" "
                                          "ORDER BY k.ticker COLLATE \"C\" ASC LIMIT :n",
}
# Historique des thèses : jamais thesis_text ; provenance vérifiée par
# sous-requête corrélée (même id, même utilisateur, même ticker sous "C").
_THESES_PAGE_COLUMNS = (
    "SELECT id, CASE WHEN octet_length(ticker) <= 512 THEN ticker END AS ticker, "
    "octet_length(ticker) AS ticker_bytes, "
    "CASE WHEN created_at >= TIMESTAMP '0001-01-01 00:00:00' AND created_at < TIMESTAMP '10000-01-01 00:00:00' "
    "THEN created_at END AS created_at, (created_at IS NULL OR (created_at >= TIMESTAMP '0001-01-01 00:00:00' "
    "AND created_at < TIMESTAMP '10000-01-01 00:00:00')) AS created_at_representable, "
    "EXISTS (SELECT 1 FROM analysis_sessions s WHERE s.id = investment_theses.analysis_session_id "
    "AND s.user_id = investment_theses.user_id AND s.user_id = :u "
    "AND s.ticker COLLATE \"C\" = investment_theses.ticker COLLATE \"C\") AS verified FROM investment_theses ")
# Prédicats keyset fermés (created_at DESC NULLS LAST, id DESC) : dernière
# date non nulle, ou dernière date nulle ; filtre ticker exact sous "C".
# ORDER BY qualifié : un nom nu y désignerait d'abord la colonne de SORTIE
# created_at (projection bornée, NULL pour une date non représentable),
# alors que WHERE et le contrat trient sur la colonne réelle.
_THESES_PAGE = {
    "first": _THESES_PAGE_COLUMNS + "WHERE user_id = :u "
                                    "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n",
    "first+ticker": _THESES_PAGE_COLUMNS + "WHERE user_id = :u AND ticker COLLATE \"C\" = CAST(:t AS text) COLLATE \"C\" "
                                           "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n",
    "after_date": _THESES_PAGE_COLUMNS + "WHERE user_id = :u "
                                         "AND (created_at < :d OR (created_at = :d AND id < :i) OR created_at IS NULL) "
                                         "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n",
    "after_date+ticker": _THESES_PAGE_COLUMNS + "WHERE user_id = :u AND ticker COLLATE \"C\" = CAST(:t AS text) "
                                                "COLLATE \"C\" "
                                                "AND (created_at < :d OR (created_at = :d AND id < :i) OR created_at IS NULL) "
                                                "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n",
    "after_null": _THESES_PAGE_COLUMNS + "WHERE user_id = :u AND (created_at IS NULL AND id < :i) "
                                         "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n",
    "after_null+ticker": _THESES_PAGE_COLUMNS + "WHERE user_id = :u AND ticker COLLATE \"C\" = CAST(:t AS text) "
                                                "COLLATE \"C\" AND (created_at IS NULL AND id < :i) "
                                                "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n",
}
# Détail : texte transféré seulement s'il respecte la borne brute.
_READ_THESIS = (
    "SELECT id, CASE WHEN octet_length(ticker) <= 512 THEN ticker END AS ticker, "
    "octet_length(ticker) AS ticker_bytes, "
    "CASE WHEN octet_length(thesis_text) <= 131072 THEN thesis_text END AS thesis_text, "
    "octet_length(thesis_text) AS thesis_text_bytes, "
    "CASE WHEN created_at >= TIMESTAMP '0001-01-01 00:00:00' AND created_at < TIMESTAMP '10000-01-01 00:00:00' "
    "THEN created_at END AS created_at, (created_at IS NULL OR (created_at >= TIMESTAMP '0001-01-01 00:00:00' "
    "AND created_at < TIMESTAMP '10000-01-01 00:00:00')) AS created_at_representable, "
    "(SELECT s.id FROM analysis_sessions s WHERE s.id = investment_theses.analysis_session_id "
    "AND s.user_id = investment_theses.user_id AND s.user_id = :u "
    "AND s.ticker COLLATE \"C\" = investment_theses.ticker COLLATE \"C\") AS verified_session_id "
    "FROM investment_theses WHERE id = :id AND user_id = :u")


def _rows(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params).mappings().all()


def _first(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params).mappings().first()


def lock_profile(conn, user_id: str, plan: c.LockPlan) -> bool:
    """Premier verrou de la phase B (rang users). False : profil absent."""
    return _first(conn, _LOCK_PROFILE[plan.users_lock], u=user_id) is not None


def _float(raw):
    return None if raw is None else pv.float_from_float8send(raw)


def _position(row) -> c.PositionView:
    """Vue diagnostique d'une ligne lue ; structure SQL impossible (type,
    révision, quantité absente) : AdapterInvariantBroken (503). Les valeurs
    historiques hors domaine restent lisibles, jamais corrigées."""
    try:
        if type(row["ticker"]) is not str or (row["envelope"] is not None and type(row["envelope"]) is not str):
            raise TypeError("ticker ou enveloppe")
        return c.position_view(row["id"], row["position_revision"], row["ticker"], _float(row["quantity"]),
                               _float(row["purchase_price"]), _float(row["target_percent"]), row["envelope"])
    except (KeyError, TypeError, ValueError):
        raise AdapterInvariantBroken("position incohérente") from None


def _thesis(row) -> c.ThesisView:
    return c.ThesisView(row["id"], row["ticker"], row["thesis_text"], row["created_at"])


def _delivery(row) -> c.DeliveryView:
    return c.DeliveryView(row["id"], row["conversation_key"], row["surface"], row["status"], row["generated_at"])


# --------------------------------------------------------------------------
# Verrous métiers (avant T_authorization) et exécution (après la décision)
# --------------------------------------------------------------------------

def _no_business_lock(conn, owner_id, command):
    return None


def _profile(row, user_id) -> c.ProfileView:
    """Vue du profil, ou AdapterInvariantBroken : profil absent (pourtant
    verrouillé par lock_profile dans cette transaction), autre identifiant,
    niveau inconnu ou révision hors contrat. Aucune correction silencieuse."""
    if (row is None or row["id"] != user_id or not c.valid_level(row["level"])
            or not c.valid_revision(row["level_revision"])):
        raise AdapterInvariantBroken("profil incohérent")
    return c.ProfileView(row["id"], row["level"], row["level_revision"])


def _read_profile(conn, ctx, command, locked):
    return _profile(_first(conn, _READ_PROFILE, u=ctx.principal.user_id), ctx.principal.user_id)


def _list_positions(conn, ctx, command, locked):
    return tuple(_position(r) for r in _rows(conn, _LIST_POSITIONS, u=ctx.principal.user_id))


def _read_position(conn, ctx, command, locked):
    row = _first(conn, _READ_POSITION, id=command.position_id, u=ctx.principal.user_id)
    return ABSENT if row is None else _position(row)


def _list_theses(conn, ctx, command, locked):
    return tuple(_thesis(r) for r in _rows(conn, _LIST_THESES, u=ctx.principal.user_id))


def _list_theses_for_ticker(conn, ctx, command, locked):
    return tuple(_thesis(r) for r in _rows(conn, _LIST_THESES_FOR_TICKER, u=ctx.principal.user_id,
                                           t=command.ticker))


def _read_delivery(conn, ctx, command, locked):
    row = _first(conn, _READ_DELIVERY, id=command.assistant_turn_id, u=ctx.principal.user_id)
    return ABSENT if row is None else _delivery(row)


def _list_conversation_deliveries(conn, ctx, command, locked):
    return tuple(_delivery(r) for r in _rows(conn, _LIST_CONVERSATION_DELIVERIES, u=ctx.principal.user_id,
                                             k=command.conversation_key))


# --------------------------------------------------------------------------
# R1 PR 8 : synthèse des analyses et historique des thèses (plan READ)
# --------------------------------------------------------------------------

def _restorable_ticker(row) -> str:
    """Ticker rendu par la projection bornée ; hors borne : non restituable."""
    if type(row["ticker_bytes"]) is not int or row["ticker_bytes"] > c.TICKER_MAX_BYTES:
        raise ConsultationUnrepresentable("ticker non restituable")
    if type(row["ticker"]) is not str:
        raise AdapterInvariantBroken("ticker incohérent")
    return row["ticker"]


def _representable(row, value: str, flag: str):
    """Date projetée seulement si elle tient dans le contrat public (années
    0001 à 9999 UTC ; ±infinity et dates hors bornes exclues), avec son
    indicateur (vrai aussi pour NULL) ; jamais lue ni convertie sinon."""
    if row[flag] is not True:
        raise ConsultationUnrepresentable("date non restituable")
    return row[value]


def _session_instant(row):
    """TIMESTAMPTZ projeté en UTC par PostgreSQL (AT TIME ZONE 'UTC') :
    indépendant du fuseau de la connexion."""
    at = _representable(row, "session_updated_at", "session_updated_at_representable")
    if type(at) is not datetime or at.tzinfo is not None:
        raise AdapterInvariantBroken("date de session incohérente")
    return at.replace(tzinfo=timezone.utc)


def _overview_entry(row) -> c.AnalysisOverviewEntry:
    """Session active prioritaire, sinon dernière thèse ; jamais d'étape de
    substitution ni d'état completed inféré d'une thèse."""
    try:
        ticker = _restorable_ticker(row)
        if row["session_id"] is not None:
            step = row["current_step"] if row["current_step"] in c.ANALYSIS_STEPS else None
            return c.AnalysisOverviewEntry(ticker, c.IN_PROGRESS, row["session_id"], step,
                                           _session_instant(row), None,
                                           c.overview_diagnostics(row["active_sessions"], step))
        if row["thesis_id"] is None:
            raise AdapterInvariantBroken("ticker sans session active ni thèse")
        return c.AnalysisOverviewEntry(ticker, c.THESIS_AVAILABLE, None, None,
                                       _representable(row, "thesis_created_at", "thesis_created_at_representable"),
                                       row["thesis_id"], ())
    except (KeyError, TypeError, ValueError):
        raise AdapterInvariantBroken("synthèse incohérente") from None


def _analysis_overview_page(conn, ctx, command, locked):
    params = {"u": ctx.principal.user_id, "n": c.page_fetch_size(command.limit)}
    if command.after_ticker is None:
        found = _rows(conn, _ANALYSIS_OVERVIEW_PAGE["first"], **params)
    else:
        found = _rows(conn, _ANALYSIS_OVERVIEW_PAGE["after"], a=command.after_ticker, **params)
    # La ligne supplémentaire ne dit que l'existence d'une page suivante.
    entries = tuple(_overview_entry(r) for r in found[:command.limit])
    try:
        return c.AnalysisOverviewPage(entries, len(found) > command.limit)
    except ValueError:
        raise AdapterInvariantBroken("page de synthèse incohérente") from None


def _thesis_entry(row) -> c.ThesisListEntry:
    try:
        ticker = _restorable_ticker(row)
        if type(row["verified"]) is not bool:
            raise TypeError("provenance")
        return c.ThesisListEntry(row["id"], ticker, _representable(row, "created_at", "created_at_representable"),
                                 c.VERIFIED if row["verified"] else c.UNVERIFIED)
    except (KeyError, TypeError, ValueError):
        raise AdapterInvariantBroken("thèse incohérente") from None


def _theses_statement(command) -> str:
    """Clé de l'instruction fermée : première page, après une date non
    nulle ou après une date nulle ; avec ou sans filtre ticker."""
    filtered = command.ticker is not None
    if command.after_thesis_id is None:
        return "first+ticker" if filtered else "first"
    if command.after_created_at is None:
        return "after_null+ticker" if filtered else "after_null"
    return "after_date+ticker" if filtered else "after_date"


def _theses_page(conn, ctx, command, locked):
    params = {"u": ctx.principal.user_id, "n": c.page_fetch_size(command.limit)}
    if command.ticker is not None:
        params["t"] = command.ticker
    if command.after_thesis_id is not None:
        params["i"] = command.after_thesis_id
    if command.after_created_at is not None:
        params["d"] = command.after_created_at  # TIMESTAMP exact, sans conversion de fuseau
    found = _rows(conn, _THESES_PAGE[_theses_statement(command)], **params)
    entries = tuple(_thesis_entry(r) for r in found[:command.limit])
    try:
        return c.ThesesPage(entries, len(found) > command.limit)
    except ValueError:
        raise AdapterInvariantBroken("page de thèses incohérente") from None


def _read_thesis(conn, ctx, command, locked):
    row = _first(conn, _READ_THESIS, id=command.thesis_id, u=ctx.principal.user_id)
    if row is None:
        return ABSENT  # étrangère ou inexistante : indiscernables
    try:
        ticker = _restorable_ticker(row)
        if type(row["thesis_text_bytes"]) is not int or row["thesis_text_bytes"] > c.THESIS_TEXT_MAX_BYTES:
            raise ConsultationUnrepresentable("texte non restituable")
        if type(row["thesis_text"]) is not str:
            raise AdapterInvariantBroken("texte incohérent")
        return c.ThesisDetailView(row["id"], ticker, row["thesis_text"],
                                  _representable(row, "created_at", "created_at_representable"), row["verified_session_id"])
    except (KeyError, TypeError, ValueError):
        raise AdapterInvariantBroken("thèse incohérente") from None



def _update_profile_level(conn, ctx, command, locked):
    user_id = ctx.principal.user_id
    current = _profile(_first(conn, _READ_PROFILE, u=user_id), user_id)  # ligne users FOR UPDATE (plan)
    if current.level_revision != command.expected_revision:
        return CONFLICT  # la révision précède toujours le niveau
    if current.level == command.level:
        return current  # aucun UPDATE, aucun incrément
    if current.level_revision == c.MAX_LEVEL_REVISION:
        raise RevisionExhausted("révision maximale atteinte")
    updated = _first(conn, _UPDATE_PROFILE_LEVEL, l=command.level, u=user_id, r=command.expected_revision)
    if updated is None:
        raise AdapterInvariantBroken("écriture conditionnelle sans ligne sous verrou")
    view = _profile(updated, user_id)
    if view.level != command.level or view.level_revision != c.next_revision(current.level_revision):
        raise AdapterInvariantBroken("valeurs écrites inattendues")
    return view


# --------------------------------------------------------------------------
# R1 PR 7 : portefeuille versionné et idempotent (plan PORTFOLIO_MUTATION)
# --------------------------------------------------------------------------

def require_read_committed(conn) -> None:
    """Début de la phase B d'un plan qui l'exige : isolation réellement
    active lue dans la transaction ; non conforme : refus fermé (503)."""
    if conn.execute(sa.text(_TRANSACTION_ISOLATION)).scalar_one() != "read committed":
        raise PortfolioUnavailable("isolation non conforme", "portfolio_isolation_refused")


OPERATION_TYPES = MappingProxyType({
    c.CreatePortfolioPosition: fp.CREATE,
    c.ReplacePortfolioPosition: fp.UPDATE,
    c.DeletePortfolioPosition: fp.DELETE,
})


def _lock_portfolio(conn, owner_id, command):
    """Reçu du triplet, puis (sans reçu) position propre ; aucune décision."""
    receipt = _first(conn, _LOCK_RECEIPT, u=owner_id, t=OPERATION_TYPES[type(command)], o=command.operation_id)
    if receipt is not None:
        return ReceiptFound(dict(receipt))
    if type(command) is c.CreatePortfolioPosition:
        return CreateReady()
    row = _first(conn, _LOCK_POSITION, id=command.position_id, u=owner_id)
    return PositionMissing() if row is None else PositionFound(dict(row))


def _business(command) -> tuple:
    if type(command) is c.DeletePortfolioPosition:
        return None, None, None, None, None
    return command.ticker, command.quantity, command.purchase_price, command.target_percent, command.envelope


def _fingerprint_fields(user_id: str, operation_type: str, command) -> dict:
    ticker, quantity, purchase_price, target_percent, envelope = _business(command)
    return {"user_id": user_id, "operation_type": operation_type, "operation_id": command.operation_id,
            "position_id": getattr(command, "position_id", None),
            "expected_revision": getattr(command, "expected_revision", None), "ticker": ticker,
            "quantity": quantity, "purchase_price": purchase_price, "target_percent": target_percent,
            "envelope": envelope}


def _same_values(view, command) -> bool:
    return (view.ticker, view.quantity, view.purchase_price, view.target_percent, view.envelope) == _business(command)


def _replay(row, command, operation_type: str, fields: dict, keys):
    """Reçu existant : validé ENTIÈREMENT, puis empreinte recalculée selon
    ses versions enregistrées ; restitué tel quel ou conflit, jamais
    réexécuté."""
    try:
        if (row["user_id"] != fields["user_id"] or row["operation_type"] != operation_type
                or row["operation_id"] != command.operation_id
                or type(row["fingerprint_version"]) is not str or type(row["fingerprint_key_version"]) is not str
                or type(row["request_fingerprint"]) is not str
                or not fp.FINGERPRINT_RE.fullmatch(row["request_fingerprint"])):
            raise results.ResultInvalid("reçu incohérent")
        renderer = results.RENDERERS.get(row["result_version"]) if type(row["result_version"]) is str else None
        if renderer is None:
            raise results.ResultInvalid("version de résultat inconnue")
        result = renderer(operation_type, command.operation_id, row["position_id"],
                          results.parse_json(row["result_payload"]))
        if type(row["result_status"]) is not int or row["result_status"] != result.status:
            raise results.ResultInvalid("statut incohérent")
    except (KeyError, results.ResultInvalid):
        raise PortfolioUnavailable("reçu invalide", "portfolio_receipt_invalid") from None
    try:
        computed = fp.fingerprint(keys, row["fingerprint_version"], row["fingerprint_key_version"], **fields)
    except fp.UnknownPortfolioKey:
        raise PortfolioUnavailable("version ou clé portefeuille introuvable", "portfolio_key_unavailable") from None
    if not fp.same_fingerprint(computed, row["request_fingerprint"]):
        return IDEMPOTENCY_CONFLICT
    # Même intention : le résultat enregistré doit lui correspondre.
    if operation_type == fp.DELETE:
        coherent = (result.position_id == command.position_id
                    and result.deleted_revision == command.expected_revision)
    else:
        coherent = _same_values(result.view, command) and result.view.data_status == pv.VALID
        if operation_type == fp.UPDATE:
            coherent = coherent and result.position_id == command.position_id and (
                result.view.position_revision in c.replace_revisions(command.expected_revision))
    if not coherent:
        raise PortfolioUnavailable("reçu incohérent avec son intention", "portfolio_receipt_invalid")
    return result


def _write_values(command, at) -> dict:
    return {"ticker": command.ticker, "q": pv.to_float(command.quantity), "p": pv.to_float(command.purchase_price),
            "tp": pv.to_float(command.target_percent), "e": command.envelope, "at": at}


def _written(row, command, revision: int, position_id=None) -> c.PositionView:
    if row is None:
        raise AdapterInvariantBroken("écriture conditionnelle sans ligne sous verrou")
    view = _position(row)
    if (view.position_revision != revision or not _same_values(view, command)
            or (position_id is not None and view.position_id != position_id)):
        raise AdapterInvariantBroken("valeurs écrites inattendues")
    return view


def _write_portfolio(conn, ctx, command, state, keys, hook):
    user_id = ctx.principal.user_id
    operation_type = OPERATION_TYPES[type(command)]
    fields = _fingerprint_fields(user_id, operation_type, command)
    kind = type(state)
    if kind is ReceiptFound:
        return _replay(state.row, command, operation_type, fields, keys)
    if kind is PositionMissing and operation_type != fp.CREATE:
        return ABSENT  # aucun reçu : absente ou étrangère, indiscernables
    at = ctx.authorized_at.astimezone(timezone.utc).replace(tzinfo=None)  # colonnes historiques sans fuseau
    view = deleted_revision = None
    if kind is CreateReady and operation_type == fp.CREATE:
        view = _written(_first(conn, _CREATE_POSITION, u=user_id, **_write_values(command, at)), command, 0)
        position_id = view.position_id
        hook("position_written")
    elif kind is PositionFound and operation_type != fp.CREATE:
        current = _position(state.row)
        position_id = command.position_id
        if current.position_id != position_id:
            raise AdapterInvariantBroken("position verrouillée inattendue")
        if current.position_revision != command.expected_revision:
            return CONFLICT  # la révision précède toujours les valeurs
        if operation_type == fp.DELETE:
            deleted = _first(conn, _DELETE_POSITION, id=position_id, u=user_id, r=command.expected_revision)
            if deleted is None or (deleted["id"], deleted["position_revision"]) != (position_id,
                                                                                    command.expected_revision):
                raise AdapterInvariantBroken("suppression conditionnelle sans ligne sous verrou")
            deleted_revision = command.expected_revision
            hook("position_written")
        elif _same_values(current, command):
            view = current  # aucun UPDATE, aucun incrément, updated_at inchangé
        else:
            if current.position_revision == c.MAX_POSITION_REVISION:
                raise RevisionExhausted("révision maximale atteinte", "position_revision_exhausted")
            row = _first(conn, _REPLACE_POSITION, id=position_id, u=user_id, r=command.expected_revision,
                         **_write_values(command, at))
            view = _written(row, command, c.next_revision(current.position_revision), position_id)
            hook("position_written")
    else:
        raise AdapterInvariantBroken("état verrouillé inattendu")
    try:
        computed = fp.fingerprint(keys, fp.FINGERPRINT_VERSION, keys.current_version, **fields)
        payload = results.build_payload(operation_type, command.operation_id, position_id, view=view,
                                        deleted_revision=deleted_revision)
        result = results.render_v1(operation_type, command.operation_id, position_id, payload)
    except fp.UnknownPortfolioKey:
        raise PortfolioUnavailable("clé portefeuille courante introuvable", "portfolio_key_unavailable") from None
    except results.ResultInvalid:
        raise AdapterInvariantBroken("résultat hors contrat") from None
    hook("receipt_ready")
    conn.execute(sa.text(_INSERT_RECEIPT), {
        "u": user_id, "t": operation_type, "o": command.operation_id, "fv": fp.FINGERPRINT_VERSION,
        "kv": keys.current_version, "f": computed, "rv": results.RESULT_VERSION, "s": result.status,
        "payload": result.body.decode("utf-8"), "pid": position_id, "at": ctx.authorized_at})
    hook("receipt_written")
    return result


@dataclass(frozen=True)
class Spec:
    plan: c.LockPlan
    lock: object     # (conn, owner_id, command) -> état verrouillé ; avant T_authorization
    execute: object  # (conn, ctx, command, état[, keys, hook]) -> vue | ABSENT | CONFLICT | ... ; après la décision
    states: tuple = NO_BUSINESS_STATE  # types EXACTS d'état verrouillé admis (registre fermé)
    keyed: bool = False  # True : reçoit le trousseau portefeuille et les points d'interruption


# Registre FERMÉ : type exact de commande -> plan et adaptateurs. Toute
# commande absente (ou sous-classe) est refusée par le runner.
REGISTRY = MappingProxyType({
    c.ReadProfile: Spec(c.READ, _no_business_lock, _read_profile),
    c.ListPositions: Spec(c.READ, _no_business_lock, _list_positions),
    c.ReadPosition: Spec(c.READ, _no_business_lock, _read_position),
    c.ListTheses: Spec(c.READ, _no_business_lock, _list_theses),
    c.ListThesesForTicker: Spec(c.READ, _no_business_lock, _list_theses_for_ticker),
    c.ReadDelivery: Spec(c.READ, _no_business_lock, _read_delivery),
    c.ListConversationDeliveries: Spec(c.READ, _no_business_lock, _list_conversation_deliveries),
    c.UpdateProfileLevel: Spec(c.PROFILE_MUTATION, _no_business_lock, _update_profile_level),
    c.CreatePortfolioPosition: Spec(c.PORTFOLIO_MUTATION, _lock_portfolio, _write_portfolio, PORTFOLIO_STATES, True),
    c.ReplacePortfolioPosition: Spec(c.PORTFOLIO_MUTATION, _lock_portfolio, _write_portfolio, PORTFOLIO_STATES,
                                     True),
    c.DeletePortfolioPosition: Spec(c.PORTFOLIO_MUTATION, _lock_portfolio, _write_portfolio, PORTFOLIO_STATES, True),
    # R1 PR 8 : lectures sous le plan READ, sans verrou métier.
    c.ListAnalysisOverviewPage: Spec(c.READ, _no_business_lock, _analysis_overview_page),
    c.ListThesesPage: Spec(c.READ, _no_business_lock, _theses_page),
    c.ReadThesis: Spec(c.READ, _no_business_lock, _read_thesis),
})
