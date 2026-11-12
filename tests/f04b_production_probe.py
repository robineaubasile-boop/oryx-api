"""Sonde exécutée DANS un environnement installé depuis requirements.txt seul
(ACT-L1 / F04-B, réserve d'audit 2) — jamais importée par pytest.

Lancée par tests/test_f04b_production_install.py avec l'interpréteur de
l'environnement de production, en mode isolé (python -I : ni PYTHONPATH, ni
site utilisateur, ni répertoire courant). Elle :

- vérifie qu'aucun outil de développement (pytest, pyyaml) n'est importable ;
- importe les modules de production pertinents (legacy, worker, Auth F2,
  Décrypter, F04-B, porte F-02A) ;
- construit, avec une configuration SYNTHÉTIQUE et sans aucune connexion
  (sockets interdits, psycopg2.connect interdit), les composants dormants :
  configuration, ressources (vrais clients httpx, SDK Anthropic, sessions
  isolées), les deux compositions isolées, la porte F-02A ; puis ferme les
  ressources ;
- écrit un rapport JSON sur la sortie standard (dernière ligne).
"""
import importlib
import importlib.util
import json
import platform
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

attempts = []


def _forbidden(*args, **kwargs):
    attempts.append("network")
    raise OSError("réseau interdit pendant la sonde de production")


socket.socket.connect = _forbidden
socket.create_connection = _forbidden

PRODUCTION_MODULES = (
    "fastapi", "starlette", "uvicorn", "pydantic", "requests", "dotenv", "yfinance", "anthropic", "httpx", "httpx2",
    "sqlalchemy", "psycopg2", "alembic",
    "api", "core.evaluation_worker", "core.db",
    "core.auth_f2.http_providers", "core.auth_f2.abuse", "core.auth_f2.service", "core.auth_f2_http.isolated",
    "core.decryptage_providers", "core.authz_f2.runner", "core.authz_f2_http.isolated",
    "core.authz_f2_http.decryptage", "core.route_policy.gate",
    "core.f2_runtime.config", "core.f2_runtime.database", "core.f2_runtime.resources", "core.f2_runtime.preflight",
)


def main() -> dict:
    report = {"python": platform.python_version(), "prefix": sys.prefix, "base_prefix": sys.base_prefix,
              "isolated_flag": sys.flags.isolated}
    report["dev_tools_importable"] = sorted(m for m in ("pytest", "yaml", "_pytest", "iniconfig", "pluggy")
                                            if importlib.util.find_spec(m) is not None)
    imported = []
    for name in PRODUCTION_MODULES:
        importlib.import_module(name)
        imported.append(name)
    report["imported"] = imported

    import psycopg2

    psycopg2.connect = lambda *a, **k: _forbidden()

    import httpx
    import requests

    from core.auth_f2_http.client_ip import PeerAddressResolver
    from core.auth_f2_http.isolated import build_isolated_f2_app
    from core.authz_f2_http.decryptage import DecryptageHttpConfig
    from core.authz_f2_http.isolated import build_isolated_personal_app
    from core.decryptage_providers import isolated_session
    from core.f2_runtime.config import load_f2_config
    from core.f2_runtime.preflight import expected_alembic_head
    from core.f2_runtime.resources import build_f2_resources
    from core.route_policy.gate import RoutePolicyGate
    from tests import f04b_support

    config = load_f2_config(f04b_support.environ())
    resources = build_f2_resources(config)
    auth_app = build_isolated_f2_app(engine=resources.engine, keyring=config.keyring, supabase=resources.supabase,
                                     mailer=resources.mailer, csrf_keyring=config.csrf_keyring,
                                     ip_resolver=PeerAddressResolver(), allowed_origins=config.allowed_origins)
    personal_app = build_isolated_personal_app(
        engine=resources.engine, keyring=config.keyring, allowed_origins=config.allowed_origins,
        csrf_keyring=config.csrf_keyring, portfolio_keyring=config.portfolio_keyring,
        decryptage=DecryptageHttpConfig(decryptage_keyring=config.decryptage_keyring, eodhd=resources.eodhd,
                                        fmp=resources.fmp, claude=resources.claude, model=config.decryptage_model))
    gate = RoutePolicyGate(f2_auth_app=auth_app, f2_personal_app=personal_app)

    def pairs(app):
        return len({(m, r.path) for r in app.routes for m in getattr(r, "methods", ())})

    session = isolated_session()
    report["components"] = {
        "engine_driver": resources.engine.dialect.driver,
        "engine_checked_out": resources.engine.pool.checkedout(),
        "supabase_client": type(resources.supabase._client).__module__ + "." + type(resources.supabase._client).__name__,
        "claude_max_retries": resources.claude.max_retries,
        "isolated_session_trust_env": session.trust_env,
        "isolated_session_type": type(session) is requests.Session,
        "auth_pairs": pairs(auth_app),
        "personal_pairs": pairs(personal_app),
        "gate": type(gate).__name__,
        "alembic_head": expected_alembic_head(),
        "httpx_version": httpx.__version__,
    }
    session.close()
    report["close"] = {"closed": list(resources.close().closed)}
    report["network_attempts"] = list(attempts)
    return report


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True))
