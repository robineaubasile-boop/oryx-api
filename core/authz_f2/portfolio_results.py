"""Résultats des écritures portefeuille et reçus durables (R1 PR 7) — pur.

Version : RESULT_VERSION = "portfolio-write-result-v1". Le résultat est
construit, validé et sérialisé AVANT le COMMIT, dans la transaction qui
écrit la position et le reçu (portfolio_write_idempotency.result_payload,
JSONB) ; un rejeu le reconstruit depuis le reçu par le même renderer
versionné, qui rend les mêmes octets.

Payloads fermés (clés exactes) :

  CREATE (201) / UPDATE (200) :
    operation_id, operation_type, position_id, outcome ("created" |
    "replaced"), position (vue publique fermée, voir view_payload)
  DELETE (200) :
    operation_id, operation_type, position_id, outcome = "deleted",
    deleted_revision

Révisions transportées en chaîne décimale canonique ; position_id en entier
JSON ; aucun user_id, aucun secret, aucune date.

JSON canonique : compact, clés triées, ensure_ascii=False, UTF-8, jamais de
NaN ni d'Infinity JSON natifs (les valeurs spéciales historiques sont des
chaînes binary64-decimal-v1), 16 Kio au plus. Toute structure, version,
clé, type ou borne inattendue lève ResultInvalid : le reçu n'est jamais
réinterprété (503, aucune réexécution).
"""
import json
import uuid
from dataclasses import dataclass

from core.authz_f2 import commands as c
from core.authz_f2 import portfolio_fingerprint as fp
from core.authz_f2 import portfolio_values as v

RESULT_VERSION = "portfolio-write-result-v1"
MAX_RESULT_BYTES = 16 * 1024
CREATED, REPLACED, DELETED = "created", "replaced", "deleted"
OUTCOMES = {fp.CREATE: CREATED, fp.UPDATE: REPLACED, fp.DELETE: DELETED}
STATUSES = {fp.CREATE: 201, fp.UPDATE: 200, fp.DELETE: 200}
VIEW_KEYS = frozenset({"position_id", "position_revision", "ticker", "quantity", "purchase_price", "target_percent",
                       "envelope", "numeric_encoding", "data_status", "validation_issues"})
WRITE_KEYS = frozenset({"operation_id", "operation_type", "position_id", "outcome", "position"})
DELETE_KEYS = frozenset({"operation_id", "operation_type", "position_id", "outcome", "deleted_revision"})


class ResultInvalid(ValueError):
    """Résultat ou reçu hors contrat : refus fermé (503), jamais réexécuté."""


@dataclass(frozen=True, slots=True)
class PortfolioWriteResult:
    """Résultat committé (ou relu d'un reçu) : statut HTTP et corps JSON
    canonique, avec les champs que la route vérifie avant de répondre."""
    operation_type: str
    operation_id: uuid.UUID
    position_id: int
    status: int
    view: object             # PositionView (CREATE / UPDATE) ou None (DELETE)
    deleted_revision: object  # int (DELETE) ou None
    body: bytes


def canonical_json(obj) -> bytes:
    try:
        return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ResultInvalid("JSON non sérialisable") from None


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("clé JSON dupliquée")
        out[key] = value
    return out


def _reject_number(_):
    raise ValueError("nombre JSON non entier refusé")


def _reject_constant(_):
    raise ValueError("constante JSON refusée")


def parse_json(text):
    """Texte JSON (relu de PostgreSQL) -> objet ; doublons, constantes et
    nombres non entiers refusés."""
    if type(text) is not str or len(text.encode("utf-8")) > 4 * MAX_RESULT_BYTES:
        raise ResultInvalid("payload illisible")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_float=_reject_number,
                          parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ResultInvalid("payload illisible") from None


def _revision_text(value) -> int:
    if (type(value) is not str or not c.REVISION_TEXT_RE.fullmatch(value)
            or not c.valid_revision(int(value))):
        raise ResultInvalid("révision transportée hors contrat")
    return int(value)


def view_payload(view) -> dict:
    if type(view) is not c.PositionView:
        raise ResultInvalid("vue de position attendue")
    return {"position_id": view.position_id, "position_revision": str(view.position_revision),
            "ticker": view.ticker, "quantity": view.quantity, "purchase_price": view.purchase_price,
            "target_percent": view.target_percent, "envelope": view.envelope,
            "numeric_encoding": v.NUMERIC_ENCODING, "data_status": view.data_status,
            "validation_issues": list(view.validation_issues)}


