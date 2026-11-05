"""Outils des tests R1 PR 9-C2 : replay canonique et ACK F2 Décrypter
(dormants) contre un VRAI PostgreSQL, sous le VRAI runner et le registre de
PRODUCTION (vingt commandes ; aucun registre de test).

CanonicalEnv étend FinalizationEnv (R1 PR 9-C1-B) : livraisons produites par
la VRAIE chaîne B1 -> B2 -> C1-B (transports simulés, aucun réseau), puis
ReadCanonicalDecryptageDelivery / AcknowledgeDecryptageDelivery exécutées par
le VRAI runner, SANS trousseau Décrypter. Les états committés sont toujours
observés par une connexion indépendante.
"""
import dataclasses

import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2.runner import AUTHORIZED
from tests.authz_f2_pr9b1_support import CONV
from tests.authz_f2_pr9c1b_support import REPLY_BUSINESS, FinalizationEnv, next_turn


@dataclasses.dataclass(frozen=True)
class Finalized:
    """Tour C1-B finalisé : identité demandée par C2 et candidat d'origine."""
    reservation_id: object
    conversation_key: str
    assistant_turn_id: object
    candidate: object

    def replay(self, **changes):
        return c.ReadCanonicalDecryptageDelivery(**self._fields(changes))

    def ack(self, **changes):
        return c.AcknowledgeDecryptageDelivery(**self._fields(changes))

    def _fields(self, changes):
        fields = {"reservation_id": self.reservation_id, "conversation_key": self.conversation_key,
                  "assistant_turn_id": self.assistant_turn_id}
        fields.update(changes)
        return fields


class CanonicalEnv(FinalizationEnv):
    """FinalizationEnv + tours finalisés et runner C2 SANS trousseau Décrypter."""

    def finalized(self, issued, command=None, *, text=REPLY_BUSINESS, conversation_key=CONV):
        command = command or next_turn(conversation_key)
        _, lease, candidate = self.generate(issued, command, text=text)
        outcome = self.finalize(issued, candidate)
        assert outcome.status == AUTHORIZED and type(outcome.value) is dfin.DecryptageFinalized, outcome
        return Finalized(lease.reservation_id, command.conversation_key, outcome.value.assistant_delivery_id,
                         candidate)

    def c2(self, issued, command, **kwargs):
        """Runner de production SANS trousseau Décrypter (C2 n'en dépend pas)."""
        kwargs.setdefault("decryptage_keys", None)
        return self.run(issued, command, **kwargs)

    def delivery(self, delivery_id):
        found = self.rows("SELECT *, response_payload::text AS payload_text, xmin::text AS xmin "
                          "FROM assistant_deliveries WHERE id = :d", d=delivery_id)
        return found[0] if found else None

    def link(self, delivery_id):
        found = self.rows("SELECT *, xmin::text AS xmin FROM decryptage_cognitive_links "
                          "WHERE assistant_delivery_id = :d", d=delivery_id)
        return found[0] if found else None

    def payload_text(self, delivery_id) -> str:
        return self.delivery(delivery_id)["payload_text"]

    def set_payload(self, delivery_id, payload_sql: str, **params):
        """Remplace le JSONB persisté (donnée de test explicite, texte SQL)."""
        with self.observer.begin() as conn:
            assert conn.execute(sa.text("UPDATE assistant_deliveries SET response_payload = " + payload_sql
                                        + " WHERE id = :d"), {"d": delivery_id, **params}).rowcount == 1

    def damage(self, sql: str, **params):
        """Donnée persistée volontairement incohérente (base jetable) : FK et
        déclencheurs contournés (session_replication_role = replica) ; les
        CHECK restent actifs."""
        with self.observer.begin() as conn:
            conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
            assert conn.execute(sa.text(sql), params).rowcount >= 1

    def update(self, table: str, assignment: str, where: str, **params):
        with self.observer.begin() as conn:
            assert conn.execute(sa.text(f"UPDATE {table} SET {assignment} WHERE {where}"), params).rowcount == 1

    def counts(self):
        out = super().counts()
        with self.observer.connect() as conn:
            out["user_statements_rows"] = conn.execute(sa.text("SELECT count(*) FROM user_statements")).scalar_one()
        return out


__all__ = ["CanonicalEnv", "Finalized"]
