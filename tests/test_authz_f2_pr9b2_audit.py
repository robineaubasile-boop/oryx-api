"""R1 PR 9-B2 — régressions du correctif d'audit (P1-01, P1-02, P2-01, P2-02).

Sans base de données (les propagations à travers l'orchestrateur réel sont
dans tests/test_authz_f2_pr9b2.py) :

- P1-01 : lookup_method(..., log=False) sélectionne EXACTEMENT la même
  méthode que l'appel historique, sur toutes les branches, sans rien
  imprimer ; l'appel historique imprime toujours ; aucun mécanisme global
  (stdout, print) n'est touché : deux exécutions concurrentes gardent
  chacune leur comportement ;
- P1-02 : la DERNIÈRE ligne chronologique d'un indicateur l'emporte, même
  NULL (règle historique du dict {type: valeur}) ; provenance, empreinte et
  prompt suivent cette dernière ligne ;
- P2-01 : fraîcheur du prix fermée (recent, dated, undated, unavailable) ;
  aucun prix n'est « actuel » sans seuil explicitement fourni ; jamais de
  date ni de fraîcheur inventées ;
- P2-02 : number() ne lève jamais sur une valeur extrême ; tout champ dérivé
  non fini est absent ; zéro réel et absence restent distincts.
"""
import ast
import inspect
import threading
from datetime import datetime, timedelta, timezone

import pytest

from core import pedagogie_library as library
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_market_data as dm
from core.decryptage_engine import build_system_prompt
from tests.authz_f2_pr9b2_support import FMP_PROFILE, FMP_RATIOS, REALTIME, fundamentals
from tests.test_authz_f2_pr9b2_units import MC, preparation_view

# --------------------------------------------------------------------------
# P1-01 — sélection de méthode sans journaliser la question
# --------------------------------------------------------------------------

SECRET = "SECRET-QUESTION-7f3a"
# Chaque branche de lookup_method (vide, verrou de thèse, sous-méthode,
# méthode principale, question trop courte, reprise de session, aucune
# correspondance), avec et sans verrou de thèse.
SELECTION_CASES = [
    ("", None, None, True),
    (f"{SECRET} et pour la suite ?", "historique", "construction_these", True),
    (f"{SECRET} quel est le moat de cette entreprise", "", None, True),
    (f"{SECRET} comment lire le bilan", "", None, True),
    (f"{SECRET} quel PER", None, None, True),
    (f"{SECRET} management", "", None, True),
    (f"{SECRET}", None, None, True),
    (f"{SECRET} ok", "ctx", "ratios_valorisation", True),
    (f"{SECRET} on avance, continue s'il te plaît vraiment", "ctx", "bilan_financier", True),
    (f"{SECRET} rien de financier dans cette longue phrase ici", "ctx", None, True),
    (f"{SECRET} et pour la suite ?", "historique", "construction_these", False),
    (f"{SECRET} ok", "ctx", "inconnue", True),
]


@pytest.mark.parametrize("question,context,last_method_id,lock", SELECTION_CASES)
def test_the_silent_selection_is_exactly_the_historical_one(question, context, last_method_id, lock, capsys):
    historical = library.lookup_method(question, context=context, last_method_id=last_method_id,
                                       allow_these_lock=lock)
    printed = capsys.readouterr().out
    silent = library.lookup_method(question, context=context, last_method_id=last_method_id, allow_these_lock=lock,
                                   log=False)
    assert silent == historical  # même méthode, mêmes mots-clés, même provenance
    assert capsys.readouterr().out == ""  # rien n'est imprimé
    assert printed.startswith("[PEDAGOGIE]")  # le comportement historique imprime toujours
    if question:
        assert SECRET in printed


def test_the_historical_signature_default_is_unchanged():
    parameters = inspect.signature(library.lookup_method).parameters
    assert list(parameters) == ["question", "context", "last_method_id", "allow_these_lock", "log"]
    assert parameters["log"].default is True and parameters["log"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        library.lookup_method("question longue ici", None, None, True, False)  # jamais positionnel


def test_every_print_of_the_library_is_guarded_by_the_explicit_flag():
    tree = ast.parse(inspect.getsource(library.lookup_method))
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "log":
            guarded |= {id(n) for n in ast.walk(node) if isinstance(n, ast.Call)}
    prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "print"]
    assert prints and all(id(p) in guarded for p in prints)
    source = inspect.getsource(library)
    for forbidden in ("redirect_stdout", "sys.stdout", "builtins"):
        assert forbidden not in source


