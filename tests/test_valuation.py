from core.valuation import compute_valuation, valuation_verdict


# --- Multiple de base (croissance) ---

def test_multiple_very_high_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 35, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 40

def test_multiple_high_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 22, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 32

def test_multiple_good_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 14, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 25

def test_multiple_moderate_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 9, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 20

def test_multiple_low_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 6, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 16

def test_multiple_flat_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12

def test_multiple_negative_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": -5, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 8


# --- Bonus FCF yield ---

def test_fcf_yield_high():
	# fcf_per_share=6, price=100 → yield 6% → +5
	data = {"eps": 5, "fcf_per_share": 6, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 5

def test_fcf_yield_medium():
	# fcf_per_share=4, price=100 → yield 4% → +3
	data = {"eps": 5, "fcf_per_share": 4, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 3

def test_fcf_yield_low():
	# fcf_per_share=1, price=100 → yield 1% → +1 (fcf_per_share > 0)
	data = {"eps": 5, "fcf_per_share": 1, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 1

def test_fcf_yield_zero():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12


# --- Bonus marge ---

def test_margin_bonus_high():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 35, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 4

def test_margin_bonus_medium():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 22, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 3

def test_margin_bonus_low():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 15, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 1


# --- Bonus ROE ---

def test_roe_bonus_high():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 30, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 3

def test_roe_bonus_medium():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 18, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 2

def test_roe_bonus_low():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 10, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 1


# --- Bonus net cash ---

def test_cash_bonus_positive():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 500, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12 + 2

def test_cash_bonus_negative():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": -100, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 12


# --- All bonuses combined ---

def test_multiple_with_all_bonuses():
	# growth 22 → 32, fcf 6/100=6%→+5, margin 35→+4, roe 30→+3, cash +2 = 46
	data = {"eps": 5, "fcf_per_share": 6, "revenue_growth": 22, "roe": 30, "operating_margin": 35, "net_cash": 100, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 32 + 5 + 4 + 3 + 2

def test_multiple_no_bonus():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 22, "roe": 5, "operating_margin": 5, "net_cash": -50, "current_price": 100}
	_, _, multiple = compute_valuation(data)
	assert multiple == 32


# --- Fair value et fallback FCF ---

def test_fair_value_uses_eps():
	data = {"eps": 10, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
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
