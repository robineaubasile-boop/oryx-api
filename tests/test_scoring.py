from core.scoring import (
	growth_score,
	quality_score,
	moat_score,
	stability_score,
	biz_quality_score,
	compute_score,
	get_verdict,
	detect_moat,
	business_stability,
	business_quality,
	SCORE_MAX,
)


# --- growth_score ---

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


# --- quality_score ---

def test_quality_score_max():
	data = {"operating_margin": 30, "roe": 25, "net_cash": 100}
	assert quality_score(data) == 5

def test_quality_score_medium():
	data = {"operating_margin": 20, "roe": 15, "net_cash": -50}
	assert quality_score(data) == 2

def test_quality_score_zero():
	data = {"operating_margin": 5, "roe": 5, "net_cash": -100}
	assert quality_score(data) == 0


# --- moat_score ---

def test_moat_score_fort():
	data = {"roic": 25, "operating_margin": 30}
	assert moat_score(data) == 2

def test_moat_score_modere():
	data = {"roic": 15, "operating_margin": 10}
	assert moat_score(data) == 1

def test_moat_score_faible():
	data = {"roic": 5, "operating_margin": 10}
	assert moat_score(data) == 0


# --- stability_score ---

def test_stability_score_stable():
	data = {"revenue_growth": 10, "operating_margin": 15}
	assert stability_score(data) == 2

def test_stability_score_moyenne():
	data = {"revenue_growth": 3, "operating_margin": 5}
	assert stability_score(data) == 1

def test_stability_score_volatile():
	data = {"revenue_growth": -5, "operating_margin": 5}
	assert stability_score(data) == 0


# --- biz_quality_score ---

def test_biz_quality_exceptionnelle():
	data = {"roe": 25, "operating_margin": 30}
	assert biz_quality_score(data) == 3

def test_biz_quality_solide():
	data = {"roe": 15, "operating_margin": 20}
	assert biz_quality_score(data) == 2

def test_biz_quality_correcte():
	data = {"roe": 10, "operating_margin": 5}
	assert biz_quality_score(data) == 1

def test_biz_quality_faible():
	data = {"roe": 5, "operating_margin": 5}
	assert biz_quality_score(data) == 0


# --- compute_score ---

def test_score_max_constant():
	assert SCORE_MAX == 16

def test_compute_score_perfect():
	data = {
		"revenue_growth": 20,
		"growth": 20,
		"operating_margin": 30,
		"roe": 25,
		"net_cash": 100,
		"roic": 25,
	}
	assert compute_score(data) == 10.0

def test_compute_score_zero():
	data = {
		"revenue_growth": 0,
		"growth": -10,
		"operating_margin": 0,
		"roe": 0,
		"net_cash": -100,
		"roic": 0,
	}
	assert compute_score(data) == 0.0

def test_compute_score_never_negative():
	data = {"revenue_growth": 0, "growth": -50}
	assert compute_score(data) >= 0


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
	assert detect_moat({"roic": 25, "operating_margin": 30}) == "fort"
	assert detect_moat({"roic": 15, "operating_margin": 10}) == "modéré"
	assert detect_moat({"roic": 5, "operating_margin": 10}) == "faible"

def test_business_stability_labels():
	assert business_stability({"revenue_growth": 10, "operating_margin": 15}) == "stable"
	assert business_stability({"revenue_growth": 3, "operating_margin": 5}) == "moyenne"
	assert business_stability({"revenue_growth": -5, "operating_margin": 5}) == "volatile"

def test_business_quality_labels():
	assert business_quality({"roe": 25, "operating_margin": 30}) == "exceptionnelle"
	assert business_quality({"roe": 15, "operating_margin": 20}) == "solide"
	assert business_quality({"roe": 10, "operating_margin": 5}) == "correcte"
	assert business_quality({"roe": 5, "operating_margin": 5}) == "faible"
