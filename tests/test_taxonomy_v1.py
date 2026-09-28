"""Tests de T4-C (1/2) : SPEC canonique Oryx Taxonomy V1
(core/pedagogy/taxonomy_v1.py) — contenu, validation statique stricte,
canonicalisation, fingerprint SHA-256 doré.

Aucune base : Python pur. Les tests de bootstrap / vérification /
activation contre PostgreSQL sont dans tests/test_taxonomy_bootstrap.py.

Défense anti-dérive : le fingerprint doré (littéral recopié ici ET dans le
module), un instantané code -> libellé, une empreinte courte par entrée
(capacité, compétence, règle) et la forme exacte de chaque mapping_guidance.
Une modification de contenu est donc visible dans le diff de ce fichier ET
casse le fingerprint doré. Ne jamais mettre à jour ces valeurs sans décision
pédagogique explicite (et, pour une définition déjà persistée, jamais en r1).
"""
import ast
import copy
import hashlib
import json
import random
import unicodedata

import pytest

from core import taxonomy_service as tax
from core.pedagogy import taxonomy_v1 as v1
from core.pedagogy.taxonomy_v1 import TaxonomySpecError
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_migration_0007_pedagogical_taxonomy import CAPABILITY_CODES, COMPETENCY_CODES

SPEC_PATH = REPO_ROOT / "core" / "pedagogy" / "taxonomy_v1.py"

# Calculé depuis la SPEC implémentée (voir la PR T4-C), puis figé.
GOLDEN_V1_FINGERPRINT = "50b690b9e5ddd6812b625b02037b6f29b8b6cd553c3cf7d4069a634f7df5e2f7"

LABELS = {
    "C1_A": "Nature économique de l'investissement",
    "C1_B": "Actif / enveloppe / intermédiaire",
    "C1_C": "Fondamentaux / mouvement de prix",
    "C2_A": "Nature du risque",
    "C2_B": "Horizon & liquidité",
    "C2_C": "Capacité vs tolérance au risque",
    "C3_A": "Poids & concentration",
    "C3_B": "Diversification réelle des expositions",
    "C3_C": "Mécanique d'allocation",
    "C4_A": "Client & proposition de valeur",
    "C4_B": "Moteur de revenus",
    "C4_C": "Chaîne de valeur & dépendances",
    "C4_D": "Position concurrentielle & moat",
    "C5_A": "Nature & décomposition de la croissance",
    "C5_B": "Moteurs & durabilité",
    "C5_C": "Normalisation & extrapolation",
    "C5_D": "Qualité de la croissance",
    "C6_A": "Structure de la rentabilité",
    "C6_B": "Moteurs des marges",
    "C6_C": "Structure de coûts & levier opérationnel",
    "C6_D": "Normalisation & durabilité",
    "C7_A": "Résultat comptable vs cash",
    "C7_B": "BFR & conversion opérationnelle",
    "C7_C": "CAPEX & construction du FCF",
    "C7_D": "Qualité & normalisation du cash-flow",
    "C8_A": "Rendement du capital investi",
    "C8_B": "Intensité capitalistique & réinvestissement",
    "C8_C": "Rendement incrémental & durabilité",
    "C8_D": "Allocation du capital",
    "C9_A": "Structure financière & bilan",
    "C9_B": "Liquidité, service de la dette & échéances",
    "C9_C": "Résilience & stress financier",
    "C9_D": "Financement externe & dilution",
    "C10_A": "Prix relatif aux fondamentaux",
    "C10_B": "Hypothèses implicites & reverse valuation",
    "C10_C": "Méthodes & cohérence de valorisation",
    "C10_D": "Incertitude, scénarios & rendement conditionnel",
    "C11_A": "Thèse explicite & testable",
    "C11_B": "Risques, hypothèses critiques & falsifiabilité",
    "C11_C": "Signal fondamental vs bruit de marché",
    "C11_D": "Révision de la thèse",
    "C12_A": "Rôle des positions",
    "C12_B": "Dépendances & hypothèses communes",
    "C12_C": "Interaction des thèses & stress test portefeuille",
    "C12_D": "Conséquence globale des changements de portefeuille",
}

COMPETENCY_LABELS = {
    "C1": "Fondations de l'investissement",
    "C2": "Risque & horizon",
    "C3": "Diversification & allocation",
    "C4": "Compréhension du business",
    "C5": "Croissance",
    "C6": "Rentabilité",
    "C7": "Cash-flow",
    "C8": "Efficacité du capital / ROIC",
    "C9": "Bilan & solidité financière",
    "C10": "Valorisation",
    "C11": "Thèse & risques",
    "C12": "Raisonnement portefeuille",
}

