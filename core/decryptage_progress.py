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

R1-C3 (hardening avant T3) :

- AnalysisSession est la source de vérité de la progression produit ; le
  contexte texte de la conversation est secondaire ;
- current_step = étape ACTUELLEMENT OUVERTE (celle que la dernière réponse
  assistant a posée et à laquelle l'utilisateur doit encore répondre), pas
  la dernière étape terminée ;
- progression MONOTONE dans l'ordre fermé DECRYPTAGE_STEP_MARKERS : un
  marqueur rétrograde n'abîme jamais la session (current_step et status
  inchangés, session retournée normalement pour le rattachement de la
  livraison) ; un saut d'étape avance sans créer d'étape intermédiaire ;
- la mutation relit l'AnalysisSession active SOUS VERROU (FOR UPDATE) :
  même si le prompt a été construit sur un snapshot stale (deux onglets,
  même ticker, en concurrence), la BDD ne régresse jamais ;
- thesis_text n'est fourni par l'orchestrateur que pour une vraie
  contribution R1-C2 (input_action contribution_appended) : un message de
  navigation ou une reprise vide ne devient jamais UserStatement ni
  InvestmentThesis.

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
# Ordre fermé de la séquence : business=0 ... swot_final=5.
STEP_ORDER = {step: index for index, step in enumerate(DECRYPTAGE_STEP_MARKERS)}

# Actions de progression (journal technique [DECRYPTAGE-PROGRESS]).
PROGRESS_NEW = "new"
PROGRESS_SAME = "same"
PROGRESS_FORWARD = "forward"
PROGRESS_STEP_SKIP = "step_skip"
PROGRESS_RETROGRADE = "retrograde_marker"

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


def find_active_analysis_session(db, user_id, ticker, *, for_update: bool = False):
    """Retourne l'AnalysisSession status=in_progress de ce user+ticker, ou
    None (T1-B2).

    Invariant applicatif : une seule session in_progress par user+ticker
    (aucune contrainte SQL ne le garantit). Si plusieurs existent par
    anomalie, on ne crée rien : on choisit déterministement la plus
    récente (started_at, puis updated_at, puis id) et on journalise.

    for_update (R1-C3C, chemin de mutation uniquement) : lecture verrouillée
    et rafraîchie (FOR UPDATE, populate_existing), même ordre déterministe
    que le verrou pris par la capture R1-C2 du tour (déjà détenu dans
    /decryptage : aucun nouvel ordre de verrous). Les lectures (prompt,
    listing des thèses) restent non verrouillées."""
    query = db.query(AnalysisSession).filter(
        AnalysisSession.user_id == user_id,
        AnalysisSession.ticker == ticker,
        AnalysisSession.status == "in_progress",
    ).order_by(
        AnalysisSession.started_at.desc(),
        AnalysisSession.updated_at.desc(),
        AnalysisSession.id.desc(),
    )
    if for_update:
        query = query.with_for_update().populate_existing()
    sessions = query.all()
    if len(sessions) > 1:
        print(
            f"[ANALYSIS-SESSION ANOMALY] {len(sessions)} sessions in_progress pour "
            f"user={user_id}, ticker={ticker} : {[str(s.id) for s in sessions]} — "
            f"session retenue (la plus récente) : {sessions[0].id}"
        )
    return sessions[0] if sessions else None


