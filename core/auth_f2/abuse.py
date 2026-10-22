"""Anti-abus F2 : sujets, verrous, quotas glissants, blocage e-mail,
marqueurs de refus, rotation de K_abuse (C.2.1 partie III, C.2.1-A D4/D5,
précisions du 22 octobre).

Sujets. Une portée logique (e-mail normalisé D1 ou IP normalisée D2) a un
sujet par version de K_abuse : scope_hmac = HMAC(K_abuse[v],
FRAME("abuse", kind, valeur)). Les versions comptées sont celles de
auth_abuse_key_state (current_version et, pendant une rotation,
previous_version) ; les nouveaux événements sont enregistrés sous
current_version. Une instance dont le trousseau ne contient pas ces
versions refuse tout (AbuseUnavailable) : elle ne peut ni sous-compter ni
enregistrer sous une version que les autres ignorent.

Ordre de verrouillage global (absence d'interblocage) :

  1. CRÉATION des sujets manquants dans une transaction COURTE et
     séparée (ensure_subjects), committée avant toute autre prise de
     verrou : INSERT ... ON CONFLICT DO NOTHING dans l'ordre
     (scope_kind, hash_key_version, scope_hmac). Une telle transaction ne
     tient que ses propres insertions et n'attend que d'autres
     insertions de même clé, prises dans le même ordre : pas de cycle.
  2. Transaction de décision : D'ABORD le verrou consultatif PARTAGÉ de
     l'état de clé puis la lecture de auth_abuse_key_state
     (lock_key_state), PUIS verrou FOR UPDATE de TOUS les sujets
     e-mail et IP de l'opération, pour TOUTES les versions lues sous ce
     verrou, en UNE instruction triée par (scope_kind, hash_key_version,
     scope_hmac) (PostgreSQL trie avant de verrouiller : LockRows
     au-dessus de Sort).
  3. Puis, selon le parcours : invitation, profil users, opération,
     candidat, demande OTP, tentative, session, génération, identité
     (core.auth_f2.service respecte cet ordre partout ; aucune transaction
     ne verrouille une ligne de rang inférieur après une ligne de rang
     supérieur). L'heure de décision est lue APRÈS tous les verrous.

  Une rotation ou un retrait ne prend QUE le verrou consultatif
  EXCLUSIF de l'état de clé, puis la ligne d'état.
  Toute transaction prend ses verrous dans un ordre total commun : aucun
  cycle d'attente n'est possible.

Rotation sans remise à zéro ni contournement (correctif d'audit P1).
Une simple lecture de l'état suivie des verrous de sujets ne suffit pas :
T1 lit {v1}, une rotation committe {v2, v1}, T2 enregistre sous v2 et
committe, puis T1 verrouille seulement v1 et ne voit pas l'événement de
T2 (sous-comptage). Désormais chaque transaction de décision prend le
verrou consultatif de transaction KEY_STATE_LOCK_KEY en mode PARTAGÉ
AVANT de lire les versions et le garde jusqu'à son COMMIT ;
rotate_abuse_key / retire_previous_abuse_key le prennent en mode EXCLUSIF
avant de modifier la ligne :

  - aucune rotation ne peut committer pendant une décision en cours : la
    décision compte et enregistre sous un ensemble de versions stable ;
  - une décision qui commence après le COMMIT d'une rotation lit le
    nouvel ensemble (READ COMMITTED : FOR SHARE relit la dernière version
    committée), qui contient l'ancienne version courante : tout
    événement enregistré avant la rotation est compté ;
  - un retrait n'intervient qu'ABUSE_KEY_RETENTION (90 min) après la
    rétrogradation ; depuis celle-ci, aucune décision n'a pu enregistrer
    sous l'ancienne version (elles ont toutes lu le nouvel état), donc
    aucun événement de fenêtre active (1 h au plus, blocage 15 min) n'est
    oublié.

Pourquoi un verrou consultatif plutôt que FOR SHARE sur la ligne
singleton : les deux sont sûrs, mais un FOR SHARE de ligne compatible
n'attend pas derrière une rotation déjà en attente du verrou exclusif
(constaté par tests/test_f2_audit_corrections.py) ; sous trafic continu,
une rotation pourrait ne jamais aboutir. Le gestionnaire de verrous de
PostgreSQL met en file une demande partagée derrière une demande
exclusive en attente : la rotation passe dès la fin des décisions en
cours, les décisions suivantes l'attendent (au plus lock_timeout, puis
refus fermé). Même convention que core/taxonomy_service.py (clé bigint
figée, dérivée par sha256, jamais hash() Python).

Les pré-contrôles sans verrou et ensure_subjects lisent l'état sans
verrou : un pré-contrôle ne fait que refuser sur des événements réels
(jamais admettre), et un sujet manquant pour une version apparue entre-
temps fait rejouer la transaction (SubjectsMissing).

Marqueurs de refus bornés. Un refus de quota n'appelle aucun fournisseur
et n'insère qu'un marqueur otp_request_refused / otp_verification_refused
daté du début de sa tranche de 5 minutes, par INSERT ... ON CONFLICT DO
NOTHING sur l'index unique partiel uq_auth_abuse_events_refusal_bucket :
au plus UN marqueur par (sujet, type, tranche), quels que soient la
concurrence et la position dans les fenêtres glissantes. Un refus ne crée
jamais de sujet (il suppose des événements, donc un sujet existant).

Croissance sous attaque. Les sujets ne sont créés que pour des
opérations dont le pré-contrôle sans verrou est passé ; les événements ne
sont insérés que pour des admissions (bornées par les quotas de chaque
sujet) ou des marqueurs (bornés ci-dessus). purge_abuse_history (dormante)
retire l'historique de plus de deux heures. Une base saturée (verrou ou
instruction expirés, erreur d'écriture) lève une erreur : jamais une
admission, jamais un appel fournisseur.
"""
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa

