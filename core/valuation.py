import logging

logger = logging.getLogger(__name__)


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
	if growth >= 30:
		multiple = 40
	elif growth >= 20:
		multiple = 32
	elif growth >= 12:
		multiple = 25
	elif growth >= 8:
		multiple = 20
	elif growth >= 5:
		multiple = 16
	elif growth >= 0:
		multiple = 12
	else:
		multiple = 8

	base_multiple = multiple

	# --- Bonus FCF (max +5) ---
	fcf_yield = (fcf_per_share / current_price * 100) if (current_price > 0 and fcf_per_share > 0) else 0
	if fcf_yield > 5:
		fcf_bonus = 5
	elif fcf_yield > 3:
		fcf_bonus = 3
	elif fcf_per_share > 0:
		fcf_bonus = 1
	else:
		fcf_bonus = 0
	multiple += fcf_bonus

	# --- Bonus marge opérationnelle (max +4) ---
	if margin > 30:
		margin_bonus = 4
	elif margin > 20:
		margin_bonus = 3
	elif margin > 12:
		margin_bonus = 1
	else:
		margin_bonus = 0
	multiple += margin_bonus

	# --- Bonus ROE (max +3) ---
	if roe > 25:
		roe_bonus = 3
	elif roe > 15:
		roe_bonus = 2
	elif roe > 8:
		roe_bonus = 1
	else:
		roe_bonus = 0
	multiple += roe_bonus

	# --- Bonus trésorerie nette (max +2) ---
	cash_bonus = 2 if net_cash > 0 else 0
	multiple += cash_bonus

	logger.info(
		f"[VALUATION] Growth={growth}% → base={base_multiple}, "
		f"FCF yield={fcf_yield:.1f}% (+{fcf_bonus}), "
		f"Margin={margin}% (+{margin_bonus}), "
		f"ROE={roe}% (+{roe_bonus}), "
		f"Net cash={'positive' if net_cash > 0 else 'negative'} (+{cash_bonus}) "
		f"→ final multiple={multiple}"
	)

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
