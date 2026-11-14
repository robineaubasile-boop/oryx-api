"""ACT-L1 / F04-D / P1-Q — verdicts CONSERVATEURS de l'analyseur (correctifs C1-C4, C6).

Base : une campagne SYNTHÉTIQUE complète (tests/p1q_support.complete_campaign),
au schéma exact de la sonde et sérialisée avec de vraies empreintes, dont
l'analyseur doit conclure GO-CANDIDATE. Chaque test négatif retire ou altère
UN élément requis et vérifie :

- qu'aucun GO-CANDIDATE n'est alors possible ;
- qu'une preuve MANQUANTE donne INCONCLUSIVE (jamais PASS, jamais FAIL) ;
- qu'un contre-exemple DÉMONTRÉ donne FAIL / NO-GO, même si d'autres
  preuves manquent ou si la collecte est altérée.

La topologie synthétique est FICTIVE : elle ne prédit rien du comportement
réel de Railway.
"""
import json

import pytest

from tests.p1q_support import (NOMINAL, PUBLIC_PEER, PUBLIC_SERVER, campaign_report, complete_campaign,
                               observation, p1q_analyze, p1q_prober, record)

GO = "GO-CANDIDATE (soumis à l'audit indépendant et à la décision du fondateur)"
QUESTIONS = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7")


def _report(tmp_path, sessions=None, **kwargs):
    return campaign_report(sessions if sessions is not None else complete_campaign(), tmp_path=tmp_path, **kwargs)


def _only_inconclusive(report, *questions):
    """Les questions citées sont INCONCLUSIVE, aucune n'est FAIL, la proposition est INCONCLUSIVE."""
    for q in questions:
        assert report[q]["verdict"] == "INCONCLUSIVE", (q, report[q]["reasons"])
        assert report[q]["missing"], q
    assert all(report[q]["verdict"] != "FAIL" for q in QUESTIONS), {q: report[q]["verdict"] for q in QUESTIONS}
    assert report["option_a_proposal"] == "INCONCLUSIVE"


def _cell(target="primary", family=None, case=None, attempt=None):
    def predicate(r):
        return (r.get("target") == target and (family is None or r.get("family") == family)
                and (case is None or r.get("case") == case) and (attempt is None or r.get("attempt") == attempt))
    return predicate


# --------------------------------------------------------------------------
# Référence positive
# --------------------------------------------------------------------------

def test_the_complete_synthetic_campaign_is_the_only_go_candidate(tmp_path):
    report = _report(tmp_path)
    assert {q: report[q]["verdict"] for q in QUESTIONS} == {q: "PASS" for q in QUESTIONS}
    assert report["collection"]["proven"] is True
    assert report["option_a_proposal"] == GO and report["blocking"] == []
    assert report["Q6"]["facts"]["qualifying_boots"] == ["boot1", "boot2"]
    assert report["Q6"]["facts"]["strict_boots"]["boot1"]["bypass"] == {"10.250.0.5:8080": "refused:connect_refused"}
    assert report["required"]["families"] == {"public": [4, 6], "private": [4, 6], "q6p": [4]}


def test_the_analyser_never_emits_a_bare_go():
    source = (p1q_analyze.__file__ and open(p1q_analyze.__file__, encoding="utf-8").read())
    assert '"GO"' not in source and "'GO'" not in source


# --------------------------------------------------------------------------
# C1 — couverture exacte Q2 / Q3 / Q4 (et Q1)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("q", ["Q2", "Q3", "Q4"])
@pytest.mark.parametrize("family", [4, 6])
def test_one_missing_header_case_cell_forbids_pass(tmp_path, q, family):
    sessions = complete_campaign()
    case = next(c for c in p1q_prober.cases_for("public") if c.q == q).id
    sessions["pub2"].drop(_cell(family=family, case=case, attempt=1))
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, q)
    assert any(f"pub2/v{family}/{case}#1: not_run" in m for m in report[q]["missing"])


@pytest.mark.parametrize("q", ["Q2", "Q3", "Q4"])
def test_a_whole_missing_case_forbids_pass(tmp_path, q):
    sessions = complete_campaign()
    case = [c for c in p1q_prober.cases_for("public") if c.q == q][-1].id
    sessions["pub1"].drop(_cell(case=case))
    _only_inconclusive(_report(tmp_path, sessions), q)


