"""CLI interne T4-C : bootstrap + vérification de la taxonomie Oryx V1.

Usage (Railway : `railway ssh`, ou tout shell disposant de DATABASE_URL) :

    python -m scripts.bootstrap_pedagogical_taxonomy_v1
        bootstrap (release candidate oryx-v1, 45 définitions r1, 45
        memberships ; NO-OP si déjà présente et conforme), vérification
        stricte, COMMIT ; ROLLBACK de tout le lot à la moindre erreur.

    python -m scripts.bootstrap_pedagogical_taxonomy_v1 --verify-only
        vérification stricte seule, dans une transaction READ ONLY
        (PostgreSQL), jamais commitée.

Cette CLI n'active JAMAIS V1 : l'activation (activate_taxonomy_v1) reste une
action distincte et explicite, et le bootstrap n'est pas branché au
pre-deploy (`python -m alembic upgrade head` inchangé).

Contrairement aux fonctions de core/pedagogy/taxonomy_bootstrap.py, qui ne
commitent jamais, ce script possède sa Session (core.db.SessionLocal) :
try -> bootstrap -> verify -> commit / except -> rollback / finally ->
close.

Sortie : version_key, spec_fingerprint, capability_count, status ; exit 0.
Erreur métier T4-B / T4-C (SPEC invalide, conflit, V1 divergente) : message
explicite sur stderr, exit 1. Erreur opérationnelle (DATABASE_URL absent,
connexion, SQL) : exit 2 ; seul le type d'une erreur SQLAlchemy est affiché,
jamais son texte (qui peut contenir des fragments de DATABASE_URL).
"""
import argparse
import sys
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core import db as core_db
from core.pedagogy.taxonomy_bootstrap import bootstrap_taxonomy_v1, verify_taxonomy_v1


def _report(result, bootstrap=None) -> str:
    lines = [
        "ORYX TAXONOMY V1 " + ("BOOTSTRAP OK" if bootstrap is not None else "VERIFY OK"),
        f"version_key: {result.version_key}",
        f"spec_fingerprint: {result.spec_fingerprint}",
        f"capability_count: {result.capability_count}",
        f"status: {result.status}",
    ]
    if bootstrap is not None:
        if bootstrap.created_release:
            lines.append(f"bootstrap: created (definitions created={bootstrap.created_definitions}"
                         f" reused={bootstrap.reused_definitions}"
                         f" memberships={bootstrap.created_memberships})")
        else:
            lines.append("bootstrap: unchanged (oryx-v1 already present and conform, nothing written)")
        lines.append("activation: not performed (separate explicit action)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.bootstrap_pedagogical_taxonomy_v1",
        description="Bootstrap + vérification de la taxonomie pédagogique Oryx V1 (sans activation).")
    parser.add_argument("--verify-only", action="store_true",
                        help="vérifier oryx-v1 sans rien écrire (transaction READ ONLY, jamais commitée)")
    args = parser.parse_args(argv)

    if core_db.SessionLocal is None:
        print("TAXONOMY ERROR: DATABASE_URL n'est pas défini : aucune base à initialiser.", file=sys.stderr)
        return 2

    db = core_db.SessionLocal()
    try:
        if args.verify_only:
            db.execute(text("SET TRANSACTION READ ONLY"))
            result, bootstrap = verify_taxonomy_v1(db), None
            db.rollback()
        else:
            bootstrap = bootstrap_taxonomy_v1(db)
            result = verify_taxonomy_v1(db)
            db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        print(f"TAXONOMY ERROR: {type(exc).__name__} (rollback, rien n'est persisté)", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — erreur métier restituée, jamais committée
        db.rollback()
        print(f"TAXONOMY ERROR: {type(exc).__name__}: {exc} (rollback, rien n'est persisté)", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(_report(result, bootstrap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
