# CI GitHub V1 d'Oryx

Intégration continue du backend Oryx : chaque modification proposée vers
`main` est vérifiée automatiquement avant le merge, sans Railway, sans secret
et sans aucune base distante.

Fichiers : `.github/workflows/ci.yml` (workflow) et `ci/` (scripts appelés par
le workflow, testés par `tests/test_ci_scripts.py`, `tests/test_pg_not_null.py`
et `tests/test_ci_check_migrations_pg.py`).

## 1. Déclencheurs et contrôles

Déclencheurs : `pull_request` vers `main`, `push` sur `main`,
`workflow_dispatch`. Aucun filtre de chemins, aucun `pull_request_target`.

Sur `pull_request`, GitHub teste le commit de fusion de la PR dans `main`
(`refs/pull/<n>/merge`) : `GITHUB_SHA` est ce commit de fusion, dont les deux
parents (main, tête de la PR) sont écrits dans le journal de chaque job.

| Contrôle (affiché) | Job | Contenu | Timeout |
|---|---|---|---|
| `ci / postgres-16` | `postgres` (matrice) | suite Python complète, PostgreSQL 16 | 90 min |
| `ci / postgres-18` | `postgres` (matrice) | suite Python complète, PostgreSQL 18 | 90 min |
| `ci / migrations-16` | `migrations` (matrice) | contrôles Alembic indépendants, PostgreSQL 16 | 20 min |
| `ci / migrations-18` | `migrations` (matrice) | contrôles Alembic indépendants, PostgreSQL 18 | 20 min |
| `ci / web` | `web` | suite Node existante (`tests/web`, `npm test`) | 15 min |
| `ci / required` | `required` | agrégateur : seul contrôle à rendre obligatoire | 10 min |

Les matrices sont en `fail-fast: false` : un échec PostgreSQL 16 n'annule pas
PostgreSQL 18, et inversement.

Le nom réel du check run GitHub est le nom du job (`required`, `postgres-16`,
...) ; l'interface l'affiche précédé du nom du workflow (`ci / required`).
C'est `required` qu'il faut sélectionner dans la protection de branche (§ 9),
après l'avoir constaté dans l'onglet Checks d'une PR.

### `ci / required`

`if: always()` et `needs: [postgres, migrations, web]` : il s'exécute même si
un job a échoué, a été annulé ou sauté. `ci/verify_required.py` refuse :

- tout job de `needs` dont le résultat n'est pas exactement `success`
  (`failure`, `cancelled`, `skipped`, vide, inconnu) ; tout job manquant ou
  inattendu ;
- tout résumé de branche (`postgres-16`, `postgres-18`, `migrations-16`,
  `migrations-18`, `web`) manquant, inattendu, illisible, au statut autre que
  `success`, produit pour un autre commit, avec une autre version de
  PostgreSQL que celle de sa branche, ou avec un test en échec, en erreur ou
  sauté.

Un job de matrice ne remonte qu'un résultat global dans `needs` ; les résumés
par branche garantissent en plus que chaque version a réellement été
exécutée. Aucun résultat rouge, absent ou inconnu ne peut produire un succès.

## 2. Environnements reproductibles

| Élément | Version épinglée | Vérification |
|---|---|---|
| Runner | `ubuntu-24.04` (hébergé par GitHub) | `ImageOS` / `ImageVersion` dans `environment.json` |
| Python | 3.11.9 (celle de `runtime.txt`) | le job échoue si `python --version` diffère |
| Dépendances Python | `ci/requirements-ci.txt` : verrou complet, empreintes sha256, `pip install --require-hashes`, puis `pip check` | liste installée dans `environment.json` |
| Node | 22.23.3 (dernière 22.x LTS au 19/10/2026) | le job échoue si `node --version` diffère |
| Dépendances Node | `tests/web/package-lock.json` via `npm ci --ignore-scripts` | — |
| PostgreSQL 16 | `docker.io/library/postgres@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d` (16.15) | `server_version` exigé égal à `16.15` |
| PostgreSQL 18 | `docker.io/library/postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336` (18.6) | `server_version` exigé égal à `18.6` |

