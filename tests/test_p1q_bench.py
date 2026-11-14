"""ACT-L1 / F04-D / P1-Q — banc de diagnostic : pseudonymisation, garde
d'isolement, catalogue de la sonde, analyseur et isolement du code.

Aucun test ici ne contacte Railway : les essais Q1-Q6 (edge public, réseau
privé) restent NON EXÉCUTÉS jusqu'à autorisation du fondateur.
"""
import ast
import io
import ipaddress
import json
import pickle
import re

import pytest

from tests.p1q_support import (BENCH_DIR, REPO_ROOT, p1q_analyze, p1q_guard, p1q_prober, p1q_redact,
                               receiver_process)

GLOBAL4, GLOBAL6 = "8.8.8.8", "2606:4700:4700::1111"


# --------------------------------------------------------------------------
# Pseudonymisation (protocole § 3.3)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, category", [
    ("203.0.113.7", "documentation"), ("2001:db8::7", "documentation"), ("10.1.2.3", "private_rfc1918"),
    ("172.20.0.1", "private_rfc1918"), ("192.168.1.1", "private_rfc1918"), ("100.64.0.9", "shared_cgnat"),
    ("fd12:3456::1", "unique_local"), ("127.0.0.1", "loopback"), ("::1", "loopback"),
    ("fe80::1", "link_local"), ("169.254.1.1", "link_local"), ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"), ("::ffff:10.0.0.1", "ipv4_mapped"),
])
def test_non_global_addresses_stay_in_clear_with_their_category(text, category):
    described = p1q_redact.Redactor().describe(text)
    assert described["category"] == category and described["sensitive"] is False
    assert described["text"] == ipaddress.ip_address(text).compressed


@pytest.mark.parametrize("text", [GLOBAL4, "1.1.1.1", GLOBAL6, "2a01:cb00::1"])
def test_global_addresses_are_never_in_clear(text):
    r = p1q_redact.Redactor()
    described = r.describe(text)
    assert described["category"] == "global" and described["sensitive"] is True
    assert re.fullmatch(r"g[46]:[0-9a-f]{16}", described["text"])
    assert text not in json.dumps(described)
    assert described["text"] == r.describe(text)["text"]
    assert described["text"] != p1q_redact.Redactor().describe(text)["text"]


def test_mapped_global_address_is_pseudonymised_on_its_ipv4():
    r = p1q_redact.Redactor()
    described = r.describe("::ffff:8.8.8.8")
    assert described["text"] == "::ffff:" + r.pseudonym(ipaddress.ip_address(GLOBAL4))
    assert described["mapped_inner"]["category"] == "global" and GLOBAL4 not in json.dumps(described)


@pytest.mark.parametrize("value, absent, kept", [
    (f"for={GLOBAL4};proto=https", [GLOBAL4], ["for=", ";proto=https"]),
    (f'for="[{GLOBAL6}]:4711"', [GLOBAL6, "2606:4700"], ["]:4711"]),
    (f"{GLOBAL4}:443", [GLOBAL4], [":443"]),
    (f"{GLOBAL4}.5", [GLOBAL4], [".5"]),
    ("2a01:cb00::1::5", ["2a01:cb00"], ["<opaque:15>"]),
    ("203.0.113.7, 198.51.100.23", [], ["203.0.113.7, 198.51.100.23"]),
    ("2001:db8::1::2", [], ["2001:db8::1::2"]),
    ("::ffff:8.8.8.8:80", [GLOBAL4], ["::ffff:<g4:"]),
    ("not-an-ip", [], ["not-an-ip"]),
])
def test_free_text_redaction(value, absent, kept):
    out = p1q_redact.Redactor().redact_text(value)
    for needle in absent:
        assert needle not in out
    for needle in kept:
        assert needle in out


def test_free_text_keeps_the_original_form_of_non_sensitive_addresses():
    r = p1q_redact.Redactor()
    assert r.redact_text("2001:DB8:0:0:0:0:0:7") == "2001:DB8:0:0:0:0:0:7"
    assert r.redact_text("for=2A01:CB00::1").startswith("for=g6:")


