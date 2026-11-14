"""P1-Q — garde d'isolement de l'environnement du banc (bibliothèque standard).

Le banc ne doit JAMAIS tourner avec des variables d'un environnement Oryx
(protocole P1-Q § 3.2) : aucune base, aucun secret fournisseur, aucune
variable ORYX_*. Au démarrage, le récepteur et la sonde refusent de s'exécuter
si une variable interdite est présente ; seuls les NOMS sont affichés, jamais
les valeurs.

Le banc refuse aussi toute cible qui désigne le service de production Oryx.
"""
import os
import re

FORBIDDEN_PREFIXES = ("ORYX_", "DATABASE_", "PG", "ANTHROPIC", "CLAUDE", "EODHD", "FMP", "SUPABASE",
                      "RESEND", "POSTGRES", "OPENAI")
FORBIDDEN_EXACT = frozenset({"DATABASE_URL", "REDIS_URL", "MONGO_URL"})
FORBIDDEN_FRAGMENTS = ("SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "TOKEN", "PRIVATE_KEY", "CREDENTIAL")

# Hôtes de production Oryx connus du dépôt (docs/act_l1_f04a_architecture_v03.md § 6.1).
PRODUCTION_HOST_MARKERS = ("oryx-api-production",)

EXIT_ENVIRONMENT_NOT_ISOLATED = 3
EXIT_PRODUCTION_TARGET = 4

_HOST_SAFE = re.compile(r"[a-z0-9.:\[\]-]{1,253}")


def forbidden_variables(environ=None) -> list:
    environ = os.environ if environ is None else environ
    found = []
    for name in environ:
        upper = name.upper()
        if (upper in FORBIDDEN_EXACT or upper.startswith(FORBIDDEN_PREFIXES)
                or any(fragment in upper for fragment in FORBIDDEN_FRAGMENTS)):
            found.append(name)
    return sorted(found)


def is_production_host(host: str) -> bool:
    lowered = (host or "").strip().lower().rstrip(".")
    return any(marker in lowered for marker in PRODUCTION_HOST_MARKERS)


def check_target_host(host: str) -> str:
    """Hôte de cible accepté (minuscules), ou ValueError."""
    lowered = (host or "").strip().lower().rstrip(".")
    if not _HOST_SAFE.fullmatch(lowered):
        raise ValueError("hôte de cible illisible")
    if is_production_host(lowered):
        raise ValueError("cible de production Oryx refusée")
    return lowered