@pytest.mark.parametrize("status, receiver_header, expected", [
    (400, False, "PASS"), (431, False, "PASS"), (413, False, "PASS"),     # refus explicite de l'edge : valide
    (400, True, "INCONCLUSIVE"),                                         # erreur du récepteur, pas de l'edge
    (502, False, "INCONCLUSIVE"), (503, False, "INCONCLUSIVE"), (404, False, "INCONCLUSIVE"),
])
def test_only_an_explicit_edge_refusal_replaces_an_observation(tmp_path, status, receiver_header, expected):
    sessions = complete_campaign()

    def refuse(r):
        r.pop("observation")
        r.update({"outcome": "not_observed", "status": status, "receiver_header": receiver_header})

    sessions["pub1"].edit(_cell(family=4, case="Q4-14"), refuse)
    report = _report(tmp_path, sessions)
    assert report["Q4"]["verdict"] == expected
    assert report["option_a_proposal"] == (GO if expected == "PASS" else "INCONCLUSIVE")


@pytest.mark.parametrize("outcome", ["connect_error", "no_response"])
def test_an_unusable_cell_is_inconclusive_not_fail(tmp_path, outcome):
    sessions = complete_campaign()

    def broken(r):
        r.pop("observation")
        r.update({"outcome": outcome, "error_class": "timeout" if outcome == "connect_error" else None})

    sessions["pub1"].edit(_cell(family=6, case="Q2-03"), broken)
    _only_inconclusive(_report(tmp_path, sessions), "Q2")


def test_a_missing_q1_attempt_forbids_pass(tmp_path):
    sessions = complete_campaign()
    sessions["pub1"].drop(_cell(family=6, case="Q1-01", attempt=5))
    _only_inconclusive(_report(tmp_path, sessions), "Q1")


def test_an_uncommitted_ipv6_self_address_is_inconclusive_not_fail(tmp_path):
    """--self-ip IPv4 seule : un X-Real-IP IPv6 non reconnu n'est pas une preuve d'erreur."""
    sessions = complete_campaign()

    def ipv4_only(r):
        r["self_committed_versions"] = [4]
        if r.get("family") == 6 and r.get("observation"):
            r["observation"]["self_match"] = {"valid": True, "x_real_ip": [False]}

    sessions["pub1"].edit(lambda r: True, ipv4_only)
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q1")
    assert any("adresse non engagée" in m for m in report["Q1"]["missing"])


def test_a_committed_self_address_mismatch_is_a_demonstrated_fail(tmp_path):
    sessions = complete_campaign()
    sessions["pub1"].edit(_cell(family=4, case="Q1-01", attempt=2),
                          lambda r: r["observation"].update(self_match={"valid": True, "x_real_ip": [False]}))
    report = _report(tmp_path, sessions)
    assert report["Q1"]["verdict"] == "FAIL" and report["option_a_proposal"] == "NO-GO"


@pytest.mark.parametrize("channel", ["pub1", "priv1"])
def test_an_unavailable_required_ipv6_family_never_validates(tmp_path, channel):
    sessions = complete_campaign()
    session = sessions[channel]
    cases = sorted({r["case"] for r in session.records if r.get("family") == 6 and r.get("target") == "primary"})
    session.drop(_cell(family=6))
    session.records.append(record(channel, session.role, "primary", 6, "*", 0, outcome="family_unavailable",
                                  error="gaierror", cases_not_run=cases))
    report = _report(tmp_path, sessions)
    affected = ("Q1", "Q2", "Q3", "Q4") if channel == "pub1" else ("Q5", "Q6")
    _only_inconclusive(report, *affected)
    assert any("family_unavailable" in m for q in affected for m in report[q]["missing"])


def test_a_partial_public_session_cannot_be_hidden_by_a_complete_one(tmp_path):
    """Une session publique concernée mais incomplète (--only) rend la question INCONCLUSIVE."""
    sessions = complete_campaign()
    from tests.p1q_support import public_session

    partial = public_session("pub_partial", "boot1", questions=("Q2",))
    partial.drop(_cell(case="Q2-08"))
    sessions["pub_partial"] = partial
    _only_inconclusive(_report(tmp_path, sessions), "Q2")


# --------------------------------------------------------------------------
# C2 — intégrité de collecte
# --------------------------------------------------------------------------

