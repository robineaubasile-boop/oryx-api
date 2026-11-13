# ACT-L1 / F04-C V0.3 — Implémentation dormante et traçabilité

## 1. Référentiel et statut

| Élément | Valeur |
|---|---|
| Chantier | Activation F2 Lot 1 (ACT-L1), lot **F04-C** |
| Contrat normatif | `docs/act_l1_f04c_freeze_v03.md` (transcription littérale, provenance en en-tête) |
| Validation | freeze limité F04-C V0.3 **validé par le fondateur le 13/11/2026** (implémentation dormante uniquement) |
| `main` relu avant implémentation | `82378d731cbd1a9ce688217bac0f426575d8062c` — identique à la référence auditée du freeze : aucun conflit |
| Rapport préalable | Q1/Q2 V0.1 : Q1 = NO-GO-A, Q2 = ÉCART À ARBITRER → décisions fondateur B, double registre, O1 |
| Activation F2 | **Aucune** : `api:app` reste l'unique point d'entrée servi ; aucun fichier Railway, aucune variable |

En cas de divergence, le freeze prévaut. Ce document ne crée aucune exigence.

## 2. Contenu du lot

| Fichier | Nature | Objet |
|---|---|---|
| `core/f2_runtime/work_registry.py` | **nouveau** | double registre (admissions + travaux physiques), soumetteur `RegisteredSubmitter` |
| `core/f2_runtime/entrypoint.py` | **nouveau** | `F2Envelope` (état, admission, maintenance, lifespan), `compose_f2`, contrôles de composition, `app` inerte |
| `core/auth_f2_http/routes.py` | modifié (site 1) | champ `Dependencies.submit` (défaut `None`) ; `call()` soumet par `deps.submit` |
| `core/authz_f2_http/routes.py` | modifié (sites 2, 3) | champ `PersonalDependencies.submit` (défaut `None`) ; `me`, `me_level` |
| `core/authz_f2_http/portfolio.py` | modifié (site 4) | `run` |
| `core/authz_f2_http/consultation.py` | modifié (site 5) | `run` |
| `core/authz_f2_http/decryptage.py` | modifié (sites 6, 7) | `_submit(work, submit=None)` ; `turns` ; `single` (`/replay`, `/ack`) |
| `core/auth_f2_http/isolated.py` | modifié (injection) | paramètre `submitter=None` ; `app.state.oryx_f2_submitter` |
| `core/authz_f2_http/isolated.py` | modifié (injection) | paramètre `submitter=None` ; `app.state.oryx_f2_submitter` |
| `tests/f04c_support.py`, `tests/f04c_q2_probe.py`, `tests/f04c_signal_child.py` | nouveaux | banc d'essai, sonde Q2, enfant « signaux réels » |
| `tests/test_f04c_*.py` (6 fichiers) | nouveaux | tests du chapitre 8 |
| `docs/act_l1_f04c_freeze_v03.md`, ce document | nouveaux | freeze transcrit, traçabilité |

Les sept fichiers existants modifiés sont exactement ceux du § 2 du freeze. Aucun autre fichier existant n'est
modifié (voir § 6, écart E-1 : sept tests de dormance existants échouent et demandent un arbitrage).

## 3. Architecture implémentée

### 3.1 Double registre (`WorkRegistry`) — freeze § 4, § 5

