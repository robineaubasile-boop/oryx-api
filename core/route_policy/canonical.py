"""Forme canonique du chemin BRUT et appariement exact aux gabarits.

Le chemin est lu dans scope["raw_path"] (octets reçus, jamais décodés). Il
est canonique si et seulement si :

- c'est une chaîne d'octets de 1 à MAX_PATH_BYTES octets commençant par « / » ;
- chaque octet appartient à [A-Za-z0-9-._~/] : AUCUN « % » (donc aucun
  encodage-pourcent, aucun double décodage possible), aucun « ; », « ? »,
  « # », « \\ », « + », espace, octet de contrôle ou non ASCII ;
- aucun segment vide (ni « // », ni « / » final), aucun segment « . » ou
  « .. ».

Un chemin non canonique n'est JAMAIS normalisé ni rapproché d'un gabarit :
la porte le refuse (404) tel quel. Les littéraux des gabarits sont comparés
octet par octet (casse comprise) ; un paramètre {nom} accepte exactement un
segment canonique non vide, que l'application F2 destinataire valide
elle-même (aucune validation métier ici, aucun second autoriseur).
"""
MAX_PATH_BYTES = 2048
_ALLOWED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")


def canonical_segments(raw_path):
    """Segments (str) d'un chemin brut canonique, ou None."""
    if type(raw_path) is not bytes or not 1 <= len(raw_path) <= MAX_PATH_BYTES or raw_path[:1] != b"/":
        return None
    if not _ALLOWED.issuperset(raw_path):
        return None
    segments = raw_path[1:].split(b"/")
    for segment in segments:
        if segment in (b"", b".", b".."):
            return None
    return tuple(segment.decode("ascii") for segment in segments)


def template_segments(template: str):
    """Segments d'un gabarit : ("lit", texte) ou ("param", nom)."""
    out = []
    for part in template[1:].split("/"):
        if part.startswith("{") and part.endswith("}"):
            out.append(("param", part[1:-1]))
        else:
            out.append(("lit", part))
    return tuple(out)


def matches(template_parts, segments) -> bool:
    if len(template_parts) != len(segments):
        return False
    for (kind, value), segment in zip(template_parts, segments):
        if kind == "lit" and value != segment:
            return False
    return True


def templates_overlap(a, b) -> bool:
    """Vrai si un même chemin concret peut correspondre aux deux gabarits."""
    if len(a) != len(b):
        return False
    for (kind_a, value_a), (kind_b, value_b) in zip(a, b):
        if kind_a == "lit" and kind_b == "lit" and value_a != value_b:
            return False
    return True
