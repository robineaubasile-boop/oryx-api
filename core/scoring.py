import logging

logger = logging.getLogger(__name__)


def _v(data, key):
	"""Get a numeric value from data, treating None as 0."""
	val = data.get(key, 0)
	return val if val is not None else 0


# ========================= # 1. Score Croissance (max 4 pts) # =========================

def growth_score(data):
	score = 0

	revenue_growth = _v(data, "revenue_growth")
	eps_growth = _v(data, "growth")

	if revenue_growth > 15:
		score += 2
	elif revenue_growth > 8:
		score += 1

	if eps_growth > 15:
		score += 2
	elif eps_growth > 8:
		score += 1
	elif eps_growth < 0:
		score -= 1

	return score

GROWTH_SCORE_MAX = 4


# ========================= # 2. Score Qualité business (max 5 pts) # =========================

def quality_score(data):
	score = 0

	margin = _v(data, "operating_margin")
	roe = _v(data, "roe")
	roic = _v(data, "roic")
	fcf_per_share = _v(data, "fcf_per_share")

	# FCF positif
	if fcf_per_share > 0:
		score += 1

	# Marge opérationnelle
	if margin > 25:
		score += 1

	# ROE
	if roe > 20:
		score += 1

	# ROIC (la métrique la plus fiable)
	if roic > 20:
		score += 2
	elif roic > 12:
		score += 1

	return score

QUALITY_SCORE_MAX = 5


# ========================= # 3. Score MOAT (max 4 pts) # =========================

def moat_score(data):
	score = 0

	roic = _v(data, "roic")
	margin = _v(data, "operating_margin")
	revenue_growth = _v(data, "revenue_growth")

	# ROIC élevé + marge élevée = moat fort
	if roic > 20 and margin > 25:
		score += 2
	elif roic > 15 and margin > 15:
		score += 1

	# Croissance positive + marge élevée = business défendable
	if revenue_growth > 5 and margin > 15:
		score += 2
	elif revenue_growth > 0 and margin > 10:
		score += 1

	return score

MOAT_SCORE_MAX = 4


# ========================= # 4. Score Structure financière (max 3 pts) # =========================

def structure_score(data):
	score = 0

	net_cash = _v(data, "net_cash")
	debt_to_equity = _v(data, "debt_to_equity")

	# Trésorerie nette positive
	if net_cash > 0:
		score += 2

	# Niveau d'endettement
	if debt_to_equity < 0.5:
		score += 1
	elif debt_to_equity > 2:
		score -= 1

	return score

STRUCTURE_SCORE_MAX = 3


# ========================= # Score final # =========================

SCORE_MAX = GROWTH_SCORE_MAX + QUALITY_SCORE_MAX + MOAT_SCORE_MAX + STRUCTURE_SCORE_MAX  # 16

def compute_score(data):
	g = growth_score(data)
	q = quality_score(data)
	m = moat_score(data)
	s = structure_score(data)

	total = g + q + m + s
	total = max(total, 0)
	score_normalized = round((total / SCORE_MAX) * 10, 1)

	logger.info(
		f"[SCORING] Growth={g}, Quality={q}, Moat={m}, Structure={s} "
		f"→ Total={total}/{SCORE_MAX} → Normalized={score_normalized}/10"
	)

	return score_normalized


# ========================= # Fonctions legacy (labels textuels) # =========================

def detect_moat(data):
	s = moat_score(data)
	if s >= 3:
		return "fort"
	elif s >= 1:
		return "modéré"
	else:
		return "faible"

def business_quality(data):
	s = quality_score(data)
	if s >= 5:
		return "exceptionnelle"
	elif s >= 4:
		return "solide"
	elif s >= 2:
		return "correcte"
	else:
		return "faible"


# ========================= # Verdict score # =========================

def get_verdict(score):
	if score >= 9:
		return "Excellente entreprise"
	elif score >= 7:
		return "Entreprise solide"
	elif score >= 5:
		return "Entreprise correcte"
	else:
		return "Entreprise faible"