def test_free_text_is_clipped_and_control_characters_escaped():
    r = p1q_redact.Redactor()
    assert r.redact_text("a\x00b\x7f") == "a\\x00b\\x7f"
    long = "x" * (p1q_redact.MAX_VALUE_CHARS + 10)
    assert r.redact_text(long).endswith("…<+10>")


def test_redactor_key_is_never_exposed():
    r = p1q_redact.Redactor(b"k" * 32)
    assert "k" * 16 not in repr(r) and "k" * 16 not in str(r)
    with pytest.raises(TypeError):
        pickle.dumps(r)
    with pytest.raises(ValueError):
        p1q_redact.Redactor(b"short")


def test_commitment_round_trip_and_mapped_equivalence():
    key = b"\x01" * 32
    r = p1q_redact.Redactor()
    macs = [p1q_redact.commitment(key, GLOBAL4)]
    assert r.matches_commitment(ipaddress.ip_address(GLOBAL4), key.hex(), macs)
    assert r.matches_commitment(ipaddress.ip_address("::ffff:8.8.8.8"), key.hex(), macs)
    assert not r.matches_commitment(ipaddress.ip_address("1.1.1.1"), key.hex(), macs)
    assert not r.matches_commitment(ipaddress.ip_address(GLOBAL4), "zz", macs)
    assert not r.matches_commitment(ipaddress.ip_address(GLOBAL4), "01" * 8, macs)  # clé trop courte


# --------------------------------------------------------------------------
# Garde d'isolement (protocole § 3.2)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["DATABASE_URL", "DATABASE_PUBLIC_URL", "PGHOST", "PGPASSWORD",
                                  "ORYX_F2_AUTH_KEYS_JSON", "ORYX_ANYTHING", "ANTHROPIC_API_KEY",
                                  "EODHD_API_KEY", "FMP_API_KEY", "SUPABASE_URL", "RESEND_API_KEY",
                                  "SOME_SECRET", "GITHUB_TOKEN", "X_PASSWORD", "POSTGRES_PASSWORD"])
def test_guard_refuses_production_like_variables(name):
    assert p1q_guard.forbidden_variables({name: "x", "PORT": "8080"}) == [name]


def test_guard_accepts_the_bench_variables():
    env = {"PORT": "8080", "P1Q_ROLE": "receiver", "P1Q_UVICORN_MODE": "strict", "P1Q_TARGET": "http://a:1",
           "FORWARDED_ALLOW_IPS": "*", "RAILWAY_PRIVATE_DOMAIN": "p1q-receiver.railway.internal",
           "RAILWAY_PUBLIC_DOMAIN": "p1q-receiver-p1q.up.railway.app", "RAILWAY_ENVIRONMENT_NAME": "p1q",
           "RAILWAY_DEPLOYMENT_ID": "d", "RAILWAY_REPLICA_REGION": "europe-west4", "PATH": "/usr/bin"}
    assert p1q_guard.forbidden_variables(env) == []


def test_guard_accepts_the_official_python_image_environment():
    image_env = {"PATH": "/usr/local/bin:/usr/bin", "LANG": "C.UTF-8", "GPG_KEY": "A035C8C19219BA821ECEA86B64E628F8",
                 "PYTHON_VERSION": "3.11.9", "PYTHON_PIP_VERSION": "24.0", "PYTHON_SETUPTOOLS_VERSION": "65.5.1",
                 "PYTHON_GET_PIP_URL": "https://example.invalid/get-pip.py", "PYTHON_GET_PIP_SHA256": "0" * 64,
                 "PYTHON_SHA256": "0" * 64, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                 "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_CACHE_DIR": "1", "HOME": "/nonexistent"}
    assert p1q_guard.forbidden_variables(image_env) == []


@pytest.mark.parametrize("host", ["oryx-api-production.up.railway.app", "OrYx-Api-Production.up.railway.app.",
                                  "x.oryx-api-production.up.railway.app"])
