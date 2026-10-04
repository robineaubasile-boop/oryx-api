"""Progression produit de la méthode construction_these de Décrypter
(T1-C1, rendue transactionnelle par R1-C1).

Extrait de api.py (_track_construction_these_progress /
_track_session_progress) SANS changement de règles produit : seule la
frontière transactionnelle change. Ce module mute et flush ; il ne fait
jamais commit() ni rollback(), n'ouvre aucune Session et n'avale aucune
erreur DB. La transaction appartient à l'orchestrateur /decryptage, qui
revendique d'abord la livraison assistant (core/assistant_delivery.py) puis
n'applique cette progression que pour la livraison gagnante : un échec ici
annule toute la transaction, livraison comprise.

Il contient aussi le vocabulaire FERMÉ des marqueurs d'étape privés
<!--ORYX_STEP:...--> émis par le modèle, et leur extraction du texte
visible. Le marqueur est une métadonnée privée : jamais affiché, jamais
renvoyé au frontend, jamais une preuve ni un SupportTrace.

Aucun appel à un service pédagogique T2+ (capture cognitive, observations,
longitudinal, inférence).
"""
import logging
import re
import uuid
from datetime import datetime, timezone

from core.models import AnalysisFact, AnalysisSession, InvestmentThesis, UserStatement

logger = logging.getLogger(__name__)

DECRYPTAGE_STEP_MARKERS = (
    "business",
    "moat",
    "chiffres",
    "valorisation",
    "risques",
    "swot_final",
)
FINAL_STEP = "swot_final"

# Tout marqueur <!--ORYX_STEP:...-->, reconnu ou non, est retiré du texte
# visible ; seul un marqueur du vocabulaire fermé est retenu.
STEP_MARKER_RE = re.compile(r"<!--ORYX_STEP:([^<>]*?)-->")

_FACT_FIELDS = [
    "operating_margin", "roe", "roic", "gross_margin_latest",
    "fcf_per_share", "revenue_growth", "net_cash", "eps",
]


class InvalidDecryptageStep(ValueError):
    """Étape hors du vocabulaire fermé DECRYPTAGE_STEP_MARKERS : aucune
    progression n'est jamais inventée."""


def extract_step_marker(text: str) -> tuple[str, str | None]:
    """Retourne (texte visible, marqueur reconnu ou None).

    Aucun marqueur : texte inchangé, None. Sinon tous les marqueurs sont
    retirés puis le texte est rstrip() (comportement historique) ; le
    premier marqueur est retenu s'il appartient à DECRYPTAGE_STEP_MARKERS,
    sinon None (journalisé, sans le texte de la réponse)."""
    match = STEP_MARKER_RE.search(text)
    if match is None:
        return text, None
    visible = STEP_MARKER_RE.sub("", text).rstrip()
    marker = match.group(1)
    if marker in DECRYPTAGE_STEP_MARKERS:
        return visible, marker
    logger.warning("[DECRYPTAGE] Marqueur d'étape inconnu ignoré (longueur=%d)", len(marker))
    return visible, None


def find_active_analysis_session(db, user_id, ticker):
    """Retourne l'AnalysisSession status=in_progress de ce user+ticker, ou
    None (T1-B2).

    Invariant applicatif : une seule session in_progress par user+ticker
    (aucune contrainte SQL ne le garantit). Si plusieurs existent par
    anomalie, on ne crée rien : on choisit déterministement la plus
    récente (started_at, puis updated_at, puis id) et on journalise."""
    sessions = db.query(AnalysisSession).filter(
        AnalysisSession.user_id == user_id,
        AnalysisSession.ticker == ticker,
        AnalysisSession.status == "in_progress",
    ).order_by(
        AnalysisSession.started_at.desc(),
        AnalysisSession.updated_at.desc(),
        AnalysisSession.id.desc(),
    ).all()
    if len(sessions) > 1:
        print(
            f"[ANALYSIS-SESSION ANOMALY] {len(sessions)} sessions in_progress pour "
            f"user={user_id}, ticker={ticker} : {[str(s.id) for s in sessions]} — "
            f"session retenue (la plus récente) : {sessions[0].id}"
        )
    return sessions[0] if sessions else None


def apply_construction_these_progress(
    db,
    *,
    user_id: str,
    ticker: str,
    step: str,
    thesis_text: str | None,
    data: dict | None,
) -> AnalysisSession | None:
    """Enregistre la progression construction_these de ce tour dans la
    transaction de l'appelant (mutation + flush ; jamais de commit,
    rollback ni capture d'erreur DB). Retourne l'AnalysisSession mise à
    jour, ou None si aucun suivi ne s'applique.

    AnalysisSession est l'unique identité d'une tentative (T1-C1) :
    - session in_progress existante pour user+ticker → réutilisée ;
    - sinon, step != swot_final → nouvelle tentative : nouvelle session ;
    - sinon (swot_final sans session active) → conversation libre après
      une analyse terminée : ignoré, aucune session artificielle (None).
    Une session completed/abandoned n'est jamais reprise.

    Tout ce qui est écrit pour la tentative porte
    analysis_session_id = session.id."""
    if step not in DECRYPTAGE_STEP_MARKERS:
        raise InvalidDecryptageStep("étape hors vocabulaire construction_these")
    active = find_active_analysis_session(db, user_id, ticker)
    if active is None and step == FINAL_STEP:
        print(f"[DB-TRACKING] Ignoré : user={user_id}, ticker={ticker}, swot_final hors tentative en cours (conversation libre après le bilan)")
        return None

    now = datetime.utcnow()
    now_aware = datetime.now(timezone.utc)

    is_new_session = active is None
    if is_new_session:
        active = AnalysisSession(
            id=uuid.uuid4(), user_id=user_id, ticker=ticker, status="in_progress",
            current_step=step, started_at=now_aware, updated_at=now_aware,
        )
        db.add(active)
        db.flush()
    previous_step = None if is_new_session else active.current_step
    is_new_swot = step == FINAL_STEP

    active.current_step = step
    active.updated_at = now_aware
    if is_new_swot:
        active.status = "completed"
        active.completed_at = now_aware

    # Le texte reçu à ce tour (thesis_text=question) répond à l'étape
    # PRÉCÉDENTE (previous_step), pas à l'étape que ce marqueur (step)
    # annonce : le marqueur reflète l'étape que la réponse de
    # l'assistant vient de traiter/entamer, toujours un tour d'avance
    # sur ce que l'utilisateur vient de dire. Au premier tour d'une
    # session, c'est le message déclencheur : on ne l'enregistre pas.
    if thesis_text and previous_step:
        db.add(UserStatement(
            user_id=user_id, ticker=ticker, step=previous_step, statement_text=thesis_text,
            analysis_session_id=active.id,
        ))

    if is_new_swot and thesis_text:
        db.add(InvestmentThesis(
            user_id=user_id, ticker=ticker, thesis_text=thesis_text, analysis_session_id=active.id,
        ))

    facts_written = False
    if is_new_session and data:
        for field in _FACT_FIELDS:
            value = data.get(field)
            if value is not None:
                db.add(AnalysisFact(
                    user_id=user_id, ticker=ticker, fact_type=field, fact_value=value, fact_date=now,
                    analysis_session_id=active.id,
                ))
                facts_written = True

    db.flush()
    print(f"[DB-TRACKING] Écrit (non commité) : user={user_id}, ticker={ticker}, session={active.id} (nouvelle={is_new_session}), étape={step}, thèse_capturée={is_new_swot and bool(thesis_text)}, faits_snapshot={facts_written}")
    return active
