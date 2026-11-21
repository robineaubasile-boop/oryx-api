"""E0 — clés de vérification de la phase courante et clés publiques historiques d'audit.

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 3.5) :
bibliothèque standard seulement. AUCUNE clé privée n'est manipulée ici.

Deux ensembles strictement séparés :
1. `E0_VERIFY_KEYS` — clés de vérification de la phase courante : SEULES
   autorisées pour une acceptation opérationnelle (`att_policy_accepted`) ;
   toutes portent l'audience de la phase (PA15) ;
2. `E0_AUDIT_PUBKEYS` — clés publiques historiques, clés retirées comprises :
   utilisables UNIQUEMENT pour reconnaître une signature historique ou une
   émission interdite (`att_crypto_valid`, origine `audit`), JAMAIS pour
   accepter. Aucune méthode de cet objet ne rend une clé d'audit comme clé
   acceptante.

Format JSON (public, aucun secret) : liste d'objets {"kid", "aud", "pub"},
`pub` = clé publique Ed25519 brute en base64url canonique ;
`kid = e0-<1a|2>-<16 hex du SHA-256 de la clé publique brute>`.
`E0_RETIRED_KIDS` : liste JSON de `kid` retirés (chacun présent dans l'ensemble
d'audit, jamais dans l'ensemble courant).
"""
import hashlib
import json
from dataclasses import dataclass

import e0_ed25519
from e0_attest import AttestationFormatError, b64url_decode_canonical, b64url_encode
from e0_version import AUDIENCE_KID_TAG, AUDIENCES, KID_PATTERN, PHASE_AUDIENCE

MAX_KEYS = 16
ORIGIN_CURRENT = "current"
ORIGIN_AUDIT = "audit"


class KeySetError(ValueError):
    """Configuration de clés refusée (refus fermé au démarrage)."""


@dataclass(frozen=True)
class KeyRef:
    kid: str
    aud: str
    public_key: bytes
    origin: str

    def record(self) -> dict:
        return {"kid": self.kid, "aud": self.aud, "origin": self.origin,
                "pub_sha256": hashlib.sha256(self.public_key).hexdigest()}


def derive_kid(aud: str, public_key: bytes) -> str:
    if aud not in AUDIENCES:
        raise KeySetError("audience inconnue")
    return f"e0-{AUDIENCE_KID_TAG[aud]}-" + hashlib.sha256(public_key).hexdigest()[:16]


def public_record(aud: str, public_key: bytes) -> dict:
    """Enregistrement public d'une clé (kid, aud, pub)."""
    e0_ed25519.check_public_key(public_key)
    return {"kid": derive_kid(aud, public_key), "aud": aud, "pub": b64url_encode(public_key)}


def parse_key_list(text, origin: str) -> tuple:
    if text is None or text == "":
        return ()
    try:
        items = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise KeySetError("liste de clés : JSON illisible") from exc
    if not isinstance(items, list) or len(items) > MAX_KEYS:
        raise KeySetError("liste de clés : tableau d'au plus 16 entrées attendu")
    refs, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"kid", "aud", "pub"}:
            raise KeySetError("entrée de clé : exactement kid, aud, pub")
        kid, aud, pub = item["kid"], item["aud"], item["pub"]
        if not all(isinstance(v, str) for v in (kid, aud, pub)):
            raise KeySetError("entrée de clé : chaînes attendues")
        if aud not in AUDIENCES or not KID_PATTERN.fullmatch(kid):
            raise KeySetError("entrée de clé : aud ou kid hors grammaire")
        try:
            public_key = e0_ed25519.check_public_key(b64url_decode_canonical(pub))
        except (AttestationFormatError, e0_ed25519.Ed25519Error) as exc:
            raise KeySetError("entrée de clé : clé publique invalide") from exc
        if derive_kid(aud, public_key) != kid:
            raise KeySetError("entrée de clé : kid non dérivé de la clé publique et de l'audience")
        if kid in seen:
            raise KeySetError("entrée de clé : kid dupliqué")
        seen.add(kid)
        refs.append(KeyRef(kid, aud, public_key, origin))
    return tuple(refs)


def parse_retired(text) -> frozenset:
    if text is None or text == "":
        return frozenset()
    try:
        items = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise KeySetError("kid retirés : JSON illisible") from exc
    if not isinstance(items, list) or not all(isinstance(k, str) and KID_PATTERN.fullmatch(k) for k in items):
        raise KeySetError("kid retirés : liste de kid attendue")
    if len(set(items)) != len(items):
        raise KeySetError("kid retirés : doublon")
    return frozenset(items)


class KeySets:
    """Ensembles de clés d'UNE phase. Contraintes (refus fermé) : au moins une
    clé courante ; toutes les clés courantes ont l'audience de la phase ;
    aucun kid à la fois courant et d'audit ; aucun kid retiré courant ; tout
    kid retiré est reconnaissable par l'ensemble d'audit."""

    def __init__(self, phase: str, current: tuple, audit: tuple, retired: frozenset):
        if phase not in PHASE_AUDIENCE:
            raise KeySetError("phase inconnue")
        if not current:
            raise KeySetError("aucune clé de vérification pour la phase courante")
        audience = PHASE_AUDIENCE[phase]
        if any(ref.aud != audience or ref.origin != ORIGIN_CURRENT for ref in current):
            raise KeySetError("clé courante d'une autre audience que celle de la phase (PA15)")
        if any(ref.origin != ORIGIN_AUDIT for ref in audit):
            raise KeySetError("ensemble d'audit mal étiqueté")
        current_kids = {ref.kid for ref in current}
        audit_kids = {ref.kid for ref in audit}
        if current_kids & audit_kids:
            raise KeySetError("kid présent dans les deux ensembles")
        if current_kids & retired:
            raise KeySetError("kid retiré présent parmi les clés courantes")
        if not retired <= audit_kids:
            raise KeySetError("kid retiré absent de l'ensemble d'audit")
        self.phase = phase
        self.audience = audience
        self._current = {ref.kid: ref for ref in current}
        self._audit = {ref.kid: ref for ref in audit}
        self._retired = frozenset(retired)

    @classmethod
    def from_environment(cls, phase: str, environ) -> "KeySets":
        return cls(phase,
                   parse_key_list(environ.get("E0_VERIFY_KEYS"), ORIGIN_CURRENT),
                   parse_key_list(environ.get("E0_AUDIT_PUBKEYS"), ORIGIN_AUDIT),
                   parse_retired(environ.get("E0_RETIRED_KIDS")))

    def accepting(self, kid: str):
        """Clé autorisée à fonder une acceptation : ensemble courant SEULEMENT."""
        return self._current.get(kid)

    def lookup(self, kid: str):
        """Clé permettant de RECONNAÎTRE une signature (courante puis audit).
        Ne confère aucun pouvoir d'acceptation."""
        return self._current.get(kid) or self._audit.get(kid)

    def is_retired(self, kid: str) -> bool:
        return kid in self._retired

    def summary(self) -> dict:
        return {"phase": self.phase, "audience": self.audience,
                "current": [self._current[k].record() for k in sorted(self._current)],
                "audit": [self._audit[k].record() for k in sorted(self._audit)],
                "retired": sorted(self._retired)}
