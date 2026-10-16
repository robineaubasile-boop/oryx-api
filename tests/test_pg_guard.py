"""Tests sans base des garde-fous PostgreSQL destructeurs (tests/pg_guard.py)."""
import pytest

from tests.pg_guard import (
    UnsafeTestDatabase,
    forbidden_environment,
    scrubbed_environment,
    validate_test_database_url,
)


@pytest.mark.parametrize("url", [
    "postgresql://oryx_test@/oryx_pr1_test?host=/tmp/oryxpg",
    "postgresql+psycopg2://postgres:postgres@localhost/oryx_t1a_test",
    "postgresql://postgres@127.0.0.1:55432/oryx_lab_test",
    "postgresql://postgres@[::1]:5432/oryx_pr1_test",
])
def test_local_disposable_urls_are_accepted(url):
    assert validate_test_database_url(url).database.endswith("_test")


@pytest.mark.parametrize("url, reason", [
    ("", "vide"),
    ("sqlite:///oryx_pr1_test", "PostgreSQL"),
    ("postgresql://postgres:secret@containers-us-west-1.railway.app:6543/oryx_pr1_test", "Railway"),
    ("postgresql://postgres:secret@monorail.proxy.rlwy.net:41234/oryx_pr1_test", "Railway"),
    ("postgresql://postgres:secret@postgres.railway.internal:5432/oryx_pr1_test", "Railway"),
    ("postgresql://postgres:secret@db.example.com/oryx_pr1_test", "boucle locale"),
    ("postgresql://postgres:secret@10.0.0.5/oryx_pr1_test", "boucle locale"),
    ("postgresql://postgres@localhost/railway", "nom de base"),
    ("postgresql://postgres@localhost/oryx", "nom de base"),
    ("postgresql://postgres@localhost/production_test", "nom de base"),
    ("postgresql://postgres@/oryx_pr1_test", "hôte implicite"),
])
def test_unsafe_urls_are_refused_without_leaking_secrets(url, reason):
    with pytest.raises(UnsafeTestDatabase, match=reason) as error:
        validate_test_database_url(url)
    assert "secret" not in str(error.value)


def test_forbidden_environment_names_railway_and_application_urls():
    environ = {"HOME": "/x", "RAILWAY_ENVIRONMENT": "production", "DATABASE_PUBLIC_URL": "postgres://...",
               "DATABASE_URL": "postgres://...", "PGHOST": "db", "ORYX_TEST_DATABASE_URL": "postgresql://..."}
    assert forbidden_environment(environ) == ["DATABASE_PUBLIC_URL", "DATABASE_URL", "PGHOST", "RAILWAY_ENVIRONMENT"]
    assert scrubbed_environment(environ) == {"HOME": "/x", "ORYX_TEST_DATABASE_URL": "postgresql://..."}


def test_current_process_has_no_forbidden_environment():
    # tests/conftest.py interrompt la session avant d'arriver ici sinon.
    assert forbidden_environment() == []