def test_production_target_is_refused(host):
    with pytest.raises(ValueError):
        p1q_guard.check_target_host(host)
    assert p1q_prober.main(["--role", "public", "--target", f"https://{host}"], {}) == 4


def test_prober_roles_require_the_right_transport(capsys):
    with pytest.raises(SystemExit):
        p1q_prober.main(["--role", "public", "--target", "http://127.0.0.1:1"], {})
    with pytest.raises(SystemExit):
        p1q_prober.main(["--role", "private", "--target", "https://p1q.up.railway.app"], {})


@pytest.mark.parametrize("value", ["192.168.1.10", "203.0.113.7", "not-an-ip", " 8.8.8.8"])
def test_prober_requires_a_global_self_address(value, capsys):
    with pytest.raises(SystemExit):
        p1q_prober.main(["--role", "public", "--target", "https://p1q.up.railway.app", "--self-ip", value], {})


def test_prober_refuses_a_non_isolated_railway_environment(capsys):
    env = {"RAILWAY_ENVIRONMENT_NAME": "p1q", "DATABASE_URL": "postgresql://synthetic"}
    assert p1q_prober.main(["--role", "private", "--target", "http://127.0.0.1:1"], env) == 3
    assert "DATABASE_URL" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Catalogue et requêtes de la sonde (protocole § 4)
# --------------------------------------------------------------------------

