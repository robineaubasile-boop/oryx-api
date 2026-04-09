import logging

logger = logging.getLogger(__name__)


def _v(data, key):
	"""Get a numeric value from data, treating None as 0."""
	val = data.get(key, 0)
	return val if val is not None else 0


# ========================= # Sector profile mapping # =========================

SECTOR_PROFILES = {
	# Growth
	"Technology": "growth",
	"Healthcare": "growth",
	"Communication Services": "growth",
	# Quality Consumer
	"Consumer Cyclical": "quality_consumer",
	"Consumer Defensive": "quality_consumer",
	# Industrial
	"Industrials": "industrial",
	"Basic Materials": "industrial",
	# Cyclical / Commodity / Regulated
	"Energy": "cyclical",
	"Utilities": "cyclical",
	"Real Estate": "cyclical",
	# Financials
	"Financial Services": "financials",
}

def _get_profile(data):
	"""Get the scoring profile based on sector."""
	sector = data.get("sector", "Unknown")
	profile = SECTOR_PROFILES.get(sector, "growth")
	logger.info(f"[PROFILE] Sector '{sector}' → profile '{profile}'")
	return profile

# ========================= # Thresholds per profile # =========================

THRESHOLDS = {
	"growth": {
		"rev_growth_high": 15, "rev_growth_mid": 8,
		"eps_growth_high": 15, "eps_growth_mid": 8,
		"margin_good": 25,
		"roe_good": 20,
		"roic_high": 20, "roic_mid": 12,
		"moat_roic_high": 20, "moat_margin_high": 25,
		"moat_roic_mid": 15, "moat_margin_mid": 15,
		"moat_growth_high": 5, "moat_growth_margin_high": 15,
		"moat_growth_mid": 0, "moat_growth_margin_mid": 10,
		"de_solid": 0.5, "de_ok": 1, "de_alert": 2,
		"margin_stab_good": 3, "margin_stab_ok": 5,
		"use_margin": True, "use_roic": True, "use_de": True,
	},
	"quality_consumer": {
		"rev_growth_high": 10, "rev_growth_mid": 5,
		"eps_growth_high": 10, "eps_growth_mid": 5,
		"margin_good": 18,
		"roe_good": 15,
		"roic_high": 15, "roic_mid": 8,
		"moat_roic_high": 15, "moat_margin_high": 18,
		"moat_roic_mid": 10, "moat_margin_mid": 12,
		"moat_growth_high": 3, "moat_growth_margin_high": 12,
		"moat_growth_mid": 0, "moat_growth_margin_mid": 8,
		"de_solid": 0.5, "de_ok": 1, "de_alert": 2,
		"margin_stab_good": 3, "margin_stab_ok": 5,
		"use_margin": True, "use_roic": True, "use_de": True,
	},
	"industrial": {
		"rev_growth_high": 10, "rev_growth_mid": 5,
		"eps_growth_high": 12, "eps_growth_mid": 5,
		"margin_good": 15,
		"roe_good": 15,
		"roic_high": 15, "roic_mid": 10,
		"moat_roic_high": 15, "moat_margin_high": 15,
		"moat_roic_mid": 10, "moat_margin_mid": 10,
		"moat_growth_high": 3, "moat_growth_margin_high": 10,
		"moat_growth_mid": 0, "moat_growth_margin_mid": 7,
		"de_solid": 0.7, "de_ok": 1.5, "de_alert": 3,
		"margin_stab_good": 4, "margin_stab_ok": 6,
		"use_margin": True, "use_roic": True, "use_de": True,
	},
	"cyclical": {
		"rev_growth_high": 8, "rev_growth_mid": 3,
		"eps_growth_high": 10, "eps_growth_mid": 3,
		"margin_good": 15,
		"roe_good": 12,
		"roic_high": 12, "roic_mid": 8,
		"moat_roic_high": 12, "moat_margin_high": 15,
		"moat_roic_mid": 8, "moat_margin_mid": 10,
		"moat_growth_high": 3, "moat_growth_margin_high": 10,
		"moat_growth_mid": 0, "moat_growth_margin_mid": 7,
		"de_solid": 1, "de_ok": 2, "de_alert": 3,
		"margin_stab_good": 5, "margin_stab_ok": 8,
		"use_margin": True, "use_roic": True, "use_de": True,
	},
	"financials": {
		"rev_growth_high": 8, "rev_growth_mid": 4,
		"eps_growth_high": 10, "eps_growth_mid": 4,
		"margin_good": 99,
		"roe_good": 12,
		"roic_high": 99, "roic_mid": 99,
		"moat_roic_high": 99, "moat_margin_high": 99,
		"moat_roic_mid": 99, "moat_margin_mid": 99,
		"moat_growth_high": 4, "moat_growth_margin_high": 0,
		"moat_growth_mid": 0, "moat_growth_margin_mid": 0,
		"de_solid": 99, "de_ok": 99, "de_alert": 99,
		"margin_stab_good": 5, "margin_stab_ok": 8,
		"use_margin": False, "use_roic": False, "use_de": False,
	},
}


