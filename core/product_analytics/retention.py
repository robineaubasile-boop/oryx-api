"""Validation métier de la conservation des événements (V0-T08 Lot 2A) —
DORMANTE.

Document retention-scopes-v1 (colonne product_activity_events.retention_scopes) :

    {"version": "retention-scopes-v1",
     "scopes": [{"kind": "campaign", "campaign_id": "<uuid canonique>",
                 "policy_version": "<politique de la campagne>",
                 "expires_at": "YYYY-MM-DDTHH:MM:SS.ffffffZ"}, ...]}

La migration 0022 n'impose que la structure EXTÉRIEURE ; ce module impose le
reste :

- finalités autorisées : kind dans SCOPE_KINDS (seule « campaign »), clés
  exactes, campaign_id UUID canonique, une seule finalité par campagne,
  policy_version du registre fermé, expires_at au format UTC canonique ;
  liste non vide, triée par (expires_at, campaign_id) ;
- rattachement : chaque campagne existe, porte la même politique de
  conservation, et l'utilisateur y a une participation dont la fenêtre
  couvre occurred_at ; expires_at <= occurred_at + retention_days de la
  campagne (aucune conservation au-delà de l'autorisation prévue) ;
- échéances : next_scope_expires_at = première échéance restante (minimum),
  purge_after_at = dernière échéance autorisée (maximum) ;
- révision (règle réservée au lot 2C, qui seul supprimera) : chaque finalité
  révisée existait déjà, avec le même kind et la même politique, et une
  échéance identique ou plus proche. Aucune prolongation rétroactive, aucune
  finalité ajoutée rétroactivement.

Le lot 2A ne révise jamais un document stocké : aucun service n'écrit
retention_scopes hors de la création d'un événement, et une répétition
idempotente renvoie l'événement stocké sans le modifier.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.models import ProductCampaign, ProductCampaignParticipant
from core.product_analytics import common
from core.product_analytics.common import RETENTION_POLICIES, InvalidRetentionScopes, RetentionRevisionRefused

RETENTION_SCOPES_VERSION = "retention-scopes-v1"
CAMPAIGN_SCOPE = "campaign"
SCOPE_KINDS = (CAMPAIGN_SCOPE,)
DOCUMENT_KEYS = frozenset({"version", "scopes"})
SCOPE_KEYS = frozenset({"kind", "campaign_id", "policy_version", "expires_at"})
INSTANT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class RetentionScope:
    kind: str
    campaign_id: uuid.UUID
    policy_version: str
    expires_at: datetime

    def sort_key(self):
        return self.expires_at, str(self.campaign_id)


def format_instant(value: datetime) -> str:
    return common.require_aware(value, "expires_at").strftime(INSTANT_FORMAT)


def parse_instant(value) -> datetime:
    if not isinstance(value, str) or not INSTANT_PATTERN.fullmatch(value):
        raise InvalidRetentionScopes("expires_at : instant UTC canonique requis")
    try:
        return datetime.strptime(value, INSTANT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise InvalidRetentionScopes("expires_at : instant invalide") from None


def _parse_scope(item) -> RetentionScope:
    if not isinstance(item, dict) or set(item) != SCOPE_KEYS:
        raise InvalidRetentionScopes("finalité : clés exactes kind, campaign_id, policy_version, expires_at requises")
    if item["kind"] not in SCOPE_KINDS:
        raise InvalidRetentionScopes("finalité : kind non autorisé")
    raw_id = item["campaign_id"]
    try:
        campaign_id = uuid.UUID(raw_id) if isinstance(raw_id, str) else None
    except ValueError:
        campaign_id = None
    if campaign_id is None or str(campaign_id) != raw_id:
        raise InvalidRetentionScopes("finalité : campaign_id UUID canonique requis")
    if not isinstance(item["policy_version"], str) or item["policy_version"] not in RETENTION_POLICIES:
        raise InvalidRetentionScopes("finalité : politique de conservation inconnue")
    return RetentionScope(kind=item["kind"], campaign_id=campaign_id, policy_version=item["policy_version"],
                          expires_at=parse_instant(item["expires_at"]))


def parse_retention_scopes(document) -> tuple:
    """Document -> finalités (RetentionScope), ou InvalidRetentionScopes."""
    if not isinstance(document, dict) or set(document) != DOCUMENT_KEYS:
        raise InvalidRetentionScopes("document : clés exactes version et scopes requises")
    if document["version"] != RETENTION_SCOPES_VERSION or not isinstance(document["version"], str):
        raise InvalidRetentionScopes("document : version non prise en charge")
    items = document["scopes"]
    if not isinstance(items, list) or not items:
        raise InvalidRetentionScopes("document : au moins une finalité requise")
    scopes = tuple(_parse_scope(item) for item in items)
    if len({scope.campaign_id for scope in scopes}) != len(scopes):
        raise InvalidRetentionScopes("document : une seule finalité par campagne")
    if list(scopes) != sorted(scopes, key=RetentionScope.sort_key):
        raise InvalidRetentionScopes("document : finalités non triées par échéance")
    return scopes


def build_retention_document(scopes) -> dict:
    """Finalités -> document canonique (trié, validé en retour)."""
    ordered = sorted(scopes, key=RetentionScope.sort_key)
    document = {"version": RETENTION_SCOPES_VERSION, "scopes": [
        {"kind": scope.kind, "campaign_id": str(scope.campaign_id), "policy_version": scope.policy_version,
         "expires_at": format_instant(scope.expires_at)} for scope in ordered]}
    parse_retention_scopes(document)
    return document


def retention_bounds(scopes) -> tuple:
    """(next_scope_expires_at, purge_after_at) = (première, dernière) échéance."""
    if not scopes:
        raise InvalidRetentionScopes("document : au moins une finalité requise")
    expirations = [scope.expires_at for scope in scopes]
    return min(expirations), max(expirations)


def validate_retention_columns(document, *, next_scope_expires_at, purge_after_at) -> tuple:
    """Document valide ET colonnes égales à ses première et dernière
    échéances. Retourne les finalités."""
    scopes = parse_retention_scopes(document)
    expected = retention_bounds(scopes)
    actual = (common.require_aware(next_scope_expires_at, "next_scope_expires_at"),
              common.require_aware(purge_after_at, "purge_after_at"))
    if actual != expected:
        raise InvalidRetentionScopes("échéances : next_scope_expires_at / purge_after_at incohérentes")
    return scopes


def authorized_expiry(occurred_at: datetime, retention_days: int) -> datetime:
    """Dernière échéance autorisée par une campagne pour un événement :
    occurred_at + retention_days. Ancrage sur occurred_at = décision produit
    figée (« analytics conservée 90 jours depuis occurred_at »), jamais
    recorded_at ni la clôture de la campagne."""
    return occurred_at + timedelta(days=retention_days)


def campaign_scope(campaign: ProductCampaign, occurred_at: datetime) -> RetentionScope:
    return RetentionScope(kind=CAMPAIGN_SCOPE, campaign_id=campaign.id,
                          policy_version=campaign.retention_policy_version,
                          expires_at=authorized_expiry(occurred_at, campaign.retention_days))


def verify_scopes_against_campaigns(db, scopes, *, user_id: str, occurred_at: datetime) -> None:
    """Rattachement de chaque finalité : campagne existante, même politique,
    participation de user_id dont la fenêtre couvre occurred_at, échéance
    <= occurred_at + retention_days. Lecture seule."""
    occurred_at = common.require_aware(occurred_at, "occurred_at")
    ids = [scope.campaign_id for scope in scopes]
    rows = db.execute(
        select(ProductCampaign, ProductCampaignParticipant)
        .join(ProductCampaignParticipant, ProductCampaignParticipant.campaign_id == ProductCampaign.id)
        .where(ProductCampaign.id.in_(ids), ProductCampaignParticipant.user_id == user_id)
    ).all()
    found = {campaign.id: (campaign, participation) for campaign, participation in rows}
    for scope in scopes:
        if scope.campaign_id not in found:
            raise InvalidRetentionScopes(f"finalité : campagne {scope.campaign_id} non rattachée à l'utilisateur")
        campaign, participation = found[scope.campaign_id]
        if scope.policy_version != campaign.retention_policy_version:
            raise InvalidRetentionScopes(f"finalité : politique différente de la campagne {campaign.id}")
        if not participation.window_start_at <= occurred_at < participation.window_end_at:
            raise InvalidRetentionScopes(f"finalité : événement hors fenêtre de la campagne {campaign.id}")
        if scope.expires_at > authorized_expiry(occurred_at, campaign.retention_days):
            raise InvalidRetentionScopes(f"finalité : conservation au-delà de l'autorisation de {campaign.id}")


def validate_scope_revision(previous_document, revised_document) -> tuple:
    """Règle de révision (raccourcissement ou retrait seulement). Retourne
    les finalités révisées. Un document révisé vide n'est pas une révision
    mais une suppression de l'événement (lot 2C)."""
    previous = {scope.campaign_id: scope for scope in parse_retention_scopes(previous_document)}
    revised = parse_retention_scopes(revised_document)
    for scope in revised:
        before = previous.get(scope.campaign_id)
        if before is None:
            raise RetentionRevisionRefused(f"finalité ajoutée rétroactivement : campagne {scope.campaign_id}")
        if (scope.kind, scope.policy_version) != (before.kind, before.policy_version):
            raise RetentionRevisionRefused(f"finalité modifiée : campagne {scope.campaign_id}")
        if scope.expires_at > before.expires_at:
            raise RetentionRevisionRefused(f"prolongation rétroactive : campagne {scope.campaign_id}")
    return revised