from core.auth_f2.crypto import abuse_scope_hash, require_key_version
from core.auth_f2.keys import Keyring
from core.auth_f2.policy import (
    ABUSE_KEY_RETENTION,
    ABUSE_PURGE_MIN_AGE,
    EMAIL_BLOCK_DURATION,
    EMAIL_FAILURE_THRESHOLD,
    EMAIL_FAILURE_WINDOW,
    ISSUE_EMAIL_LIMIT,
    ISSUE_EMAIL_WINDOW,
    ISSUE_IP_LIMIT,
    ISSUE_IP_WINDOW,
    ISSUE_MIN_INTERVAL,
    POLICY_VERSION,
    VERIFY_IP_LIMIT,
    VERIFY_IP_WINDOW,
)
from core.auth_f2.txn import pg_clock, transaction

# int.from_bytes(sha256(b"oryx:f2:auth_abuse_key_state").digest()[:8], "big",
# signed=True) : constante figée, plage bigint PostgreSQL.
KEY_STATE_LOCK_KEY = -8112447256766139957

EMAIL = "email"
IP = "ip"
OTP_REQUEST = "otp_request"
OTP_FAILURE = "otp_failure"
OTP_VERIFICATION_ATTEMPT = "otp_verification_attempt"
OTP_EMAIL_BLOCK = "otp_email_block"
OTP_REQUEST_REFUSED = "otp_request_refused"
OTP_VERIFICATION_REFUSED = "otp_verification_refused"


class AbuseUnavailable(Exception):
    """Protection anti-abus indisponible : refus fermé."""


class SubjectsMissing(Exception):
    """Un sujet attendu a disparu entre création et verrou (purge)."""


@dataclass(frozen=True)
class Scope:
    kind: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class KeyState:
    current: str
    previous: object
    previous_demoted_at: object

    def versions(self) -> tuple:
        return (self.current,) if self.previous is None else (self.current, self.previous)