def test_the_f2_path_calls_the_selection_silently_and_nothing_global():
    source = inspect.getsource(dg)
    assert "lookup_method(lookup_text, context=context, last_method_id=self._reservation.last_method_id, " \
           "log=False)" in source
    for forbidden in ("redirect_stdout", "sys.stdout", "builtins"):
        assert forbidden not in source
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)}
    assert "print" not in calls


def test_concurrent_silent_and_historical_selections_keep_their_own_behavior(capsys):
    barrier = threading.Barrier(2, timeout=20)
    results = {}

    def historical():
        barrier.wait()
        results["historical"] = [library.lookup_method("HIST-QUESTION quel est le moat", context="")
                                 for _ in range(200)]

    def silent():
        barrier.wait()
        results["silent"] = [library.lookup_method(f"{SECRET} quel est le moat", context="", log=False)
                             for _ in range(200)]

    threads = [threading.Thread(target=historical), threading.Thread(target=silent)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    out = capsys.readouterr().out
    assert out.count("HIST-QUESTION") == 200  # aucune suppression globale n'affecte l'appelant historique
    assert SECRET not in out
    assert {m["method_id"] for m in results["silent"]} == {m["method_id"] for m in results["historical"]}


# --------------------------------------------------------------------------
# P1-02 — la dernière ligne chronologique l'emporte, même NULL
# --------------------------------------------------------------------------

def facts(*rows):
    return tuple(dc.PreparationFact(i + 1, kind, value) for i, (kind, value) in enumerate(rows))


def snapshot(rows):
    return dcand.build_snapshot(preparation_view(facts=facts(*rows)), "MC.PA", "construction_these",
                                dcand.BASIS_ACTIVE_ANALYSIS)


def test_a_null_latest_value_wins_over_an_older_known_value():  # A
    used = dcand.used_facts(facts(("roe", 20.0), ("roe", None)))
    assert [(f.fact_id, f.value) for f in used] == [(2, None)]


def test_a_known_latest_value_wins_over_an_older_null():  # B
    used = dcand.used_facts(facts(("roe", None), ("roe", 20.0)))
    assert [(f.fact_id, f.value) for f in used] == [(2, 20.0)]


def test_the_latest_of_several_known_values_wins():  # C
    used = dcand.used_facts(facts(("roe", 18.0), ("eps", 2.0), ("roe", 20.0), ("roe", 24.0)))
    assert [(f.fact_id, f.fact_type, f.value) for f in used] == [(2, "eps", 2.0), (4, "roe", 24.0)]


def test_types_outside_the_prompt_vocabulary_are_excluded():  # D
    used = dcand.used_facts(facts(("inconnu", 1.0), ("roe", 20.0), ("inconnu", None)))
    assert [f.fact_type for f in used] == ["roe"]


def test_the_snapshot_carries_the_provenance_of_the_true_latest_value():  # E
    snap = snapshot([("roe", 20.0), ("roe", None)])
    assert snap.facts_used == (dc.PreparationFact(2, "roe", None),)
    assert snap.canonical()["facts"] == [{"id": 2, "type": "roe", "value": None}]


def test_the_fingerprint_follows_the_latest_value():  # F
    base = snapshot([("roe", 20.0), ("roe", 24.0)]).fingerprint
    assert snapshot([("roe", 20.0), ("roe", None)]).fingerprint != base
    assert snapshot([("roe", 20.0), ("roe", 25.0)]).fingerprint != base
    assert snapshot([("roe", 19.0), ("roe", 24.0)]).fingerprint == base  # seule la dernière ligne compte
    assert snapshot([("roe", 20.0), ("roe", None)]).fingerprint == snapshot([("roe", 20.0),
                                                                             ("roe", None)]).fingerprint


def test_the_prompt_never_reuses_an_older_value_that_became_unavailable():  # G
    analysis = snapshot([("roe", 20.0), ("roe", None)]).in_progress_analysis()
    assert analysis["facts"] == {"roe": "N/D"}  # jamais 20.0, jamais un zéro
    prompt = build_system_prompt({"name": "LVMH", "currency": "EUR"}, None, "debutant", None, analysis)
    assert "- ROE : N/D" in prompt and "20.0" not in prompt


# --------------------------------------------------------------------------
# P2-01 — fraîcheur du prix
# --------------------------------------------------------------------------

REFERENCE = datetime.fromtimestamp(REALTIME["timestamp"], tz=timezone.utc) + timedelta(hours=2)


def test_a_dated_quote_is_dated_never_current_without_an_explicit_threshold():
    snap = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), REFERENCE, None)
    assert (snap.price_freshness, snap.value("current_price"), snap.price_as_of) == (
        dm.PRICE_DATED, 600.5, datetime.fromtimestamp(REALTIME["timestamp"], tz=timezone.utc).isoformat())
    assert snap.price_reference_at == REFERENCE.isoformat()
    assert snap.prompt_data()["current_price"] is None  # jamais « Prix actuel » sans fraîcheur établie
    assert snap.public_price() is None


