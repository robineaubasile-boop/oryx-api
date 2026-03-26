from core.valuation import compute_valuation, valuation_verdict


# --- Multiple de base (croissance) ---

def test_multiple_high_growth():
	data = {"eps": 5, "fcf_per_share": 3, "revenue_growth": 20, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 25

def test_multiple_medium_growth():
	data = {"eps": 5, "fcf_per_share": 3, "revenue_growth": 12, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 20

def test_multiple_moderate_growth():
	data = {"eps": 5, "fcf_per_share": 3, "revenue_growth": 7, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 16

def test_multiple_low_growth():
	data = {"eps": 5, "fcf_per_share": 3, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12


# --- Bonus qualité ---

def test_multiple_with_all_bonuses():
	data = {"eps": 5, "fcf_per_share": 3, "revenue_growth": 20, "roe": 25, "operating_margin": 30, "net_cash": 100, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 25 + 3 + 2 + 1  # 31

def test_multiple_no_bonus():
	data = {"eps": 5, "fcf_per_share": 3, "revenue_growth": 20, "roe": 10, "operating_margin": 10, "net_cash": -50, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 25


# --- Fair value et fallback FCF ---

def test_fair_value_uses_eps():
	data = {"eps": 10, "fcf_per_share": 5, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	fair_value, _, multiple = compute_valuation(data)
	assert fair_value == 10 * multiple

def test_fair_value_fallback_fcf_when_eps_zero():
	data = {"eps": 0, "fcf_per_share": 5, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	fair_value, _, multiple = compute_valuation(data)
	assert fair_value == 5 * multiple

def test_fair_value_fallback_fcf_when_eps_negative():
	data = {"eps": -3, "fcf_per_share": 5, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	fair_value, _, multiple = compute_valuation(data)
	assert fair_value == 5 * multiple

def test_fair_value_zero_when_both_non_positive():
	data = {"eps": -3, "fcf_per_share": -2, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	fair_value, upside, _ = compute_valuation(data)
	assert fair_value == 0
	assert upside == 0


# --- Upside ---

def test_upside_calculation():
	data = {"eps": 10, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	fair_value, upside, _ = compute_valuation(data)
	expected_upside = ((fair_value - 100) / 100) * 100
	assert upside == expected_upside

def test_upside_zero_when_price_zero():
	data = {"eps": 10, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 0}
	_, upside, _ = compute_valuation(data)
	assert upside == 0


# --- valuation_verdict ---

def test_verdict_fort_potentiel():
	assert valuation_verdict(30) == "Sous-valorisée (fort potentiel)"

def test_verdict_sous_valorisee():
	assert valuation_verdict(15) == "Sous-valorisée"

def test_verdict_raisonnable():
	assert valuation_verdict(0) == "Valorisation raisonnable"

def test_verdict_surevaluee():
	assert valuation_verdict(-20) == "Surévaluée"

def test_verdict_boundaries():
	assert valuation_verdict(25) == "Sous-valorisée (fort potentiel)"
	assert valuation_verdict(10) == "Sous-valorisée"
	assert valuation_verdict(-10) == "Valorisation raisonnable"
	assert valuation_verdict(-10.1) == "Surévaluée"
