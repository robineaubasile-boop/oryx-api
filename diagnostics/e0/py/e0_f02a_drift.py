"""E0 — garde de dérive du registre de routage F2 : SHA-256 et extraction AST statique.

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 3.9) :
bibliothèque standard seulement. Le registre est LU COMME DONNÉES :

1. verrou d'empreinte (config/f2_registry.lock.v1.json) : SHA-256 attendu du
   fichier de registre figé et instantané des couples F2_AUTH (14) et
   F2_PERSONAL (13) ; SHA-256 courant différent → INCONCLUSIVE avec alerte,
   jamais PASS ;
2. extraction AST STRICTEMENT statique : `ast.parse` du texte ; résolution par
   un résolveur FERMÉ des seuls littéraux (`ast.literal_eval`), des noms de
   constantes chaîne de niveau module et des concaténations
   `CONSTANTE + suffixe` ; AUCUN import, AUCUN exec/eval, aucune construction
   d'objet du runtime ;
3. PASS uniquement si le SHA-256 concorde ET si l'extraction reconstruit
   exactement l'instantané (14 + 13, EXPECTED_COUNTS cohérent) ; structure non
   résoluble → INCONCLUSIVE.

Le chemin du registre est TOUJOURS fourni par l'appelant (`--registry`) : le
banc ne désigne lui-même aucun module applicatif (la fidélité runtime reste
possédée par les tests existants du dépôt, jamais ré-exécutés ici).
"""
import argparse
import ast
import hashlib
import json
import sys

PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
CATEGORIES = {"f2_auth": "F2_AUTH", "f2_personal": "F2_PERSONAL"}


class Unresolvable(Exception):
    """Structure non résoluble statiquement (U-F02A)."""


def _string_constants(tree) -> dict:
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                name = node.targets[0].id
                if name in constants:
                    raise Unresolvable(f"constante réaffectée : {name}")
                constants[name] = value.value
    return constants


def _resolve_string(node, constants) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    raise Unresolvable("chaîne non résoluble")


