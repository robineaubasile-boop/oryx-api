"""Tests du hotfix T0-F : dialecte PostgreSQL explicite (psycopg2).

Contexte : un build frais (SQLAlchemy 2.1.1 + psycopg2-binary 2.9.13, sans
`psycopg` v3) fait planter le démarrage d'Uvicorn au moment de
`create_engine(DATABASE_URL, ...)` dans core/db.py, avec
`ModuleNotFoundError: No module named 'psycopg'`, car une URL
`postgresql://` générique résout vers le driver `psycopg` par défaut sous
SQLAlchemy 2.x. `core.db.normalize_database_url` doit donc toujours
produire une URL de dialecte explicite `postgresql+psycopg2://` pour tout
schéma `postgres://`/`postgresql://`, sans jamais retoucher une URL qui
précise déjà son propre driver ou un schéma non-PostgreSQL (sqlite, etc.).

Aucun test ici ne se connecte à une vraie base de données.
"""
from sqlalchemy import create_engine

from core.db import normalize_database_url


def test_normalize_postgres_scheme_becomes_explicit_psycopg2():
    assert (
        normalize_database_url("postgres://u:p@host/db")
        == "postgresql+psycopg2://u:p@host/db"
    )


def test_normalize_postgresql_scheme_becomes_explicit_psycopg2():
    assert (
        normalize_database_url("postgresql://u:p@host/db")
        == "postgresql+psycopg2://u:p@host/db"
    )


def test_normalize_leaves_already_explicit_psycopg2_url_untouched():
    url = "postgresql+psycopg2://u:p@host/db"
    assert normalize_database_url(url) == url


def test_normalize_leaves_other_explicit_driver_untouched():
    url = "postgresql+asyncpg://u:p@host/db"
    assert normalize_database_url(url) == url


def test_normalize_leaves_non_postgres_scheme_untouched():
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_normalize_leaves_empty_url_untouched():
    assert normalize_database_url("") == ""


def test_engine_creation_resolves_psycopg2_driver_without_importerror():
    """Reproduit exactement le crash observé : create_engine() doit pouvoir
    résoudre et importer le driver déclaré dans requirements.txt
    (psycopg2-binary), sans jamais tenter d'importer `psycopg` (v3)."""
    url = normalize_database_url("postgres://u:p@host/db")
    engine = create_engine(url)
    assert engine.url.drivername == "postgresql+psycopg2"
    assert engine.dialect.driver == "psycopg2"


def test_alembic_env_reuses_core_db_database_url():
    """Garde-fou de cohérence T0-F : alembic/env.py doit continuer à
    réutiliser core.db.DATABASE_URL (déjà normalisé), pour qu'Alembic et
    l'application utilisent toujours la même URL/le même driver. On lit le
    fichier source plutôt que de l'importer, car son import déclenche
    l'exécution des migrations."""
    with open("alembic/env.py", encoding="utf-8") as f:
        source = f.read()
    assert "from core.db import DATABASE_URL, Base" in source
