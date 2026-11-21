"""E0 — adaptateur Ed25519 vers l'outil OpenSSL (validations D1 à D9).

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 3.4) :
bibliothèque standard seulement. AUCUNE implémentation mathématique de la
courbe : signature, vérification, génération et dérivation de clé publique
sont déléguées à l'outil `openssl` (sans shell, arguments fermés). L'adaptateur
ne fait que :

- détecter la capacité (D1 version ≥ 1.1.1, D2 présence d'ED25519) et
  refuser fermement sinon (D9, aucun repli) ;
- encoder les conteneurs DER fixes (PKCS#8 v1 et SubjectPublicKeyInfo
  d'Ed25519, préfixes constants de la RFC 8410) ;
- rejeter AVANT l'appel les encodages non canoniques (D4) : longueurs
  erronées, `S ≥ L`, `R` ou `A` dont la coordonnée y encodée est ≥ p ;
- tenir la clé privée hors de `argv` et hors disque (D8) : elle transite par
  un tube anonyme (`/dev/fd/N`). Le message et la signature, publics, passent
  par des fichiers d'un répertoire temporaire privé (0700) effacé aussitôt :
  `pkeyutl -rawin` exige une entrée de taille connue.

Limite consignée : `openssl pkeyutl -rawin` (3.0) refuse un message vide ;
l'adaptateur refuse donc tout message vide (aucun message E0 ne l'est : le
domaine de séparation le préfixe).
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading

PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
PUBLIC_KEY_BYTES = 32
SEED_BYTES = 32
SIGNATURE_BYTES = 64
GROUP_ORDER_L = 2 ** 252 + 27742317777372353535851937790883648493
FIELD_P = 2 ** 255 - 19
MIN_VERSION = (1, 1, 1)
TIMEOUT_SECONDS = 15

_VERSION = re.compile(r"OpenSSL (\d+)\.(\d+)\.(\d+)")
_SAFE_PATH = "/usr/bin:/bin:/usr/local/bin"
_lock = threading.Lock()
_capability = None


class Ed25519Unavailable(RuntimeError):
    """OpenSSL absent, trop ancien ou sans Ed25519 : refus fermé (D9)."""


class Ed25519Error(ValueError):
    """Entrée refusée par l'adaptateur (encodage, longueur, message vide)."""


def _openssl_path():
    path = shutil.which("openssl", path=_SAFE_PATH)
    if path is None:
        raise Ed25519Unavailable("outil openssl introuvable")
    return path


def _env():
    # Environnement fermé : aucune configuration OpenSSL héritée.
    return {"PATH": _SAFE_PATH, "LC_ALL": "C", "OPENSSL_CONF": os.devnull}


def _run(argv, *, pass_fds=(), stdin_data=None, feed=None):
    """Exécute openssl. `feed` : [(fd_écriture, octets)] écrits après le
    lancement puis fermés. Rend (code, stdout, stderr)."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=tuple(pass_fds),
                               env=_env(), close_fds=True)
    try:
        for fd, data in feed or ():
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        out, err = process.communicate(stdin_data, timeout=TIMEOUT_SECONDS)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    return process.returncode, out, err


def capability(refresh: bool = False) -> dict:
    """D1 et D2 : version et présence d'ED25519, mises en cache. Lève
    Ed25519Unavailable (refus fermé) si une condition manque."""
    global _capability
    with _lock:
        if _capability is not None and not refresh:
            return dict(_capability)
        path = _openssl_path()
        code, out, _ = _run([path, "version"])
        text = out.decode("ascii", "replace").strip()
        match = _VERSION.search(text)
        if code != 0 or match is None:
            raise Ed25519Unavailable("version d'OpenSSL illisible")
        version = tuple(int(part) for part in match.groups())
        if version < MIN_VERSION:
            raise Ed25519Unavailable("OpenSSL < 1.1.1 : Ed25519 non garanti")
        code, out, _ = _run([path, "list", "-public-key-algorithms"])
        if code != 0 or b"ED25519" not in out.upper():
            raise Ed25519Unavailable("OpenSSL sans Ed25519")
        _capability = {"openssl": text, "version": ".".join(map(str, version)), "ed25519": True,
                       "path": path}
        return dict(_capability)


def require() -> dict:
    return capability()


# --------------------------------------------------------------------------
# Encodages fixes (RFC 8410) et bornes canoniques (D4)
# --------------------------------------------------------------------------

def pkcs8_from_seed(seed: bytes) -> bytes:
    if not isinstance(seed, bytes) or len(seed) != SEED_BYTES:
        raise Ed25519Error("graine Ed25519 : 32 octets")
    return PKCS8_PREFIX + seed


def check_pkcs8(der: bytes) -> bytes:
    """PKCS#8 v1 Ed25519 STRICT (48 octets, préfixe fixe) ; rend la DER."""
    if not isinstance(der, bytes) or len(der) != len(PKCS8_PREFIX) + SEED_BYTES or not der.startswith(PKCS8_PREFIX):
        raise Ed25519Error("clé privée : PKCS#8 v1 Ed25519 de 48 octets attendu")
    return der


def spki_from_public(public_key: bytes) -> bytes:
    check_public_key(public_key)
    return SPKI_PREFIX + public_key


def public_from_spki(der: bytes) -> bytes:
    if not isinstance(der, bytes) or len(der) != len(SPKI_PREFIX) + PUBLIC_KEY_BYTES or not der.startswith(SPKI_PREFIX):
        raise Ed25519Error("SubjectPublicKeyInfo Ed25519 de 44 octets attendu")
    return check_public_key(der[len(SPKI_PREFIX):])


def _y_is_canonical(encoded: bytes) -> bool:
    """Encodage de point canonique quant à y : y (255 bits) < p."""
    y = int.from_bytes(encoded, "little") & ((1 << 255) - 1)
    return y < FIELD_P


def check_public_key(public_key: bytes) -> bytes:
    if not isinstance(public_key, bytes) or len(public_key) != PUBLIC_KEY_BYTES:
        raise Ed25519Error("clé publique Ed25519 : 32 octets")
    if not _y_is_canonical(public_key):
        raise Ed25519Error("clé publique non canonique (y ≥ p)")
    return public_key


def signature_is_canonical(signature: bytes) -> bool:
    if not isinstance(signature, bytes) or len(signature) != SIGNATURE_BYTES:
        return False
    if not _y_is_canonical(signature[:32]):
        return False
    return int.from_bytes(signature[32:], "little") < GROUP_ORDER_L


# --------------------------------------------------------------------------
# Opérations déléguées à OpenSSL
# --------------------------------------------------------------------------

class _Scratch:
    """Répertoire privé (0700), en mémoire si /dev/shm existe ; effacé à la sortie."""

    def __enter__(self):
        base = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
        self.path = tempfile.mkdtemp(prefix="e0-ed25519-", dir=base)
        os.chmod(self.path, 0o700)
        return self

    def write(self, name: str, data: bytes) -> str:
        target = os.path.join(self.path, name)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return target

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def _private_pipe(private_der: bytes):
    read_fd, write_fd = os.pipe()
    return read_fd, write_fd, f"/dev/fd/{read_fd}"


def public_from_private(private_der: bytes) -> bytes:
    """Clé publique brute (32 octets) dérivée par OpenSSL ; clé par tube."""
    require()
    check_pkcs8(private_der)
    read_fd, write_fd, source = _private_pipe(private_der)
    try:
        code, out, _ = _run([_openssl_path(), "pkey", "-inform", "DER", "-in", source, "-pubout", "-outform", "DER"],
                            pass_fds=(read_fd,), feed=[(write_fd, private_der)])
    finally:
        os.close(read_fd)
    if code != 0:
        raise Ed25519Error("dérivation de la clé publique refusée par OpenSSL")
    return public_from_spki(out)


def sign(private_der: bytes, message: bytes) -> bytes:
    """Signature Ed25519 (RFC 8032, déterministe) par OpenSSL."""
    require()
    check_pkcs8(private_der)
    if not isinstance(message, bytes) or not message:
        raise Ed25519Error("message vide ou non binaire refusé")
    with _Scratch() as scratch:
        message_path = scratch.write("message", message)
        read_fd, write_fd, source = _private_pipe(private_der)
        try:
            code, out, _ = _run([_openssl_path(), "pkeyutl", "-sign", "-rawin", "-keyform", "DER", "-inkey", source,
                                 "-in", message_path], pass_fds=(read_fd,), feed=[(write_fd, private_der)])
        finally:
            os.close(read_fd)
    if code != 0 or len(out) != SIGNATURE_BYTES:
        raise Ed25519Error("signature refusée par OpenSSL")
    return out


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Vérification STRICTE : encodages contrôlés avant l'appel (D4), puis
    OpenSSL. Toute entrée non canonique rend False, jamais une exception."""
    require()
    try:
        check_public_key(public_key)
    except Ed25519Error:
        return False
    if not signature_is_canonical(signature):
        return False
    if not isinstance(message, bytes) or not message:
        return False
    with _Scratch() as scratch:
        key_path = scratch.write("public.der", SPKI_PREFIX + public_key)
        signature_path = scratch.write("signature", signature)
        message_path = scratch.write("message", message)
        code, out, _ = _run([_openssl_path(), "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey",
                             key_path, "-sigfile", signature_path, "-in", message_path])
    return code == 0 and out.strip() == b"Signature Verified Successfully"


def generate() -> tuple:
    """Nouvelle paire éphémère générée par OpenSSL : (PKCS#8 DER, clé publique
    brute). La clé privée ne quitte la mémoire que par stdout du processus."""
    require()
    code, out, _ = _run([_openssl_path(), "genpkey", "-algorithm", "ED25519", "-outform", "DER"])
    if code != 0:
        raise Ed25519Error("génération refusée par OpenSSL")
    private_der = check_pkcs8(out)
    return private_der, public_from_private(private_der)