def _pairs_from_generator(node, constants) -> list:
    """`tuple((method, NS + suffix) for method, suffix in (<littéral>))`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tuple"
            and len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.GeneratorExp)):
        raise Unresolvable("forme de génération inconnue")
    generator = node.args[0]
    if len(generator.generators) != 1:
        raise Unresolvable("générateur multiple")
    comp = generator.generators[0]
    if comp.ifs or comp.is_async or not isinstance(comp.target, ast.Tuple) or len(comp.target.elts) != 2 \
            or not all(isinstance(e, ast.Name) for e in comp.target.elts):
        raise Unresolvable("cible de générateur inconnue")
    method_var, suffix_var = (e.id for e in comp.target.elts)
    try:
        source = ast.literal_eval(comp.iter)
    except ValueError as exc:
        raise Unresolvable("itérable non littéral") from exc
    elt = generator.elt
    if not (isinstance(elt, ast.Tuple) and len(elt.elts) == 2 and isinstance(elt.elts[0], ast.Name)
            and elt.elts[0].id == method_var and isinstance(elt.elts[1], ast.BinOp)
            and isinstance(elt.elts[1].op, ast.Add) and isinstance(elt.elts[1].right, ast.Name)
            and elt.elts[1].right.id == suffix_var):
        raise Unresolvable("élément de générateur inconnu")
    prefix = _resolve_string(elt.elts[1].left, constants)
    pairs = []
    for item in source:
        if not (isinstance(item, tuple) and len(item) == 2 and all(isinstance(v, str) for v in item)):
            raise Unresolvable("couple littéral invalide")
        pairs.append((item[0], prefix + item[1]))
    return pairs


def _pairs_from_value(node, constants) -> list:
    if isinstance(node, (ast.Tuple, ast.List)):
        try:
            literal = ast.literal_eval(node)
        except ValueError as exc:
            raise Unresolvable("couples non littéraux") from exc
        if not all(isinstance(i, tuple) and len(i) == 2 and all(isinstance(v, str) for v in i) for i in literal):
            raise Unresolvable("couple littéral invalide")
        return list(literal)
    return _pairs_from_generator(node, constants)


def extract(source_text: str) -> dict:
    """Couples par catégorie F2 et EXPECTED_COUNTS, extraits sans exécution."""
    tree = ast.parse(source_text)
    constants = _string_constants(tree)
    by_category = {value: [] for value in CATEGORIES}
    expected = None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name, value = node.targets[0].id, node.value
        if name == "EXPECTED_COUNTS":
            if not isinstance(value, ast.Dict) or None in value.keys:
                raise Unresolvable("EXPECTED_COUNTS non littéral")
            expected = {}
            for key, count in zip(value.keys, value.values):
                label = _resolve_string(key, constants)
                if not (isinstance(count, ast.Constant) and type(count.value) is int):
                    raise Unresolvable("compte non littéral")
                expected[label] = count.value
            continue
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "_rules":
            if len(value.args) != 3 or value.keywords:
                raise Unresolvable("appel _rules non conforme")
            try:
                category = _resolve_string(value.args[0], constants)
            except Unresolvable:
                continue
            if category in by_category:
                if by_category[category]:
                    raise Unresolvable(f"catégorie {category} définie deux fois")
                by_category[category] = _pairs_from_value(value.args[2], constants)
    if expected is None:
        raise Unresolvable("EXPECTED_COUNTS absent")
    for category, pairs in by_category.items():
        if not pairs:
            raise Unresolvable(f"catégorie {category} introuvable")
    return {"pairs": {CATEGORIES[c]: sorted(map(list, p)) for c, p in by_category.items()},
            "expected_counts": {CATEGORIES[c]: expected.get(c) for c in CATEGORIES}}


def pairs_digest(pairs) -> str:
    """Empreinte canonique d'une liste de couples [méthode, gabarit]."""
    canonical = json.dumps(sorted(list(p) for p in pairs), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def load_lock(path) -> dict:
    with open(path, encoding="utf-8") as handle:
        lock = json.load(handle)
    if lock.get("e0") != "f2-registry-lock" or lock.get("fmt") != 1:
        raise ValueError("verrou de registre illisible")
    return lock


def check(registry_path, lock: dict) -> dict:
    """Verdict de dérive : PASS ou INCONCLUSIVE (jamais FAIL : une dérive ne
    démontre pas une violation, elle interdit seulement la conclusion)."""
    alerts, reasons = [], []
    try:
        with open(registry_path, "rb") as handle:
            data = handle.read()
    except OSError:
        return {"verdict": INCONCLUSIVE, "reasons": ["registre illisible"], "alerts": ["registry_unreadable"],
                "sha256": None, "extraction": None}
    digest = hashlib.sha256(data).hexdigest()
    if digest != lock.get("registry_sha256"):
        alerts.append("registry_sha256_mismatch")
        reasons.append("SHA-256 du registre différent du verrou")
    extraction = None
    try:
        extraction = extract(data.decode("utf-8"))
    except (Unresolvable, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        reasons.append(f"structure non résoluble statiquement : {type(exc).__name__}")
    if extraction is not None:
        snapshot = lock.get("snapshot_sha256") or {}
        counts = lock.get("expected_counts") or {}
        for label in CATEGORIES.values():
            pairs = extraction["pairs"][label]
            if pairs_digest(pairs) != snapshot.get(label):
                reasons.append(f"{label} : couples différents de l'instantané")
            if len(pairs) != counts.get(label) or extraction["expected_counts"].get(label) != counts.get(label):
                reasons.append(f"{label} : comptes incohérents")
            if len({tuple(p) for p in pairs}) != len(pairs):
                reasons.append(f"{label} : couple dupliqué")
    verdict = PASS if not reasons and not alerts else INCONCLUSIVE
    return {"verdict": verdict, "reasons": reasons, "alerts": alerts, "sha256": digest,
            "extraction": extraction}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Garde de dérive statique du registre de routage F2 (E0).")
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--registry", required=True, help="chemin du registre figé (fourni par l'opérateur)")
    parser.add_argument("--lock", required=True, help="config/f2_registry.lock.v1.json")
    args = parser.parse_args(argv)
    result = check(args.registry, load_lock(args.lock))
    summary = {key: result[key] for key in ("verdict", "reasons", "alerts", "sha256")}
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0 if result["verdict"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
