# ========================= # 1️⃣ Score Croissance # =========================

def _v(data, key):
	"""Get a numeric value from data, treating None as 0."""
	val = data.get(key, 0)
	return val if val is not None else 0

def growth_score(data):
	# Max: 4 pts
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

# ========================= # 2️⃣ Score Qualité # =========================

def quality_score(data):
	# Max: 5 pts
	score = 0

	margin = _v(data, "operating_margin")
	roe = _v(data, "roe")
	net_cash = _v(data, "net_cash")

	if margin > 25:
		score += 2
	elif margin > 15:
		score += 1

	if roe > 20:
		score += 2
	elif roe > 10:
		score += 1

	if net_cash > 0:
		score += 1

	return score

QUALITY_SCORE_MAX = 5

# ========================= # 3️⃣ Score MOAT # =========================

def moat_score(data):
	# Max: 2 pts
	roic = _v(data, "roic")
	margin = _v(data, "operating_margin")

	if roic > 20 and margin > 25:
		return 2
	elif roic > 12:
		return 1
	else:
		return 0

MOAT_SCORE_MAX = 2

# ========================= # 4️⃣ Score Stabilité business # =========================

def stability_score(data):
	# Max: 2 pts
	revenue_growth = _v(data, "revenue_growth")
	margin = _v(data, "operating_margin")

	if revenue_growth > 5 and margin > 10:
		return 2
	elif revenue_growth > 0:
		return 1
	else:
		return 0

STABILITY_SCORE_MAX = 2

# ========================= # 5️⃣ Score Business Quality # =========================

def biz_quality_score(data):
	# Max: 3 pts
	roe = _v(data, "roe")
	margin = _v(data, "operating_margin")

	if roe > 20 and margin > 25:
		return 3
	elif roe > 12 and margin > 15:
		return 2
	elif roe > 8:
		return 1
	else:
		return 0

BIZ_QUALITY_SCORE_MAX = 3

# ========================= # 6️⃣ Score final # =========================

SCORE_MAX = GROWTH_SCORE_MAX + QUALITY_SCORE_MAX + MOAT_SCORE_MAX + STABILITY_SCORE_MAX + BIZ_QUALITY_SCORE_MAX

def compute_score(data):
	total = (
		growth_score(data)
		+ quality_score(data)
		+ moat_score(data)
		+ stability_score(data)
		+ biz_quality_score(data)
	)

	total = max(total, 0)
	score_normalized = round((total / SCORE_MAX) * 10, 1)

	return score_normalized

# ========================= # 7️⃣ Fonctions legacy (labels textuels) # =========================

def detect_moat(data):
	s = moat_score(data)
	if s == 2:
		return "fort"
	elif s == 1:
		return "modéré"
	else:
		return "faible"

def business_stability(data):
	s = stability_score(data)
	if s == 2:
		return "stable"
	elif s == 1:
		return "moyenne"
	else:
		return "volatile"

def business_quality(data):
	s = biz_quality_score(data)
	if s == 3:
		return "exceptionnelle"
	elif s == 2:
		return "solide"
	elif s == 1:
		return "correcte"
	else:
		return "faible"

# ========================= # 8️⃣ Verdict score # =========================

def get_verdict(score):
	if score >= 9:
		return "Excellente entreprise"
	elif score >= 7:
		return "Entreprise solide"
	elif score >= 5:
		return "Entreprise correcte"
	else:
		return "Entreprise faible"
