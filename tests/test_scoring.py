from core.scoring import (
	growth_score,
	quality_score,
	moat_score,
	structure_score,
	compute_score,
	get_verdict,
	detect_moat,
	business_quality,
	SCORE_MAX,
)


# --- growth_score (max 4) ---

def test_growth_score_high():
	data = {"revenue_growth": 20, "growth": 20}
	assert growth_score(data) == 4

def test_growth_score_medium():
	data = {"revenue_growth": 10, "growth": 10}
	assert growth_score(data) == 2

def test_growth_score_low():
	data = {"revenue_growth": 3, "growth": 3}
	assert growth_score(data) == 0

def test_growth_score_negative_eps():
	data = {"revenue_growth": 20, "growth": -5}
	assert growth_score(data) == 1  # 2 (revenue) - 1 (negative eps)

def test_growth_score_empty_data():
	assert growth_score({}) == 0


# --- quality_score (max 5) ---

def test_quality_score_max():
	data = {"operating_margin": 30, "roe": 25, "roic": 25, "fcf_per_share": 5}
	assert quality_score(data) == 5  # 1 (fcf) + 1 (margin) + 1 (roe) + 2 (roic)

def test_quality_score_roic_medium():
	data = {"operating_margin": 10, "roe": 10, "roic": 15, "fcf_per_share": 2}
	assert quality_score(data) == 2  # 1 (fcf) + 0 (margin) + 0 (roe) + 1 (roic)

def test_quality_score_no_fcf():
	data = {"operating_margin": 30, "roe": 25, "roic": 25, "fcf_per_share": 0}
	assert quality_score(data) == 4  # 0 (fcf) + 1 (margin) + 1 (roe) + 2 (roic)

def test_quality_score_zero():
	data = {"operating_margin": 5, "roe": 5, "roic": 5, "fcf_per_share": 0}
	assert quality_score(data) == 0


# --- moat_score (max 4) ---

def test_moat_score_max():
	data = {"roic": 25, "operating_margin": 30, "revenue_growth": 10}
	assert moat_score(data) == 4  # 2 (roic+margin) + 2 (growth+margin)

def test_moat_score_medium():
	data = {"roic": 18, "operating_margin": 20, "revenue_growth": 8}
	assert moat_score(data) == 3  # 1 (roic+margin) + 2 (growth+margin)

def test_moat_score_growth_only():
	data = {"roic": 5, "operating_margin": 12, "revenue_growth": 3}
	assert moat_score(data) == 1  # 0 + 1 (growth>0, margin>10)

def test_moat_score_zero():
	data = {"roic": 5, "operating_margin": 5, "revenue_growth": -2}
	assert moat_score(data) == 0


# --- structure_score (max 3) ---

def test_structure_score_max():
	data = {"net_cash": 100, "debt_to_equity": 0.3}
	assert structure_score(data) == 3  # 2 (cash) + 1 (low debt)

def test_structure_score_cash_only():
	data = {"net_cash": 100, "debt_to_equity": 1.0}
	assert structure_score(data) == 2

def test_structure_score_low_debt_only():
	data = {"net_cash": -50, "debt_to_equity": 0.3}
	assert structure_score(data) == 1

def test_structure_score_high_debt():
	data = {"net_cash": -50, "debt_to_equity": 2.5}
	assert structure_score(data) == -1  # 0 - 1

def test_structure_score_zero():
	data = {"net_cash": -50, "debt_to_equity": 1.0}
	assert structure_score(data) == 0


# --- compute_score ---

def test_score_max_constant():
	assert SCORE_MAX == 16

def test_compute_score_perfect():
	data = {
		"revenue_growth": 20,
		"growth": 20,
		"operating_margin": 30,
		"roe": 25,
		"roic": 25,
		"fcf_per_share": 5,
		"net_cash": 100,
		"debt_to_equity": 0.3,
	}
	assert compute_score(data) == 10.0

def test_compute_score_zero():
	data = {
		"revenue_growth": 0,
		"growth": -10,
		"operating_margin": 0,
		"roe": 0,
		"roic": 0,
		"fcf_per_share": 0,
		"net_cash": -100,
		"debt_to_equity": 1.0,
	}
	assert compute_score(data) == 0.0

def test_compute_score_never_negative():
	data = {"revenue_growth": 0, "growth": -50, "debt_to_equity": 5}
	assert compute_score(data) >= 0

def test_compute_score_netflix():
	"""Netflix-like: CAGR 11%, margin 29.5%, ROE 43%, ROIC 36%, D/E 0.54, FCF 2.24"""
	data = {
		"revenue_growth": 11,
		"growth": 16,
		"operating_margin": 29.5,
		"roe": 43,
		"roic": 36,
		"fcf_per_share": 2.24,
		"net_cash": -5000,
		"debt_to_equity": 0.54,
	}
	score = compute_score(data)
	# growth=1+2=3, quality=5, moat=4, structure=0 → 12/16=7.5
	assert 7 <= score <= 9.5

def test_compute_score_schneider():
	"""Schneider-like: CAGR 8.5%, margin 17.5%, ROE 15.6%, ROIC 14.2%, D/E 0.73, FCF 8.16"""
	data = {
		"revenue_growth": 8.5,
		"growth": 9,
		"operating_margin": 17.5,
		"roe": 15.6,
		"roic": 14.2,
		"fcf_per_share": 8.16,
		"net_cash": -3000,
		"debt_to_equity": 0.73,
	}
	score = compute_score(data)
	# growth=1+1=2, quality=1+0+0+1=2, moat=0+2=2, structure=0 → 6/16=3.8
	assert 3.5 <= score <= 6


# --- get_verdict ---

def test_verdict_excellente():
	assert get_verdict(9.5) == "Excellente entreprise"

def test_verdict_solide():
	assert get_verdict(7.5) == "Entreprise solide"

def test_verdict_correcte():
	assert get_verdict(5.5) == "Entreprise correcte"

def test_verdict_faible():
	assert get_verdict(3.0) == "Entreprise faible"

def test_verdict_boundaries():
	assert get_verdict(9) == "Excellente entreprise"
	assert get_verdict(7) == "Entreprise solide"
	assert get_verdict(5) == "Entreprise correcte"
	assert get_verdict(4.9) == "Entreprise faible"


# --- Legacy label functions ---

def test_detect_moat_labels():
	assert detect_moat({"roic": 25, "operating_margin": 30, "revenue_growth": 10}) == "fort"
	assert detect_moat({"roic": 18, "operating_margin": 20, "revenue_growth": 8}) == "fort"  # 1+2=3 → fort
	assert detect_moat({"roic": 10, "operating_margin": 12, "revenue_growth": 3}) == "modéré"  # 0+1=1
	assert detect_moat({"roic": 5, "operating_margin": 5, "revenue_growth": -2}) == "faible"

def test_business_quality_labels():
	assert business_quality({"operating_margin": 30, "roe": 25, "roic": 25, "fcf_per_share": 5}) == "exceptionnelle"
	assert business_quality({"operating_margin": 30, "roe": 25, "roic": 25, "fcf_per_share": 0}) == "solide"
	assert business_quality({"operating_margin": 10, "roe": 10, "roic": 15, "fcf_per_share": 2}) == "correcte"
	assert business_quality({"operating_margin": 5, "roe": 5, "roic": 5, "fcf_per_share": 0}) == "faible"
