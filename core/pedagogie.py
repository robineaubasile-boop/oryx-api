def _v(val):
	"""Treat None as 0 for numeric comparisons."""
	return val if val is not None else 0


def generate_analysis(data):

	analysis = {}

	rg = _v(data.get("revenue_growth"))
	if rg >= 10:
		analysis["growth"] = "forte"
	elif rg >= 5:
		analysis["growth"] = "correcte"
	else:
		analysis["growth"] = "faible"

	margin = _v(data.get("operating_margin"))
	if margin >= 10:
		analysis["margin"] = "excellente"
	elif margin >= 7:
		analysis["margin"] = "solide"
	else:
		analysis["margin"] = "faible"

	roe = _v(data.get("roe"))
	if roe >= 15:
		analysis["roe"] = "élevé"
	elif roe >= 10:
		analysis["roe"] = "correct"
	else:
		analysis["roe"] = "faible"

	nc = _v(data.get("net_cash"))
	if nc > 0:
		analysis["balance_sheet"] = "solide"
	else:
		analysis["balance_sheet"] = "endetté"

	return analysis
