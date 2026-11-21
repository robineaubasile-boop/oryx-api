"""E0 — gardes d'isolement : environnement, cibles, production, CI, écriture.

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 2.2,
§ 3.1 à § 3.3) : bibliothèque standard seulement, aucun import d'un module
Oryx. Réimplémentation conceptuelle (sans import ni lecture) de la garde du
banc précédent, renforcée par une LISTE BLANCHE exacte pour l'origine.

Règles :
- seuls les NOMS de variables sont rapportés, jamais leurs valeurs ;
- toute cible désignant la production Oryx est refusée en premier ;
- les outils à effet distant refusent de s'exécuter en CI ;
- les outils refusent d'écrire dans un arbre Git (preuves hors dépôt).

La liste noire de production est le miroir EXACT de
config/production_denylist.v1.json (égalité vérifiée par les tests) : le
conteneur de l'origine ne contient que le répertoire py/.
"""
import os
import re

EXIT_USAGE = 2
EXIT_ENVIRONMENT_NOT_ISOLATED = 3
EXIT_PRODUCTION_TARGET = 4

# Miroir de config/production_denylist.v1.json (host_markers + founder_additions).
PRODUCTION_HOST_MARKERS = ("oryx-api-production",)

# Liste noire générique (tous les outils).
FORBIDDEN_PREFIXES = ("ORYX_", "DATABASE_", "PG", "ANTHROPIC", "CLAUDE", "EODHD", "FMP", "SUPABASE", "RESEND",
                      "POSTGRES", "OPENAI")