def test_catalogue_covers_q1_to_q7_with_unique_ids_and_scopes():
    ids = [c.id for c in p1q_prober.CASES]
    assert len(ids) == len(set(ids))
    assert {c.q for c in p1q_prober.CASES} == {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"}
    for case in p1q_prober.CASES:
        assert case.id.startswith(case.q + "-")
        if case.q == "Q1":
            assert case.scopes == {"public"}
        if case.q in ("Q5", "Q6"):
            assert case.scopes == {"private"}
    assert len(p1q_prober.cases_for("public")) >= 30 and len(p1q_prober.cases_for("private")) >= 30


def test_catalogue_forges_only_documentation_or_non_routable_addresses():
    r = p1q_redact.Redactor()
    for case in p1q_prober.CASES:
        texts = [v for _, v in case.headers] + list(case.raw_lines)
        if case.imitate_edge:
            texts += [v for _, v in p1q_prober.edge_imitation_headers("p1q.up.railway.app")]
        for text in texts:
            out = r.redact_text(text)
            assert out.startswith(text[:p1q_redact.MAX_VALUE_CHARS]) and "g4:" not in out and "g6:" not in out and "<opaque" not in out, (case.id, text)
    for value in p1q_prober.FORGED_VALUES:
        assert not ipaddress.ip_address(value).is_global


def test_requests_are_written_byte_for_byte():
    dup = next(c for c in p1q_prober.CASES if c.id == "Q4-01")
    payload = p1q_prober.build_request(dup, host="p1q-receiver.railway.internal:8080")
    assert payload.startswith(b"GET /p1q/observe HTTP/1.1\r\nHost: p1q-receiver.railway.internal:8080\r\n")
    assert payload.count(b"X-Real-IP: ") == 2 and payload.endswith(b"\r\n\r\n")
    fold = next(c for c in p1q_prober.CASES if c.id == "Q4-14")
    assert b"X-Real-IP: 203.0.113.7\r\n 198.51.100.23\r\n\r\n" in p1q_prober.build_request(fold, host="h")
    bad = p1q_prober.Case("X-01", "Q2", frozenset({"public"}), "injection", (("X-Real-IP", "1\r\nX: y"),))
    with pytest.raises(ValueError):
        p1q_prober.build_request(bad, host="h")
    assert p1q_prober.host_header("fd12::1", 8080, False) == "[fd12::1]:8080"
    assert p1q_prober.host_header("a.up.railway.app", 443, True) == "a.up.railway.app"


def test_edge_imitation_replays_every_documented_edge_header():
    case = next(c for c in p1q_prober.CASES if c.id == "Q6-03")
    payload = p1q_prober.build_request(case, host="p1q-receiver.railway.internal:8080",
                                       public_host="p1q-receiver.up.railway.app").decode()
    assert "Host: p1q-receiver.up.railway.app\r\n" in payload
    for name in ("X-Real-IP: 203.0.113.7", "X-Forwarded-For: 203.0.113.7", "X-Forwarded-Proto: https",
                 "X-Forwarded-Host: p1q-receiver.up.railway.app", "X-Railway-Edge: ", "X-Railway-Request-Id: ",
                 "X-Request-Start: "):
        assert name in payload


def test_self_address_never_travels_in_clear():
    run = p1q_prober.Run(role="public", self_ips=[GLOBAL4, GLOBAL6], out=io.StringIO())
    payload = p1q_prober.build_request(p1q_prober.CASES[0], host="h", self_headers=run.self_headers)
    assert GLOBAL4.encode() not in payload and GLOBAL6.encode() not in payload
    assert b"X-P1Q-Self-Key: " in payload and payload.count(b",") >= 1


def test_end_to_end_private_run_against_a_local_receiver_is_complete_and_analysable(tmp_path):
    out = tmp_path / "private.jsonl"
    with receiver_process("strict") as rx:
        code = p1q_prober.main(["--role", "private", "--target", f"http://127.0.0.1:{rx.port}", "--family", "4",
                                "--delay", "0", "--prefix", "--out", str(out)], {})
    assert code == 0
    lines = out.read_text().splitlines()
    assert lines[-1].startswith("P1Q-DIGEST ") and all(l.startswith("P1Q-RESULT ") for l in lines[:-1])
    results, digests, integrity = p1q_analyze.read_lines([str(out)])
    assert integrity == [] and len(results) == len(lines) - 1
    assert [d["complete"] for d in p1q_analyze.verify_digests(results, digests)] == [True]
    records = [d for _, d in results]
    assert all(r["outcome"] == "observed" for r in records)
    assert {r["case"] for r in records} == {c.id for c in p1q_prober.cases_for("private")}
    report = p1q_analyze.analyze(records)
    assert report["Q5"]["verdict"] == "PASS"
    # Sans observation publique, Q1-Q4 et Q6 ne peuvent pas conclure.
    for q in ("Q1", "Q2", "Q3", "Q4", "Q6"):
        assert report[q]["verdict"] == "INCONCLUSIVE", q
    assert report["option_a_proposal"] == "INCONCLUSIVE"
    # Tampering : une ligne modifiée rend la collecte incomplète.
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join([lines[0].replace('"attempt":1', '"attempt":9')] + lines[1:]) + "\n")
    results2, digests2, _ = p1q_analyze.read_lines([str(tampered)])
    assert [d["complete"] for d in p1q_analyze.verify_digests(results2, digests2)] == [False]


# --------------------------------------------------------------------------
# Analyseur : critères mécaniques (protocole § 5)
# --------------------------------------------------------------------------

NOMINAL = "g4:00112233445566aa"


def _addr(text, category, version=4):
    return {"parsed": True, "text": text, "category": category, "version": version, "canonical": True}


def _obs(*, boot="b1", xri=(), raw=("10.9.0.1", "private_rfc1918"), server=("10.9.0.5", "private_rfc1918"),
         self_xri=None, mode="strict", effective_equals_raw=True, env=None, headers=None, inventory=None):
    return {"p1q": "receiver-observation", "boot_id": boot, "mode": mode,
            "uvicorn": {"proxy_headers": mode != "strict", "forwarded_allow_ips_env": env,
                        "forwarded_allow_ips": "127.0.0.1"},
            "raw_client": _addr(*raw), "effective_client": _addr(*raw), "server": _addr(*server),
            "effective_equals_raw": effective_equals_raw,
            "x_real_ip": {"count": len(xri), "values": list(xri)},
            "self_match": ({"valid": True, "x_real_ip": self_xri} if self_xri is not None else None),
            "headers": headers or {}, "simulation": {}, "inventory": inventory or {}}


