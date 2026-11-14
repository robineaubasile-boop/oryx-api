"""P1-Q — analyse des résultats collectés (Q1 à Q7) et proposition de verdicts.

ACT-L1 / F04-D / P1-Q. Bibliothèque standard seulement.

Entrées : fichiers JSONL de la sonde (sortie directe, ou journaux Railway
contenant des lignes « P1Q-RESULT ... » / « P1Q-DIGEST ... », éventuellement
précédées d'un horodatage), et, avec --curl-public, des observations brutes du
récepteur obtenues par curl (variante HTTP/2).

Sortie : rapport JSON (et résumé texte) : faits observés par question,
verdict PROPOSÉ (PASS / FAIL / INCONCLUSIVE) selon les critères du protocole
docs/act_l1_f04d_p1q_protocol_v01.md § 5, intégrité de la collecte.

Le verdict calculé est une PROPOSITION MÉCANIQUE : il ne remplace ni l'audit
indépendant, ni la décision du fondateur, et une observation ponctuelle n'est
jamais une garantie durable de Railway.
"""
import argparse
import hashlib
import json
import sys

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
FORGED_LITERALS = {"203.0.113.7", "198.51.100.23", "192.0.2.44", "2001:db8::7", "2001:db8:abcd::23",
                   "::ffff:203.0.113.7", "10.1.2.3", "127.0.0.1", "::1", "100.64.0.9", "fd00::9", "fe80::1"}
EDGE_HEADERS = ("x-railway-edge", "x-railway-request-id", "x-request-start", "x-forwarded-proto",
                "x-forwarded-host", "x-forwarded-for", "x-real-ip", "forwarded")


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------

def read_lines(paths, curl_public=()):
    results, digests, integrity = [], [], []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                for marker, sink in (("P1Q-RESULT ", "result"), ("P1Q-DIGEST ", "digest")):
                    if marker in line:
                        payload = line.split(marker, 1)[1].strip()
                        break
                else:
                    payload, sink = line.strip(), None
                if not payload.startswith("{"):
                    continue
                try:
                    document = json.loads(payload)
                except ValueError:
                    integrity.append({"file": path, "error": "ligne JSON illisible"})
                    continue
                kind = document.get("p1q")
                if kind == "prober-result":
                    results.append((payload, document))
                elif kind == "prober-digest":
                    digests.append(document)
                elif sink is None and kind not in ("receiver-startup",):
                    integrity.append({"file": path, "error": f"document ignoré ({kind})"})
    for path in curl_public:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        if document.get("p1q") != "receiver-observation":
            integrity.append({"file": path, "error": "observation curl attendue"})
            continue
        results.append((None, {"p1q": "prober-result", "role": "public", "target": "primary", "tls": True,
                               "family": None, "case": document.get("case") or "curl", "q": "curl",
                               "attempt": 1, "outcome": "observed", "observation": document,
                               "run_id": "curl", "client": "curl-http2"}))
    return results, digests, integrity


def verify_digests(results, digests):
    by_run = {}
    for payload, document in results:
        if payload is not None:
            by_run.setdefault(document.get("run_id"), []).append(payload)
    report = []
    for digest in digests:
        lines = by_run.get(digest.get("run_id"), [])
        h = hashlib.sha256()
        for payload in lines:
            h.update(payload.encode("ascii", "replace") + b"\n")
        report.append({"run_id": digest.get("run_id"), "expected": digest.get("results"), "found": len(lines),
                       "complete": len(lines) == digest.get("results") and h.hexdigest() == digest.get("sha256")})
    declared = {d.get("run_id") for d in digests}
    for run_id in sorted(set(by_run) - declared, key=str):
        report.append({"run_id": run_id, "expected": None, "found": len(by_run[run_id]), "complete": False})
    return report


# --------------------------------------------------------------------------
# Accès aux observations
# --------------------------------------------------------------------------

def obs(record):
    return record.get("observation") if record.get("outcome") == "observed" else None


def xri_values(o):
    return (o.get("x_real_ip") or {}).get("values") or []


def xri_count(o):
    return (o.get("x_real_ip") or {}).get("count", 0)


def text_of(described):
    return described.get("text") if isinstance(described, dict) else None


def family_key(record, o):
    raw = o.get("raw_client") or {}
    return (o.get("boot_id"), record.get("family"), raw.get("version"))


def select(records, *, role=None, target=None, q=None, case=None, prefix=None):
    out = []
    for r in records:
        if role and r.get("role") != role:
            continue
        if target and r.get("target") != target:
            continue
        if prefix and not str(r.get("target", "")).startswith(prefix):
            continue
        if q and r.get("q") != q:
            continue
        if case and r.get("case") != case:
            continue
        out.append(r)
    return out


