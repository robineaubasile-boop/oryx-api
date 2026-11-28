"""Dormance de V0-T08 Lots 2A, 2B-1 et 2B-2 (bloc A) : les services
core/product_analytics/ existent mais ne sont raccordés à rien.

- aucun module applicatif (api.py, core/, scripts/, alembic/, ci/,
  diagnostics/, test.py) n'importe le paquet ; le vrai api:app, importé
  dans un sous-processus réseau coupé, ne le charge pas et n'expose aucune
  route nouvelle (même avec des variables évoquant une activation) ;
- le paquet n'importe que la bibliothèque standard utile, SQLAlchemy,
  core.models et lui-même : ni api, ni FastAPI / Starlette, ni F2
  (auth_f2*, authz_f2*, f2_runtime), ni route_policy (F-02A), ni moteurs
  pédagogiques, ni réseau, ni threads, ni planificateur, ni lecture
  d'environnement ;
- lot 2B-1 : seule la migration 0023_analytics_cohort_sessions a été
  ajoutée, 0022 inchangée ; lot 2B-2 bloc A : seule la migration
  0024_product_activity_proofs est ajoutée (tête, 24 fichiers), 0022 et
  0023 inchangées, aucun module du paquet ajouté ni modifié (aucun service
  du registre de preuves : tests/test_migration_0024_product_activity_proofs.py) ;
  cohort.py est le SEUL module qui
  lit des tables F2, uniquement via les modèles Invitation et
  AuthOperation de core.models (aucun module F2 importé, aucune écriture :
  tests/test_product_analytics_cohort.py) ;
- frontend : aucune référence au paquet ni aux services ;
- lot 2B-2 bloc B : un seul module ajouté au paquet (proofs.py, registre et
  certification des preuves) ; UNIQUE exception nominative hors du paquet,
  core/_activity_proof_bridge.py, qui n'importe que proofs et n'a aucun
  appelant (tests/test_activity_proof_bridge.py) ; aucun autre module du
  paquet n'importe proofs ;
- lot 2B-2 bloc C : un seul module ajouté au paquet (finalization.py,
  finalisation atomique des preuves certifiées), SEUL autre importateur de
  proofs (et de events) ; UNIQUE exception nominative supplémentaire hors du
  paquet, core/_activity_event_finalization.py (frontière privée de
  transaction), qui n'importe que finalization et n'a aucun appelant
  (tests/test_activity_event_finalization_chain.py) ;
- lot 2C PR 1 : un seul module ajouté au paquet (proof_lifecycle.py,
  sélection bornée et purge du registre provisoire), seul autre importateur
  de proofs et SEUL importateur de finalization dans le paquet (jamais de
  events) ; UNIQUE exception nominative supplémentaire hors du paquet,
  core/_activity_proof_lifecycle.py (frontière privée de transaction), qui
  n'importe que finalization et proof_lifecycle (plus la frontière du bloc C)
  et n'a aucun appelant (tests/test_activity_proof_lifecycle.py) ;
- lot 2C PR 2 : un seul module ajouté au paquet (event_lifecycle.py,
  expiration des finalités et suppression des événements et attributions),
  seul autre importateur de events (conventions de verrous de record_event),
  de finalization (conventions de verrous du bloc C) et de proofs (horloge,
  statut recorded) ; UNIQUE exception nominative supplémentaire hors du
  paquet, core/_activity_event_lifecycle.py (frontière privée de
  transaction), qui n'importe que event_lifecycle (plus la transaction du
  bloc C et core/_activity_transactions.py, sans import analytics) et n'a
  aucun appelant (tests/test_activity_event_lifecycle.py) ;
- lot 2C PR 3 : un seul module ajouté au paquet (participation_lifecycle.py,
  finalisation bornée des visites et purge atomique des participations),
  seul autre importateur de proofs (horloge PostgreSQL seulement) et de
  sessions (vocabulaire de fin de visite) ; jamais events ni finalization ;
  UNIQUE exception nominative supplémentaire hors du paquet,
  core/_activity_participation_lifecycle.py (frontière privée de
  transaction), qui n'importe que participation_lifecycle (plus la
  transaction du bloc C et core/_activity_transactions.py) et n'a aucun
  appelant (tests/test_activity_participation_lifecycle.py).
"""
import ast
import hashlib
import json
import subprocess
import sys

