"""Frontière privée F2 -> analytics produit : certification APRÈS COMMIT des
preuves d'utilisation significative (V0-T08 Lot 2B-2, bloc B) — DORMANTE.

UNIQUE module autorisé à consommer la sortie attestée de F2
(AuthorizedTransactionRunner._run_attested) ET à appeler le registre des
preuves (core.product_analytics.proofs). Il n'appartient à aucun des deux
domaines : core.authz_f2 n'importe pas l'analytics, core.product_analytics
n'importe pas F2. AUCUN appelant de production (ni api.py, ni route, ni
worker, ni script ; tests/test_activity_proof_bridge.py) ; aucune variable
d'activation.

run_with_proof_certification(runner, credential, command, *,
analytics_engine, campaign_id) :

L'appelant ne désigne NI utilisateur, NI visite, NI interaction, NI
environnement (audit de la PR #253 : liaison causale interaction ->
opération). Seule la campagne de mesure (configuration serveur) est reçue.

  1. F2 : UN SEUL appel à runner._run_attested(credential, command), sans
     rien capturer ni modifier : son outcome est rendu TEL QUEL (même objet),
     ses exceptions sont celles de run(). Aucune réexécution, jamais. Aucune
     écriture ni lecture analytics avant F2.
  2. Après le retour de F2 (donc après son COMMIT confirmé, seul cas où une
     attestation existe), tout le reste est NON BLOQUANT (toute exception est
     absorbée ici, après le COMMIT métier, jamais dans F2) :
     a. opération établie seulement si : outcome AUTHORIZED, principal
        AuthorizedPrincipal exact, BusinessAttestation exacte, liée au
        principal (propriétaire), à la commande exécutée (nature canonique et
        identifiant canonique : _CERTIFIABLE_COMMANDS) et de qualification
        admise ; new_effect lu sur l'attestation (rejeux et no-op : jamais
        certifiables, aucune écriture analytics, aucune interaction) ;
     a bis. base canonique : la frontière exige que le moteur analytics
        désigne la MÊME base PostgreSQL que le runner F2 (hôte, port, base) ;
        sinon aucune émission (source_unverified) ;
     b. transaction analytics 1 : proofs.open_origin émet l'origine de CETTE
        opération : interaction 'activity' NOUVELLE (clé générée par le
        service), propriétaire = principal.user_id, environnement = la
        session F2 AUTHENTIFIÉE qui a exécuté l'opération
        (principal.session_id, source serveur de environment_key choisie par
        ce raccordement : contrat 2B-1), visite déterminée par le service
        2B-1. Une interaction ancienne, d'un autre appareil ou d'une autre
        session ne peut donc jamais être l'origine ; un environnement dont la
        déconnexion 2B-1 est inscrite n'est jamais rouvert (vérification
        sous le verrou d'environnement de 2B-1 : absent / not_authorized) ;
        PUIS, dans la MÊME transaction et en DERNIER verrou (arbitrage du
        fondateur, option A, exception nominative) : lecture minimale de la
        session F2 canonique (oryx_sessions, id = principal.session_id) FOR
        SHARE ; propriétaire = principal, statut 'active', non révoquée, et
        horloge PostgreSQL < absolute_expires_at (lue après le verrou ;
        l'égalité refuse, comme le runner). Sinon l'émission ENTIÈRE est
        annulée (ROLLBACK : ni interaction, ni visite, ni prolongation) :
        révoquée ou expirée -> not_authorized ; absente, d'un autre
        propriétaire ou incohérente -> source_unverified. Le verrou partagé
        est gardé jusqu'au COMMIT de l'émission : une révocation F2
        (revoke_session, FOR UPDATE sur la même ligne) committée avant est
        vue, une révocation en cours est attendue puis vue, une révocation
        postérieure attend ce COMMIT (l'interaction émise reste
        historiquement valide). Contrôle de non-révocation seulement : ni
        authentification, ni secret, ni génération, ni écriture F2 ;
     c. transaction analytics 2 : proofs.register_pending (seule l'origine
        émise en b, liée à cette opération, est acceptée) ;
     d. transaction analytics 3 : proofs.certify_pending (certified_at =
        horloge PostgreSQL lue après les verrous, après le COMMIT F2 et après
        les transactions 1 et 2) ;
     e. certification en échec ou COMMIT analytics incertain : transaction 4,
        proofs.settle (relecture sous verrou de la MÊME preuve : certified
        constatée, pending -> indeterminate) ; si elle échoue aussi, l'issue
        reste inconnue (UNKNOWN) et rien n'est prétendu.

Aucun événement définitif, aucune attribution (bloc C). Journaux : états et
motifs FERMÉS seulement (aucun identifiant, aucun propriétaire, aucun
contenu, aucun secret).
"""
import logging
import uuid
from dataclasses import dataclass
from types import MappingProxyType