# --------------------------------------------------------------------------
# Référence nominale (Q1) et classement de X-Real-IP
# --------------------------------------------------------------------------

def nominal_reference(public_records):
    """{(boot, famille de connexion) : texte X-Real-IP nominal} depuis Q1-01."""
    ref = {}
    for r in public_records:
        o = obs(r)
        if r.get("case") != "Q1-01" or o is None or xri_count(o) != 1:
            continue
        key = (o.get("boot_id"), r.get("family"))
        ref.setdefault(key, set()).add(text_of(xri_values(o)[0]))
    return {k: next(iter(v)) for k, v in ref.items() if len(v) == 1}


def classify_xri(record, reference):
    o = obs(record)
    if o is None:
        status = record.get("status")
        if record.get("outcome") == "not_observed" and status is not None and 400 <= status < 500:
            return "edge_rejected"
        return "not_observed"
    count = xri_count(o)
    if count == 0:
        return "absent"
    if count > 1:
        return "multiple"
    value = xri_values(o)[0]
    text = text_of(value)
    if text in FORGED_LITERALS or not value.get("parsed"):
        return "client_controlled"
    match = (o.get("self_match") or {}).get("x_real_ip") if (o.get("self_match") or {}).get("valid") else None
    if match == [True]:
        return "nominal"
    if match == [False]:
        return "not_self"
    nominal = reference.get((o.get("boot_id"), record.get("family")))
    if nominal is None:
        return "unreferenced"
    return "nominal" if text == nominal else "different"


def summarize_cases(records, reference):
    cases = {}
    for r in records:
        entry = cases.setdefault(r.get("case"), {"outcomes": {}, "xri": {}, "families": set()})
        entry["families"].add(r.get("family"))
        entry["outcomes"][r.get("outcome")] = entry["outcomes"].get(r.get("outcome"), 0) + 1
        klass = classify_xri(r, reference)
        entry["xri"][klass] = entry["xri"].get(klass, 0) + 1
    for entry in cases.values():
        entry["families"] = sorted(f for f in entry["families"] if f is not None)
    return cases


def header_values(o, name):
    return [v.get("value") for v in (o.get("headers") or {}).get(name, [])]


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

def analyze_q1(records):
    q1 = [r for r in select(records, role="public", target="primary", case="Q1-01")]
    observed = [r for r in q1 if obs(r) is not None]
    facts = {"requests": len(q1), "observed": len(observed), "by_family": {}}
    fail, complete = [], True
    for r in observed:
        o = obs(r)
        fam = facts["by_family"].setdefault(str(r.get("family")), {
            "n": 0, "xri_counts": set(), "xri_texts": set(), "xri_categories": set(), "self_match": set(),
            "raw_client_categories": set(), "server": set(), "edge_headers_present": set(), "xff_counts": set()})
        fam["n"] += 1
        fam["xri_counts"].add(xri_count(o))
        for v in xri_values(o):
            fam["xri_texts"].add(text_of(v))
            fam["xri_categories"].add(v.get("category") if v.get("parsed") else "unparsed")
        sm = o.get("self_match") or {}
        fam["self_match"].add(str(sm.get("x_real_ip")) if sm.get("valid") else "none")
        fam["raw_client_categories"].add((o.get("raw_client") or {}).get("category"))
        fam["server"].add(text_of(o.get("server")))
        fam["xff_counts"].add((o.get("x_forwarded_for") or {}).get("count", 0))
        for name in EDGE_HEADERS:
            if name in (o.get("headers") or {}):
                fam["edge_headers_present"].add(name)
        if xri_count(o) != 1 or not xri_values(o)[0].get("parsed") or xri_values(o)[0].get("category") != "global":
            fail.append(f"{r.get('case')}#{r.get('attempt')}/v{r.get('family')}: X-Real-IP absent, multiple, illisible ou non global")
        if sm.get("valid") and sm.get("x_real_ip") == [False]:
            fail.append(f"{r.get('case')}#{r.get('attempt')}/v{r.get('family')}: X-Real-IP différent de l'adresse engagée")
        if not (sm.get("valid") and sm.get("x_real_ip") == [True]):
            complete = False
    for fam in facts["by_family"].values():
        for key, value in list(fam.items()):
            if isinstance(value, set):
                fam[key] = sorted(map(str, value))
        if len(fam["xri_texts"]) > 1:
            complete = False
    if fail:
        verdict = FAIL
    elif observed and complete and len(observed) >= 5:
        verdict = PASS
    else:
        verdict = INCONCLUSIVE
    return {"verdict": verdict, "facts": facts, "reasons": fail or ([] if verdict == PASS else [
        "PASS exige ≥ 5 observations, X-Real-IP unique, global, stable et égal à l'adresse engagée (--self-ip)"])}


