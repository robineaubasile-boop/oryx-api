"""P1-Q — analyse CONSERVATRICE des résultats collectés (Q1 à Q7).

ACT-L1 / F04-D / P1-Q. Bibliothèque standard et catalogue de la sonde seulement.

Entrées : fichiers JSONL de la sonde (sortie directe, ou journaux Railway
contenant des lignes « P1Q-RESULT ... » / « P1Q-DIGEST ... », éventuellement
précédées d'un horodatage), et, avec --curl-public, des observations brutes du
récepteur obtenues par curl (variante HTTP/2).

Principes (protocole docs/act_l1_f04d_p1q_protocol_v01.md § 5, correctifs
C1-C4 de l'audit) :

- COUVERTURE EXACTE : chaque question exige, pour chaque session concernée,
  chaque cas du catalogue (p1q_prober.CASES), chaque famille IP requise et
  chaque tentative prévue. Une cellule absente ou inexploitable rend la
  question INCONCLUSIVE, jamais PASS ;
- FAIL DÉMONTRÉ : un contre-exemple observé (valeur forgée reçue, réécriture
  en mode strict, contournement réussi...) rend la question FAIL, quelle que
  soit la couverture ailleurs ; il n'est jamais masqué ;
- INTÉGRITÉ (C2) : une exécution sans empreinte, à empreinte fausse, tronquée,
  dupliquée ou hétérogène (plusieurs démarrages du récepteur) ne compte pas
  dans la couverture ; toute anomalie de collecte interdit un GO-CANDIDATE ;
- une famille IP requise indisponible n'est jamais une validation.

Le verdict calculé est une PROPOSITION MÉCANIQUE : il ne remplace ni l'audit
indépendant, ni la décision du fondateur, et une observation ponctuelle n'est
jamais une garantie durable de Railway.
"""
import argparse
import hashlib
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p1q_prober import CASES, FORGED_VALUES, F4A, PROTOCOL_VERSION, Q6P_CASES, cases_for  # noqa: E402

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
QUESTIONS = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7")
FORGED_LITERALS = frozenset(FORGED_VALUES)

# Familles IP REQUISES par canal. Q6-P : IPv4 seule requise ; la sortie IPv6 des
# services Railway est une option à activer (CLI « railway outbound-network ipv6
# enable ») ; une observation IPv6 Q6-P éventuelle est évaluée (FAIL possible),
# jamais exigée pour un PASS.
REQUIRED_FAMILIES = {"public": (4, 6), "private": (4, 6), "q6p": (4,)}
REQUIRED_STRICT_BOOTS = 2
# Q7 : (mode, valeur de FORWARDED_ALLOW_IPS telle que rapportée ; None = absente).
Q7_CONFIGS = {"a": ("strict", None), "b": ("strict", "*"), "c": ("uvicorn_defaults", None),
              "d": ("trust_all_negative_control", None)}
# Refus explicites de l'edge (réponse NON marquée x-p1q-receiver) admis comme résultat valide.
EDGE_REFUSAL_STATUSES = frozenset({400, 413, 414, 431})
REFUSED_CLASSES = frozenset({"connect_refused", "unreachable", "timeout"})
EDGE_HEADERS = ("x-railway-edge", "x-railway-request-id", "x-request-start", "x-forwarded-proto",
                "x-forwarded-host", "x-forwarded-for", "x-real-ip", "forwarded")
MAX_LISTED = 40

CATALOGUE = {c.id: c for c in CASES}
PUBLIC_CASES = {q: [c for c in cases_for("public") if c.q == q] for q in QUESTIONS}
PRIVATE_CASES = cases_for("private")


# --------------------------------------------------------------------------
# Lecture et intégrité (C2)
# --------------------------------------------------------------------------

def read_lines(paths, curl_public=()):
    """Rend (résultats [(ligne, document)], empreintes, anomalies). Toute ligne
    marquée mais illisible est une anomalie (troncature probable)."""
    results, digests, notes = [], [], []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                line = line.rstrip("\n")
                marker = next((m for m in ("P1Q-RESULT ", "P1Q-DIGEST ") if m in line), None)
                payload = line.split(marker, 1)[1].strip() if marker else line.strip()
                if not payload.startswith("{"):
                    if marker:
                        notes.append({"file": path, "line": number, "error": "ligne marquée sans JSON"})
                    continue
                try:
                    document = json.loads(payload)
                except ValueError:
                    notes.append({"file": path, "line": number, "error": "ligne JSON illisible (troncature ?)"})
                    continue
                kind = document.get("p1q") if isinstance(document, dict) else None
                if kind == "prober-result":
                    results.append((payload, document))
                elif kind == "prober-digest":
                    digests.append(document)
                elif kind != "receiver-startup":
                    notes.append({"file": path, "line": number, "error": f"document inattendu ({kind})"})
    curl = []
    for path in curl_public:
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            notes.append({"file": path, "error": "observation curl illisible"})
            continue
        if not isinstance(document, dict) or document.get("p1q") != "receiver-observation":
            notes.append({"file": path, "error": "observation curl attendue"})
            continue
        curl.append({"p1q": "prober-result", "role": "public", "target": "primary", "tls": True, "family": None,
                     "case": document.get("case") or "curl", "q": "curl", "attempt": 1, "outcome": "observed",
                     "observation": document, "run_id": "curl", "client": "curl"})
    return results, digests, notes, curl


