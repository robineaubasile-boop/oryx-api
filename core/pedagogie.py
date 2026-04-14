def _v(val):
    """Treat None as 0 for numeric comparisons."""
    return val if val is not None else 0


def generate_analysis(data):
    analysis = {}

    # Croissance
    rg = _v(data.get("revenue_growth"))
    if rg >= 20:
        analysis["growth"] = "très forte"
    elif rg >= 10:
        analysis["growth"] = "forte"
    elif rg >= 5:
        analysis["growth"] = "modérée"
    elif rg >= 0:
        analysis["growth"] = "faible"
    else:
        analysis["growth"] = "en déclin"

    # Marge opérationnelle
    margin = _v(data.get("operating_margin"))
    if margin >= 30:
        analysis["margin"] = "exceptionnelle"
    elif margin >= 20:
        analysis["margin"] = "excellente"
    elif margin >= 12:
        analysis["margin"] = "solide"
    elif margin >= 5:
        analysis["margin"] = "correcte"
    else:
        analysis["margin"] = "faible"

    # ROE
    roe = _v(data.get("roe"))
    if roe >= 25:
        analysis["roe"] = "exceptionnel"
    elif roe >= 15:
        analysis["roe"] = "élevé"
    elif roe >= 10:
        analysis["roe"] = "correct"
    else:
        analysis["roe"] = "faible"

    # ROIC
    if data.get("roic") is None:
        analysis["roic"] = "non disponible"
    else:
        roic = data.get("roic")
        if roic >= 20:
            analysis["roic"] = "exceptionnel"
        elif roic >= 15:
            analysis["roic"] = "excellent"
        elif roic >= 10:
            analysis["roic"] = "correct"
        else:
            analysis["roic"] = "faible"

    # Structure financière (combinaison D/E + net_cash)
    de_val = data.get("debt_to_equity")
    nc_val = data.get("net_cash")
    if de_val is None and nc_val is None:
        analysis["balance_sheet"] = "non disponible"
    elif de_val is None:
        nc = nc_val
        if nc > 0:
            analysis["balance_sheet"] = "très solide"
        else:
            analysis["balance_sheet"] = "acceptable"
    elif nc_val is None:
        de = de_val
        if de < 0.3:
            analysis["balance_sheet"] = "très solide"
        elif de < 0.5:
            analysis["balance_sheet"] = "très solide"
        elif de < 1:
            analysis["balance_sheet"] = "saine"
        elif de < 1.5:
            analysis["balance_sheet"] = "acceptable"
        elif de < 2.5:
            analysis["balance_sheet"] = "tendue"
        else:
            analysis["balance_sheet"] = "fragile"
    else:
        de = de_val
        nc = nc_val
        if nc > 0 and de < 0.3:
            analysis["balance_sheet"] = "forteresse"
        elif nc > 0 or de < 0.5:
            analysis["balance_sheet"] = "très solide"
        elif de < 1:
            analysis["balance_sheet"] = "saine"
        elif de < 1.5:
            analysis["balance_sheet"] = "acceptable"
        elif de < 2.5:
            analysis["balance_sheet"] = "tendue"
        else:
            analysis["balance_sheet"] = "fragile"

    # Prévisibilité
    rev_years = _v(data.get("revenue_growth_years"))
    margin_stab = data.get("margin_stability")
    eps_pos = _v(data.get("eps_positive_years"))

    predictability_signals = 0
    if rev_years >= 4:
        predictability_signals += 1
    if margin_stab is not None and margin_stab < 4:
        predictability_signals += 1
    if eps_pos >= 5:
        predictability_signals += 1

    if predictability_signals >= 3:
        analysis["predictability"] = "élevée"
    elif predictability_signals >= 2:
        analysis["predictability"] = "correcte"
    elif predictability_signals >= 1:
        analysis["predictability"] = "limitée"
    else:
        analysis["predictability"] = "faible"

    return analysis
