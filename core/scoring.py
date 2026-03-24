# ========================= # 1️⃣ Score Croissance # =========================

def growth_score(data):

	score = 0

	revenue_growth = data.get("revenue_growth", 0)
	eps_growth = data.get("growth", 0)

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

# ========================= # 2️⃣ Score Qualité # =========================

def quality_score(data):

	score = 0

	margin = data.get("operating_margin", 0)
	roe = data.get("roe", 0)
	net_cash = data.get("net_cash", 0)

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

# ========================= # 3️⃣ Détection MOAT # =========================

def detect_moat(data):

	roic = data.get("roic", 0)
	margin = data.get("operating_margin", 0)

	if roic > 20 and margin > 25:
		return "fort"
	elif roic > 12:
		return "modéré"
	else:
		return "faible"

# ========================= # 4️⃣ Stabilité business # =========================

def business_stability(data):

	revenue_growth = data.get("revenue_growth", 0)
	margin = data.get("operating_margin", 0)

	if revenue_growth > 5 and margin > 10:
		return "stable"
	elif revenue_growth > 0:
		return "moyenne"
	else:
		return "volatile"

# ========================= # 5️⃣ Diagnostic Business # =========================

def business_quality(data):

	roe = data.get("roe", 0)
	margin = data.get("operating_margin", 0)

	if roe > 20 and margin > 25:
		return "exceptionnelle"
	elif roe > 12 and margin > 15:
		return "solide"
	elif roe > 8:
		return "correcte"
	else:
		return "faible"
# ========================= # 6️⃣ Score final # =========================

def compute_score(data):

	total = growth_score(data) + quality_score(data)

	# score max = 9
	score_normalized = round((total / 9) * 10, 1)

	return score_normalized

# ========================= # 7️⃣ Verdict score # =========================

def get_verdict(score):

	if score >= 9:
		return "Excellente entreprise"
	elif score >= 7:
		return "Entreprise solide"
	elif score >= 5:
		return "Entreprise correcte"
	else:
		return "Entreprise faible"