def verify_collection(results, digests, notes):
    """Intégrité par exécution : une empreinte unique, exacte, même protocole,
    aucune ligne dupliquée, un seul démarrage du récepteur."""
    by_run, keys, runs = {}, {}, {}
    for payload, document in results:
        run_id = document.get("run_id")
        by_run.setdefault(run_id, []).append(payload)
        key = (document.get("target"), document.get("family"), document.get("case"), document.get("attempt"))
        keys.setdefault(run_id, []).append(key)
    digests_by_run = {}
    for digest in digests:
        digests_by_run.setdefault(digest.get("run_id"), []).append(digest)
    for run_id in sorted(set(by_run) | set(digests_by_run), key=str):
        lines = by_run.get(run_id, [])
        problems = []
        found = digests_by_run.get(run_id, [])
        if not found:
            problems.append("empreinte absente")
        elif len(found) > 1:
            problems.append("empreintes multiples")
        else:
            digest = found[0]
            h = hashlib.sha256()
            for payload in lines:
                h.update(payload.encode("ascii", "replace") + b"\n")
            if digest.get("results") != len(lines):
                problems.append(f"nombre de lignes {len(lines)} ≠ {digest.get('results')} annoncées")
            if digest.get("sha256") != h.hexdigest():
                problems.append("empreinte SHA-256 différente")
            if digest.get("protocol") != PROTOCOL_VERSION:
                problems.append("protocole de l'empreinte différent")
        if len(set(keys.get(run_id, []))) != len(keys.get(run_id, [])):
            problems.append("lignes dupliquées")
        docs = [d for _, d in results if d.get("run_id") == run_id]
        if any(d.get("protocol") != PROTOCOL_VERSION for d in docs):
            problems.append("protocole de résultat différent")
        boots = {(d.get("observation") or {}).get("boot_id") for d in docs if d.get("outcome") == "observed"}
        if len(boots) > 1:
            problems.append("plusieurs démarrages du récepteur dans une même exécution")
        runs[run_id] = {"lines": len(lines), "complete": not problems, "problems": problems}
    proven = bool(runs) and all(r["complete"] for r in runs.values()) and not notes
    return {"runs": runs, "notes": notes, "proven": proven}


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


def header_values(o, name):
    return [v.get("value") for v in (o.get("headers") or {}).get(name, [])]


def config_of(o):
    settings = o.get("uvicorn") or {}
    return (o.get("mode"), settings.get("forwarded_allow_ips_env"))


class RunView:
    """Une exécution de la sonde = une session."""

    def __init__(self, run_id, records, intact):
        self.run_id = run_id
        self.records = records
        self.intact = intact
        self.role = records[0].get("role") if records else None
        observed = [obs(r) for r in records if obs(r) is not None]
        self.boots = {o.get("boot_id") for o in observed}
        self.configs = {config_of(o) for o in observed}
        self.boot = next(iter(self.boots)) if len(self.boots) == 1 else None
        self.config = next(iter(self.configs)) if len(self.configs) == 1 else None
        self.usable = intact and self.boot is not None and self.config is not None
        self.index = {}
        self.skips = {}
        for r in records:
            if r.get("case") == "*":
                for case_id in r.get("cases_not_run") or ():
                    self.skips[(r.get("target"), r.get("family"), case_id)] = r.get("outcome")
            else:
                self.index.setdefault((r.get("target"), r.get("family"), r.get("case"), r.get("attempt")), r)

    def cell(self, target, family, case_id, attempt):
        """Enregistrement de la cellule, ou statut explicite de son absence."""
        record = self.index.get((target, family, case_id, attempt))
        if record is not None:
            return record, None
        return None, self.skips.get((target, family, case_id), "not_run")

    def has_q(self, q, target="primary"):
        return any(r.get("q") == q and r.get("target") == target for r in self.records)


def build_runs(results, collection):
    grouped = {}
    for _, document in results:
        grouped.setdefault(document.get("run_id"), []).append(document)
    return [RunView(run_id, records, collection["runs"].get(run_id, {}).get("complete", False))
            for run_id, records in sorted(grouped.items(), key=lambda item: str(item[0]))]