Les digests PostgreSQL sont ceux des index multi-architecture des tags
officiels `postgres:16` (16.15-1.pgdg13+2) et `postgres:18` (18.6-1.pgdg13+2),
relevés le 19/10/2026 et identiques sur deux miroirs indépendants
(`mirror.gcr.io/library/postgres`, `public.ecr.aws/docker/library/postgres`).
Un pull par digest échoue si le contenu servi diffère.

`ci/requirements-ci.txt` est généré depuis `ci/requirements-ci.in`
(`requirements.txt` de l'application, inchangé, + `pytest`, `httpx`, `pyyaml`)
par la commande notée dans son en-tête, avec `--exclude-newer
2026-10-05T00:00:00Z` (aucune publication de moins de deux semaines au moment
du verrou). Versions clés : FastAPI 0.110.0, Starlette 0.36.3, httpx 0.28.1
(requis par `fastapi.testclient`), pytest 9.1.1, SQLAlchemy 2.1.3, Alembic
1.20.0, psycopg2-binary 2.9.13.

Actions tierces, épinglées par SHA complet (tags vérifiés par `git ls-remote`
sur les dépôts `actions/*`) :

| Action | Tag | SHA |
|---|---|---|
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/setup-python` | v7.0.0 | `5fda3b95a4ea91299a34e894583c3862153e4b97` |
| `actions/setup-node` | v7.0.0 | `820762786026740c76f36085b0efc47a31fe5020` |
| `actions/upload-artifact` | v7.0.1 | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |
| `actions/download-artifact` | v8.0.1 | `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` |

Aucun cache (pip, npm, actions/cache) : chaque exécution part d'un état neuf.

## 3. Bases PostgreSQL jetables

Chaque job PostgreSQL démarre son propre serveur (`ci/start_postgres.sh`) sur
son propre runner : aucune base n'est partagée entre jobs, et à l'intérieur
d'un job les tests destructeurs s'exécutent l'un après l'autre (aucune
parallélisation).

- Conteneur en **réseau hôte** (`--network host`), `listen_addresses =
  127.0.0.1`, port 55432. Mesuré : avec un port publié en réseau bridge (le
  mode des « service containers » GitHub), `inet_server_addr()` vaut
  l'adresse du conteneur (`172.17.0.2`) et le garde-fou serveur de la PR #220
  (`tests/pg_guard.py`, adresse serveur de boucle locale exigée) refuse la
  base. En réseau hôte, `inet_server_addr()` vaut `127.0.0.1`.
- Nom `^oryx_[a-z0-9_]*_test$` (`oryx_ci_postgres_16_test`, ...), commentaire
  sentinelle `oryx-disposable-test-db` posé à la création.
- Mot de passe aléatoire propre au job (masqué dans les journaux), aucun
  secret GitHub ; `fsync`, `synchronous_commit` et `full_page_writes`
  désactivés (base jetable).
- `ci/verify_test_database.py` vérifie avant toute suite, dans la connexion :
  `current_database()`, `inet_server_addr()` non NULL et de boucle locale,
  commentaire sentinelle, `pg_is_in_recovery() = false`, `listen_addresses`,
  version exacte épinglée.
- pytest et les contrôles Alembic sont lancés sous `env -i` avec une liste
  blanche (`ci/clean_env.sh`) : `PATH`, `HOME`, locale, `TZ=UTC`,
  `LD_LIBRARY_PATH` (interpréteur de setup-python) et
  `ORYX_TEST_DATABASE_URL`. Aucune variable `RAILWAY_*`, `DATABASE_*` ou
  `PG*`, aucun jeton n'atteint les tests. `DATABASE_URL` n'est injectée que
  dans les sous-processus Alembic (par les tests eux-mêmes ou par
  `ci/check_migrations.py`), avec la même URL jetable.

## 4. Suites Python (`ci / postgres-16`, `ci / postgres-18`)

`ci/run_pytest.sh` : `python -m pytest -p no:cacheprovider -rfEsxX
--junitxml=...`, sans `-k`, `-m`, `--deselect`, `xdist` ni
`continue-on-error`. Historique git complet (`fetch-depth: 0`) : certains
tests comparent l'état actuel à d'anciens commits de `main`.

`ci/check_test_report.py` n'accepte le résultat que si : code de sortie 0,
au moins un test, aucun échec, erreur ou test sauté (un `xfail` est compté
comme sauté), ligne de résumé ne contenant que `passed` / `warnings` (ni
`deselected`, ni `xfailed`, ni `xpassed`), totaux JUnit cohérents.

## 5. Contrôles Alembic (`ci / migrations-16`, `ci / migrations-18`)

`ci/check_migrations.py`, indépendant des tests :

1. une seule base (`0001_current_oryx_baseline`), une seule tête, chaîne
   linéaire, aucune révision manquante, aucun fichier hors chaîne ;
2. identifiants `NNNN_[a-z0-9_]+`, au plus 32 caractères
   (`alembic_version.version_num` est un `VARCHAR(32)`), préfixes 0001..N
   contigus ; un fichier par révision portant son préfixe numérique (le
   fichier `0013_decryptage_conversation_affinity.py` porte la révision
   raccourcie `0013_decryptage_conv_affinity`) ;
3. migrations historiques figées : `ci/migration_history.sha256` (0001 à
   0014 du `main` de référence `108e65f`), vérifié aussi par `sha256sum
   --check --strict` ;
4. SQL offline : `upgrade head --sql` (versions écrites dans l'ordre) et
   `downgrade head:0001 --sql` ;
5. upgrade d'une base vide jusqu'à `head`, révision finale lue dans
   `alembic_version` et par `alembic current` ;
6. invariants du schéma de `head` : contraintes NOT NULL de PostgreSQL 18
   (`contype = 'n'`) en bijection avec les colonnes `attnotnull`, aucune
   avant 18 ; aucune contrainte non validée ou différable ; FK `NO ACTION`
   / `NO ACTION` ; aucun ENUM, aucune fonction ni trigger, aucune extension
   autre que `plpgsql` (conventions de toutes les migrations,
   `alembic/README`) ;
7. modèles SQLAlchemy et schéma de `head` identiques : `compare_metadata`
   (types et server defaults compris) doit être **vide**. Mesuré sur `main`
   (PG 16 et 18) : aucun écart, donc aucune exception n'est déclarée. Un écart
   légitime futur exigerait une décision explicite et une exception nommée,
   testée ;
8. SQL offline appliqué à une seconde base jetable : schéma identique à
   l'upgrade en ligne ;
9. montée révision par révision depuis une base vide, avec des données
   historiques insérées en 0001 (users, portfolio_positions,
   investment_theses, analysis_facts, user_statements ; company_analyses
   reste vide, comme 0004 l'exige) : données conservées à chaque étape,
   schéma final identique à la montée directe ;
10. descente révision par révision jusqu'à 0001, **jamais en dessous** :
    chaque downgrade restaure exactement le schéma de la révision
    précédente, données conservées ; puis remontée finale jusqu'à `head`.

Ces contrôles couvrent automatiquement toute nouvelle migration : après
intégration de la PR #220, la montée 0014 → 0015 → 0016 → 0017 avec données
et les downgrades 0017 → 0014 seront vérifiés sans modification de la CI.

## 6. Suite Node (`ci / web`)

Node 22.23.3, `npm ci --ignore-scripts` dans `tests/web`, puis la commande du
dépôt `npm test` (`node --test`), à laquelle on ajoute seulement deux
reporters (`spec` vers le journal, `junit` vers le rapport). Le rapport passe
par `ci/check_test_report.py --kind node`.

## 7. Sécurité

`permissions: contents: read` au niveau du workflow, aucune permission par
job, aucun `id-token` (OIDC), aucun secret, aucun `pull_request_target`,
runners GitHub hébergés uniquement, `persist-credentials: false` sur chaque
checkout, aucune commande Railway, aucun déploiement. Les seules expressions
interpolées dans des commandes shell sont des valeurs de matrice définies
dans le workflow ; `toJSON(needs)` passe par une variable d'environnement.

## 8. Rapports

Chaque job publie, même en échec (`if: always()`), conservés 14 jours :

- `reports-<branche>` : JUnit, journal pytest / Node / Alembic,
  `environment.json` (commit, Python, distributions installées, runner),
  `postgres.json` (version et adresse réelles du serveur), journal du serveur
  PostgreSQL ;
- `result-<branche>` : résumé JSON relu par `ci / required`.

Aucun secret n'y figure (le mot de passe PostgreSQL est aléatoire, propre au
job, et le serveur disparaît avec le runner).

## 9. Protection de `main` (procédure manuelle du propriétaire)

À faire par le propriétaire du dépôt, **après** le merge de la PR CI et une
exécution verte sur `main` (le check `required` doit avoir déjà été vu par
GitHub pour être sélectionnable).

1. Vérifier le nom réel du contrôle : onglet *Checks* d'une PR ou du dernier
   commit de `main` → workflow `ci` → job **`required`** (affiché
   `ci / required`).
2. *Settings → Rules → Rulesets → New ruleset → New branch ruleset* :
   - *Ruleset name* : `main` ; *Enforcement status* : **Active** ;
   - *Bypass list* : **vide** (aucun rôle, aucune app) ;
   - *Target branches* : *Add target → Include default branch* (`main`) ;
   - cocher **Restrict deletions** ;
   - cocher **Block force pushes** ;
   - cocher **Require a pull request before merging** :
     - *Required approvals* : voir l'encadré ci-dessous ;
     - cocher **Require conversation resolution before merging** ;
   - cocher **Require status checks to pass** :
     - cocher **Require branches to be up to date before merging** ;
     - *Add checks* → rechercher `required` → choisir celui dont la source est
       **GitHub Actions** ;
   - *Create*.
3. *Settings → General → Pull Requests* : **décocher** *Allow auto-merge*.
4. Contrôle : ouvrir une PR de test qui casse un test → `ci / required`
   rouge, bouton de merge bloqué ; aucune option « bypass » proposée.

**Approbation indépendante.** Le dépôt n'a qu'un collaborateur (le
propriétaire, admin). GitHub interdit d'approuver sa propre PR : exiger une
approbation (`Required approvals ≥ 1`) bloquerait tout merge tant qu'un
second compte disposant d'un accès en écriture n'existe pas. L'audit
indépendant (ChatGPT) reste donc une étape de processus, non imposée par
GitHub. Pour la rendre obligatoire, il faut d'abord inviter un second compte
humain habilité, puis passer *Required approvals* à 1 (et cocher *Dismiss
stale pull request approvals when new commits are pushed*).

**Limite.** Le propriétaire, admin du dépôt, peut toujours modifier ou
désactiver le ruleset : la liste de contournement vide empêche le
contournement ordinaire, pas un changement de configuration.

## 10. Risque de redéploiement Railway au merge

Merger une PR sur `main` (y compris la PR qui introduit cette CI) peut
déclencher automatiquement un redéploiement des services Railway
`oryx-api` et `oryx-evaluation-worker`, qui se déploient depuis `main` (sans
attendre les checks GitHub, constaté en lecture seule le 16/10/2026 dans la
PR #220). Le pre-deploy d'`oryx-api` exécute `alembic upgrade head`.

Avant le merge de la PR CI, le propriétaire confirme que ses changements ne
déclenchent aucune évolution du schéma ni de l'application :

- aucun fichier sous `alembic/` modifié ; `alembic heads` =
  `0014_evaluation_run_leases` (inchangé) → `upgrade head` est un NO-OP sur
  une production en 0014 ;
- aucun fichier applicatif modifié (`api.py`, `core/`, `scripts/`,
  `web/`, `web-v2/`, `requirements.txt`, `runtime.txt`, `mise.toml`) : la
  PR ne touche que `.github/`, `ci/`, `docs/ci.md` et des fichiers `tests/`.

Checklist de validation après le merge (propriétaire, Railway) :

1. GitHub : la CI du commit de merge sur `main` est verte (`ci / required`).
2. Railway, `oryx-api` : un déploiement du commit de merge a démarré ; logs
   du pre-deploy : aucune ligne `Running upgrade` (révision déjà à `head`),
   sortie 0, durée bien en deçà du timeout de 300 s.
3. Railway, `oryx-api` : déploiement `SUCCESS`, healthcheck vert, logs de
   démarrage sans erreur.
4. Railway, `oryx-evaluation-worker` : déploiement `SUCCESS`, logs sans
   erreur.
5. Révision de production toujours `0014_evaluation_run_leases` (lecture
   seule, par exemple via le préflight de la PR #220 ou
   `alembic current` depuis le conteneur).
6. Parcours applicatif de fumée (connexion, portefeuille, Décrypter) sans
   régression.
7. En cas d'anomalie : redéployer le déploiement précédent depuis Railway
   (rollback applicatif ; aucune migration n'a été exécutée).

## 11. Reprise de la PR #220 après intégration de la CI

La PR #220 (`r1-pr1-postgres-foundations`) a été écrite sur `108e65f`. Après
le merge de la CI :

1. Mettre à jour sa branche avec `main` (merge de `main` dans la branche,
   sans réécriture d'historique). Conflits attendus, tous dans `tests/` :
   la PR #220 modifie aussi les tests de structure de 0002, 0004 à 0012.
   Garder **les deux** changements : ses nouvelles attentes (tête 0017,
   `users.level_revision`, comparateur PR 1) et l'appel à
   `without_verified_not_null(...)` de la CI autour de chaque comparaison
   exacte de `pg_constraint` (avec, pour `users`, la colonne NOT NULL
   `level_revision` dans les attentes).
2. Ses garde-fous (`tests/conftest.py`, `tests/pg_guard.py`) sont
   compatibles avec la CI : aucune variable `RAILWAY_*` / `DATABASE_*` /
   `PG*` dans l'environnement de pytest, URL `127.0.0.1`, nom
   `^oryx_[a-z0-9_]*_test$`, commentaire sentinelle, `inet_server_addr()` =
   `127.0.0.1`.
3. `ci/migration_history.sha256` reste inchangé (0001 à 0014) ; les
   migrations 0015 à 0017 sont nouvelles et passent par tous les contrôles
   Alembic (montée 0014 → 0017 avec données, downgrades exacts, SQL offline,
   invariants, `compare_metadata` vide à `head`).
4. Toute la CI doit être verte sur la PR mise à jour avant son audit puis son
   merge, qui reste soumis à sa propre checklist de préflight production.
   Une fois 0015 à 0017 en production, le manifeste figé pourra être étendu à
   ces révisions par une PR dédiée.

## 12. Reproduire localement

```bash
# PostgreSQL jetable (Docker, réseau hôte) : écrit l'URL sur la sortie standard
bash ci/start_postgres.sh 18 oryx_ci_postgres_18_test
export ORYX_TEST_DATABASE_URL=...  ORYX_CI_POSTGRES_EXPECTED_VERSION=18.6
python ci/describe_environment.py --out reports/environment.json
bash ci/run_pytest.sh postgres-18 "$(command -v python)" reports

# Contrôles Alembic (deux bases jetables sur le même serveur)
bash ci/start_postgres.sh 16 oryx_ci_migrations_16_test oryx_ci_offline_16_test
bash ci/run_migration_checks.sh migrations-16 "$(command -v python)" reports oryx_ci_offline_16_test
```