def analyze_header_q(records, q, reference):
    """Q2 / Q3 / Q4 : X-Real-IP reçu par l'application après l'edge public."""
    selected = select(records, role="public", target="primary", q=q)
    cases = summarize_cases(selected, reference)
    bad = {"multiple", "client_controlled", "not_self", "different", "absent"}
    reasons, unresolved = [], False
    for case_id, entry in sorted(cases.items()):
        for klass, n in entry["xri"].items():
            if klass in bad:
                reasons.append(f"{case_id}: X-Real-IP {klass} ({n})")
            elif klass in ("unreferenced", "not_observed"):
                unresolved = True
    extra = {}
    for r in selected:
        o = obs(r)
        if o is None:
            continue
        e = extra.setdefault(r.get("case"), {"xff": set(), "forwarded": set(), "x_forwarded_host": set(),
                                             "x_forwarded_proto": set(), "x_railway_edge": set(),
                                             "x_railway_request_id_forged_kept": set(), "client_ip_headers": set()})
        e["xff"].update(header_values(o, "x-forwarded-for"))
        e["forwarded"].update(header_values(o, "forwarded"))
        e["x_forwarded_host"].update(header_values(o, "x-forwarded-host"))
        e["x_forwarded_proto"].update(header_values(o, "x-forwarded-proto"))
        e["x_railway_edge"].update(header_values(o, "x-railway-edge"))
        e["x_railway_request_id_forged_kept"].add(
            any("p1q-forged" in str(v) for v in header_values(o, "x-railway-request-id")))
        for name in ("true-client-ip", "cf-connecting-ip", "x-client-ip", "x-original-forwarded-for",
                     "x-envoy-external-address", "x-cluster-client-ip"):
            if name in (o.get("headers") or {}):
                e["client_ip_headers"].add(name)
    for e in extra.values():
        for key in e:
            e[key] = sorted(map(str, e[key]))
    if reasons:
        verdict = FAIL
    elif cases and not unresolved and any("nominal" in e["xri"] for e in cases.values()):
        verdict = PASS
    else:
        verdict = INCONCLUSIVE
    return {"verdict": verdict, "cases": {k: {**v} for k, v in cases.items()}, "headers_received": extra,
            "reasons": reasons or ([] if verdict == PASS else [
                "aucune référence nominale (Q1 / --self-ip) ou cas non observés"])}


def _side_properties(records):
    props = {"raw_categories": set(), "raw_texts": set(), "server_texts": set(), "server_versions": set(),
             "boots": set(), "n": 0}
    for r in records:
        o = obs(r)
        if o is None:
            continue
        props["n"] += 1
        raw, server = o.get("raw_client") or {}, o.get("server") or {}
        props["raw_categories"].add(str(raw.get("category")))
        props["raw_texts"].add(str(raw.get("text")))
        props["server_texts"].add(str(server.get("text")))
        props["server_versions"].add(str(server.get("version")))
        props["boots"].add(o.get("boot_id"))
    return props


def _self_private_addresses(records):
    found = set()
    for r in records:
        o = obs(r)
        if o is None:
            continue
        resolution = ((o.get("inventory") or {}).get("private_domain_resolution") or {})
        for values in resolution.values():
            if isinstance(values, list):
                found.update(str(v.get("text")) for v in values if isinstance(v, dict))
    return found


def analyze_q5(records):
    q5 = select(records, role="private", target="primary", case="Q5-01")
    observed = [r for r in q5 if obs(r) is not None]
    with_xri = [r for r in observed if xri_count(obs(r)) > 0]
    edge_like = [r for r in observed if any(h in (obs(r).get("headers") or {})
                                            for h in ("x-railway-edge", "x-railway-request-id"))]
    props = _side_properties(observed)
    facts = {"requests": len(q5), "observed": len(observed), "with_x_real_ip": len(with_xri),
             "with_railway_edge_headers": len(edge_like),
             "families": sorted({r.get("family") for r in observed}),
             "unreachable": sorted({f"v{r.get('family')}:{r.get('outcome')}" for r in q5 if obs(r) is None}),
             **{k: sorted(map(str, v)) if isinstance(v, set) else v for k, v in props.items()}}
    if observed and not with_xri and not edge_like:
        return {"verdict": PASS, "facts": facts, "reasons": []}
    return {"verdict": INCONCLUSIVE, "facts": facts,
            "reasons": ["service A injoignable en privé, ou en-têtes edge présents sur le trafic privé"]}


