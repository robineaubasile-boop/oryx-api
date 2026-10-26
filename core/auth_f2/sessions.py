"""Lecture, verrouillage et jugement d'un SessionCredential (C.2.1 §9.6).

Interface interne commune au moteur F2 (core.auth_f2.service) et à la
frontière d'autorisation de R1 PR 5. Ces fonctions sont celles de
F2AuthService._lock_session / _session_valid / _session_prevalidated,
extraites SANS changement de comportement pour qu'un second appelant ne
recopie ni le HMAC ni les règles de validité :

- lock_session : verrous de la session présentée (oryx_sessions FOR SHARE
  puis génération, mode choisi par l'appelant), aucune décision ;
- read_session : mêmes lignes, sans verrou (pré-validation) ;
- session_valid : décision pure à un instant `at` lu par l'appelant après
  ses verrous — session et génération actives, `at < absolute_expires_at`,
  secret actif conforme à l'empreinte sous la version EXACTE de K_active
  enregistrée (version inconnue : refus, jamais de substitution).

Aucune écriture : la confirmation de livraison reste propre à
F2AuthService.authenticate_session.
"""
import uuid

import sqlalchemy as sa

from core.auth_f2.crypto import CryptoContractError, Redacted, active_secret_hash, b64url_decode_secret, hashes_equal
from core.auth_f2.keys import UnknownKeyVersion

GENERATION_LOCKS = ("FOR SHARE", "FOR UPDATE")


def decode_secret(secret):
    """Secret transporté (Redacted, base64url canonique) -> 32 octets, ou None."""
    if not isinstance(secret, Redacted):
        return None
    try:
        return b64url_decode_secret(secret.reveal())
    except CryptoContractError:
        return None


def _credential_parts(credential):
    # Import local : SessionCredential est défini par le service, qui
    # importe ce module.
    from core.auth_f2.service import SessionCredential

    if not isinstance(credential, SessionCredential) or not isinstance(credential.generation_id, uuid.UUID):
        return None
    secret = decode_secret(credential.secret)
    if secret is None:
        return None
    return credential.generation_id, secret


def lock_session(conn, credential, *, generation_lock: str):
    """(session, génération, secret) verrouillés, ou None."""
    if generation_lock not in GENERATION_LOCKS:
        raise ValueError("verrou de génération : FOR SHARE ou FOR UPDATE")
    parts = _credential_parts(credential)
    if parts is None:
        return None
    generation_id, secret = parts
    session_id = conn.execute(sa.text("SELECT session_id FROM oryx_session_generations WHERE id = :g"),
                              {"g": generation_id}).scalar()
    if session_id is None:
        return None
    session = conn.execute(sa.text("SELECT * FROM oryx_sessions WHERE id = :s FOR SHARE"),
                           {"s": session_id}).mappings().first()
    generation = conn.execute(sa.text(f"SELECT * FROM oryx_session_generations WHERE id = :g {generation_lock}"),
                              {"g": generation_id}).mappings().first()
    return session, generation, secret


def read_session(conn, credential):
    """(session, génération, secret) lus sans verrou ni écriture, ou None."""
    parts = _credential_parts(credential)
    if parts is None:
        return None
    generation_id, secret = parts
    generation = conn.execute(sa.text("SELECT * FROM oryx_session_generations WHERE id = :g"),
                              {"g": generation_id}).mappings().first()
    if generation is None:
        return None
    session = conn.execute(sa.text("SELECT * FROM oryx_sessions WHERE id = :s"),
                           {"s": generation["session_id"]}).mappings().first()
    return session, generation, secret


def session_valid(keyring, locked, at) -> bool:
    if locked is None:
        return False
    session, generation, secret = locked
    if session is None or generation is None:
        return False
    if session["status"] != "active" or generation["status"] != "active" or at >= session["absolute_expires_at"]:
        return False
    try:
        key = keyring.key("active", generation["hash_key_version"])
    except UnknownKeyVersion:
        return False
    return hashes_equal(active_secret_hash(key, session["id"], generation["id"], secret),
                        generation["active_secret_hash"])