def _drop_digest(run_id):
    def mutate(lines):
        return [l for l in lines if not (l.startswith("P1Q-DIGEST ") and f'"run_id":"{run_id}"' in l)]
    return mutate


@pytest.mark.parametrize("run_id", ["pub1", "priv2", "pub_d"])
def test_a_missing_digest_forbids_go(tmp_path, run_id):
    report = _report(tmp_path, mutate_lines=_drop_digest(run_id))
    assert report["collection"]["proven"] is False
    assert report["collection"]["runs"][run_id]["problems"] == ["empreinte absente"]
    assert report["option_a_proposal"] == "INCONCLUSIVE"
    assert "intégrité de collecte non prouvée" in report["blocking"]


def test_a_truncated_line_forbids_go(tmp_path):
    def truncate(lines):
        index = next(i for i, l in enumerate(lines) if '"run_id":"priv1"' in l and l.startswith("P1Q-RESULT "))
        lines[index] = lines[index][: len(lines[index]) // 2]
        return lines

    report = _report(tmp_path, mutate_lines=truncate)
    assert report["collection"]["proven"] is False and report["collection"]["notes"]
    assert report["collection"]["runs"]["priv1"]["complete"] is False
    assert report["option_a_proposal"] == "INCONCLUSIVE"


def test_a_modified_line_with_its_original_digest_forbids_go(tmp_path):
    def alter(lines):
        index = next(i for i, l in enumerate(lines) if '"run_id":"pub2"' in l and '"case":"Q3-02"' in l)
        lines[index] = lines[index].replace('"status":200', '"status":201')
        return lines

    report = _report(tmp_path, mutate_lines=alter)
    assert "empreinte SHA-256 différente" in report["collection"]["runs"]["pub2"]["problems"]
    assert report["option_a_proposal"] == "INCONCLUSIVE"


@pytest.mark.parametrize("extra", [
    'P1Q-RESULT {"p1q":"prober-result","run_id":"lost","case":"Q2-0',                  # ligne tronquée orpheline
    'P1Q-DIGEST {"p1q":"prober-digest","protocol":"P1Q-V0.1-R1","run_id":"lost","results":12,"sha256":"00"}',
])
def test_integrity_alone_forbids_go_even_when_coverage_is_complete(tmp_path, extra):
    """Verrou direct (C2) : la couverture des sessions est complète, mais une
    exécution a perdu des lignes ; la proposition ne peut pas être GO."""
    report = _report(tmp_path, extra_lines=[extra])
    assert {q: report[q]["verdict"] for q in QUESTIONS} == {q: "PASS" for q in QUESTIONS}
    assert report["collection"]["proven"] is False
    assert report["option_a_proposal"] == "INCONCLUSIVE"
    assert report["blocking"] == ["intégrité de collecte non prouvée"]


def test_a_collection_passed_twice_forbids_go(tmp_path):
    report = _report(tmp_path, mutate_lines=lambda lines: lines + lines)
    assert report["collection"]["proven"] is False
    assert report["option_a_proposal"] == "INCONCLUSIVE"


def test_a_run_mixing_two_receiver_boots_is_not_evidence(tmp_path):
    sessions = complete_campaign()
    sessions["pub1"].edit(_cell(family=6, case="Q3-01"), lambda r: r["observation"].update(boot_id="boot-other"))
    report = _report(tmp_path, sessions)
    assert "plusieurs démarrages du récepteur dans une même exécution" in report["collection"]["runs"]["pub1"]["problems"]
    assert report["option_a_proposal"] == "INCONCLUSIVE"


def test_records_of_another_protocol_version_are_not_evidence(tmp_path):
    sessions = complete_campaign()
    sessions["pub2"].edit(_cell(family=4, case="Q2-01"), lambda r: r.update(protocol="P1Q-V0.1"))
    report = _report(tmp_path, sessions)
    assert "protocole de résultat différent" in report["collection"]["runs"]["pub2"]["problems"]
    assert report["option_a_proposal"] == "INCONCLUSIVE"


def test_a_demonstrated_fail_is_not_masked_by_a_broken_collection(tmp_path):
    sessions = complete_campaign()
    sessions["pub1"].edit(_cell(family=4, case="Q2-01"),
                          lambda r: r["observation"].update(
                              x_real_ip={"count": 1, "values": [{"parsed": True, "text": "203.0.113.7",
                                                                 "category": "documentation", "version": 4}]}))
    report = _report(tmp_path, sessions, mutate_lines=_drop_digest("pub1"))
    assert report["Q2"]["verdict"] == "FAIL"
    assert report["option_a_proposal"] == "NO-GO"
    assert "FAIL maintenu" in report["warning"]


# --------------------------------------------------------------------------
# C3 — couverture Q6
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["Q6-01", "Q6-02", "Q6-03"])
def test_a_missing_private_imitation_forbids_pass(tmp_path, case):
    sessions = complete_campaign()
    sessions["priv2"].drop(_cell(family=4, case=case))
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q6")
    assert any(case in m for m in report["Q6"]["missing"])