def analyze_q6(records):
    public = [r for r in select(records, role="public", target="primary") if obs(r) is not None]
    private = [r for r in select(records, role="private", target="primary") if obs(r) is not None]
    extras = select(records, role="private", prefix="extra:")
    via_edge = select(records, role="private", target="public")
    spoof_reaches = [r for r in private if any(text_of(v) in FORGED_LITERALS for v in xri_values(obs(r)))]
    pub, priv = _side_properties(public), _side_properties(private)
    self_private = _self_private_addresses(public + private)
    discriminators = {
        "D1_raw_client_category": {"public": sorted(pub["raw_categories"]), "private": sorted(priv["raw_categories"]),
                                   "separates": bool(pub["n"] and priv["n"])
                                   and not (pub["raw_categories"] & priv["raw_categories"])},
        "D2_server_address": {"public": sorted(pub["server_texts"]), "private": sorted(priv["server_texts"]),
                              "separates": bool(pub["n"] and priv["n"])
                              and not (pub["server_texts"] & priv["server_texts"]),
                              "self_discovered_private_addresses": sorted(self_private),
                              "rule_self_discovered": bool(self_private) and bool(pub["n"] and priv["n"])
                              and priv["server_texts"] <= self_private and not (pub["server_texts"] & self_private)},
        "D3_listener_family": {"public": sorted(pub["server_versions"]), "private": sorted(priv["server_versions"]),
                               "separates": bool(pub["n"] and priv["n"])
                               and not (pub["server_versions"] & priv["server_versions"])},
        "D4_raw_client_exact_NOT_A_RULE": {"separates": bool(pub["n"] and priv["n"])
                                           and not (pub["raw_texts"] & priv["raw_texts"]),
                                           "note": "valeurs exactes : non généralisable sans plage CIDR (interdit)"},
    }
    candidates = [name for name in ("D1_raw_client_category", "D2_server_address", "D3_listener_family")
                  if discriminators[name]["separates"]]

    def looks_public(o):
        raw, server = o.get("raw_client") or {}, o.get("server") or {}
        return any([
            "D1_raw_client_category" in candidates and str(raw.get("category")) in pub["raw_categories"],
            "D2_server_address" in candidates and str(server.get("text")) in pub["server_texts"],
            "D3_listener_family" in candidates and str(server.get("version")) in pub["server_versions"],
        ])

    bypass = [r for r in extras if obs(r) is not None and candidates and looks_public(obs(r))]
    edge_forged_kept = [r for r in via_edge if any(text_of(v) in FORGED_LITERALS for v in xri_values(obs(r) or {}))]
    facts = {
        "public_observations": pub["n"], "private_observations": priv["n"],
        "public_boots": len(pub["boots"]), "private_boots": len(priv["boots"]),
        "spoofed_x_real_ip_reaches_application": len(spoof_reaches),
        "discriminators": discriminators, "candidate_discriminators": candidates,
        "bypass_attempts": len(extras), "bypass_observed": len([r for r in extras if obs(r) is not None]),
        "bypass_matching_public_properties": len(bypass),
        "internal_via_edge_requests": len(via_edge), "internal_via_edge_forged_kept": len(edge_forged_kept),
    }
    reasons = []
    if not priv["n"] or not pub["n"]:
        return {"verdict": INCONCLUSIVE, "facts": facts, "reasons": ["observations publiques ou privées absentes"]}
    if edge_forged_kept:
        reasons.append("l'edge conserve un X-Real-IP forgé pour un appelant interne passant par le domaine public")
    if bypass:
        reasons.append("une cible de contournement présente les propriétés du trafic edge")
    if spoof_reaches and not candidates:
        reasons.append("X-Real-IP forgé reçu en privé, et aucune propriété non contrôlable par l'appelant "
                       "ne distingue le trafic privé du trafic edge")
    if reasons:
        return {"verdict": FAIL, "facts": facts, "reasons": reasons}
    if candidates and len(pub["boots"]) >= 2 and len(priv["boots"]) >= 2 and extras:
        return {"verdict": PASS, "facts": facts, "reasons": [
            "discriminateur candidat stable sur ≥ 2 démarrages, aucun contournement observé — comportement "
            "OBSERVÉ, non garanti par Railway"]}
    return {"verdict": INCONCLUSIVE, "facts": facts, "reasons": [
        "discriminateur candidat non confirmé : exiger ≥ 2 démarrages de chaque côté et des essais de "
        "contournement (P1Q_EXTRA_TARGETS)"]}