@dataclass
class Subjects:
    """Sujets d'une opération : {(kind, valeur): {version: id}}."""
    state: KeyState
    by_scope: dict

    def ids(self, scope: Scope) -> list:
        return sorted(self.by_scope.get((scope.kind, scope.value), {}).values())

    def recording_id(self, scope: Scope):
        """Sujet d'enregistrement : version courante (doit exister)."""
        return self.by_scope[(scope.kind, scope.value)][self.state.current]

    def marker_id(self, scope: Scope):
        """Sujet d'un marqueur de refus : version courante si présente,
        sinon un sujet existant de la portée (refus sous l'ancienne)."""
        versions = self.by_scope.get((scope.kind, scope.value), {})
        if self.state.current in versions:
            return versions[self.state.current]
        return versions[sorted(versions)[0]] if versions else None


# --------------------------------------------------------------------------
# État de clé et sujets
# --------------------------------------------------------------------------

def load_key_state(conn, keyring: Keyring) -> KeyState:
    """Lecture SANS verrou : pré-contrôles (refus seulement) et création
    des sujets. Une décision utilise lock_key_state."""
    return _key_state(conn, keyring, "")


def lock_key_state(conn, keyring: Keyring) -> KeyState:
    """Étape 2 de l'ordre global : verrou consultatif PARTAGÉ jusqu'au
    COMMIT, puis lecture de l'état (instantané pris après l'obtention du
    verrou, donc après le COMMIT de toute rotation qui le détenait) ;
    aucune rotation ne peut changer les versions comptées pendant la
    décision."""
    conn.execute(sa.text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KEY_STATE_LOCK_KEY})
    return _key_state(conn, keyring, "")


def _key_state(conn, keyring: Keyring, lock: str) -> KeyState:
    row = conn.execute(sa.text(
        "SELECT current_version, previous_version, previous_demoted_at FROM auth_abuse_key_state WHERE id = 1" + lock
    )).first()
    if row is None:
        raise AbuseUnavailable("état de clé anti-abus absent")
    state = KeyState(row[0], row[1], row[2])
    if not all(keyring.has("abuse", v) for v in state.versions()):
        raise AbuseUnavailable("trousseau anti-abus incompatible avec l'état en base")
    return state


def _scope_rows(keyring: Keyring, state: KeyState, scopes) -> list:
    rows = []
    for scope in scopes:
        for version in state.versions():
            rows.append((scope.kind, version, abuse_scope_hash(keyring.key("abuse", version), scope.kind,
                                                                scope.value), scope))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return rows


def ensure_subjects(engine, keyring: Keyring, scopes) -> None:
    """Étape 1 de l'ordre de verrouillage : transaction courte, committée."""
    with transaction(engine) as conn:
        state = load_key_state(conn, keyring)
        for kind, version, scope_hmac, _ in _scope_rows(keyring, state, scopes):
            conn.execute(sa.text(
                "INSERT INTO auth_abuse_subjects (id, scope_kind, scope_hmac, hash_key_version, created_at) "
                "VALUES (:id, :kind, :hmac, :version, clock_timestamp()) "
                "ON CONFLICT (scope_kind, hash_key_version, scope_hmac) DO NOTHING"
            ), {"id": uuid.uuid4(), "kind": kind, "hmac": scope_hmac, "version": version})


