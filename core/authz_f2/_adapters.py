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
| lock_linked_identities (ACT-L1-PR1) | auth_identities | user_id (projection fermée : id, provider, email_verified_at, présence de l'e-mail, expurgation ; jamais l'e-mail, l'issuer ni le subject) | FOR KEY SHARE (rang identité, après génération, avant tout verrou métier) |
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
| _finalize_candidate (R1 PR 9-C1-B) : verrou | decryptage_turn_reservations | id ET user_id | FOR UPDATE (après T_authorization) |
| _FinalizationReadPort.read_preparation (R1 PR 9-C1-B) | _PREPARATION_CONTEXT (même instruction que B2) | user_id serveur ET réservation / conversation / ticker FIGÉS dans le port | - (dans le SAVEPOINT C1, réservation déjà verrouillée) |
| _finalize_reservation (R1 PR 9-C1-B) | decryptage_turn_reservations | id ET user_id ET state reserved ET epoch ET compteur ET jeton haché ET échéance > T_finalize (clock_timestamp() capturé PAR l'UPDATE) ET identité complète ET aucune livraison liée | (ligne déjà verrouillée) |
| _block_proven_collision (R1 PR 9-C1-B) | assistant_deliveries (preuve, identité complète du tour) puis decryptage_turn_reservations (mêmes conditions que la finalisation) | user_id | (ligne déjà verrouillée) |
| _recognize_finalized (R1 PR 9-C1-B) | assistant_deliveries (_LINKED_DELIVERY) | id ET user_id | - |
| _canonical_proof (R1 PR 9-C2 : replay et ACK) | UNE instruction (_CANONICAL_DELIVERY) : decryptage_turn_reservations (propriété projetée en booléen ; colonnes lues pour :u seulement), assistant_deliveries (liée, propre, finalized, conversation et livraison demandées), decryptage_cognitive_links, support_traces (parent de la trace du lien), cognitive_events et analysis_sessions (seulement COMPTÉES si manquantes ou d'autrui) | user_id serveur (unique paramètre :u, propagé par la CTE o) ET id de réservation / conversation / livraison demandés | - (aucun verrou, aucune écriture) |
| _acknowledge_delivery (R1 PR 9-C2) : verrou | decryptage_turn_reservations (_LOCK_RESERVATION) | id ET user_id | FOR UPDATE (après T_authorization), puis pont (DeliveryAcknowledgement + AckAdmissionPort) |
| _read_preparation_context (R1 PR 9-B2) | UNE instruction : decryptage_turn_reservations, users, conversation_identities, assistant_deliveries + decryptage_cognitive_links (ancre), cognitive_events, analysis_sessions, analysis_facts, user_statements, investment_theses | user_id serveur (unique paramètre :u, propagé par la CTE o) ET id de réservation / conversation / ticker résolu | - (plan READ, aucune écriture) |

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

R1 PR 9-B2 (plan READ INCHANGÉ, aucun verrou métier, aucune écriture) :
ReadDecryptagePreparationContext reçoit le trousseau Décrypter par le
dispatch EXISTANT (Spec.decryptage_keyed) pour revérifier l'intention B1.
UNE SEULE instruction (_PREPARATION_CONTEXT, CTE) : tout le contexte est lu
sous le même instantané d'instruction (READ COMMITTED : un instantané par
instruction, jamais entre deux instructions). L'unique paramètre de
propriétaire :u est propagé par la CTE o ; chaque table est filtrée par ce
user_id serveur ; les lignes d'autrui rattachées à la session active sont
seulement COMPTÉES (refus fermé, jamais lues). Ordre de décision :
réservation absente ou d'autrui : ABSENT ; identité du tour, état, epoch,
jeton ou échéance (clock_timestamp() de l'instruction) : CONFLICT ;
intention HMAC divergente : IDEMPOTENCY_CONFLICT ; version ou clé
introuvable, profil, conversation, ancre ou provenance incohérents :
AdapterInvariantBroken (503) ; puis refus métier fermés
(DecryptagePreparationRefused) : sessions actives multiples, étape ouverte
invalide, conversation obsolète (même sémantique que
core.decryptage_cognitive_runtime.precheck_conversation_context), contenus
hors bornes, valeur non finie. Contenus bornés DANS la projection (texte
transféré seulement sous sa borne, longueur toujours projetée ; 65 lignes au
plus pour 64 admises) ; flottants lus par float8send.

R1 PR 9-C1-B (plan DECRYPTAGE_MUTATION INCHANGÉ, trousseau Décrypter du
dispatch existant) : FinalizeDecryptageCandidate. Les adaptateurs restent le
SEUL propriétaire du SQL, de B1, du HMAC, du fencing et des mutations de
réservation ; ils appellent le pont ORM privé (unique importateur) avec une
capacité scellée qu'ils construisent : _TransactionProbe (sonde de la
transaction) ou _FinalizationReadPort (+ UNE relecture B2 dans le SAVEPOINT
C1). Ordre : réservation propre FOR UPDATE -> T_lease_decision -> identité
du tour -> HMAC d'intention (version de clé enregistrée) -> finalized :
reconnaissance terminale (aucune écriture) ; blocked : vue terminale ;
reserved : epoch, jeton, échéance -> pont -> après RELEASE : UNIQUE UPDATE
reserved -> finalized, qui capture lui-même T_finalize (aucune ligne :
CONFLICT, ROLLBACK global de TOUTES les écritures ORM) ; ou issue de l'annulation locale (vue métier,
blocage d'une collision PROUVÉE, ou sentinelle de ROLLBACK).

R1 PR 9-C2 (plans READ et DECRYPTAGE_MUTATION INCHANGÉS, SANS trousseau : le
HMAC B1 n'est jamais recalculé) : ReadCanonicalDecryptageDelivery et
AcknowledgeDecryptageDelivery. Preuve terminale C1-B commune
(_canonical_proof, UNE instruction sans verrou) dans l'ordre figé : propriété
(absente ou d'autrui : ABSENT) -> état B1 (non finalized : CONFLICT) ->
identité demandée (conversation ou livraison différente du lien B1 :
CONFLICT) -> intégrité persistée (réservation terminale complète, livraison
liée propre de même identité et de schéma connu, runtime et lien V2, couple
statut / lien admis, provenance transitive, payload public : 503). Payload :
texte JSONB persisté (response_payload::text), validé en Decimal (prix :
représentation exacte d'un binary64 fini), restitué en octets UTF-8 SANS
seconde sérialisation. ACK : réservation propre FOR UPDATE -> preuve sous ce
verrou -> attestation scellée _AckAdmissionPort (fermée dans un finally) ->
pont -> résultat comparé à la livraison admise (DeliveryAcknowledged,
response_action V2 non nulle). Aucune écriture B1, aucun retry, aucune
progression d'entrée.
"""
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

import sqlalchemy as sa

from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2 import decryptage_market_data as dm
from core.authz_f2 import decryptage_fingerprint as dfp
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2 import portfolio_fingerprint as fp
from core.authz_f2 import portfolio_results as results
from core.authz_f2 import portfolio_values as pv


# R1 PR 9-C1-B (F-01) : sentinelles et exception racine DÉFINIES dans les
# contrats communs purs et réexportées ici (MÊMES objets) : le pont ORM ne
# dépend plus de ce module, qui peut donc l'appeler sans cycle.
ABSENT = contracts.ABSENT
CONFLICT = contracts.CONFLICT  # révision ou capacité périmée : aucune écriture
IDEMPOTENCY_CONFLICT = contracts.IDEMPOTENCY_CONFLICT  # même identité, autre intention : aucune écriture
AdapterInvariantBroken = contracts.AdapterInvariantBroken


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
# ACT-L1-PR1 : identités fournisseur du propriétaire (rang identité de l'ordre
# global F2). Aucune donnée personnelle projetée : ni l'adresse e-mail, ni
# l'issuer, ni le subject ; seulement leur présence / expurgation en booléens.
_LOCK_LINKED_IDENTITIES = ("SELECT id, provider, email_verified_at, "
                           "verified_email_normalized IS NOT NULL AS email_present, "
                           "pii_redacted_at IS NOT NULL AS pii_redacted FROM auth_identities WHERE user_id = :u "
                           "ORDER BY id FOR KEY SHARE")
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


# ACT-L1-PR1 : seul fournisseur admis (liste fermée, ck_auth_identities_provider).
LINKED_IDENTITY_PROVIDERS = ("supabase",)


def lock_linked_identities(conn, user_id: str) -> bool:
    """Rang identité de la phase B (après génération, avant tout verrou
    métier et avant T_authorization) : verrouille FOR KEY SHARE les identités
    fournisseur du user_id serveur et rend True si l'une d'elles est
    ADMISSIBLE, False sinon. Aucune écriture, aucune donnée personnelle lue.

    Admissible : fournisseur de la liste fermée, vérification e-mail durable
    (email_verified_at renseigné) ET preuve encore portée par la ligne —
    adresse vérifiée présente, ou expurgée pour confidentialité
    (pii_redacted_at renseigné : l'expurgation n'invalide pas une
    vérification durable). Une ligne sans vérification, ou vérifiée sans
    adresse ni expurgation (état jamais écrit par F2), ne prouve rien.

    FOR KEY SHARE : bloque jusqu'au COMMIT toute suppression ou tout
    changement de clé (id, user_id, provider, issuer, subject) d'une identité
    lue ; compatible avec la connexion OTP (FOR NO KEY UPDATE puis
    last_authenticated_at) et avec une expurgation (colonnes non clés).
    L'insertion d'une identité (liaison e-mail) est sérialisée en amont par
    le rang users (liaison : users FOR UPDATE ; runner : plan)."""
    rows = conn.execute(sa.text(_LOCK_LINKED_IDENTITIES), {"u": user_id}).mappings().all()
    return any(row["provider"] in LINKED_IDENTITY_PROVIDERS and row["email_verified_at"] is not None
               and (row["email_present"] is True or row["pii_redacted"] is True) for row in rows)


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
                       "assistant_delivery_id, finalized_at, resolved_ticker")
_RESERVATION_COLUMNS = "SELECT " + _RESERVATION_LIST_COLUMNS + " FROM decryptage_turn_reservations "
_RESERVATION_RETURNING = "RETURNING " + _RESERVATION_LIST_COLUMNS
_LOCK_RESERVATION_TURN = (_RESERVATION_COLUMNS + "WHERE user_id = :u AND conversation_key = :k AND surface = :s "
                          "AND source_user_turn_id = :t AND delivery_ordinal = :o FOR UPDATE")
_LOCK_RESERVATION = _RESERVATION_COLUMNS + "WHERE id = :id AND user_id = :u FOR UPDATE"
# T_lease_decision : instruction DISTINCTE, exécutée après l'obtention du verrou.
_LEASE_DECISION_CLOCK = "SELECT clock_timestamp()"
_LEGACY_DELIVERY = ("SELECT id FROM assistant_deliveries WHERE user_id = :u AND conversation_key = :k "
                    "AND surface = :s AND source_user_turn_id = :t AND delivery_ordinal = :o")
_LINKED_DELIVERY = ("SELECT id, conversation_key, surface, source_user_turn_id, delivery_ordinal, "
                    "request_fingerprint, status, delivery_schema_version "
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
    resolved_ticker: object = None  # R1 PR 9-C1-B : écrit seulement par la finalisation


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
                           row["assistant_delivery_id"], row["finalized_at"], row["resolved_ticker"])
        valid = (type(res.id) is uuid.UUID and row["user_id"] == user_id and row["surface"] == dfp.SURFACE
                 and row["delivery_ordinal"] == dfp.DELIVERY_ORDINAL and type(res.conversation_key) is str
                 and type(res.source_user_turn_id) is uuid.UUID
                 and all(type(v) is str for v in (res.fingerprint_version, res.fingerprint_key_version,
                                                  res.request_fingerprint))
                 and type(res.lease_epoch) is int and 0 <= res.lease_epoch <= dr.MAX_EPOCH
                 and type(res.attempts_used) is int and 0 <= res.attempts_used <= dr.MAX_ATTEMPTS
                 and all(_aware_or_none(v) for v in (res.lease_expires_at, res.retry_not_before_at,
                                                     res.finalized_at))
                 and res.state in dr.STATES
                 and (res.resolved_ticker is None or dc.valid_resolved_ticker(res.resolved_ticker)))
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


# --------------------------------------------------------------------------
# R1 PR 9-B2 : lecture de préparation d'un tour Décrypter (plan READ)
# --------------------------------------------------------------------------

# UNE instruction : un seul instantané pour tout le contexte. Bornes en
# constantes (égalité avec core.authz_f2.decryptage_context vérifiée par les
# tests) : 2 sessions actives lues au plus (détection d'anomalie), 65 faits /
# contributions (64 admis), étape 32 octets, type de fait 64 octets,
# contribution 16384 octets, thèse 32768 octets.
_PREPARATION_CONTEXT = (
    "WITH o AS (SELECT CAST(:u AS text) AS u, CAST(:t AS text) COLLATE \"C\" AS t, CAST(:k AS text) AS k, "
    "clock_timestamp() AS at), "
    "r AS (SELECT " + _RESERVATION_LIST_COLUMNS + " FROM decryptage_turn_reservations, o WHERE id = :rid AND user_id = o.u), "
    "p AS (SELECT users.level, users.level_revision FROM users, o WHERE users.id = o.u), "
    "cv AS (SELECT ci.user_id = o.u AS owned FROM conversation_identities ci, o WHERE ci.conversation_key = o.k), "
    "an AS (SELECT l.response_action, l.response_event_id FROM decryptage_cognitive_links l "
    "JOIN assistant_deliveries d ON d.id = l.assistant_delivery_id CROSS JOIN o "
    "WHERE d.user_id = o.u AND d.conversation_key = o.k AND d.surface = 'decryptage' AND d.status = 'delivered' "
    "AND l.capture_state = 'captured' AND l.capture_version = 'decryptage-cognitive-runtime-v2' "
    "ORDER BY l.captured_at DESC, d.delivered_at DESC, d.generated_at DESC, d.id DESC LIMIT 1), "
    "ae AS (SELECT e.status, e.analysis_session_id, e.user_id = o.u AS owned FROM cognitive_events e, an, o "
    "WHERE e.id = an.response_event_id), "
    "asx AS (SELECT s.user_id = o.u AS owned, s.ticker COLLATE \"C\" = o.t AS same_ticker "
    "FROM analysis_sessions s, ae, o WHERE s.id = ae.analysis_session_id), "
    "act AS (SELECT s.id, CASE WHEN octet_length(s.current_step) <= 32 THEN s.current_step END AS current_step "
    "FROM analysis_sessions s, o WHERE s.user_id = o.u AND s.ticker COLLATE \"C\" = o.t "
    "AND s.status = 'in_progress' ORDER BY s.started_at DESC, s.updated_at DESC, s.id DESC LIMIT 2), "
    "sel AS (SELECT act.id, act.current_step FROM act WHERE (SELECT count(*) FROM act) = 1), "
    "fa AS (SELECT f.id, CASE WHEN octet_length(f.fact_type) <= 64 THEN f.fact_type END AS fact_type, "
    "octet_length(f.fact_type) AS type_bytes, encode(float8send(f.fact_value), 'hex') AS value_hex, "
    "row_number() OVER (ORDER BY f.fact_date ASC, f.id ASC) AS ordinal "
    "FROM analysis_facts f, sel, o WHERE f.analysis_session_id = sel.id AND f.user_id = o.u "
    "ORDER BY f.fact_date ASC, f.id ASC LIMIT 65), "
    "st AS (SELECT x.id, CASE WHEN octet_length(x.step) <= 32 THEN x.step END AS step, "
    "octet_length(x.step) AS step_bytes, "
    "CASE WHEN octet_length(x.statement_text) <= 16384 THEN x.statement_text END AS statement_text, "
    "octet_length(x.statement_text) AS text_bytes, "
    "row_number() OVER (ORDER BY x.statement_date ASC, x.id ASC) AS ordinal "
    "FROM user_statements x, sel, o WHERE x.analysis_session_id = sel.id AND x.user_id = o.u "
    "ORDER BY x.statement_date ASC, x.id ASC LIMIT 65), "
    "th AS (SELECT i.id, CASE WHEN octet_length(i.thesis_text) <= 32768 THEN i.thesis_text END AS thesis_text, "
    "octet_length(i.thesis_text) AS text_bytes, (i.created_at IS NULL OR (i.created_at >= TIMESTAMP "
    "'0001-01-01 00:00:00' AND i.created_at < TIMESTAMP '10000-01-01 00:00:00')) AS date_representable, "
    "CASE WHEN i.created_at >= TIMESTAMP '0001-01-01 00:00:00' AND i.created_at < TIMESTAMP "
    "'10000-01-01 00:00:00' THEN to_char(i.created_at, 'DD/MM/YYYY') END AS date_label "
    "FROM investment_theses i, o WHERE i.user_id = o.u AND i.ticker COLLATE \"C\" = o.t "
    "AND NOT EXISTS (SELECT 1 FROM act) ORDER BY i.created_at DESC NULLS LAST, i.id DESC LIMIT 1) "
    "SELECT o.at AS read_at, r.*, (SELECT count(*) FROM r) AS reservation_rows, "
    "(SELECT count(*) FROM p) AS profile_rows, (SELECT p.level FROM p) AS profile_level, "
    "(SELECT p.level_revision FROM p) AS profile_level_revision, "
    "(SELECT count(*) FROM cv) AS conversation_rows, (SELECT cv.owned FROM cv) AS conversation_owned, "
    "(SELECT an.response_action FROM an) AS anchor_action, (SELECT an.response_event_id FROM an) AS anchor_event_id, "
    "(SELECT count(*) FROM ae) AS anchor_event_rows, (SELECT ae.status FROM ae) AS anchor_status, "
    "(SELECT ae.owned FROM ae) AS anchor_owned, (SELECT ae.analysis_session_id FROM ae) AS anchor_session_id, "
    "(SELECT count(*) FROM asx) AS anchor_session_rows, (SELECT asx.owned FROM asx) AS anchor_session_owned, "
    "(SELECT asx.same_ticker FROM asx) AS anchor_same_ticker, "
    "(SELECT count(*) FROM act) AS active_sessions, (SELECT sel.id FROM sel) AS active_session_id, "
    "(SELECT sel.current_step FROM sel) AS active_current_step, "
    "(SELECT count(*) FROM analysis_facts f, sel, o WHERE f.analysis_session_id = sel.id AND f.user_id <> o.u) "
    "AS foreign_facts, "
    "(SELECT count(*) FROM user_statements x, sel, o WHERE x.analysis_session_id = sel.id AND x.user_id <> o.u) "
    "AS foreign_statements, "
    "(SELECT coalesce(jsonb_agg(jsonb_build_object('id', fa.id, 'fact_type', fa.fact_type, 'type_bytes', "
    "fa.type_bytes, 'value_hex', fa.value_hex) ORDER BY fa.ordinal), '[]'::jsonb) FROM fa) AS facts, "
    "(SELECT coalesce(jsonb_agg(jsonb_build_object('id', st.id, 'step', st.step, 'step_bytes', st.step_bytes, "
    "'text', st.statement_text, 'text_bytes', st.text_bytes) ORDER BY st.ordinal), '[]'::jsonb) FROM st) "
    "AS statements, "
    "(SELECT count(*) FROM th) AS thesis_rows, (SELECT th.id FROM th) AS thesis_id, "
    "(SELECT th.thesis_text FROM th) AS thesis_text, (SELECT th.text_bytes FROM th) AS thesis_text_bytes, "
    "(SELECT th.date_representable FROM th) AS thesis_date_representable, "
    "(SELECT th.date_label FROM th) AS thesis_date_label "
    "FROM o LEFT JOIN r ON TRUE")

# Actions de réponse V2 qui ancrent une conversation (core.decryptage_cognitive_runtime,
# recopiées : égalité vérifiée par les tests).
ANCHORING_RESPONSE_ACTIONS = ("opened_event", "continued_event", "continued_without_boundary_signal",
                              "transitioned_event")
NON_ANCHORING_RESPONSE_ACTIONS = ("closed_terminal", "no_cognitive_action", "stale_delivery")
COGNITIVE_OPEN = "open"


class _OutOfBounds(Exception):
    """Contenu historique hors des bornes de la vue : refus fermé."""


class _NonFinite(Exception):
    """Valeur numérique historique non finie : refus fermé."""


def _incoherent(message: str) -> DecryptageUnavailable:
    return DecryptageUnavailable(message, "decryptage_preparation_incoherent")


def _count(row, key: str, upper: int) -> int:
    value = row[key]
    if type(value) is not int or not 0 <= value <= upper:
        raise _incoherent("compte hors contrat")
    return value


def _anchor(row, user_id: str) -> dc.PreparationAnchor:
    """Ancre R1-C4 : dernière livraison V2 delivered + captured, jamais plus
    loin ; action ancrante sans event (ou l'inverse), event ou AnalysisSession
    d'autrui ou introuvable : invariant rompu (503)."""
    action, event_id = row["anchor_action"], row["anchor_event_id"]
    if action is None:
        if event_id is not None or _count(row, "anchor_event_rows", 0) != 0:
            raise _incoherent("ancre incohérente")
        return dc.PreparationAnchor(dc.ANCHOR_NONE, None, None)
    if action in NON_ANCHORING_RESPONSE_ACTIONS:
        if event_id is not None:
            raise _incoherent("action non ancrante avec event")
        return dc.PreparationAnchor(dc.ANCHOR_NONE, None, None)
    if action not in ANCHORING_RESPONSE_ACTIONS or type(event_id) is not uuid.UUID:
        raise _incoherent("action ancrante sans event")
    if _count(row, "anchor_event_rows", 1) != 1 or row["anchor_owned"] is not True:
        raise _incoherent("event d'ancrage introuvable ou d'autrui")
    session_id = row["anchor_session_id"]
    if session_id is None:
        if _count(row, "anchor_session_rows", 0) != 0:
            raise _incoherent("ancre incohérente")
        return dc.PreparationAnchor(dc.ANCHOR_UNBOUND, event_id, None)
    if _count(row, "anchor_session_rows", 1) != 1 or row["anchor_session_owned"] is not True \
            or type(row["anchor_same_ticker"]) is not bool:
        raise _incoherent("AnalysisSession d'ancrage introuvable ou d'autrui")
    state = dc.ANCHOR_CURRENT if row["anchor_same_ticker"] else dc.ANCHOR_OTHER_TICKER
    return dc.PreparationAnchor(state, event_id, session_id)


def _preparation_facts(raw) -> tuple:
    if type(raw) is not list:
        raise _incoherent("faits illisibles")
    if len(raw) > dc.MAX_FACTS:
        raise _OutOfBounds()
    facts = []
    for item in raw:
        if type(item) is not dict or type(item.get("type_bytes")) is not int:
            raise _incoherent("fait illisible")
        if item["type_bytes"] > dc.FACT_TYPE_MAX_BYTES:
            raise _OutOfBounds()
        hex_value = item.get("value_hex")
        value = None
        if hex_value is not None:
            if type(hex_value) is not str or len(hex_value) != 16:
                raise _incoherent("valeur de fait illisible")
            value = pv.float_from_float8send(bytes.fromhex(hex_value))
            if value != value or value in (float("inf"), float("-inf")):
                raise _NonFinite()
        facts.append(dc.PreparationFact(item.get("id"), item.get("fact_type"), value))
    return tuple(facts)


def _preparation_statements(raw) -> tuple:
    if type(raw) is not list:
        raise _incoherent("contributions illisibles")
    if len(raw) > dc.MAX_STATEMENTS:
        raise _OutOfBounds()
    statements, total = [], 0
    for item in raw:
        if type(item) is not dict or type(item.get("text_bytes")) is not int:
            raise _incoherent("contribution illisible")
        step_bytes = item.get("step_bytes")
        if item["text_bytes"] > dc.STATEMENT_TEXT_MAX_BYTES or (
                step_bytes is not None and (type(step_bytes) is not int or step_bytes > dc.STEP_MAX_BYTES)):
            raise _OutOfBounds()
        total += item["text_bytes"]
        if total > dc.STATEMENTS_TOTAL_MAX_BYTES:
            raise _OutOfBounds()
        statements.append(dc.PreparationStatement(item.get("id"), item.get("step"), item.get("text")))
    return tuple(statements)


def _preparation_thesis(row):
    rows = _count(row, "thesis_rows", 1)
    if rows == 0:
        return None
    if type(row["thesis_text_bytes"]) is not int:
        raise _incoherent("thèse illisible")
    if row["thesis_text_bytes"] > dc.THESIS_TEXT_MAX_BYTES or row["thesis_date_representable"] is not True:
        raise _OutOfBounds()
    return dc.PreparationThesis(row["thesis_id"], row["thesis_text"], row["thesis_date_label"])


def _preparation(row, res: _Reservation, user_id: str, command, at):
    """Vue ou refus fermé ; toute structure impossible : 503."""
    profile = row["profile_level"], row["profile_level_revision"]
    if _count(row, "profile_rows", 1) != 1 or not c.valid_level(profile[0]) or not c.valid_revision(profile[1]):
        raise _incoherent("profil incohérent")
    if _count(row, "conversation_rows", 1) != 1 or row["conversation_owned"] is not True:
        raise _incoherent("conversation de la réservation introuvable ou d'autrui")
    anchor = _anchor(row, user_id)
    active_count = _count(row, "active_sessions", 2)
    if active_count == 2:
        return dc.DecryptagePreparationRefused(dc.MULTIPLE_ACTIVE_SESSIONS)  # jamais la plus récente
    active = None
    if active_count == 1:
        if type(row["active_session_id"]) is not uuid.UUID:
            raise _incoherent("session active illisible")
        if row["active_current_step"] not in dc.TASK_STEPS:
            return dc.DecryptagePreparationRefused(dc.ACTIVE_SESSION_STEP_INVALID)
        active = dc.PreparationActiveSession(row["active_session_id"], row["active_current_step"])
    # R1-C4 : même ticker que l'ancre et état cognitif obsolète => stale (avant tout appel coûteux).
    if anchor.state == dc.ANCHOR_CURRENT and (row["anchor_status"] != COGNITIVE_OPEN or active is None
                                              or active.analysis_session_id != anchor.analysis_session_id):
        return dc.DecryptagePreparationRefused(dc.STALE_CONVERSATION_CONTEXT)
    if active is None:
        if row["facts"] != [] or row["statements"] != []:
            raise _incoherent("faits ou contributions sans session active")
        facts = statements = ()
    else:
        if _count(row, "foreign_facts", dr.MAX_EPOCH) or _count(row, "foreign_statements", dr.MAX_EPOCH):
            raise _incoherent("faits ou contributions d'autrui rattachés à la session active")
        facts, statements = _preparation_facts(row["facts"]), _preparation_statements(row["statements"])
    thesis = _preparation_thesis(row)
    if active is not None and thesis is not None:
        raise _incoherent("thèse lue avec une session active")
    return dc.DecryptagePreparationView(
        dc.CONTRACT_VERSION, res.id, res.lease_epoch, res.attempts_used, res.lease_expires_at, at, user_id,
        res.conversation_key, res.source_user_turn_id, command.resolved_ticker, profile[0], profile[1], anchor,
        active, facts, statements, thesis)


def _read_preparation_context(conn, ctx, command, locked, keys):
    return _preparation_outcome(conn, keys, ctx.principal.user_id, command)


def _preparation_outcome(conn, keys, user_id: str, command):
    """Projection _PREPARATION_CONTEXT et décision fermée, partagées par la
    lecture B2 (ReadDecryptagePreparationContext) et par la relecture de la
    finalisation C1-B (FinalizationReadPort.read_preparation, _TurnIntent) :
    une seule instruction, un seul code de décision."""
    row = _first(conn, _PREPARATION_CONTEXT, u=user_id, rid=command.reservation_id, k=command.conversation_key,
                 t=command.resolved_ticker)
    if row is None or _count(row, "reservation_rows", 1) == 0:
        return ABSENT  # réservation absente ou d'autrui : indiscernables, rien n'est lu d'autrui
    at = row["read_at"]
    if type(at) is not datetime or at.tzinfo is None:
        raise AdapterInvariantBroken("horloge PostgreSQL illisible")
    res = _reservation(row, user_id)
    if (res.conversation_key, res.source_user_turn_id) != (command.conversation_key, command.client_turn_id) \
            or res.state != dr.RESERVED or res.lease_epoch != command.lease_epoch or res.lease_token_hash is None \
            or not command.lease_token.matches(res.lease_token_hash) or not at < res.lease_expires_at:
        return CONFLICT  # capacité périmée ou d'un autre tour : rien n'est lu au-delà
    computed = _fingerprint(keys, res.fingerprint_version, res.fingerprint_key_version, user_id, command)
    if not dfp.same_fingerprint(computed, res.request_fingerprint):
        return IDEMPOTENCY_CONFLICT  # entrées différentes de l'intention réservée
    try:
        return _preparation(row, res, user_id, command, at)
    except _OutOfBounds:
        return dc.DecryptagePreparationRefused(dc.CONTEXT_OUT_OF_BOUNDS)
    except _NonFinite:
        return dc.DecryptagePreparationRefused(dc.CONTEXT_VALUE_INVALID)
    except (KeyError, TypeError, ValueError):
        raise _incoherent("contexte de préparation incohérent") from None


# --------------------------------------------------------------------------
# R1 PR 9-C1-B : finalisation atomique d'un candidat Décrypter
# (plan DECRYPTAGE_MUTATION inchangé, trousseau Décrypter dédié)
# --------------------------------------------------------------------------

# T_finalize (correctif d'audit P1, PR #233) : l'instant de décision est
# capturé PAR l'instruction d'écriture elle-même — CTE MATERIALIZED (une
# fonction volatile : UNE évaluation de clock_timestamp(), jamais inlinée),
# réutilisée pour l'échéance (lease_expires_at > decision.at) ET pour les
# horodatages écrits. Aucun instant n'est lu avant ni transmis par le code :
# un bail échu au moment de l'écriture n'est jamais finalisé ni bloqué, même
# si une lecture antérieure le voyait encore valide. Ligne déjà verrouillée
# FOR UPDATE par cette transaction : aucune attente entre la capture et
# l'écriture.
_DECISION_CLOCK = "WITH decision AS MATERIALIZED (SELECT clock_timestamp() AS at) "
# Seule mutation de finalisation : reserved -> finalized, après RELEASE.
# Efface les champs actifs du bail ; ne réécrit jamais l'intention.
_FINALIZE_RESERVATION = (_DECISION_CLOCK + "UPDATE decryptage_turn_reservations SET state = 'finalized', "
                         "assistant_delivery_id = :d, resolved_ticker = :rt, finalized_at = decision.at, "
                         "updated_at = decision.at, lease_token_hash = NULL, lease_expires_at = NULL, "
                         "retry_not_before_at = NULL FROM decision "
                         "WHERE id = :id AND user_id = :u AND state = 'reserved' AND lease_epoch = :e "
                         "AND attempts_used = :a AND lease_token_hash = :h AND lease_expires_at > decision.at "
                         "AND assistant_delivery_id IS NULL AND blocked_reason IS NULL AND finalized_at IS NULL "
                         # identité COMPLÈTE et intention revérifiées dans l'instruction
                         "AND conversation_key = :k AND surface = :s AND source_user_turn_id = :t "
                         "AND delivery_ordinal = :o AND fingerprint_version = :fv AND fingerprint_key_version = :kv "
                         "AND request_fingerprint = :f "
                         + _RESERVATION_RETURNING + ", decision.at AS decided_at")
# Blocage d'une collision legacy PROUVÉE : jeton et échéance vérifiés DANS
# l'instruction (contrairement à _BLOCK_RESERVATION de B1) ; motif existant.
_BLOCK_PROVEN_COLLISION = (_DECISION_CLOCK + "UPDATE decryptage_turn_reservations SET state = 'blocked', "
                           "blocked_reason = 'legacy_collision', lease_token_hash = NULL, lease_expires_at = NULL, "
                           "retry_not_before_at = NULL, updated_at = decision.at FROM decision "
                           "WHERE id = :id AND user_id = :u AND state = 'reserved' AND lease_epoch = :e "
                           "AND attempts_used = :a AND lease_token_hash = :h AND lease_expires_at > decision.at "
                           "AND assistant_delivery_id IS NULL "
                           "AND conversation_key = :k AND surface = :s AND source_user_turn_id = :t "
                           "AND delivery_ordinal = :o AND fingerprint_version = :fv AND fingerprint_key_version = :kv "
                           "AND request_fingerprint = :f "
                           + _RESERVATION_RETURNING + ", decision.at AS decided_at")


@dataclass(frozen=True, slots=True, repr=False)
class _TurnIntent:
    """Intention ORIGINALE normalisée d'un candidat (trame HMAC B1) et
    capacité de bail : même forme que ReadDecryptagePreparationContext pour
    _fingerprint et _preparation_outcome (aucun code de décision dupliqué).
    Construite depuis le candidat validé ; jamais depuis une saisie client."""
    reservation_id: uuid.UUID
    lease_epoch: int
    lease_token: dr.LeaseToken
    conversation_key: str
    client_turn_id: uuid.UUID
    ticker: str            # ticker SAISI normalisé (trame B1), jamais le ticker résolu
    question: str
    context: str
    last_method_id: object
    resolved_ticker: str

    @classmethod
    def of(cls, candidate) -> "_TurnIntent":
        turn, lease = candidate.turn, candidate.lease
        return cls(lease.reservation_id, lease.lease_epoch, lease.token, turn.conversation_key, turn.client_turn_id,
                   turn.ticker_input, turn.question, turn.context, turn.last_method_id, candidate.preparation.ticker)

    @property
    def texts(self) -> tuple:
        return self.question, self.context

    def __repr__(self) -> str:
        return "_TurnIntent(<secret>)"  # ni jeton, ni texte, ni identité

    def __reduce_ex__(self, protocol):
        raise TypeError("intention de tour non sérialisable")


_CAPABILITY = object()  # seules les fabriques de ce module construisent une capacité


class _TransactionProbe(contracts.TransactionProbe):
    """Implémentation SCELLÉE de TransactionProbe : liée à la connexion
    EXACTE du runner, à l'objet de transaction externe actif constaté à la
    construction, au principal autorisé et à la durée d'UNE opération du
    pont (_close). require_live() : identité et activité de la transaction,
    puis sonde PostgreSQL READ COMMITTED (échoue sur une transaction
    avortée, même si SQLAlchemy la croit active). Aucune autre capacité."""

    __slots__ = ("_conn", "_outer", "_user_id", "_open")

    def __init__(self, token, conn, user_id: str):
        if token is not _CAPABILITY:
            raise contracts.CapabilityRefused("capacité construite hors des adaptateurs SQL")
        outer = conn.get_transaction()
        if type(user_id) is not str or not user_id or outer is None or not outer.is_active:
            raise contracts.TransactionLost("aucune transaction active du runner")
        for name, value in (("_conn", conn), ("_outer", outer), ("_user_id", user_id), ("_open", True)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("capacité immuable")

    def __delattr__(self, name):
        raise AttributeError("capacité immuable")

    def __reduce_ex__(self, protocol):
        raise TypeError("capacité non sérialisable")

    def __copy__(self):
        raise TypeError("capacité non copiable")

    def __deepcopy__(self, memo):
        raise TypeError("capacité non copiable")

    def __repr__(self) -> str:
        return "TransactionProbe(<private>)"

    @property
    def user_id(self) -> str:
        return self._user_id

    def bound_to(self, conn, user_id: str) -> bool:
        return self._open and conn is self._conn and user_id == self._user_id

    def require_live(self) -> None:
        if not self._open:
            raise contracts.CapabilityRefused("opération du pont close")
        outer = self._outer
        if not outer.is_active or self._conn.get_transaction() is not outer:
            raise contracts.TransactionLost("transaction externe inactive ou remplacée")
        require_read_committed(self._conn, c.DECRYPTAGE_MUTATION)

    def _close(self) -> None:
        object.__setattr__(self, "_open", False)


class _FinalizationReadPort(_TransactionProbe, contracts.FinalizationReadPort):
    """Implémentation SCELLÉE de FinalizationReadPort, construite seulement
    par _finalize_candidate APRÈS l'autorisation du principal ET le
    verrouillage + la vérification de la réservation (HMAC, epoch, jeton,
    échéance). Réservation, identité du tour, epoch, capacité de bail,
    ticker résolu, principal et paramètres de projection sont FIGÉS ici :
    l'opération du pont ne peut en substituer aucun. Seule lecture métier :
    read_preparation, UNE fois, dans le SAVEPOINT C1 lié par bind_scope (une
    seule association, faite par le pont à l'ouverture du SAVEPOINT).
    Aucune méthode d'exécution SQL ni de rappel."""

    __slots__ = ("_keys", "_intent", "_reservation_id", "_scope", "_consumed")

    def __init__(self, token, conn, user_id: str, keys, reservation: _Reservation, intent: _TurnIntent):
        super().__init__(token, conn, user_id)
        if type(intent) is not _TurnIntent or type(reservation) is not _Reservation \
                or reservation.id != intent.reservation_id:
            raise contracts.CapabilityRefused("réservation ou intention hors contrat")
        for name, value in (("_keys", keys), ("_intent", intent), ("_reservation_id", reservation.id),
                            ("_scope", None), ("_consumed", False)):
            object.__setattr__(self, name, value)

    def bind_scope(self, scope) -> None:
        if (not self._open or self._scope is not None or type(scope) is not bridge._SavepointScope
                or not scope.is_open or scope._db.bind is not self._conn):
            raise contracts.CapabilityRefused("association au SAVEPOINT C1 refusée")
        object.__setattr__(self, "_scope", scope)

    def read_preparation(self, db, scope):
        """Relecture B2 (_PREPARATION_CONTEXT, une instruction) de LA
        réservation de ce port, sur la connexion du runner, DANS le
        SAVEPOINT C1 lié, ouvert et courant de la Session du pont. Consommée
        avant tout contrôle ; hors contrat : refus SANS requête métier.
        Réservation absente ou intention divergente sous le verrou déjà
        détenu : impossibles, jamais présentées comme une absence (503)."""
        if self._consumed:
            raise contracts.CapabilityRefused("relecture de préparation déjà consommée")
        object.__setattr__(self, "_consumed", True)
        if not self._open or type(db) is not bridge._BridgeSession or db.bind is not self._conn:
            raise contracts.CapabilityRefused("relecture hors de la Session du pont de cette opération")
        if self._scope is None or scope is not self._scope:
            raise contracts.CapabilityRefused("relecture hors du SAVEPOINT C1 lié")
        scope._require_current(db)
        outer = self._outer
        if not outer.is_active or self._conn.get_transaction() is not outer:
            raise contracts.TransactionLost("transaction externe inactive ou remplacée")
        outcome = _preparation_outcome(self._conn, self._keys, self._user_id, self._intent)
        if outcome is ABSENT or outcome is IDEMPOTENCY_CONFLICT:
            raise DecryptageUnavailable("réservation verrouillée relue absente ou divergente",
                                        "decryptage_finalization_incoherent")
        return outcome


def _transaction_probe(conn, ctx) -> _TransactionProbe:
    """Sonde SEULE pour une opération historique du pont (TurnFinalization,
    DeliveryAcknowledgement sur le chemin historique : bancs d'essai PR 9-A /
    C1-A ; aucune commande de production — R1 PR 9-C2 : l'ACK de production
    reçoit TOUJOURS l'attestation _AckAdmissionPort, jamais cette sonde) :
    jamais de relecture B2."""
    return _TransactionProbe(_CAPABILITY, conn, ctx.principal.user_id)


def _identity_params(user_id: str, res: _Reservation) -> dict:
    return {"id": res.id, "u": user_id, "e": res.lease_epoch, "a": res.attempts_used, "k": res.conversation_key,
            "s": dfp.SURFACE, "t": res.source_user_turn_id, "o": dfp.DELIVERY_ORDINAL,
            "fv": res.fingerprint_version, "kv": res.fingerprint_key_version, "f": res.request_fingerprint}


def _lease_is_held(res: _Reservation, intent: _TurnIntent, at) -> bool:
    return (res.state == dr.RESERVED and res.lease_epoch == intent.lease_epoch and res.lease_token_hash is not None
            and intent.lease_token.matches(res.lease_token_hash) and at < res.lease_expires_at)


def _recognize_finalized(conn, user_id: str, res: _Reservation, candidate):
    """Reconnaissance terminale (aucun SAVEPOINT, aucune écriture, aucun
    jeton exigé : effacé par la finalisation). Intention HMAC déjà revérifiée
    avec la version de clé ENREGISTRÉE. Le candidat doit être celui qui a été
    finalisé (epoch, ticker résolu, SHA historique recalculé avec le niveau
    DU CANDIDAT) : sinon CONFLICT. Lien persisté incohérent (livraison absente
    ou d'autrui, identité, statut, version de schéma, ticker non enregistré) :
    503, jamais de réparation."""
    if res.lease_epoch != candidate.lease.lease_epoch:
        return CONFLICT
    if res.resolved_ticker is None:
        raise DecryptageUnavailable("réservation finalisée sans ticker résolu", "decryptage_reservation_incoherent")
    if res.resolved_ticker != candidate.preparation.ticker:
        return CONFLICT
    linked = _first(conn, _LINKED_DELIVERY, id=res.assistant_delivery_id, u=user_id)
    if linked is None or (linked["conversation_key"], linked["surface"], linked["source_user_turn_id"],
                          linked["delivery_ordinal"]) != (res.conversation_key, dfp.SURFACE, res.source_user_turn_id,
                                                          dfp.DELIVERY_ORDINAL) \
            or linked["status"] not in bridge.DELIVERY_STATUSES \
            or linked["delivery_schema_version"] != bridge.DELIVERY_SCHEMA_VERSION:
        raise DecryptageUnavailable("réservation finalisée incohérente", "decryptage_reservation_incoherent")
    if linked["request_fingerprint"] != bridge._delivery_request_fingerprint(candidate):
        return CONFLICT  # autre candidat (niveau, entrées) que celui finalisé : jamais substitué
    return dfin.AlreadyFinalized(res.id, res.assistant_delivery_id)


def _finalize_reservation(conn, user_id: str, res: _Reservation, intent: _TurnIntent, applied):
    """Après RELEASE SAVEPOINT : UNIQUE mutation reserved -> finalized,
    conditionnelle (identité complète, état, epoch, compteur, jeton,
    échéance à T_finalize capturé PAR l'UPDATE, aucune livraison liée).
    Aucune ligne : CONFLICT,
    donc ROLLBACK GLOBAL du runner — livraison, lien, contribution,
    progression, UserStatements, thèse et faits écrits dans le SAVEPOINT
    libéré disparaissent avec la transaction externe."""
    row = _first(conn, _FINALIZE_RESERVATION, d=applied.assistant_turn_id, rt=intent.resolved_ticker,
                 h=intent.lease_token.digest(), **_identity_params(user_id, res))
    if row is None:
        return CONFLICT
    out = _reservation(row, user_id)
    if (out.id, out.state, out.lease_epoch, out.attempts_used, out.assistant_delivery_id, out.resolved_ticker,
            out.lease_token_hash, out.lease_expires_at) != (
            res.id, dr.FINALIZED, res.lease_epoch, res.attempts_used, applied.assistant_turn_id, intent.resolved_ticker,
            None, None) or type(row["decided_at"]) is not datetime or out.finalized_at != row["decided_at"]:
        raise AdapterInvariantBroken("finalisation écrite inattendue")
    return dfin.DecryptageFinalized(res.id, applied.assistant_turn_id)


def _block_proven_collision(conn, user_id: str, res: _Reservation, intent: _TurnIntent):
    """Après l'annulation locale legacy_collision (aucun objet ORM du
    SAVEPOINT réutilisé) : livraison gagnante relue par l'identité complète
    du tour, filtrée par le propriétaire ; collision non prouvée : 503.
    Puis blocage conditionnel (epoch, compteur, jeton, échéance à
    T_finalize capturé PAR l'UPDATE, identité) ; bail expiré ou capacité
    périmée : CONFLICT,
    rien n'est committé. LegacyCollisionBlocked seulement si le blocage est
    écrit (et rendu après COMMIT par le runner)."""
    if not _legacy_delivery(conn, _turn(user_id, res.conversation_key, res.source_user_turn_id)):
        raise DecryptageUnavailable("collision legacy non prouvée", "decryptage_collision_unproven")
    row = _first(conn, _BLOCK_PROVEN_COLLISION, h=intent.lease_token.digest(), **_identity_params(user_id, res))
    if row is None:
        return CONFLICT
    out = _mutated(row, user_id, res, dr.BLOCKED, res.lease_epoch, res.attempts_used)
    if out.blocked_reason != dr.LEGACY_COLLISION or type(row["decided_at"]) is not datetime:
        raise AdapterInvariantBroken("blocage écrit inattendu")
    return dfin.LegacyCollisionBlocked(res.id)


def _finalize_candidate(conn, ctx, command, locked, keys):
    """Phases 2 à 5 et 14 à 15 du protocole figé (autorisation et COMMIT :
    runner ; phases 6 à 13 : pont ORM privé). Ordre de décision : candidat
    d'un autre principal ou réservation absente / d'autrui -> ABSENT (rien
    n'est verrouillé chez autrui) ; réservation d'un autre tour -> CONFLICT ;
    intention HMAC divergente -> IDEMPOTENCY_CONFLICT (clé introuvable :
    503) ; finalized -> reconnaissance terminale ; blocked -> vue terminale ;
    reserved : epoch, jeton, échéance (T_lease_decision) -> sinon CONFLICT ;
    puis pont ; puis UPDATE final ou issue de l'annulation locale."""
    user_id = ctx.principal.user_id
    candidate = command.candidate
    if candidate.owner_user_id != user_id:
        return ABSENT
    intent = _TurnIntent.of(candidate)
    row = _first(conn, _LOCK_RESERVATION, id=intent.reservation_id, u=user_id)
    if row is None:
        return ABSENT
    at = _lease_clock(conn)  # T_lease_decision : après le verrou
    res = _reservation(row, user_id)
    if (res.conversation_key, res.source_user_turn_id) != (intent.conversation_key, intent.client_turn_id):
        return CONFLICT
    computed = _fingerprint(keys, res.fingerprint_version, res.fingerprint_key_version, user_id, intent)
    if not dfp.same_fingerprint(computed, res.request_fingerprint):
        return IDEMPOTENCY_CONFLICT
    if res.state == dr.FINALIZED:
        return _recognize_finalized(conn, user_id, res, candidate)
    if res.state == dr.BLOCKED:
        return dr.ReservationBlocked(res.id, res.blocked_reason)
    if not _lease_is_held(res, intent, at):
        return CONFLICT  # ancien epoch, jeton faux, bail libéré ou expiré : rien n'est écrit
    port = _FinalizationReadPort(_CAPABILITY, conn, user_id, keys, res, intent)
    try:
        outcome = bridge._execute(conn, ctx, bridge.CandidateFinalization(candidate), port)
    finally:
        port._close()
    if type(outcome) is bridge.PreparedTurnApplied:
        return _finalize_reservation(conn, user_id, res, intent, outcome)
    if type(outcome) is not bridge.FinalizationRolledBack:
        raise AdapterInvariantBroken("issue de finalisation hors contrat")
    if outcome.reason == bridge.CONTEXT_CHANGED:
        return dfin.FinalizationContextChanged(res.id)
    if outcome.reason == bridge.PREPARATION_REFUSED:
        return dfin.FinalizationPreparationRefused(res.id, outcome.detail)
    if outcome.reason == bridge.LEGACY_COLLISION:
        return _block_proven_collision(conn, user_id, res, intent)
    if outcome.reason == bridge.LEASE_STALE:
        return CONFLICT
    if outcome.reason == bridge.INTENT_MISMATCH:
        return IDEMPOTENCY_CONFLICT
    if outcome.reason == bridge.NOT_FOUND:
        return ABSENT
    raise AdapterInvariantBroken("motif d'annulation locale hors contrat")


# --------------------------------------------------------------------------
# R1 PR 9-C2 : replay canonique (plan READ) et ACK F2 (plan DECRYPTAGE_MUTATION)
# --------------------------------------------------------------------------

# UNE instruction propriétaire-centrée, point d'entrée
# decryptage_turn_reservations, SANS verrou ni écriture : l'unique paramètre
# de propriétaire :u est propagé par la CTE o. La réservation d'autrui n'est
# jamais lue (seule sa propriété est projetée en booléen, comme
# _CONVERSATION_OWNED) ; la livraison n'est jointe que pour une réservation
# PROPRE, finalized, de la conversation demandée, dont le lien B1 est
# EXACTEMENT la livraison demandée, ET filtrée par :u (une livraison liée
# d'autrui est une livraison manquante) ; jointures externes depuis o : une
# incohérence persistée (livraison ou lien absent) reste visible, jamais une
# disparition silencieuse de la ligne résultat. Le lien cognitif est celui de
# la livraison exacte ; la provenance transitive (cinq events du lien, event
# parent de la SupportTrace, AnalysisSessions de la livraison et des events)
# n'est que COMPTÉE quand elle manque ou appartient à autrui (même contrôle
# que bridge._require_owned_provenance). Le payload est le JSONB persisté
# (response_payload::text), jamais les octets envoyés à l'écriture.
_CANONICAL_DELIVERY = (
    "WITH o AS (SELECT CAST(:u AS text) AS u, CAST(:rid AS uuid) AS rid, CAST(:k AS text) AS k, "
    "CAST(:tid AS uuid) AS tid), "
    "f AS (SELECT t.user_id = o.u AS owned FROM decryptage_turn_reservations t, o WHERE t.id = o.rid), "
    "r AS (SELECT " + _RESERVATION_LIST_COLUMNS + " FROM decryptage_turn_reservations, o "
    "WHERE id = o.rid AND user_id = o.u), "
    "dl AS (SELECT d.id AS d_id, d.conversation_key AS d_conversation_key, d.surface AS d_surface, "
    "d.source_user_turn_id AS d_source_user_turn_id, d.delivery_ordinal AS d_delivery_ordinal, "
    "d.delivery_schema_version AS d_schema_version, d.status AS d_status, "
    "d.visible_content_fingerprint AS d_visible_fingerprint, d.analysis_session_id AS d_analysis_session_id, "
    "jsonb_typeof(d.private_metadata -> 'cognitive_runtime_version') AS d_runtime_type, "
    "d.private_metadata ->> 'cognitive_runtime_version' AS d_runtime, d.response_payload::text AS d_payload "
    "FROM assistant_deliveries d, r, o WHERE d.id = r.assistant_delivery_id AND d.user_id = o.u "
    "AND r.state = 'finalized' AND r.conversation_key = o.k AND r.assistant_delivery_id = o.tid), "
    "lk AS (SELECT l.capture_version, l.capture_state, l.response_action, l.input_event_id, "
    "l.input_context_event_id, l.response_context_event_id, l.context_exit_event_id, l.response_event_id, "
    "l.support_trace_id FROM decryptage_cognitive_links l, dl WHERE l.assistant_delivery_id = dl.d_id), "
    "ev AS (SELECT x.e AS id FROM lk CROSS JOIN LATERAL (VALUES (lk.input_event_id), (lk.input_context_event_id), "
    "(lk.response_context_event_id), (lk.context_exit_event_id), (lk.response_event_id)) AS x(e) "
    "WHERE x.e IS NOT NULL "
    "UNION SELECT st.cognitive_event_id FROM support_traces st, lk WHERE st.id = lk.support_trace_id), "
    "pv AS (SELECT (SELECT count(*) FROM lk WHERE lk.support_trace_id IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM support_traces st WHERE st.id = lk.support_trace_id)) AS missing_traces, "
    "(SELECT count(*) FROM ev WHERE NOT EXISTS (SELECT 1 FROM cognitive_events e, o "
    "WHERE e.id = ev.id AND e.user_id = o.u)) AS foreign_events, "
    "(SELECT count(*) FROM (SELECT dl.d_analysis_session_id AS s FROM dl UNION SELECT e.analysis_session_id "
    "FROM cognitive_events e, ev, o WHERE e.id = ev.id AND e.user_id = o.u) ss WHERE ss.s IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM analysis_sessions a, o WHERE a.id = ss.s AND a.user_id = o.u)) "
    "AS foreign_sessions) "
    "SELECT f.owned, r.*, dl.*, (SELECT count(*) FROM lk) AS l_count, lk.capture_version AS l_capture_version, "
    "lk.capture_state AS l_capture_state, lk.response_action AS l_response_action, pv.missing_traces, "
    "pv.foreign_events, pv.foreign_sessions "
    "FROM o LEFT JOIN f ON true LEFT JOIN r ON true LEFT JOIN dl ON true LEFT JOIN lk ON true CROSS JOIN pv")

# Payload public C1-B (decryptage_candidate.CandidatePayload.as_dict) : huit
# champs EXACTS, aucune clé supplémentaire ni réservée.
CANONICAL_PAYLOAD_KEYS = frozenset(("success", "ticker", "name", "method_used", "analysis", "price", "currency",
                                    "disclaimer"))


@dataclass(frozen=True, slots=True, repr=False)
class _CanonicalProof:
    """Preuve terminale C1-B établie par _canonical_proof (aucun contenu dans
    repr) : réservation finalized validée, statut et octets JSONB du payload."""
    reservation: _Reservation
    delivery_status: str
    payload_json: bytes

    def __repr__(self) -> str:
        return "_CanonicalProof(<private>)"

    def __reduce_ex__(self, protocol):
        raise TypeError("preuve canonique non sérialisable")


def _json_object_pairs(pairs) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:  # JSONB n'en produit jamais : refus fermé
            raise ValueError("clé JSON dupliquée")
        out[key] = value
    return out


def _json_constant(name):
    raise ValueError("constante JSON non standard")


def _binary64_decimal(value) -> bool:
    """Nombre JSON lu en Decimal (jamais en float) : fini et EXACTEMENT la
    représentation décimale la plus courte d'un binary64 fini — domaine des
    prix écrits par C1-B (float fini sérialisé par json, conservé par JSONB
    numeric, éventuellement sous une autre notation). Aucun arrondi accepté."""
    if type(value) is not Decimal or not value.is_finite():
        return False
    as_float = float(value)
    return math.isfinite(as_float) and Decimal(repr(as_float)) == value


def _visible_analysis(value) -> bool:
    """Texte visible C1-B : non vide, sans marqueur privé résiduel ni NUL,
    au plus RESPONSE_MAX_BYTES octets UTF-8 (mêmes règles que
    decryptage_candidate.validate_response ; aucune borne nouvelle)."""
    if type(value) is not str or not value.strip() or "\x00" in value:
        return False
    if "ORYX_STEP" in value or "<!--" in value:
        return False
    length = dc.utf8_length(value)
    return length is not None and length <= dcand.RESPONSE_MAX_BYTES


def _canonical_payload(text, resolved_ticker: str, recorded_fingerprint) -> bytes:
    """Valide le texte JSONB persisté SANS le resérialiser et rend ses octets
    UTF-8 exacts ; tout écart : DecryptageUnavailable (aucun détail public)."""
    try:
        payload = json.loads(text, parse_float=Decimal, parse_int=Decimal, parse_constant=_json_constant,
                             object_pairs_hook=_json_object_pairs) if type(text) is str else None
    except (ValueError, RecursionError):
        payload = None
    valid = type(payload) is dict and set(payload) == CANONICAL_PAYLOAD_KEYS
    if valid:
        valid = (payload["success"] is True
                 and dc.valid_resolved_ticker(payload["ticker"]) and payload["ticker"] == resolved_ticker
                 and type(payload["name"]) is str and payload["name"] != ""
                 and (payload["method_used"] is None or type(payload["method_used"]) is str)
                 and _visible_analysis(payload["analysis"])
                 and (payload["price"] is None or _binary64_decimal(payload["price"]))
                 and type(payload["currency"]) is str and dm.CURRENCY_RE.fullmatch(payload["currency"]) is not None
                 and type(payload["disclaimer"]) is str and payload["disclaimer"] == dcand.DISCLAIMER
                 and type(recorded_fingerprint) is str
                 and bridge._visible_content_fingerprint(payload["analysis"]) == recorded_fingerprint)
    if not valid:
        raise DecryptageUnavailable("contenu canonique incohérent", "decryptage_payload_incoherent")
    return text.encode("utf-8")


def _terminal(res: _Reservation) -> bool:
    """Réservation finalized C1-B complète : champs actifs du bail effacés,
    aucun motif de blocage, instant de finalisation et ticker résolu présents,
    epoch et compteur d'une attribution réelle, versions et empreinte B1
    structurellement valides (jamais recalculées : aucune clé)."""
    return (res.state == dr.FINALIZED and res.lease_token_hash is None and res.lease_expires_at is None
            and res.retry_not_before_at is None and res.blocked_reason is None and res.finalized_at is not None
            and res.resolved_ticker is not None and res.lease_epoch >= 1 and res.attempts_used >= 1
            and res.fingerprint_version in dfp.FRAMES
            and dfp.KEY_VERSION_RE.fullmatch(res.fingerprint_key_version) is not None
            and dfp.FINGERPRINT_RE.fullmatch(res.request_fingerprint) is not None)


def _canonical_proof(conn, user_id: str, command):
    """Preuve de provenance terminale C1-B (replay et ACK), UNE instruction
    (_CANONICAL_DELIVERY). Ordre : propriété (absente ou d'autrui : ABSENT,
    indiscernables) -> état B1 (non finalized : CONFLICT) -> identité demandée
    (conversation ou livraison différente du lien B1 : CONFLICT) -> intégrité
    persistée (réservation, livraison liée, runtime et lien V2, couple
    statut / lien, provenance transitive, payload : DecryptageUnavailable,
    503) -> _CanonicalProof. Aucune écriture, aucun verrou."""
    row = _first(conn, _CANONICAL_DELIVERY, u=user_id, rid=command.reservation_id, k=command.conversation_key,
                 tid=command.assistant_turn_id)
    if row is None or row["owned"] not in (True, False, None):
        raise AdapterInvariantBroken("projection canonique hors contrat")
    if row["owned"] is not True:
        return ABSENT
    res = _reservation(row, user_id)
    if res.state != dr.FINALIZED:
        return CONFLICT
    if res.conversation_key != command.conversation_key or res.assistant_delivery_id != command.assistant_turn_id:
        return CONFLICT
    if not _terminal(res):
        raise DecryptageUnavailable("réservation finalisée incohérente", "decryptage_reservation_incoherent")
    if row["d_id"] != res.assistant_delivery_id or (
            row["d_conversation_key"], row["d_surface"], row["d_source_user_turn_id"], row["d_delivery_ordinal"],
            row["d_schema_version"]) != (res.conversation_key, dfp.SURFACE, res.source_user_turn_id,
                                         dfp.DELIVERY_ORDINAL, bridge.DELIVERY_SCHEMA_VERSION) \
            or row["d_status"] not in bridge.DELIVERY_STATUSES:
        raise DecryptageUnavailable("livraison liée manquante ou incohérente", "decryptage_delivery_incoherent")
    if (row["d_runtime_type"], row["d_runtime"]) != ("string", bridge.COGNITIVE_RUNTIME_V2) \
            or row["l_count"] != 1 or row["l_capture_version"] != bridge.COGNITIVE_RUNTIME_V2 \
            or (row["d_status"], row["l_capture_state"]) not in bridge.CANONICAL_COUPLES \
            or (row["l_response_action"] is None) != (row["l_capture_state"] == bridge.CANONICAL_COUPLES[0][1]) \
            or (row["l_response_action"] is not None and row["l_response_action"] not in bridge.RESPONSE_ACTIONS):
        raise DecryptageUnavailable("provenance cognitive V2 incompatible", "decryptage_cognitive_link_incoherent")
    if (row["missing_traces"], row["foreign_events"], row["foreign_sessions"]) != (0, 0, 0):
        raise DecryptageUnavailable("provenance transitive incohérente", "decryptage_provenance_incoherent")
    payload = _canonical_payload(row["d_payload"], res.resolved_ticker, row["d_visible_fingerprint"])
    return _CanonicalProof(res, row["d_status"], payload)


def _read_canonical_delivery(conn, ctx, command, locked):
    """Replay (plan READ) : contenu public canonique d'une livraison C1-B, tel
    que persisté, et son statut à l'instant de l'instruction. Aucune écriture,
    aucun verrou métier, aucun fournisseur, aucune progression."""
    proof = _canonical_proof(conn, ctx.principal.user_id, command)
    if proof is ABSENT or proof is CONFLICT:
        return proof
    return c.CanonicalDecryptageDelivery(proof.reservation.id, proof.reservation.assistant_delivery_id,
                                         proof.delivery_status, proof.payload_json)


class _AckAdmissionPort(_TransactionProbe, contracts.AckAdmissionPort):
    """Implémentation SCELLÉE d'AckAdmissionPort, construite seulement par
    _acknowledge_delivery APRÈS l'autorisation du principal, le verrou FOR
    UPDATE de la réservation B1 propre et la preuve terminale C1-B. Principal,
    réservation, conversation et identité complète de la livraison admise sont
    FIGÉS ici ; liée à la connexion, à la transaction externe et à la durée
    d'UNE opération ACK (_close dans un finally). require_delivery : UNE fois,
    identité de la livraison verrouillée par le pont comparée à l'attestation.
    Aucune méthode d'exécution SQL ni de rappel ; non sérialisable, non
    copiable (hérité)."""

    __slots__ = ("_reservation_id", "_conversation_key", "_assistant_turn_id", "_source_user_turn_id", "_consumed")

    def __init__(self, token, conn, user_id: str, proof: _CanonicalProof):
        super().__init__(token, conn, user_id)
        if type(proof) is not _CanonicalProof or type(proof.reservation) is not _Reservation \
                or proof.reservation.state != dr.FINALIZED:
            raise contracts.CapabilityRefused("preuve canonique hors contrat")
        res = proof.reservation
        for name, value in (("_reservation_id", res.id), ("_conversation_key", res.conversation_key),
                            ("_assistant_turn_id", res.assistant_delivery_id),
                            ("_source_user_turn_id", res.source_user_turn_id), ("_consumed", False)):
            object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "AckAdmissionPort(<private>)"

    def require_delivery(self, *, assistant_turn_id, user_id, conversation_key, surface, source_user_turn_id,
                         delivery_ordinal, delivery_schema_version) -> None:
        if self._consumed:
            raise contracts.CapabilityRefused("attestation déjà consommée")
        object.__setattr__(self, "_consumed", True)
        if not self._open:
            raise contracts.CapabilityRefused("attestation close")
        outer = self._outer
        if not outer.is_active or self._conn.get_transaction() is not outer:
            raise contracts.TransactionLost("transaction externe inactive ou remplacée")
        if (assistant_turn_id, user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal,
                delivery_schema_version) != (self._assistant_turn_id, self._user_id, self._conversation_key,
                                             dfp.SURFACE, self._source_user_turn_id, dfp.DELIVERY_ORDINAL,
                                             bridge.DELIVERY_SCHEMA_VERSION):
            raise contracts.CapabilityRefused("livraison verrouillée différente de la livraison attestée")


def _acknowledge_delivery(conn, ctx, command, locked):
    """ACK F2 (plan DECRYPTAGE_MUTATION, après T_authorization) : réservation
    propre FOR UPDATE (absente ou d'autrui : ABSENT, rien n'est verrouillé
    chez autrui) -> preuve terminale C1-B (_canonical_proof, sous ce verrou)
    -> attestation scellée -> pont (opération DeliveryAcknowledgement
    INCHANGÉE : livraison verrouillée, attestation et V2 revérifiées,
    provenance transitive, capture historique, pending -> delivered) ->
    résultat comparé à la livraison admise. ACK rejoué : no-op. Aucun retry ;
    aucune progression d'entrée ; aucune écriture B1."""
    user_id = ctx.principal.user_id
    if _first(conn, _LOCK_RESERVATION, id=command.reservation_id, u=user_id) is None:
        return ABSENT
    proof = _canonical_proof(conn, user_id, command)
    if proof is ABSENT:  # propre et verrouillée par cette transaction : impossible
        raise DecryptageUnavailable("réservation verrouillée relue absente", "decryptage_reservation_incoherent")
    if proof is CONFLICT:
        return CONFLICT
    port = _AckAdmissionPort(_CAPABILITY, conn, user_id, proof)
    try:
        outcome = bridge._execute(conn, ctx, bridge.DeliveryAcknowledgement(command.assistant_turn_id,
                                                                            command.conversation_key), port)
    finally:
        port._close()
    if type(outcome) is not bridge.DeliveryAcknowledged or outcome.assistant_turn_id != command.assistant_turn_id \
            or outcome.delivery_status != bridge.DELIVERED or outcome.response_action is None \
            or outcome.response_action not in bridge.RESPONSE_ACTIONS:
        raise DecryptageUnavailable("ACK hors contrat", "decryptage_ack_incoherent")
    return outcome


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
    # R1 PR 9-B2 : lecture de préparation, plan READ inchangé, trousseau Décrypter
    # par le dispatch existant (revérification de l'intention B1), aucune écriture.
    c.ReadDecryptagePreparationContext: Spec(c.READ, _no_business_lock, _read_preparation_context,
                                             decryptage_keyed=True),
    # R1 PR 9-C1-B : finalisation, plan DECRYPTAGE_MUTATION INCHANGÉ (aucun
    # verrou préparatoire : réservation verrouillée par l'adaptateur après la
    # décision), trousseau Décrypter par le dispatch existant.
    c.FinalizeDecryptageCandidate: Spec(c.DECRYPTAGE_MUTATION, _no_business_lock, _finalize_candidate,
                                        decryptage_keyed=True),
    # R1 PR 9-C2 : replay (plan READ) et ACK (plan DECRYPTAGE_MUTATION),
    # aucun verrou préparatoire, SANS trousseau (aucun HMAC B1 recalculé).
    c.ReadCanonicalDecryptageDelivery: Spec(c.READ, _no_business_lock, _read_canonical_delivery),
    c.AcknowledgeDecryptageDelivery: Spec(c.DECRYPTAGE_MUTATION, _no_business_lock, _acknowledge_delivery),
})
