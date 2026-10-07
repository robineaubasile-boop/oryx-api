"""Point d'entrée du worker interne R1-D1 (service Railway
oryx-evaluation-worker) :

    python scripts/run_evaluation_worker.py
    # équivalent : python -m core.evaluation_worker

Configuration et garanties : voir core/evaluation_worker.py et
docs/evaluation_worker.md. Aucun serveur HTTP, aucune route.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.evaluation_worker import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
