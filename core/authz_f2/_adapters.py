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
| lock_profile | users | id | plan (FOR KEY SHARE / FOR UPDATE / FOR NO KEY UPDATE) |
| _read_profile | users | id (level ET level_revision, une requête) | - |
| _list_positions | portfolio_positions | user_id | - |
| _read_position | portfolio_positions | id ET user_id | - |
| _list_theses | investment_theses | user_id | - |
| _list_theses_for_ticker | investment_theses | user_id ET ticker | - |
| _read_delivery | assistant_deliveries | id ET user_id | - |
| _list_conversation_deliveries | assistant_deliveries | user_id ET conversation_key | - |
| _update_profile_level | users | id ET level_revision attendue | (ligne déjà verrouillée, plan PROFILE_MUTATION) |
| require_read_committed | - | current_setting('transaction_isolation') | - (plans PORTFOLIO_MUTATION, DECRYPTAGE_MUTATION) |
| _lock_portfolio (reçu) | portfolio_write_idempotency | user_id ET operation_type ET operation_id | FOR UPDATE |
| _lock_portfolio (position, PUT / DELETE sans reçu) | portfolio_positions | id ET user_id | FOR UPDATE |
| _create_position | portfolio_positions | INSERT, user_id serveur | (users déjà FOR UPDATE) |
| _replace_position | portfolio_positions | id ET user_id ET position_revision attendue | (ligne déjà verrouillée) |
| _delete_position | portfolio_positions | id ET user_id ET position_revision attendue | (ligne déjà verrouillée) |
| _insert_receipt | portfolio_write_idempotency | INSERT, user_id serveur | (users déjà FOR UPDATE) |
| _analysis_overview_page (R1 PR 8) | analysis_sessions + investment_theses | user_id (chaque sous-requête), keyset ticker | - |
| _theses_page (R1 PR 8) | investment_theses (+ analysis_sessions pour la provenance) | user_id [ET ticker], keyset | - |
| _read_thesis (R1 PR 8) | investment_theses (+ analysis_sessions pour la provenance) | id ET user_id | - |
| _register_conversation (R1 PR 9-B1) | conversation_identities | lecture : conversation_key (propriété projetée en booléen pour :u) ; INSERT en SAVEPOINT, user_id serveur | - (FK users : KEY SHARE, compatible) |
| _reserve_turn : verrou | decryptage_turn_reservations | user_id ET conversation_key ET surface ET source_user_turn_id ET delivery_ordinal | FOR UPDATE (après T_authorization) |
| _reserve_turn / _record_attempt_failure : horloge | - | clock_timestamp() (T_lease_decision, après le verrou) | - |
| _reserve_turn / _record_attempt_failure : livraison historique | assistant_deliveries | user_id ET identité complète du tour | - |
| _reserve_turn : livraison liée (finalized) | assistant_deliveries | id ET user_id | - |
| _insert_reservation | decryptage_turn_reservations | INSERT en SAVEPOINT, user_id serveur | (users déjà FOR NO KEY UPDATE) |
| _grant_lease / _block_reservation / _release_lease | decryptage_turn_reservations | id ET user_id ET state reserved ET epoch [ET compteur / jeton haché / échéance] | (ligne déjà verrouillée) |
| _record_attempt_failure : verrou | decryptage_turn_reservations | id ET user_id | FOR UPDATE (après T_authorization) |

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

R1 PR 9-A : verrou users FOR NO KEY UPDATE du plan DECRYPTAGE_MUTATION
(même instruction fermée, autre mode) ; require_read_committed reçoit le
plan pour journaliser une alerte propre (portfolio_isolation_refused
inchangée). Aucune commande ni instruction métier nouvelle ici : les
opérations Décrypter passent par le pont ORM privé
core.authz_f2._decryptage_orm_bridge, absent du registre de production.