def test_q5_results_alone_are_not_q6_evidence(tmp_path):
    sessions = complete_campaign()
    for name in ("priv1", "priv2"):
        sessions[name].drop(lambda r: r.get("target") == "primary" and r.get("q") not in ("Q5", "Q0"))
    report = _report(tmp_path, sessions)
    assert report["Q5"]["verdict"] == "PASS"
    _only_inconclusive(report, "Q6")


def test_a_missing_q6p_forbids_pass(tmp_path):
    sessions = complete_campaign()
    sessions["priv1"].drop(lambda r: r.get("target") == "public")
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q6")
    assert any("Q6-P" in m for m in report["Q6"]["missing"])


def test_an_observed_optional_ipv6_q6p_is_still_evaluated(tmp_path):
    sessions = complete_campaign()
    forged = observation(boot="boot1", raw=PUBLIC_PEER, server=PUBLIC_SERVER, xri=["203.0.113.7"], case="Q2-01")
    sessions["priv1"].records.append(record("priv1", "private", "public", 6, "Q2-01", 1, obs=forged))
    report = _report(tmp_path, sessions)
    assert report["Q6"]["verdict"] == "FAIL" and report["option_a_proposal"] == "NO-GO"


def test_a_missing_bypass_attempt_is_not_attempted_and_forbids_pass(tmp_path):
    sessions = complete_campaign()
    sessions["priv1"].drop(lambda r: str(r.get("target")).startswith("extra:"))
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q6")
    assert report["Q6"]["facts"]["strict_boots"]["boot1"]["bypass"] == {"10.250.0.5:8080": "not_attempted"}


@pytest.mark.parametrize("outcome, error_class, expected", [
    ("connect_error", "connect_refused", "refused:connect_refused"),
    ("connect_error", "unreachable", "refused:unreachable"),
    ("connect_error", "timeout", "refused:timeout"),
    ("connect_error", "tls_error", "tls_error"),
    ("connect_error", "other", "other"),
    ("family_unavailable", None, "family_unavailable"),
    ("refused_by_guard", None, "refused_by_guard"),
])
def test_bypass_statuses_distinguish_real_refusal_unavailability_and_absence(tmp_path, outcome, error_class,
                                                                              expected):
    sessions = complete_campaign()
    sessions["priv1"].drop(lambda r: str(r.get("target")).startswith("extra:"))
    literal = {"target_kind": "ip_literal", "target_address": PUBLIC_SERVER[0], "target_port": 8080}
    if outcome == "connect_error":
        sessions["priv1"].records.append(record("priv1", "private", "extra:0", 4, "Q0-ID", 1, outcome=outcome,
                                                error_class=error_class, connect_address=PUBLIC_SERVER[0],
                                                **literal))
    else:
        sessions["priv1"].records.append(record("priv1", "private", "extra:0", 4, "*", 0, outcome=outcome,
                                                cases_not_run=list(p1q_prober.BYPASS_CASES), **literal))
    report = _report(tmp_path, sessions)
    assert report["Q6"]["facts"]["strict_boots"]["boot1"]["bypass"] == {"10.250.0.5:8080": expected}
    if expected.startswith("refused:"):
        assert report["option_a_proposal"] == GO
    else:
        _only_inconclusive(report, "Q6")


