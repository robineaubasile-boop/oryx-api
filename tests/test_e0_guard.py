"""E0 — refus de production, gardes d'environnement, de CI et d'écriture (spécification V0.2-R3 § 2.2, § 3.1 à § 3.3)."""
import os

import pytest

from tests import e0_support as s

import e0_guard as g  # noqa: E402

IMAGE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "HOSTNAME": "abc123", "LANG": "C.UTF-8",
             "GPG_KEY": "A035C8C19219BA821ECEA86B64E628F8D684696D", "PYTHON_VERSION": "3.11.9",
             "PYTHON_SHA256": "0" * 64, "PORT": "8080"}
RAILWAY_ENV = {"RAILWAY_SERVICE_NAME": "e0-origin", "RAILWAY_PROJECT_NAME": "oryx-e0-20261120",
               "RAILWAY_PUBLIC_DOMAIN": "e0-origin-production-1a2b.up.railway.app",
               "RAILWAY_REPLICA_ID": "r-1", "RAILWAY_DEPLOYMENT_ID": "d-1", "RAILWAY_ENVIRONMENT_NAME": "e0"}
ORIGIN_E0 = {"E0_ROLE": "origin", "E0_PHASE": "2", "E0_PUBLIC_HOST": s.PUBLIC_HOST, "E0_VERIFY_KEYS": "[]"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


def test_production_denylist_mirrors_the_versioned_config():
    config = s.config("production_denylist.v1.json")
    markers = [m["marker"] for m in config["host_markers"]] + [m["marker"] for m in config["founder_additions"]]
    assert list(g.PRODUCTION_HOST_MARKERS) == markers == ["oryx-api-production"]


@pytest.mark.parametrize("host", [
    "oryx-api-production.up.railway.app", "ORYX-API-PRODUCTION.up.railway.app", "oryx-api-production.up.railway.app.",
    " oryx-api-production.up.railway.app ", "xn--oryx-api-production-.example", "a.oryx-api-production.example",
])
def test_production_hosts_are_refused_first(host):
    assert g.is_production_host(host)
    with pytest.raises(g.GuardError) as excinfo:
        g.check_target_host(host, allowed=[host.strip().lower().rstrip(".")])
    assert excinfo.value.exit_code == g.EXIT_PRODUCTION_TARGET


def test_punycode_labels_are_decoded_before_comparison():
    # étiquette IDNA décodée contenant le marqueur après décodage (ü → xn--)
    encoded = "oryx-api-production-ü".encode("idna").decode("ascii")
    assert encoded.startswith("xn--")
    assert g.is_production_host(encoded + ".example")


@pytest.mark.parametrize("host", ["e0-origin-x.up.railway.app.attacker.example", "e0-originx.up.railway.app.evil",
                                  "x-e0-origin.up.railway.app", "e0-origin.up.railway.app:443",
                                  "e0-origin_x.up.railway.app", "e0-origin.up.railway.app.", "192.0.2.1"])
def test_spoofed_origin_suffixes_are_refused(host):
    with pytest.raises(g.GuardError):
        g.check_origin_host(host.rstrip(".") + ".evil" if host.endswith(".") else host)


def test_origin_host_is_normalised_then_matched_exactly():
    assert g.check_origin_host("E0-Origin-A.up.railway.app.") == "e0-origin-a.up.railway.app"


def test_valid_origin_and_target_hosts():
    assert g.check_origin_host("e0-origin-production-1a2b.up.railway.app") == "e0-origin-production-1a2b.up.railway.app"
    assert g.check_target_host("App.E0-Zone.Example.", allowed=[s.PUBLIC_HOST]) == s.PUBLIC_HOST
    with pytest.raises(g.GuardError):
        g.check_target_host("other.e0-zone.example", allowed=[s.PUBLIC_HOST])
    for literal in ("192.0.2.1", "10.0.0.1", "1.2.3.4"):
        with pytest.raises(g.GuardError):
            g.check_target_host(literal, allowed=[literal])


def test_public_host_rules():
    assert g.check_public_host(s.PUBLIC_HOST) == s.PUBLIC_HOST
    for bad in ("APP.e0-zone.example", "app.e0-zone.example.", "app", "e0-origin-x.up.railway.app",
                "oryx-api-production.example", "app.e0-zone.example:443", None):
        with pytest.raises(g.GuardError):
            g.check_public_host(bad)


@pytest.mark.parametrize("name", ["ORYX_F2_AUTH_KEY", "ORYX_TEST_DATABASE_URL", "DATABASE_URL", "PGPASSWORD",
                                  "ANTHROPIC_API_KEY", "EODHD_API_TOKEN", "FMP_KEY", "SUPABASE_URL", "RESEND_KEY",
                                  "MY_SECRET", "SOME_TOKEN", "OPENAI_API_KEY", "REDIS_URL", "X_CREDENTIALS",
                                  "POSTGRES_PASSWORD"])
def test_generic_denylist(name):
    assert g.forbidden_variables({name: "valeur-secrete"}) == [name]


def test_only_names_never_values_are_reported():
    problems = g.origin_environment_violations(dict(IMAGE_ENV, ORYX_F2_X="valeur-tres-secrete"))
    assert problems and all("valeur-tres-secrete" not in p for p in problems)
    with pytest.raises(g.GuardError) as excinfo:
        g.check_origin_environment({"ANTHROPIC_API_KEY": "sk-ant-should-never-print"})
    assert "sk-ant-should-never-print" not in str(excinfo.value)
    assert excinfo.value.exit_code == g.EXIT_ENVIRONMENT_NOT_ISOLATED


def test_origin_whitelist_accepts_the_official_image_and_railway_environment():
    environ = dict(IMAGE_ENV, **RAILWAY_ENV, **ORIGIN_E0)
    assert g.origin_environment_violations(environ) == []
    assert g.origin_environment_violations(dict(IMAGE_ENV, **ORIGIN_E0)) == []


@pytest.mark.parametrize("name", ["FORWARDED_ALLOW_IPS", "LD_PRELOAD", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
                                  "E0_UNKNOWN", "E0_SIGNING_KEY_2", "E0_CF_INVENTORY_TOKEN", "WEB_CONCURRENCY",
                                  "UVICORN_PROXY_HEADERS", "CI", "GITHUB_ACTIONS", "OPENSSL_CONF"])
def test_origin_whitelist_refuses_any_unknown_name(name):
    problems = g.origin_environment_violations(dict(IMAGE_ENV, **ORIGIN_E0, **{name: "1"}))
    assert any(name in p for p in problems)


@pytest.mark.parametrize("override, fragment", [
    ({"RAILWAY_SERVICE_NAME": "api"}, "RAILWAY_SERVICE_NAME"),
    ({"RAILWAY_PROJECT_NAME": "oryx-api"}, "RAILWAY_PROJECT_NAME"),
    ({"RAILWAY_PUBLIC_DOMAIN": "oryx-api-production.up.railway.app"}, "RAILWAY_PUBLIC_DOMAIN"),
    ({"RAILWAY_PUBLIC_DOMAIN": "e0-origin.up.railway.app.attacker.example"}, "RAILWAY_PUBLIC_DOMAIN"),
    ({"RAILWAY_PROJECT_NAME": "oryx-e0-oryx-api-production"}, "production"),
])
def test_railway_identity_must_be_e0(override, fragment):
    environ = dict(IMAGE_ENV, **RAILWAY_ENV, **ORIGIN_E0)
    environ.update(override)
    assert any(fragment in p for p in g.origin_environment_violations(environ))


def test_railway_without_identity_is_refused():
    environ = dict(IMAGE_ENV, **ORIGIN_E0, RAILWAY_REPLICA_ID="r-1")
    problems = g.origin_environment_violations(environ)
    assert len([p for p in problems if p.startswith("RAILWAY_")]) == 3


def test_inventory_token_exception_is_exact_and_nominative():
    assert g.forbidden_variables({"E0_CF_INVENTORY_TOKEN": "x"}, allow=[g.INVENTORY_TOKEN_NAME]) == []
    assert g.forbidden_variables({"E0_CF_INVENTORY_TOKEN": "x"}) == ["E0_CF_INVENTORY_TOKEN"]
    assert g.forbidden_variables({"E0_CF_INVENTORY_TOKEN2": "x"}, allow=[g.INVENTORY_TOKEN_NAME]) == \
        ["E0_CF_INVENTORY_TOKEN2"]
    assert g.forbidden_variables({"CLOUDFLARE_API_TOKEN": "x"}, allow=[g.INVENTORY_TOKEN_NAME]) == \
        ["CLOUDFLARE_API_TOKEN"]


@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS"])
def test_remote_tools_refuse_to_run_in_ci(marker):
    with pytest.raises(g.GuardError):
        g.refuse_in_ci({marker: "true"})
    assert any(marker in p for p in g.tool_environment_violations({marker: "true"}))
    g.refuse_in_ci({"PATH": "/usr/bin"})


def test_refuses_to_write_inside_a_git_tree(tmp_path):
    with pytest.raises(g.GuardError):
        g.refuse_write_in_repo(s.REPO_ROOT / "diagnostics" / "e0" / "evidence.jsonl")
    with pytest.raises(g.GuardError):
        g.refuse_write_in_repo(s.REPO_ROOT)
    outside = tmp_path / "evidence" / "run.jsonl"
    if not g.inside_git_tree(tmp_path):
        assert g.refuse_write_in_repo(outside) == os.path.realpath(outside)
    fake_repo = tmp_path / "repo"
    (fake_repo / ".git").mkdir(parents=True)
    with pytest.raises(g.GuardError):
        g.refuse_write_in_repo(fake_repo / "deep" / "file")


def test_origin_startup_refuses_forbidden_variables_with_exit_code_3():
    for name in ("ORYX_F2_SECRET", "FORWARDED_ALLOW_IPS", "DATABASE_URL", "E0_SIGNING_KEY_2", "PYTHONPATH"):
        value = f"valeur-distinctive-{name.lower()}"
        code, out, err = s.run_origin_once(s.origin_environment(**{name: value}))
        assert code == 3 and name in err and "E0-ORIGIN" not in out
        assert value not in err and value not in out


def test_origin_startup_refuses_production_like_railway_identity():
    env = s.origin_environment(RAILWAY_SERVICE_NAME="api", RAILWAY_PROJECT_NAME="oryx-api",
                               RAILWAY_PUBLIC_DOMAIN="oryx-api-production.up.railway.app")
    code, out, err = s.run_origin_once(env)
    assert code == 3 and "E0-ORIGIN" not in out