from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _script_directory

PACKAGE = REPO_ROOT / "core" / "product_analytics"
PACKAGE_MODULE = "core.product_analytics"
MODULES = ("__init__.py", "campaign_lifecycle.py", "campaigns.py", "cohort.py", "common.py", "event_lifecycle.py", "events.py",
           "finalization.py", "participation.py", "participation_lifecycle.py", "proof_lifecycle.py", "proofs.py",
           "retention.py", "sessions.py")
# V0-T08 Lot 2B-2 bloc B (nominatif) : UNIQUE module hors du paquet autorisé à
# l'importer, et seulement le registre des preuves ; lui-même sans appelant
# (tests/test_activity_proof_bridge.py).
PROOF_BRIDGE = "core/_activity_proof_bridge.py"
PROOF_BRIDGE_IMPORTS = {"core.product_analytics", "core.product_analytics.proofs"}
# V0-T08 Lot 2B-2 bloc C (nominatif) : frontière privée de transaction de la
# finalisation, qui n'importe que le service de finalisation ; elle-même sans
# appelant (tests/test_activity_event_finalization_chain.py).
FINALIZATION_BOUNDARY = "core/_activity_event_finalization.py"
FINALIZATION_BOUNDARY_IMPORTS = {"core.product_analytics", "core.product_analytics.finalization"}
# V0-T08 Lot 2C PR 1 (nominatif) : frontière privée du cycle de vie du
# registre, qui n'importe que la finalisation (bloc C, inchangé) et le cycle de
# vie ; elle-même sans appelant (tests/test_activity_proof_lifecycle.py).
LIFECYCLE_BOUNDARY = "core/_activity_proof_lifecycle.py"
LIFECYCLE_BOUNDARY_IMPORTS = {"core.product_analytics", "core.product_analytics.finalization",
                              "core.product_analytics.proof_lifecycle"}
# V0-T08 Lot 2C PR 2 (nominatif) : frontière privée du cycle de vie des
# événements, qui n'importe que ce service ; elle-même sans appelant
# (tests/test_activity_event_lifecycle.py).
EVENT_LIFECYCLE_BOUNDARY = "core/_activity_event_lifecycle.py"
EVENT_LIFECYCLE_BOUNDARY_IMPORTS = {"core.product_analytics", "core.product_analytics.event_lifecycle"}
# V0-T08 Lot 2C PR 3 (nominatif) : frontière privée du cycle de vie des
# participations, qui n'importe que ce service ; elle-même sans appelant
# (tests/test_activity_participation_lifecycle.py).
PARTICIPATION_LIFECYCLE_BOUNDARY = "core/_activity_participation_lifecycle.py"
PARTICIPATION_LIFECYCLE_BOUNDARY_IMPORTS = {"core.product_analytics", "core.product_analytics.participation_lifecycle"}
# V0-T08 Lot 2C PR 4 (nominatif) : frontière privée du cycle de vie des
# campagnes, qui n'importe que ce service ; elle-même sans appelant
# (tests/test_activity_campaign_lifecycle.py).
CAMPAIGN_LIFECYCLE_BOUNDARY = "core/_activity_campaign_lifecycle.py"
CAMPAIGN_LIFECYCLE_BOUNDARY_IMPORTS = {"core.product_analytics", "core.product_analytics.campaign_lifecycle"}
MIGRATION_0022 = REPO_ROOT / "alembic" / "versions" / "0022_product_analytics_v1.py"
# sha256 de 0022 telle que fusionnée sur main (PR #247, 47bb843) : le lot 2A
# ne la modifie pas.
MIGRATION_0022_SHA256 = "860305fd4b13a312347444fc31add80587ad58fa03c7f07818f7ec10013be053"
MIGRATION_0023 = REPO_ROOT / "alembic" / "versions" / "0023_analytics_cohort_sessions.py"
# sha256 de 0023 telle que proposée par le lot 2B-1 : toute retouche
# ultérieure doit être explicite (mise à jour de cette empreinte).
MIGRATION_0023_SHA256 = "896c33583892f8037eeb1bb3fd4af00548a8557e9548fca4070e76ca8a8fcb52"
MIGRATION_0024 = REPO_ROOT / "alembic" / "versions" / "0024_product_activity_proofs.py"
# sha256 de 0024 telle que proposée par le lot 2B-2 bloc A : toute retouche
# ultérieure doit être explicite (mise à jour de cette empreinte).
MIGRATION_0024_SHA256 = "e585a348e1165358da3ca7b784c2ff1ac87a9dbef8504ec1f5e09de55e84017c"
ALLOWED_IMPORTS = {"hashlib", "json", "re", "uuid", "dataclasses", "datetime", "sqlalchemy", "sqlalchemy.exc",
                   "core.models",
                   "core.product_analytics", "core.product_analytics.common", "core.product_analytics.campaigns",
                   "core.product_analytics.retention", "core.product_analytics.participation"}
