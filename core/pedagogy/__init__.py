"""Contenu pédagogique versionné d'Oryx (T4-C).

- taxonomy_v1 : SPEC canonique Oryx Taxonomy V1 (données, validation,
  canonicalisation, fingerprint). Python pur : aucune Session, aucune
  écriture.
- taxonomy_bootstrap : orchestration base de données au-dessus du service
  T4-B (core/taxonomy_service.py) : bootstrap idempotent, vérification
  stricte, activation contrôlée. Jamais de commit() ni de rollback().

Alembic porte la STRUCTURE (0007_pedagogical_taxonomy) ; ce paquet porte le
CONTENU sémantique versionné. Rien n'est exécuté à l'import.
"""
