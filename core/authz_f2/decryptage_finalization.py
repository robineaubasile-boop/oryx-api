"""Finalisation Décrypter F2 (R1 PR 9-C1-B) : vues fermées et projection
financière — module PUR, dormant.

Résultats fermés de la commande FinalizeDecryptageCandidate (aucun payload,
aucun texte Claude, aucun secret de bail ; identifiants techniques et
vocabulaires fermés seulement) :

| Vue | Effet durable committé |
|---|---|
| DecryptageFinalized | livraison canonique + capture cognitive + progression + réservation finalized, dans UNE transaction |
| AlreadyFinalized | aucun (reconnaissance terminale ; le contenu canonique relève de PR 9-C2) |
| FinalizationContextChanged | aucun (contexte B2 différent : SAVEPOINT annulé) |
| FinalizationPreparationRefused | aucun (refus fermé de préparation B2) |
| LegacyCollisionBlocked | réservation blocked / legacy_collision (preuve relue, bail encore valide) |
| core.authz_f2.decryptage_reservation.ReservationBlocked | aucun (réservation déjà terminale) |

La projection progress_facts transforme le FinancialSnapshot typé B2 en
données attendues par la progression historique (champs de faits
supportés seulement) : valeur finie réelle (zéro compris) telle quelle,
absence ou valeur non admise -> None ; aucune conversion d'unité ni de
devise, aucune valeur antérieure réutilisée.

N'importe aucun module applicatif, n'ouvre aucune connexion.
"""
import uuid
from dataclasses import dataclass

from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_market_data as dm
from core.authz_f2 import decryptage_reservation as dr

# Champs de faits de la progression historique (core.decryptage_progress.
# _FACT_FIELDS, recopiés : égalité vérifiée par les tests).
PROGRESS_FACT_FIELDS = ("operating_margin", "roe", "roic", "gross_margin_latest", "fcf_per_share", "revenue_growth",
                        "net_cash", "eps")


def _uuid(value) -> bool:
    return type(value) is uuid.UUID


@dataclass(frozen=True, slots=True)
class DecryptageFinalized:
    """Tour finalisé par CETTE commande (COMMIT confirmé)."""
    reservation_id: uuid.UUID
    assistant_delivery_id: uuid.UUID

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _uuid(self.assistant_delivery_id):
            raise ValueError("finalisation hors contrat")


@dataclass(frozen=True, slots=True)
class AlreadyFinalized:
    """Réservation déjà finalisée pour la MÊME intention et le MÊME epoch :
    aucune écriture, aucun contenu."""
    reservation_id: uuid.UUID
    assistant_delivery_id: uuid.UUID

    def __post_init__(self):
        if not _uuid(self.reservation_id) or not _uuid(self.assistant_delivery_id):
            raise ValueError("finalisation reconnue hors contrat")


@dataclass(frozen=True, slots=True)
class FinalizationContextChanged:
    """Le contexte pédagogique relu sous verrous diffère du snapshot du
    candidat : rien n'est écrit, la réservation reste reserved."""
    reservation_id: uuid.UUID

    def __post_init__(self):
        if not _uuid(self.reservation_id):
            raise ValueError("contexte modifié hors contrat")


@dataclass(frozen=True, slots=True)
class FinalizationPreparationRefused:
    """Refus fermé de préparation (vocabulaire B2) : rien n'est écrit."""
    reservation_id: uuid.UUID
    reason: str

    def __post_init__(self):
        if not _uuid(self.reservation_id) or type(self.reason) is not str or self.reason not in dc.REFUSAL_REASONS:
            raise ValueError("refus de préparation hors contrat")


@dataclass(frozen=True, slots=True)
class LegacyCollisionBlocked:
    """Collision legacy PROUVÉE : blocage legacy_collision committé."""
    reservation_id: uuid.UUID

    def __post_init__(self):
        if not _uuid(self.reservation_id):
            raise ValueError("blocage de collision hors contrat")


FINALIZE_RESULTS = (DecryptageFinalized, AlreadyFinalized, FinalizationContextChanged, FinalizationPreparationRefused,
                    LegacyCollisionBlocked, dr.ReservationBlocked)


def progress_facts(market: dm.FinancialSnapshot) -> dict:
    """NOUVEAU dict {champ de fait: float fini ou None} (aucune conversion)."""
    if type(market) is not dm.FinancialSnapshot:
        raise TypeError("instantané financier B2 attendu")
    out = {}
    for name in PROGRESS_FACT_FIELDS:
        field = market.field(name)
        out[name] = field.value if field.status == dm.PRESENT else None
    return out
