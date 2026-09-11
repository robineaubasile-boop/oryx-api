import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

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
    print("[DB] Tables créées/vérifiées avec succès.")


def get_db():
    if not SessionLocal:
        raise RuntimeError("Base de données non configurée (DATABASE_URL manquant).")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
