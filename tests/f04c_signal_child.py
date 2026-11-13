"""Processus enfant de tests/test_f04c_signals.py (Q1-10, signaux RÉELS).

Uvicorn 0.27.0 dans le FIL PRINCIPAL (gestionnaires de signaux d'Uvicorn
actifs) sert le VRAI point d'entrée F04-C (banc tests/f04c_support.py) ; un
client local envoie PUT /me/level, dont le fil se bloque au point « locked »
du runner (transaction ouverte) jusqu'à la création du fichier de relâche.
Journal JSON-lines écrit au fil de l'eau (survit à l'arrêt du serveur).

argv : <journal.jsonl> <relâche> <timeout_graceful|none> <drain_timeout|none>
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn  # noqa: E402
from _pytest.monkeypatch import MonkeyPatch  # noqa: E402

from tests import f04c_support as f  # noqa: E402

journal_path, release_path, graceful, drain = sys.argv[1:5]
graceful = None if graceful == "none" else float(graceful)
drain = None if drain == "none" else float(drain)
out = open(journal_path, "a", buffering=1, encoding="utf-8")


class FileJournal(f.Journal):
    def rec(self, kind, **fields):
        event = super().rec(kind, **fields)
        out.write(json.dumps(event, default=str) + "\n")
        return event


journal = FileJournal()
patch = MonkeyPatch()
f.install_tracing(patch, journal)
bench = f.Bench(os.environ["ORYX_TEST_DATABASE_URL"], journal, drain_timeout=drain)
bench.install_runner_hook(patch)
issued = bench.issue_session()


def blocked():
    journal.rec("HOOK_BLOCKED", checked_out=bench.engine.pool.checkedout())
    while not os.path.exists(release_path):
        time.sleep(0.01)
    journal.rec("HOOK_RELEASED")


bench.hooks["locked"] = blocked
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(f.Probe(bench.envelope, journal), lifespan="on", log_level="warning",
                                       http="h11", proxy_headers=False, timeout_graceful_shutdown=graceful))


def client():
    while not server.started:
        time.sleep(0.01)
    c = f.RawClient(port)
    c.send(f.http_bytes("PUT", f.ME_LEVEL, f.mutation_headers(bench, issued, "victim"), f.level_body()))
    journal.rec("CLIENT_RESPONSE", status=f.status_of(c.read_all(timeout=120)))


threading.Thread(target=client, name="f04c-client", daemon=True).start()
journal.rec("CHILD_READY", main_thread=threading.current_thread() is threading.main_thread())
server.run(sockets=[sock])
journal.rec("UVICORN_RUN_RETURNED", force_exit=server.force_exit, state=bench.envelope.state,
            outcome=bench.envelope.outcome, level=bench.level(),
            nondaemon=sorted(t.name for t in threading.enumerate() if not t.daemon
                             and t is not threading.current_thread()))
# Fin du module : l'interpréteur attend les fils non démons (fil AnyIO encore bloqué).