# ========================= # 1. Score Croissance (max 4 pts) # =========================

def growth_score(data, t=None):
	if t is None:
		t = THRESHOLDS["growth"]
	score = 0
	revenue_growth = _v(data, "revenue_growth")
	eps_growth = _v(data, "growth")

	if revenue_growth > t["rev_growth_high"]:
		score += 2
	elif revenue_growth > t["rev_growth_mid"]:
		score += 1

	if eps_growth > t["eps_growth_high"]:
		score += 2
	elif eps_growth > t["eps_growth_mid"]:
		score += 1
	elif eps_growth < 0:
		score -= 1

	return score

GROWTH_SCORE_MAX = 4


# ========================= # 2. Score Qualité business (max 5 pts) # =========================

def quality_score(data, t=None):
	if t is None:
		t = THRESHOLDS["growth"]
	score = 0
	margin = _v(data, "operating_margin")
	roe = _v(data, "roe")
	roic = _v(data, "roic")
	fcf_per_share = _v(data, "fcf_per_share")

	if fcf_per_share > 0:
		score += 1

	if t["use_margin"] and margin > t["margin_good"]:
		score += 1

	if roe > t["roe_good"]:
		score += 1

	if t["use_roic"]:
		if roic > t["roic_high"]:
			score += 2
		elif roic > t["roic_mid"]:
			score += 1

	return score

QUALITY_SCORE_MAX = 5


# ========================= # 3. Score MOAT (max 4 pts) # =========================

def moat_score(data, t=None):
	if t is None:
		t = THRESHOLDS["growth"]
	score = 0
	roic = _v(data, "roic")
	margin = _v(data, "operating_margin")
	revenue_growth = _v(data, "revenue_growth")

	if t["use_roic"] and t["use_margin"]:
		if roic > t["moat_roic_high"] and margin > t["moat_margin_high"]:
			score += 2
		elif roic > t["moat_roic_mid"] and margin > t["moat_margin_mid"]:
			score += 1

	if revenue_growth > t["moat_growth_high"] and margin > t["moat_growth_margin_high"]:
		score += 2
	elif revenue_growth > t["moat_growth_mid"] and margin > t["moat_growth_margin_mid"]:
		score += 1

	return score

MOAT_SCORE_MAX = 4


# ========================= # 4. Score Structure financière (max 3 pts) # =========================

def structure_score(data, t=None):
	if t is None:
		t = THRESHOLDS["growth"]
	score = 0
	net_cash = _v(data, "net_cash")
	debt_to_equity = _v(data, "debt_to_equity")

	if net_cash > 0:
		score += 1

	if t["use_de"]:
		if debt_to_equity < t["de_solid"]:
			score += 2
		elif debt_to_equity < t["de_ok"]:
			score += 1
		elif debt_to_equity > t["de_alert"]:
			score -= 1

	return score

STRUCTURE_SCORE_MAX = 3


# ========================= # 5. Score Prévisibilité (max 3 pts) # =========================

def predictability_score(data, t=None):
	if t is None:
		t = THRESHOLDS["growth"]
	score = 0
	revenue_growth_years = _v(data, "revenue_growth_years")
	margin_stability = data.get("margin_stability")
	eps_positive_years = _v(data, "eps_positive_years")

	if revenue_growth_years >= 4:
		score += 1
	elif revenue_growth_years >= 2:
		score += 0.5

	if margin_stability is not None:
		if margin_stability < t["margin_stab_good"]:
			score += 1
		elif margin_stability < t["margin_stab_ok"]:
			score += 0.5

	if eps_positive_years >= 5:
		score += 1
	elif eps_positive_years >= 4:
		score += 0.5

	return score

PREDICTABILITY_SCORE_MAX = 3


# ========================= # Score final # =========================

SCORE_MAX = GROWTH_SCORE_MAX + QUALITY_SCORE_MAX + MOAT_SCORE_MAX + STRUCTURE_SCORE_MAX + PREDICTABILITY_SCORE_MAX  # 19

def compute_score(data):
	profile = _get_profile(data)
	t = THRESHOLDS.get(profile, THRESHOLDS["growth"])

	g = growth_score(data, t)
	q = quality_score(data, t)
	m = moat_score(data, t)
	s = structure_score(data, t)
	p = predictability_score(data, t)

	total = g + q + m + s + p
	total = max(total, 0)
	score_normalized = round((total / SCORE_MAX) * 10, 1)

	logger.info(
		f"[SCORING] Profile={profile}, Growth={g}, Quality={q}, Moat={m}, "
		f"Structure={s}, Predictability={p} "
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
