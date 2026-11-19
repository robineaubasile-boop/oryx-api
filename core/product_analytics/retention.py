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
    """Dernière échéance autorisée par une campagne pour un événement."""
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
