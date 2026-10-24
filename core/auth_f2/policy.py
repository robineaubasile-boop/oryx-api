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

# --------------------------------------------------------------------------
# R1 PR 4 (C.4.5 §2.2 et §2.4) : admission HTTP et quotas métier.
# --------------------------------------------------------------------------

# Version de politique des événements écrits par PR 4 (probes d'admission
# HTTP et resume_attempt) ; les événements PR 2 gardent POLICY_VERSION.
HTTP_POLICY_VERSION = "f2-pr4-http-v1"

# Sept actions HTTP (liste fermée, CHECK de 0019).
HTTP_ACTIONS = (
    "invitation_begin", "invitation_complete", "invitation_resume", "email_link_start", "email_link_resend",
    "email_link_verify", "email_link_result",
)
# Budget commun : tous les probes d'une même IP, toutes actions confondues.
HTTP_COMMON_LIMIT = 60
HTTP_COMMON_WINDOW = timedelta(minutes=5)
# Budgets propres à une action (filtre http_action) ; les trois routes OTP
# de liaison n'en ont pas, elles gardent en plus les quotas OTP de PR 2.
HTTP_ACTION_BUDGETS = {
    "invitation_begin": (20, timedelta(hours=1)),
    "invitation_complete": (30, timedelta(minutes=5)),
    "invitation_resume": (30, timedelta(minutes=5)),
    "email_link_result": (30, timedelta(minutes=5)),
}

# Ouvertures d'une invitation reconnue (opérations invitation_activation
# conservées, quel que soit leur statut).
INVITATION_OPEN_WINDOW = timedelta(hours=1)
INVITATION_OPEN_WINDOW_LIMIT = 3
INVITATION_OPEN_TOTAL_LIMIT = 12

# Reprise d'activation et consultation du résultat de liaison : admissions
# par opération (resume_attempt), toutes IP et versions de clé confondues.
OPERATION_RESUME_WINDOW = timedelta(minutes=5)
OPERATION_RESUME_LIMIT = 10