RULE_LABELS = {
    "R1": "Capability != preuve",
    "R2": "Capability != stade",
    "R3": "Pas de comptage mécanique",
    "R4": "Plusieurs capabilities possibles",
    "R5": "Jamais cross-competency",
    "R6": "competency_only est légitime",
    "R7": "Raisonnement > mot-clé",
    "R8": "Minimum nécessaire",
}

# 16 premiers caractères hexadécimaux du SHA-256 du JSON canonique de chaque
# entrée : localise immédiatement l'entrée qui a dérivé.
ENTRY_DIGESTS = {
    "C1_A": "cdd1d2c935644a26", "C1_B": "7af5debc25a5251f", "C1_C": "e6597c8ac35c33e9",
    "C2_A": "e95838b3af64d422", "C2_B": "aeba40fde5c7ed52", "C2_C": "a098661cab32e9ac",
    "C3_A": "f4e393e3236dad93", "C3_B": "98776c68b98b157d", "C3_C": "e1d5aa0e434139e3",
    "C4_A": "da1507ccac0f0848", "C4_B": "e2a16a7a7f3258d1", "C4_C": "10d43a5f8d92dcc6",
    "C4_D": "541b0ebf0f0e0a2f",
    "C5_A": "078c7fef1e789b0e", "C5_B": "deb6fe91f27f2f23", "C5_C": "ad2009f54f92514c",
    "C5_D": "1a8689d39845223d",
    "C6_A": "1f9d11311498f2a8", "C6_B": "814627aad3e3475d", "C6_C": "b45e41121196bfca",
    "C6_D": "dbaa8325549e9f99",
    "C7_A": "50293612ed5e3701", "C7_B": "0dad8e16a042fb0a", "C7_C": "e3bca70459f217e6",
    "C7_D": "fe38d4c4f982db8e",
    "C8_A": "e3941dfdabddfb4b", "C8_B": "4126ea173e2679fa", "C8_C": "a6c2b6e42e909c5b",
    "C8_D": "073ea2238961c568",
    "C9_A": "03bea773d680a38d", "C9_B": "8f044a8e01be780a", "C9_C": "20c1b857722664f4",
    "C9_D": "3139eccf9bd0ef45",
    "C10_A": "8d12542f73f62580", "C10_B": "60bb848e2a0e492c", "C10_C": "95336054f3106444",
    "C10_D": "155ce6884ec36bdc",
    "C11_A": "9576c141fd8c05bb", "C11_B": "0813be337e3f7b8d", "C11_C": "fe8fd0ee1a046ae0",
    "C11_D": "719ecbe61e7ae73c",
    "C12_A": "7c97228c9f21ee30", "C12_B": "fc23f6a75c55236d", "C12_C": "141ddfaa3a0d1d3c",
    "C12_D": "f8708ccd471b30e2",
    "C1": "e3c15991cf570807", "C2": "185c24e253739cef", "C3": "6dfb33f0e53ca6c7",
    "C4": "b9e0f63a2c94debb", "C5": "9ccf899e135f66fa", "C6": "539db9f072acd64c",
    "C7": "4547c6154831f6d7", "C8": "27e8f4f2471a49c0", "C9": "2c673e83ffb78941",
    "C10": "29f30cda822da1b9", "C11": "686013105939a1c3", "C12": "dd55a0246849d1f7",
    "R1": "8b86f58393a89acf", "R2": "f4f0c5fc02f2f1cc", "R3": "7a0e2845858f4ffa",
    "R4": "c6cc820690c3b2ce", "R5": "b0f9a9417804bfc5", "R6": "f18e0b411f82d719",
    "R7": "5d50727f45e75034", "R8": "2cb1abca14e69043",
}