def test_a_quote_within_an_explicit_threshold_is_recent():
    snap = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), REFERENCE, timedelta(days=1))
    assert snap.price_freshness == dm.PRICE_RECENT
    assert snap.prompt_data()["current_price"] == 600.5 and snap.public_price() == 600.5


@pytest.mark.parametrize("age", [timedelta(days=30), timedelta(days=1, seconds=1)])
def test_a_manifestly_old_quote_stays_dated(age):
    reference = datetime.fromtimestamp(REALTIME["timestamp"], tz=timezone.utc) + age
    snap = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), reference, timedelta(days=1))
    assert snap.price_freshness == dm.PRICE_DATED and snap.prompt_data()["current_price"] is None


def test_a_quote_dated_after_the_reference_is_never_recent():
    reference = datetime.fromtimestamp(REALTIME["timestamp"], tz=timezone.utc) - timedelta(minutes=1)
    snap = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), reference, timedelta(days=1))
    assert snap.price_freshness == dm.PRICE_DATED


@pytest.mark.parametrize("realtime", [dict(REALTIME, timestamp=None), dict(REALTIME, timestamp="1793000000"),
                                      {k: v for k, v in REALTIME.items() if k != "timestamp"}])
def test_a_quote_without_a_reliable_timestamp_is_undated(realtime):
    snap = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), realtime), REFERENCE, timedelta(days=1))
    assert (snap.price_freshness, snap.price_as_of, snap.value("current_price")) == (dm.PRICE_UNDATED, None, 600.5)
    assert snap.prompt_data()["current_price"] is None and snap.public_price() is None


def test_a_missing_quote_is_unavailable():
    snap = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), None), REFERENCE, timedelta(days=1))
    assert (snap.price_freshness, snap.value("current_price")) == (dm.PRICE_UNAVAILABLE, None)


def test_an_fmp_profile_price_has_no_reliable_quote_date():
    snap = dm.qualify_price(dm.parse_fmp(MC, FMP_PROFILE, FMP_RATIOS), REFERENCE, timedelta(days=365))
    assert (snap.price_freshness, snap.price_as_of, snap.value("current_price")) == (dm.PRICE_UNDATED, None, 601.0)
    assert snap.public_price() is None


def test_the_provenance_block_never_calls_a_non_recent_quote_current():
    dated = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), REFERENCE, None)
    text = dg.provenance_block(dated)
    assert "Dernier cours connu : 600.5 EUR" in text and "ne le présente jamais comme le cours actuel" in text
    undated = dm.qualify_price(dm.parse_fmp(MC, FMP_PROFILE, FMP_RATIOS), REFERENCE, None)
    assert "sans date fiable" in dg.provenance_block(undated)
    missing = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), None), REFERENCE, None)
    assert "Cours : non disponible" in dg.provenance_block(missing)
    recent = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), REFERENCE, timedelta(days=1))
    assert "Cours récent vérifié" in dg.provenance_block(recent)