import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.authz_f2 import attestations as att
from core.authz_f2 import commands as c
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, AttestedOutcome, AuthorizedTransactionRunner
from core.product_analytics import proofs

logger = logging.getLogger("oryx.activity_proofs")

# Commande F2 exécutée -> (nature canonique, attribut de l'identifiant
# canonique). Égal au registre attesté de F2 (testé), jamais
# portfolio_write ni assistant_delivery.
_CERTIFIABLE_COMMANDS = MappingProxyType({
    c.AcknowledgeDecryptageDelivery: (att.DECRYPTAGE_DELIVERY_ACK, "assistant_turn_id"),
    c.CreatePortfolioPosition: (att.PORTFOLIO_CREATE, "operation_id"),
    c.ReplacePortfolioPosition: (att.PORTFOLIO_UPDATE, "operation_id"),
    c.DeletePortfolioPosition: (att.PORTFOLIO_DELETE, "operation_id"),
})

# Lecture minimale de la session F2 canonique (exception nominative,
# arbitrage du fondateur, option A du § 5.4) : colonnes de non-révocation
# seulement, jamais secret, empreinte, génération ni écriture. FOR SHARE :
# sérialisée avec revoke_session (FOR UPDATE sur la même ligne).
_F2_SESSION_STATUS = sa.text("SELECT user_id, status, revoked_at, absolute_expires_at FROM oryx_sessions "
                             "WHERE id = :session_id FOR SHARE")
_F2_ACTIVE, _F2_REVOKED = "active", "revoked"

# Garde-fous de chaque transaction analytics (SET LOCAL).
ANALYTICS_GUARDS = ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCAL lock_timeout = '2s'",
                    "SET LOCAL statement_timeout = '5s'")

# Issue d'une transaction analytics.
_COMMITTED, _FAILED, _UNCERTAIN, _DISCARDED = "committed", "failed", "uncertain", "discarded"


class _Discard(Exception):
    """Levée par un travail analytics pour ANNULER sa transaction (ROLLBACK)
    en rendant une valeur fermée."""

    def __init__(self, value):
        super().__init__("transaction analytics annulée")
        self.value = value

# État rendu quand aucune lecture fiable ne permet de conclure.
UNKNOWN = "unknown"
REPORT_STATES = proofs.DECISION_STATES + (UNKNOWN,)


@dataclass(frozen=True, slots=True)
class ProofReport:
    """Issue de la mesure (jamais une condition de l'opération) : state
    REPORT_STATES (état de la preuve de cette origine après l'appel, ABSENT
    si aucune, UNKNOWN si rien n'a pu être établi) ; reason : motif fermé
    (proofs.RESOLUTION_REASONS) ou None."""
    state: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CertifiedRun:
    """outcome : EXACTEMENT l'AuthorizationOutcome rendu par F2 ; proof :
    ProofReport."""
    outcome: object
    proof: ProofReport