def _missing(bucket, text):
    if len(bucket) < MAX_LISTED:
        bucket.append(text)
    elif len(bucket) == MAX_LISTED:
        bucket.append("…")


# --------------------------------------------------------------------------
# Classement de X-Real-IP (Q1-Q4)
# --------------------------------------------------------------------------

def nominal_reference(runs):
    """{(démarrage, famille) : X-Real-IP nominal} depuis Q1-01 publics, s'il est unique."""
    ref = {}
    for run in runs:
        if run.role != "public":
            continue
        for r in run.records:
            o = obs(r)
            if r.get("case") != "Q1-01" or r.get("target") != "primary" or o is None or xri_count(o) != 1:
                continue
            ref.setdefault((o.get("boot_id"), r.get("family")), set()).add(text_of(xri_values(o)[0]))
    return {k: next(iter(v)) for k, v in ref.items() if len(v) == 1}


FAIL_CLASSES = frozenset({"multiple", "client_controlled", "not_self", "different", "absent"})
GOOD_CLASSES = frozenset({"nominal", "edge_rejected"})


def classify_xri(record, reference):
    """nominal | edge_rejected (résultat valide) | classes FAIL | classes inexploitables."""
    o = obs(record)
    if o is None:
        status = record.get("status")
        if (record.get("outcome") == "not_observed" and status in EDGE_REFUSAL_STATUSES
                and record.get("receiver_header") is False):
            return "edge_rejected"
        return record.get("error_class") or record.get("outcome") or "not_run"
    count = xri_count(o)
    if count == 0:
        return "absent"
    if count > 1:
        return "multiple"
    value = xri_values(o)[0]
    text = text_of(value)
    if text in FORGED_LITERALS or not value.get("parsed"):
        return "client_controlled"
    sm = o.get("self_match") or {}
    committed = value.get("version") in (record.get("self_committed_versions") or ())
    if sm.get("valid") and committed:
        return "nominal" if sm.get("x_real_ip") == [True] else "not_self"
    nominal = reference.get((o.get("boot_id"), record.get("family")))
    if nominal is None:
        return "uncommitted" if not committed else "unreferenced"
    return "nominal" if text == nominal else "different"


# --------------------------------------------------------------------------
# Q1 — public nominal
# --------------------------------------------------------------------------

def q1_session(run):
    """(complet, échecs, manquants, faits) pour une session publique."""
    fails, missing, facts = [], [], {}
    case = CATALOGUE["Q1-01"]
    for family in REQUIRED_FAMILIES["public"]:
        texts, fam = set(), {"observed": 0, "raw_client_categories": set(), "server": set(),
                             "edge_headers_present": set(), "xff_counts": set()}
        for attempt in range(1, case.repeat + 1):
            record, absent = run.cell("primary", family, case.id, attempt)
            label = f"{run.run_id}/v{family}/Q1-01#{attempt}"
            o = obs(record) if record else None
            if o is None:
                _missing(missing, f"{label}: {absent or classify_xri(record, {})}")
                continue
            fam["observed"] += 1
            fam["raw_client_categories"].add((o.get("raw_client") or {}).get("category"))
            fam["server"].add(text_of(o.get("server")))
            fam["xff_counts"].add((o.get("x_forwarded_for") or {}).get("count", 0))
            fam["edge_headers_present"].update(n for n in EDGE_HEADERS if n in (o.get("headers") or {}))
            values = xri_values(o)
            if xri_count(o) != 1 or not values[0].get("parsed") or values[0].get("category") != "global":
                fails.append(f"{label}: X-Real-IP absent, multiple, illisible ou non global")
                continue
            texts.add(text_of(values[0]))
            sm = o.get("self_match") or {}
            committed = values[0].get("version") in (record.get("self_committed_versions") or ())
            if not (sm.get("valid") and committed):
                _missing(missing, f"{label}: adresse non engagée (--self-ip de cette famille)")
            elif sm.get("x_real_ip") != [True]:
                fails.append(f"{label}: X-Real-IP différent de l'adresse engagée")
        if len(texts) > 1:
            _missing(missing, f"{run.run_id}/v{family}: X-Real-IP instable ({len(texts)} valeurs)")
        facts[f"v{family}"] = {k: sorted(map(str, v)) if isinstance(v, set) else v for k, v in fam.items()}
    return not fails and not missing, fails, missing, facts


