"""Socle commun des services analytics produit (V0-T08 Lot 2A) — DORMANT.

Erreurs, vocabulaires FERMÉS, registre des politiques de campagne, horloge
serveur, validation structurelle et verrous consultatifs d'identité. Accès à
la base limités à require_existing_user et lock_identities.

Horloge (audit R1) : toute décision temporelle d'un service est prise avec
un utcnow() lu APRÈS l'acquisition de tous les verrous PostgreSQL dont elle
dépend (verrous de ligne et verrous consultatifs d'identité) ; jamais avec
une valeur lue avant une attente de verrou.

Messages d'erreur : uniquement des noms de champs, des identifiants
techniques (UUID, code de campagne) et des valeurs de vocabulaire fermé ;
jamais une valeur libre fournie par l'appelant.
"""
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.models import User


# --------------------------------------------------------------------------
# Erreurs
# --------------------------------------------------------------------------

class ProductAnalyticsError(Exception):
    """Racine des refus des services analytics produit."""


class InvalidProductAnalyticsInput(ProductAnalyticsError, ValueError):
    """Entrée structurellement invalide : refusée avant tout accès à la base."""


class ProductAnalyticsUserNotFound(ProductAnalyticsError):
    """L'utilisateur n'existe pas (aucune création implicite)."""


class CampaignNotFound(ProductAnalyticsError):
    pass


class CampaignCodeConflict(ProductAnalyticsError):
    """Code de campagne déjà utilisé (aucune reprise implicite)."""


class CampaignTransitionRefused(ProductAnalyticsError):
    """Transition d'état non autorisée (dont toute réouverture)."""


class CampaignPolicyLocked(ProductAnalyticsError):
    """Politique de mesure / conservation modifiée hors de l'état planned."""


class ParticipationNotFound(ProductAnalyticsError):
    pass


class ParticipationRefused(ProductAnalyticsError):
    """Inscription ou retrait non autorisé (campagne, fenêtre, autorisation)."""


class ParticipationConflict(ProductAnalyticsError):
    """Une participation différente existe déjà pour (campagne, utilisateur)."""


class EventNotAuthorized(ProductAnalyticsError):
    """Aucune participation active ne couvre l'événement : refus fermé."""


class EventIdempotencyCollision(ProductAnalyticsError):
    """Même (user_id, idempotency_key) avec des données incompatibles."""