# V0-T08 Lot 2B-2 bloc B, audit PR #253 (nominatif) : proofs.py émet l'origine
# d'une opération via le service 2B-1 (sessions) et garde les origines émises
# en références faibles (weakref) ; aucun autre module n'en bénéficie.
EXTRA_IMPORTS = {"proofs.py": {"weakref", "core.product_analytics.sessions"},
                 # V0-T08 Lot 2B-2 bloc C (nominatif) : la finalisation relit le
                 # registre (proofs) et écrit par le chemin certifié de events.
                 "finalization.py": {"core.product_analytics.events", "core.product_analytics.proofs"},
                 # V0-T08 Lot 2C PR 1 (nominatif) : le cycle de vie relit le registre
                 # (proofs) et reprend les vocabulaires et l'échéance du bloc C.
                 "proof_lifecycle.py": {"core.product_analytics.finalization", "core.product_analytics.proofs"},
                 # V0-T08 Lot 2C PR 2 (nominatif) : le cycle de vie des événements
                 # reprend les conventions de verrous des écrivains (events,
                 # finalization), l'horloge et les statuts du registre (proofs).
                 "event_lifecycle.py": {"core.product_analytics.events", "core.product_analytics.finalization",
                                        "core.product_analytics.proofs"},
                 # V0-T08 Lot 2C PR 3 (nominatif) : le cycle de vie des participations lit
                 # l'horloge PostgreSQL (proofs.server_clock) et le vocabulaire de fin de
                 # visite (sessions) ; ni events, ni finalization.
                 "participation_lifecycle.py": {"core.product_analytics.proofs", "core.product_analytics.sessions"},
                 # V0-T08 Lot 2C PR 4 (nominatif) : le cycle de vie des campagnes réutilise
                 # l'échéance et les contrôles de la PR 3 (participation_lifecycle), les
                 # clés et la résolution du bloc C (finalization), le contrôle canonique de
                 # la PR 2 (event_lifecycle), l'horloge et les vocabulaires du registre
                 # (proofs) ; horloge monotone du budget (time), photographies en valeurs
                 # (types.SimpleNamespace). Jamais events, sessions ni cohort.
                 "campaign_lifecycle.py": {"time", "types", "core.product_analytics.event_lifecycle",
                                           "core.product_analytics.finalization",
                                           "core.product_analytics.participation_lifecycle",
                                           "core.product_analytics.proofs"}}
# Modèles F2 lisibles par le paquet (lot 2B-1, cohort.py seulement) : preuve
# canonique invitation -> utilisateur. Aucun autre modèle F2.
F2_MODELS = {"Invitation", "AuthOperation", "AuthIdentity", "AuthPreauthCandidate", "AuthOtpRequest",
             "AuthOtpVerificationAttempt", "AuthAbuseSubject", "AuthAbuseEvent", "AuthAbuseKeyState", "OryxSession",
             "OryxSessionGeneration", "AuthOperationResult"}
F2_MODELS_READ = {"cohort.py": {"Invitation", "AuthOperation"}}