R1 PR 9-B1 (plan DECRYPTAGE_MUTATION INCHANGÉ, spec.lock =
_no_business_lock : aucun préverrouillage avant T_authorization) : deux
commandes, ReserveDecryptageTurn et RecordDecryptageAttemptFailure, dont
l'exécution reçoit le trousseau Décrypter DÉDIÉ (Spec.decryptage_keyed,
distinct de Spec.keyed qui désigne exclusivement le trousseau
portefeuille). Les verrous métier sont pris par l'adaptateur d'exécution
APRÈS l'autorisation positive, dans cet ordre : (conversation : lecture,
puis INSERT en SAVEPOINT si absente) -> réservation propre FOR UPDATE (ou
INSERT en SAVEPOINT si absente) -> T_lease_decision = clock_timestamp()
(instruction distincte, après le verrou) -> lectures de livraison
historique (sans verrou) -> une seule mutation conditionnelle. Aucune
AnalysisSession, aucune livraison, aucune ligne cognitive n'est
verrouillée ni écrite.

Conversation (E1.2) : seconde écriture applicative de
conversation_identities, inventoriée ici, aux mêmes règles que
core.interaction_identity : propriété lue avant création ; collision
résoluble SEULEMENT si SQLSTATE 23505 ET contrainte exactement
conversation_identities_pkey ou uq_conversation_identities_key_owner ;
retour au SAVEPOINT, gagnant relu par une NOUVELLE instruction (READ
COMMITTED), propriétaire vérifié ; même propriétaire : idempotent ; autre :
ABSENT ; introuvable : refus fermé. Toute autre erreur est propagée.

