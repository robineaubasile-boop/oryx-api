import os
from sqlalchemy import create_engine, text
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