def analyze_q1(runs):
    concerned = [r for r in runs if r.role == "public" and r.has_q("Q1")]
    fails, missing, complete, facts = [], [], 0, {}
    for run in concerned:
        ok, f, m, fa = q1_session(run)
        fails += f
        facts[run.run_id] = fa
        if not run.usable:
            _missing(missing, f"{run.run_id}: exécution non intègre, hétérogène ou sans observation")
        elif ok:
            complete += 1
        else:
            missing += m
    return _verdict(fails, missing, complete, concerned, "Q1",
                    {"sessions": len(concerned), "complete_sessions": complete, "by_session": facts})


def _verdict(fails, missing, complete, concerned, q, facts):
    if fails:
        return {"verdict": FAIL, "reasons": fails[:MAX_LISTED], "missing": missing, "facts": facts}
    if not concerned:
        return {"verdict": INCONCLUSIVE, "reasons": [f"{q} : aucune session"], "missing": [f"{q} : aucune session"],
                "facts": facts}
    if missing or complete == 0 or complete != len(concerned):
        return {"verdict": INCONCLUSIVE, "reasons": [f"{q} : couverture incomplète ({complete}/{len(concerned)} "
                                                     "session(s) complète(s))"], "missing": missing, "facts": facts}
    return {"verdict": PASS, "reasons": [], "missing": [], "facts": facts}


# --------------------------------------------------------------------------
# Q2 / Q3 / Q4 — falsification et ambiguïté via l'edge public (C1)
# --------------------------------------------------------------------------

def header_session(run, q, reference):
    fails, missing, nominal, cells = [], [], 0, {}
    for case in PUBLIC_CASES[q]:
        for family in REQUIRED_FAMILIES["public"]:
            for attempt in range(1, case.repeat + 1):
                record, absent = run.cell("primary", family, case.id, attempt)
                klass = absent if record is None else classify_xri(record, reference)
                cells.setdefault(case.id, {}).setdefault(klass, 0)
                cells[case.id][klass] += 1
                label = f"{run.run_id}/v{family}/{case.id}#{attempt}"
                if klass in FAIL_CLASSES:
                    fails.append(f"{label}: X-Real-IP {klass}")
                elif klass not in GOOD_CLASSES:
                    _missing(missing, f"{label}: {klass}")
                nominal += klass == "nominal"
    if nominal == 0 and not fails:
        _missing(missing, f"{run.run_id}/{q}: aucun X-Real-IP nominal observé")
    return not fails and not missing, fails, missing, cells


def headers_received(run, q):
    extra = {}
    for r in run.records:
        o = obs(r)
        if o is None or r.get("q") != q or r.get("target") != "primary":
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
        e["client_ip_headers"].update(n for n in ("true-client-ip", "cf-connecting-ip", "x-client-ip",
                                                  "x-original-forwarded-for", "x-envoy-external-address",
                                                  "x-cluster-client-ip") if n in (o.get("headers") or {}))
    return {case: {k: sorted(map(str, v)) for k, v in e.items()} for case, e in extra.items()}


def analyze_header_q(runs, q, reference, curl=()):
    concerned = [r for r in runs if r.role == "public" and r.has_q(q)]
    fails, missing, complete, facts = [], [], 0, {}
    for run in concerned:
        ok, f, m, cells = header_session(run, q, reference)
        fails += f
        facts[run.run_id] = {"cells": cells, "headers_received": headers_received(run, q)}
        if not run.usable:
            _missing(missing, f"{run.run_id}: exécution non intègre, hétérogène ou sans observation")
        elif ok:
            complete += 1
        else:
            missing += m
    # Observations curl : preuve de FAIL seulement, jamais de couverture.
    for r in curl:
        if str(r.get("case", "")).endswith(q) and classify_xri(r, {}) in ("multiple", "client_controlled"):
            fails.append(f"curl/{r.get('case')}: X-Real-IP {classify_xri(r, {})}")
    return _verdict(fails, missing, complete, concerned, q,
                    {"sessions": len(concerned), "complete_sessions": complete, "by_session": facts})


# --------------------------------------------------------------------------
# Q5 — privé nominal
# --------------------------------------------------------------------------

def private_runs(runs):
    return [r for r in runs if r.role == "private"]


def q5_session(run):
    missing, notes = [], []
    case = CATALOGUE["Q5-01"]
    for family in REQUIRED_FAMILIES["private"]:
        for attempt in range(1, case.repeat + 1):
            record, absent = run.cell("primary", family, case.id, attempt)
            o = obs(record) if record else None
            label = f"{run.run_id}/v{family}/Q5-01#{attempt}"
            if o is None:
                _missing(missing, f"{label}: {absent if record is None else classify_xri(record, {})}")
            elif xri_count(o) or any(h in (o.get("headers") or {}) for h in ("x-railway-edge", "x-railway-request-id")):
                _missing(missing, f"{label}: en-têtes edge présents sur le trafic privé")
    return not missing, missing