def _transaction(engine, work):
    """(issue, valeur) d'une transaction analytics DISTINCTE de F2. Jamais
    d'exception : _FAILED (rien committé), _DISCARDED (annulée par le travail
    lui-même, rien committé ; valeur de _Discard), _UNCERTAIN (COMMIT envoyé,
    issue inconnue ; la valeur est celle calculée avant le COMMIT),
    _COMMITTED."""
    try:
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    except Exception:  # noqa: BLE001 — moteur inutilisable
        return _FAILED, None
    try:
        try:
            for guard in ANALYTICS_GUARDS:
                session.execute(sa.text(guard))
            value = work(session)
        except _Discard as discard:  # refus : rien n'est committé (aucun COMMIT envoyé)
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 — connexion perdue : le serveur annule
                pass
            return _DISCARDED, discard.value
        except Exception:  # noqa: BLE001 — rien n'est committé
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return _FAILED, None
        try:
            session.commit()
        except Exception:  # noqa: BLE001 — confirmation perdue : jamais présumée
            return _UNCERTAIN, value
        return _COMMITTED, value
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _committed_operation(command, attested) -> tuple:
    """(proofs.CommittedOperation, None) ou (None, motif) depuis la SEULE
    sortie attestée de F2 ; ne lève jamais."""
    try:
        if type(attested) is not AttestedOutcome:
            return None, proofs.SOURCE_UNVERIFIED
        outcome, attestation = attested.outcome, attested.attestation
        expected = _CERTIFIABLE_COMMANDS.get(type(command))
        if outcome.status != AUTHORIZED or expected is None or type(attestation) is not att.BusinessAttestation:
            return None, proofs.SOURCE_UNVERIFIED  # refus, rollback, COMMIT incertain, aucune attestation
        principal = outcome.principal
        operation_id = getattr(command, expected[1])
        if (type(principal) is not AuthorizedPrincipal or type(operation_id) is not uuid.UUID
                or attestation.owner_user_id != principal.user_id or attestation.operation != expected[0]
                or attestation.operation_id != operation_id
                or attestation.qualification not in att.ADMISSIBLE[attestation.operation]):
            return None, proofs.SOURCE_UNVERIFIED
        return proofs.CommittedOperation(principal.user_id, attestation.operation, operation_id,
                                         attestation.qualification in att.NEW_EFFECTS), None
    except Exception:  # noqa: BLE001
        return None, proofs.SOURCE_UNVERIFIED


def _report(decision) -> ProofReport:
    return ProofReport(decision.state, decision.reason)


def _same_canonical_database(runner, engine) -> bool:
    """Le moteur analytics désigne-t-il la MÊME base PostgreSQL que le runner
    F2 (hôte, port, nom de base) ? Seule condition pour que FOR SHARE et la
    révocation F2 portent sur la même ligne. Lecture seule de la
    configuration ; jamais d'exception (doute : non)."""
    try:
        f2, analytics = runner._engine.url, engine.url
        return (f2.database is not None and f2.get_backend_name() == analytics.get_backend_name() == "postgresql"
                and (f2.host, f2.port, f2.database) == (analytics.host, analytics.port, analytics.database))
    except Exception:  # noqa: BLE001
        return False


def _f2_session_problem(db, principal) -> str | None:
    """Contrôle de non-révocation de la session F2 qui a exécuté
    l'opération, DANS la transaction d'émission et APRÈS tous ses verrous
    analytics (voir l'en-tête). None si active ; sinon motif fermé. Le
    verrou partagé est gardé jusqu'à la fin de la transaction."""
    row = db.execute(_F2_SESSION_STATUS, {"session_id": str(principal.session_id)}).mappings().one_or_none()
    if row is None or row["user_id"] != principal.user_id:
        return proofs.SOURCE_UNVERIFIED  # absente ou d'un autre propriétaire
    if row["status"] == _F2_REVOKED:
        return proofs.NOT_AUTHORIZED  # logout, admin ou security : même protection
    if row["status"] != _F2_ACTIVE or row["revoked_at"] is not None or row["absolute_expires_at"] is None:
        return proofs.SOURCE_UNVERIFIED  # incohérente
    now = db.execute(sa.text("SELECT clock_timestamp()")).scalar_one()  # lue APRÈS le verrou
    if now >= row["absolute_expires_at"]:
        return proofs.NOT_AUTHORIZED  # expiration absolue atteinte : l'égalité refuse
    return None


