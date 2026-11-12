# ACT-L1 / F04-B V0.3 — Configuration stricte, dépendances et ressources F2 (implémentation dormante)

## 1. Référentiel et statut

| Élément | Valeur |
|---|---|
| Chantier | Activation F2 Lot 1 (ACT-L1), lot **F04-B** |
| Version | **V0.3** |
| Statut | **Freeze limité validé par le fondateur** : implémentation **dormante** uniquement |
| Contrat amont | `docs/act_l1_f04a_architecture_v03.md` (F04-A V0.3, § 8 et § 13 « F04-B ») |
| Référence auditée lors du freeze | `5ccc9e2bc122a44f1a2bfd810b28257bb6e31b31` |
| `main` relu avant implémentation | `5ccc9e2bc122a44f1a2bfd810b28257bb6e31b31` — **identique** : aucun conflit avec le freeze |
| Activation F2 | **Aucune** |

Ce lot **n'autorise pas** : l'activation de F2, le changement du point d'entrée ASGI public, la
modification des variables ou secrets Railway, un déploiement manuel, le merge de la PR, la modification
des contrats métier, des routes ou des règles transactionnelles. Workflow : architecture figée →
implémentation → PR → audit indépendant → merge par le fondateur → validation Railway.

## 2. Contenu du lot

| Bloc | Fichier | Objet |
|---|---|---|
| B01 | `core/f2_runtime/config.py` | chargeur **pur** de la configuration stricte F2 |
| B02 | `requirements.in`, `requirements.txt` | dépendances directes épinglées + verrou complet avec empreintes |
| B03 / B04 | `core/f2_runtime/database.py` | fabrique explicite du moteur SQLAlchemy synchrone F2, pool borné |
| B05 | `core/f2_runtime/resources.py` | gestionnaire de ressources à propriété explicite |
| B05 | `core/auth_f2/http_providers.py`, `core/decryptage_providers.py` | extensions **techniques** (fermeture, sessions par appel) |
| B06 | `core/f2_runtime/preflight.py` | précontrôles PostgreSQL en **lecture seule** |

Traçabilité exigence → implémentation → tests : `docs/act_l1_f04b_traceability_v03.md` (reconstruction, pas
le texte du freeze).

Aucun de ces modules n'est importé par `api.py`, un worker, un script, la CI ou un point d'entrée : ils
ne sont exercés que par les tests (`tests/test_f04b_*.py`).

## 3. B01 — Configuration stricte

`load_f2_config(variables)` lit **exclusivement** le mappage reçu (F04-C lui transmettra `os.environ`) :
aucune lecture implicite de l'environnement, aucun effet à l'import, aucun réseau, aucune base, aucun
secret généré, aucune valeur par défaut, aucun repli legacy. Résultat déterministe.

### 3.1 Variables contractuelles (toutes requises)