def analyze_q5(runs):
    concerned = [r for r in private_runs(runs) if r.has_q("Q5")]
    missing, complete = [], 0
    for run in concerned:
        ok, m = q5_session(run)
        if not run.usable:
            _missing(missing, f"{run.run_id}: exécution non intègre, hétérogène ou sans observation")
        elif ok:
            complete += 1
        else:
            missing += m
    return _verdict([], missing, complete, concerned, "Q5", {"sessions": len(concerned), "complete_sessions": complete})


# --------------------------------------------------------------------------
# Q6 — usurpation privée (C3)
# --------------------------------------------------------------------------

def _props(observations):
    props = {"raw_categories": set(), "raw_texts": set(), "server_texts": set(), "server_versions": set(), "n": 0}
    for o in observations:
        raw, server = o.get("raw_client") or {}, o.get("server") or {}
        props["n"] += 1
        props["raw_categories"].add(str(raw.get("category")))
        props["raw_texts"].add(str(raw.get("text")))
        props["server_texts"].add(str(server.get("text")))
        props["server_versions"].add(str(server.get("version")))
    return props


def _discriminators(pub, priv, self_private):
    both = bool(pub["n"] and priv["n"])
    return {
        "D1_raw_client_category": {"public": sorted(pub["raw_categories"]), "private": sorted(priv["raw_categories"]),
                                   "separates": both and not (pub["raw_categories"] & priv["raw_categories"])},
        "D2_server_address": {"public": sorted(pub["server_texts"]), "private": sorted(priv["server_texts"]),
                              "separates": both and not (pub["server_texts"] & priv["server_texts"]),
                              "self_discovered_private_addresses": sorted(self_private),
                              "rule_self_discovered": bool(self_private) and both
                              and priv["server_texts"] <= self_private and not (pub["server_texts"] & self_private)},
        "D3_listener_family": {"public": sorted(pub["server_versions"]), "private": sorted(priv["server_versions"]),
                               "separates": both and not (pub["server_versions"] & priv["server_versions"])},
        "D4_raw_client_exact_NOT_A_RULE": {"separates": both and not (pub["raw_texts"] & priv["raw_texts"]),
                                           "note": "valeurs exactes : non généralisable sans plage CIDR (interdit)"},
    }


CANDIDATE_NAMES = ("D1_raw_client_category", "D2_server_address", "D3_listener_family")


def _looks_public(o, candidates, pub):
    raw, server = o.get("raw_client") or {}, o.get("server") or {}
    return any([
        "D1_raw_client_category" in candidates and str(raw.get("category")) in pub["raw_categories"],
        "D2_server_address" in candidates and str(server.get("text")) in pub["server_texts"],
        "D3_listener_family" in candidates and str(server.get("version")) in pub["server_versions"],
    ])


def _self_private(observations):
    found = set()
    for o in observations:
        for values in ((o.get("inventory") or {}).get("private_domain_resolution") or {}).values():
            if isinstance(values, list):
                found.update(str(v.get("text")) for v in values if isinstance(v, dict))
    return found


def private_matrix_missing(run):
    """Cellules manquantes de la matrice privée (Q2-Q7 privés, Q5, Q6-01..03)."""
    missing = []
    for case in PRIVATE_CASES:
        for family in REQUIRED_FAMILIES["private"]:
            for attempt in range(1, case.repeat + 1):
                record, absent = run.cell("primary", family, case.id, attempt)
                if record is None or obs(record) is None:
                    status = absent if record is None else (record.get("error_class") or record.get("outcome"))
                    _missing(missing, f"{run.run_id}/v{family}/{case.id}#{attempt}: {status}")
    return missing


def q6p_status(run):
    """(manquants, échecs) de Q6-P : IPv4 requise ; IPv6 évaluée si observée."""
    missing, fails = [], []
    for case_id in Q6P_CASES:
        case = CATALOGUE[case_id]
        for family in (4, 6):
            for attempt in range(1, case.repeat + 1):
                record, absent = run.cell("public", family, case_id, attempt)
                o = obs(record) if record else None
                label = f"{run.run_id}/Q6-P/v{family}/{case_id}#{attempt}"
                if o is None:
                    if family in REQUIRED_FAMILIES["q6p"]:
                        status = absent if record is None else (record.get("error_class") or record.get("outcome"))
                        _missing(missing, f"{label}: {status}")
                    continue
                values = xri_values(o)
                if xri_count(o) != 1 or not values[0].get("parsed") or text_of(values[0]) in FORGED_LITERALS:
                    fails.append(f"{label}: X-Real-IP forgé, multiple ou illisible pour un appelant interne via l'edge")
    return missing, fails


