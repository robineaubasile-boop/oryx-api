"""E0 — JSON canonique, chaînage SHA-256, empreintes de fin.

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 5,
§ 5.3) : bibliothèque standard seulement.

- JSON canonique : `sort_keys`, séparateurs `,` et `:`, `ensure_ascii` ; seuls
  les entiers, chaînes, booléens, null, listes et objets à clés chaînes sont
  admis (aucun flottant : représentation non déterministe).
- Empreinte d'une ligne = SHA-256 de `ligne + "\\n"` (la ligne JSON, sans
  préfixe de journal).
- Chaînage : chaque enregistrement porte `seq` (0, 1, 2, …) et `prev` =
  empreinte de la ligne précédente (64 zéros pour la première).

AC-02 : une chaîne SHA-256 valide démontre la COHÉRENCE INTERNE d'une
collecte, JAMAIS l'authenticité de sa source (un fichier modifié peut être
entièrement rechaîné). `verify_chain` ne conclut donc jamais à l'authenticité :
il compare seulement, si elle est fournie, la tête de chaîne à une empreinte
EXTERNE conservée par un collecteur indépendant, et rend `external_head`
`absent` / `match` / `mismatch`.
"""
import hashlib
import json

from e0_version import FORMAT, PROTOCOL

GENESIS = "0" * 64
DEFAULT_MAX_LINE_BYTES = 4096


class CanonicalError(ValueError):
    """Valeur non représentable en JSON canonique E0."""


class LineTooLong(ValueError):
    """Ligne canonique au-delà de la taille bornée."""


def _check(value, depth=0):
    if depth > 32:
        raise CanonicalError("profondeur excessive")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise CanonicalError("flottant refusé")
    if isinstance(value, (list, tuple)):
        for item in value:
            _check(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("clé non chaîne")
            _check(item, depth + 1)
        return
    raise CanonicalError(f"type refusé : {type(value).__name__}")


def canonical(value) -> str:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def line_digest(line: str) -> str:
    return hashlib.sha256((line + "\n").encode("ascii")).hexdigest()


def file_digest(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class ChainWriter:
    """Producteur de lignes chaînées (aucune écriture de fichier ici : le
    consommateur décide de la sortie, par exemple stdout de l'origine)."""

    def __init__(self, bench: str, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES):
        if not isinstance(bench, str) or not bench:
            raise ValueError("identité du banc absente")
        self.bench = bench
        self.max_line_bytes = max_line_bytes
        self.seq = 0
        self.head = GENESIS

    def build(self, record_type: str, body: dict) -> str:
        """Ligne canonique suivante (non encore engagée)."""
        if not isinstance(body, dict):
            raise CanonicalError("objet attendu")
        reserved = {"e0", "fmt", "protocol", "bench", "seq", "prev"} & set(body)
        if reserved:
            raise CanonicalError("champs réservés dans le corps : " + ", ".join(sorted(reserved)))
        record = dict(body)
        record.update({"e0": record_type, "fmt": FORMAT, "protocol": PROTOCOL, "bench": self.bench,
                       "seq": self.seq, "prev": self.head})
        line = canonical(record)
        if len(line) + 1 > self.max_line_bytes:
            raise LineTooLong(f"ligne de {len(line) + 1} octets > {self.max_line_bytes}")
        return line

    def commit(self, line: str) -> str:
        self.head = line_digest(line)
        self.seq += 1
        return self.head

    def append(self, record_type: str, body: dict) -> str:
        line = self.build(record_type, body)
        self.commit(line)
        return line


def verify_chain(lines, *, expected_bench=None, external_head=None) -> dict:
    """Contrôle de cohérence interne d'une suite de lignes JSON (sans préfixe).
    Rend {"ok", "count", "head", "problems", "external_head"}. `ok` ne vaut
    jamais preuve d'authenticité (AC-02)."""
    problems, head, count = [], GENESIS, 0
    for index, line in enumerate(lines):
        count += 1
        try:
            record = json.loads(line)
        except ValueError:
            problems.append(f"ligne {index} : JSON illisible")
            head = line_digest(line)
            continue
        if not isinstance(record, dict):
            problems.append(f"ligne {index} : objet attendu")
        else:
            try:
                if canonical(record) != line:
                    problems.append(f"ligne {index} : forme non canonique")
            except CanonicalError:
                problems.append(f"ligne {index} : valeur non canonique")
            if record.get("fmt") != FORMAT or record.get("protocol") != PROTOCOL:
                problems.append(f"ligne {index} : en-tête de format invalide")
            if expected_bench is not None and record.get("bench") != expected_bench:
                problems.append(f"ligne {index} : banc différent")
            if record.get("seq") != index:
                problems.append(f"ligne {index} : seq discontinu")
            if record.get("prev") != head:
                problems.append(f"ligne {index} : chaînage rompu")
        head = line_digest(line)
    if external_head is None:
        external = "absent"
    else:
        external = "match" if external_head == head else "mismatch"
        if external == "mismatch":
            problems.append("tête de chaîne différente de l'empreinte externe")
    return {"ok": not problems and count > 0, "count": count, "head": head, "problems": problems,
            "external_head": external}