| Variable | Contrôle | Champ de `F2Config` |
|---|---|---|
| `DATABASE_URL` | `postgres://`, `postgresql://` ou `postgresql+psycopg2://` ; hôte, utilisateur, base | `database_url` (`sqlalchemy.engine.URL`, mot de passe masqué) |
| `ORYX_F2_ALLOWED_ORIGIN` | origine https canonique **unique** (`validate_allowed_origins`) | `allowed_origin`, `allowed_origins` |
| `ORYX_F2_AUTH_KEYS_JSON` | `oryx-f2-auth-keys-v1` → `Keyring` | `keyring` |
| `ORYX_F2_CSRF_KEYS_JSON` | `oryx-f2-csrf-keys-v1` → `CsrfKeyring` | `csrf_keyring` |
| `ORYX_F2_PORTFOLIO_KEYS_JSON` | `oryx-f2-portfolio-keys-v1` → `PortfolioKeyring` | `portfolio_keyring` |
| `ORYX_F2_DECRYPTAGE_KEYS_JSON` | `oryx-f2-decryptage-keys-v1` → `DecryptageKeyring` | `decryptage_keyring` |
| `ORYX_F2_SUPABASE_URL` | `https://<hôte>[:port]` sans chemin ; issuer valide | `supabase_url` |
| `ORYX_F2_SUPABASE_SERVICE_KEY` | jeton ASCII imprimable, sans blanc, ≤ 4096 | `supabase_service_key` (`Redacted`) |
| `ORYX_F2_SUPABASE_VERIFY_KEY` | idem | `supabase_verify_key` (`Redacted`) |
| `ORYX_F2_RESEND_API_KEY` | idem | `resend_api_key` (`Redacted`) |
| `ORYX_F2_RESEND_SENDER` | imprimable, contient `@`, ≤ 320 | `resend_sender` |
| `ORYX_F2_EODHD_API_KEY` | jeton | `eodhd_api_key` (`Redacted`) |
| `ORYX_F2_FMP_API_KEY` | jeton | `fmp_api_key` (`Redacted`) |
| `ORYX_F2_ANTHROPIC_API_KEY` | jeton | `anthropic_api_key` (`Redacted`) |
| `ORYX_F2_DECRYPTAGE_MODEL` | règle exacte de `valid_model` (C3), ≤ 128 | `decryptage_model` |

Règles communes : valeur vide, blanc de bord ou caractère de contrôle → refus (aucun rognage silencieux).
**Validation fermée de l'espace F2 seulement** : toute variable `ORYX_F2_*` hors liste est refusée
(`unknown_variable`) ; toutes les autres variables (Railway, legacy `ANTHROPIC_API_KEY`, …) sont ignorées
sans être lues.

### 3.2 Documents JSON des trousseaux

```json
{"schema": "oryx-f2-auth-keys-v1",
 "purposes": {"candidate":  {"current": "v1", "keys": {"v1": "<43 car.>"}},
              "derive":     {"current": "v1", "keys": {"v1": "<43 car.>"}},
              "active":     {"current": "v1", "keys": {"v1": "<43 car.>"}},
              "otp":        {"current": "v1", "keys": {"v1": "<43 car.>"}},
              "invitation": {"current": "v1", "keys": {"v1": "<43 car.>"}},
              "abuse":      {"current": "v1", "keys": {"v1": "<43 car.>"}}}}

{"schema": "oryx-f2-csrf-keys-v1",       "current": "c1", "keys": {"c1": "<43 car.>"}}
{"schema": "oryx-f2-portfolio-keys-v1",  "current": "p1", "keys": {"p1": "<43 car.>"}}
{"schema": "oryx-f2-decryptage-keys-v1", "current": "d1", "keys": {"d1": "<43 car.>"}}
```

- JSON strict : propriétés dupliquées (à tout niveau), `NaN` / `Infinity`, BOM, texte résiduel refusés ;
  64 Kio au plus ; blancs JSON (espaces, retours à la ligne) admis à l'intérieur.
- Propriétés **exactes** : aucun champ inconnu, aucun champ manquant, schéma versionné exact, six usages
  exacts pour le trousseau F2.
- Clé : base64url **canonique sans remplissage**, exactement **32 octets** (43 caractères ; réencodage
  identique), via `core.auth_f2.crypto.b64url_decode_secret`.
- Versions : au moins une ; la version courante doit figurer dans `keys` ; les **versions historiques**
  (anciennes clés conservées pour la vérification et la rotation) sont admises. La **syntaxe** des versions
  est celle de chaque classe existante, réutilisée telle quelle : `Keyring` `[A-Za-z0-9._-]{1,64}`,
  `CsrfKeyring` `[A-Za-z0-9_-]{1,32}`, `PortfolioKeyring` et `DecryptageKeyring`
  `[a-z0-9][a-z0-9_-]{0,31}`.
- **Aucune réutilisation** d'une valeur de clé : ni entre versions ou usages d'un trousseau, ni entre les
  quatre familles (F2, CSRF, portefeuille, Décrypter).

### 3.3 Erreurs