class EventUniquenessConflict(ProductAnalyticsError):
    """Unicité métier déjà occupée par un autre événement (session_started
    de la visite, ou opération métier canonique du propriétaire)."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class UnsupportedProductEventSchema(ProductAnalyticsError):
    """Ligne existante d'une autre version de schéma : jamais lue de manière
    permissive."""


class InvalidRetentionScopes(ProductAnalyticsError):
    """Document retention-scopes-v1 ou échéances incohérents."""


class RetentionRevisionRefused(ProductAnalyticsError):
    """Révision de conservation interdite (prolongation, finalité ajoutée,
    finalité modifiée)."""


class EventRetentionExpired(ProductAnalyticsError):
    """Répétition d'un événement dont au moins une finalité de conservation
    est expirée (totalement ou partiellement) : refus sans aucune écriture ni
    suppression. La clé d'idempotence reste consommée ; la ligne attend la
    purge du lot 2C."""


# --------------------------------------------------------------------------
# Vocabulaires fermés (identiques aux CHECK de la migration 0022)
# --------------------------------------------------------------------------

PLANNED, ACTIVE, CLOSED, CANCELLED = "planned", "active", "closed", "cancelled"
CAMPAIGN_STATUSES = (PLANNED, ACTIVE, CLOSED, CANCELLED)
TERMINAL_CAMPAIGN_STATUSES = (CLOSED, CANCELLED)

ENROLLED, COMPLETED, WITHDRAWN = "enrolled", "completed", "withdrawn"
PARTICIPATION_STATUSES = (ENROLLED, COMPLETED, WITHDRAWN)

EVENT_TYPES = ("session_started", "education_used", "coach_used", "decrypter_used", "portfolio_used",
               "profile_viewed", "view_opened")
CLIENT_SIGNAL, SERVER_BUSINESS, SERVER_VISIT = "client_signal", "server_business", "server_visit"
SOURCES = (CLIENT_SIGNAL, SERVER_BUSINESS, SERVER_VISIT)
IN_PROGRESS = "in_progress"
OUTCOMES = (IN_PROGRESS, "succeeded", "failed", "indeterminate")
NOT_APPLICABLE, PENDING, CONFIRMED, UNCONFIRMED = "not_applicable", "pending", "confirmed", "unconfirmed"
DELIVERY_STATUSES = (NOT_APPLICABLE, PENDING, CONFIRMED, UNCONFIRMED)

# Version des lignes écrites par ce lot ; toute autre version est refusée.
PRODUCT_EVENT_SCHEMA_VERSION = "product-activity-event-v1"

# --------------------------------------------------------------------------
# Vocabulaires fermés propres au lot 2A (non imposés en SQL : VARCHAR libres
# en base). Fermés pour qu'aucun texte libre — question, ticker, URL,
# contenu pédagogique — ne puisse transiter par surface, executed_route ou
# source_operation_kind. Toute extension est une décision explicite.
# --------------------------------------------------------------------------

SURFACES = ("app", "education", "coach", "decrypter", "portfolio", "profile")
EXECUTED_ROUTES = ("legacy", "f2")
SOURCE_OPERATION_KINDS = ("assistant_delivery", "portfolio_write")


@dataclass(frozen=True)
class EventTypeRule:
    """Règles de cohérence d'un event_type.

    sources : sources admises ; surface : surface imposée (None = toute
    surface fermée) ; operation_kind : type d'opération canonique exigé
    quand la source est server_business (None = server_business interdit)."""
    sources: tuple
    surface: str | None
    operation_kind: str | None


EVENT_TYPE_RULES = {
    "session_started": EventTypeRule((SERVER_VISIT,), "app", None),
    "view_opened": EventTypeRule((CLIENT_SIGNAL, SERVER_VISIT), None, None),
    "profile_viewed": EventTypeRule((CLIENT_SIGNAL, SERVER_VISIT), "profile", None),
    "education_used": EventTypeRule((CLIENT_SIGNAL, SERVER_VISIT), "education", None),
    "coach_used": EventTypeRule((CLIENT_SIGNAL, SERVER_VISIT), "coach", None),
    "decrypter_used": EventTypeRule((CLIENT_SIGNAL, SERVER_VISIT, SERVER_BUSINESS), "decrypter",
                                    "assistant_delivery"),
    "portfolio_used": EventTypeRule((CLIENT_SIGNAL, SERVER_VISIT, SERVER_BUSINESS), "portfolio",
                                    "portfolio_write"),
}


# --------------------------------------------------------------------------
# Registre fermé des politiques de campagne
# --------------------------------------------------------------------------

# Politique de conservation : version -> durée en jours. La durée reste une
# donnée EXPLICITE de chaque campagne (retention_days, sans valeur par
# défaut SQL ni Python) ; le registre ne sert qu'à refuser une durée
# incohérente avec la version de politique déclarée.
RETENTION_POLICIES = {"v0-retention-v1": 90}
MEASUREMENT_POLICIES = ("v0-measurement-v1",)

# Fenêtre d'observation individuelle de chaque politique de mesure (décision
# produit Vague 0 : 12 jours complets à partir du J0 individuel, J0 = première
# connexion authentifiée et éligible). Portée par la politique de mesure, donc
# immuable dès l'activation de la campagne. Utilisée par
# participation.enroll_at_first_eligible_login : window_end_at = J0 + durée,
# exactement.
OBSERVATION_WINDOWS = {"v0-measurement-v1": timedelta(days=12)}


@dataclass(frozen=True)
class CampaignPolicy:
    retention_days: int
    retention_policy_version: str
    measurement_policy_version: str


# Politique Vague 0 : 90 jours de conservation depuis occurred_at (décision
# produit figée ; voir retention.authorized_expiry). Constante de référence que
# l'appelant transmet explicitement à create_campaign ; jamais appliquée
# implicitement.
WAVE_0_POLICY = CampaignPolicy(retention_days=90, retention_policy_version="v0-retention-v1",
                               measurement_policy_version="v0-measurement-v1")


def validate_policy(*, retention_days, retention_policy_version, measurement_policy_version) -> None:
    if type(retention_days) is not int:
        raise InvalidProductAnalyticsInput("retention_days : entier explicite requis")
    # Type vérifié AVANT toute recherche dans le registre : une valeur non
    # hachable (liste, dict, ensemble) lèverait sinon TypeError.
    if type(retention_policy_version) is not str or retention_policy_version not in RETENTION_POLICIES:
        raise InvalidProductAnalyticsInput("retention_policy_version : politique inconnue")
    if RETENTION_POLICIES[retention_policy_version] != retention_days:
        raise InvalidProductAnalyticsInput(
            f"retention_days : incohérent avec la politique {retention_policy_version}")
    if type(measurement_policy_version) is not str or measurement_policy_version not in MEASUREMENT_POLICIES:
        raise InvalidProductAnalyticsInput("measurement_policy_version : politique inconnue")


# --------------------------------------------------------------------------
# Horloge et validation structurelle
# --------------------------------------------------------------------------

def utcnow() -> datetime:
    """Horloge serveur, seule source de created_at / enrolled_at /
    recorded_at (remplaçable en test)."""
    return datetime.now(timezone.utc)


CODE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
NOTICE_VERSION_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
USER_ID_MAX_LENGTH = 255


def require_aware(value, field: str) -> datetime:
    """datetime avec fuseau, ramenée en UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidProductAnalyticsInput(f"{field} : datetime avec fuseau horaire requise")
    return value.astimezone(timezone.utc)


