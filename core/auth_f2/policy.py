"""Paramètres figés de la politique F2 (C.2.1 §6, C.2.1-A).

Toute modification de ces valeurs est une décision d'architecture :
POLICY_VERSION est enregistré sur chaque événement anti-abus.
"""
from datetime import timedelta

POLICY_VERSION = "f2-c2-v1"
OPERATION_SCHEMA_VERSION = "f2-c2-v1"
SESSION_SCHEMA_VERSION = "f2-c2-v1"
RESULT_VERSION = "f2-c2-result-v1"
DELIVERY_PROOF_VERSION = "f2-c2-delivery-v1"

OTP_TTL = timedelta(seconds=600)
OTP_MAX_FAILURES_PER_REQUEST = 5
EMAIL_FAILURE_WINDOW = timedelta(minutes=15)
EMAIL_FAILURE_THRESHOLD = 5
EMAIL_BLOCK_DURATION = timedelta(minutes=15)
ISSUE_MIN_INTERVAL = timedelta(seconds=60)
ISSUE_EMAIL_WINDOW = timedelta(hours=1)
ISSUE_EMAIL_LIMIT = 5
ISSUE_IP_WINDOW = timedelta(hours=1)
ISSUE_IP_LIMIT = 20
VERIFY_IP_WINDOW = timedelta(minutes=5)
VERIFY_IP_LIMIT = 30

CANDIDATE_TTL = timedelta(minutes=10)
OPERATION_TTL = timedelta(minutes=10)
RESULT_RESUME_WINDOW = timedelta(minutes=5)
SESSION_ABSOLUTE_TTL = timedelta(days=15)
INVITATION_TTL = timedelta(days=7)

# Marqueurs de refus : au plus un par (sujet, type, tranche de 5 minutes),
# garanti par l'index unique partiel uq_auth_abuse_events_refusal_bucket.
REFUSAL_BUCKET = timedelta(minutes=5)

# Rotation K_abuse : une version rétrogradée reste comptée au moins
# 90 minutes (plus longue fenêtre : 1 h ; blocage : 15 min ; marge des
# transactions en vol bornées par statement_timeout).
ABUSE_KEY_RETENTION = timedelta(minutes=90)

# Purge (dormante, jamais planifiée par PR 2) : jamais en deçà de deux
# heures, pour ne jamais effacer un événement d'une fenêtre active.
ABUSE_PURGE_MIN_AGE = timedelta(hours=2)

# Une vérification fournisseur restée 'in_flight' au-delà de ce délai
# (processus interrompu) est requalifiée 'uncertain' : jamais un succès.
PROVIDER_STALE_AFTER = timedelta(seconds=60)

# Garde-fous de verrouillage des transactions F2 (SET LOCAL) : une attente
# ou une saturation se traduit par un refus, jamais par une admission.
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "10s"

# Longueur maximale d'un code soumis examinée (au-delà : mal formé, sans
# autre traitement).
SUBMITTED_CODE_MAX_CHARS = 64