- **Une serrure** (`threading.Lock`) protège admissions, travaux, drapeau d'acceptation et scellement.
- Admissions : `admit()` atomique (refus `None` hors OPEN), `Admission.release()` une seule fois.
- Travaux : `register(admission)` exige une admission **vivante** de CE registre (sinon `SubmissionRefused`, aucun
  fil) ; `REGISTERED → STARTED` (`start()`, première instruction du fil), `STARTED → FINISHED` (`finish()`, `finally`
  du fil), `REGISTERED → ABANDONED` (`abandon()`, `finally` de l'attente HTTP, effectif seulement avant `STARTED`).
  Toute autre transition est refusée et comptée (`violations`), jamais silencieuse.
- Prédicat de fermeture : `seal_if_idle()` évalue sous la serrure `admissions == 0 ET travaux non terminaux == 0`
  avec admissions fermées, puis **scelle** (plus aucune admission ni inscription). Après `close_admissions()`, plus
  aucune admission ne peut naître, donc plus aucune inscription : le prédicat vrai est **stable** — la fermeture ne
  peut pas être suivie d'une soumission.
- `wait_idle()` : attente sans bloquer la boucle, notifiée depuis n'importe quel fil par `call_soon_threadsafe`.

### 3.2 Soumetteur explicite (`RegisteredSubmitter`) — freeze § 5

Même interface que `starlette.concurrency.run_in_threadpool` et même exécuteur (`anyio.to_thread.run_sync`).
Inscription sous la serrure **avant** toute soumission AnyIO ; wrapper du fil : `if not job.start(): raise
JobAbandoned` (le callable n'est **jamais** invoqué) ; `try: callable finally: job.finish()` ; côté requête
`finally: job.abandon()`. L'admission courante est lue dans `CURRENT_ADMISSION` (contextvar posée par l'enveloppe
dans la tâche de la requête).

Injection aux sept sites : `(run_in_threadpool if deps.submit is None else deps.submit)(...)`. Le nouveau paramètre
`submitter` de `build_isolated_personal_app` précède `decryptage`, qui reste le dernier paramètre (contrat figé par
`test_authz_f2_pr9c3_boundary::test_the_router_never_accepts_an_external_orchestrator_or_runner`). Sans injection, le
chemin historique est **inchangé** (tests historiques, `c3._submit` remplaçable avec un seul argument) ; la
composition F04-C refuse toute application dont `app.state.oryx_f2_submitter` n'est pas son soumetteur.

C3 : `_TurnCapacity` / `_TurnSlot` inchangés. Le wrapper prend `STARTED` avant `work()` ; `slot.start()` reste le garde
avant B1 ; un travail abandonné avant démarrage n'appelle jamais `work()` : le slot reste `ACQUIRED` et la requête le
rend une fois par `slot.abandon()` (constaté : place rendue une fois, aucune réservation, aucun fournisseur).

### 3.3 Enveloppe (`F2Envelope`) — freeze § 3, § 4, § 6

- Import : seuls la porte F-02A (pure) et le registre ; aucun secret, connexion, fil, moteur, ni `api`, ni
  composition (prouvé en sous-processus).
- HTTP : `classify` (fonction existante de la porte) ; décision `FORWARD_F2_*` → admission atomique avant tout
  `await` ; hors OPEN → `503 {"error":"temporarily_unavailable"}` (`content-type`, `content-length`, en-têtes
  no-store de la porte, aucun cookie, corps vide pour HEAD), **avant** lecture du corps ; autres décisions → porte
  F-02A (porte fermée `None/None` avant OPEN, identique pour toute décision non F2).
- Démarrage : config F04-B (`load_f2_config(dict(os.environ))`, copie, aucune mutation) → ressources (fabrique
  injectable, défaut `build_f2_resources`) → `run_f2_preflight` → **premier import pouvant charger `core.db`**
  (compositions) → vérifications (égalité 14 / 13 avec `registry.pairs_of`, pas de documentation, pas de CORS,
  soumetteur suivi, lifespan aval inactif) → `RoutePolicyGate` inchangée → OPEN → un `startup.complete`. Tout
  échec : `F2Resources.close()` (ou rapport de nettoyage F04-B), un `startup.failed` (`stage`, type d'erreur, code
  de précontrôle ; aucun secret ni URL), état CLOSED définitif.
- Lifespan aval actif : `router.lifespan_context` autre que `_DefaultLifespan` de Starlette 0.36.3, ou listes
  `on_startup` / `on_shutdown` non vides (FastAPI 0.110.0 y recopie celles des routeurs inclus). Le lifespan d'un
  `APIRouter(lifespan=...)` inclus n'est pas fusionné par FastAPI 0.110.0 (jamais exécuté) : non actif.
- Arrêt : `close_admissions()` (DRAINING) → attente conjointe (`seal_if_idle` / `wait_idle`) → `F2Resources.close()` →
  rapport vérifié → CLOSED → `shutdown.complete` (GRACEFUL). Échéance `drain_timeout` dépassée (défaut `None` :
  aucune valeur inventée) → DRAIN_TIMEOUT, ressources **non** fermées, `shutdown.failed`. Fermeture en échec →
  CLOSE_FAILED, `shutdown.failed`. Une seule portée lifespan possédée.
- IP : `PeerAddressResolver` (pair TCP direct), **provisoire, ne valide pas P1** (F04-D).

### 3.4 Q2 / O1

`core.db` n'est chargé qu'à l'étape composition, **après** validation de la configuration et précontrôle : une URL
invalide ou une base injoignable échoue sans jamais charger `core.db` (Q2-03, Q2-04). Lors d'un démarrage complet, le
moteur legacy est construit (objet inerte) et n'est jamais utilisé (Q2-05, Q2-06).

## 4. Traçabilité exigences → implémentation → tests

| Freeze | Exigence | Implémentation | Tests |
|---|---|---|---|
| § 3.1 | import sans effet, `app` inerte | `entrypoint.py` (imports paresseux) | `test_f04c_entrypoint::test_importing_the_entrypoint_reads_nothing_connects_nothing_and_loads_no_legacy` (sous-processus, avec et sans configuration complète : 0 lecture `ORYX_F2_*`/`DATABASE_URL`, 0 moteur, 0 connexion, 0 fil, ni `api` ni `core.db`) ; `test_the_registry_module_has_no_import_effect` ; `test_no_application_module_imports_the_f04c_entrypoint` |
| § 3.2, § 6.2, § 7 | config avant tout import chargeant `core.db` | ordre de `_startup` | `test_f04c_legacy_engine::test_q2_03_*` (3 URL invalides), `test_q2_04_*`, `test_q2_05_*` (`before_startup.core.db = False`) |
| § 3.3, § 3.7 | porte unique, F-02A octet pour octet | `_http` + `classify` | `test_f02a_matrix_is_byte_identical_and_f2_is_in_maintenance_while_inert`, `test_f02a_matrix_is_byte_identical_in_open_and_draining` (> 300 cas : gabarits × 8 méthodes, chemins non canoniques, `root_path`, `raw_path`), `test_websocket_is_closed_1008_in_every_state` |
| § 3.4, § 3.5 | égalité 14 / 13, C3 obligatoire, même moteur / clés / soumetteur | `compose_f2`, `verify_composition` | `test_a_successful_startup_composes_both_apps_with_one_engine_same_keys_and_one_submitter` ; refus : `test_an_out_of_contract_composition_fails_closed_and_closes_resources_once` (sans C3, route en plus, documentation, CORS, soumetteur non suivi × 2, gestionnaire startup, gestionnaire via routeur inclus, lifespan explicite, `api:app`) |
| § 3.6 | résolveur IP explicite, P1 non validée | `PeerAddressResolver()` | revue ; P1 hors lot |
| § 4 | états, 503 exact hors OPEN, avant lecture du corps | `F2Envelope`, `_maintenance` | `test_maintenance_touches_no_database_and_no_provider`, matrices ci-dessus (`receive` jamais appelé) |
| § 4 | admission avant tout await, libérée une fois même sous CancelledError | `_http` (`finally`) | `test_f04c_registry::test_admissions_*` ; `test_f04c_lifecycle::test_q1_02_*`, `test_q1_04_*` (comptes `admitted == released`) |
| § 4 | DRAINING : anciennes requêtes terminables, nouvelles refusées | `close_admissions`, `register` | `test_q1_09_an_admission_without_thread_blocks_closure_then_submits_during_draining`, `test_submissions_during_draining_are_allowed_only_for_live_admissions` |
| § 5 | protocole REGISTERED/STARTED/FINISHED/ABANDONED, comptes, double terminal | `WorkRegistry`, `RegisteredSubmitter` | `test_f04c_registry` (22 tests : nominal, abandon, hors protocole, courses de 400 travaux × 5 graines, scellement concurrent ×200, notification inter-fils, limiteur saturé, fil actif) |
| § 5 | sept sites, même soumetteur | 7 fichiers | `test_f04c_submission_sites` (AST : exactement 7 sites, tous via le soumetteur injecté) ; `test_q1_01_*` (les 7 sites observés à travers Uvicorn) |
| § 5 | atomicité inter-registres, fermeture impossible avec admission vivante (jobs = 0) | `seal_if_idle` | `test_an_admission_without_thread_prevents_sealing_even_with_zero_jobs`, `test_draining_and_late_registrations_race_*`, `test_q1_09_*`, `test_f02a_matrix_*_open_and_draining` |
| § 5 C3 | deux places, garde avant B1, libération unique | inchangé | `test_c3_keeps_two_places_behind_the_envelope`, `test_q1_03_turns_window_*`, `test_q1_04_turns_*`, `test_q1_05_*` ; suites C3 historiques |
| § 6 | ordre de démarrage, refus fermés, fermeture unique | `_startup`, `_fail` | config incomplète, URL invalide ×4, anti-abus incompatible, tête Alembic, serveur injoignable, fournisseur avorté, composition hors contrat ×10, seconde portée lifespan |
| § 6 | arrêt gracieux / délai dépassé / échec de fermeture | `_shutdown` | `test_q1_10_*` (Uvicorn, fil), `test_f04c_signals` (SIGTERM réel ×2, double SIGINT réel) |
| § 6.6 | lifespan aval : implicite accepté, explicite refusé | `downstream_lifespan_is_active` | `test_downstream_lifespan_detection_accepts_only_the_implicit_default` + refus de composition |
| § 7 | O1 : aucun usage legacy | aucun accès | `test_f04c_legacy_engine` (Q2-01 à Q2-06 en sous-processus + test statique AST) |
| § 8.C | Q1-01 à Q1-10 avec B, négatifs | banc `f04c_support` | `test_f04c_lifecycle` (23 + C3) ; `test_f04c_signals` ; négatifs : soumetteur non suivi, fil brut |

## 5. Invariants du banc (journal indépendant)

`tests/f04c_support.check_invariants` confronte le journal du registre / de l'enveloppe à deux observations
indépendantes : le callable métier réellement invoqué (`RegisteredSubmitter.__call__` enveloppé) et toute exécution
physique d'un fil AnyIO (`anyio.to_thread.run_sync` enveloppé), plus le recensement des fils (`sys._current_frames`)
et `pool.checkedout()` à l'instant de la fermeture du moteur.

- J1 aucun callable hors job `STARTED`, aucune exécution physique sans job ;
- J2 aucun callable après un abandon ;
- J3 `FINISHED` après la fin réelle du callable ;
- J4 aucune fermeture avant `SEALED`, ni avant la dernière libération / fin de job ; aucun fil métier actif ni connexion
  empruntée à la fermeture ; rien n'est admis, inscrit ni invoqué après `SEALED` ;
- J5 une transition terminale par job, une libération par admission, aucune violation signalée ;
- J6 GRACEFUL seulement si J4 tient ; DRAIN_TIMEOUT sans fermeture.

Les tests négatifs prouvent que le banc détecte : un soumetteur qui contourne le registre (J1, J4, J6 violés), un fil
brut (recensement physique à la fermeture).

## 6. Écarts et points soumis à l'arbitrage

**E-1 — Sept tests de dormance existants échouent (BLOQUANT, arbitrage fondateur requis).** Ils encodent « seuls les
tests composent F2 / C3 » et « `core/f2_runtime` ne lit pas l'environnement », ce que le point d'entrée F04-C contredit par
nature (le freeze § 2 propose `core/f2_runtime/entrypoint.py` et § 6.2 exige `load_f2_config(copie_de_os.environ)`).
Aucun emplacement hors `tests/` / `docs/` n'y échappe ; ces fichiers ne figurent pas dans les sept fichiers
modifiables : **ils n'ont pas été modifiés**. Seul `core/f2_runtime/entrypoint.py` est mis en cause.

| Test | Cause | Ajustement nominatif proposé (non appliqué) |
|---|---|---|
| `test_f2_dormancy::test_no_application_module_references_the_f2_package` | `auth_f2` dans `entrypoint.py` | matrice fermée `F04C_REFERENCE_MATRIX = {"core/f2_runtime/entrypoint.py": {"core.auth_f2_http.client_ip": {"PeerAddressResolver"}, "core.auth_f2_http.isolated": {"build_isolated_f2_app"}}}` |
| `test_f2_dormancy::test_f04b_reference_matrix_is_closed_and_exact` | `core/f2_runtime` contient un fichier F2 hors matrice F04-B | `mentioning == F04B ∪ F04C` ; imports F04-C exacts ; mêmes interdits (`F2AuthService`, `HttpAdmission`, anti-abus, cookies) |
| `test_f2_http_dormancy::test_only_tests_compose_the_isolated_application` | `build_isolated_f2_app` nommé hors tests | exception nominative pour le seul `core/f2_runtime/entrypoint.py` |
| `test_authz_f2_dormancy::test_only_tests_and_docs_reference_the_boundary` | marqueurs `authz_f2`, `build_isolated_personal_app` | liste EXACTE de marqueurs pour ce seul fichier (comme `F04B_BOUNDARY_MARKERS`) |
| `test_route_policy_dormancy::test_only_tests_and_docs_reference_the_route_policy` | marqueurs `route_policy`, `RoutePolicyGate`, `F-02A` | liste exacte de marqueurs pour ce seul fichier |
| `test_f04b_database::test_f04b_sources_never_reference_the_legacy_engine_nor_the_environment` | `import os` / `environ` (freeze § 6.2) | `os` / `environ` admis pour ce seul fichier ; `core.db`, `core.models`, `api`, `getenv`, `putenv`, `SessionLocal`, `get_db` toujours interdits |
| `test_authz_f2_pr9c3_boundary::test_no_production_file_mentions_c3` | marqueurs `DecryptageHttpConfig`, `authz_f2_http.decryptage` (freeze § 3.5 : `DecryptageHttpConfig` complet) | liste exacte de marqueurs C3 pour ce seul fichier ; aucune route C3, aucun code d'erreur C3 |

Les garanties de dormance réelles restent prouvées autrement : aucun module applicatif n'importe le point d'entrée
(`test_no_application_module_imports_the_f04c_entrypoint`), `api:app` et les fichiers figés sont inchangés (suites
F04-B / F-02A vertes), l'import du point d'entrée n'a aucun effet.

**E-2 — Preuves écrites en `.log`.** Les chronologies sont écrites dans `reports/f04c/*.log` lorsque `reports/`
existe (CI : artefact `reports-postgres-NN`), sinon dans un répertoire temporaire. Elles citent des routes et des
sites F2 ; les balayages de dormance parcourent `reports/` pour les suffixes `.json`, `.txt`, `.md`… Le suffixe
`.log` évite qu'un journal de TEST soit pris pour un module applicatif ; à valider par l'audit.

**E-3 — PostgreSQL 18 : NON QUALIFIÉ localement** (pas de Docker, dépôt apt PostgreSQL refusé par le proxy de la
session). Toute la qualification locale est sur PostgreSQL 16.15. La CI de la PR exécute la suite complète sur 16 **et**
18 ; seuls ses résultats peuvent qualifier PG18.

**E-4 — Démarrage synchrone sur la boucle.** Config, ressources, précontrôle (une connexion) et composition
s'exécutent dans la tâche lifespan, avant tout service : aucune requête n'est servie pendant ce temps.

**E-5 — `drain_timeout=None` par défaut** (aucune valeur inventée, freeze § 6) : sans échéance F04-C, seule l'échéance
de la plateforme (SIGKILL) borne l'arrêt ; valeur à qualifier en F04-E.

**E-6 — COMMIT incertain observé.** Sous SIGTERM avec délai Uvicorn fini, le client reçoit `500` alors que le fil
suivi commite ensuite (Q1-10) : constat P2 / F04-E, non traité ici.

**E-7 — Hors périmètre, non modifié :** `print` d'identifiants dans `core/decryptage_progress.py` (correctif séparé
requis avant activation) ; P1 (F04-D) ; P2 (F04-E).

