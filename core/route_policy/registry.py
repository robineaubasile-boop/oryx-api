"""Registre de routage FERMÉ F-02A V0.2 (données pures, figées).

Chaque entrée est un couple (méthode HTTP, gabarit de chemin) et une
catégorie. Le registre couvre exactement :

- l'inventaire RÉEL de api:app au freeze (d30cbc1) : 23 couples déclarés par
  api.py et 8 couples ajoutés automatiquement par FastAPI (documentation :
  GET et HEAD sur /openapi.json, /docs, /docs/oauth2-redirect, /redoc) ;
- les 27 routes F2 existantes des compositions isolées (14 routes
  d'authentification, 13 routes personnelles).

Catégories (fermées) et décision de la porte (gate.py) :

  LEGACY_RETIRED      A  15  410 {"error":"legacy_personal_api_retired"},
                             quelle que soit la méthode
  DISABLED            B   4  404, quelle que soit la méthode
  PUBLIC_MAINTAINED   C   1  GET /health servi par la porte elle-même
  PUBLIC_PENDING_F05  C   1  GET /web-v2 : 404 tant que F-05 n'a pas validé
                             le nouveau frontend
  PUBLIC_PENDING_AUDIT C  2  GET /web-v2/search, GET /web-v2/fx-rate : 404
                             tant que l'audit (entrées, effets, quotas,
                             délais, coûts fournisseurs) n'est pas conclu
  F2_AUTH             D  14  transmis à l'application F2 d'authentification
  F2_PERSONAL         D  13  transmis à l'application personnelle F2
  FRAMEWORK_DOCS      —   8  404 (documentation jamais exposée)

Toute modification de ce registre est une révision de la politique : elle
exige un nouveau freeze et casse volontairement les tests de
correspondance (tests/test_route_policy.py).
"""
from dataclasses import dataclass

POLICY_VERSION = "F-02A-V0.2"

# Catégories fermées.
LEGACY_RETIRED = "legacy_personal_retired"
DISABLED = "disabled"
PUBLIC_MAINTAINED = "public_maintained"
PUBLIC_PENDING_F05 = "public_pending_f05"
PUBLIC_PENDING_AUDIT = "public_pending_audit"
F2_AUTH = "f2_auth"
F2_PERSONAL = "f2_personal"
FRAMEWORK_DOCS = "framework_documentation"

CATEGORIES = (LEGACY_RETIRED, DISABLED, PUBLIC_MAINTAINED, PUBLIC_PENDING_F05, PUBLIC_PENDING_AUDIT, F2_AUTH,
              F2_PERSONAL, FRAMEWORK_DOCS)

# Origine de chaque entrée (inventaire).
SOURCE_API = "legacy-entrypoint"         # déclarée par api.py
SOURCE_FRAMEWORK = "legacy-entrypoint:auto"  # ajoutée automatiquement par FastAPI
SOURCE_F2_AUTH = "f2:auth"          # composition isolée d'authentification
SOURCE_F2_PERSONAL = "f2:personal"  # composition isolée personnelle

AUTH_NAMESPACE = "/api/auth/f2"
PERSONAL_NAMESPACE = "/api/v2"


@dataclass(frozen=True)
class RouteRule:
    method: str
    template: str
    category: str
    source: str


def _rules(category, source, pairs):
    return tuple(RouteRule(method, template, category, source) for method, template in pairs)


# A — 15 routes legacy personnelles retirées.
_LEGACY_RETIRED = _rules(LEGACY_RETIRED, SOURCE_API, (
    ("POST", "/decryptage"),
    ("POST", "/api/runtime/assistant-deliveries/{assistant_turn_id}/ack"),
    ("POST", "/education"),
    ("POST", "/coach"),
    ("POST", "/checklist"),
    ("POST", "/web-chat"),
    ("GET", "/api/user/{user_id}"),
    ("PUT", "/api/user/{user_id}/level"),
    ("GET", "/api/user/{user_id}/portfolio"),
    ("POST", "/api/user/{user_id}/portfolio"),
    ("DELETE", "/api/user/{user_id}/portfolio/{position_id}"),
    ("PUT", "/api/user/{user_id}/portfolio/{position_id}"),
    ("GET", "/api/user/{user_id}/theses"),
    ("DELETE", "/api/user/{user_id}/theses/{ticker}"),
    ("POST", "/web-v2/portfolio-analyze"),
))