def _imports(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            if node.module in ("core", "core.product_analytics"):
                found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_package_is_exactly_the_expected_modules():
    assert sorted(p.name for p in PACKAGE.iterdir() if p.suffix == ".py") == sorted(MODULES)
    assert not [p for p in PACKAGE.iterdir() if p.is_dir() and p.name != "__pycache__"]
    # Importer le paquet n'importe aucun service.
    assert _imports(PACKAGE / "__init__.py") == set()


def test_package_imports_only_the_allowed_modules():
    for name in MODULES:
        imported = _imports(PACKAGE / name)
        allowed = ALLOWED_IMPORTS | EXTRA_IMPORTS.get(name, set())
        assert imported <= allowed, (name, imported - allowed)
        assert EXTRA_IMPORTS.get(name, set()) <= imported, name  # exception exacte, jamais élargie en silence
        source = (PACKAGE / name).read_text(encoding="utf-8")
        for marker in ("os.environ", "getenv", "__main__", "Thread", "asyncio", "schedule", "socket", "requests",
                       "httpx", "anthropic", "APIRouter", "FastAPI", "route_policy", "authz_f2", "auth_f2",
                       "f2_runtime"):
            assert marker not in source, (name, marker)


def test_no_application_module_imports_the_package():
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        if parts[0] in ("tests", ".git") or "node_modules" in parts or path.parent == PACKAGE:
            continue
        imported = _imports(path)
        text = path.read_text(encoding="utf-8")
        if path.relative_to(REPO_ROOT).as_posix() == PROOF_BRIDGE:
            # Exception nominative : imports analytics EXACTS (registre des
            # preuves seulement), aucun chargement dynamique.
            assert {i for i in imported if i.startswith(PACKAGE_MODULE)} == PROOF_BRIDGE_IMPORTS, imported
            assert "import_module" not in text and "__import__" not in text
            continue
        if path.relative_to(REPO_ROOT).as_posix() == FINALIZATION_BOUNDARY:
            # Exception nominative (bloc C) : imports analytics EXACTS
            # (service de finalisation seulement), aucun chargement dynamique.
            assert {i for i in imported if i.startswith(PACKAGE_MODULE)} == FINALIZATION_BOUNDARY_IMPORTS, imported
            assert "import_module" not in text and "__import__" not in text
            continue
        if path.relative_to(REPO_ROOT).as_posix() == EVENT_LIFECYCLE_BOUNDARY:
            # Exception nominative (lot 2C PR 2) : imports analytics EXACTS
            # (cycle de vie des événements seulement), aucun chargement dynamique.
            assert {i for i in imported if i.startswith(PACKAGE_MODULE)} == EVENT_LIFECYCLE_BOUNDARY_IMPORTS, imported
            assert "import_module" not in text and "__import__" not in text
            continue
        if path.relative_to(REPO_ROOT).as_posix() == PARTICIPATION_LIFECYCLE_BOUNDARY:
            # Exception nominative (lot 2C PR 3) : imports analytics EXACTS
            # (cycle de vie des participations seulement), aucun chargement dynamique.
            assert {i for i in imported if i.startswith(PACKAGE_MODULE)} == \
                PARTICIPATION_LIFECYCLE_BOUNDARY_IMPORTS, imported
            assert "import_module" not in text and "__import__" not in text
            continue
        if path.relative_to(REPO_ROOT).as_posix() == CAMPAIGN_LIFECYCLE_BOUNDARY:
            # Exception nominative (lot 2C PR 4) : imports analytics EXACTS
            # (cycle de vie des campagnes seulement), aucun chargement dynamique.
            assert {i for i in imported if i.startswith(PACKAGE_MODULE)} == \
                CAMPAIGN_LIFECYCLE_BOUNDARY_IMPORTS, imported
            assert "import_module" not in text and "__import__" not in text
            continue
        if path.relative_to(REPO_ROOT).as_posix() == LIFECYCLE_BOUNDARY:
            # Exception nominative (lot 2C PR 1) : imports analytics EXACTS
            # (finalisation et cycle de vie), aucun chargement dynamique.
            assert {i for i in imported if i.startswith(PACKAGE_MODULE)} == LIFECYCLE_BOUNDARY_IMPORTS, imported
            assert "import_module" not in text and "__import__" not in text
            continue
        if any(i == PACKAGE_MODULE or i.startswith(PACKAGE_MODULE + ".") for i in imported) or \
                "product_analytics." in text or "import_module" in text and "product_analytics" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
    for directory in ("web", "web-v2"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in (".html", ".js", ".mjs", ".css", ".json"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert "product_analytics" not in text and "record_event" not in text, path


def test_only_the_bridge_imports_the_proof_registry_service():
    for name in MODULES:
        # Lot 2C PR 1 : seul le cycle de vie importe la finalisation dans le paquet ;
        # lot 2C PR 2 (nominatif) : et le cycle de vie des événements (clés du bloc C).
        # lot 2C PR 4 (nominatif) : et le cycle de vie des campagnes (clés, échéance et
        # résolution du bloc C, jamais son chemin d'écriture d'événement).
        if name not in ("proof_lifecycle.py", "event_lifecycle.py", "campaign_lifecycle.py"):
            assert "core.product_analytics.finalization" not in _imports(PACKAGE / name), name
        if name == "event_lifecycle.py":
            # Exception nominative (lot 2C PR 2) : events, finalization et proofs, exactement.
            assert {"core.product_analytics.events", "core.product_analytics.finalization",
                    "core.product_analytics.proofs"} <= _imports(PACKAGE / name)
            continue
        if name == "campaign_lifecycle.py":
            # Exception nominative (lot 2C PR 4) : proofs et finalization, jamais events.
            imported = _imports(PACKAGE / name)
            assert {"core.product_analytics.proofs", "core.product_analytics.finalization"} <= imported
            assert "core.product_analytics.events" not in imported
            continue
        if name == "participation_lifecycle.py":
            # Exception nominative (lot 2C PR 3) : proofs (horloge seulement), jamais events.
            imported = _imports(PACKAGE / name)
            assert "core.product_analytics.proofs" in imported and "core.product_analytics.events" not in imported
            continue
        if name in ("finalization.py", "proof_lifecycle.py"):
            # Exceptions nominatives : relecture des preuves certifiées (bloc C),
            # sélection et purge du registre (lot 2C PR 1).
            assert "core.product_analytics.proofs" in _imports(PACKAGE / name)
            if name == "proof_lifecycle.py":
                assert "core.product_analytics.events" not in _imports(PACKAGE / name)
            continue
        assert "core.product_analytics.proofs" not in _imports(PACKAGE / name), name
        assert "import proofs" not in (PACKAGE / name).read_text(encoding="utf-8"), name
        # Seule la finalisation importe aussi le service d'événements (chemin certifié).
        assert "core.product_analytics.events" not in _imports(PACKAGE / name), name


def test_only_migration_0024_added_and_0022_0023_unchanged():
    script = _script_directory()
    assert script.get_heads() == ["0024_product_activity_proofs"]
    assert script.get_revision("0024_product_activity_proofs").down_revision == "0023_analytics_cohort_sessions"
    assert script.get_revision("0023_analytics_cohort_sessions").down_revision == "0022_product_analytics_v1"
    assert len(list((REPO_ROOT / "alembic" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))) == 24
    assert hashlib.sha256(MIGRATION_0022.read_bytes()).hexdigest() == MIGRATION_0022_SHA256
    assert hashlib.sha256(MIGRATION_0023.read_bytes()).hexdigest() == MIGRATION_0023_SHA256
    assert hashlib.sha256(MIGRATION_0024.read_bytes()).hexdigest() == MIGRATION_0024_SHA256


def test_only_cohort_reads_f2_tables_and_only_the_canonical_invitation_proof():
    for name in MODULES:
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    and node.module == "core.models" for alias in node.names}
        assert imported & F2_MODELS == F2_MODELS_READ.get(name, set()), name


PROBE = r'''
import json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit pendant le test de dormance")
socket.socket.connect = guard
socket.create_connection = lambda *a, **k: guard(None, a[0] if a else None)

import api
routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
loaded = sorted(name for name in sys.modules if name == "core.product_analytics"
                or name.startswith("core.product_analytics."))
print(json.dumps({"routes": routes, "loaded": loaded, "attempts": attempts}))
'''


def test_real_api_never_loads_the_services_whatever_the_environment():
    from tests.test_f2_dormancy import MAIN_ROUTES

    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1",
                             "ORYX_ANALYTICS_CAMPAIGN": "wave-0", "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["loaded"] == []
        assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
            set(MAIN_ROUTES))
        assert result["attempts"] == []