# code -> (nb include, nb exclude, cibles des boundary_notes dans l'ordre).
GUIDANCE_SHAPE = {
    "C1_A": (3, 2, ()), "C1_B": (2, 2, ()), "C1_C": (2, 1, ("C11_C",)),
    "C2_A": (3, 2, ()), "C2_B": (2, 2, ()), "C2_C": (2, 2, ()),
    "C3_A": (2, 1, ("C12_B",)), "C3_B": (2, 1, ("C12_B",)), "C3_C": (3, 1, ("C12_D",)),
    "C4_A": (3, 2, ()), "C4_B": (2, 2, ()), "C4_C": (2, 1, ("C11_B",)), "C4_D": (2, 2, ()),
    "C5_A": (2, 2, ()), "C5_B": (2, 2, ()), "C5_C": (3, 2, ()), "C5_D": (2, 1, ("C6-C8",)),
    "C6_A": (2, 2, ()), "C6_B": (1, 2, ()), "C6_C": (2, 1, ()), "C6_D": (2, 2, ()),
    "C7_A": (2, 2, ("C7_B",)), "C7_B": (3, 2, ("C7_A",)), "C7_C": (2, 1, ("C8",)), "C7_D": (2, 2, ()),
    "C8_A": (2, 2, ()), "C8_B": (2, 2, ()), "C8_C": (3, 2, ()), "C8_D": (3, 1, ("C10",)),
    "C9_A": (2, 1, ()), "C9_B": (1, 2, ()), "C9_C": (2, 1, ()), "C9_D": (2, 1, ("C10",)),
    "C10_A": (2, 2, ()), "C10_B": (2, 2, ()), "C10_C": (3, 1, ()), "C10_D": (3, 2, ()),
    "C11_A": (2, 2, ()), "C11_B": (2, 2, ("C4_C",)), "C11_C": (2, 1, ("C1_C",)), "C11_D": (3, 2, ()),
    "C12_A": (2, 2, ()), "C12_B": (2, 1, ("C3",)), "C12_C": (2, 2, ()), "C12_D": (2, 1, ("C3_C",)),
}


def _spec():
    return v1.taxonomy_v1_spec()


def _cap(spec, code):
    return next(c for c in spec["capabilities"] if c["capability_code"] == code)


def _comp(spec, code):
    return next(c for c in spec["competencies"] if c["competency_code"] == code)


def _rule(spec, code):
    return next(r for r in spec["global_mapping_rules"] if r["rule_code"] == code)


