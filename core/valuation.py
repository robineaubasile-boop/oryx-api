def compute_valuation(data):
	"""
	data attendu :
	{
		"eps": float,
		"fcf_per_share": float,
		"revenue_growth": float,
		"roe": float,
		"operating_margin": float,
		"net_cash": float,
		"current_price": float
	}
	"""

	eps = data.get("eps", 0)
	fcf_per_share = data.get("fcf_per_share", 0)
	growth = data.get("revenue_growth", 0)
	roe = data.get("roe", 0)
	margin = data.get("operating_margin", 0)
	net_cash = data.get("net_cash", 0)
	current_price = data.get("current_price", 0)

	# --- Multiple de base basé sur la croissance ---
	if growth >= 15:
		multiple = 25
	elif growth >= 10:
		multiple = 20
	elif growth >= 5:
		multiple = 16
	else:
		multiple = 12

	# --- Bonus qualité du business ---
	if roe > 20:
		multiple += 3
	if margin > 25:
		multiple += 2
	if net_cash > 0:
		multiple += 1

	# --- Calcul fair value ---
	# Fallback sur FCF/share quand EPS <= 0 (entreprise non rentable)
	if eps > 0:
		base_value = eps
	elif fcf_per_share > 0:
		base_value = fcf_per_share
	else:
		base_value = 0

	fair_value = base_value * multiple

	# --- Calcul potentiel ---
	if current_price > 0 and fair_value > 0:
		upside = ((fair_value - current_price) / current_price) * 100
	else:
		upside = 0

	return fair_value, upside, multiple


def valuation_verdict(upside):
	if upside >= 25:
		return "Sous-valorisée (fort potentiel)"
	elif upside >= 10:
		return "Sous-valorisée"
	elif -10 <= upside < 10:
		return "Valorisation raisonnable"
	else:
		return "Surévaluée"
