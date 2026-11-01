"""Outils des tests R1 PR 9-B1 : réservations Décrypter F2 contre un VRAI
PostgreSQL, sous le VRAI runner et le registre de PRODUCTION (seize
commandes ; aucun registre de test, aucun chemin parallèle à BF2).

ReservationEnv étend BenchEnv (R1 PR 9-A : moteur F2, sessions F2 réelles,
moteur historique application_name oryx-legacy-test, observation par une
connexion indépendante) avec :

- un trousseau Décrypter FICTIF (clés D1 / D2, jamais des clés réelles,
  distinctes des clés F2 et portefeuille de test) ;
- l'observation exacte des réservations (xmin compris : inchangé = aucune
  écriture durable) et des conversations ;
- des manipulations de DONNÉES de test explicites (échéance ou backoff
  dépassés, état finalisé forcé) : le temps PostgreSQL n'est jamais simulé
  côté serveur, seules les lignes sont repositionnées ;
- la création d'une livraison historique par le VRAI service
  core.assistant_delivery.claim_delivery (Session ORM du moteur historique)
  ou par la VRAIE route api.decryptage (doubles des seuls appels externes).
"""
import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import api
import core.db
from core import assistant_delivery as ad
from core.authz_f2 import commands as c
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.runner import AuthorizedTransactionRunner
from tests.authz_f2_pr9a_support import BenchEnv, Externals
from tests.authz_f2_support import USER_A

# Clés FICTIVES du trousseau Décrypter de test.
D1 = bytes(range(100, 132))
D2 = bytes(range(200, 232))
CONV = "conv-pr9b1-a"


def decryptage_keyring(keys=None, current="d1") -> DecryptageKeyring:
    return DecryptageKeyring(keys if keys is not None else {"d1": D1, "d2": D2}, current)


def reserve_command(turn=None, conversation_key=CONV, ticker="MC.PA", question="Pourquoi ?", context="",
                    last_method_id=None) -> c.ReserveDecryptageTurn:
    return c.ReserveDecryptageTurn(conversation_key, turn or uuid.uuid4(), ticker, question, context, last_method_id)


class ReservationEnv(BenchEnv):
    def __init__(self, pg_url):
        super().__init__(pg_url)
        self.decryptage_keys = decryptage_keyring()

    def b1_runner(self, *, decryptage_keys=False, hook=None, clock=None, engine=None):
        kwargs = {"fault_hook": hook or self.hook, "portfolio_keyring": self.portfolio_keys,
                  "decryptage_keyring": self.decryptage_keys if decryptage_keys is False else decryptage_keys}
        if clock is not None:
            kwargs["clock"] = clock
        return AuthorizedTransactionRunner(engine or self.engine, self.keyring, **kwargs)

    def run(self, issued, command, **kwargs):
        return self.b1_runner(**kwargs).run(issued.credential(), command)

    # -- observation ------------------------------------------------------

    def reservations(self, user_id=USER_A):
        return self.rows("SELECT *, xmin::text AS xmin FROM decryptage_turn_reservations WHERE user_id = :u "
                         "ORDER BY created_at, id", u=user_id)

    def all_reservations(self):
        return self.rows("SELECT *, xmin::text AS xmin FROM decryptage_turn_reservations ORDER BY created_at, id")

    def reservation(self, reservation_id):
        found = self.rows("SELECT *, xmin::text AS xmin FROM decryptage_turn_reservations WHERE id = :i",
                          i=reservation_id)
        return found[0] if found else None

    def reservation_text(self, reservation_id) -> str:
        """Ligne entière rendue en texte par PostgreSQL (recherche d'un secret)."""
        return self.rows("SELECT t::text AS row FROM decryptage_turn_reservations t WHERE id = :i",
                         i=reservation_id)[0]["row"]

    def conversations(self):
        return self.rows("SELECT conversation_key, user_id, xmin::text AS xmin FROM conversation_identities "
                         "ORDER BY conversation_key")

    def full_snapshot(self):
        state = self.snapshot()
        state["decryptage_turn_reservations"] = self.all_reservations()
        return state

    # -- données de test explicites ---------------------------------------

    def force(self, reservation_id, assignment: str):
        with self.observer.begin() as conn:
            assert conn.execute(sa.text(f"UPDATE decryptage_turn_reservations SET {assignment} WHERE id = :i"),
                                {"i": reservation_id}).rowcount == 1

    def expire(self, reservation_id):
        self.force(reservation_id, "lease_expires_at = clock_timestamp() - interval '1 second'")

    def end_backoff(self, reservation_id):
        self.force(reservation_id, "retry_not_before_at = clock_timestamp() - interval '1 second'")

    def claim_legacy(self, user_id, conversation_key, turn_id, *, text="Réponse historique."):
        """Livraison historique par le VRAI service (identité complète du tour)."""
        with Session(self.legacy_engine) as db:
            delivery, created = ad.claim_delivery(
                db, user_id=user_id, conversation_key=conversation_key, surface=ad.DECRYPTAGE_SURFACE,
                source_user_turn_id=turn_id, delivery_ordinal=1,
                request_fingerprint=hashlib.sha256(f"legacy:{turn_id}".encode()).hexdigest(),
                visible_content_fingerprint=ad.visible_content_fingerprint(text),
                response_payload={"analysis": text}, private_metadata={},
                delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
            assert created
            db.commit()
            return delivery.id


@pytest.fixture
def legacy_route(env, monkeypatch):
    """Route historique /decryptage réelle (Sessions du moteur historique),
    doubles des seuls appels externes."""
    fake = Externals()
    monkeypatch.setattr(core.db, "SessionLocal", env.Sessions)
    monkeypatch.setattr(api.anthropic, "Anthropic", fake)
    monkeypatch.setattr(api, "fetch_financial_data", fake.fetch)
    monkeypatch.setattr(api, "normalize_ticker", fake.resolve)
    return fake


def legacy_decryptage(env, fake, user_id, conversation_key, turn_id, *, question="Pourquoi ?"):
    """UN appel réel de api.decryptage pour ce tour (Session historique)."""
    fake.texts = ["Réponse historique.\n<!--ORYX_STEP:business-->"]
    with env.Sessions() as session:
        return api.decryptage(api.DecryptageRequest(
            ticker="MC.PA", question=question, context="", last_method_id=None, user_id=user_id,
            conversation_key=conversation_key, client_turn_id=str(turn_id)), db=session)