def test_a_successful_bypass_with_edge_properties_is_a_fail(tmp_path):
    sessions = complete_campaign()
    reached = observation(boot="boot1", raw=PUBLIC_PEER, server=PUBLIC_SERVER, xri=["203.0.113.7"], case="Q6-01")
    sessions["priv1"].records.append(record("priv1", "private", "extra:0", 4, "Q6-01", 1, obs=reached,
                                            connect_address=PUBLIC_SERVER[0], target_address=PUBLIC_SERVER[0],
                                            target_kind="ip_literal", target_port=8080))
    report = _report(tmp_path, sessions)
    assert report["Q6"]["verdict"] == "FAIL" and report["option_a_proposal"] == "NO-GO"


def test_a_single_strict_boot_is_inconclusive(tmp_path):
    sessions = complete_campaign()
    del sessions["pub2"], sessions["priv2"]
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q6")
    assert any("1/2 démarrage" in m for m in report["Q6"]["missing"])


def test_indistinguishable_private_spoofing_is_a_demonstrated_fail_even_on_one_boot(tmp_path):
    sessions = complete_campaign()
    del sessions["pub2"], sessions["priv2"]

    def same_as_edge(r):
        if r.get("target") == "primary" and r.get("observation"):
            r["observation"]["raw_client"] = dict(r["observation"]["raw_client"], text=PUBLIC_PEER[0],
                                                  category=PUBLIC_PEER[1], version=4)
            r["observation"]["server"] = dict(r["observation"]["server"], text=PUBLIC_SERVER[0],
                                              category=PUBLIC_SERVER[1], version=4)

    sessions["priv1"].edit(lambda r: True, same_as_edge)
    report = _report(tmp_path, sessions)
    assert report["Q6"]["verdict"] == "FAIL" and report["Q6"]["facts"]["candidate_discriminators"] == []
    assert report["option_a_proposal"] == "NO-GO"


# --------------------------------------------------------------------------
# C4 — couverture Q7
# --------------------------------------------------------------------------

@pytest.mark.parametrize("session", ["pub_b", "priv_b", "pub_c", "priv_c", "pub_d", "priv_d"])
def test_each_q7_configuration_and_role_is_required(tmp_path, session):
    sessions = complete_campaign()
    del sessions[session]
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q7")
    assert any("aucune session" in m for m in report["Q7"]["missing"])


def test_configuration_a_is_required_too(tmp_path):
    sessions = complete_campaign()
    for name in ("pub1", "pub2"):
        sessions[name].drop(_cell(case="Q7-03"))
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q7")
    assert any(m.startswith("(a)") for m in report["Q7"]["missing"])


def test_a_missing_q7_family_cell_forbids_pass(tmp_path):
    sessions = complete_campaign()
    sessions["priv_c"].drop(_cell(family=6, case="Q7-02"))
    _only_inconclusive(_report(tmp_path, sessions), "Q7")


@pytest.mark.parametrize("session", ["pub_d", "priv_d"])
def test_a_negative_control_that_detects_nothing_forbids_pass(tmp_path, session):
    sessions = complete_campaign()

    def no_rewrite(r):
        r["observation"].update(effective_equals_raw=True, effective_client=r["observation"]["raw_client"])

    sessions[session].edit(_cell(case="Q7-01"), no_rewrite)
    report = _report(tmp_path, sessions)
    _only_inconclusive(report, "Q7")
    assert any("témoin négatif" in m for m in report["Q7"]["missing"])


def test_a_private_negative_control_must_show_the_forged_client(tmp_path):
    sessions = complete_campaign()
    sessions["priv_d"].edit(_cell(family=4, case="Q7-01"),
                            lambda r: r["observation"]["effective_client"].update(text="10.9.9.9"))
    _only_inconclusive(_report(tmp_path, sessions), "Q7")


def test_a_strict_rewrite_anywhere_is_a_demonstrated_fail(tmp_path):
    sessions = complete_campaign()
    sessions["pub_b"].edit(_cell(family=6, case="Q7-01"),
                           lambda r: r["observation"].update(effective_equals_raw=False))
    del sessions["priv_d"]  # preuve manquante ailleurs : le FAIL reste démontré
    report = _report(tmp_path, sessions)
    assert report["Q7"]["verdict"] == "FAIL" and report["option_a_proposal"] == "NO-GO"


def test_the_report_is_json_serialisable(tmp_path):
    report = _report(tmp_path)
    json.dumps(report, default=p1q_analyze._default)
    assert NOMINAL[4].startswith("g4:")