def _rec(role, case, o, *, target="primary", family=4, attempt=1):
    return {"p1q": "prober-result", "role": role, "target": target, "family": family, "case": case,
            "q": case.split("-")[0], "attempt": attempt, "outcome": "observed", "observation": o, "run_id": "r"}


def _public_nominal(boot="b1", n=5):
    return [_rec("public", "Q1-01", _obs(boot=boot, xri=[_addr(NOMINAL, "global")], self_xri=[True],
                                         raw=("100.64.1.2", "shared_cgnat"), server=("10.9.0.5", "private_rfc1918")),
                 attempt=i + 1) for i in range(n)]


def test_q1_pass_requires_engaged_global_stable_x_real_ip():
    assert p1q_analyze.analyze_q1(_public_nominal())["verdict"] == "PASS"
    assert p1q_analyze.analyze_q1(_public_nominal(n=4))["verdict"] == "INCONCLUSIVE"
    wrong = _public_nominal()
    wrong[0]["observation"]["self_match"]["x_real_ip"] = [False]
    assert p1q_analyze.analyze_q1(wrong)["verdict"] == "FAIL"
    absent = _public_nominal()
    absent[1]["observation"]["x_real_ip"] = {"count": 0, "values": []}
    assert p1q_analyze.analyze_q1(absent)["verdict"] == "FAIL"


def test_q2_forged_x_real_ip_preserved_by_the_edge_is_a_fail():
    base = _public_nominal()
    overwritten = base + [_rec("public", "Q2-01", _obs(xri=[_addr(NOMINAL, "global")], self_xri=[True]))]
    ref = p1q_analyze.nominal_reference(overwritten)
    assert p1q_analyze.analyze_header_q(overwritten, "Q2", ref)["verdict"] == "PASS"
    kept = base + [_rec("public", "Q2-01", _obs(xri=[_addr("203.0.113.7", "documentation")]))]
    assert p1q_analyze.analyze_header_q(kept, "Q2", p1q_analyze.nominal_reference(kept))["verdict"] == "FAIL"
    twice = base + [_rec("public", "Q4-01", _obs(xri=[_addr(NOMINAL, "global"), _addr(NOMINAL, "global")]))]
    assert p1q_analyze.analyze_header_q(twice, "Q4", p1q_analyze.nominal_reference(twice))["verdict"] == "FAIL"
    rejected = base + [{**_rec("public", "Q4-14", None), "outcome": "not_observed", "status": 400}]
    rejected.append(_rec("public", "Q4-01", _obs(xri=[_addr(NOMINAL, "global")], self_xri=[True])))
    assert p1q_analyze.analyze_header_q(rejected, "Q4",
                                        p1q_analyze.nominal_reference(rejected))["verdict"] == "PASS"


def test_q6_indistinguishable_private_spoof_is_a_fail():
    public = _public_nominal()
    private = [_rec("private", "Q6-01", _obs(xri=[_addr("203.0.113.7", "documentation")],
                                             raw=("100.64.3.4", "shared_cgnat"), server=("10.9.0.5", "private_rfc1918")))]
    result = p1q_analyze.analyze_q6(public + private)
    assert result["verdict"] == "FAIL" and result["facts"]["candidate_discriminators"] == []


def test_q6_pass_requires_a_stable_discriminator_over_two_boots_and_failed_bypass():
    inventory = {"private_domain_resolution": {"ipv6": [_addr("fd12::5", "unique_local", 6)]}}

    def private(boot):
        return _rec("private", "Q6-01", _obs(boot=boot, xri=[_addr("203.0.113.7", "documentation")],
                                             raw=("fd12::9", "unique_local", 6), server=("fd12::5", "unique_local", 6),
                                             inventory=inventory))

    records = _public_nominal("b1") + _public_nominal("b2") + [private("b1"), private("b2")]
    one_boot = p1q_analyze.analyze_q6(_public_nominal("b1") + [private("b1")])
    assert one_boot["verdict"] == "INCONCLUSIVE"
    no_bypass_attempt = p1q_analyze.analyze_q6(records)
    assert no_bypass_attempt["verdict"] == "INCONCLUSIVE"
    blocked = {**_rec("private", "Q6-01", None, target="extra:0"), "outcome": "connect_error"}
    result = p1q_analyze.analyze_q6(records + [blocked])
    assert result["verdict"] == "PASS"
    assert result["facts"]["discriminators"]["D2_server_address"]["rule_self_discovered"] is True
    bypass = _rec("private", "Q6-01", _obs(raw=("100.64.1.2", "shared_cgnat")), target="extra:0")
    assert p1q_analyze.analyze_q6(records + [bypass])["verdict"] == "FAIL"