def bypass_status(run, required):
    """Pour chaque adresse locale de A atteinte côté edge : tentative privée
    exploitable (observée ou réellement refusée), ou statut explicite."""
    status, attempted = {}, {}
    for r in run.records:
        # L'identification Q0-ID d'une cible Q6-B est la tentative de connexion réelle : elle compte.
        if r.get("case") == "*":
            continue
        o = obs(r)
        if r.get("target") == "primary" and o is not None:
            key = (text_of(o.get("server")), (o.get("server") or {}).get("port"))
            attempted.setdefault(key, set()).add("observed")
        elif str(r.get("target", "")).startswith("extra:"):
            key = (r.get("connect_address") or r.get("target_address"), r.get("target_port"))
            if o is not None:
                attempted.setdefault(key, set()).add("observed")
            elif r.get("error_class") in REFUSED_CLASSES:
                attempted.setdefault(key, set()).add("refused:" + r.get("error_class"))
            else:
                attempted.setdefault(key, set()).add(r.get("error_class") or r.get("outcome") or "not_run")
    for r in run.records:
        if r.get("case") == "*" and str(r.get("target", "")).startswith("extra:"):
            attempted.setdefault((r.get("target_address"), r.get("target_port")), set()).add(r.get("outcome"))
    for address, port in sorted(required, key=str):
        outcomes = attempted.get((address, port), set())
        if str(address).startswith(("g4:", "g6:")):
            status[f"{address}:{port}"] = "untestable_pseudonymised"
        elif "observed" in outcomes:
            status[f"{address}:{port}"] = "observed"
        elif any(o.startswith("refused:") for o in outcomes):
            status[f"{address}:{port}"] = sorted(o for o in outcomes if o.startswith("refused:"))[0]
        elif outcomes:
            status[f"{address}:{port}"] = sorted(outcomes)[0]
        else:
            status[f"{address}:{port}"] = "not_attempted"
    return status


def public_matrix_complete(run, reference):
    if not run.usable or run.role != "public":
        return False
    if not q1_session(run)[0]:
        return False
    return all(header_session(run, q, reference)[0] for q in ("Q2", "Q3", "Q4"))