# --------------------------------------------------------------------------
# Classification d'un document relativement à UNE campagne (correctif R5 bis
# de la PR 3 du lot 2C, extrait SANS changement sémantique pour être partagé
# par la purge des participations (PR 3, périmètre d'un utilisateur) et la
# purge définitive des campagnes (PR 4, périmètre global)). Fonctions PURES :
# aucune transaction, aucune table, aucune décision de suppression, aucune
# modification du document ; un document seulement interprétable n'est
# JAMAIS déclaré canonique.
# --------------------------------------------------------------------------

REFERENCE_ABSENT = "absent"  # aucune finalité ne désigne la campagne (document canonique ou identifiable)
REFERENCE_PRESENT = "present"  # document canonique avec une finalité de la campagne, échue ou non
REFERENCE_INCOHERENT = "incoherent"  # document hors contrat qui la désigne, ou qui ne permet pas de l'exclure
CAMPAIGN_REFERENCES = (REFERENCE_ABSENT, REFERENCE_PRESENT, REFERENCE_INCOHERENT)

# Lecture tolérante : seules les deux notations hexadécimales usuelles d'un
# UUID sont interprétées (8-4-4-4-12 ou 32 chiffres, casse indifférente) ;
# toute autre notation (accolades, urn:uuid:, espaces) est ambiguë.
_LENIENT_UUID = re.compile(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                           r"|[0-9a-fA-F]{32})$")
# Anomalies structurelles converties en classification (jamais une exception).
_DOCUMENT_ERRORS = (InvalidRetentionScopes, common.InvalidProductAnalyticsInput, TypeError, ValueError, KeyError,
                    AttributeError)


