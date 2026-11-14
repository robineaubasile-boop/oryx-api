# Banc de diagnostic P1-Q (ACT-L1 / F04-D) — TEMPORAIRE

Banc **isolé** de qualification de la confiance réseau Railway (réserve P1). Il ne fait pas partie du runtime
Oryx : aucun module Oryx n'est importé, aucun fichier du runtime ne le référence, aucune base ni aucun secret
n'est utilisé. Sa conservation au-delà de P1-Q exigerait une décision ultérieure du fondateur.

- Protocole : `docs/act_l1_f04d_p1q_protocol_v01.md`
- Guide Railway (**NE PAS EXÉCUTER sans autorisation explicite du fondateur**) : `docs/act_l1_f04d_p1q_railway_guide_v01.md`
- Modèle de rapport : `docs/act_l1_f04d_p1q_report_template_v01.md`
- Tests locaux : `tests/test_p1q_bench.py`, `tests/test_p1q_receiver_http.py`, `tests/test_p1q_analysis.py`,
  `tests/test_p1q_targeting.py`

| Fichier | Rôle |
|---|---|
| `p1q_receiver.py` | Service A : ASGI minimal + Uvicorn 0.27.0, observation pseudonymisée de chaque requête |
| `p1q_prober.py` | Sonde HTTP/1.1 brute, catalogue Q1-Q7 (`--list`), identification du récepteur, cibles autorisées seulement, sortie JSONL + empreinte |
| `p1q_analyze.py` | Couverture exacte et intégrité de collecte ; verdicts **proposés** (PASS / FAIL / INCONCLUSIVE), jamais un GO |
| `p1q_redact.py` | Classification IANA, pseudonymisation HMAC des adresses globales |
| `p1q_guard.py` | Refus d'un environnement non isolé et de toute cible de production |
| `p1q_main.py` | Lanceur Railway (`P1Q_ROLE`), durée de vie bornée (`P1Q_TTL_SECONDS`) |
| `Dockerfile`, `railway.json`, `requirements.txt` | Image et configuration des deux services temporaires |

Essai local rapide (Python 3.11 avec uvicorn 0.27.0) :

```bash
P1Q_ROLE=receiver PORT=18080 P1Q_LISTEN=v4 python p1q_main.py
```

```bash
python p1q_prober.py --role private --target http://127.0.0.1:18080 --allow-loopback --family 4 --out /tmp/p1q-local.jsonl
```

```bash
python p1q_analyze.py /tmp/p1q-local.jsonl
```

`--allow-loopback` est réservé aux tests locaux (refusé sur Railway). Un essai local ne qualifie **ni** l'edge
Railway **ni** le réseau privé, et l'analyseur le rend INCONCLUSIVE (IPv6, Q6, Q7 manquants) : P1 reste ouverte.