def select_subjects(conn, keyring: Keyring, state: KeyState, scopes, *, lock: bool) -> Subjects:
    """Sujets existants des portées pour les versions comptées. lock=True :
    étape 2 (FOR UPDATE, une instruction triée) et tous doivent exister."""
    rows = _scope_rows(keyring, state, scopes)
    params, tuples = {}, []
    for i, (kind, version, scope_hmac, _) in enumerate(rows):
        params.update({f"k{i}": kind, f"v{i}": version, f"h{i}": scope_hmac})
        tuples.append(f"(:k{i}, :v{i}, :h{i})")
    found = {}
    if rows:
        found = {(r[1], r[2], r[3]): r[0] for r in conn.execute(sa.text(
            "SELECT id, scope_kind, hash_key_version, scope_hmac FROM auth_abuse_subjects "
            f"WHERE (scope_kind, hash_key_version, scope_hmac) IN ({', '.join(tuples)}) "
            "ORDER BY scope_kind, hash_key_version, scope_hmac" + (" FOR UPDATE" if lock else "")
        ), params)}
    by_scope = {}
    for kind, version, scope_hmac, scope in rows:
        subject_id = found.get((kind, version, scope_hmac))
        if subject_id is None:
            if lock:
                raise SubjectsMissing()
            continue
        by_scope.setdefault((scope.kind, scope.value), {})[version] = subject_id
    return Subjects(state, by_scope)


# --------------------------------------------------------------------------
# Comptages (fenêtres glissantes ]t - W, t])
# --------------------------------------------------------------------------

def count_since(conn, ids, kind: str, since, *, after=None) -> int:
    if not ids:
        return 0
    sql = ("SELECT count(*) FROM auth_abuse_events WHERE subject_id = ANY(:ids) AND event_kind = :kind "
           "AND occurred_at > :since")
    params = {"ids": list(ids), "kind": kind, "since": since}
    if after is not None:
        sql += " AND occurred_at > :after"
        params["after"] = after
    return conn.execute(sa.text(sql), params).scalar_one()


def last_event_at(conn, ids, kind: str):
    if not ids:
        return None
    return conn.execute(sa.text(
        "SELECT max(occurred_at) FROM auth_abuse_events WHERE subject_id = ANY(:ids) AND event_kind = :kind"
    ), {"ids": list(ids), "kind": kind}).scalar_one()


def block_active(conn, ids, at) -> bool:
    """Blocage e-mail en cours : blocked_until > t (à t = blocked_until, levé)."""
    if not ids:
        return False
    return conn.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM auth_abuse_events WHERE subject_id = ANY(:ids) "
        "AND event_kind = 'otp_email_block' AND blocked_until > :at)"
    ), {"ids": list(ids), "at": at}).scalar_one()


def issue_refusals(conn, subjects: Subjects, email: Scope, ip: Scope, at) -> list:
    """[(portée, motif)] des quotas d'émission violés à l'instant t."""
    refusals = []
    email_ids, ip_ids = subjects.ids(email), subjects.ids(ip)
    if block_active(conn, email_ids, at):
        refusals.append((email, "email_blocked"))
    last = last_event_at(conn, email_ids, OTP_REQUEST)
    if last is not None and last > at - ISSUE_MIN_INTERVAL:
        refusals.append((email, "issue_interval"))
    if count_since(conn, email_ids, OTP_REQUEST, at - ISSUE_EMAIL_WINDOW) >= ISSUE_EMAIL_LIMIT:
        refusals.append((email, "quota_email_issue"))
    if count_since(conn, ip_ids, OTP_REQUEST, at - ISSUE_IP_WINDOW) >= ISSUE_IP_LIMIT:
        refusals.append((ip, "quota_ip_issue"))
    return refusals


def verification_ip_exhausted(conn, subjects: Subjects, ip: Scope, at) -> bool:
    return count_since(conn, subjects.ids(ip), OTP_VERIFICATION_ATTEMPT, at - VERIFY_IP_WINDOW) >= VERIFY_IP_LIMIT


# --------------------------------------------------------------------------
# Écritures (ajout seul)
# --------------------------------------------------------------------------

def insert_event(conn, *, subject_id, kind: str, at, operation_id=None, otp_request_id=None,
                 blocked_until=None, event_id=None):
    event_id = event_id or uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO auth_abuse_events (id, subject_id, event_kind, occurred_at, auth_operation_id, otp_request_id, "
        "policy_version, blocked_until) VALUES (:id, :subject, :kind, :at, :op, :req, :policy, :until)"
    ), {"id": event_id, "subject": subject_id, "kind": kind, "at": at, "op": operation_id, "req": otp_request_id,
        "policy": POLICY_VERSION, "until": blocked_until})
    return event_id


