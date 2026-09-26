import os
from sqlalchemy import create_engine, text
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


def init_db():
    """Crée les tables si elles n'existent pas. Ne fait rien si
    DATABASE_URL n'est pas configuré, pour ne jamais bloquer le
    démarrage de l'app pendant la transition."""
    if not engine:
        print("[DB] DATABASE_URL non configuré — base de données désactivée.")
        return
    from core import models  # noqa: enregistre les modèles avant create_all
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()
    print("[DB] Tables créées/vérifiées avec succès.")


def _migrate_add_columns():
    """Migrations légères pour les colonnes ajoutées après la création
    initiale d'une table (create_all ne touche jamais une table déjà
    existante). Volontairement sans DEFAULT SQL : les lignes existantes
    doivent rester NULL, pas être backfillées à la date du déploiement
    (voir CompanyAnalysis.created_at). Idempotent, ne doit jamais
    bloquer le démarrage."""
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE company_analyses ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"
            ))
            conn.commit()
    except Exception as e:
        print(f"[DB-MIGRATION ERROR] {type(e).__name__}: {e}")


def get_db():
    if not SessionLocal:
        raise RuntimeError("Base de données non configurée (DATABASE_URL manquant).")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
