"""R1-C4 : affinité conversationnelle des CognitiveEvents Décrypter (EXPAND).

Revision ID: 0013_decryptage_conv_affinity
Revises: 0012_decryptage_cognitive_links
Create Date: 2026-10-06

Migration du chantier R1-C4 (« Conversation affinity & parallel
CognitiveEvent safety »). Fichier nommé selon la spec
(0013_decryptage_conversation_affinity) ; identifiant de révision court
(29 caractères) car alembic_version.version_num est un VARCHAR(32) et le nom
complet en fait 37.

Étend UNIQUEMENT decryptage_cognitive_links :

- colonne input_context_event_id UUID NULL (FK cognitive_events.id, sans ON
  DELETE / ON UPDATE) : CognitiveEvent auquel la conversation du tour était
  effectivement ancrée (dernière livraison R1-C4 delivered + captured de
  cette conversation_key) AVANT le tour utilisateur ;
- colonne response_context_event_id UUID NULL (FK cognitive_events.id, sans
  ON DELETE / ON UPDATE) : CognitiveEvent existant auquel la réponse
  assistant de CE tour est destinée, figé sous verrou à la capture côté
  entrée (l'event open de l'AnalysisSession cible à cet instant, ou NULL).
  Seul candidat de continuation à l'ACK (stale_delivery s'il a été fermé) :
  aucun event apparu après la capture ne peut devenir rétroactivement la
  cible de la réponse ;
- vocabulaires fermés étendus (anciennes valeurs V1 toujours valides) :
  input_action + 'context_switched' (la conversation se détache de son
  ancre, qui reste inchangée) ; response_action + 'stale_delivery' (réponse
  réellement rendue, mais destinée à un event fermé entre-temps : aucun
  rattachement) ;
- CHECK ck_decryptage_cognitive_links_context_switched : context_switched
  => input_event_id NULL, input_context_event_id NOT NULL,
  context_exit_event_id NULL ;
- CHECK ck_decryptage_cognitive_links_stale_delivery : stale_delivery =>
  response_event_id NULL et support_trace_id NULL ;
- CHECK ck_decryptage_cognitive_links_runtime_version : capture_version
  connue ; une ligne V1 garde sa sémantique (jamais d'input_context_event_id,
  de response_context_event_id, de context_switched ni de stale_delivery) ;
  une ligne V2 ne ferme jamais d'event sur navigation (context_exit_event_id
  NULL, jamais event_closed_/event_abandoned_context_change), n'a
  d'input_context_event_id NULL que pour no_open_event, une contribution V2
  vise exactement l'event d'ancrage (input_event_id =
  input_context_event_id), un tour ancré destine sa réponse à son ancre
  (response_context_event_id = input_context_event_id) et un context switch
  ne la destine jamais à l'event quitté ;
- CHECK ck_decryptage_cognitive_links_response_attachment (V2) : continuation
  => response_event_id = response_context_event_id ; transition / fermeture
  terminale => response_context_event_id présent (et distinct du nouvel
  event) ; ouverture / no_cognitive_action => response_context_event_id
  NULL ; stale_delivery : quelconque.

Les CHECK de vocabulaire sont remplacés (DROP + ADD du même nom) ; les
autres CHECK de 0012 (capture_state, capture_state_fields, input_event,
response_refs) et l'index unique partiel d'ouverture sont inchangés. Aucune
colonne supprimée ni renommée, aucune autre table modifiée, aucun index,
aucun trigger, aucune fonction, aucun ENUM, aucun server_default. Aucune
donnée écrite hors alembic_version : AUCUN backfill (les liens V1 gardent
input_context_event_id NULL et leur ancienne sémantique ;
context_exit_event_id reste lisible pour l'historique V1).

Les lignes existantes (toutes V1) respectent les nouveaux CHECK : ils sont
validés à l'ajout (pas de NOT VALID).

Downgrade : retour exact à 0012 (CHECK d'origine, colonnes et FK
supprimées). Il échoue, sans rien perdre, si des lignes V2 utilisent déjà
context_switched ou stale_delivery (le CHECK d'origine les refuse).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0013_decryptage_conv_affinity"
down_revision: Union[str, Sequence[str], None] = "0012_decryptage_cognitive_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LINKS = "decryptage_cognitive_links"
COLUMN = "input_context_event_id"
FK = "decryptage_cognitive_links_input_context_event_id_fkey"
RESPONSE_COLUMN = "response_context_event_id"
RESPONSE_FK = "decryptage_cognitive_links_response_context_event_id_fkey"
RESPONSE_ATTACHMENT_CHECK = "ck_decryptage_cognitive_links_response_attachment"
INPUT_ACTION_CHECK = "ck_decryptage_cognitive_links_input_action"
RESPONSE_ACTION_CHECK = "ck_decryptage_cognitive_links_response_action"
CONTEXT_SWITCHED_CHECK = "ck_decryptage_cognitive_links_context_switched"
STALE_DELIVERY_CHECK = "ck_decryptage_cognitive_links_stale_delivery"
RUNTIME_VERSION_CHECK = "ck_decryptage_cognitive_links_runtime_version"

INPUT_ACTIONS_0012 = (
    "input_action IN ('no_open_event', 'no_user_contribution', 'contribution_appended', "
    "'event_closed_context_change', 'event_abandoned_context_change')"
)
RESPONSE_ACTIONS_0012 = (
    "response_action IS NULL OR response_action IN ('opened_event', 'continued_event', "
    "'continued_without_boundary_signal', 'transitioned_event', 'closed_terminal', "
    "'no_cognitive_action')"
)
INPUT_ACTIONS_0013 = (
    "input_action IN ('no_open_event', 'no_user_contribution', 'contribution_appended', "
    "'event_closed_context_change', 'event_abandoned_context_change', 'context_switched')"
)
RESPONSE_ACTIONS_0013 = (
    "response_action IS NULL OR response_action IN ('opened_event', 'continued_event', "
    "'continued_without_boundary_signal', 'transitioned_event', 'closed_terminal', "
    "'no_cognitive_action', 'stale_delivery')"
)
CONTEXT_SWITCHED = (
    "input_action <> 'context_switched' OR (input_event_id IS NULL AND input_context_event_id IS NOT NULL "
    "AND context_exit_event_id IS NULL)"
)
STALE_DELIVERY = (
    "response_action IS NULL OR response_action <> 'stale_delivery' OR (response_event_id IS NULL "
    "AND support_trace_id IS NULL)"
)
RUNTIME_VERSION = (
    "(capture_version = 'decryptage-cognitive-runtime-v1' AND input_context_event_id IS NULL "
    "AND response_context_event_id IS NULL AND input_action <> 'context_switched' "
    "AND (response_action IS NULL OR response_action <> 'stale_delivery')) "
    "OR (capture_version = 'decryptage-cognitive-runtime-v2' AND context_exit_event_id IS NULL "
    "AND input_action NOT IN ('event_closed_context_change', 'event_abandoned_context_change') "
    "AND (input_action = 'no_open_event') = (input_context_event_id IS NULL) "
    "AND (input_action <> 'contribution_appended' OR input_event_id = input_context_event_id) "
    "AND (input_action NOT IN ('contribution_appended', 'no_user_contribution') "
    "OR (response_context_event_id IS NOT NULL AND response_context_event_id = input_context_event_id)) "
    "AND (input_action <> 'context_switched' OR response_context_event_id IS NULL "
    "OR response_context_event_id <> input_context_event_id))"
)
RESPONSE_ATTACHMENT = (
    "capture_version <> 'decryptage-cognitive-runtime-v2' OR response_action IS NULL "
    "OR response_action = 'stale_delivery' "
    "OR (response_action IN ('continued_event', 'continued_without_boundary_signal') "
    "AND response_context_event_id IS NOT NULL AND response_event_id = response_context_event_id) "
    "OR (response_action IN ('transitioned_event', 'closed_terminal') "
    "AND response_context_event_id IS NOT NULL "
    "AND (response_event_id IS NULL OR response_event_id <> response_context_event_id)) "
    "OR (response_action IN ('opened_event', 'no_cognitive_action') AND response_context_event_id IS NULL)"
)


def upgrade() -> None:
    """Ajoute input_context_event_id et response_context_event_id (+ FK) et étend
    les CHECK (rien d'autre)."""
    op.add_column(LINKS, sa.Column(COLUMN, sa.Uuid(), nullable=True))
    op.add_column(LINKS, sa.Column(RESPONSE_COLUMN, sa.Uuid(), nullable=True))
    op.create_foreign_key(FK, LINKS, "cognitive_events", [COLUMN], ["id"])
    op.create_foreign_key(RESPONSE_FK, LINKS, "cognitive_events", [RESPONSE_COLUMN], ["id"])
    op.drop_constraint(INPUT_ACTION_CHECK, LINKS, type_="check")
    op.create_check_constraint(INPUT_ACTION_CHECK, LINKS, INPUT_ACTIONS_0013)
    op.drop_constraint(RESPONSE_ACTION_CHECK, LINKS, type_="check")
    op.create_check_constraint(RESPONSE_ACTION_CHECK, LINKS, RESPONSE_ACTIONS_0013)
    op.create_check_constraint(CONTEXT_SWITCHED_CHECK, LINKS, CONTEXT_SWITCHED)
    op.create_check_constraint(STALE_DELIVERY_CHECK, LINKS, STALE_DELIVERY)
    op.create_check_constraint(RUNTIME_VERSION_CHECK, LINKS, RUNTIME_VERSION)
    op.create_check_constraint(RESPONSE_ATTACHMENT_CHECK, LINKS, RESPONSE_ATTACHMENT)


def downgrade() -> None:
    """Retour exact à 0012 (échoue sans perte si des valeurs V2 existent)."""
    op.drop_constraint(RESPONSE_ATTACHMENT_CHECK, LINKS, type_="check")
    op.drop_constraint(RUNTIME_VERSION_CHECK, LINKS, type_="check")
    op.drop_constraint(STALE_DELIVERY_CHECK, LINKS, type_="check")
    op.drop_constraint(CONTEXT_SWITCHED_CHECK, LINKS, type_="check")
    op.drop_constraint(RESPONSE_ACTION_CHECK, LINKS, type_="check")
    op.create_check_constraint(RESPONSE_ACTION_CHECK, LINKS, RESPONSE_ACTIONS_0012)
    op.drop_constraint(INPUT_ACTION_CHECK, LINKS, type_="check")
    op.create_check_constraint(INPUT_ACTION_CHECK, LINKS, INPUT_ACTIONS_0012)
    op.drop_constraint(RESPONSE_FK, LINKS, type_="foreignkey")
    op.drop_constraint(FK, LINKS, type_="foreignkey")
    op.drop_column(LINKS, RESPONSE_COLUMN)
    op.drop_column(LINKS, COLUMN)
