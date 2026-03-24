def generate_analysis(data):

	analysis = {}

	if data["revenue_growth"] >= 10:
		analysis["growth"] = "forte"
	elif data["revenue_growth"] >= 5:
		analysis["growth"] = "correcte"
	else:
		analysis["growth"] = "faible"

	if data["operating_margin"] >= 10:
		analysis["margin"] = "excellente"
	elif data["operating_margin"] >= 7:
		analysis["margin"] = "solide"
	else:
		analysis["margin"] = "faible"

	if data["roe"] >= 15:
		analysis["roe"] = "élevé"
	elif data["roe"] >= 10:
		analysis["roe"] = "correct"
	else:
		analysis["roe"] = "faible"

	if data["net_cash"] > 0:
		analysis["balance_sheet"] = "solide"
	else:
		analysis["balance_sheet"] = "endetté"

	return analysis