def test_the_freshness_is_part_of_the_market_fingerprint():
    dated = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), REFERENCE, None)
    recent = dm.qualify_price(dm.parse_eodhd(MC, fundamentals(), REALTIME), REFERENCE, timedelta(days=1))
    assert dcand.market_fingerprint(dated) != dcand.market_fingerprint(recent)
    with pytest.raises(ValueError):
        dm.qualify_price(dated, REFERENCE.replace(tzinfo=None), None)
    with pytest.raises(ValueError):
        dm.qualify_price(dated, REFERENCE, timedelta(0))


def test_the_orchestrator_threshold_is_explicit_and_absent_by_default():
    parameters = inspect.signature(dg.DecryptageGenerationOrchestrator).parameters
    assert parameters["price_recent_max_age"].default is None


# --------------------------------------------------------------------------
# P2-02 — conversions numériques sûres
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [10**400, -(10**400), "1e400", "-1e400", "9" * 400, float("nan"), float("inf"),
                                   float("-inf"), "NaN", "Infinity", True, False, None, [], {}, "", " "])
def test_unrepresentable_or_invalid_numbers_are_absent_never_an_error(value):
    assert dm.number(value) is None


@pytest.mark.parametrize("value,expected", [(0, 0.0), ("0", 0.0), (0.0, 0.0), (-0.0, 0.0), (12, 12.0),
                                            ("-3.5", -3.5), ("1e3", 1000.0), (10**300, 1e300)])
def test_valid_numbers_including_a_real_zero_are_kept(value, expected):
    assert dm.number(value) == expected and type(dm.number(value)) is float


def test_a_real_zero_and_an_absence_stay_distinct():
    zero = dm.parse_eodhd(MC, fundamentals(Highlights__EarningsShare=0), REALTIME)
    absent = dm.parse_eodhd(MC, fundamentals(Highlights__EarningsShare=None), REALTIME)
    assert (zero.field("eps").status, zero.value("eps")) == ("present", 0.0)
    assert (absent.field("eps").status, absent.value("eps")) == ("absent", None)


@pytest.mark.parametrize("overrides", [
    dict(Highlights__ReturnOnEquityTTM=10**400), dict(Highlights__ReturnOnEquityTTM=1e307),
    {"Financials__Income_Statement__yearly__2025-12-31__operatingIncome": 10**400},
    {"Financials__Balance_Sheet__yearly__2025-12-31__cashAndShortTermInvestments": 1.7e308,
     "Financials__Balance_Sheet__yearly__2025-12-31__shortLongTermDebtTotal": -1.7e308},
    dict(SharesStats__SharesOutstanding=1e-320)])
def test_extreme_provider_values_never_interrupt_the_parsing(overrides):
    snap = dm.parse_eodhd(MC, fundamentals(**overrides), REALTIME)
    assert snap is not None
    for field in snap.fields:
        assert field.value is None or (type(field.value) is float and field.value == field.value
                                       and abs(field.value) != float("inf"))


def test_extreme_fmp_values_never_interrupt_the_fallback():
    snap = dm.parse_fmp(MC, [dict(FMP_PROFILE[0], price=10**400)],
                        [dict(FMP_RATIOS[0], returnOnEquity=1e307, operatingProfitMargin=10**400)])
    assert snap is not None and snap.value("current_price") is None
    assert snap.field("roe").status == "absent" and snap.field("operating_margin").status == "absent"


def test_programming_errors_are_not_masked_by_the_conversion():
    tree = ast.parse(inspect.getsource(dm.number))
    handlers = [h for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)]
    # Seule la non-représentabilité binary64 est traduite en absence.
    assert [ast.unparse(h.type) for h in handlers] == ["OverflowError"]


@pytest.mark.parametrize("bad", [0, 1.5, timedelta(0), timedelta(seconds=-1), "1d"])
def test_an_invalid_freshness_threshold_is_refused_at_construction(bad):
    from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock, FakeEodhd, FakeFmp
    from tests.test_authz_f2_pr9b2_units import _unconnected_runner
    clock = FakeClock()
    with pytest.raises(TypeError):
        dg.DecryptageGenerationOrchestrator(_unconnected_runner()[1], eodhd=FakeEodhd(clock), fmp=FakeFmp(clock),
                                            claude=FakeClaude(clock), model="m", price_recent_max_age=bad)