def insert_refusal_marker(conn, *, subject_id, kind: str, at) -> None:
    """Au plus un marqueur par (sujet, type, tranche de 5 minutes).

    Ordre de verrouillage : le sujet est pris en FOR KEY SHARE AVANT
    l'insertion. Sans cela, un refus sans verrou réserverait d'abord la
    clé unique du marqueur puis attendrait le sujet (contrôle de FK),
    pendant qu'une transaction tenant le sujet en FOR UPDATE attendrait
    cette clé : interblocage (constaté par tests/test_f2_concurrency.py).
    Ici l'ordre est partout sujet -> marqueur ; deux refus sans verrou ne
    se gênent pas sur le sujet (FOR KEY SHARE est partagé) ; les appelants
    insèrent les marqueurs e-mail avant IP, l'ordre des sujets."""
    if subject_id is None:
        return
    conn.execute(sa.text("SELECT 1 FROM auth_abuse_subjects WHERE id = :s FOR KEY SHARE"), {"s": subject_id})
    conn.execute(sa.text(
        "INSERT INTO auth_abuse_events (id, subject_id, event_kind, occurred_at, policy_version) "
        "VALUES (:id, :subject, :kind, date_bin(interval '5 minutes', CAST(:at AS timestamptz), "
        "timestamptz '2000-01-01 00:00:00+00'), :policy) "
        "ON CONFLICT (subject_id, event_kind, occurred_at) "
        "WHERE event_kind IN ('otp_request_refused', 'otp_verification_refused') DO NOTHING"
    ), {"id": uuid.uuid4(), "subject": subject_id, "kind": kind, "at": at, "policy": POLICY_VERSION})


def record_failure(conn, subjects: Subjects, email: Scope, *, at, operation_id, otp_request_id) -> bool:
    """Échec certain : événement otp_failure ; si c'est le cinquième échec
    e-mail sur 15 minutes postérieur au dernier blocage, blocage durable
    de 15 minutes à partir de cet instant. Retourne True si un blocage est
    créé. Appelé sous le verrou des sujets e-mail."""
    insert_event(conn, subject_id=subjects.recording_id(email), kind=OTP_FAILURE, at=at,
                 operation_id=operation_id, otp_request_id=otp_request_id)
    ids = subjects.ids(email)
    if block_active(conn, ids, at):
        return False
    last_block = last_event_at(conn, ids, OTP_EMAIL_BLOCK)
    failures = count_since(conn, ids, OTP_FAILURE, at - EMAIL_FAILURE_WINDOW, after=last_block)
    if failures < EMAIL_FAILURE_THRESHOLD:
        return False
    insert_event(conn, subject_id=subjects.recording_id(email), kind=OTP_EMAIL_BLOCK, at=at,
                 operation_id=operation_id, otp_request_id=otp_request_id, blocked_until=at + EMAIL_BLOCK_DURATION)
    return True


# --------------------------------------------------------------------------
# Rotation (administration, dormante) et purge
# --------------------------------------------------------------------------

def initialize_abuse_key_state(engine, keyring: Keyring, version: str) -> bool:
    """Crée la ligne d'état si elle n'existe pas (idempotent)."""
    require_key_version(version)
    if not keyring.has("abuse", version):
        raise AbuseUnavailable("version anti-abus inconnue du trousseau")
    with transaction(engine) as conn:
        result = conn.execute(sa.text(
            "INSERT INTO auth_abuse_key_state (id, current_version, updated_at) "
            "VALUES (1, :v, clock_timestamp()) ON CONFLICT (id) DO NOTHING"
        ), {"v": version})
        return result.rowcount == 1