def lenient_campaigns(document) -> set | None:
    """Lecture LIMITÉE d'un document refusé par parse_retention_scopes,
    destinée EXCLUSIVEMENT à établir qu'aucune finalité ne désigne une
    campagne donnée ; jamais une validation, une réparation ni
    l'interprétation d'un schéma inconnu. Rend les campagnes désignées, ou
    None (ambigu : aucune campagne ne peut être exclue) sauf si :

    - le document a exactement les clés et la version retention-scopes-v1 ;
    - chaque finalité est un objet aux clés EXACTES du contrat (kind,
      campaign_id, policy_version, expires_at), de nature connue (kind =
      'campaign', seule nature du contrat) ;
    - chaque campaign_id est une chaîne d'une notation hexadécimale usuelle
      d'UUID (_LENIENT_UUID).

    Les autres valeurs (politique, échéance, ordre, doublons) ne sont pas
    lues : leurs anomalies relèvent du contrôle d'intégrité des événements."""
    try:
        if (not isinstance(document, dict) or set(document) != DOCUMENT_KEYS
                or document["version"] != RETENTION_SCOPES_VERSION):
            return None
        items = document["scopes"]
        if not isinstance(items, list) or not items:
            return None
        found = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != SCOPE_KEYS:
                return None  # structure ambiguë (clé absente ou supplémentaire)
            if type(item["kind"]) is not str or item["kind"] != CAMPAIGN_SCOPE:
                return None  # nature inconnue
            raw = item["campaign_id"]
            if type(raw) is not str or not _LENIENT_UUID.fullmatch(raw):
                return None  # identifiant absent, illisible ou ambigu
            found.add(uuid.UUID(raw))
        return found
    except _DOCUMENT_ERRORS:
        return None


def campaign_reference(document, campaign_id: uuid.UUID) -> str:
    """Classification PURE de `document` relativement à `campaign_id` :

    - document conforme (parse_retention_scopes) avec une finalité de la
      campagne : REFERENCE_PRESENT ;
    - document conforme sans finalité de la campagne : REFERENCE_ABSENT (une
      anomalie de rattachement ou d'échéance d'une AUTRE campagne relève du
      contrôle d'intégrité des événements) ;
    - document hors contrat dont la structure et la nature restent
      identifiables (lenient_campaigns) : REFERENCE_INCOHERENT s'il désigne la
      campagne, REFERENCE_ABSENT sinon ;
    - structure indéterminable : REFERENCE_INCOHERENT (la campagne ne peut
      pas être exclue).

    Ordre d'évaluation (lot 2C, PR 4 : coût du contrôle global), sans aucun
    changement de classification : tout document accepté par
    parse_retention_scopes l'est aussi par lenient_campaigns, avec les mêmes
    campagnes (clés, version, nature et notation canonique exigées par la
    première sont un cas particulier de la seconde). Donc lenient_campaigns
    rend None => analyse stricte refusée => REFERENCE_INCOHERENT ; la
    campagne absente de l'ensemble lu => absente de toute finalité stricte =>
    REFERENCE_ABSENT ; l'analyse stricte (échéances comprises) ne départage
    que les documents qui désignent la campagne (PRESENT ou INCOHERENT).
    Équivalence avec la formulation d'origine testée (formes R5 bis, formes
    d'enveloppe, documents altérés aléatoirement)."""
    named = lenient_campaigns(document)
    if named is None:
        return REFERENCE_INCOHERENT  # structure indéterminable : l'analyse stricte la refuserait aussi
    if campaign_id not in named:
        return REFERENCE_ABSENT
    try:
        scopes = parse_retention_scopes(document)
    except _DOCUMENT_ERRORS:
        return REFERENCE_INCOHERENT  # document hors contrat qui désigne la campagne
    return REFERENCE_PRESENT if any(scope.campaign_id == campaign_id for scope in scopes) else REFERENCE_ABSENT
