def _v(val):
	"""Treat None as 0 for numeric comparisons."""
	return val if val is not None else 0


def compute_valuation(data):
	"""
	data attendu :
	{
		"eps": float | None,
		"fcf_per_share": float | None,
		"revenue_growth": float | None,
		"roe": float | None,
		"operating_margin": float | None,
		"net_cash": float | None,
		"current_price": float | None
	}
	"""

	eps = _v(data.get("eps"))
	fcf_per_share = _v(data.get("fcf_per_share"))
	growth = _v(data.get("revenue_growth"))
	roe = _v(data.get("roe"))
	margin = _v(data.get("operating_margin"))
	net_cash = _v(data.get("net_cash"))
	current_price = _v(data.get("current_price"))

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