def _open_origin(engine, campaign_id, principal, operation) -> tuple:
    """(OriginSnapshot émis, None) ou (None, motif fermé) ; ne lève jamais.
    Panne, refus ou COMMIT incertain : aucune origine n'est présumée (le
    registre n'a pas été touché). Un refus de la session F2 annule TOUTE
    l'émission."""
    def open_(db):
        try:
            origin = proofs.open_origin(db, campaign_id=campaign_id, environment_key=principal.session_id,
                                        operation=operation)
        except proofs.ProofRefused as refused:
            return None, refused.reason  # rien n'a été écrit
        except proofs.InvalidProductAnalyticsInput:
            return None, proofs.ORIGIN_UNVERIFIED
        problem = _f2_session_problem(db, principal)  # dernier verrou, gardé jusqu'au COMMIT
        if problem is not None:
            raise _Discard((None, problem))  # ROLLBACK : ni interaction, ni visite, ni prolongation
        return origin, None

    status, value = _transaction(engine, open_)
    if status == _DISCARDED:
        return value
    if status != _COMMITTED:
        return None, proofs.TECHNICAL_UNCERTAINTY
    return value


def _certify(engine, origin, operation) -> ProofReport:
    """Transactions analytics 2 à 4 (voir l'en-tête)."""
    status, registered = _transaction(engine, lambda db: proofs.register_pending(db, origin=origin,
                                                                                 operation=operation))
    if status == _FAILED:
        return ProofReport(UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)  # rien n'a été inscrit
    if registered.state != proofs.PENDING:
        return _report(registered)  # absente (motif), déjà certifiée, ou déjà résolue : rien n'a été écrit
    # COMMIT de l'inscription incertain : la certification relit la MÊME preuve
    # (identifiant et identité) sous verrou et ne crée jamais rien.
    proof_id = registered.proof_id
    status, certified = _transaction(engine, lambda db: proofs.certify_pending(db, origin=origin, operation=operation,
                                                                              proof_id=proof_id))
    if status == _COMMITTED:
        return _report(certified)
    if status == _UNCERTAIN and not certified.written:
        return _report(certified)  # rien n'était à écrire : l'état lu sous verrou suffit
    status, settled = _transaction(engine, lambda db: proofs.settle(db, origin=origin, operation=operation,
                                                                    proof_id=proof_id))
    if status != _COMMITTED and (status == _FAILED or settled.written):
        return ProofReport(UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    return _report(settled)


def _log(report: ProofReport) -> None:
    level = logging.INFO if report.state in (proofs.CERTIFIED, proofs.ABSENT) else logging.WARNING
    logger.log(level, "activity_proof state=%s reason=%s", report.state, report.reason)


def run_with_proof_certification(runner, credential, command, *, analytics_engine, campaign_id) -> CertifiedRun:
    """Opération F2 inchangée, puis mesure non bloquante (voir l'en-tête)."""
    if type(runner) is not AuthorizedTransactionRunner:
        raise TypeError("AuthorizedTransactionRunner attendu")

    attested = runner._run_attested(credential, command)  # UN SEUL appel F2 ; jamais rejoué

    try:
        operation, reason = _committed_operation(command, attested)
        if operation is None:
            report = ProofReport(proofs.ABSENT, reason)
        elif not operation.new_effect:
            report = ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT)  # rejeu, no-op : rien n'est écrit
        elif not _same_canonical_database(runner, analytics_engine):
            report = ProofReport(proofs.ABSENT, proofs.SOURCE_UNVERIFIED)  # verrou F2 sans portée : rien n'est écrit
        else:
            # Environnement = session F2 authentifiée qui a exécuté l'opération
            # (principal vérifié par _committed_operation) ; jamais l'appelant.
            origin, origin_reason = _open_origin(analytics_engine, campaign_id, attested.outcome.principal,
                                                 operation)
            report = (ProofReport(proofs.ABSENT, origin_reason) if origin is None
                      else _certify(analytics_engine, origin, operation))
    except Exception:  # noqa: BLE001 — la mesure ne fait jamais échouer l'opération
        report = ProofReport(UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    try:
        _log(report)
    except Exception:  # noqa: BLE001
        pass
    return CertifiedRun(attested.outcome, report)
