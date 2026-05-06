from core.valuation import compute_valuation, valuation_verdict


# --- Multiple de base (croissance) ---

def test_multiple_very_high_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 35, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 40

def test_multiple_high_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 22, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 32

def test_multiple_good_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 14, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 25

def test_multiple_moderate_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 9, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 20

def test_multiple_low_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 6, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 16

def test_multiple_flat_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12

def test_multiple_negative_growth():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": -5, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 8


# --- Bonus FCF yield ---

def test_fcf_yield_high():
	# fcf_per_share=6, price=100 → yield 6% → +5
	data = {"eps": 5, "fcf_per_share": 6, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 5

def test_fcf_yield_medium():
	# fcf_per_share=4, price=100 → yield 4% → +3
	data = {"eps": 5, "fcf_per_share": 4, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 3

def test_fcf_yield_low():
	# fcf_per_share=1, price=100 → yield 1% → +1 (fcf_per_share > 0)
	data = {"eps": 5, "fcf_per_share": 1, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 1

def test_fcf_yield_zero():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12


# --- Bonus marge ---

def test_margin_bonus_high():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 35, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 4

def test_margin_bonus_medium():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 22, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 3

def test_margin_bonus_low():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 15, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 1


# --- Bonus ROE ---

def test_roe_bonus_high():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 30, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 3

def test_roe_bonus_medium():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 18, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 2

def test_roe_bonus_low():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 10, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 1


# --- Bonus net cash ---

def test_cash_bonus_positive():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 500, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12 + 2

def test_cash_bonus_negative():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": -100, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 12


# --- All bonuses combined ---

def test_multiple_with_all_bonuses():
	# growth 22 → 32, fcf 6/100=6%→+5, margin 35→+4, roe 30→+3, cash +2 = 46
	data = {"eps": 5, "fcf_per_share": 6, "revenue_growth": 22, "roe": 30, "operating_margin": 35, "net_cash": 100, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 32 + 5 + 4 + 3 + 2

def test_multiple_no_bonus():
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 22, "roe": 5, "operating_margin": 5, "net_cash": -50, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_raw"] == 32


# --- Fair value et fallback FCF ---

def test_fair_value_uses_eps():
	data = {"eps": 10, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	# pe_ratio = 100/10 = 10, cap = 15, multiple_raw = 12, 12 < 15 → no cap
	assert result["fair_value"] == 10 * result["multiple"]

def test_fair_value_fallback_fcf_when_eps_zero():
	data = {"eps": 0, "fcf_per_share": 5, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["fair_value"] == 5 * result["multiple"]

def test_fair_value_fallback_fcf_when_eps_negative():
	data = {"eps": -3, "fcf_per_share": 5, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["fair_value"] == 5 * result["multiple"]

def test_fair_value_zero_when_both_non_positive():
	data = {"eps": -3, "fcf_per_share": -2, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["fair_value"] == 0
	assert result["upside"] == 0


# --- Upside ---

def test_upside_calculation():
	data = {"eps": 10, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	expected_upside = ((result["fair_value"] - 100) / 100) * 100
	assert result["upside"] == expected_upside

def test_upside_zero_when_price_zero():
	data = {"eps": 10, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 0}
	result = compute_valuation(data)
	assert result["upside"] == 0


# --- Capping ---

def test_multiple_cap_applied():
	# eps=5, price=40 → P/E=8, cap=12. Multiple raw: growth 2%→12, no bonus. 12 == 12 → no cap
	# eps=5, price=30 → P/E=6, cap=9. Multiple raw=12 > 9 → capped to 9
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 30}
	result = compute_valuation(data)
	assert result["multiple_capped"] is True
	assert result["multiple"] == 6 * 1.5  # 9
	assert result["multiple_raw"] == 12
	assert result["cap_reason"] == "market_pe"

def test_upside_cap_applied():
	# eps=10, price=50 → P/E=5, cap=7.5. Multiple raw: growth 35%→40+bonus → capped to 7.5
	# fair_value = 10*7.5 = 75, upside_raw = 50%. Under 60% → no upside cap.
	# Let's force upside > 60%: eps=10, price=20, growth=2 → multiple_raw=12, pe=2, cap=3
	# fair_value_raw = 10*3 = 30, upside_raw = 50%... still under.
	# Use no cap scenario: eps=0, fcf=0, price=100 → upside=0
	# Better: eps=10, price=10, growth=2 → pe=1, cap=1.5, fv=15, upside=50. Still <60.
	# Make it extreme: eps=20, price=100, growth=2 → pe=5, cap=7.5, mult_raw=12>7.5→capped
	# fv=20*7.5=150, upside=50. Hmm.
	# To get upside>60: need fair_value > 1.6*price. With pe cap at 1.5x pe_ratio:
	# fv = eps * 1.5 * pe = eps * 1.5 * (price/eps) = 1.5 * price → upside = 50%. Always 50% max with cap1.
	# So upside cap only triggers when pe_ratio=0 (eps<=0, no cap1). Use fcf fallback:
	data = {"eps": 0, "fcf_per_share": 20, "revenue_growth": 35, "roe": 30, "operating_margin": 35, "net_cash": 100, "current_price": 100}
	result = compute_valuation(data)
	# pe_ratio=0 (eps<=0), no cap1. multiple_raw=40+5+4+3+2=54. fv=20*54=1080. upside=980%
	assert result["upside_capped"] is True
	assert result["upside"] == 60
	assert result["upside_raw"] > 60

def test_no_cap_when_within_limits():
	# eps=5, price=100 → P/E=20, cap=30. Multiple raw=12 (growth 2%), 12 < 30 → no cap
	# fair_value=60, upside=-40%, within ±60 → no upside cap
	data = {"eps": 5, "fcf_per_share": 0, "revenue_growth": 2, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["multiple_capped"] is False
	assert result["upside_capped"] is False
	assert result["cap_reason"] is None

def test_negative_upside_cap():
	# eps=1, price=100 → P/E=100, cap=150. Multiple raw=8 (growth -5%), no bonus
	# fair_value=1*8=8, upside=-92% → capped to -60%
	data = {"eps": 1, "fcf_per_share": 0, "revenue_growth": -5, "roe": 0, "operating_margin": 0, "net_cash": 0, "current_price": 100}
	result = compute_valuation(data)
	assert result["upside_capped"] is True
	assert result["upside"] == -60
	assert result["upside_raw"] < -60


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