def rotate_abuse_key(engine, keyring: Keyring, new_version: str, *, clock=pg_clock) -> None:
    """current -> previous, new -> current. Exige qu'aucune version
    précédente ne soit encore comptée (au plus deux versions)."""
    require_key_version(new_version)
    if not keyring.has("abuse", new_version):
        raise AbuseUnavailable("nouvelle version anti-abus inconnue du trousseau")
    with transaction(engine) as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": KEY_STATE_LOCK_KEY})
        row = conn.execute(sa.text(
            "SELECT current_version, previous_version FROM auth_abuse_key_state WHERE id = 1 FOR UPDATE"
        )).first()
        if row is None:
            raise AbuseUnavailable("état de clé anti-abus absent")
        if row[1] is not None:
            raise AbuseUnavailable("rotation refusée : la version précédente n'est pas retirée")
        if row[0] == new_version:
            raise AbuseUnavailable("rotation refusée : version déjà courante")
        if not keyring.has("abuse", row[0]):
            raise AbuseUnavailable("rotation refusée : version courante inconnue du trousseau")
        at = clock(conn)
        conn.execute(sa.text(
            "UPDATE auth_abuse_key_state SET previous_version = current_version, previous_demoted_at = :at, "
            "current_version = :v, updated_at = :at WHERE id = 1"
        ), {"v": new_version, "at": at})


def retire_previous_abuse_key(engine, *, clock=pg_clock) -> None:
    """Cesse de compter la version précédente, au plus tôt 90 minutes après
    sa rétrogradation (aucune fenêtre active ne dépend plus d'elle)."""
    with transaction(engine) as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": KEY_STATE_LOCK_KEY})
        row = conn.execute(sa.text(
            "SELECT previous_version, previous_demoted_at FROM auth_abuse_key_state WHERE id = 1 FOR UPDATE"
        )).first()
        if row is None or row[0] is None:
            raise AbuseUnavailable("aucune version précédente à retirer")
        at = clock(conn)
        if at - row[1] < ABUSE_KEY_RETENTION:
            raise AbuseUnavailable("retrait refusé : fenêtres et blocages encore actifs")
        conn.execute(sa.text(
            "UPDATE auth_abuse_key_state SET previous_version = NULL, previous_demoted_at = NULL, updated_at = :at "
            "WHERE id = 1"
        ), {"at": at})


def purge_abuse_history(engine, *, older_than: timedelta, batch: int = 1000, clock=pg_clock) -> tuple:
    """Retire les événements de plus de older_than (>= 2 h) puis les sujets
    sans événement créés avant la même limite, verrouillés SKIP LOCKED (un
    sujet en cours d'utilisation n'est jamais supprimé ; un sujet supprimé
    entre création et verrou fait rejouer l'opération). Dormante : jamais
    planifiée par R1 PR 2."""
    if not isinstance(older_than, timedelta) or older_than < ABUSE_PURGE_MIN_AGE:
        raise ValueError("purge : au moins deux heures d'historique conservées")
    with transaction(engine) as conn:
        cutoff = clock(conn) - older_than
        events = conn.execute(sa.text(
            "DELETE FROM auth_abuse_events WHERE id IN (SELECT id FROM auth_abuse_events WHERE occurred_at < :cutoff "
            "AND (blocked_until IS NULL OR blocked_until < :cutoff) LIMIT :batch)"
        ), {"cutoff": cutoff, "batch": batch}).rowcount
        subjects = conn.execute(sa.text(
            "DELETE FROM auth_abuse_subjects WHERE id IN (SELECT s.id FROM auth_abuse_subjects s "
            "WHERE s.created_at < :cutoff AND NOT EXISTS (SELECT 1 FROM auth_abuse_events e WHERE e.subject_id = s.id) "
            "LIMIT :batch FOR UPDATE SKIP LOCKED)"
        ), {"cutoff": cutoff, "batch": batch}).rowcount
    return events, subjects
