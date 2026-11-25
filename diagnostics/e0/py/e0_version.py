"""E0 — constantes versionnées du banc (protocole E0 V0.1c, spécification V0.2-R3).

Code TEMPORAIRE et ISOLÉ du runtime Oryx : bibliothèque standard seulement,
aucun import d'un module Oryx. Ce module ne contient que des constantes :
identifiants de format, noms d'en-têtes, domaines de séparation et
correspondance phase -> audience (PA15).
"""
import re

PROTOCOL = "E0-V0.1c"
FORMAT = 1
SPEC = "E0-BENCH-V0.2-R3"
PROTOCOL_DOCUMENT = "docs/act_l1_f04d_p1b_e0_protocol_v01c.md"
PROTOCOL_SHA256 = "b25af66a9c387532961dfa6a9811a31502d6fe38431e1bb538d71497ad320b27"

# En-têtes HTTP (minuscules).
ATTESTATION_HEADER = "x-oryx-e0-attestation"
ORIGIN_HEADER = "x-e0-origin"
EDGE_HEADER = "x-e0-edge"
EDGE_DIAG_HEADER = "x-e0-edge-diag"
RUN_HEADER = "x-e0-run"
SEQ_HEADER = "x-e0-seq"
CASE_HEADER = "x-e0-case"
CMP_PRE_KEY_HEADER = "x-e0-cmp-pre-key"
CMP_PRE_MAC_HEADER = "x-e0-cmp-pre-mac"
FAULT_HEADER = "x-e0-fault"

# Préfixes d'en-têtes neutralisés par l'edge (PA13).
NEUTRALIZED_HEADER_PREFIXES = ("x-oryx-", "x-e0-edge-")

# Domaines de séparation (jamais réutilisés entre usages).
ATTESTATION_DOMAIN = b"oryx-e0-attestation/v1\n"
IPCOMPARE_PRE_DOMAIN = "oryx-e0-ipcompare/pre/v1"
IPCOMPARE_POST_DOMAIN = "oryx-e0-ipcompare/post/v1"
IPCOMPARE_KEY_ID_DOMAIN = "oryx-e0-ipcompare/post/v1/key-id"
N2_DOMAIN = b"oryx-e0-n2/v1\x00"
PSEUDONYM_DOMAIN = b"oryx-e0-pseudonym/v1\x00"

# Attestation v1 (§ 5.1).
ATTESTATION_VERSION = "1"
ATTESTATION_PREFIX = "v1."
ATTESTATION_MAX_BYTES = 4096
ATTESTATION_FIELDS = ("v", "aud", "kid", "ts", "nonce", "ip", "c", "m", "h", "hi", "t")
SIGNED_CLASS = "V"

# Phases et audiences (PA15). La phase 1c (tunnel) se situe entre 1b et 2 :
# elle relève de l'audience des « phases 1b à 2 » du protocole (§ 6.1, PA15).
AUD_1A = "oryx-e0-1a"
AUD_2 = "oryx-e0-2"
AUDIENCES = (AUD_1A, AUD_2)
PHASES = ("1a", "1b", "1c", "2")
PHASE_AUDIENCE = {"1a": AUD_1A, "1b": AUD_2, "1c": AUD_2, "2": AUD_2}
AUDIENCE_KID_TAG = {AUD_1A: "1a", AUD_2: "2"}
KID_PATTERN = re.compile(r"e0-(1a|2)-[0-9a-f]{16}")

# Préfixe protégé de l'origine (§ 2.2).
GUARDED_SEGMENTS = ("e0", "guarded")

# Réponse de refus V-ALL : octets constants recopiés de F04-C § 4 (sans import).
REFUSAL_STATUS = 503
REFUSAL_BODY = b'{"error":"temporarily_unavailable"}'
REFUSAL_NO_STORE_HEADERS = (
    ("cache-control", "no-store"),
    ("pragma", "no-cache"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
)

# Codes de refus du vérificateur gardé (§ 2.2), dans l'ordre PA8.
REFUSAL_CODES = ("att_absent", "att_multiple", "grammar", "aud_not_current_phase", "kid_unknown", "kid_retired",
                 "sig_invalid", "ts_future", "ts_expired", "bind_method", "bind_h", "bind_hi", "bind_target",
                 "ip_not_v", "replay_in_boot")

# Codes de motif du contrôle négatif pré-signature (§ 2.3, étape 2).
CMP_MALFORMED = "cmp_malformed"
CMP_DISAGREEMENT = "cmp_disagreement"


def line_header(record_type: str, bench: str) -> dict:
    """Champs communs à toute ligne JSONL (§ 5)."""
    if not isinstance(record_type, str) or not record_type:
        raise ValueError("type d'enregistrement absent")
    if not isinstance(bench, str) or not bench:
        raise ValueError("SHA du banc absent")
    return {"e0": record_type, "fmt": FORMAT, "protocol": PROTOCOL, "bench": bench}