## 7. Exécution et preuves

Environnement local : Python 3.11.9, dépendances installées depuis `ci/requirements-ci.txt` avec empreintes
(fastapi 0.110.0, starlette 0.36.3, anyio 4.15.1, uvicorn 0.27.0, sqlalchemy 2.1.3, psycopg2-binary 2.9.13),
PostgreSQL 16.15 jetable en boucle locale.

```bash
ORYX_TEST_DATABASE_URL=postgresql://…@127.0.0.1:55432/oryx_…_test python -m pytest -p no:cacheprovider tests/test_f04c_registry.py tests/test_f04c_entrypoint.py tests/test_f04c_submission_sites.py tests/test_f04c_lifecycle.py tests/test_f04c_legacy_engine.py tests/test_f04c_signals.py
```

Résultats : voir la description de la PR (comptes exacts, SHA, suite complète, écarts). Les chronologies de chaque
scénario sont écrites par `save_evidence` (§ E-2).

## 8. Dormance

- Aucun module applicatif, script, worker, CI ni configuration Railway n'importe ou ne sert le point d'entrée.
- `api.py`, `core/db.py`, `core/models.py`, `core/route_policy/*`, `core/f2_runtime/{config,database,resources,preflight}.py`,
  worker, Alembic, `requirements*` : inchangés.
- Aucune route, aucun contrat HTTP, aucune règle métier ou transactionnelle modifiés ; compositions isolées
  historiques inchangées par défaut (`submitter=None`).
- P1 et P2 restent des **réserves ouvertes**.
