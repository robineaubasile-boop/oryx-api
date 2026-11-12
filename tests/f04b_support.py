"""Outils des tests F04-B : configuration SYNTHÉTIQUE complète.

Aucune valeur réelle : clés de 32 octets dérivées d'un compteur, clés
fournisseurs fictives, URL PostgreSQL non joignable par défaut (port 1 de la
boucle locale), origine et hôte Supabase fictifs. Aucun réseau.
"""
import base64
import json

from core.auth_f2.keys import PURPOSES

FAKE_DATABASE_URL = "postgresql://oryx_user:synthetic-password@127.0.0.1:1/oryx_f04b_never_test"
ORIGIN = "https://oryx-f04b.example"
SUPABASE_URL = "https://oryx-f04b-fake.supabase.co"
MODEL = "claude-f04b-synthetic"


def raw_key(n: int) -> bytes:
    """Clé synthétique de 32 octets, distincte pour chaque n."""
    return n.to_bytes(4, "big") * 8


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def key(n: int) -> str:
    return b64(raw_key(n))


def auth_document(**overrides) -> dict:
    """Format EXACT du freeze F04-B V0.3 § 2.2 : {schema, keys, current}."""
    document = {"schema": "oryx-f2-auth-keys-v1",
                "keys": {p: {"v1": key(100 + i)} for i, p in enumerate(PURPOSES)},
                "current": {p: "v1" for p in PURPOSES}}
    document.update(overrides)
    return document


def set_purpose(document: dict, purpose: str, versions: dict, current: str) -> dict:
    """Remplace les versions et la version courante d'un usage Auth F2."""
    document["keys"][purpose] = versions
    document["current"][purpose] = current
    return document


def single_document(family: str, base: int, **overrides) -> dict:
    document = {"schema": f"oryx-f2-{family}-keys-v1", "current": f"{family[0]}1",
                "keys": {f"{family[0]}1": key(base)}}
    document.update(overrides)
    return document


def environ(**overrides) -> dict:
    """Environnement F2 complet et valide, plus des variables étrangères
    (Railway, legacy) qui doivent être ignorées."""
    env = {
        "DATABASE_URL": FAKE_DATABASE_URL,
        "ORYX_F2_ALLOWED_ORIGIN": ORIGIN,
        "ORYX_F2_AUTH_KEYS_JSON": json.dumps(auth_document()),
        "ORYX_F2_CSRF_KEYS_JSON": json.dumps(single_document("csrf", 200)),
        "ORYX_F2_PORTFOLIO_KEYS_JSON": json.dumps(single_document("portfolio", 300)),
        "ORYX_F2_DECRYPTAGE_KEYS_JSON": json.dumps(single_document("decryptage", 400)),
        "ORYX_F2_SUPABASE_URL": SUPABASE_URL,
        "ORYX_F2_SUPABASE_SERVICE_KEY": "synthetic-supabase-service-key",
        "ORYX_F2_SUPABASE_VERIFY_KEY": "synthetic-supabase-verify-key",
        "ORYX_F2_RESEND_API_KEY": "re_synthetic_resend_key",
        "ORYX_F2_RESEND_SENDER": "Oryx <no-reply@oryx-f04b.example>",
        "ORYX_F2_EODHD_API_KEY": "synthetic-eodhd-key",
        "ORYX_F2_FMP_API_KEY": "synthetic-fmp-key",
        "ORYX_F2_ANTHROPIC_API_KEY": "sk-ant-synthetic-key",
        "ORYX_F2_DECRYPTAGE_MODEL": MODEL,
        # Variables étrangères à F2 : jamais lues, jamais refusées.
        "RAILWAY_ENVIRONMENT": "synthetic",
        "ANTHROPIC_API_KEY": "legacy-synthetic",
        "ORYX_LEGACY_FLAG": "1",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


SECRET_VALUES = (
    "synthetic-password", "synthetic-supabase-service-key", "synthetic-supabase-verify-key",
    "re_synthetic_resend_key", "synthetic-eodhd-key", "synthetic-fmp-key", "sk-ant-synthetic-key",
)


def all_key_strings() -> list:
    """Toutes les clés encodées de l'environnement synthétique."""
    out = [key(100 + i) for i in range(len(PURPOSES))]
    return out + [key(200), key(300), key(400)]
