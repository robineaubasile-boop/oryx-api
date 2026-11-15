# Banc E0 (ACT-L1 / F04-D / P1-B) — image TEMPORAIRE de l'origine de diagnostic e0-origin.
# NE PAS CONSTRUIRE NI DÉPLOYER sans feu vert de phase (G-1a et suivants).
# Aucune donnée, aucun secret, aucun code Oryx : seulement les modules e0_*.py
# de ce répertoire, uvicorn 0.27.0 (versions et empreintes de production) et
# l'outil openssl (Ed25519, validations D1 à D9 relevées au démarrage).
# Python 3.11.9 = runtime.txt du dépôt. Aucune variable ENV ajoutée : la garde
# de démarrage applique une liste blanche EXACTE (spécification § 2.2).
FROM python:3.11.9-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /e0
COPY requirements-origin.txt ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --no-deps \
    -r requirements-origin.txt
COPY e0_version.py e0_guard.py e0_redact.py e0_ipclass.py e0_ipcompare.py e0_ed25519.py e0_attest.py \
    e0_keysets.py e0_jsonl.py e0_origin.py e0_main.py ./
USER nobody
CMD ["python", "-B", "-u", "e0_main.py"]
