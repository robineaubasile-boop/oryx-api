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

Ce fichier couvre aussi T0-G : Alembic (via le Pre-Deploy Railway) devient
l'unique propriétaire du schéma PostgreSQL. L'application ne doit plus
créer ni modifier ce schéma elle-même au démarrage — ni via
`Base.metadata.create_all()`, ni via l'ancien `ALTER TABLE` artisanal de
`_migrate_add_columns()`. Ces garde-fous vérifient que ce mécanisme
historique a bien été retiré de core/db.py et de api.py, sans se connecter
à une vraie base de données.
"""
import inspect

from sqlalchemy import create_engine

import core.db as db_module
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


def test_core_db_stays_importable_and_exposes_expected_api():
    """T0-G : core.db doit rester importable et continuer à fournir les
    éléments nécessaires au fonctionnement normal de l'application."""
    assert hasattr(db_module, "normalize_database_url")
    assert hasattr(db_module, "DATABASE_URL")
    assert hasattr(db_module, "engine")
    assert hasattr(db_module, "SessionLocal")
    assert hasattr(db_module, "Base")
    assert hasattr(db_module, "get_db")
    assert inspect.isgeneratorfunction(db_module.get_db)


def test_get_db_still_raises_without_database_url():
    """get_db() doit garder son comportement : sans DATABASE_URL configuré,
    SessionLocal est None et la génératrice lève RuntimeError plutôt que
    de planter au démarrage."""
    if db_module.SessionLocal is not None:
        return
    gen = db_module.get_db()
    try:
        next(gen)
        assert False, "get_db() aurait dû lever RuntimeError"
    except RuntimeError:
        pass


def test_init_db_and_legacy_schema_mutation_helpers_are_removed():
    """T0-G : l'ancien mécanisme de gestion du schéma (init_db(),
    _migrate_add_columns()) ne doit plus exister dans core.db — Alembic
    (Pre-Deploy Railway) est désormais l'unique propriétaire du schéma."""
    assert not hasattr(db_module, "init_db")
    assert not hasattr(db_module, "_migrate_add_columns")


def test_core_db_source_has_no_create_all_or_artisanal_alter_table():
    """Garde-fou statique complémentaire : aucun Base.metadata.create_all()
    ni ALTER TABLE artisanal ne doit subsister dans core/db.py."""
    with open("core/db.py", encoding="utf-8") as f:
        source = f.read()
    assert "create_all" not in source
    assert "ALTER TABLE" not in source
    assert "init_db" not in source


def test_api_no_longer_imports_or_calls_init_db():
    """T0-G : api.py ne doit plus importer init_db ni l'appeler dans un
    handler de startup FastAPI — mais doit toujours importer get_db."""
    with open("api.py", encoding="utf-8") as f:
        source = f.read()
    assert "init_db" not in source
    assert "get_db" in source


def test_api_startup_handler_calling_init_db_is_gone():
    """Garde-fou ciblé sur le handler historique
    `@app.on_event(\"startup\")` dont le seul rôle était d'appeler
    init_db(). D'autres mécanismes startup/lifespan éventuels, sans lien
    avec le schéma, ne sont pas concernés par ce test."""
    with open("api.py", encoding="utf-8") as f:
        source = f.read()
    assert "_startup_init_db" not in source