def test_q7_requires_env_independence_and_a_detecting_negative_control():
    strict = _rec("private", "Q7-01", _obs(mode="strict", env="*"))
    negative = _rec("private", "Q7-01", _obs(mode="trust_all_negative_control", effective_equals_raw=False))
    assert p1q_analyze.analyze_q7([strict, negative])["verdict"] == "PASS"
    assert p1q_analyze.analyze_q7([_rec("private", "Q7-01", _obs(mode="strict")), negative])["verdict"] \
        == "INCONCLUSIVE"
    rewritten = _rec("private", "Q7-01", _obs(mode="strict", env="*", effective_equals_raw=False))
    assert p1q_analyze.analyze_q7([rewritten, negative])["verdict"] == "FAIL"


def test_any_fail_makes_the_proposal_no_go_and_nothing_is_ever_a_go():
    kept = _public_nominal() + [_rec("public", "Q2-01", _obs(xri=[_addr("203.0.113.7", "documentation")]))]
    report = p1q_analyze.analyze(kept)
    assert report["Q2"]["verdict"] == "FAIL" and report["option_a_proposal"] == "NO-GO"
    assert "non décisionnelle" in report["warning"]
    source = (BENCH_DIR / "p1q_analyze.py").read_text(encoding="utf-8")
    assert '"GO"' not in source  # jamais un GO nu : au mieux un GO-CANDIDATE soumis à audit


# --------------------------------------------------------------------------
# Isolement du banc vis-à-vis du runtime Oryx (protocole § 3)
# --------------------------------------------------------------------------

BENCH_FILES = sorted(BENCH_DIR.glob("*.py"))
ALLOWED_IMPORTS = {"argparse", "dataclasses", "hashlib", "hmac", "ipaddress", "json",
                   "os", "re", "secrets", "socket", "ssl", "sys", "threading", "time", "urllib",
                   "uvicorn", "p1q_guard", "p1q_redact", "p1q_receiver", "p1q_prober", "p1q_analyze"}


def test_bench_imports_only_stdlib_uvicorn_and_itself():
    assert [p.name for p in BENCH_FILES] == ["p1q_analyze.py", "p1q_guard.py", "p1q_main.py", "p1q_prober.py",
                                             "p1q_receiver.py", "p1q_redact.py"]
    for path in BENCH_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert roots <= ALLOWED_IMPORTS, (path.name, roots)


def test_bench_names_no_oryx_variable_secret_or_production_endpoint():
    for path in sorted(BENCH_DIR.iterdir()):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8")
        assert "ORYX_F2_" not in text, path.name
        assert "oryx-api-production.up.railway.app" not in text or path.name == "p1q_guard.py", path.name
        for forbidden in ("core.", "from core", "import api", "sqlalchemy", "psycopg", "anthropic", "supabase"):
            assert forbidden not in text.lower() or path.name == "p1q_guard.py", (path.name, forbidden)


def _requirement_blocks(text):
    blocks = {}
    for block in re.split(r"\n(?=[a-z0-9_.-]+==)", text):
        match = re.match(r"([a-z0-9_.-]+)==(\S+)", block)
        if match:
            blocks[match.group(1)] = (match.group(2), sorted(re.findall(r"sha256:[0-9a-f]{64}", block)))
    return blocks