def _progress_action(current_step: str | None, incoming_step: str) -> str:
    """Classe le marqueur entrant par rapport à l'étape ouverte d'une session
    existante (ordre fermé STEP_ORDER). Une étape courante absente ou hors
    vocabulaire (donnée historique anormale) n'offre aucun ordre fiable : le
    marqueur est appliqué comme une avancée."""
    if current_step not in STEP_ORDER:
        return PROGRESS_FORWARD
    distance = STEP_ORDER[incoming_step] - STEP_ORDER[current_step]
    if distance == 0:
        return PROGRESS_SAME
    if distance < 0:
        return PROGRESS_RETROGRADE
    return PROGRESS_FORWARD if distance == 1 else PROGRESS_STEP_SKIP


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

    Progression monotone (R1-C3C) d'une session existante, relue sous
    verrou ; current_step = étape actuellement ouverte :
    - même étape → current_step inchangé (updated_at rafraîchi) ;
    - étape suivante → current_step avance ;
    - saut d'étapes → current_step avance directement, aucune étape
      intermédiaire inventée, anomalie step_skip journalisée ;
    - étape rétrograde → current_step et status INCHANGÉS, aucune nouvelle
      session, anomalie retrograde_marker journalisée ; la session est
      retournée normalement (la livraison y reste rattachée ; R1-C2 traite
      ce marqueur comme continued_without_boundary_signal) ;
    - swot_final (toujours en avant) → completed + completed_at.

    thesis_text : fourni par l'orchestrateur UNIQUEMENT pour une vraie
    contribution R1-C2 (contribution_appended), sinon None. Il répond à
    l'étape qui était ouverte AVANT ce tour (UserStatement sur cette
    étape) et, sur swot_final, devient l'InvestmentThesis. Sans
    contribution réelle : ni UserStatement, ni InvestmentThesis.

    Tout ce qui est écrit pour la tentative porte
    analysis_session_id = session.id."""
    if step not in DECRYPTAGE_STEP_MARKERS:
        raise InvalidDecryptageStep("étape hors vocabulaire construction_these")
    active = find_active_analysis_session(db, user_id, ticker, for_update=True)
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
        action = PROGRESS_NEW
        logger.info("[DECRYPTAGE-PROGRESS] session=%s action=new step=%s", active.id, step)
    else:
        action = _progress_action(active.current_step, step)
    # Étape ouverte AVANT ce tour : celle à laquelle le texte de ce tour
    # répond (None au premier tour d'une session : message déclencheur).
    previous_step = None if is_new_session else active.current_step

    if action == PROGRESS_RETROGRADE:
        # Le marqueur rétrograde reste attaché à la session active sans muter
        # sa progression : current_step, status, completed_at intacts.
        logger.warning("[DECRYPTAGE-PROGRESS] session=%s anomaly=retrograde_marker current=%s received=%s",
                       active.id, active.current_step, step)
    else:
        if action == PROGRESS_SAME:
            logger.info("[DECRYPTAGE-PROGRESS] session=%s action=same step=%s", active.id, step)
        elif action == PROGRESS_FORWARD:
            logger.info("[DECRYPTAGE-PROGRESS] session=%s action=forward from=%s to=%s",
                        active.id, previous_step, step)
        elif action == PROGRESS_STEP_SKIP:
            logger.warning("[DECRYPTAGE-PROGRESS] session=%s anomaly=step_skip from=%s to=%s",
                           active.id, previous_step, step)
        active.current_step = step
    active.updated_at = now_aware
    is_new_swot = step == FINAL_STEP and action != PROGRESS_RETROGRADE
    if is_new_swot:
        active.status = "completed"
        active.completed_at = now_aware

    # Le texte reçu à ce tour répond à l'étape qui était OUVERTE avant lui
    # (previous_step), pas à l'étape que ce marqueur annonce : le marqueur
    # reflète l'étape que la réponse de l'assistant vient d'ouvrir,
    # toujours un tour d'avance sur ce que l'utilisateur vient de dire. Au
    # premier tour d'une session, c'est le message déclencheur : on ne
    # l'enregistre pas. thesis_text n'est non vide que pour une vraie
    # contribution R1-C2 (voir api.py).
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
    print(f"[DB-TRACKING] Écrit (non commité) : user={user_id}, ticker={ticker}, session={active.id} (nouvelle={is_new_session}), étape={active.current_step}, marqueur={step}, progression={action}, thèse_capturée={is_new_swot and bool(thesis_text)}, faits_snapshot={facts_written}")
    return active