def require_optional_aware(value, field: str) -> datetime | None:
    return None if value is None else require_aware(value, field)


def require_uuid(value, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidProductAnalyticsInput(f"{field} : uuid.UUID requis")
    return value


def require_user_id(value) -> str:
    if (not isinstance(value, str) or not value or len(value) > USER_ID_MAX_LENGTH or value != value.strip()
            or any(ord(c) < 32 for c in value)):
        raise InvalidProductAnalyticsInput("user_id : identifiant non vide requis")
    return value


def require_choice(value, choices, field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise InvalidProductAnalyticsInput(f"{field} : valeur hors vocabulaire fermé")
    return value


def require_pattern(value, pattern, field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidProductAnalyticsInput(f"{field} : format invalide")
    return value


def require_text(value, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or value != value.strip():
        raise InvalidProductAnalyticsInput(f"{field} : texte non vide de {max_length} caractères au plus requis")
    return value


def require_existing_user(db, user_id: str) -> None:
    if db.execute(select(User.id).where(User.id == user_id)).scalar_one_or_none() is None:
        raise ProductAnalyticsUserNotFound("utilisateur inconnu")


# --------------------------------------------------------------------------
# Verrous consultatifs d'identité
# --------------------------------------------------------------------------

IDENTITY_LOCK_NAMESPACE = "oryx-v0t08-lot2a"


def identity_lock_key(kind: str, *parts) -> int:
    """Clé bigint déterministe entre processus (jamais hash() Python)."""
    encoded = json.dumps([IDENTITY_LOCK_NAMESPACE, kind, *(str(part) for part in parts)], separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)


def lock_identities(db, *keys: int) -> None:
    """pg_advisory_xact_lock de chaque identité métier, en ordre croissant
    (anti-interblocage), libérés à la fin de la transaction de l'appelant.

    Deux écritures concurrentes d'une même identité (code de campagne,
    participation, clé d'idempotence, session_started d'une visite,
    opération canonique) sont ainsi sérialisées AVANT toute décision : le
    perdant attend ici, puis relit l'état validé et décide avec une horloge
    fraîche, au lieu d'attendre sur l'index UNIQUE pendant l'INSERT avec une
    décision prise avant l'attente. Les contraintes UNIQUE restent la défense
    finale (SAVEPOINT)."""
    for key in sorted(set(keys)):
        db.execute(select(func.pg_advisory_xact_lock(key)))


def is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint
