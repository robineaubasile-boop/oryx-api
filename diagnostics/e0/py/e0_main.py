"""E0 — lanceur Railway de l'origine (E0_ROLE=origin) : garde, TTL, démarrage.

Code TEMPORAIRE et ISOLÉ du runtime Oryx (spécification E0 V0.2-R3 § 2.2) :
bibliothèque standard + uvicorn 0.27.0 (pins de production) + modules e0_*.

Ordre de démarrage (refus fermé, code de sortie 3, noms seulement) :
  1. garde d'environnement : liste blanche EXACTE des variables, liste noire,
     identifiants Railway E0 ;
  2. OpenSSL avec Ed25519 (D1, D2, D9) : sans lui, aucune vérification
     possible → refus de démarrer (aucun repli) ;
  3. configuration stricte (phase, clés, hôte public, bornes, clé
     post-signature, mode TLS réservé à 1c) ;
  4. Uvicorn : proxy_headers=False, http="h11", lifespan="off",
     access_log=False, server_header=False, journal au niveau warning ;
  5. ligne `E0-ORIGIN` de démarrage (nouveau boot_id), TTL borné, puis point
     de contrôle à l'échéance du TTL et à l'arrêt.

Identité du banc (`bench`) : empreinte de l'arbre des modules e0_*.py
réellement présents (`sha256-tree:<hex>`), le conteneur n'ayant pas de dépôt
Git ; le manifeste de run la relie au SHA Git du banc.
"""
import base64
import binascii
import hashlib
import os
import shutil
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import e0_ed25519  # noqa: E402
import e0_guard  # noqa: E402
import e0_origin  # noqa: E402

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))


def bench_identity(directory: str = MODULE_DIR) -> str:
    digest = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        if name.startswith("e0_") and name.endswith(".py"):
            with open(os.path.join(directory, name), "rb") as handle:
                content = handle.read()
            digest.update(name.encode("ascii") + b"\x00" + hashlib.sha256(content).hexdigest().encode("ascii") + b"\n")
    return "sha256-tree:" + digest.hexdigest()


def uvicorn_config(app, *, ssl_certfile=None, ssl_keyfile=None):
    import uvicorn

    return uvicorn.Config(app, http="h11", loop="asyncio", ws="none", lifespan="off", interface="asgi3",
                          proxy_headers=False, access_log=False, server_header=False, log_level="warning",
                          ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)


def open_listener(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    return sock


def _port(environ) -> int:
    text = environ.get("PORT", "8080")
    if not text.isdigit() or not 0 <= int(text) <= 65535:
        raise e0_origin.OriginConfigError("PORT invalide")
    return int(text)


def _write_tls_material(environ):
    """Certificat et clé du saut privé (1c) : écrits en 0600 dans un tmpfs,
    lus par Uvicorn au chargement, puis effacés."""
    base = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    directory = tempfile.mkdtemp(prefix="e0-tls-", dir=base)
    os.chmod(directory, 0o700)
    paths = []
    for name, variable in (("cert.pem", "E0_TLS_CERT_PEM_B64"), ("key.pem", "E0_TLS_KEY_PEM_B64")):
        try:
            data = base64.b64decode(environ[variable], validate=True)
        except (binascii.Error, ValueError) as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise e0_origin.OriginConfigError(f"{variable} : base64 invalide") from exc
        path = os.path.join(directory, name)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        paths.append(path)
    return directory, paths[0], paths[1]


def _refuse(message: str) -> int:
    print("e0-origin: démarrage refusé : " + message, file=sys.stderr, flush=True)
    return e0_guard.EXIT_ENVIRONMENT_NOT_ISOLATED


def run_origin(environ) -> int:
    import uvicorn

    try:
        e0_guard.check_origin_environment(environ)
    except e0_guard.GuardError as exc:
        return _refuse(str(exc))
    try:
        capability = e0_ed25519.require()
    except e0_ed25519.Ed25519Unavailable as exc:
        return _refuse(f"OpenSSL Ed25519 indisponible ({exc})")
    try:
        config = e0_origin.config_from_environment(environ)
        port = _port(environ)
    except e0_origin.OriginConfigError as exc:
        return _refuse(str(exc))

    app = e0_origin.OriginApp(config, bench=bench_identity())
    tls_dir = None
    try:
        if config.tls:
            tls_dir, cert, key = _write_tls_material(environ)
            server_config = uvicorn_config(app, ssl_certfile=cert, ssl_keyfile=key)
        else:
            server_config = uvicorn_config(app)
        server_config.load()
    except e0_origin.OriginConfigError as exc:
        return _refuse(str(exc))
    finally:
        if tls_dir is not None:
            shutil.rmtree(tls_dir, ignore_errors=True)
    sock = open_listener(port)
    server = uvicorn.Server(server_config)
    app.on_exhausted = lambda: setattr(server, "should_exit", True)
    app.startup({"listeners": [{"family": "ipv4", "bind": "0.0.0.0", "port": sock.getsockname()[1]}],
                 "uvicorn": {"version": uvicorn.__version__, "proxy_headers": server_config.proxy_headers,
                             "http": "h11", "lifespan": "off", "access_log": False, "server_header": False},
                 "openssl": {"version": capability["version"], "text": capability["openssl"],
                             "ed25519": capability["ed25519"]},
                 "ttl_seconds": config.ttl_seconds})

    def expire():
        app.checkpoint("ttl")
        server.should_exit = True

    timer = threading.Timer(config.ttl_seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        server.run(sockets=[sock])
    finally:
        timer.cancel()
        app.checkpoint("stop")
    return 0


def main(environ=None) -> int:
    environ = dict(os.environ if environ is None else environ)
    if environ.get("E0_ROLE") != "origin":
        print("E0_ROLE=origin attendu", file=sys.stderr)
        return e0_guard.EXIT_USAGE
    return run_origin(environ)


if __name__ == "__main__":
    sys.exit(main())