def analyze_q7(records):
    observed = [r for r in records if obs(r) is not None]
    modes = {}
    for r in observed:
        o = obs(r)
        settings = o.get("uvicorn") or {}
        key = f"{o.get('mode')}|env={settings.get('forwarded_allow_ips_env')}"
        m = modes.setdefault(key, {"mode": o.get("mode"), "n": 0, "rewritten": 0, "proxy_headers": set(),
                                   "forwarded_allow_ips": set(), "raw_categories": set(),
                                   "sim_default_rewrites": 0, "sim_all_rewrites": 0, "roles": set()})
        m["n"] += 1
        m["roles"].add(r.get("role"))
        m["rewritten"] += 0 if o.get("effective_equals_raw") else 1
        m["proxy_headers"].add(str(settings.get("proxy_headers")))
        m["forwarded_allow_ips"].add(str(settings.get("forwarded_allow_ips")))
        m["raw_categories"].add(str((o.get("raw_client") or {}).get("category")))
        raw_text = text_of(o.get("raw_client"))
        sim = o.get("simulation") or {}
        if text_of((sim.get("uvicorn_trust_127_0_0_1") or {}).get("client")) != raw_text:
            m["sim_default_rewrites"] += 1
        if text_of((sim.get("uvicorn_trust_all") or {}).get("client")) != raw_text:
            m["sim_all_rewrites"] += 1
    for m in modes.values():
        for key in ("proxy_headers", "forwarded_allow_ips", "raw_categories", "roles"):
            m[key] = sorted(m[key])
    strict = [m for m in modes.values() if m["mode"] == "strict"]
    negative = [m for m in modes.values() if m["mode"] == "trust_all_negative_control"]
    reasons = [f"mode strict : scope[client] réécrit ({m['rewritten']})" for m in strict if m["rewritten"]]
    if reasons:
        return {"verdict": FAIL, "modes": modes, "reasons": reasons}
    strict_with_env = [m for k, m in modes.items() if m["mode"] == "strict" and not k.endswith("env=None")]
    if strict and strict_with_env and negative and all(m["rewritten"] for m in negative):
        return {"verdict": PASS, "modes": modes, "reasons": []}
    return {"verdict": INCONCLUSIVE, "modes": modes, "reasons": [
        "PASS exige : mode strict sans réécriture, AVEC FORWARDED_ALLOW_IPS défini, et témoin négatif "
        "trust_all_negative_control montrant la réécriture"]}


def analyze(records):
    public = select(records, role="public", target="primary")
    reference = nominal_reference(public)
    report = {
        "Q1": analyze_q1(records),
        "Q2": analyze_header_q(records, "Q2", reference),
        "Q3": analyze_header_q(records, "Q3", reference),
        "Q4": analyze_header_q(records, "Q4", reference),
        "Q5": analyze_q5(records),
        "Q6": analyze_q6(records),
        "Q7": analyze_q7(records),
        "Q8": {"verdict": "DEFINED", "reasons": ["matrice normative du protocole § 7 ; non expérimentale"]},
    }
    verdicts = [report[q]["verdict"] for q in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7")]
    if FAIL in verdicts:
        proposal = "NO-GO"
    elif all(v == PASS for v in verdicts):
        proposal = "GO-CANDIDATE (soumis à l'audit indépendant et à la décision du fondateur)"
    else:
        proposal = "INCONCLUSIVE"
    report["option_a_proposal"] = proposal
    report["warning"] = ("Proposition mécanique non décisionnelle ; comportement observé, non garanti par Railway ; "
                         "P1 reste ouverte jusqu'au freeze du fondateur.")
    return report


def _default(value):
    if isinstance(value, set):
        return sorted(map(str, value))
    raise TypeError(type(value).__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analyse P1-Q")
    parser.add_argument("files", nargs="*", help="JSONL sonde / journaux Railway")
    parser.add_argument("--curl-public", action="append", default=[], help="observation JSON obtenue par curl")
    parser.add_argument("--out", help="rapport JSON")
    args = parser.parse_args(argv)
    results, digests, integrity = read_lines(args.files, args.curl_public)
    records = [document for _, document in results]
    report = analyze(records)
    report["collection"] = {"results": len(records), "digests": verify_digests(results, digests),
                            "integrity_notes": integrity}
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=_default)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8"):
        print(f"{q}: {report[q]['verdict']}" + (f" — {'; '.join(report[q]['reasons'])}" if report[q]["reasons"] else ""))
    print(f"Option A (proposition) : {report['option_a_proposal']}")
    for d in report["collection"]["digests"]:
        print(f"collecte {d['run_id']}: {d['found']}/{d['expected']} {'complète' if d['complete'] else 'INCOMPLÈTE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