def analyze_q6(runs, reference):
    publics = [r for r in runs if r.role == "public"]
    privates = private_runs(runs)
    pub_obs = [obs(r) for run in publics for r in run.records if r.get("target") == "primary" and obs(r) is not None]
    priv_obs = [obs(r) for run in privates for r in run.records if r.get("target") == "primary" and obs(r) is not None]
    pub_all, priv_all = _props(pub_obs), _props(priv_obs)
    discriminators = _discriminators(pub_all, priv_all, _self_private(pub_obs + priv_obs))
    candidates = [n for n in CANDIDATE_NAMES if discriminators[n]["separates"]]
    fails, missing = [], []

    spoof = [r for run in privates for r in run.records if r.get("target") == "primary"
             and r.get("q") == "Q6" and obs(r) is not None and any(text_of(v) in FORGED_LITERALS
                                                                    for v in xri_values(obs(r)))]
    if spoof and pub_all["n"] and not candidates:
        fails.append("X-Real-IP forgé reçu en privé, et aucune propriété non contrôlable par l'appelant "
                      "ne distingue le trafic privé du trafic edge")
    for run in privates:
        for r in run.records:
            o = obs(r)
            if str(r.get("target", "")).startswith("extra:") and o is not None and candidates \
                    and _looks_public(o, candidates, pub_all):
                fails.append(f"{run.run_id}/{r.get('target')}: contournement présentant les propriétés edge")
        fails += q6p_status(run)[1]

    boots, per_boot = [], {}
    strict_boots = sorted({run.boot for run in publics + privates if run.usable and run.config
                           and run.config[0] == "strict"}, key=str)
    for boot in strict_boots:
        entry = per_boot.setdefault(str(boot), {"public_complete": False, "private_complete": False,
                                                "bypass": {}, "q6p_missing": [], "private_missing": []})
        boot_publics = [r for r in publics if r.boot == boot and r.usable]
        boot_privates = [r for r in privates if r.boot == boot and r.usable]
        entry["public_complete"] = any(public_matrix_complete(r, reference) for r in boot_publics)
        required = set()
        for run in boot_publics:
            for r in run.records:
                o = obs(r)
                if r.get("target") == "primary" and o is not None:
                    required.add((text_of(o.get("server")), (o.get("server") or {}).get("port")))
        best = None
        for run in boot_privates:
            matrix = private_matrix_missing(run)
            q6p_missing, _ = q6p_status(run)
            bypass = bypass_status(run, required)
            bypass_missing = [f"{k}: {v}" for k, v in bypass.items()
                              if v != "observed" and not v.startswith("refused:")]
            score = (matrix, q6p_missing, bypass_missing)
            if best is None or sum(map(len, score)) < sum(map(len, best[1])):
                best = (bypass, score)
        if best is None:
            entry["private_missing"] = ["aucune session privée intègre"]
        else:
            entry["bypass"], (entry["private_missing"], entry["q6p_missing"], bypass_missing) = best
            entry["private_complete"] = not any(best[1])
            entry["bypass_missing"] = bypass_missing
        # Discriminateur par démarrage.
        b_pub = _props([obs(r) for run in boot_publics for r in run.records
                        if r.get("target") == "primary" and obs(r) is not None])
        b_priv = _props([obs(r) for run in boot_privates for r in run.records
                         if r.get("target") == "primary" and obs(r) is not None])
        b_disc = _discriminators(b_pub, b_priv, set())
        entry["candidates"] = [n for n in CANDIDATE_NAMES if b_disc[n]["separates"]]
        if entry["public_complete"] and entry["private_complete"] and set(candidates) & set(entry["candidates"]):
            boots.append(boot)
        else:
            gaps = ([] if entry["public_complete"] else ["session publique Q1-Q4 complète absente"])
            gaps += (entry["private_missing"] + entry["q6p_missing"] + entry.get("bypass_missing", []))[:10]
            if not set(candidates) & set(entry["candidates"]):
                gaps.append("aucun discriminateur commun")
            entry["gaps"] = gaps
    if len(boots) < REQUIRED_STRICT_BOOTS:
        # Les lacunes des démarrages non qualifiants ne sont listées que si elles empêchent la preuve.
        for boot in strict_boots:
            for item in per_boot[str(boot)].get("gaps", []):
                _missing(missing, f"démarrage {boot}: {item}")
        _missing(missing, f"{len(boots)}/{REQUIRED_STRICT_BOOTS} démarrage(s) strict(s) complet(s) "
                          "(public Q1-Q4, privé Q5-Q7, imitations Q6-01..03, Q6-P, contournements Q6-B)")
    facts = {"public_observations": pub_all["n"], "private_observations": priv_all["n"],
             "spoofed_x_real_ip_reaches_application": len(spoof), "discriminators": discriminators,
             "candidate_discriminators": candidates, "strict_boots": per_boot,
             "qualifying_boots": [str(b) for b in boots]}
    if fails:
        return {"verdict": FAIL, "reasons": fails[:MAX_LISTED], "missing": missing, "facts": facts}
    if missing or not candidates:
        return {"verdict": INCONCLUSIVE, "reasons": ["Q6 : preuve incomplète (voir missing)"],
                "missing": missing or ["aucun discriminateur candidat"], "facts": facts}
    return {"verdict": PASS, "missing": [], "facts": facts, "reasons": [
        "discriminateur candidat stable sur ≥ 2 démarrages, aucun contournement observé — comportement "
        "OBSERVÉ, non garanti par Railway"]}


# --------------------------------------------------------------------------
# Q7 — configurations Uvicorn (C4)
# --------------------------------------------------------------------------

def analyze_q7(runs):
    fails, missing, facts = [], [], {}
    for run in runs:
        for r in run.records:
            o = obs(r)
            if o is not None and o.get("mode") == "strict" and not o.get("effective_equals_raw"):
                fails.append(f"{run.run_id}/v{r.get('family')}/{r.get('case')}: mode strict, scope[client] réécrit")
    q7_cases = [c for c in CASES if c.q == "Q7"]
    for label, config in Q7_CONFIGS.items():
        entry = facts.setdefault(label, {"mode": config[0], "forwarded_allow_ips_env": config[1], "observed": 0,
                                         "rewritten": 0, "raw_categories": set(), "negative_detected": None})
        candidates = [run for run in runs if run.usable and run.config == config and run.has_q("Q7")]
        for role in ("public", "private"):
            role_runs = [run for run in candidates if run.role == role]
            best_missing = None
            for run in role_runs:
                cell_missing = []
                for case in q7_cases:
                    for family in REQUIRED_FAMILIES[role]:
                        record, absent = run.cell("primary", family, case.id, 1)
                        o = obs(record) if record else None
                        if o is None:
                            status = absent if record is None else (record.get("error_class") or record.get("outcome"))
                            cell_missing.append(f"({label}) {run.run_id}/{role}/v{family}/{case.id}: {status}")
                            continue
                        entry["observed"] += 1
                        entry["rewritten"] += 0 if o.get("effective_equals_raw") else 1
                        entry["raw_categories"].add(str((o.get("raw_client") or {}).get("category")))
                        if label == "d" and case.id == "Q7-01":
                            detected = not o.get("effective_equals_raw") and (
                                role == "public" or text_of(o.get("effective_client")) == F4A)
                            if not detected:
                                cell_missing.append(f"(d) {run.run_id}/{role}/v{family}/Q7-01: témoin négatif sans "
                                                    "réécriture détectée")
                            entry["negative_detected"] = detected if entry["negative_detected"] is not False \
                                else False
                if best_missing is None or len(cell_missing) < len(best_missing):
                    best_missing = cell_missing
            if best_missing is None:
                _missing(missing, f"configuration ({label}) {config[0]} / FORWARDED_ALLOW_IPS="
                                  f"{config[1] if config[1] is not None else 'absente'} : aucune session {role}")
            else:
                for item in best_missing:
                    _missing(missing, item)
        entry["raw_categories"] = sorted(entry["raw_categories"])
    if fails:
        return {"verdict": FAIL, "reasons": fails[:MAX_LISTED], "missing": missing, "facts": facts}
    if missing:
        return {"verdict": INCONCLUSIVE, "reasons": ["Q7 : configurations (a)-(d) incomplètes ou témoin négatif "
                                                     "non démontré"], "missing": missing, "facts": facts}
    return {"verdict": PASS, "reasons": [], "missing": [], "facts": facts}