def _digest(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _reverse_keys(value):
    """Même contenu, ordre d'insertion des clés inversé à tous les niveaux."""
    if isinstance(value, dict):
        return {k: _reverse_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Identité, fingerprint doré
# --------------------------------------------------------------------------

def test_release_identity():
    assert v1.VERSION_KEY == "oryx-v1"
    assert v1.TAXONOMY_SCHEMA_VERSION == 1 and type(v1.TAXONOMY_SCHEMA_VERSION) is int
    assert v1.V1_SEMANTIC_REVISION == 1
    spec = _spec()
    assert set(spec) == {"taxonomy_schema_version", "version_key", "global_mapping_rules",
                         "competencies", "capabilities"}
    assert (spec["taxonomy_schema_version"], spec["version_key"]) == (1, "oryx-v1")


def test_golden_fingerprint():
    fingerprint = v1.compute_spec_fingerprint(_spec())
    assert fingerprint == GOLDEN_V1_FINGERPRINT == v1.EXPECTED_V1_FINGERPRINT
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()
    assert set(fingerprint) <= set("0123456789abcdef")


def test_fingerprint_is_the_documented_sha256_of_the_canonical_json():
    spec = _spec()
    expected_bytes = json.dumps(v1.canonical_payload(spec), sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
    assert v1.canonical_json(spec) == expected_bytes
    assert hashlib.sha256(expected_bytes).hexdigest() == GOLDEN_V1_FINGERPRINT


def test_expected_fingerprint_is_a_frozen_literal():
    """Jamais calculé à l'import : la constante est un littéral du source."""
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    assign = next(n for n in tree.body if isinstance(n, ast.Assign)
                  and [t.id for t in n.targets if isinstance(t, ast.Name)] == ["EXPECTED_V1_FINGERPRINT"])
    assert isinstance(assign.value, ast.Constant) and assign.value.value == GOLDEN_V1_FINGERPRINT


def test_load_validates_and_returns_the_golden_fingerprint():
    spec, fingerprint = v1.load_taxonomy_v1()
    assert spec == _spec() and fingerprint == GOLDEN_V1_FINGERPRINT


def test_load_refuses_a_spec_that_no_longer_matches_the_expected_fingerprint(monkeypatch):
    drifted = _spec()
    _cap(drifted, "C7_A")["definition"] += " "
    monkeypatch.setattr(v1, "_SPEC", drifted)
    with pytest.raises(TaxonomySpecError, match="EXPECTED_V1_FINGERPRINT"):
        v1.load_taxonomy_v1()


def test_load_refuses_an_expected_fingerprint_that_does_not_match(monkeypatch):
    monkeypatch.setattr(v1, "EXPECTED_V1_FINGERPRINT", "0" * 64)
    with pytest.raises(TaxonomySpecError, match="EXPECTED_V1_FINGERPRINT"):
        v1.load_taxonomy_v1()


def test_load_validates_before_fingerprinting(monkeypatch):
    broken = _spec()
    _cap(broken, "C7_A")["semantic_revision"] = 2
    monkeypatch.setattr(v1, "_SPEC", broken)
    with pytest.raises(TaxonomySpecError, match="semantic_revision"):
        v1.load_taxonomy_v1()


def test_spec_accessor_returns_an_independent_deep_copy():
    first = _spec()
    first["capabilities"][0]["mapping_guidance"]["include"].append("mutation")
    first["competencies"].pop()
    assert v1.compute_spec_fingerprint(_spec()) == GOLDEN_V1_FINGERPRINT
    assert _spec() != first


# --------------------------------------------------------------------------
# Contenu : compétences, règles, capacités
# --------------------------------------------------------------------------

def test_exactly_12_competencies_c1_to_c12_in_natural_order():
    competencies = _spec()["competencies"]
    assert [c["competency_code"] for c in competencies] == [f"C{i}" for i in range(1, 13)]
    assert tuple(c["competency_code"] for c in competencies) == v1.COMPETENCY_CODES == tuple(COMPETENCY_CODES)
    assert {c["competency_code"]: c["label"] for c in competencies} == COMPETENCY_LABELS
    for competency in competencies:
        assert set(competency) == {"competency_code", "label", "central_question"}
        assert competency["central_question"].startswith("L'utilisateur ")
        assert competency["central_question"].endswith(" ?")


def test_central_questions_are_canonical():
    questions = {c["competency_code"]: c["central_question"] for c in _spec()["competencies"]}
    assert questions["C1"] == ("L'utilisateur comprend-il ce qu'il est réellement en train de faire "
                               "lorsqu'il investit ?")
    assert questions["C7"] == ("L'utilisateur comprend-il comment l'activité se transforme en cash, pourquoi "
                               "bénéfice et trésorerie divergent, où le cash est absorbé et quelle part reste "
                               "disponible ?")
    assert questions["C12"] == ("L'utilisateur comprend-il comment les positions, leurs rôles, leurs thèses et "
                                "leurs dépendances interagissent à l'échelle du portefeuille ?")


def test_exactly_8_global_mapping_rules():
    rules = _spec()["global_mapping_rules"]
    assert [r["rule_code"] for r in rules] == list(v1.GLOBAL_RULE_CODES) == [f"R{i}" for i in range(1, 9)]
    assert {r["rule_code"]: r["label"] for r in rules} == RULE_LABELS
    assert all(set(r) == {"rule_code", "label", "rule"} for r in rules)
    spec = _spec()
    assert _rule(spec, "R1")["rule"] == "Un mapping ne crée jamais une observation supplémentaire."
    assert _rule(spec, "R5")["rule"] == ("Une observation C7 ne mappe jamais C8_A. Si le raisonnement démontre "
                                         "C7 et C8, produire deux observations.")
    assert _rule(spec, "R6")["rule"] == ("Si Cx est clair mais Cx_A/B/C/D n'est pas localisable proprement, "
                                         "ne rien inventer.")


def test_exactly_the_45_frozen_capability_codes_in_natural_order():
    capabilities = _spec()["capabilities"]
    codes = [c["capability_code"] for c in capabilities]
    assert len(codes) == len(set(codes)) == 45
    assert tuple(codes) == v1.CAPABILITY_CODES
    # Recopie volontaire : identique au vocabulaire T4-B et au CHECK de 0007.
    assert v1.CAPABILITY_CODES == tax._CAPABILITY_CODES == CAPABILITY_CODES
    assert sorted(codes) != codes  # ordre naturel, pas lexical
    assert codes.index("C9_D") + 1 == codes.index("C10_A")


def test_every_capability_has_exactly_the_canonical_shape():
    for capability in _spec()["capabilities"]:
        code = capability["capability_code"]
        assert set(capability) == set(v1.PERSISTED_CAPABILITY_FIELDS), code
        assert capability["semantic_revision"] == 1 and type(capability["semantic_revision"]) is int, code
        assert capability["competency_code"] == code.split("_")[0], code
        assert capability["label"].strip() and capability["definition"].strip(), code
        guidance = capability["mapping_guidance"]
        assert set(guidance) == {"include", "exclude", "boundary_notes"}, code
        assert guidance["include"] and guidance["exclude"], code
        assert all(type(s) is str and s.strip() for s in guidance["include"] + guidance["exclude"]), code
        for note in guidance["boundary_notes"]:
            assert set(note) == {"against", "rule"} and note["against"].strip() and note["rule"].strip(), code


def test_capability_labels_snapshot():
    assert {c["capability_code"]: c["label"] for c in _spec()["capabilities"]} == LABELS


def test_every_entry_matches_its_digest_snapshot():
    spec = _spec()
    digests = {c["capability_code"]: _digest(c) for c in spec["capabilities"]}
    digests |= {c["competency_code"]: _digest(c) for c in spec["competencies"]}
    digests |= {r["rule_code"]: _digest(r) for r in spec["global_mapping_rules"]}
    drifted = sorted(code for code in ENTRY_DIGESTS if digests.get(code) != ENTRY_DIGESTS[code])
    assert drifted == [] and set(digests) == set(ENTRY_DIGESTS)


def test_mapping_guidance_shape_snapshot():
    shape = {c["capability_code"]: (len(c["mapping_guidance"]["include"]), len(c["mapping_guidance"]["exclude"]),
                                    tuple(n["against"] for n in c["mapping_guidance"]["boundary_notes"]))
             for c in _spec()["capabilities"]}
    assert shape == GUIDANCE_SHAPE
    assert sum(len(notes) for _, _, notes in shape.values()) == 15
    assert sum(1 for _, _, notes in shape.values() if not notes) == 30


def test_selected_definitions_and_guidance_are_canonical():
    spec = _spec()
    assert _cap(spec, "C7_A")["definition"] == (
        "Comprendre pourquoi bénéfice et génération de trésorerie peuvent diverger et ne jamais assimiler "
        "automatiquement résultat comptable à cash disponible.")
    assert _cap(spec, "C6_A")["definition"] == (
        "Savoir distinguer les principaux niveaux de marge et comprendre ce qu'ils révèlent - ou ne révèlent "
        "pas - sur l'économie de l'entreprise.")
    assert _cap(spec, "C7_B")["mapping_guidance"]["exclude"] == [
        "Simple constat que résultat comptable et cash diffèrent sans identifier le BFR.", "CAPEX."]
    assert _cap(spec, "C5_D")["mapping_guidance"]["boundary_notes"] == [{
        "against": "C6-C8",
        "rule": "C5 qualifie la croissance ; C6, C7 et C8 analysent respectivement ses conséquences sur profit, "
                "cash et capital."}]
    # Notes réciproques explicitement identiques dans la SPEC métier.
    assert (_cap(spec, "C7_A")["mapping_guidance"]["boundary_notes"][0]["rule"]
            == _cap(spec, "C7_B")["mapping_guidance"]["boundary_notes"][0]["rule"])


def test_boundary_note_targets_are_codes_of_the_taxonomy():
    """Cible = capacité (C7_B), compétence (C8) ou plage de compétences
    (C6-C8) du référentiel ; jamais la capacité elle-même."""
    spec = _spec()
    for capability in spec["capabilities"]:
        for note in capability["mapping_guidance"]["boundary_notes"]:
            target = note["against"]
            bounds = target.split("-")
            assert all(b in v1.CAPABILITY_CODES or b in v1.COMPETENCY_CODES for b in bounds), target
            assert target != capability["capability_code"]


# --------------------------------------------------------------------------
# Unicode : jamais normalisé
# --------------------------------------------------------------------------

def test_content_is_french_nfc_with_ascii_apostrophes():
    strings = list(_strings(_spec()))
    assert all(unicodedata.is_normalized("NFC", s) for s in strings)
    assert not any(ch in s for s in strings for ch in "’‘  –—")
    joined = "".join(strings)
    for accented in "éèàêîôûùÉ":
        assert accented in joined, accented


def test_canonical_json_preserves_utf8_without_escaping():
    raw = v1.canonical_json(_spec())
    text = raw.decode("utf-8")
    assert "Nature économique de l'investissement" in text
    assert "\\u00e9" not in text and "é".encode("utf-8") in raw
    assert text.startswith('{"capabilities":[{"capability_code":"C1_A","competency_code":"C1",')  # compact


def test_no_silent_unicode_normalization():
    """NFD, apostrophe typographique : même rendu, autre contenu, autre
    fingerprint — rien n'est normalisé silencieusement."""
    for transform in (lambda s: unicodedata.normalize("NFD", s), lambda s: s.replace("'", "’")):
        spec = _spec()
        capability = _cap(spec, "C1_A")
        capability["label"] = transform(capability["label"])
        assert capability["label"] != LABELS["C1_A"]
        v1.validate_spec(spec)  # accepté tel quel (aucune correction)...
        assert v1.compute_spec_fingerprint(spec) != GOLDEN_V1_FINGERPRINT  # ... mais jamais confondu


# --------------------------------------------------------------------------
# Validation statique : chaque invariant est détecté
# --------------------------------------------------------------------------

def test_the_canonical_spec_is_valid():
    v1.validate_spec(_spec())


def _set(path, value):
    def mutate(spec):
        target = spec
        for key in path[:-1]:
            target = key(target) if callable(key) else target[key]
        target[path[-1]] = value
    return mutate


def _drop(section, code_key, code):
    return lambda spec: spec[section].remove(next(e for e in spec[section] if e[code_key] == code))


def _duplicate(section, index):
    return lambda spec: spec[section].append(copy.deepcopy(spec[section][index]))


def C7A(spec):
    return _cap(spec, "C7_A")


def C1C(spec):
    return _cap(spec, "C1_C")


def GUIDE(spec):
    return _cap(spec, "C7_A")["mapping_guidance"]


def NOTE(spec):
    return _cap(spec, "C7_A")["mapping_guidance"]["boundary_notes"][0]


MUTATIONS = {
    # identité / racine
    "schema_version_2": (_set(["taxonomy_schema_version"], 2), "taxonomy_schema_version"),
    "schema_version_bool": (_set(["taxonomy_schema_version"], True), "taxonomy_schema_version"),
    "schema_version_str": (_set(["taxonomy_schema_version"], "1"), "taxonomy_schema_version"),
    "version_key_v2": (_set(["version_key"], "oryx-v2"), "version_key"),
    "version_key_case": (_set(["version_key"], "Oryx-V1"), "version_key"),
    "extra_root_key": (_set(["status"], "candidate"), "en trop"),
    "missing_root_key": (lambda s: s.pop("global_mapping_rules"), "manquantes"),
    # règles globales
    "rule_missing": (_drop("global_mapping_rules", "rule_code", "R8"), "global_mapping_rules"),
    "rule_duplicate": (_duplicate("global_mapping_rules", 0), "double"),
    "rule_unknown": (_set(["global_mapping_rules", 7, "rule_code"], "R9"), "global_mapping_rules"),
    "rule_empty": (_set(["global_mapping_rules", 0, "rule"], " "), "rule"),
    "rule_extra_key": (_set(["global_mapping_rules", 0, "weight"], 1), "en trop"),
    "rules_tuple": (lambda s: s.update(global_mapping_rules=tuple(s["global_mapping_rules"])), "list"),
    # compétences
    "competency_missing": (_drop("competencies", "competency_code", "C12"), "competencies"),
    "competency_duplicate": (_duplicate("competencies", 3), "double"),
    "competency_c13": (_set(["competencies", 11, "competency_code"], "C13"), "competencies"),
    "competency_c0": (_set(["competencies", 0, "competency_code"], "C0"), "competencies"),
    "competency_label_empty": (_set(["competencies", 2, "label"], ""), "label"),
    "competency_question_blank": (_set(["competencies", 2, "central_question"], "\n\t"), "central_question"),
    "competency_question_nul": (_set(["competencies", 2, "central_question"], "a\x00b"), "NUL"),
    "competency_extra_key": (_set(["competencies", 0, "stage"], "Discovery"), "en trop"),
    "competency_missing_key": (lambda s: s["competencies"][0].pop("central_question"), "manquantes"),
    # capacités : ensemble exact
    "capability_missing": (_drop("capabilities", "capability_code", "C10_D"), "C10_D"),
    "capability_duplicate": (_duplicate("capabilities", 21), "double"),
    "capability_unknown": (_set([C7A, "capability_code"], "C7_E"), "45 codes"),
    "capability_c1_d": (_set([lambda s: _cap(s, "C1_C"), "capability_code"], "C1_D"), "45 codes"),
    "capability_extra": (lambda s: s["capabilities"].append({**copy.deepcopy(_cap(s, "C7_A")),
                                                            "capability_code": "C3_D", "competency_code": "C3"}),
                         "45 codes"),
    "capability_lowercase": (_set([C7A, "capability_code"], "c7_a"), "45 codes"),
    # capacités : champs
    "revision_2": (_set([C7A, "semantic_revision"], 2), "semantic_revision"),
    "revision_0": (_set([C7A, "semantic_revision"], 0), "semantic_revision"),
    "revision_bool": (_set([C7A, "semantic_revision"], True), "semantic_revision"),
    "revision_float": (_set([C7A, "semantic_revision"], 1.0), "semantic_revision"),
    "revision_str": (_set([C7A, "semantic_revision"], "1"), "semantic_revision"),
    "competency_mismatch": (_set([C7A, "competency_code"], "C8"), "n'appartient pas"),
    "competency_unknown": (_set([C7A, "competency_code"], "C13"), "C1..C12"),
    "label_empty": (_set([C7A, "label"], ""), "label"),
    "label_blank": (_set([C7A, "label"], "   "), "label"),
    "label_none": (_set([C7A, "label"], None), "label"),
    "label_nul": (_set([C7A, "label"], "Résultat\x00"), "NUL"),
    "definition_empty": (_set([C7A, "definition"], ""), "definition"),
    "definition_bytes": (_set([C7A, "definition"], b"x"), "definition"),
    "capability_extra_key": (_set([C7A, "score"], 0), "en trop"),
    "capability_mastered_key": (_set([C7A, "mastered"], False), "en trop"),
    "capability_missing_key": (lambda s: C7A(s).pop("definition"), "manquantes"),
    "capability_not_dict": (_set(["capabilities", 0], ["C1_A"]), "dict"),
    "capabilities_tuple": (lambda s: s.update(capabilities=tuple(s["capabilities"])), "list"),
    # mapping_guidance
    "guidance_not_dict": (_set([C7A, "mapping_guidance"], []), "mapping_guidance"),
    "guidance_empty_dict": (_set([C7A, "mapping_guidance"], {}), "manquantes"),
    "guidance_extra_key": (_set([GUIDE, "notes"], []), "en trop"),
    "guidance_missing_boundary": (lambda s: GUIDE(s).pop("boundary_notes"), "manquantes"),
    "include_empty": (_set([GUIDE, "include"], []), "include"),
    "exclude_empty": (_set([GUIDE, "exclude"], []), "exclude"),
    "include_tuple": (lambda s: GUIDE(s).update(include=tuple(GUIDE(s)["include"])), "include"),
    "include_str": (_set([GUIDE, "include"], "Distingue."), "include"),
    "include_item_empty": (lambda s: GUIDE(s)["include"].append(""), "include"),
    "include_item_int": (lambda s: GUIDE(s)["include"].append(1), "include"),
    "exclude_item_blank": (lambda s: GUIDE(s)["exclude"].append(" "), "exclude"),
    "exclude_item_nul": (lambda s: GUIDE(s)["exclude"].append("x\x00"), "NUL"),
    "boundary_not_list": (_set([GUIDE, "boundary_notes"], {}), "boundary_notes"),
    "boundary_note_not_dict": (_set([GUIDE, "boundary_notes"], ["C7_B"]), "dict"),
    "boundary_note_extra_key": (_set([NOTE, "priority"], 1), "en trop"),
    "boundary_note_missing_rule": (lambda s: NOTE(s).pop("rule"), "manquantes"),
    "boundary_note_against_empty": (_set([NOTE, "against"], ""), "against"),
    "boundary_note_rule_blank": (_set([NOTE, "rule"], " "), "rule"),
    "boundary_note_rule_nul": (_set([NOTE, "rule"], "\x00"), "NUL"),
    "boundary_note_against_none": (_set([NOTE, "against"], None), "against"),
}


class _Str(str):
    pass


MUTATIONS["label_str_subclass"] = (_set([C7A, "label"], _Str("Résultat comptable vs cash")), "label")
MUTATIONS["include_item_str_subclass"] = (lambda s: GUIDE(s)["include"].append(_Str("x")), "include")


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_invariant_mutation_is_detected(name):
    mutate, match = MUTATIONS[name]
    spec = _spec()
    mutate(spec)
    with pytest.raises(TaxonomySpecError, match=match):
        v1.validate_spec(spec)


@pytest.mark.parametrize("value", [None, [], "spec", 1, ()])
def test_spec_root_must_be_a_dict(value):
    with pytest.raises(TaxonomySpecError, match="dict"):
        v1.validate_spec(value)


def test_validation_does_not_mutate_the_spec():
    spec = _spec()
    snapshot = copy.deepcopy(spec)
    v1.validate_spec(spec)
    assert spec == snapshot


# --------------------------------------------------------------------------
# Canonicalisation et fingerprint : stabilité et sensibilité
# --------------------------------------------------------------------------

def test_canonical_payload_contains_exactly_the_five_documented_keys():
    payload = v1.canonical_payload(_spec())
    assert set(payload) == {"taxonomy_schema_version", "version_key", "global_mapping_rules",
                            "competencies", "capabilities"}
    serialized = v1.canonical_json(_spec()).decode("utf-8")
    for db_field in ('"id"', "created_at", "activated_at", '"status"', "release_id", "membership",
                     "spec_fingerprint", "user_id"):
        assert db_field not in serialized, db_field


def test_fingerprint_is_stable_across_deepcopy_and_key_order():
    spec = _spec()
    assert v1.compute_spec_fingerprint(copy.deepcopy(spec)) == GOLDEN_V1_FINGERPRINT
    reordered = _reverse_keys(spec)
    assert list(reordered) != list(spec)
    assert list(reordered["capabilities"][0]) != list(spec["capabilities"][0])
    assert v1.compute_spec_fingerprint(reordered) == GOLDEN_V1_FINGERPRINT


@pytest.mark.parametrize("seed", range(5))
def test_fingerprint_is_independent_of_the_source_order_of_entries(seed):
    spec = _spec()
    rng = random.Random(seed)
    for section in ("global_mapping_rules", "competencies", "capabilities"):
        rng.shuffle(spec[section])
    spec["competencies"].reverse()
    v1.validate_spec(spec)
    assert v1.compute_spec_fingerprint(spec) == GOLDEN_V1_FINGERPRINT
    payload = v1.canonical_payload(spec)
    assert [c["capability_code"] for c in payload["capabilities"]] == list(CAPABILITY_CODES)
    assert [c["competency_code"] for c in payload["competencies"]] == [f"C{i}" for i in range(1, 13)]
    assert [r["rule_code"] for r in payload["global_mapping_rules"]] == [f"R{i}" for i in range(1, 9)]


def test_order_inside_guidance_lists_is_content():
    spec = _spec()
    GUIDE(spec)["include"].reverse()
    assert v1.compute_spec_fingerprint(spec) != GOLDEN_V1_FINGERPRINT


SEMANTIC_CHANGES = {
    "definition": lambda s: C7A(s).update(definition=C7A(s)["definition"].replace("diverger", "différer")),
    "label": lambda s: C7A(s).update(label="Résultat vs cash"),
    "include": lambda s: GUIDE(s)["include"].append("Nouveau critère."),
    "exclude": lambda s: GUIDE(s)["exclude"].pop(),
    "boundary_note_rule": lambda s: NOTE(s).update(rule=NOTE(s)["rule"] + " "),
    "boundary_note_added": lambda s: C1C(s)["mapping_guidance"]["boundary_notes"].append(
        {"against": "C1_A", "rule": "r"}),
    "central_question": lambda s: _comp(s, "C10").update(central_question=_comp(s, "C10")["central_question"][:-2]),
    "competency_label": lambda s: _comp(s, "C8").update(label="ROIC"),
    "global_rule": lambda s: _rule(s, "R7").update(rule=_rule(s, "R7")["rule"].replace("PER, ", "")),
    "global_rule_label": lambda s: _rule(s, "R3").update(label="Comptage"),
    "semantic_revision": lambda s: C7A(s).update(semantic_revision=2),
    "taxonomy_schema_version": lambda s: s.update(taxonomy_schema_version=2),
    "version_key": lambda s: s.update(version_key="oryx-v2"),
    "accent": lambda s: C7A(s).update(label=C7A(s)["label"].replace("é", "e")),
}


@pytest.mark.parametrize("name", sorted(SEMANTIC_CHANGES))
def test_every_semantic_change_breaks_the_golden_fingerprint(name):
    spec = _spec()
    SEMANTIC_CHANGES[name](spec)
    assert spec != _spec()
    assert v1.compute_spec_fingerprint(spec) != GOLDEN_V1_FINGERPRINT


def test_canonical_json_is_strict_json():
    for mutate in (lambda s: GUIDE(s).update(include=tuple(GUIDE(s)["include"])),
                   lambda s: C7A(s).update(semantic_revision=float("nan")),
                   lambda s: C7A(s).update(label={"é"}),
                   lambda s: GUIDE(s).update({1: "clé non str"})):
        spec = _spec()
        mutate(spec)
        with pytest.raises(TaxonomySpecError):
            v1.canonical_json(spec)


@pytest.mark.parametrize("code", ["C07_A", "C7-A", "C7_a", "", None, 7, "X1"])
def test_unorderable_codes_are_refused_by_canonicalization(code):
    spec = _spec()
    C7A(spec)["capability_code"] = code
    with pytest.raises(TaxonomySpecError, match="ordonnable"):
        v1.canonical_payload(spec)


# --------------------------------------------------------------------------
# Module pur
# --------------------------------------------------------------------------

def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_spec_module_is_pure_standard_library():
    assert _imports(SPEC_PATH) == {"copy", "hashlib", "json", "math", "re"}


def test_spec_module_code_has_no_score_stage_or_inference_identifier():
    """Le CONTENU parle de stage / Mastery (règles R2, R3) ; le CODE ne
    définit ni ne manipule aucune de ces notions (identifiants seuls)."""
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    names |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.args}
    parts = {part for name in names for part in name.lower().split("_") if part}
    for word in ("score", "stage", "confidence", "mastery", "mastered", "progress", "coverage", "percent",
                 "user", "llm", "prompt", "anthropic", "session", "commit", "rollback", "lineage", "remap"):
        assert word not in parts, word
    for stem in ("infer", "master", "progress", "compatib", "confiden"):
        assert not any(stem in part for part in parts), stem
    tokens = _code_tokens(SPEC_PATH.read_text(encoding="utf-8")).split("\n")
    for forbidden in ("SessionLocal", "execute", "flush", "add", "commit", "rollback"):
        assert forbidden not in tokens, forbidden