def test_bench_dependencies_are_the_production_pins_and_hashes():
    bench = _requirement_blocks((BENCH_DIR / "requirements.txt").read_text(encoding="utf-8"))
    production = _requirement_blocks((REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"))
    assert set(bench) == {"uvicorn", "h11", "click"}
    for name, pin in bench.items():
        assert pin == production[name], name
    assert bench["uvicorn"][0] == "0.27.0"


def test_container_and_railway_config_are_minimal_and_non_restarting():
    dockerfile = (BENCH_DIR / "Dockerfile").read_text(encoding="utf-8")
    runtime = (REPO_ROOT / "runtime.txt").read_text(encoding="utf-8").strip()
    assert f"FROM python:{runtime.split('-')[1]}-slim" in dockerfile
    assert "--require-hashes" in dockerfile and "USER nobody" in dockerfile
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert copies == ["COPY requirements.txt ./",
                      "COPY p1q_guard.py p1q_redact.py p1q_receiver.py p1q_prober.py p1q_analyze.py p1q_main.py ./"]
    assert "ENV" in dockerfile and "ORYX" not in dockerfile and "ARG" not in dockerfile
    config = json.loads((BENCH_DIR / "railway.json").read_text(encoding="utf-8"))
    assert config["deploy"] == {"startCommand": "python p1q_main.py", "restartPolicyType": "NEVER"}
    assert config["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}


def test_production_runtime_never_references_the_bench():
    for path in [REPO_ROOT / "api.py", *sorted((REPO_ROOT / "core").rglob("*.py")),
                 *sorted((REPO_ROOT / "scripts").rglob("*.py"))]:
        assert "p1q" not in path.read_text(encoding="utf-8", errors="replace").lower(), path
    # Aucun fichier de configuration Railway au niveau du dépôt (le service servi reste inchangé).
    for name in ("railway.json", "railway.toml", "Procfile", "nixpacks.toml", "Dockerfile"):
        assert not (REPO_ROOT / name).exists(), name


# --------------------------------------------------------------------------
# Chaîne publique complète contre un « edge » TLS SIMULÉ (pas Railway)
# --------------------------------------------------------------------------

def _public_run(tmp_path, monkeypatch, *, overwrite):
    from tests.p1q_support import FakeEdge, make_localhost_certificate

    key, cert = make_localhost_certificate(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    out = tmp_path / ("public_ok.jsonl" if overwrite else "public_bad.jsonl")
    with receiver_process("strict") as rx, FakeEdge(rx.port, key, cert, client_ip=GLOBAL4,
                                                    overwrite=overwrite) as edge:
        code = p1q_prober.main(["--role", "public", "--target", f"https://localhost:{edge.port}", "--family", "4",
                                "--self-ip", GLOBAL4, "--delay", "0", "--only", "Q1", "--only", "Q2",
                                "--only", "Q3", "--only", "Q4", "--out", str(out)], {})
    assert code == 0
    assert GLOBAL4 not in out.read_text()
    results, digests, _ = p1q_analyze.read_lines([str(out)])
    assert [d["complete"] for d in p1q_analyze.verify_digests(results, digests)] == [True]
    return p1q_analyze.analyze([d for _, d in results]), [d for _, d in results]


def test_public_chain_passes_against_an_edge_that_overwrites_x_real_ip(tmp_path, monkeypatch):
    report, records = _public_run(tmp_path, monkeypatch, overwrite=True)
    assert all(r["outcome"] == "observed" and r["tls"] is True and "connect_address" not in r for r in records)
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert report[q]["verdict"] == "PASS", (q, report[q]["reasons"])
    # Sans essais privés ni modes Q7, aucune conclusion globale.
    assert report["option_a_proposal"] == "INCONCLUSIVE"


def test_public_chain_fails_against_an_edge_that_keeps_a_forged_x_real_ip(tmp_path, monkeypatch):
    report, _ = _public_run(tmp_path, monkeypatch, overwrite=False)
    assert report["Q1"]["verdict"] == "PASS"
    assert report["Q2"]["verdict"] == "FAIL" and report["Q4"]["verdict"] == "FAIL"
    assert report["option_a_proposal"] == "NO-GO"
