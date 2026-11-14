# Banc P1-Q (ACT-L1 / F04-D) — image TEMPORAIRE des services de diagnostic A et B.
# Aucune donnée, aucun secret, aucun code Oryx : seulement les fichiers p1q_*.py
# de ce répertoire et uvicorn 0.27.0 (versions et empreintes de production).
# Python 3.11.9 = runtime.txt du dépôt. Étiquette non épinglée par empreinte
# d'image : limite consignée dans docs/act_l1_f04d_p1q_protocol_v01.md § 9.
FROM python:3.11.9-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /p1q
COPY requirements.txt ./
RUN pip install --require-hashes --no-deps -r requirements.txt
COPY p1q_guard.py p1q_redact.py p1q_receiver.py p1q_prober.py p1q_analyze.py p1q_main.py ./
USER nobody
CMD ["python", "p1q_main.py"]
