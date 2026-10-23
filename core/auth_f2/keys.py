"""Trousseau de clés serveur F2 (C.2.1 §10 et §25, C.2.1-A D4 et D6).

Six usages indépendants : candidate, derive, active, otp, invitation,
abuse. Chaque clé fait exactement 32 octets et porte une version ASCII
stable (^[A-Za-z0-9._-]{1,64}$).

Règles :

- Résolution EXACTE : key(purpose, version) rend la clé de cette version
  ou lève UnknownKeyVersion. Aucune substitution par la clé courante,
  aucune version inconnue acceptée (reprise, vérification, hachage).
- Une même valeur de clé ne peut servir à deux usages ni à deux versions
  (indépendance des clés) ; une version désigne une seule clé dans ce
  trousseau. La permanence d'une version dans le temps (jamais réattribuée
  à une autre clé) relève de la configuration : voir docs/f2_r1_pr2.md.
- La version COURANTE sert aux nouvelles écritures (candidat, dérivation,
  session, OTP, invitation). Pour K_abuse, la version d'enregistrement est
  fixée en base (auth_abuse_key_state, migration 0018), pas par ce
  trousseau : voir core.auth_f2.abuse.

Le trousseau ne lit aucune variable d'environnement : PR 2 ne fournit
aucun chargeur de production (dormance). repr() ne révèle aucune clé.
"""
from types import MappingProxyType

from core.auth_f2.crypto import KEY_BYTES, CryptoContractError, require_key_version

PURPOSES = ("candidate", "derive", "active", "otp", "invitation", "abuse")


class KeyringError(Exception):
    """Configuration de trousseau invalide."""


class UnknownKeyVersion(Exception):
    """Version de clé absente du trousseau : refus fermé."""

    def __init__(self, purpose: str, version):
        super().__init__(f"clé {purpose} : version inconnue")
        self.purpose = purpose
        self.version = version


class Keyring:
    """keys[purpose] = {version: clé de 32 octets} ; current[purpose] =
    version des nouvelles écritures (doit exister dans keys[purpose])."""

    __slots__ = ("_keys", "_current")

    def __init__(self, keys: dict, current: dict):
        if set(keys) != set(PURPOSES) or set(current) != set(PURPOSES):
            raise KeyringError("trousseau : les six usages sont requis")
        seen = {}
        frozen = {}
        for purpose in PURPOSES:
            versions = keys[purpose]
            if not versions:
                raise KeyringError(f"trousseau : aucune clé {purpose}")
            entry = {}
            for version, key in versions.items():
                try:
                    require_key_version(version)
                except CryptoContractError:
                    raise KeyringError(f"trousseau : version {purpose} invalide") from None
                if not isinstance(key, bytes) or len(key) != KEY_BYTES:
                    raise KeyringError(f"trousseau : clé {purpose} de 32 octets attendue")
                if key in seen:
                    raise KeyringError("trousseau : une même clé ne peut servir deux usages ou deux versions")
                seen[key] = (purpose, version)
                entry[version] = key
            if current[purpose] not in entry:
                raise KeyringError(f"trousseau : version courante {purpose} absente")
            frozen[purpose] = MappingProxyType(entry)
        self._keys = MappingProxyType(frozen)
        self._current = MappingProxyType(dict(current))

    def __repr__(self) -> str:
        return "Keyring(<" + ", ".join(f"{p}:{len(self._keys[p])}" for p in PURPOSES) + ">)"

    def __reduce__(self):
        raise TypeError("Keyring n'est pas sérialisable")

    def key(self, purpose: str, version) -> bytes:
        if purpose not in PURPOSES:
            raise KeyringError("usage de clé inconnu")
        try:
            return self._keys[purpose][version]
        except (KeyError, TypeError):
            raise UnknownKeyVersion(purpose, version) from None

    def has(self, purpose: str, version) -> bool:
        try:
            self.key(purpose, version)
        except UnknownKeyVersion:
            return False
        return True

    def current_version(self, purpose: str) -> str:
        if purpose not in PURPOSES:
            raise KeyringError("usage de clé inconnu")
        return self._current[purpose]

    def current(self, purpose: str) -> tuple:
        version = self.current_version(purpose)
        return version, self._keys[purpose][version]

    def versions(self, purpose: str) -> tuple:
        if purpose not in PURPOSES:
            raise KeyringError("usage de clé inconnu")
        return tuple(sorted(self._keys[purpose]))