Réservation : INSERT en SAVEPOINT ; seule la violation 23505 de
uq_decryptage_turn_reservations_turn est traduite (retour au SAVEPOINT,
relecture verrouillée par une nouvelle instruction, puis même décision
qu'un retry) ; toute autre contrainte est propagée. Une réservation
existante est revérifiée avec ses versions ENREGISTRÉES (empreinte
divergente : IDEMPOTENCY_CONFLICT, rien n'est écrit ; version ou clé
introuvable : DecryptageUnavailable, 503). Toutes les mutations portent
state = 'reserved' et l'epoch lu sous verrou : une ligne terminale n'est
jamais modifiée ; identité, empreinte, versions et created_at ne sont
jamais réécrits. Une capacité périmée (état, epoch, jeton ou échéance)
rend CONFLICT : rien n'est écrit.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_fingerprint as dfp
from core.authz_f2 import decryptage_reservation as dr
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
    # R1 PR 9-A : plan DECRYPTAGE_MUTATION.
    "FOR NO KEY UPDATE": "SELECT id FROM users WHERE id = :u FOR NO KEY UPDATE",
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

# Alerte interne d'une isolation refusée, par plan qui exige READ COMMITTED.
ISOLATION_ALERTS = MappingProxyType({
    c.PORTFOLIO_MUTATION.name: "portfolio_isolation_refused",
    c.DECRYPTAGE_MUTATION.name: "decryptage_isolation_refused",  # R1 PR 9-A
})


def require_read_committed(conn, plan: c.LockPlan) -> None:
    """Début de la phase B d'un plan qui l'exige : isolation réellement
    active lue dans la transaction ; non conforme : refus fermé (503),
    alerte propre au plan."""
    if conn.execute(sa.text(_TRANSACTION_ISOLATION)).scalar_one() != "read committed":
        raise PortfolioUnavailable("isolation non conforme", ISOLATION_ALERTS[plan.name])


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


# --------------------------------------------------------------------------
# R1 PR 9-B1 : réservation durable des tours Décrypter (plan DECRYPTAGE_MUTATION)
# --------------------------------------------------------------------------

CONVERSATION_IDENTITY_CONSTRAINTS = ("conversation_identities_pkey", "uq_conversation_identities_key_owner")
RESERVATION_TURN_CONSTRAINT = "uq_decryptage_turn_reservations_turn"
UNIQUE_VIOLATION = "23505"
LEASE_SECONDS = int(dr.LEASE_DURATION.total_seconds())

_CONVERSATION_OWNED = "SELECT user_id = :u AS owned FROM conversation_identities WHERE conversation_key = :k"
_REGISTER_CONVERSATION = ("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                          "VALUES (:k, :u, :at)")
_RESERVATION_LIST_COLUMNS = ("id, user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal, "
                       "fingerprint_version, fingerprint_key_version, request_fingerprint, state, blocked_reason, "
                       "lease_epoch, lease_token_hash, lease_expires_at, attempts_used, retry_not_before_at, "
                       "assistant_delivery_id, finalized_at")
_RESERVATION_COLUMNS = "SELECT " + _RESERVATION_LIST_COLUMNS + " FROM decryptage_turn_reservations "
_RESERVATION_RETURNING = "RETURNING " + _RESERVATION_LIST_COLUMNS
_LOCK_RESERVATION_TURN = (_RESERVATION_COLUMNS + "WHERE user_id = :u AND conversation_key = :k AND surface = :s "
                          "AND source_user_turn_id = :t AND delivery_ordinal = :o FOR UPDATE")
_LOCK_RESERVATION = _RESERVATION_COLUMNS + "WHERE id = :id AND user_id = :u FOR UPDATE"
# T_lease_decision : instruction DISTINCTE, exécutée après l'obtention du verrou.
_LEASE_DECISION_CLOCK = "SELECT clock_timestamp()"
_LEGACY_DELIVERY = ("SELECT id FROM assistant_deliveries WHERE user_id = :u AND conversation_key = :k "
                    "AND surface = :s AND source_user_turn_id = :t AND delivery_ordinal = :o")
_LINKED_DELIVERY = ("SELECT id, conversation_key, surface, source_user_turn_id, delivery_ordinal "
                    "FROM assistant_deliveries WHERE id = :id AND user_id = :u")
_INSERT_RESERVATION = {
    # Première attribution : epoch 1, une tentative, jeton haché, échéance
    # = T_lease_decision + durée figée (arithmétique PostgreSQL).
    "leased": ("INSERT INTO decryptage_turn_reservations (id, user_id, conversation_key, surface, "
               "source_user_turn_id, delivery_ordinal, fingerprint_version, fingerprint_key_version, "
               "request_fingerprint, state, lease_epoch, lease_token_hash, lease_expires_at, attempts_used, "
               "created_at, updated_at) VALUES (:id, :u, :k, :s, :t, :o, :fv, :kv, :f, 'reserved', 1, :h, "
               "CAST(:at AS timestamptz) + make_interval(secs => :d), 1, :at, :at) " + _RESERVATION_RETURNING),
    # Livraison historique préexistante : blocage direct, aucun bail.
    "blocked": ("INSERT INTO decryptage_turn_reservations (id, user_id, conversation_key, surface, "
                "source_user_turn_id, delivery_ordinal, fingerprint_version, fingerprint_key_version, "
                "request_fingerprint, state, blocked_reason, lease_epoch, attempts_used, created_at, updated_at) "
                "VALUES (:id, :u, :k, :s, :t, :o, :fv, :kv, :f, 'blocked', 'legacy_collision', 0, 0, :at, :at) "
                + _RESERVATION_RETURNING),
}
_GRANT_LEASE = ("UPDATE decryptage_turn_reservations SET lease_epoch = lease_epoch + 1, "
                "attempts_used = attempts_used + 1, lease_token_hash = :h, "
                "lease_expires_at = CAST(:at AS timestamptz) + make_interval(secs => :d), "
                "retry_not_before_at = NULL, updated_at = :at "
                "WHERE id = :id AND user_id = :u AND state = 'reserved' AND lease_epoch = :e "
                "AND attempts_used = :a " + _RESERVATION_RETURNING)
_BLOCK_RESERVATION = ("UPDATE decryptage_turn_reservations SET state = 'blocked', blocked_reason = :reason, "
                      "lease_token_hash = NULL, lease_expires_at = NULL, retry_not_before_at = NULL, "
                      "updated_at = :at WHERE id = :id AND user_id = :u AND state = 'reserved' "
                      "AND lease_epoch = :e AND attempts_used = :a " + _RESERVATION_RETURNING)
_RELEASE_LEASE = ("UPDATE decryptage_turn_reservations SET lease_token_hash = NULL, lease_expires_at = NULL, "
                  "retry_not_before_at = CAST(:at AS timestamptz) + make_interval(secs => :b), updated_at = :at "
                  "WHERE id = :id AND user_id = :u AND state = 'reserved' AND lease_epoch = :e "
                  "AND attempts_used = :a AND lease_token_hash = :h AND lease_expires_at > :at "
                  + _RESERVATION_RETURNING)


class DecryptageUnavailable(AdapterInvariantBroken):
    """Version ou clé Décrypter introuvable, réservation ou lien incohérent,
    epoch épuisé : 503, rien n'est écrit (ROLLBACK)."""

    def __init__(self, message: str, alert: str):
        super().__init__(message)
        self.alert = alert


@dataclass(frozen=True, slots=True)
class _Reservation:
    """Ligne verrouillée, validée (aucune correction)."""
    id: uuid.UUID
    conversation_key: str
    source_user_turn_id: uuid.UUID
    fingerprint_version: str
    fingerprint_key_version: str
    request_fingerprint: str
    state: str
    blocked_reason: object
    lease_epoch: int
    lease_token_hash: object
    lease_expires_at: object
    attempts_used: int
    retry_not_before_at: object
    assistant_delivery_id: object
    finalized_at: object


def _sqlstate_and_constraint(exc) -> tuple:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "sqlstate", None), getattr(diag, "constraint_name", None)


def _owned(row) -> bool:
    if type(row["owned"]) is not bool:
        raise AdapterInvariantBroken("propriété de conversation illisible")
    return row["owned"]


def _register_conversation(conn, user_id: str, conversation_key: str, at) -> bool:
    """True : conversation du principal (lue ou créée) ; False : d'autrui."""
    found = _first(conn, _CONVERSATION_OWNED, u=user_id, k=conversation_key)
    if found is not None:
        return _owned(found)
    try:
        with conn.begin_nested():  # SAVEPOINT local : seule la collision d'identité est traduite
            conn.execute(sa.text(_REGISTER_CONVERSATION), {"k": conversation_key, "u": user_id, "at": at})
    except sa.exc.IntegrityError as exc:
        sqlstate, constraint = _sqlstate_and_constraint(exc)
        if sqlstate != UNIQUE_VIOLATION or constraint not in CONVERSATION_IDENTITY_CONSTRAINTS:
            raise
        winner = _first(conn, _CONVERSATION_OWNED, u=user_id, k=conversation_key)  # nouvelle instruction
        if winner is None:
            raise AdapterInvariantBroken("collision de conversation sans gagnant visible") from None
        return _owned(winner)
    return True


def _insert_reservation(conn, kind: str, params: dict):
    """Ligne insérée, ou None si l'identité du tour existe déjà (gagnant
    concurrent) ; toute autre erreur est propagée."""
    try:
        with conn.begin_nested():  # SAVEPOINT local : seule la collision d'identité du tour est traduite
            return _first(conn, _INSERT_RESERVATION[kind], **params)
    except sa.exc.IntegrityError as exc:
        if _sqlstate_and_constraint(exc) != (UNIQUE_VIOLATION, RESERVATION_TURN_CONSTRAINT):
            raise
        return None


def _lease_clock(conn) -> datetime:
    at = conn.execute(sa.text(_LEASE_DECISION_CLOCK)).scalar_one()
    if type(at) is not datetime or at.tzinfo is None:
        raise AdapterInvariantBroken("horloge PostgreSQL illisible")
    return at


def _aware_or_none(value) -> bool:
    return value is None or (type(value) is datetime and value.tzinfo is not None)


def _reservation(row, user_id: str, turn=None) -> _Reservation:
    """Contrôle structurel de la ligne verrouillée (défense en profondeur
    des CHECK) ; tout écart : refus fermé, jamais de réparation."""
    try:
        res = _Reservation(row["id"], row["conversation_key"], row["source_user_turn_id"],
                           row["fingerprint_version"], row["fingerprint_key_version"], row["request_fingerprint"],
                           row["state"], row["blocked_reason"], row["lease_epoch"], row["lease_token_hash"],
                           row["lease_expires_at"], row["attempts_used"], row["retry_not_before_at"],
                           row["assistant_delivery_id"], row["finalized_at"])
        valid = (type(res.id) is uuid.UUID and row["user_id"] == user_id and row["surface"] == dfp.SURFACE
                 and row["delivery_ordinal"] == dfp.DELIVERY_ORDINAL and type(res.conversation_key) is str
                 and type(res.source_user_turn_id) is uuid.UUID
                 and all(type(v) is str for v in (res.fingerprint_version, res.fingerprint_key_version,
                                                  res.request_fingerprint))
                 and type(res.lease_epoch) is int and 0 <= res.lease_epoch <= dr.MAX_EPOCH
                 and type(res.attempts_used) is int and 0 <= res.attempts_used <= dr.MAX_ATTEMPTS
                 and all(_aware_or_none(v) for v in (res.lease_expires_at, res.retry_not_before_at,
                                                     res.finalized_at))
                 and res.state in dr.STATES)
        if valid and turn is not None:
            valid = (res.conversation_key, res.source_user_turn_id) == (turn["k"], turn["t"])
        if valid and res.state == dr.RESERVED:
            leased = res.lease_token_hash is not None and res.lease_expires_at is not None
            released = res.lease_token_hash is None and res.lease_expires_at is None
            valid = (res.blocked_reason is None and res.assistant_delivery_id is None and res.lease_epoch >= 1
                     and ((leased and res.retry_not_before_at is None)
                          or (released and res.retry_not_before_at is not None)))
        elif valid and res.state == dr.BLOCKED:
            valid = res.blocked_reason in dr.BLOCKED_REASONS and res.lease_token_hash is None
        elif valid:  # finalized
            valid = type(res.assistant_delivery_id) is uuid.UUID and res.finalized_at is not None
    except KeyError:
        valid = False
    if not valid:
        raise DecryptageUnavailable("réservation incohérente", "decryptage_reservation_incoherent")
    return res


def _turn(user_id: str, conversation_key: str, source_user_turn_id: uuid.UUID) -> dict:
    return {"u": user_id, "k": conversation_key, "s": dfp.SURFACE, "t": source_user_turn_id,
            "o": dfp.DELIVERY_ORDINAL}


def _fingerprint(keys, fingerprint_version, key_version, user_id: str, command) -> str:
    question, context = command.texts
    try:
        return dfp.fingerprint(keys, fingerprint_version, key_version, user_id=user_id,
                               conversation_key=command.conversation_key,
                               source_user_turn_id=command.client_turn_id, ticker=command.ticker,
                               question=question, context=context, last_method_id=command.last_method_id)
    except dfp.UnknownDecryptageKey:
        raise DecryptageUnavailable("version ou clé Décrypter introuvable", "decryptage_key_unavailable") from None


def _mutated(row, user_id: str, res: _Reservation, state: str, epoch: int, attempts: int) -> _Reservation:
    """Ligne rendue par une mutation conditionnelle sous verrou : aucune
    ligne = invariant rompu (jamais un faux conflit)."""
    if row is None:
        raise AdapterInvariantBroken("mutation conditionnelle sans ligne sous verrou")
    out = _reservation(row, user_id)
    if (out.id, out.state, out.lease_epoch, out.attempts_used) != (res.id, state, epoch, attempts):
        raise AdapterInvariantBroken("valeurs écrites inattendues")
    return out


def _block(conn, user_id: str, res: _Reservation, reason: str, at) -> _Reservation:
    row = _first(conn, _BLOCK_RESERVATION, reason=reason, at=at, id=res.id, u=user_id, e=res.lease_epoch,
                 a=res.attempts_used)
    return _mutated(row, user_id, res, dr.BLOCKED, res.lease_epoch, res.attempts_used)


def _lease(res: _Reservation, token, attempts: int) -> dr.LeaseGranted:
    return dr.LeaseGranted(dr.DecryptageLease(res.id, res.lease_epoch, token, res.lease_expires_at,
                                              res.conversation_key, res.source_user_turn_id,
                                              dfp.DELIVERY_ORDINAL), attempts)


def _grant(conn, user_id: str, res: _Reservation, at) -> dr.LeaseGranted:
    """Nouvelle attribution : epoch et compteur + 1, nouveau jeton, échéance
    = T_lease_decision + 360 s ; jamais sur le même epoch."""
    if res.lease_epoch >= dr.MAX_EPOCH or res.attempts_used >= dr.MAX_ATTEMPTS:
        raise DecryptageUnavailable("epoch ou compteur maximal atteint", "decryptage_epoch_exhausted")
    token = dr.LeaseToken.generate()
    row = _first(conn, _GRANT_LEASE, h=token.digest(), at=at, d=LEASE_SECONDS, id=res.id, u=user_id,
                 e=res.lease_epoch, a=res.attempts_used)
    out = _mutated(row, user_id, res, dr.RESERVED, dr.next_epoch(res.lease_epoch), dr.next_attempt(res.attempts_used))
    if not token.matches(out.lease_token_hash) or out.lease_expires_at is None or out.retry_not_before_at is not None:
        raise AdapterInvariantBroken("bail écrit inattendu")
    return _lease(out, token, out.attempts_used)


def _legacy_delivery(conn, turn: dict) -> bool:
    return _first(conn, _LEGACY_DELIVERY, **turn) is not None


def _finalized(conn, user_id: str, res: _Reservation) -> dr.ReservationFinalized:
    """Lien de livraison relu (id ET user_id) et identité complète comparée :
    un état finalized manifestement incohérent n'est jamais présenté."""
    linked = _first(conn, _LINKED_DELIVERY, id=res.assistant_delivery_id, u=user_id)
    if linked is None or (linked["conversation_key"], linked["surface"], linked["source_user_turn_id"],
                          linked["delivery_ordinal"]) != (res.conversation_key, dfp.SURFACE,
                                                          res.source_user_turn_id, dfp.DELIVERY_ORDINAL):
        raise DecryptageUnavailable("réservation finalisée incohérente", "decryptage_reservation_incoherent")
    return dr.ReservationFinalized(res.id, res.assistant_delivery_id)


def _decide_reserved(conn, user_id: str, res: _Reservation, turn: dict, at):
    """Décision sur une réservation 'reserved' verrouillée, à T_lease_decision."""
    if _legacy_delivery(conn, turn):  # collision prouvée : prévaut sur le bail, le backoff et la reprise
        return dr.ReservationBlocked(_block(conn, user_id, res, dr.LEGACY_COLLISION, at).id, dr.LEGACY_COLLISION)
    if res.lease_token_hash is not None:
        if at < res.lease_expires_at:  # bail actif, même au plafond : aucun second détenteur
            return dr.ReservationInProgress(res.id, res.lease_epoch, res.lease_expires_at)
    elif at < res.retry_not_before_at:
        return dr.ReservationRetryDeferred(res.id, res.retry_not_before_at, res.attempts_used)
    if res.attempts_used >= dr.MAX_ATTEMPTS:
        return dr.ReservationBlocked(_block(conn, user_id, res, dr.ATTEMPTS_EXHAUSTED, at).id, dr.ATTEMPTS_EXHAUSTED)
    return _grant(conn, user_id, res, at)


def _reserve_turn(conn, ctx, command, locked, keys):
    user_id = ctx.principal.user_id
    if not _register_conversation(conn, user_id, command.conversation_key, ctx.authorized_at):
        return ABSENT  # conversation d'autrui : rien n'est écrit (ROLLBACK)
    turn = _turn(user_id, command.conversation_key, command.client_turn_id)
    row = _first(conn, _LOCK_RESERVATION_TURN, **turn)
    if row is None:
        at = _lease_clock(conn)
        params = {"id": uuid.uuid4(), **turn, "fv": dfp.FINGERPRINT_VERSION, "kv": keys.current_version,
                  "f": _fingerprint(keys, dfp.FINGERPRINT_VERSION, keys.current_version, user_id, command),
                  "at": at}
        if _legacy_delivery(conn, turn):
            inserted = _insert_reservation(conn, "blocked", params)
            if inserted is not None:
                created = _reservation(inserted, user_id, turn)
                if (created.state, created.blocked_reason, created.lease_epoch, created.attempts_used) != (
                        dr.BLOCKED, dr.LEGACY_COLLISION, 0, 0):
                    raise AdapterInvariantBroken("blocage écrit inattendu")
                return dr.ReservationBlocked(created.id, dr.LEGACY_COLLISION)
        else:
            token = dr.LeaseToken.generate()
            inserted = _insert_reservation(conn, "leased", {**params, "h": token.digest(), "d": LEASE_SECONDS})
            if inserted is not None:
                created = _reservation(inserted, user_id, turn)
                if (created.state, created.lease_epoch, created.attempts_used) != (dr.RESERVED, 1, 1) \
                        or not token.matches(created.lease_token_hash):
                    raise AdapterInvariantBroken("bail écrit inattendu")
                return _lease(created, token, 1)
        row = _first(conn, _LOCK_RESERVATION_TURN, **turn)  # gagnant relu par une NOUVELLE instruction
        if row is None:
            raise AdapterInvariantBroken("collision de réservation sans gagnant visible")
    at = _lease_clock(conn)  # T_lease_decision : après le verrou de la réservation
    res = _reservation(row, user_id, turn)
    computed = _fingerprint(keys, res.fingerprint_version, res.fingerprint_key_version, user_id, command)
    if not dfp.same_fingerprint(computed, res.request_fingerprint):
        return IDEMPOTENCY_CONFLICT  # autre intention pour la même identité : rien n'est écrit
    if res.state == dr.FINALIZED:
        return _finalized(conn, user_id, res)
    if res.state == dr.BLOCKED:
        return dr.ReservationBlocked(res.id, res.blocked_reason)
    return _decide_reserved(conn, user_id, res, turn, at)


def _record_attempt_failure(conn, ctx, command, locked, keys):
    user_id = ctx.principal.user_id
    row = _first(conn, _LOCK_RESERVATION, id=command.reservation_id, u=user_id)
    if row is None:
        return ABSENT  # absente ou d'autrui : indiscernables, aucun verrou sur la ligne d'autrui
    at = _lease_clock(conn)  # T_lease_decision : après le verrou
    res = _reservation(row, user_id)
    if (res.state != dr.RESERVED or res.lease_epoch != command.lease_epoch or res.lease_token_hash is None
            or not command.lease_token.matches(res.lease_token_hash) or not at < res.lease_expires_at):
        return CONFLICT  # capacité périmée (état, epoch, jeton ou échéance) : rien n'est écrit
    turn = _turn(user_id, res.conversation_key, res.source_user_turn_id)
    if _legacy_delivery(conn, turn):
        out = _block(conn, user_id, res, dr.LEGACY_COLLISION, at)
    elif res.attempts_used >= dr.MAX_ATTEMPTS:
        out = _block(conn, user_id, res, dr.ATTEMPTS_EXHAUSTED, at)
    else:
        row = _first(conn, _RELEASE_LEASE, at=at, b=int(dr.backoff_after(res.attempts_used).total_seconds()),
                     id=res.id, u=user_id, e=res.lease_epoch, a=res.attempts_used, h=res.lease_token_hash)
        out = _mutated(row, user_id, res, dr.RESERVED, res.lease_epoch, res.attempts_used)
        if out.lease_token_hash is not None or out.retry_not_before_at is None:
            raise AdapterInvariantBroken("libération écrite inattendue")
    return dr.AttemptFailureRecorded(out.id, out.state, out.blocked_reason, out.retry_not_before_at,
                                     out.attempts_used)


class SpecError(TypeError):
    """Spécification de commande hors contrat (registre impossible à construire)."""


@dataclass(frozen=True)
class Spec:
    plan: c.LockPlan
    lock: object     # (conn, owner_id, command) -> état verrouillé ; avant T_authorization
    execute: object  # (conn, ctx, command, état[, keys, hook]) -> vue | ABSENT | CONFLICT | ... ; après la décision
    states: tuple = NO_BUSINESS_STATE  # types EXACTS d'état verrouillé admis (registre fermé)
    keyed: bool = False  # True : reçoit le trousseau PORTEFEUILLE et les points d'interruption
    # R1 PR 9-B1 : True : reçoit le trousseau DÉCRYPTER (jamais les deux familles).
    decryptage_keyed: bool = False

    def __post_init__(self):
        if type(self.keyed) is not bool or type(self.decryptage_keyed) is not bool:
            raise SpecError("familles de clés : booléens attendus")
        if self.keyed and self.decryptage_keyed:
            raise SpecError("une commande ne reçoit jamais deux familles de clés")


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
    # R1 PR 9-B1 : réservation Décrypter, aucun verrou préparatoire, trousseau Décrypter.
    c.ReserveDecryptageTurn: Spec(c.DECRYPTAGE_MUTATION, _no_business_lock, _reserve_turn, decryptage_keyed=True),
    c.RecordDecryptageAttemptFailure: Spec(c.DECRYPTAGE_MUTATION, _no_business_lock, _record_attempt_failure,
                                           decryptage_keyed=True),
})
