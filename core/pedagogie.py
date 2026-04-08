def _v(val):
    """Treat None as 0 for numeric comparisons."""
    return val if val is not None else 0


def generate_analysis(data):
    analysis = {}

    # Croissance
    rg = _v(data.get("revenue_growth"))
    if rg >= 15:
        analysis["growth"] = "forte"
    elif rg >= 8:
        analysis["growth"] = "correcte"
    elif rg >= 0:
        analysis["growth"] = "faible"
    else:
        analysis["growth"] = "en déclin"

    # Marge opérationnelle
    margin = _v(data.get("operating_margin"))
    if margin >= 25:
        analysis["margin"] = "excellente"
    elif margin >= 15:
        analysis["margin"] = "solide"
    elif margin >= 8:
        analysis["margin"] = "correcte"
    else:
        analysis["margin"] = "faible"

    # ROE
    roe = _v(data.get("roe"))
    if roe >= 20:
        analysis["roe"] = "élevé"
    elif roe >= 12:
        analysis["roe"] = "correct"
    else:
        analysis["roe"] = "faible"

    # ROIC
    roic = _v(data.get("roic"))
    if roic >= 20:
        analysis["roic"] = "excellent"
    elif roic >= 12:
        analysis["roic"] = "correct"
    else:
        analysis["roic"] = "faible"

    # Structure financière (combinaison D/E + net_cash)
    de = _v(data.get("debt_to_equity"))
    nc = _v(data.get("net_cash"))
    if nc > 0 and de < 0.5:
        analysis["balance_sheet"] = "très solide"
    elif de < 1:
        analysis["balance_sheet"] = "raisonnable"
    elif de < 2:
        analysis["balance_sheet"] = "tendu"
    else:
        analysis["balance_sheet"] = "fragile"

    # Prévisibilité
    rev_years = _v(data.get("revenue_growth_years"))
    margin_stab = data.get("margin_stability")
    eps_pos = _v(data.get("eps_positive_years"))

    predictability_signals = 0
    if rev_years >= 3:
        predictability_signals += 1
    if margin_stab is not None and margin_stab < 4:
        predictability_signals += 1
    if eps_pos >= 4:
        predictability_signals += 1

    if predictability_signals >= 3:
        analysis["predictability"] = "élevée"
    elif predictability_signals >= 2:
        analysis["predictability"] = "correcte"
    else:
        analysis["predictability"] = "faible"

    return analysis
