"""Garde-fou de session pytest (R1 PR 1).

Les tests de migration détruisent le schéma public de la base de test : la
session est interrompue avant toute collecte si l'environnement contient une
variable Railway, une URL applicative ou une variable libpq (voir
tests/pg_guard.py). Seuls les noms des variables sont affichés.
"""
import pytest

from tests.pg_guard import forbidden_environment


def pytest_configure(config):
    banned = forbidden_environment()
    if banned:
        pytest.exit(
            "Tests interrompus : variables interdites dans l'environnement ("
            + ", ".join(banned)
            + "). Lancer les tests sans aucune variable Railway / DATABASE_* / PG*.",
            returncode=3,
        )