# --------------------------------------------------------------------------
# Synthèse
# --------------------------------------------------------------------------

def analyze(results, digests=(), notes=(), curl=()):
    """results : [(ligne JSON, document)] ; digests ; anomalies de lecture."""
    collection = verify_collection(results, list(digests), list(notes))
    runs = build_runs(results, collection)
    reference = nominal_reference([r for r in runs if r.usable])
    report = {
        "Q1": analyze_q1(runs),
        "Q2": analyze_header_q(runs, "Q2", reference, curl),
        "Q3": analyze_header_q(runs, "Q3", reference, curl),
        "Q4": analyze_header_q(runs, "Q4", reference, curl),
        "Q5": analyze_q5(runs),
        "Q6": analyze_q6(runs, reference),
        "Q7": analyze_q7(runs),
        "Q8": {"verdict": "DEFINED", "reasons": ["matrice normative du protocole § 7 ; non expérimentale"],
               "missing": []},
        "collection": collection,
        "required": {"families": {k: list(v) for k, v in REQUIRED_FAMILIES.items()},
                     "strict_boots": REQUIRED_STRICT_BOOTS,
                     "q7_configs": {k: list(v) for k, v in Q7_CONFIGS.items()}},
    }
    verdicts = [report[q]["verdict"] for q in QUESTIONS]
    if FAIL in verdicts:
        proposal = "NO-GO"
    elif not collection["proven"]:
        proposal = "INCONCLUSIVE"
    elif all(v == PASS for v in verdicts):
        proposal = "GO-CANDIDATE (soumis à l'audit indépendant et à la décision du fondateur)"
    else:
        proposal = "INCONCLUSIVE"
    report["option_a_proposal"] = proposal
    report["blocking"] = ([] if collection["proven"] else ["intégrité de collecte non prouvée"]) + [
        f"{q}: {report[q]['verdict']}" for q in QUESTIONS if report[q]["verdict"] != PASS]
    report["warning"] = ("Proposition mécanique non décisionnelle ; comportement observé, non garanti par Railway ; "
                         "P1 reste ouverte jusqu'au freeze du fondateur."
                         + ("" if collection["proven"] or proposal != "NO-GO" else
                            " FAIL issu d'une collecte dont l'intégrité n'est pas prouvée : FAIL maintenu, à confirmer."))
    return report


def analyze_files(paths, curl_public=()):
    results, digests, notes, curl = read_lines(paths, curl_public)
    return analyze(results, digests, notes, curl)


def _default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(map(str, value))
    raise TypeError(type(value).__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analyse P1-Q")
    parser.add_argument("files", nargs="*", help="JSONL sonde / journaux Railway")
    parser.add_argument("--curl-public", action="append", default=[], help="observation JSON obtenue par curl")
    parser.add_argument("--out", help="rapport JSON")
    args = parser.parse_args(argv)
    report = analyze_files(args.files, args.curl_public)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=_default)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    for q in QUESTIONS + ("Q8",):
        print(f"{q}: {report[q]['verdict']}" + (f" — {'; '.join(report[q]['reasons'])}" if report[q]["reasons"] else ""))
    print(f"Option A (proposition) : {report['option_a_proposal']}")
    for run_id, run in sorted(report["collection"]["runs"].items(), key=lambda item: str(item[0])):
        print(f"collecte {run_id}: {run['lines']} ligne(s) {'complète' if run['complete'] else 'INCOMPLÈTE'}"
              + (f" — {'; '.join(run['problems'])}" if run["problems"] else ""))
    for note in report["collection"]["notes"]:
        print(f"anomalie de lecture : {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