def view_from_payload(payload) -> c.PositionView:
    if type(payload) is not dict or set(payload) != VIEW_KEYS:
        raise ResultInvalid("vue : clés hors contrat")
    if payload["numeric_encoding"] != v.NUMERIC_ENCODING or type(payload["validation_issues"]) is not list:
        raise ResultInvalid("vue : encodage ou diagnostic hors contrat")
    try:
        view = c.PositionView(payload["position_id"], _revision_text(payload["position_revision"]),
                              payload["ticker"], payload["quantity"], payload["purchase_price"],
                              payload["target_percent"], payload["envelope"], payload["data_status"],
                              tuple(payload["validation_issues"]))
    except (TypeError, ValueError):
        raise ResultInvalid("vue hors contrat") from None
    if view_payload(view) != payload:
        raise ResultInvalid("vue non canonique")
    return view


def build_payload(operation_type: str, operation_id: uuid.UUID, position_id: int, *, view=None,
                  deleted_revision=None) -> dict:
    if operation_type not in OPERATION_SET or type(operation_id) is not uuid.UUID:
        raise ResultInvalid("opération hors contrat")
    payload = {"operation_id": str(operation_id), "operation_type": operation_type, "position_id": position_id,
               "outcome": OUTCOMES[operation_type]}
    if operation_type == fp.DELETE:
        if view is not None or not c.valid_revision(deleted_revision):
            raise ResultInvalid("suppression hors contrat")
        payload["deleted_revision"] = str(deleted_revision)
    else:
        if deleted_revision is not None:
            raise ResultInvalid("écriture hors contrat")
        payload["position"] = view_payload(view)
    return payload


OPERATION_SET = frozenset(fp.OPERATION_TYPES)


def render_v1(operation_type: str, operation_id: uuid.UUID, position_id: int, payload) -> PortfolioWriteResult:
    """Payload (construit ou relu) -> résultat validé et corps canonique."""
    if operation_type not in OPERATION_SET or type(operation_id) is not uuid.UUID:
        raise ResultInvalid("opération hors contrat")
    if type(position_id) is not int or not 1 <= position_id <= c.MAX_POSITION_ID:
        raise ResultInvalid("position_id hors contrat")
    expected_keys = DELETE_KEYS if operation_type == fp.DELETE else WRITE_KEYS
    if type(payload) is not dict or set(payload) != expected_keys:
        raise ResultInvalid("payload : clés hors contrat")
    if (payload["operation_id"] != str(operation_id) or payload["operation_type"] != operation_type
            or type(payload["position_id"]) is not int or payload["position_id"] != position_id
            or payload["outcome"] != OUTCOMES[operation_type]):
        raise ResultInvalid("payload incohérent avec le reçu")
    view = deleted_revision = None
    if operation_type == fp.DELETE:
        deleted_revision = _revision_text(payload["deleted_revision"])
    else:
        view = view_from_payload(payload["position"])
        if view.position_id != position_id or (operation_type == fp.CREATE and view.position_revision != 0):
            raise ResultInvalid("vue incohérente avec le reçu")
    body = canonical_json(payload)
    if len(body) > MAX_RESULT_BYTES:
        raise ResultInvalid("résultat au-delà de 16 Kio")
    return PortfolioWriteResult(operation_type, operation_id, position_id, STATUSES[operation_type], view,
                                deleted_revision, body)


RENDERERS = {RESULT_VERSION: render_v1}


def location(result: PortfolioWriteResult):
    """En-tête Location déterministe d'un 201 (la position a pu être
    supprimée depuis : accepté)."""
    if result.operation_type != fp.CREATE:
        return None
    return f"/api/v2/me/portfolio/{result.position_id}"


def same_result(result, operation_type: str, operation_id: uuid.UUID) -> bool:
    """Vérification de la route avant de répondre : résultat de ce type,
    de cette opération, et corps identique au rendu de son propre payload."""
    if type(result) is not PortfolioWriteResult or result.operation_type != operation_type \
            or result.operation_id != operation_id:
        return False
    try:
        payload = build_payload(operation_type, operation_id, result.position_id, view=result.view,
                                deleted_revision=result.deleted_revision)
        again = render_v1(operation_type, operation_id, result.position_id, payload)
    except ResultInvalid:
        return False
    return again == result