`F2ConfigError.problems` : tuple de `(variable, code, usage|None)` — **tous** les problèmes détectés, codes
stables (`missing`, `empty`, `surrounding_whitespace`, `invalid_characters`, `too_long`, `unknown_variable`,
`invalid_database_url`, `invalid_origin`, `invalid_url`, `invalid_sender`, `invalid_model`, `invalid_json`,
`duplicate_property`, `not_an_object`, `unknown_property`, `missing_property`, `unknown_schema`,
`no_key_version`, `invalid_version_field`, `current_version_absent`, `invalid_key_encoding`, `key_reused`,
`keyring_rejected`). Aucune valeur, aucun fragment de valeur, aucun message de décodeur n'y figure ;
rien n'est journalisé. `F2Config` : `repr()` = `F2Config(<private>)`, non sérialisable.

Le **même** objet `Keyring` et le **même** objet `CsrfKeyring` sont acceptés par les deux compositions
isolées (`test_both_isolated_compositions_accept_the_same_keyrings_and_engine`).

## 4. B02 — Dépendances de production

### 4.1 Versions retenues

Python de référence **3.11.9** (`runtime.txt`, inchangé). Dépendances **directes** (`requirements.in`) :

| Paquet | Version | Justification |
|---|---|---|
| fastapi | 0.110.0 | inchangé (déjà épinglé) |
| starlette | 0.36.3 | désormais explicite (lifespan par défaut constaté par F04-A § 9.2) |
| uvicorn | 0.27.0 | inchangé (déjà épinglé) |
| pydantic | 2.10.0 | inchangé (déjà épinglé) |
| requests | 2.34.2 | EODHD / FMP ; était non épinglé |
| python-dotenv | 1.2.4 | legacy ; était non épinglé |
| yfinance | 1.7.0 | legacy ; était non épinglé |
| anthropic | 1.11.0 | `ClaudeTransport` ; était `>=0.25.0` |
| httpx | 0.28.1 | **nouveau en production** : Supabase / Resend (`core/auth_f2/http_providers.py`) |
| httpx2 | 2.13.1 | **explicite** : importé directement par `core/decryptage_providers.py` (n'arrivait que via anthropic) |
| sqlalchemy | 2.1.3 | était non épinglé |
| psycopg2-binary | 2.9.13 | était non épinglé |
| alembic | 1.20.0 | était `>=1.13` |

Toutes ces versions sont **exactement** celles déjà qualifiées par la CI (`ci/requirements-ci.txt`).
Aucun alias global `httpx` / `httpx2` : chaque module importe son paquet sous son nom
(`test_no_global_httpx_alias_anywhere`).

### 4.2 Verrou et reproduction

`requirements.txt` (point d'entrée Railway, nom inchangé) est désormais le **verrou complet** de
`requirements.in` : 47 distributions, toutes épinglées, toutes avec empreintes sha256 (toutes les
distributions publiées de chaque version). Sa présence d'empreintes fait passer `pip install -r
requirements.txt` en mode *hash-checking* sans option supplémentaire. Régénération (mêmes paramètres que
le verrou CI, aucune publication de moins de deux semaines au moment du verrou) :

```bash
uv pip compile requirements.in --python-version 3.11 --python-platform x86_64-manylinux_2_28 --exclude-newer 2026-10-05T00:00:00Z --generate-hashes -o requirements.txt
```

Cohérence vérifiée par `tests/test_f04b_dependencies.py` : fermeture exacte des 47 versions ; chaque
distribution de production présente dans `ci/requirements-ci.txt` avec **la même version et les mêmes
empreintes** ; le verrou CI n'ajoute que `pytest`, `iniconfig`, `packaging`, `pluggy`, `pygments`,
`pyyaml`. `ci/requirements-ci.txt` et `ci/requirements-ci.in` sont **inchangés** (régénérer le verrou CI
ne modifierait que des commentaires `# via`, constaté localement).

### 4.3 Preuve d'installation propre (exécutée localement, Python 3.11.9)

```bash
python3.11 -m venv /tmp/venv-prod
/tmp/venv-prod/bin/python -m pip install --no-cache-dir -r requirements.txt
/tmp/venv-prod/bin/python -m pip check
```

Résultats constatés : installation réussie (pip 24.0, 47 paquets) ; `pip check` : *No broken
requirements found* ; `pip freeze` identique au verrou ; un verrou dont les deux empreintes d'`alembic`
sont altérées est **refusé** (`THESE PACKAGES DO NOT MATCH THE HASHES`). Depuis cette seule installation
(sans pytest) : import de `api`, des adaptateurs, des compositions isolées, de la porte F-02A, du worker
et des quatre modules F04-B ; construction des ressources, des deux compositions et de la porte avec une
configuration synthétique (non servies) ; `uvicorn api:app` démarre et `GET /health` répond
`200 {"status":"ok"}`. Les suites de tests s'exécutent ensuite dans l'environnement CI
(`ci/requirements-ci.txt` = verrou de production + outils de test, versions et empreintes identiques).

**Preuve rejouée et journalisée en CI (réserve d'audit 2)** :
`tests/test_f04b_production_install.py::test_a_clean_environment_installs_and_runs_from_requirements_txt_alone`
crée un environnement **vierge** (`python -m venv --clear`), y exécute **uniquement**
`pip install --no-cache-dir -r requirements.txt` (jamais `ci/requirements-ci.txt`), puis `pip check` et
`pip freeze --all` (fermeture exacte des 47 versions exigée), puis exécute `tests/f04b_production_probe.py`
avec l'interpréteur de cet environnement en mode isolé (`python -I`) : aucun outil de développement
importable (`pytest`, `yaml`…), imports de production (legacy, worker, Auth F2, Décrypter, F04-B, porte
F-02A), construction sans réseau (sockets interdits) des composants dormants. Chaque commande, son code de
retour et ses sorties sont écrits dans `reports/f04b-production-install/install.log` et `summary.json`, publiés
avec l'artefact `reports-postgres-16` / `reports-postgres-18` des jobs CI. Aucun workflow n'est modifié : la
CI exécute déjà toute la suite et publie `reports/`, et refuse tout test sauté (le test ne peut donc pas être
contourné en CI).

## 5. B03 / B04 — Moteur PostgreSQL F2 et pool

- `normalize_f2_database_url(raw)` : formats PostgreSQL existants (`postgres://`, `postgresql://`,
  `postgresql+psycopg2://`) normalisés vers `postgresql+psycopg2` — mêmes résultats que
  `core.db.normalize_database_url` sur ces formats — ; tout autre schéma ou pilote **refusé** (fermé, alors
  que le legacy laisse passer). Messages sans URL ni mot de passe.
- `create_f2_engine(url)` : **aucun moteur à l'import**, aucune connexion à la construction ; n'importe ni
  `core.db` ni `core.models` (vérifié en sous-processus : `core.db` absent de `sys.modules`, zéro instance
  `Engine` après import, même avec `DATABASE_URL` défini).
- Paramètres **candidats figés** (non validés pour l'exploitation Railway) : `pool_size=5`,
  `max_overflow=2`, `pool_timeout=5`, `pool_pre_ping=True`, `pool_recycle=1800`,
  `isolation_level="READ COMMITTED"` explicite ; `QueuePool` ; `hide_parameters=True`.
- Aucun retry transactionnel : saturation → `sqlalchemy.exc.TimeoutError` après ≤ 5 s ; serveur
  injoignable → une seule tentative.
- Un seul moteur, construit et **possédé** par le gestionnaire de ressources, fermé par `dispose()`.

## 6. B05 — Ressources à propriété explicite

### 6.1 Inventaire

| Ordre | Ressource | Construite par | Propriétaire | Fermeture |
|---|---|---|---|---|
| 1 | moteur PostgreSQL F2 (unique, partagé Auth + Personnel) | `create_f2_engine` | gestionnaire | `engine.dispose()` |
| 2 | `HttpSupabaseOtpProvider` (client `httpx` interne) | gestionnaire | gestionnaire | `close()` |
| 3 | `HttpResendMailer` (client `httpx` interne) | gestionnaire | gestionnaire | `close()` |
| 4 | `EodhdTransport`, mode `session_factory` | gestionnaire | gestionnaire | aucune ressource durable : une session par appel, fermée à l'issue de l'appel |
| 5 | `FmpTransport`, mode `session_factory` | gestionnaire | gestionnaire | idem |
| 6 | `ClaudeTransport` (client SDK `anthropic`, `max_retries=0`) | gestionnaire | gestionnaire | `close()` |

`build_f2_resources(config)` enregistre chaque ressource **effectivement construite** ; en cas d'échec à
une étape, les ressources déjà construites sont fermées en ordre inverse, puis
`F2ResourceBuildError(stage, cause_type, cleanup)` est levée (aucun message d'origine). `close()` ferme
chaque ressource possédée **une seule fois**, en ordre inverse (claude → fmp → eodhd → resend → supabase →
engine), poursuit malgré l'échec d'une fermeture (rapporté dans `CloseReport.failed`) et est
**idempotent** (même compte rendu, aucune seconde fermeture, y compris sous appels concurrents).

Ressources **injectées** (jamais fermées par F04-B) : un `engine=` fourni est enregistré comme
*emprunté* ; un transport `httpx` fourni à Supabase / Resend et un `http_client` fourni à Claude restent à
leur propriétaire (les `close()` des adaptateurs ne les ferment pas). Aucune fermeture n'utilise un
attribut privé d'adaptateur (vérifié par analyse AST du module).

### 6.2 Extensions techniques des adaptateurs

- `core/auth_f2/http_providers.py` : `close()` sur `HttpSupabaseOtpProvider` et `HttpResendMailer` (ferme
  le client possédé, idempotent ; ne ferme pas un transport injecté). Méthodes, en-têtes, corps, TLS
  vérifié, `trust_env=False`, `follow_redirects=False`, délai 10 s et classification des erreurs
  inchangés.
- `core/decryptage_providers.py` :
  - `EodhdTransport` / `FmpTransport` : nouveau paramètre `session_factory=` ; `session=` et
    `session_factory=` ensemble → `ValueError` ; mode historique (`session=` ou session interne)
    **inchangé** ; en mode fabrique, chaque appel obtient sa propre session et la ferme (`finally`), quelle
    que soit l'issue ; aucune session mutable partagée entre appels concurrents.
  - `isolated_session()` : session neuve, mêmes adaptateurs `HTTPAdapter(max_retries=0)` que la session
    historique, `trust_env=False` (aucun proxy, `.netrc` ni magasin de certificats lu dans
    l'environnement), TLS vérifié.
  - `ClaudeTransport.close()` : ferme le client SDK possédé ; un `http_client` injecté n'est pas fermé.
    `create_message`, `max_retries=0`, délais et classification inchangés.
  - Méthodes B2, retries, redirections, timeouts et classification des erreurs **inchangés**.

### 6.3 Limite

**F04-B ne décide jamais qu'une ressource est sûre à fermer.** Appeler `close()` alors qu'un travail suivi
(requête d'authentification, fil C3 `/turns`, `/replay`, `/ack`) peut encore l'utiliser n'est **pas** une
fermeture gracieuse (F04-A § 8.6, § 10.3) : garantir la fin effective des travaux avant `close()` relève de
F04-C (suivi des travaux engagés) et F04-E (drainage, délai dépassé, arrêt forcé).

## 7. B06 — Précontrôles PostgreSQL en lecture seule

`run_f2_preflight(engine, keyring)` — opération explicite, une transaction ouverte par
`SET TRANSACTION READ ONLY` (première instruction), `SET LOCAL lock_timeout` / `statement_timeout` (valeurs
F2), puis **toujours** `ROLLBACK` :

1. serveur attendu : `current_database()` et `current_user` égaux à l'URL ; majeure PostgreSQL ∈ {16, 18}
   (versions qualifiées par la CI) ;
2. isolation effective `read committed` ; 3. `transaction_read_only = on` ;
4. `alembic_version` : une seule ligne, égale à l'unique tête du code (`0021_decryptage_reservations`,
   lue par `ScriptDirectory` sans exécuter `alembic/env.py`) ;
5. tables et colonnes F2 nécessaires (`F2_REQUIRED_TABLES`, `F2_REQUIRED_COLUMNS`), résolues comme les
   requêtes F2 non qualifiées (`to_regclass`) ;
6. `load_key_state` : état anti-abus présent et compatible avec le trousseau.

Interdits, vérifiés par test : aucun `alembic upgrade` / `stamp`, aucune `initialize_abuse_key_state`,
aucune migration, aucune écriture, aucune rotation, aucune réparation. Toute écriture tentée dans la
transaction du précontrôle est refusée par PostgreSQL. Tout état absent ou incompatible :
`F2PreflightError(code)` — `connection_failed`, `unexpected_server`, `unsupported_server_version`,
`isolation_not_read_committed`, `transaction_not_read_only`, `alembic_version_missing`,
`alembic_head_mismatch`, `alembic_head_ambiguous`, `f2_objects_missing`, `abuse_key_state_unavailable`,
`database_error`.

## 8. Tests

| Fichier | Couverture |
|---|---|
| `tests/test_f04b_config.py` | configuration absente, partielle, invalide ; JSON malformé et dupliqué ; propriétés exactes ; encodage canonique ; versions historiques et syntaxes par classe ; collisions intra- et inter-familles ; erreurs sans secret ; variables étrangères ignorées |
| `tests/test_f04b_database.py` | normalisation, paramètres figés, import sans effet ni moteur ni `core.db` ; **PostgreSQL** : plafond 7, saturation et attente bornée ≤ 5 s, concurrence, restitution, isolation READ COMMITTED effective malgré un défaut serveur SERIALIZABLE, paramètres masqués, aucun retry |
| `tests/test_f04b_resources.py` | ordre, propriété, fermeture unique et idempotente, nettoyage partiel à chaque étape, ressources injectées jamais fermées, contrats des cinq fournisseurs, sessions isolées sous concurrence, combinaison ambiguë refusée, mêmes trousseaux et moteur acceptés par les deux compositions |
| `tests/test_f04b_preflight.py` | **PostgreSQL** : nominal, instructions en lecture seule, aucune écriture, écriture refusée, état anti-abus absent ou incompatible, tête Alembic décalée, absente ou multiple, tables / colonnes manquantes, serveur inattendu, version non qualifiée, isolation, serveur injoignable |
| `tests/test_f04b_dependencies.py` | dépendances directes, verrou complet et empreintes, cohérence CI, `runtime.txt`, aucun alias httpx |
| `tests/test_f04b_dormancy.py` | fichiers figés inchangés (sha256), aucun module applicatif ne référence F04-B ; avec les **quinze** variables (dont `DATABASE_URL` synthétique) : `api:app` identique, aucun moteur F2 ni module F04-B, vrai serveur Uvicorn identique ; chargeur F2 appelé explicitement : complet accepté, chaque variable manquante refusée |
| `tests/test_f04b_production_install.py` (+ `tests/f04b_production_probe.py`) | installation de production seule dans un environnement vierge, journalisée (voir § 4.3) |

Tests existants ajustés (nominativement, sans affaiblir leurs garanties) :

- `tests/test_f2_dormancy.py` : `MAIN_REQUIREMENTS` devient la liste exacte des dépendances directes de
  `requirements.in` (lue par `production_requirements()`) ; nouvelle matrice fermée
  `F04B_REFERENCE_MATRIX` / `F04B_REFERENCE_NAMES` (fichier → modules F2 et noms exacts) et test
  `test_f04b_reference_matrix_is_closed_and_exact` ; marqueurs exacts tolérés `F04B_*_MARKERS`.
- `tests/test_f2_http_dormancy.py`, `tests/test_authz_f2_dormancy.py`, `tests/test_route_policy_dormancy.py` :
  comparaison des dépendances via `production_requirements()` ; `config.py` admis par la matrice F04-B.
- `tests/test_authz_f2_pr9b1_boundary.py`, `tests/test_authz_f2_pr9b2_boundary.py` : exceptions
  nominatives avec liste **exacte** des marqueurs (`DecryptageKeyring` pour la configuration,
  `decryptage_turn_reservations` pour le précontrôle, `decryptage_providers` pour les ressources).

## 9. Risques résiduels et réserves

1. **Pool non qualifié pour Railway** : 5 + 2 connexions, attente 5 s, recyclage 30 min sont des valeurs
   candidates ; la limite de connexions du PostgreSQL Railway, la concurrence réelle (2 orchestrations C3
   + requêtes courtes + authentification) et le comportement sous saturation restent à mesurer (F04-E).
2. **Délai de connexion TCP non borné par F04-B** : `pool_timeout` borne l'attente d'une connexion du
   pool, pas l'établissement d'une connexion neuve (aucun `connect_timeout` n'a été ajouté, hors
   paramètres figés). À arbitrer en F04-C / F04-E (ou via l'URL fournie par Railway).
3. **Version PostgreSQL Railway inconnue** : le précontrôle exige une majeure qualifiée (16 ou 18) ; une
   autre version fera échouer le démarrage (fermé) jusqu'à qualification.
4. **`core.db` chargé indirectement** par le runner (`core.models`) lors de la future composition : le
   moteur legacy est alors *construit* (sans connexion) si `DATABASE_URL` est défini. F04-B ne l'utilise
   jamais et ne le charge pas ; à traiter par F04-C (preuve d'absence d'usage, voire d'import).
5. **Usage après fermeture** : une fois fermés, les clients Supabase / Resend / Claude possédés lèvent
   l'erreur de leur bibliothèque (non classée comme incertitude fournisseur) ; la garantie qu'aucun
   travail n'utilise une ressource fermée appartient à F04-C / F04-E (§ 6.3).
6. **Mise à jour des dépendances legacy** : `requests`, `yfinance`, `sqlalchemy`, `psycopg2-binary`,
   `alembic`, `python-dotenv` et `anthropic` étaient résolus librement par Railway à chaque build ; ils sont
   désormais figés aux versions qualifiées par la CI. Les versions actuellement installées sur Railway ne
   sont pas connues du dépôt : la **validation Railway** après merge doit confirmer le build et le
   démarrage legacy.
7. **Plateforme Railway** : le verrou contient les empreintes de toutes les distributions publiées ; une
   plateforme exigeant une distribution absente ferait échouer le build (fermé, jamais d'installation
   non vérifiée).
8. **P1 (frontière réseau Railway) et P2 (arrêt et drainage C3) restent des RÉSERVES OUVERTES**, non
   traitées par F04-B ; aucune activation ne peut être décidée.
9. `docs/ci.md` décrit encore `requirements.txt` comme « inchangé » : non modifié (hors documentation
   F04-B) ; les versions qu'il cite restent exactes.

## 10. Preuve de dormance

- `api.py`, `core/db.py`, `alembic/env.py`, `core/route_policy/*`, `runtime.txt` : octet pour octet ceux de
  `5ccc9e2` (`test_frozen_files_are_unchanged`).
- Aucun point d'entrée, aucune route, aucune migration, aucun middleware, aucune variable d'activation,
  aucune modification Railway, aucun déploiement.
- `api:app` répond identiquement avec ou sans les quinze variables F2 (synthétiques) : aucune route F2,
  aucun module F2 ni F04-B chargé, aucune tentative de connexion
  (`test_the_served_entrypoint_ignores_a_complete_f2_configuration`).
- Seul effet de production possible : l'installation des dépendances épinglées par `requirements.txt`
  (mêmes versions que la CI) lors du prochain build Railway.
