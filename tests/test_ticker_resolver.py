from core.ticker_resolver import _pick_best_match

def _candidate(code, exchange, ctype, name, country=""):
	return {"Code": code, "Exchange": exchange, "Type": ctype, "Name": name, "Country": country}

def test_amazon_prefers_us_over_xetra_cross_listing():
	results = [
		_candidate("AMZN", "US", "Common Stock", "Amazon.com Inc", "USA"),
		_candidate("AMZ", "XETRA", "Common Stock", "Amazon.com Inc", "UK"),
	]
	assert _pick_best_match(results, prefer_us=False, query="AMAZON") == "AMZN"

def test_siemens_stays_on_xetra_not_regressed():
	results = [
		_candidate("SIE", "XETRA", "Common Stock", "Siemens AG", "Germany"),
		_candidate("SIEGY", "US", "Common Stock", "Siemens AG", "Germany"),
	]
	assert _pick_best_match(results, prefer_us=False, query="SIEMENS") == "SIE.XETRA"

def test_merck_us_group_prefers_its_own_us_listing():
	results = [
		_candidate("MRK", "US", "Common Stock", "Merck & Co Inc", "USA"),
		_candidate("MRK", "XETRA", "Common Stock", "Merck & Co Inc", "USA"),
	]
	assert _pick_best_match(results, prefer_us=False, query="MERCK") == "MRK"

def test_merck_kgaa_group_stays_on_xetra_not_regressed():
	results = [
		_candidate("MRK", "XETRA", "Common Stock", "Merck KGaA", "Germany"),
		_candidate("MKGAY", "US", "Common Stock", "Merck KGaA", "Germany"),
	]
	assert _pick_best_match(results, prefer_us=False, query="MERCK") == "MRK.XETRA"

def test_french_only_company_unaffected():
	results = [_candidate("XYZ", "PA", "Common Stock", "Une Entreprise Francaise SA", "France")]
	assert _pick_best_match(results, prefer_us=False, query="UNE ENTREPRISE FRANCAISE") == "XYZ.PA"

def test_us_ticker_prefer_us_path_unaffected():
	results = [
		_candidate("NVDA", "US", "Common Stock", "NVIDIA Corp", "USA"),
		_candidate("NVD", "XETRA", "Common Stock", "NVIDIA Corp", "USA"),
	]
	assert _pick_best_match(results, prefer_us=True, query="NVDA") == "NVDA"