FORBIDDEN_EXACT = frozenset({"DATABASE_URL", "REDIS_URL", "MONGO_URL"})
FORBIDDEN_FRAGMENTS = ("SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "TOKEN", "PRIVATE_KEY", "CREDENTIAL")

# Liste blanche EXACTE de l'origine (§ 2.2) : environnement de l'image officielle,
# variables Railway et seuls E0_* de l'origine.
ORIGIN_BASE_NAMES = frozenset({"PATH", "HOME", "HOSTNAME", "LANG", "PORT", "GPG_KEY"})
ORIGIN_BASE_PREFIXES = ("PYTHON_", "RAILWAY_")
ORIGIN_E0_NAMES = frozenset({
    "E0_ROLE", "E0_PHASE", "E0_VERIFY_KEYS", "E0_AUDIT_PUBKEYS", "E0_RETIRED_KIDS", "E0_PUBLIC_HOST",
    "E0_CMP_KEY_POST", "E0_CMP_KEY_POST_ID", "E0_TTL_SECONDS", "E0_MAX_REQUESTS", "E0_W_MS", "E0_S_MS",
    "E0_SLOW_MAX_S", "E0_SLOW_CONCURRENCY", "E0_BODY_MAX", "E0_TLS_CERT_PEM_B64", "E0_TLS_KEY_PEM_B64",
})
# Exception exacte et nominative : le jeton de l'inventaire (outil e0_inventory seulement).
INVENTORY_TOKEN_NAME = "E0_CF_INVENTORY_TOKEN"

CI_MARKERS = ("CI", "GITHUB_ACTIONS")

RAILWAY_SERVICE_PREFIX = "e0-origin"
RAILWAY_PROJECT_PREFIX = "oryx-e0"
ORIGIN_HOST_PATTERN = re.compile(r"e0-origin[a-z0-9-]*\.up\.railway\.app")
_LDH_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class GuardError(Exception):
    """Refus d'une garde ; `exit_code` est le code de sortie à rendre."""

    def __init__(self, message: str, exit_code: int = EXIT_ENVIRONMENT_NOT_ISOLATED):
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# Variables d'environnement
# --------------------------------------------------------------------------

def forbidden_variables(environ=None, allow=()) -> list:
    """Noms (triés) des variables interdites par la liste noire générique.
    `allow` : exceptions EXACTES et nominatives (par exemple le jeton de
    l'inventaire) ; aucune exception par motif."""
    environ = os.environ if environ is None else environ
    allowed = frozenset(allow)
    found = []
    for name in environ:
        if name in allowed:
            continue
        upper = name.upper()
        if (upper in FORBIDDEN_EXACT or upper.startswith(FORBIDDEN_PREFIXES)
                or any(fragment in upper for fragment in FORBIDDEN_FRAGMENTS)):
            found.append(name)
    return sorted(found)


def on_railway(environ) -> bool:
    return any(name.startswith("RAILWAY_") for name in environ)


def origin_environment_violations(environ) -> list:
    """Violations de la garde de démarrage de l'origine (noms seulement) :
    tout nom hors liste blanche exacte, toute variable de la liste noire, et,
    sur Railway, des noms de projet, de service ou de domaine non E0."""
    problems = []
    for name in sorted(environ):
        if name in ORIGIN_BASE_NAMES or name in ORIGIN_E0_NAMES or name.startswith(ORIGIN_BASE_PREFIXES):
            continue
        problems.append(f"variable hors liste blanche : {name}")
    for name in forbidden_variables(environ):
        problems.append(f"variable interdite : {name}")
    if on_railway(environ):
        service = environ.get("RAILWAY_SERVICE_NAME", "")
        project = environ.get("RAILWAY_PROJECT_NAME", "")
        domain = environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if not service.startswith(RAILWAY_SERVICE_PREFIX):
            problems.append("RAILWAY_SERVICE_NAME non préfixé e0-origin")
        if not project.startswith(RAILWAY_PROJECT_PREFIX):
            problems.append("RAILWAY_PROJECT_NAME non préfixé oryx-e0")
        if not ORIGIN_HOST_PATTERN.fullmatch(domain or ""):
            problems.append("RAILWAY_PUBLIC_DOMAIN hors motif e0-origin….up.railway.app")
        for value in (service, project, domain):
            if is_production_host(value):
                problems.append("identifiant Railway de production")
    return problems


def check_origin_environment(environ) -> None:
    problems = origin_environment_violations(environ)
    if problems:
        raise GuardError("environnement de l'origine NON isolé : " + " ; ".join(problems))


def tool_environment_violations(environ, allow=()) -> list:
    """Outils à effet distant (sonde, inventaire, déploiement, keygen, témoin
    IP) : liste noire générique et refus en CI."""
    problems = [f"variable interdite : {name}" for name in forbidden_variables(environ, allow=allow)]
    problems += [f"exécution en CI refusée : {name}" for name in CI_MARKERS if name in environ]
    return problems


def refuse_in_ci(environ) -> None:
    present = [name for name in CI_MARKERS if name in environ]
    if present:
        raise GuardError("outil à effet distant refusé en CI : " + ", ".join(present))


# --------------------------------------------------------------------------
# Hôtes et cibles
# --------------------------------------------------------------------------

def _decode_labels(lowered: str) -> str:
    labels = []
    for label in lowered.split("."):
        if label.startswith("xn--"):
            try:
                labels.append(label.encode("ascii").decode("idna"))
            except (UnicodeError, ValueError):
                labels.append(label)
        else:
            labels.append(label)
    return ".".join(labels)


def normalize_host(host) -> str:
    if not isinstance(host, str):
        raise GuardError("hôte illisible", EXIT_USAGE)
    return host.strip().lower().rstrip(".")


def is_production_host(host) -> bool:
    """Vrai si l'hôte (ou un identifiant) contient un marqueur de production,
    sous sa forme brute ou après décodage punycode des étiquettes."""
    if not isinstance(host, str):
        return False
    lowered = host.strip().lower().rstrip(".")
    candidates = {lowered, _decode_labels(lowered)}
    return any(marker in candidate for marker in PRODUCTION_HOST_MARKERS for candidate in candidates)


def is_ldh_host(host: str) -> bool:
    if not isinstance(host, str) or not 1 <= len(host) <= 253 or host != host.lower():
        return False
    labels = host.split(".")
    # Dernière étiquette non numérique : une IP littérale n'est jamais un nom d'hôte.
    return len(labels) >= 2 and all(_LDH_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit()


def check_target_host(host, allowed) -> str:
    """Cible acceptée : refus de production EN PREMIER (code 4), puis nom LDH
    strict, puis appartenance exacte à la liste blanche du manifeste."""
    if is_production_host(host if isinstance(host, str) else ""):
        raise GuardError("cible de production Oryx refusée", EXIT_PRODUCTION_TARGET)
    lowered = normalize_host(host)
    if not is_ldh_host(lowered):
        raise GuardError("hôte de cible non conforme (LDH minuscule)", EXIT_USAGE)
    if lowered not in frozenset(allowed):
        raise GuardError("hôte hors de la liste blanche du manifeste", EXIT_USAGE)
    return lowered


def check_origin_host(host) -> str:
    """Hôte d'origine E0 : motif exact ancré (un suffixe usurpé est refusé)."""
    if is_production_host(host if isinstance(host, str) else ""):
        raise GuardError("hôte d'origine de production refusé", EXIT_PRODUCTION_TARGET)
    lowered = normalize_host(host)
    if not ORIGIN_HOST_PATTERN.fullmatch(lowered) or not is_ldh_host(lowered):
        raise GuardError("hôte d'origine hors motif e0-origin….up.railway.app", EXIT_USAGE)
    return lowered


def check_public_host(host) -> str:
    """Hôte public d'entrée (PA14) : nom LDH minuscule exact, jamais de
    production, jamais un hôte d'origine Railway."""
    if is_production_host(host if isinstance(host, str) else ""):
        raise GuardError("hôte public de production refusé", EXIT_PRODUCTION_TARGET)
    if not isinstance(host, str) or not is_ldh_host(host):
        raise GuardError("hôte public non conforme (LDH minuscule exact)", EXIT_USAGE)
    if host.endswith(".up.railway.app"):
        raise GuardError("hôte public égal à un domaine Railway", EXIT_USAGE)
    return host


# --------------------------------------------------------------------------
# Écriture hors dépôt
# --------------------------------------------------------------------------

def inside_git_tree(path) -> bool:
    current = os.path.realpath(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def refuse_write_in_repo(path) -> str:
    """Chemin d'écriture accepté seulement hors de tout arbre Git."""
    if inside_git_tree(path):
        raise GuardError("écriture refusée dans un arbre Git (preuves hors dépôt)", EXIT_USAGE)
    return os.path.realpath(os.path.abspath(path))
