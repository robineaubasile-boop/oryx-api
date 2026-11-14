"""P1-Q — lanceur unique des deux services temporaires Railway.

P1Q_ROLE=receiver : service A (p1q_receiver), arrêt automatique après
                    P1Q_TTL_SECONDS (défaut 14400 s = 4 h) ;
P1Q_ROLE=prober   : service B (p1q_prober, rôle private) : exécute la matrice
                    privée une fois au démarrage (sauf P1Q_PROBE_ON_START=0),
                    puis reste inactif jusqu'à P1Q_TTL_SECONDS pour permettre
                    des relances par `railway ssh`, et s'arrête.

La configuration Railway du banc (railway.json) fixe restartPolicyType NEVER :
un service arrêté (TTL échu ou erreur) n'est pas relancé et ne consomme plus.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_TTL_SECONDS = 14400
MAX_TTL_SECONDS = 86400


def ttl_seconds(environ) -> int:
    value = int(environ.get("P1Q_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
    if not 60 <= value <= MAX_TTL_SECONDS:
        raise SystemExit("P1Q_TTL_SECONDS hors bornes [60, 86400]")
    return value


def run_receiver(environ) -> int:
    import p1q_receiver
    from p1q_guard import forbidden_variables

    banned = forbidden_variables(environ)
    if banned:
        print("p1q-receiver: environnement NON isolé ; variables : " + ", ".join(banned), file=sys.stderr)
        return p1q_receiver.EXIT_ENVIRONMENT_NOT_ISOLATED
    ttl = ttl_seconds(environ)
    server, sockets, receiver = p1q_receiver.build_server(environ)
    print("P1Q-STARTUP " + json.dumps(
        {"p1q": "receiver-startup", "protocol": p1q_receiver.PROTOCOL_VERSION, "boot_id": receiver.boot_id,
         "mode": receiver.mode, "uvicorn": receiver.settings, "inventory": receiver.inventory, "ttl_seconds": ttl},
        ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)

    def expire():
        print("P1Q-TTL receiver: durée de vie échue, arrêt", flush=True)
        server.should_exit = True

    timer = threading.Timer(ttl, expire)
    timer.daemon = True
    timer.start()
    server.run(sockets=sockets)
    return 0


def run_prober(environ) -> int:
    import p1q_prober

    ttl = ttl_seconds(environ)
    deadline = time.monotonic() + ttl
    status = 0
    if environ.get("P1Q_PROBE_ON_START", "1") != "0":
        argv = ["--role", "private", "--prefix"]
        status = p1q_prober.main(argv, environ)
        print(f"P1Q-PROBER-EXIT {status}", flush=True)
    while time.monotonic() < deadline:
        time.sleep(min(60.0, max(0.0, deadline - time.monotonic())))
    print("P1Q-TTL prober: durée de vie échue, arrêt", flush=True)
    return status


def main(environ=None) -> int:
    environ = dict(os.environ if environ is None else environ)
    role = environ.get("P1Q_ROLE")
    if role == "receiver":
        return run_receiver(environ)
    if role == "prober":
        return run_prober(environ)
    print("P1Q_ROLE attendu : receiver ou prober", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
