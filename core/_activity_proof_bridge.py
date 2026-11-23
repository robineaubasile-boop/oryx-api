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
analytics_engine, campaign_id, user_id, visit_id, interaction_key) :

  1. Origine AVANT F2 (transaction analytics courte, lecture seule) :
     proofs.read_origin observe l'interaction 'activity' DÉJÀ acceptée et
     committée (2B-1) désignée par l'appelant. Absente, d'un autre type,
     d'une autre visite ou hors observation : aucune preuve possible. Une
     interaction créée pendant ou après l'opération n'est donc jamais une
     origine (aucune justification rétroactive).
  2. F2 : UN SEUL appel à runner._run_attested(credential, command), sans
     rien capturer ni modifier : son outcome est rendu TEL QUEL (même objet),
     ses exceptions sont celles de run(). Aucune réexécution, jamais.
  3. Après le retour de F2 (donc après son COMMIT confirmé, seul cas où une
     attestation existe), tout le reste est NON BLOQUANT (toute exception est
     absorbée ici, après le COMMIT métier, jamais dans F2) :
     a. opération établie seulement si : outcome AUTHORIZED, principal
        AuthorizedPrincipal exact, BusinessAttestation exacte, liée au
        principal (propriétaire), à la commande exécutée (nature canonique et
        identifiant canonique : _CERTIFIABLE_COMMANDS) et de qualification
        admise ; new_effect lu sur l'attestation (rejeux et no-op : jamais
        certifiables) ; propriétaire de l'origine = principal ;
     b. transaction analytics 1 : proofs.register_pending ;
     c. transaction analytics 2 : proofs.certify_pending (certified_at =
        horloge PostgreSQL lue après les verrous, après le COMMIT F2 et après
        la transaction 1) ;
     d. certification en échec ou COMMIT analytics incertain : transaction 3,
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

# Garde-fous de chaque transaction analytics (SET LOCAL).
ANALYTICS_GUARDS = ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", "SET LOCAL lock_timeout = '2s'",
                    "SET LOCAL statement_timeout = '5s'")

# Issue d'une transaction analytics.
_COMMITTED, _FAILED, _UNCERTAIN = "committed", "failed", "uncertain"

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
    d'exception : _FAILED (rien committé), _UNCERTAIN (COMMIT envoyé, issue
    inconnue ; la valeur est celle calculée avant le COMMIT), _COMMITTED."""
    try:
        session = Session(bind=engine, autoflush=False, expire_on_commit=False)
    except Exception:  # noqa: BLE001 — moteur inutilisable
        return _FAILED, None
    try:
        try:
            for guard in ANALYTICS_GUARDS:
                session.execute(sa.text(guard))
            value = work(session)
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


def _certify(engine, origin, operation) -> ProofReport:
    """Transactions analytics 1 à 3 (voir l'en-tête)."""
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


def _observe_origin(engine, campaign_id, user_id, visit_id, interaction_key) -> tuple:
    """(OriginSnapshot, None) ou (None, motif fermé) ; ne lève jamais. Une
    panne vaut technical_uncertainty : aucune origine n'est présumée."""
    def observe(db):
        try:
            return proofs.read_origin(db, campaign_id=campaign_id, user_id=user_id, visit_id=visit_id,
                                      interaction_key=interaction_key), None
        except proofs.ProofRefused as refused:
            return None, refused.reason
        except proofs.InvalidProductAnalyticsInput:
            return None, proofs.ORIGIN_UNVERIFIED

    status, value = _transaction(engine, observe)
    if status != _COMMITTED:
        return None, proofs.TECHNICAL_UNCERTAINTY
    return value


def run_with_proof_certification(runner, credential, command, *, analytics_engine, campaign_id, user_id, visit_id,
                                 interaction_key) -> CertifiedRun:
    """Opération F2 inchangée, puis mesure non bloquante (voir l'en-tête)."""
    if type(runner) is not AuthorizedTransactionRunner:
        raise TypeError("AuthorizedTransactionRunner attendu")
    origin, origin_reason = _observe_origin(analytics_engine, campaign_id, user_id, visit_id, interaction_key)

    attested = runner._run_attested(credential, command)  # UN SEUL appel F2 ; jamais rejoué

    try:
        operation, reason = _committed_operation(command, attested)
        if operation is None:
            report = ProofReport(proofs.ABSENT, reason)
        elif not operation.new_effect:
            report = ProofReport(proofs.ABSENT, proofs.NO_NEW_EFFECT)  # rejeu, no-op : rien n'est écrit
        elif origin is None:
            report = ProofReport(proofs.ABSENT, origin_reason)
        elif operation.owner_user_id != origin.user_id:
            report = ProofReport(proofs.ABSENT, proofs.OWNER_CONFLICT)
        else:
            report = _certify(analytics_engine, origin, operation)
    except Exception:  # noqa: BLE001 — la mesure ne fait jamais échouer l'opération
        report = ProofReport(UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    try:
        _log(report)
    except Exception:  # noqa: BLE001
        pass
    return CertifiedRun(attested.outcome, report)
