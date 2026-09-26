import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


def normalize_database_url(raw_url: str) -> str:
    """Normalise l'URL fournie par Railway (postgres://) vers le schéma
    SQLAlchemy (postgresql://), puis rend le driver explicite
    (postgresql+psycopg2://). Sans ce dernier point, SQLAlchemy 2.x résout
    un `postgresql://` générique vers le driver `psycopg` (v3), absent de
    requirements.txt qui ne fournit que `psycopg2-binary` — d'où le crash
    `ModuleNotFoundError: No module named 'psycopg'` au démarrage.
    Une URL qui précise déjà un driver (ex: postgresql+psycopg2://,
    postgresql+asyncpg://) n'est jamais modifiée, tout comme un autre
    schéma (sqlite://, etc.)."""
    if raw_url.startswith("postgres://"):
        raw_url = raw_url.replace("postgres://", "postgresql://", 1)
    if raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return raw_url


DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL", ""))

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None
Base = declarative_base()


def get_db():
    if not SessionLocal:
        raise RuntimeError("Base de données non configurée (DATABASE_URL manquant).")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
