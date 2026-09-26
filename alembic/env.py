"""Environnement Alembic d'Oryx.

L'URL de la base est lue depuis DATABASE_URL, exactement comme l'application :
on réutilise core.db.DATABASE_URL, qui applique déjà la normalisation
postgres:// → postgresql:// (format fourni par Railway) puis rend le
driver psycopg2 explicite (postgresql+psycopg2://). Aucune URL n'est
stockée dans alembic.ini.

L'URL est passée directement à SQLAlchemy plutôt que via
config.set_main_option(), pour éviter l'interpolation configparser des '%'
présents dans les mots de passe encodés.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from core.db import DATABASE_URL, Base
from core import models  # noqa: F401 — enregistre les modèles dans Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL n'est pas défini : Alembic a besoin de la même "
            "variable d'environnement que l'application."
        )
    return DATABASE_URL


def run_migrations_offline() -> None:
    """Génère le SQL sans se connecter à la base (alembic ... --sql)."""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Exécute les migrations sur une connexion à la base."""
    connectable = create_engine(_get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