# B — 4 routes désactivées.
_DISABLED = _rules(DISABLED, SOURCE_API, (
    ("POST", "/analyze"),
    ("POST", "/analyze-etf"),
    ("POST", "/pedagogie/lookup"),
    ("GET", "/web"),
))

# C — 4 routes publiques candidates.
_PUBLIC = (
    _rules(PUBLIC_MAINTAINED, SOURCE_API, (("GET", "/health"),))
    + _rules(PUBLIC_PENDING_F05, SOURCE_API, (("GET", "/web-v2"),))
    + _rules(PUBLIC_PENDING_AUDIT, SOURCE_API, (("GET", "/web-v2/search"), ("GET", "/web-v2/fx-rate")))
)

# Documentation ajoutée automatiquement par FastAPI sur api:app.
_FRAMEWORK_DOCS = _rules(FRAMEWORK_DOCS, SOURCE_FRAMEWORK, tuple(
    (method, path) for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
    for method in ("GET", "HEAD")))

# D — 14 routes d'authentification F2 (chemins, méthodes inchangés).
_F2_AUTH = _rules(F2_AUTH, SOURCE_F2_AUTH, tuple((method, AUTH_NAMESPACE + suffix) for method, suffix in (
    ("GET", "/csrf/bootstrap"),
    ("POST", "/sign-in/start"),
    ("POST", "/sign-in/resend"),
    ("POST", "/sign-in/verify"),
    ("POST", "/sign-in/resume"),
    ("POST", "/session/resolve"),
    ("POST", "/logout"),
    ("POST", "/invitation/begin"),
    ("POST", "/invitation/complete"),
    ("POST", "/invitation/resume"),
    ("POST", "/email-link/start"),
    ("POST", "/email-link/resend"),
    ("POST", "/email-link/verify"),
    ("POST", "/email-link/result"),
)))

# D — 13 routes personnelles F2 (profil, portefeuille, consultations, C3).
_F2_PERSONAL = _rules(F2_PERSONAL, SOURCE_F2_PERSONAL, tuple(
    (method, PERSONAL_NAMESPACE + suffix) for method, suffix in (
        ("GET", "/me"),
        ("PUT", "/me/level"),
        ("GET", "/me/portfolio"),
        ("GET", "/me/portfolio/{position_id}"),
        ("POST", "/me/portfolio"),
        ("PUT", "/me/portfolio/{position_id}"),
        ("DELETE", "/me/portfolio/{position_id}"),
        ("GET", "/me/analysis-overview"),
        ("GET", "/me/theses"),
        ("GET", "/me/theses/{thesis_id}"),
        ("POST", "/me/decryptage/turns"),
        ("POST", "/me/decryptage/replay"),
        ("POST", "/me/decryptage/ack"),
    )))

RULES = _LEGACY_RETIRED + _DISABLED + _PUBLIC + _FRAMEWORK_DOCS + _F2_AUTH + _F2_PERSONAL

EXPECTED_COUNTS = {LEGACY_RETIRED: 15, DISABLED: 4, PUBLIC_MAINTAINED: 1, PUBLIC_PENDING_F05: 1,
                   PUBLIC_PENDING_AUDIT: 2, F2_AUTH: 14, F2_PERSONAL: 13, FRAMEWORK_DOCS: 8}


def rules_of(*categories) -> tuple:
    return tuple(rule for rule in RULES if rule.category in categories)


def pairs_of(*categories) -> frozenset:
    """{(méthode, gabarit)} des catégories demandées."""
    return frozenset((rule.method, rule.template) for rule in rules_of(*categories))


def legacy_inventory() -> frozenset:
    """Couples attendus sur le véritable api:app (déclarés + automatiques)."""
    return frozenset((rule.method, rule.template) for rule in RULES
                     if rule.source in (SOURCE_API, SOURCE_FRAMEWORK))


def _check_closed():
    counts = {category: 0 for category in CATEGORIES}
    seen = set()
    templates = {}
    for rule in RULES:
        if rule.category not in counts:
            raise AssertionError(f"catégorie inconnue : {rule.category}")
        counts[rule.category] += 1
        key = (rule.method, rule.template)
        if key in seen:
            raise AssertionError(f"entrée dupliquée : {key}")
        seen.add(key)
        # Un gabarit appartient à UNE seule catégorie (refus indépendant de la méthode).
        if templates.setdefault(rule.template, rule.category) != rule.category:
            raise AssertionError(f"gabarit dans deux catégories : {rule.template}")
    if counts != EXPECTED_COUNTS:
        raise AssertionError(f"registre non conforme au freeze : {counts}")


_check_closed()
