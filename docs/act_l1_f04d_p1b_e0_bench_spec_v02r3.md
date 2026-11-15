# ACT-L1 / F04-D / P1-B — Banc E0 : spécification technique d'implémentation V0.2-R3

| Élément | Valeur |
|---|---|
| Document | `docs/act_l1_f04d_p1b_e0_bench_spec_v02r3.md` |
| Version | **V0.2-R3** (consolidée, intégrant R3-F01 à R3-F04, P1 à P4, FZ-01, FZ-02 et les deux décisions finales d'audit) |
| Date | 15/11/2026 |
| Référence `main` | `5906163ae32cff0d1caed7b6aae5b9e4fc38261d` (merge de la PR #244) |
| Protocole d'autorité | `docs/act_l1_f04d_p1b_e0_protocol_v01c.md` — E0 V0.1c, SHA-256 `b25af66a9c387532961dfa6a9811a31502d6fe38431e1bb538d71497ad320b27`, **inchangé** |

> **Statut : V0.2-R3 — candidate au freeze documentaire, approuvée en conception, en attente de l'audit de PR et de
> la décision formelle du fondateur.** Le freeze documentaire **n'est pas effectif** à la date de ce document.
>
> Ce document **n'autorise aucune implémentation** : la PR documentaire qui l'introduit **ne vaut pas G-E0-I**. Aucun
> code, aucune ressource Cloudflare ou Railway, aucun secret, aucun déploiement, aucun essai distant. **Aucun élément
> expérimental n'est présenté comme validé** : toutes les cellules distantes de la matrice (§ 4) sont **NON
> EXÉCUTÉES**.
>
> **P1 OUVERTE.** Option A INCONCLUSIVE. **B2 non qualifiée.** **F2 dormant.** **F-02A inchangé.** **G-E0-I non
> autorisé.** **Aucun essai distant réalisé.** Aucune disposition de ce document ne modifie un critère du protocole
> E0 V0.1c ; en cas de divergence, **le protocole prévaut** et la divergence est signalée (§ 10).

## 0. Référentiel

| Élément | Valeur |
|---|---|
| `main` relu | `5906163ae32cff0d1caed7b6aae5b9e4fc38261d` ; arbre propre ; aucun changement depuis le commit audité |
| Protocole figé | `docs/act_l1_f04d_p1b_e0_protocol_v01c.md`, SHA-256 `b25af66a9c387532961dfa6a9811a31502d6fe38431e1bb538d71497ad320b27` |
| Contrats relus | F04-A V0.3 (`docs/act_l1_f04a_architecture_v03.md` : § 6.1, § 6.3, § 7.2, § 8.3) ; F04-B V0.3 (`docs/act_l1_f04b_freeze_v03.md` : § 9, § 10, verrous) ; F04-C V0.3 (`docs/act_l1_f04c_freeze_v03.md` : § 1 à § 4, réponse 503 de maintenance) ; F-02A V0.2 (`docs/f02a_routing_policy.md` : § 5 à § 7) |
| Registre F-02A | `core/route_policy/registry.py` (SHA-256 `d8accc92a55170f5fa4092eae18c7e8c29cf921f97b9853402516decefdb4b29` au commit de référence) : `EXPECTED_COUNTS` = `F2_AUTH: 14`, `F2_PERSONAL: 13` |
| Banc existant | `diagnostics/p1q/` (P1Q-V0.1-R1) et `tests/test_p1q_*.py` — réutilisation **conceptuelle** seule (§ 3.10) |
| CI | `.github/workflows/ci.yml` (SHA-256 `bb81b3d914847b1e79882d536b1223e7bb06e0a34573592a5ae99a6208c5fb96`) : six contrôles stables `postgres-16`, `postgres-18`, `migrations-16`, `migrations-18`, `web`, `required` ; aucun test sauté (`ci/check_test_report.py`) ; Python 3.11.9 ; Node 22.23.3 ; PostgreSQL 16.15 et 18.6 jetables en boucle locale |
| Verrous | `requirements.txt` (production) ; `ci/requirements-ci.txt` = production + `CI_ONLY` = {iniconfig, packaging, pluggy, pygments, pytest, pyyaml} (`tests/test_f04b_dependencies.py`) |

**Note de statut du protocole.** Le fichier du protocole E0 V0.1c porte encore, en tête, la mention « Statut :
PROPOSITION soumise à la validation finale du freeze expérimental », alors que le commit `d39be60` qui l'a introduit
le désigne comme « frozen … (G-E0-F) » et que le fondateur a validé G-E0-F. Le présent document **ne modifie pas** le
protocole ; il le traite comme figé (G-E0-F) et signale cette incohérence de statut pour décision du fondateur.

### 0.1 Historique de la spécification

| Version | Objet |
|---|---|
| V0.1 | première spécification d'implémentation (architecture, sécurité, matrice, formats, livraison) |
| V0.2 | corrections C1 à C9 et R5 à R7 (verdicts à trois niveaux, cryptographie éprouvée, IP indépendante, traçabilité Wrangler, attestation émise ≠ reçue, sonde non décisionnelle, L-ACC ≠ G-2, autorisations exactes, origine des preuves) |
| V0.2-R1 | corrections C-AUD-01 à C-AUD-07 (table A-E0, ambiguïté IP-11c/L-ACC, champs de rapport, verdicts Wrangler, contrat de comparaison IP, classification sans engagement, clés retirées) |
| V0.2-R2 | corrections B1 à B5 (identités de script, contrôle négatif pré-signature, exception IP-11c explicite, attribution par l'analyseur, intégrité des FAIL) |
| Arbitrages R2/R3 | R2-01 à R2-04, R3-01 à R3-03 (préfixe gardé, contrôle F-02A statique, divergence L-ACC B2, comparaison post-signature sans endpoint) |
| **V0.2-R3** | R3-F01 à R3-F04, P1 à P4, décisions A-05/A-06, FZ-01, FZ-02, schéma définitif `ipwitness-record`, décisions finales DF-1 (corroboration des extrémités calculée par l'analyseur) et DF-2 (inventaire exact des tests, `test_e0_manifest.py`) |

La traçabilité de chaque correction de la révision R3 vers les sections du présent document figure en annexe D.

## 1. Principes et portée

### 1.1 Invariants de la future PR d'implémentation

| # | Invariant |
|---|---|
| I1 | **Isolement du code** : aucun import d'un module Oryx (`api`, `core`, `scripts`, `alembic`) ni du banc P1-Q, **y compris dans les tests du banc** ; le contrôle F-02A est **statique** (SHA-256 + AST, § 3.9). |
| I2 | **Aucune activation automatique** : après merge, rien ne se crée, ne se déploie ni ne s'active — ni chez Cloudflare (aucune intégration Git ni Workers Builds), ni chez Railway (projet E0 non rattaché au dépôt, déploiement par `railway up` depuis un extrait local), ni dans GitHub Actions (aucun workflow ajouté ou modifié). |
| I3 | **Aucune nouvelle dépendance de production ou de CI.** Bibliothèque standard et paquets déjà présents dans `requirements.txt`, aux mêmes versions et empreintes ; OpenSSL côté Python (§ 3.4) ; WebCrypto natif côté Worker ; Wrangler épinglé dans un verrou propre au banc, jamais installé en CI. |
| I4 | **Défaut fermé** : cellule absente, famille indisponible, préflight non conforme, registre incomplet, collecte non intègre, origine de preuve insuffisante, configuration Wrangler non reconstructible, comparaison IP non attribuable ou garde d'activation Railway non satisfaite → INCONCLUSIVE, **jamais PASS**. |
| I5 | **Proposition, pas décision** : l'analyseur propose ; il ne simule **jamais** l'audit indépendant ni une décision du fondateur ; il ne prononce pas la qualification de P1 ; l'acceptation de L-ACC ne produit **jamais** automatiquement GO-CANDIDATE B2. |
| I6 | **Confidentialité** : aucune adresse IP globale brute persistée ; aucun secret et aucun couple « clé de comparaison + empreinte d'adresse » dans un journal, un rapport, une preuve versionnée ou Git. |
| I7 | **Fidélité au protocole** : catalogue machine relié 1:1 aux identifiants du § 10 du protocole ; toute dérive (identifiant, classe, phase) fait échouer la CI. |
| I8 | **Neutralité frontend** : la sécurité du transport backend ne dépend d'aucun navigateur ni frontend particulier ; l'attestation est strictement interne au transport Cloudflare → Railway, n'authentifie pas l'utilisateur et ne prouve aucune identité ; aucun contrat F2 figé n'est modifié (§ 3.11). |

### 1.2 Trois niveaux de résultat

L'analyseur sépare strictement trois niveaux, jamais fusionnés :

1. **Niveau 1 — Verdict des essais** : `PASS`, `FAIL`, `INCONCLUSIVE` ou `RELEVÉ` (essais INFO), selon la classification du protocole.
2. **Niveau 2 — Éligibilité expérimentale du chemin** : contrôle de la matrice, de la couverture, de l'IPv6 native, des preuves, des inventaires, des configurations effectives et des exceptions ; émission d'une **proposition technique**, sans prétendre avoir réalisé l'audit indépendant.
3. **Niveau 3 — Qualification formelle** : un GO-CANDIDATE B2 formel exige l'intégralité des conditions normatives du § 11 du protocole (§ 5.5, FZ-01), dont l'audit indépendant sans réserve bloquante et les décisions requises. **La validation finale ne se déduit jamais de la seule sortie du logiciel.**

Interdictions directrices : ne jamais attribuer un GO-CANDIDATE validé automatiquement ; ne jamais considérer une
matrice intégralement PASS comme un audit indépendant ; ne jamais qualifier F2 ni fermer P1 ; ne jamais transformer
une décision reportée à F04-D en critère éliminatoire des essais E0 (§ 5.5).

### 1.3 Correspondance des décisions A-E0-02 à A-E0-11

Intitulés fournis par l'audit d'architecture (non reconstruits) et éléments qui en assurent le respect :

| Décision | Principe architectural | Modules | Tests | Sections |
|---|---|---|---|---|
| A-E0-02 | Isolation stricte : aucune dépendance au runtime Oryx, aucune donnée ou ressource de production | `e0_guard.py`, `.gitignore`, `production_denylist.v1.json` | `test_e0_isolation.py`, `test_e0_guard.py` | § 3.1, § 3.3, § 3.9, § 3.10 |
| A-E0-03 | M-X-RT comme chemin d'entrée prioritaire, autres chemins selon les conditions du protocole | `e0_edge.js`, `e0_edge_core.js`, gabarits `p1b` et `p2` | `test_e0_wrangler_config.py`, `e0_edge.test.mjs` | § 2.3, § 2.4, § 4.8 |
| A-E0-04 | Preuves indépendantes : sonde, Worker, origine et analyseur ne s'autoqualifient pas | `e0_probe.py`, `e0_ipwitness.py`, `e0_origin.py`, `e0_analyze.py` | `test_e0_probe.py`, `test_e0_ipwitness.py`, `test_e0_origin_http.py`, `test_e0_analysis.py` | § 2.2, § 2.6, § 2.8, § 4.5, § 5.3 |
| A-E0-05 | Aucun effet E0 au merge : aucun déploiement ni activation automatique | `e0_wrangler.py` (garde de déploiement) ; aucun fichier racine | `test_e0_isolation.py`, `test_e0_wrangler_config.py` | I2, § 3.8, § 7.5 |
| A-E0-06 | Contrat unique et versionné de collecte, distinction des données fiables et non fiables | `e0_jsonl.py`, `e0_manifest.py`, `e0_version.py`, `config/*.v1.json` | `test_e0_manifest.py`, `test_e0_catalogue.py`, `test_e0_analysis.py` | § 5 |
| A-E0-07 | Verdicts établis par rapprochement de preuves indépendantes | `e0_analyze.py` | `test_e0_analysis.py`, `test_e0_corrections.py` | § 1.2, § 2.8, § 4.5, § 5.3, § 5.5 |
| A-E0-08 | Arborescence autonome dans `diagnostics/e0/` | arborescence § 2.1 | `test_e0_isolation.py` | § 2.1, § 7.1 |
| A-E0-09 | Modules séparés selon leurs responsabilités et leurs frontières de confiance | tous les modules `e0_*` et `edge/src/*` | tests par module (§ 6) | § 2, § 3 |
| A-E0-10 | CI existante conservée, tests E0 bloquants, aucun déploiement automatique | aucun changement de `.github/` ni de `ci/` | `test_e0_isolation.py`, `test_e0_analysis.py` | § 6.4, § 7.5 |
| A-E0-11 | Workers adversariaux et variantes de transport inactifs tant que leur phase n'est pas autorisée | `edge/src/attackers/*`, gabarits `e0-cross`, `e0-samezone-*`, `e0-binder`, `e0-tlsneg`, `tunnel/` | `e0_attackers.test.mjs`, `test_e0_wrangler_config.py` | § 2.4, § 2.5, § 2.10, § 7.6, § 7.7 |

## 2. Architecture détaillée

### 2.1 Arborescence et inventaire exact de la future PR d'implémentation

Seul le présent document est créé par la PR documentaire. Tous les autres fichiers ci-dessous sont des **livrables de
la future PR d'implémentation**, soumise à G-E0-I, et **n'existent pas** au commit de référence.

```text
diagnostics/e0/
  README.md                         rôle, phases, renvois aux guides ; « NE PAS EXÉCUTER sans feu vert »
  .gitignore                        evidence/, *.jsonl, node_modules/, .wrangler/, .dev.vars, *.pem, *.key, rendered/
  py/
    Dockerfile                      image e0-origin (python:3.11.9-slim, USER nobody, COPY explicite, openssl présent)
    requirements-origin.txt         uvicorn 0.27.0, h11 0.16.0, click 8.5.0 : versions et empreintes de production
    requirements-n2.txt             psycopg2-binary 2.9.13 : version et empreintes de production
    e0_version.py                   PROTOCOL="E0-V0.1c", FORMAT=1, noms d'en-têtes, domaines de séparation
    e0_guard.py                     garde d'environnement, de cibles, de production, de CI
    e0_redact.py                    table IANA v1, pseudonymisation HMAC par démarrage, rédaction de texte libre
    e0_ipclass.py                   classification V / I-a / I-b / I-c / I-d / F (miroir de la logique Worker, sans engagement)
    e0_ipcompare.py                 contrat versionné de comparaison IP (pré-signature et post-signature)
    e0_ed25519.py                   adaptateur OpenSSL (validations D1 à D9), aucune implémentation mathématique de courbe
    e0_attest.py                    format v1 : encodage, décodage strict, vérificateur par phase (PA2 à PA15)
    e0_keysets.py                   clés de vérification de la phase courante et clés publiques historiques d'audit
    e0_jsonl.py                     JSON canonique, chaînage SHA-256, empreintes de fin
    e0_manifest.py                  manifestes, registre des points d'entrée, deploy-evidence, preuves manuelles
    e0_origin.py                    application ASGI e0-origin
    e0_main.py                      lanceur Railway (E0_ROLE=origin), TTL, garde au démarrage
    e0_probe.py                     sonde (CLI)
    e0_probe_cases.py               catalogue des requêtes, par identifiant du protocole
    e0_curl.py                      transport curl (HTTP/2, HTTP/3 si disponible), arguments fermés
    e0_ipwitness.py                 témoin IP indépendant : observations brutes de l'adresse publique de sortie
    e0_inventory.py                 inventaire Cloudflare en lecture seule, comparaison aux attendus
    e0_wrangler.py                  rendu, validation et identité des fichiers Wrangler ; enveloppe de déploiement (opérateur)
    e0_keygen.py                    génération éphémère des clés de phase (via e0_ed25519), tube vers wrangler secret put
    e0_analyze.py                   intégrité, attribution, corroboration des extrémités, verdicts à trois niveaux, rapport
    e0_catalogue.py                 chargement et contrôle du catalogue
    e0_f02a_drift.py                garde de dérive F-02A : SHA-256 du registre et extraction AST statique, sans import
    e0_n2.py                        magasin anti-rejeu N2 (psycopg2), refus fermé
    e0_n2_bench.py                  banc N2 multiprocessus, mandataire d'injection de fautes PostgreSQL
    e0_vectors.py                   génération et contrôle des vecteurs croisés
  edge/
    package.json                    "type": "module", private, devDependencies : wrangler en version exacte
    package-lock.json               verrou avec intégrité (jamais installé en CI)
    src/e0_edge.js                  module d'entrée : export default { fetch } UNIQUEMENT
    src/e0_edge_core.js             classification réseau, contrôle négatif pré-signature, PA13, PA14, PA15, signature
    src/e0_attest.js                encodage v1, signature WebCrypto Ed25519
    src/e0_ipclass.js               analyse stricte d'IP, table IANA
    src/e0_ipcompare.js             contrat de comparaison pré-signature (détection, validation, comparaison)
    src/e0_registry.generated.js    table générée depuis config/ip_special_registry.v1.json (contrôlée par test)
    src/attackers/e0_cross.js       Worker adversarial inter-zones (sans clé)
    src/attackers/e0_samezone.js    Worker adversarial même zone (variantes plain et strict)
    src/attackers/e0_binder.js      Worker de test par service binding
    src/attackers/e0_tlsneg.js      témoin négatif TLS
    wrangler/
      e0-edge.p1a.toml.tmpl  e0-edge.p1a-close.toml.tmpl  e0-edge.p1b.toml.tmpl  e0-edge.p2.toml.tmpl
      e0-edge.p2-cd.toml.tmpl  e0-edge.p2-fault.toml.tmpl  e0-edge.witness-16c.toml.tmpl
      e0-cross.toml.tmpl  e0-samezone-plain.toml.tmpl  e0-samezone-strict.toml.tmpl
      e0-binder.toml.tmpl  e0-tlsneg.toml.tmpl
  tunnel/
    cloudflared.config.tmpl.yml     M-T-TLS (1c) : ingress https seul, noTLSVerify=false
  config/
    catalogue.v1.json               essais du § 10 du protocole : classe, phase, chemins, cellules, engagement_required
    ip_special_registry.v1.json     registres IANA spéciaux IPv4 et IPv6 (colonne « Globally Reachable »)
    i_a_addresses.v1.json           [2a06:98c0:3600::103] (D18) ; liste versionnée, jamais inventée
    f2_canonical_targets.v1.json    27 couples F2 canoniques (RT-13) et leur instanciation canonique
    f2_registry.lock.v1.json        SHA-256 de core/route_policy/registry.py et instantané 14 + 13
    rt13_variants.v1.json           cibles encodées, normalisation, paramètres répétés
    to_levels.v1.json               14 paliers GET (valeurs proposées, U-TOL)
    phase_params.v1.json            W et S provisoires, budgets, TTL, concurrence
    inventory_expectations.v1.json  état attendu par phase et par fenêtre
    production_denylist.v1.json     marqueurs de production (oryx-api-production, ...)
    ipwitness_sources.v1.json       sources autorisées du témoin IP, par famille
    worker_identities.v1.json       identité de script attendue par gabarit
    phase_manifest.template.json    gabarit à remplir par l'opérateur (aucun secret)
  vectors/
    attestation_v1.json  ipclass_v1.json  commitment_v1.json  ipcompare_v1.json
docs/
  act_l1_f04d_p1b_e0_bench_spec_v02r3.md     présent document (créé par la PR documentaire)
  act_l1_f04d_p1b_e0_bench_guide_v01.md      guide d'exécution phase par phase (NE PAS EXÉCUTER) — future PR
  act_l1_f04d_p1b_e0_formats_v01.md          formats versionnés détaillés (§ 5) — future PR
  act_l1_f04d_p1b_e0_report_template_v01.md  modèle du rapport daté — future PR
tests/
  e0_support.py
  test_e0_ed25519.py  test_e0_attestation.py  test_e0_keysets.py  test_e0_ipclass.py  test_e0_ipcompare.py
  test_e0_guard.py  test_e0_origin_http.py  test_e0_probe.py  test_e0_ipwitness.py  test_e0_inventory.py
  test_e0_wrangler_config.py  test_e0_keygen.py  test_e0_catalogue.py  test_e0_f02a_drift.py  test_e0_manifest.py
  test_e0_analysis.py  test_e0_n2_pg.py  test_e0_isolation.py  test_e0_corrections.py
  web/e0/e0_edge.test.mjs  e0_attest.test.mjs  e0_ipclass.test.mjs  e0_ipcompare.test.mjs
  web/e0/e0_attackers.test.mjs  e0_static.test.mjs
```

**Inventaire exact de la future PR d'implémentation (101 fichiers, tous nouveaux) :**

| Emplacement | Nombre | Contenu |
|---|---|---|
| `diagnostics/e0/` (racine du banc) | 2 | `README.md`, `.gitignore` |
| `diagnostics/e0/py/` | 28 | `Dockerfile`, 2 fichiers `requirements-*.txt`, 25 modules `e0_*.py` |
| `diagnostics/e0/edge/` | 24 | `package.json`, `package-lock.json`, 6 modules `src/`, 4 Workers de test `src/attackers/`, 12 gabarits `wrangler/` |
| `diagnostics/e0/tunnel/` | 1 | `cloudflared.config.tmpl.yml` |
| `diagnostics/e0/config/` | 13 | fichiers `*.v1.json` et `phase_manifest.template.json` |
| `diagnostics/e0/vectors/` | 4 | vecteurs croisés |
| `docs/` | 3 | guide, formats, modèle de rapport (le présent document est créé par la PR documentaire) |
| `tests/` | 26 | `e0_support.py`, 19 fichiers `test_e0_*.py`, 6 fichiers `web/e0/*.test.mjs` |

Règles de placement : aucun fichier n'est ajouté à la racine du dépôt (ni `railway.json`, `railway.toml`,
`Procfile`, `Dockerfile`, `package.json` ou `wrangler.toml`), afin de ne pas changer la détection du constructeur
Railway de production ; aucun `test_*.py` sous `diagnostics/` ; conformément au § 9.1 du protocole, **aucun
`railway.json`** n'est versionné : les réglages Railway sont appliqués par l'opérateur et consignés en preuve. Le
**test réel de suppression des variables Railway** n'est pas un test CI : c'est une opération distante autorisée
(§ 2.6, § 11.2 de l'annexe C).

### 2.2 e0-origin

| Aspect | Spécification |
|---|---|
| Rôle | Origine de diagnostic sur Railway (projet E0 dédié). Mesure TO, RT et HTTP ; vérificateur de diagnostic par phase (PA15) ; journal des attestations reçues et de leur `h` ; compteur des requêtes en clair ; calcul transitoire de l'engagement post-signature de l'IP signée. |
| Exécution | ASGI 3 minimal sous **Uvicorn 0.27.0** ; `proxy_headers=False`, `http="h11"`, `lifespan="off"`, `access_log=False`, `server_header=False`, journal Uvicorn au niveau `warning`. Une seule réplique. `restartPolicy=NEVER`. TTL borné. |
| Dépendances | bibliothèque standard + uvicorn, h11, click (pins de production) + modules `e0_*` (guard, redact, ipclass, ipcompare, ed25519, attest, keysets, jsonl). |
| `GET /e0/health` | identification : en-tête `x-e0-origin: E0-V0.1c`, `boot_id`. |
| `/e0/observe` (toutes méthodes) | observation pseudonymisée et observations élémentaires d'attestation. |
| `/e0/slow?d=<s>&n=<octets>` | en GET, attente de `d` secondes **sans aucun octet anticipé**, puis 200 avec un corps déterministe de `n` octets ; en POST, lecture complète du corps (≤ 512 Kio), attente, puis renvoi de la longueur et du SHA-256. Concurrence bornée (`E0_SLOW_CONCURRENCY`). |
| `/e0/cookies` | trois `Set-Cookie` `__Host-e0-a`, `__Host-e0-b`, `__Host-e0-c` (valeurs **synthétiques**, `Secure; Path=/; HttpOnly; SameSite=Strict`). |
| `/e0/redirect` | 302 vers `https://<hôte public>/e0/observe?redirected=1` : un suivi éventuel laisserait une seconde trace à l'origine (RT-06). |
| **Préfixe protégé `/e0/guarded` et `/e0/guarded/<tid>`** | tout chemin dont les deux premiers segments sont `e0` puis `guarded`, avec ou sans segment `<tid>`, est routé **déterministiquement** vers le **vérificateur gardé** : 200 si attestation valide dont `t` est égal à la cible brute reçue (incluant `/e0/guarded/<tid>`), sinon **503 `{"error":"temporarily_unavailable"}`** avec les en-têtes no-store, octets constants recopiés de F04-C § 4, **sans import**. Le `<tid>` est un segment de chemin inclus dans la cible signée `t`. **Le catch-all exclut explicitement ce préfixe** : aucune cible adversariale n'atteint la route générique d'observation. |
| `/e0/page` | page HTML statique (CSP `default-src 'self'`) exécutant GET et POST same-origin vers `/e0/observe` et permettant de télécharger l'observation : **instrument de mesure** (RT-04, RT-05), non une dépendance (§ 3.11). |
| Catch-all | toute autre cible : observation (RT-13, 27 couples F2 et variantes). |
| Aucun endpoint de comparaison | l'origine n'expose **aucune** route de comparaison IP ; la comparaison post-signature est faite hors ligne par l'analyseur (§ 2.6, § 2.8). |
| Vérificateur par phase | accepte **seulement** la clé et l'`aud` de `E0_PHASE` (`1a` → `oryx-e0-1a` ; `1b` et `2` → `oryx-e0-2`), à partir du seul ensemble « clés de vérification de la phase courante » (`E0_VERIFY_KEYS`). Ordre PA8 : comptage de l'en-tête → grammaire → `v` → `aud` → `kid` → bornes → signature Ed25519 (OpenSSL) → fenêtre temporelle → liaisons pures (méthode, `h` = hôte public attendu, `hi` = Host reçu, `t` = cible brute reçue) → reclassement de l'IP → **puis seulement** consommation du nonce (magasin en mémoire par démarrage). Codes de refus : `att_absent`, `att_multiple`, `grammar`, `aud_not_current_phase`, `kid_unknown`, `kid_retired`, `sig_invalid`, `ts_future`, `ts_expired`, `bind_method`, `bind_h`, `bind_hi`, `bind_target`, `ip_not_v`, `replay_in_boot`. |
| Observations élémentaires d'attestation | pour chaque requête : **`att_present`** (en-tête `x-oryx-e0-attestation` observé) ; **`att_crypto_valid`** (signature valide avec une clé publique identifiée de l'ensemble courant **ou** de l'ensemble historique d'audit, avec `kid`, `aud` et origine `current` ou `audit`) ; **`att_policy_accepted`** (acceptée par la politique de la phase courante) ; **cible, nonce, horodatage et références de tentative observés** (`t`, `nonce`, `ts`, `x-e0-run`, `x-e0-seq`, `tid`). **L'origine ne produit jamais `att_issued_evidence`** : cette conclusion dérivée appartient au seul analyseur (§ 2.8). |
| Engagement post-signature | pour les essais concernés, l'origine calcule **transitoirement** `eng_post = HMAC(cmp_key_post, canonical(ip_signée))`, domaine `oryx-e0-ipcompare/post/v1`, avec `cmp_key_post` provisionnée avant le run (§ 2.6). L'IP signée est traitée en mémoire puis écartée ; l'origine journalise `eng_post` et `cmp_key_post_id`, **jamais** l'IP brute ni la clé. |
| Observation | méthode ; version HTTP ; `raw_path` et chaîne de requête (base64url) ; Host ; Origin ; Referer ; `Sec-Fetch-*` ; SHA-256 de Cookie (valeurs synthétiques) ; longueur et SHA-256 du corps ; `X-Forwarded-Proto` ; en-têtes d'IP **pseudonymisés** (clé par démarrage) ; observations élémentaires d'attestation avec `aud`, `kid`, origine de la clé, `h`, classe et pseudonyme de l'IP signée ; `eng_post` et `cmp_key_post_id` ; diagnostic d'edge (`x-e0-edge-diag`, marqué **non authentifié**) ; `seq` ; `boot_id` ; `RAILWAY_REPLICA_ID`. **Jamais** l'en-tête d'attestation brut, la valeur de Cookie, une IP globale brute ni une clé. |
| Journal | une ligne `E0-ORIGIN {json}` par requête sur stdout, **chaînée** (`prev` = SHA-256 de la ligne précédente) ; point de contrôle `origin-checkpoint` toutes les 100 lignes, à l'échéance du TTL et à l'arrêt ; taille ≤ 4 Kio par ligne. La ligne de démarrage porte le nouveau `boot_id` (garde d'activation, § 2.6). |
| Compteur en clair | nombre de requêtes reçues avec `X-Forwarded-Proto: http`, par cas ; exposé dans chaque observation et chaque point de contrôle. |
| Paramètres (variables) | `E0_ROLE=origin` ; `E0_PHASE` ∈ {1a, 1b, 2, 1c} ; `E0_VERIFY_KEYS` (JSON public de la phase courante : `kid`, `aud`, clé publique base64url) ; `E0_AUDIT_PUBKEYS` (JSON public historique, clés retirées comprises, **sans pouvoir d'acceptation**) ; `E0_RETIRED_KIDS` ; `E0_PUBLIC_HOST` ; `E0_CMP_KEY_POST` et `E0_CMP_KEY_POST_ID` (par run, détruits après analyse) ; `E0_TTL_SECONDS` (60 à 86 400, défaut 14 400) ; `E0_MAX_REQUESTS` (défaut 20 000) ; `E0_W_MS` et `E0_S_MS` (provisoires, U-WS) ; `E0_SLOW_MAX_S` (300) ; `E0_SLOW_CONCURRENCY` (8) ; `E0_BODY_MAX` (524 288) ; en 1c seulement, `E0_TLS_CERT_PEM_B64` et `E0_TLS_KEY_PEM_B64` (écrits en 0600 dans un tmpfs, puis `ssl_certfile`/`ssl_keyfile` d'Uvicorn). |
| Garde au démarrage | liste blanche exacte des variables : `PATH`, `HOME`, `HOSTNAME`, `LANG`, `PORT`, `GPG_KEY` et `PYTHON_*` de l'image officielle, `RAILWAY_*`, et les seuls `E0_*` ci-dessus ; **tout autre nom → code de sortie 3** (noms affichés, jamais les valeurs) ; liste noire P1-Q réimplémentée en complément. Sur Railway : `RAILWAY_SERVICE_NAME` préfixé `e0-origin`, `RAILWAY_PROJECT_NAME` préfixé `oryx-e0`, `RAILWAY_PUBLIC_DOMAIN` de la forme `e0-origin….up.railway.app` ; sinon code 3. |

### 2.3 e0-edge (Worker)

| Aspect | Spécification |
|---|---|
| Module d'entrée | `e0_edge.js` exporte **uniquement** `default { fetch }` : aucune exportation nommée, aucune classe `WorkerEntrypoint` ou `DurableObject`, aucun gestionnaire `scheduled`, `queue`, `email`, `tail` ni `alarm`. La logique est dans `e0_edge_core.js` (importé), que les tests importent directement. |
| Configuration | `[vars]` : `E0_PHASE`, `E0_ENTRY_HOST`, `E0_ORIGIN_HOST` (motif `^e0-origin[a-z0-9-]*\.up\.railway\.app$`), `E0_KID`, `E0_ALLOW_FAULT` ("0" sauf fenêtre RT-12). Secret : **`E0_SIGNING_KEY_1A`** (phase 1a) **ou** **`E0_SIGNING_KEY_2`** (phases 1b à 2), PKCS#8 en base64url. Le schéma `https://` est codé en dur ; l'hôte d'origine est validé à chaque requête. |
| Refus de configuration (HTTP-03) | hôte d'origine absent ou hors motif ; schéma, port, chemin ou IP littérale présents ; hôte d'origine égal à l'hôte d'entrée ou dans la zone d'entrée ; phase inconnue ; secret de la phase absent ; **secret d'une autre phase présent** (PA15) ; `kid` incohérent avec la phase → **503 local `e0_config_invalid`**, aucun `fetch()`, aucune signature. |
| Requête visiteur en `http:` | 403 local `e0_https_required`, aucun `fetch()` (HTTP-01, HTTP-02). |
| PA13 | retrait de **tout** en-tête entrant `x-oryx-*` et `x-e0-edge-*` avant toute décision. Les en-têtes `x-e0-cmp-pre-key` et `x-e0-cmp-pre-mac` sont lus par l'étape 2 **puis non transmis** à l'origine. |
| **Étape 1 — classification réseau V/I/F** (indépendante de toute donnée déclarée par la sonde) | **I-d** si l'hôte de `request.url` est différent de `E0_ENTRY_HOST` exact, ou si un port est explicite (PA14). **I-b** si `CF-Worker` ou `CF-EW-Via` est présent. **I-c** si `request.cf.asn === 13335` (indice de refus seulement, R5 ; **classe I-c non signée en E0**). **F** si `CF-Connecting-IP` est absent, multiple (virgule, puisque `Headers.get` concatène les doublons) ou illisible selon une grammaire stricte (pas d'espace, de crochet, de zone ni de zéro non significatif en IPv4), ou si l'adresse est non globale selon la table v1, mappée IPv4, de classe E (240.0.0.0/4) ou égale à une valeur de plage de documentation ou non routable. **I-a** si l'adresse figure dans `i_a_addresses.v1.json`. Sinon **candidat V**. Aucun engagement n'intervient à cette étape. |
| **Étape 2 — contrôle négatif pré-signature** (purement soustractif ; candidat V seulement) | **1. Détection** : un engagement est **présent** si au moins un des en-têtes `x-e0-cmp-pre-key` ou `x-e0-cmp-pre-mac` est présent. **Aucun engagement présent → no-op** : l'étape 2 n'est pas entrée et le candidat V poursuit. Le Worker ne lit jamais un indicateur `engagement_required` dans la requête : cette propriété appartient au catalogue et s'applique au verdict. **2. Validation** : un engagement présent est **bien formé** si les deux en-têtes sont présents, uniques et conformes à la grammaire de `e0_ipcompare` ; **présent mal formé → refus de signer** (`cmp_malformed`). **3. Comparaison** : pour un engagement présent et bien formé, calcul de `HMAC(cmp_key_pre, canonical(CF-Connecting-IP))`, domaine `oryx-e0-ipcompare/pre/v1`, et comparaison aux MAC committés : **concordant → aucun effet** (signature seulement si tous les autres critères V sont satisfaits) ; **discordant → classe F pour l'essai, refus de signer** (`cmp_disagreement`). |
| Invariant de l'étape 2 | elle **ne peut que retrancher** (F ou refus), **jamais promouvoir une adresse en V**, **jamais émettre de signature**. Anti-masquage : si l'étape 1 classe à tort une requête adversariale en V et qu'aucun engagement n'est présent, le Worker **signe** et l'émission interdite est **exposée** (FAIL), jamais masquée par un refus `cmp_*`. Les engagements restent des données contrôlables par le client et ne constituent jamais une preuve d'origine visiteur. |
| Signature | **classe V seulement** (PA12), après les deux étapes. Champs (§ 5.1) : `v`, `aud`, `kid`, `ts` (ms, `Date.now()`), `nonce` (16 octets, `crypto.getRandomValues`), `ip` canonique, `c=V`, méthode, `h` (hôte d'entrée), `hi` (`E0_ORIGIN_HOST`), `t` (`pathname+search` de l'URL sortante, incluant `/e0/guarded/<tid>` lorsque présent). |
| Transmission | `fetch(new Request("https://" + E0_ORIGIN_HOST + t, {method, headers, body: request.body, redirect: "manual", cache: "no-store"}))`. **Aucun `passThroughOnException`**, aucun autre `fetch()`, aucun `fetch()` vers l'hôte d'entrée, aucune nouvelle tentative. Exception de `fetch()` → 502 local `e0_origin_unreachable`. Réponses 3xx rendues telles quelles. Ajout de `x-e0-edge-diag` (base64url JSON : phase, classe, codes de motif dont `cmp_*`, marqueurs, `asn` **seulement si la classe est différente de V**, `httpProtocol`, `tlsVersion`, `signed`) : **aucune IP, aucune clé ni MAC**, **non authentifié**. |
| Réponse | inchangée, plus `x-e0-edge: E0-V0.1c;<phase>` (indice **non authentifié**, distinguant une réponse du Worker d'une erreur Cloudflare). |
| Injection de faute (RT-12) | `x-e0-fault: throw` lève une exception **seulement** si `E0_ALLOW_FAULT="1"` (gabarit `p2-fault`, fenêtre G-2 bornée, suivie d'IP-16b). |
| Journaux | aucun `console.*` (contrôle statique) ; `[observability] enabled=false`, `logpush=false`, aucun `tail_consumers`, `send_metrics=false`, `upload_source_maps=false`, `minify=false`. Jamais une IP, une clé ou un MAC. |
| Compatibilité | `compatibility_date` épinglée (≥ 2024-11-11 pour `cache: "no-store"`, à vérifier, U-CACHE) ; **aucun** drapeau de compatibilité (en particulier pas `global_fetch_strictly_public`). |

### 2.4 Configuration Wrangler : identités, rendu et traçabilité

**Identités de script** (`worker_identities.v1.json`) :

| Gabarit | Identité (`name`) | Rôle |
|---|---|---|
| `e0-edge.p1a`, `e0-edge.p1a-close`, `e0-edge.p1b`, `e0-edge.p2`, `e0-edge.p2-cd`, `e0-edge.p2-fault` | `e0-edge` | Worker principal, phases et fenêtres autorisées |
| `e0-edge.witness-16c` | `e0-edge` | exception IP-16c : redéploiement du **Worker principal** avec `workers_dev` et `preview_urls` **omis**, dans la seule fenêtre prévue (fin de E0-2) |
| `e0-cross` | `e0-cross` | Worker adversarial inter-zones |
| `e0-samezone-plain`, `e0-samezone-strict` | `e0-samezone` | Worker adversarial même zone, variantes de configuration |
| `e0-binder` | `e0-binder` | Worker de test par service binding |
| `e0-tlsneg` | `e0-tlsneg` | témoin TLS |

**Contenu des gabarits.**
- `p1a` : `workers_dev=true`, `preview_urls=false`, aucune Route.
- `p1a-close` : `workers_dev=false`, `preview_urls=false`, aucune Route (clôture 1a).
- `p1b` et `p2` : `workers_dev=false`, `preview_urls=false`, Route `app.<zone>/*` (M-X-RT).
- `p2-cd` : Custom Domain temporaire (fenêtre IP-11d) ; `p2-fault` : `E0_ALLOW_FAULT="1"` (fenêtre RT-12).
- `witness-16c` : **omet** `workers_dev` et `preview_urls` (témoin IP-16c uniquement).
- Aucune section `[env]` (un environnement Wrangler créerait un script distinct, contraire à « un seul script », P2 du protocole) ; aucun `durable_objects`, `queues`, `triggers`, `tail_consumers`, `assets`, `site` ni `build` ; `services` **seulement** dans `e0-binder` ; `global_fetch_strictly_public` **seulement** dans `e0-samezone-strict`.

**Rendu.** `e0_wrangler.py render --template … --manifest …` substitue des marqueurs fermés (`{{ACCOUNT_ID}}`,
`{{ZONE_NAME}}`, `{{ENTRY_HOST}}`, `{{ORIGIN_HOST}}`, `{{KID}}`, `{{COMPAT_DATE}}`), chaque valeur étant validée par
motif et contre la liste noire de production. Le fichier rendu est relu par `tomllib`, puis validé par un schéma
fermé (clés interdites, clés obligatoires, valeurs par phase, **identité de script**). Le `name` rendu doit être
exactement celui que `worker_identities.v1.json` associe au gabarit ; **toute substitution est refusée** (un gabarit
adversarial ne peut jamais produire `name = "e0-edge"`, et le Worker principal ne peut jamais être rendu sous une
identité adversariale). L'exception IP-16c est la seule où `e0-edge` est rendu avec des clés omises, uniquement depuis
`witness-16c`. Le fichier rendu est écrit **hors du dépôt** et son SHA-256 est consigné.

**Preuve de déploiement `deploy-evidence.v1`** (immuable, identité de version et empreinte, jamais remplacée
silencieusement), pour chaque déploiement :
1. commit Git et empreinte du gabarit ;
2. version verrouillée de Wrangler et empreinte de `package-lock.json` ;
3. manifeste des paramètres de rendu ;
4. contenu complet du fichier Wrangler effectivement transmis, hors secret ;
5. SHA-256 de ce fichier ;
6. identité de script, identifiant et version du Worker déployé ;
7. instantané Cloudflare avant et après déploiement ;
8. inventaire des points d'entrée anciens et nouveaux.

Un audit doit pouvoir reproduire le fichier rendu depuis le commit, le gabarit et le manifeste. **Aucun PASS n'est
fondé uniquement sur le contenu déclaré du gabarit.**

**Verdicts Wrangler.**

| Situation | Verdict |
|---|---|
| Configuration effective vérifiée et conforme (dont identité de script correcte), ensemble de preuves complet | PASS possible |
| Configuration effective dangereuse ou point d'entrée interdit démontré (réactivation constatée de `workers.dev`, d'une URL de version, d'un alias, d'un point d'entrée interdit, ou substitution d'identité), même si le fichier Wrangler semble conforme | FAIL |
| Configuration non reconstructible, preuve manquante ou inventaire incomplet | INCONCLUSIVE |

L'absence d'une empreinte ou d'une preuve de déploiement ne constitue pas, à elle seule, un échec de sécurité
démontré. Pour IP-16b : configuration non reconstructible → INCONCLUSIVE, sauf violation effective démontrée → FAIL.
Une preuve insuffisante interdit toujours GO-CANDIDATE.

**Enveloppe de déploiement.** `e0_wrangler.py deploy` est **réservé à l'opérateur**. Il refuse de s'exécuter si :
`CI`, `GITHUB_ACTIONS` ou une variable Oryx est présente ; l'arbre Git n'est pas propre ou le HEAD diffère du
`bench_sha` du manifeste ; la version de Wrangler diffère de la version épinglée ; `.dev.vars` ou `.wrangler/` existe
dans le dépôt ; l'`account_id` diffère de celui du manifeste ; l'identité rendue diffère de l'identité attendue. Il
exécute `wrangler deploy -c <rendu>`, extrait l'identifiant de version et les URL (ajoutés au registre des points
d'entrée) et rappelle l'obligation d'IP-15 et d'IP-16a (IP-16b). Il n'appelle **jamais** `wrangler dev`,
`wrangler preview`, `versions upload` (sauf le témoin d'alias d'IP-15, en commande dédiée et nominative) ni un
déploiement progressif. **Wrangler** : version exacte ≥ 4.44.0 (D26), publiée avant le 05/10/2026 ; installation par
`npm ci --ignore-scripts` dans `diagnostics/e0/edge/` sur le poste de l'opérateur, jamais en CI.

### 2.5 Workers de test (fenêtres bornées)

| Worker (identité) | Phase | Comportement | Garanties |
|---|---|---|---|
| `e0-tlsneg` | 1a (si G-3P) ou 1b (domaine E0 non proxifié) | `fetch()` d'une seule URL `E0_TLSNEG_URL` (https, hôte validé) ; renvoie le statut ou la classe d'erreur | aucune clé ; aucun autre `fetch()` |
| `e0-cross` | 2 | sur `workers.dev`, `fetch()` vers `https://<hôte public>/e0/guarded/<tid>` avec `CF-Connecting-IP`, `X-Real-IP` et `True-Client-IP` forgés (plages de documentation), puis variante P4 (IP-10b) | aucune clé, aucune dépendance cryptographique ; cible unique ; `tid` imprévisible ; recopie `x-e0-case` et `x-e0-run` |
| `e0-samezone` (plain, puis strict) | 2 | Route `atk.<zone>/*` ; `fetch()` simple avec `x-real-ip` posé (IP-11a) ; redéploiement avec `global_fetch_strictly_public`, `x-real-ip` posé puis non posé (IP-11b) ; contre le Custom Domain temporaire (IP-11d) | idem |
| `e0-binder` | 2 | service binding vers `e0-edge` ; en-têtes, URL et `cf` forgés (IP-11c) | idem ; seul gabarit autorisé à déclarer `services` |

Chaque Worker de test répond un document `attacker-result` : identité, `test_id`, `tid`, sous-variante, `attempted`,
**classe d'issue** (`invoked_marker`, `cloudflare_error`, `connect_refused`, `waf_blocked`, `timeout`), statut,
présence du marqueur `x-e0-edge`, SHA-256 du corps. Ce document est une **autodéclaration** du Worker de test : il
n'est jamais, à lui seul, une preuve (§ 4.5). Chaque Worker est supprimé à la fin de sa fenêtre et la suppression est
vérifiée par IP-15. Aucun Worker adversarial ne peut usurper l'identité `e0-edge` (§ 2.4).

### 2.6 e0-probe, témoin IP indépendant et cycle de vie de la comparaison

| Aspect | Spécification |
|---|---|
| Rôle | client lancé depuis le **poste de l'opérateur** (IPv4 et IPv6 natives) ; exécute les cas du catalogue pour une phase, un chemin et un manifeste. |
| Transports | `raw-h1` : socket et ssl, requêtes écrites octet par octet (doublons, obs-fold, non-ASCII, cibles brutes RT-13), **jamais de suivi de redirection**. `curl` : HTTP/2 et HTTP/3 si curl le permet ; arguments fermés, sans `-L` ni `--insecure` ; version et fonctionnalités consignées. Certificats relevés (émetteur, SAN, dates, SHA-256 de la feuille). |
| Ciblage | seuls les hôtes du manifeste de phase : hôte d'entrée, hôte d'origine (TO-C, RT-08, HTTP-04), points d'entrée historiques (IP-16a), Workers de test de la fenêtre courante, témoin TLS. Ports 443, et 80 pour les seuls cas `http`. Aucune IP littérale (sauf boucle locale avec `--local`, refusée sur Railway et en CI). Marqueurs de production refusés en premier (code 4). |
| Identifiant de tentative | `tid` **imprévisible et non réutilisé** (16 octets aléatoires) ; inclus dans la cible lorsque le cas l'exige (`/e0/guarded/<tid>`), donc lié cryptographiquement à l'attestation par le champ signé `t` (PA3). |
| Identification | avant tout envoi expérimental vers un couple (cible, famille) : `GET /e0/health` sans en-tête forgé ; marqueur attendu et même `boot_id` que la référence, sinon `aborted` sans autre envoi. **Exception IP-16a** : vers un point d'entrée censé être fermé, l'absence de marqueur est un **résultat** (injoignable), pas un motif d'abandon. |
| Corrélation | `x-e0-run` (aléatoire) et `x-e0-seq` (compteur) sur chaque requête, recopiés par les Workers de test, relevés par l'origine. |
| Sous-variantes adversariales | IP-10a, IP-11a, IP-11b, IP-11c et IP-11d sont exécutés **(i) sans engagement** et **(ii) avec un engagement valide auto-cohérent** de l'adresse observée par l'adversaire, de sorte qu'un refus ne puisse jamais être attribué à `cmp_*`. |
| Engagement d'adresse | `--self-ip4` et `--self-ip6` ne sont jamais émises en clair ; ces valeurs sont **déclaratives** : elles ne prouvent ni la provenance visiteur, ni l'adresse réellement empruntée. |
| Comparaison pré-signature | `x-e0-cmp-pre-key` et `x-e0-cmp-pre-mac`, clé `cmp_key_pre` **par `run_id`**, domaine `oryx-e0-ipcompare/pre/v1`, utilisés par le contrôle négatif du Worker (§ 2.3) ; retirés par PA13, jamais transmis à l'origine. |
| Comparaison post-signature (sans endpoint) | clé `cmp_key_post` **par run, indépendante des clés de signature**, domaine `oryx-e0-ipcompare/post/v1`, **provisionnée dans l'origine avant le run** (variable Railway E0) et **détenue par le témoin** (poste de l'opérateur, tmpfs). L'origine calcule `eng_post(ip_signée)` ; le témoin calcule `eng_post(ip_observée)` ; la **comparaison est faite hors ligne par l'analyseur** (§ 2.8). Aucun endpoint public n'est créé. |
| Garde | refus si `CI`, `GITHUB_ACTIONS` ou une variable Oryx est présente ; refus d'écrire dans l'arbre Git. |

**Témoin IP indépendant.** `e0_ipwitness.py` observe l'adresse publique de sortie de l'opérateur sur **la même
famille** que les requêtes expérimentales, par une **source autorisée et distincte de la décision d'`e0-edge`**
(`ipwitness_sources.v1.json` ; un service tiers exige G-3P). Transport HTTPS vérifié, hôte unique validé, aucune
redirection suivie. L'adresse n'est manipulée qu'en mémoire, le temps du calcul de `eng_post`, puis écartée ; elle
n'est **jamais** écrite. Le témoin produit uniquement des **observations brutes** (`ipwitness-record`, ci-dessous) ;
il ne calcule **aucune** conclusion de stabilité ou de corroboration.

**Granularité et attribution.** Le témoin observe par (`run_id`, famille, fenêtre-témoin), non par tentative.
Chaque fenêtre est encadrée par une observation `before` et une observation `after`. Pour IP-01 et IP-02, des
**observations rapprochées** (`phase = "proximal"`) sont en outre associées à chaque tentative ou au lot de cinq
tentatives, avec une **vérification du réseau utilisé** (`network_check`). L'analyseur, et lui seul, évalue ensuite :
- la **corroboration des extrémités** d'une fenêtre (`before.eng_post == after.eng_post` sous le même
  `cmp_key_post_id`) ; cette égalité est une **corroboration des extrémités**, **jamais une preuve de stabilité
  continue** : un changement d'adresse intermédiaire suivi d'un retour n'est pas exclu ;
- l'attribution d'un essai à une fenêtre (§ 2.8) et, pour IP-01 et IP-02, la concordance des observations
  rapprochées et du réseau vérifié.

Preuve insuffisante (fenêtre aux extrémités non corroborées, essai non plaçable, `cmp_key_post_id` divergent,
observation rapprochée ou vérification du réseau manquante ou discordante pour IP-01 et IP-02, source absente, non
autorisée ou non attribuable à la connexion testée) → **INCONCLUSIVE**.

**Schéma définitif `ipwitness-record`** (format `fmt = 1`). Un enregistrement par observation : chaque `before`,
chaque `after` et chaque observation rapprochée est un enregistrement distinct.

| Champ | Type | Présence | Description |
|---|---|---|---|
| `e0` | chaîne | obligatoire | valeur fixe `ipwitness-record` |
| `fmt` | entier | obligatoire | `1` |
| `protocol` | chaîne | obligatoire | `E0-V0.1c` |
| `bench` | chaîne | obligatoire | SHA Git du banc |
| `run_id` | chaîne | obligatoire | identifiant du run (lié à `boot_id` et `cmp_key_post_id` par le manifeste) |
| `famille` | entier | obligatoire | `4` ou `6` |
| `window_id` | chaîne | obligatoire | identifiant de la fenêtre-témoin |
| `phase` | chaîne | obligatoire | `before`, `after` ou `proximal` |
| `t_obs` | horodatage UTC (ms) | obligatoire | instant de l'observation |
| `test_id` | chaîne | obligatoire si `phase` vaut `proximal`, absent sinon | essai concerné |
| `tid` | chaîne | obligatoire si `phase` vaut `proximal`, absent sinon | tentative concernée |
| `eng_post` | chaîne (hex) | obligatoire | `HMAC(cmp_key_post, canonical(ip_observée))`, domaine `oryx-e0-ipcompare/post/v1` ; jamais l'adresse |
| `cmp_key_post_id` | chaîne | obligatoire | identifiant de la clé du run ; jamais la clé |
| `source` | objet | obligatoire | `source_id` (entrée de `ipwitness_sources.v1.json`), `interface`, `proxy_vpn_relay` (déclaré par l'opérateur) |
| `network_check` | objet | obligatoire si `phase` vaut `proximal`, absent sinon | interface, famille, absence de VPN, relais ou proxy sur le poste, consignées |

Règles du schéma :
- l'enregistrement brut ne contient **jamais** l'adresse en clair, la clé, ni un champ de stabilité, de corroboration
  ou de verdict : **aucun champ `stability`** n'existe dans `ipwitness-record` ;
- les bornes d'une fenêtre se déduisent des `t_obs` de son `before` et de son `after` ;
- la corroboration des extrémités, l'attribution et la concordance des observations rapprochées sont **calculées par
  l'analyseur** et consignées dans l'enregistrement dérivé `witness-window-assessment` (§ 5.3).

**Cycle de vie Railway et de la clé post-signature.**

1. **Staged ≠ actif.** Une modification de variable Railway (provisionnement ou rotation de `cmp_key_post`) peut
   rester `staged` jusqu'à son déploiement effectif ; un changement `staged` n'est ni actif ni une destruction.
2. **Garde d'activation avant tout nouveau run.** Un nouveau run ne commence qu'après vérification, consignée en
   `manual-evidence`, des quatre points suivants :
   - **(G1)** le déploiement qui porte la nouvelle valeur est **actif** (non `staged`) : identifiant du déploiement
     actif relevé côté Railway ;
   - **(G2)** **absence de trafic servi par l'ancien déploiement** : aucune requête d'essai routée vers l'ancien
     déploiement ;
   - **(G3)** **nouveau `boot_id`** observé dans la ligne `E0-ORIGIN` de démarrage ;
   - **(G4)** **correspondance** `cmp_key_post_id ↔ run_id ↔ boot_id ↔ active_deployment_id` consignée au manifeste.

   À défaut d'un seul point, le run ne démarre pas ; tout essai collecté sous une garde non satisfaite est
   **INCONCLUSIVE** (attribution de run non établie). Une rotation de `cmp_key_post` équivaut à un nouveau run ;
   aucune rotation n'est admise en cours de run ; AT-R04 consigne le redémarrage entre runs.
3. **Stockage temporaire protégé.** Les deux `eng_post` (origine et témoin) et les enregistrements bruts résident
   hors du dépôt, à accès contrôlé, jusqu'à la comparaison hors ligne.
4. **Destruction après analyse.** La suppression de `E0_CMP_KEY_POST` doit être **appliquée** (déployée, non
   `staged`) **et vérifiée côté Railway** (variable absente du déploiement actif du projet Railway E0 ; l'inventaire
   Cloudflare ne couvre pas cette variable). Une modification `staged` ne constitue **pas** une destruction et n'est
   jamais consignée comme telle. La preuve `manual-evidence` porte l'état `applied`, l'identifiant du déploiement
   actif postérieur à la suppression et l'horodatage. Le tmpfs de l'opérateur est effacé ; l'export de l'opérateur et
   les intermédiaires du rapport sont purgés ; le rapport ne conserve que `match`, `basis` et les références
   d'intégrité (SHA-256 des enregistrements sources, `cmp_key_post_id`, `boot_id`, `run_id`).
5. **Limites non vérifiables (réserves conservées).** La destruction vérifiable porte uniquement sur la variable du
   déploiement actif, le tmpfs de l'opérateur et les copies sous contrôle du banc. Elle **ne couvre pas** les journaux
   de plateforme Railway (sortie standard de l'origine, qui contient des `eng_post`), les sauvegardes internes du
   fournisseur, ni la mémoire hors du contrôle du banc. **Aucune destruction globale n'est promise.** Un HMAC sous une
   clé supprimée **n'est pas prouvablement irréversible** : la force brute sur l'espace d'adresses reste possible si
   la clé réapparaît depuis une copie résiduelle ; l'inertie des copies non contrôlées est donc **conditionnelle**,
   non absolue (U-CMP-POST).
6. **IP transitoire et IP persistée.** L'IP brute (signée côté origine, observée côté témoin) n'est manipulée que
   transitoirement en mémoire pendant le calcul de `eng_post`. **Aucune IP globale brute n'est jamais persistée**
   dans une preuve, un journal ou un rapport ; seuls persistent des pseudonymes par démarrage (origine) et des
   `eng_post` (HMAC).

### 2.7 e0-inventory (lecture seule)

| Aspect | Spécification |
|---|---|
| Rôle | instantané **en lecture seule** du compte Cloudflare E0 (§ 9.1 du protocole, IP-15), puis comparaison à l'attendu de la phase ou de la fenêtre. |
| Transport | bibliothèque standard (`urllib`) ; hôte unique `api.cloudflare.com` ; TLS vérifié ; **méthode GET seulement** (liste blanche dans le code, vérifiée par test AST) ; aucune redirection suivie. |
| Jeton | lu dans `E0_CF_INVENTORY_TOKEN` (jamais en argument), jamais écrit ni journalisé, `Authorization` exclu de toute erreur. La garde de `e0-inventory` autorise exactement ce nom de variable ; la liste noire générique (fragment `TOKEN`) s'applique à tout autre nom. |
| Éléments | scripts et gestionnaires (exportations du dernier envoi ; Cron Triggers ; consommateurs Queues ; Email Routing) ; **identité de chaque script** ; réglage `workers.dev` par script ; réglage des URL de version ; versions et alias ; Previews (réglage et existantes) ; intégrations Builds et Git ; déploiements actifs (versions, pourcentages) ; secrets Worker (**noms** seulement, dont `E0_SIGNING_KEY_*`) ; Routes de **toutes** les zones du compte ; Custom Domains ; service bindings ; Snippets ; projets Pages ; espaces Workers for Platforms ; règles WAF custom et Transform ; réglages de zone (SSL, normalisation, Pseudo IPv4, IPv6, Managed Transforms, Always Use HTTPS, règles de cache) ; membres (rôles, comptes pseudonymisés) et jetons (noms, statut, portée résumée). Hors verdict IP-15, pour la confidentialité : `observability`, `logpush`, `tail_consumers`. **`E0_CMP_KEY_POST` est une variable Railway, hors de cet inventaire.** |
| Statut par élément | `observed`, `api_unavailable`, `permission_denied` ou `manual` (capture du fondateur, référencée par SHA-256). Un élément requis non `observed` ni `manual` validé rend IP-15 **INCONCLUSIVE**. |
| Sorties | `inventory-snapshot.v1` (JSON canonique normalisé, IP globales rédigées dans les expressions de règles, adresses e-mail pseudonymisées) et `inventory-diff.v1` (`unexpected`, `missing`, `unverifiable`, verdict de cellule). |
| Points d'API | à vérifier lors de l'implémentation contre la documentation de l'API Cloudflare (U-API) ; un point non vérifié donne `api_unavailable`, jamais une hypothèse. |

### 2.8 e0-analyze

| Aspect | Spécification |
|---|---|
| Rôle | hors ligne, bibliothèque standard. Commandes `verify` (intégrité et origine des preuves), `analyze` (attribution, corroboration, verdicts à trois niveaux) et `render` (rapport Markdown daté selon le modèle). |
| Entrées | manifestes ; registre des points d'entrée ; JSONL de la sonde et empreintes ; export des journaux `E0-ORIGIN` (observations élémentaires et `eng_post`) ; `attacker-result` ; `ipwitness-record` ; instantanés et différences d'inventaire ; `deploy-evidence.v1` et SHA-256 ; preuves de phase L (artefacts CI : JUnit, `result.json`, `postgres.json` de chaque jambe) ; preuves manuelles (dont garde d'activation Railway et destruction des clés) ; **fichier de décision L-ACC** (`pending` par défaut, `accepted` ou `refused`, rempli par le fondateur, jamais par l'outil) ; **fichier des décisions requises par le § 11** (décision N2, classe I-c, règle P4, politique de fermeture des points d'entrée, révisions contractuelles : état `figée en F04-D` ou `non figée`, rempli par le fondateur). |
| Corroboration des extrémités (DF-1) | calculée **uniquement** par l'analyseur à partir des `ipwitness-record` bruts : pour chaque fenêtre, `endpoints = corroborated` si un `before` et un `after` existent sous le même `cmp_key_post_id` avec `eng_post` égaux, `not_corroborated` s'ils diffèrent, `incomplete` s'il en manque un. Le résultat est consigné dans `witness-window-assessment` et **n'est jamais présenté comme une stabilité continue démontrée**. |
| Comparaison IP post-signature (hors ligne) | jointure `(run_id, tid, test_id, famille)` entre l'`origin-record` (porteur de `tid`, `test_id` et `eng_post(ip_signée)`) et une fenêtre `endpoints = corroborated` de même `run_id`, même famille et même `cmp_key_post_id`, dont les bornes encadrent l'horodatage d'origine de l'essai ; pour IP-01 et IP-02, observation `proximal` concordante et `network_check` exigés. Résultat `match` consigné dans `ipcompare-result`. Tout défaut → INCONCLUSIVE. |
| Garde d'activation Railway | un essai n'est attribué à un run que si les preuves G1 à G4 (§ 2.6) sont présentes et concordantes ; sinon INCONCLUSIVE. |
| Production de `att_issued_evidence` | l'analyseur est **le seul** composant à produire la conclusion dérivée `att_issued_evidence` ∈ {`demonstrated`, `not_demonstrated`, `indeterminate`}, par preuves croisées : identité de tentative (`tid`), cible signée `t`, nonce et fenêtre temporelle, clé utilisée (courante ou historique), phase et point d'entrée, enregistrements d'origine, `attacker-result` et toute source indépendante disponible. Une attestation rejouée n'est jamais attribuée à une nouvelle tentative sur la seule présence d'un en-tête ou d'un identifiant déclaratif. |
| Niveau 1 | verdict des essais (§ 4) ; émission selon § 4.5 ; intégrité selon la règle B5 (§ 4.1). |
| Niveau 2 | `path_verdict_proposal` selon le § 11 du protocole : proposition technique, sans prétendre à l'audit. |
| Niveau 3 | `b2_global_verdict` selon § 5.5 (FZ-01) ; exige `independent_audit_status` sans réserve bloquante, L-ACC `accepted` et les décisions requises par le § 11 ; jamais simulé par l'analyseur. |
| Champs du rapport | `test_results`, `technical_eligibility`, `path_verdict_proposal`, `b2_global_proposal`, `independent_audit_status`, `founder_decision_status`, `lacc_state`, `b2_global_verdict`, `b2_decision_status`, `protocol_divergence`, `f04d_decisions_pending`, `entrypoints` (§ 5.4). |
| Interdits | aucun GO-CANDIDATE validé automatique ; aucune matrice PASS traitée comme un audit ; aucune qualification de F2 ni fermeture de P1 ; aucune décision reportée à F04-D convertie en FAIL ou en critère éliminatoire d'un essai E0. |

### 2.9 Banc N2 local (phase L) — anti-rejeu N2 PostgreSQL prioritaire

| Aspect | Spécification |
|---|---|
| Magasin | table `e0_n2.attestation_nonce(aud text, kid text, nonce_digest bytea CHECK (octet_length(nonce_digest) = 32), expires_at timestamptz, PRIMARY KEY (aud, kid, nonce_digest))`, **sans IP ni chemin**. `nonce_digest` = SHA-256 de `"oryx-e0-n2/v1\0" ‖ aud ‖ "\0" ‖ kid ‖ "\0" ‖ nonce`. `expires_at` = `ts + W + S`, calculé depuis l'attestation et non depuis l'heure du serveur. |
| Consommation | transaction courte en `READ COMMITTED` : `SET LOCAL statement_timeout`, puis `INSERT … ON CONFLICT DO NOTHING RETURNING 1`, puis COMMIT. Une ligne rendue → ACCEPT ; aucune → REPLAY. Base indisponible, délai d'acquisition du pool dépassé, `statement_timeout` ou erreur de connexion avant COMMIT → **REFUSE_UNAVAILABLE**. Exception pendant ou après l'envoi du COMMIT → **REFUSE_UNCERTAIN**. **Jamais de réinsertion ni de nouvelle tentative.** |
| Pool | taille paramétrable, **7 par défaut** (référence F04-D, non requalifiée ici) ; attente bornée (100 ms par défaut). |
| Purge | hors chemin de requête : `DELETE … WHERE expires_at < now() - marge` ; un test prouve qu'elle ne supprime aucune ligne dont l'attestation passerait encore le contrôle temporel. |
| Mandataire de fautes | relais TCP local entre client et PostgreSQL, lisant le protocole simple : coupe **après** avoir transmis `COMMIT` sans relayer la réponse (COMMIT incertain), coupe **avant** (rollback certain), refuse les connexions (base indisponible) ou retarde (délais). |
| Garde DSN | DSN reçu **par tube stdin**, jamais par argument ni variable ; hôte de boucle locale ou socket Unix ; base `^oryx_e0_n2_[a-z0-9]{8}_test$` ; commentaire sentinelle `oryx-e0-disposable` relu dans la connexion même qui exécute le DDL ; sous-processus en environnement purgé. |
| CI | base E0 créée par la fixture sur le serveur jetable de la jambe (`postgres` y est superutilisateur), après contrôle de `ORYX_TEST_DATABASE_URL` par `tests/pg_guard.py` ; supprimée en `finally`. **Aucune modification** de `ci/start_postgres.sh`. |

### 2.10 Tunnel (E0-1c, seulement si G-1c)

`tunnel/cloudflared.config.tmpl.yml` : ingress `https://` seulement, `originRequest.noTLSVerify: false`,
`originServerName` et `caPool` explicites. Le validateur refuse tout `http://` (TLS-T2 par construction) et tout
`noTLSVerify: true`. e0-origin se met en écoute TLS (§ 2.2) avec un certificat de banc généré par l'opérateur avec
`openssl`, propre à E0. Le jeton de tunnel et la clé privée du saut privé sont des secrets E0 : ils ne vont que dans
les variables du projet Railway E0 et du service `cloudflared` (image officielle épinglée par digest), jamais en Git
ni en journal. La future PR fournit le gabarit, son validateur, le mode TLS de l'origine et les cas de la sonde ;
l'exécution relève de G-1c.

## 3. Sécurité et isolement

### 3.1 Refus absolu des cibles de production

| Couche | Contrôle |
|---|---|
| Liste noire | `production_denylist.v1.json` : `oryx-api-production` (F04-A § 6.1) et tout hôte ajouté par le fondateur. Comparaison après passage en minuscules, retrait du point final et décodage punycode. Le suffixe usurpé (`….up.railway.app.attacker.example`) est refusé. |
| Liste blanche | sonde, inventaire, rendu Wrangler, Workers de test et témoin IP n'acceptent que les hôtes du manifeste de phase, eux-mêmes validés : origine `^e0-origin…\.up\.railway\.app$`, entrée `e0-edge.<sous-domaine>.workers.dev` (1a) ou `app.<zone E0>`. La zone E0 ne doit figurer dans aucune liste de production. |
| Identification | aucun envoi expérimental sans marqueur E0 et `boot_id` attendu (§ 2.6). |
| Origine | refus de démarrer hors d'un projet et d'un service nommés `oryx-e0*` et `e0-origin*` (§ 2.2). |
| Compte | le rendu et le déploiement exigent l'`account_id` E0 du manifeste ; l'inventaire refuse un compte dont les zones incluent un domaine de la liste noire. |
| CI | tous les outils à effet distant (sonde, inventaire, déploiement, keygen, témoin IP) refusent de s'exécuter si `CI` ou `GITHUB_ACTIONS` est défini ; en test, le réseau sortant non local est bloqué par une fixture (`socket.connect` refusé hors boucle locale). |

### 3.2 Configuration strictement limitée aux ressources E0

| Ressource | Règle |
|---|---|
| Cloudflare | **compte dédié E0**, une zone E0, scripts limités à {`e0-edge`, `e0-tlsneg`, `e0-cross`, `e0-samezone`, `e0-binder`} pendant leurs fenêtres ; aucun projet Pages, Snippet, espace Workers for Platforms ni intégration Git. |
| Railway | **projet `oryx-e0-*` dédié**, jamais rattaché à GitHub, aucune variable partagée ni référencée depuis un autre projet ; service `e0-origin` (plus `e0-tunnel` en 1c) ; une réplique ; veille désactivée ; restart NEVER. |
| Variables | uniquement les `E0_*` énumérées ; liste blanche exacte par outil (origine : § 2.2 ; inventaire : `E0_CF_INVENTORY_TOKEN`) et liste noire générique pour tous les outils. |

### 3.3 Interdiction des secrets et données Oryx

Le banc ne lit, n'importe et ne référence aucune variable `ORYX_*` (en particulier `ORYX_F2_*`), aucune base Oryx,
aucun secret fournisseur (Anthropic, EODHD, FMP, Supabase, Resend) et aucun module Oryx, y compris dans ses tests.
Ces interdits sont vérifiés par la garde à l'exécution et par des tests AST et texte (§ 6.2). Les seules données
manipulées sont synthétiques : cookies `__Host-e0-*`, corps déterministes, valeurs forgées des plages RFC 5737 et
RFC 3849.

### 3.4 Cryptographie Ed25519 : OpenSSL côté Python, WebCrypto côté Worker

L'encodage contractuel des attestations (§ 5.1) est développé dans le banc ; les **opérations cryptographiques**
Ed25519 ne reposent sur aucune implémentation maison. Côté Python, `e0_ed25519.py` est une **interface d'adaptation**
vers OpenSSL (outil `openssl` de l'image et de l'environnement de test, invoqué sans shell, arguments fermés) ;
côté Worker, WebCrypto natif. Les validations D1 à D9 sont **obligatoires et bloquantes** ; **aucun repli silencieux**.

| # | Vérification | Critère de succès | À défaut |
|---|---|---|---|
| D1 | présence et version d'OpenSSL dans la CI (`ubuntu-24.04`) **et** dans l'image E0 (`python:3.11.9-slim`) | version supportant Ed25519 (OpenSSL ≥ 1.1.1), relevée dans le manifeste | test en échec (jamais `skip`), refus fermé |
| D2 | capacité Ed25519 (`openssl list -public-key-algorithms` ou équivalent) | `ED25519` présent | refus fermé |
| D3 | vecteurs RFC 8032 § 7.1 | signature octet pour octet égale à l'attendu (Ed25519 déterministe) | FAIL du test |
| D4 | rejets canoniques à la vérification : `S ≥ L`, encodages non canoniques de `R` et `A`, longueurs erronées | rejet de chacun ; si l'outil ne contrôle pas `S` ou `R`, l'adaptateur recalcule les bornes et rejette **avant** l'appel | garde stricte de l'adaptateur obligatoire |
| D5 | rejet des messages altérés (un octet muté du message ou de `P`) | vérification en échec | FAIL du test |
| D6 | croisé OpenSSL ↔ WebCrypto dans les deux sens, sur les vecteurs partagés | succès dans les deux sens, signatures identiques | FAIL (AT-L01) |
| D7 | interopérabilité PKCS#8 : clé privée produite par OpenSSL chargée par WebCrypto (`importKey pkcs8`) | signatures identiques | FAIL |
| D8 | non-fuite : clé par stdin ou tmpfs, jamais en `argv` ; absence dans la liste des processus ; aucun fichier de clé persistant | aucun secret exposé | FAIL du test de confidentialité |
| D9 | comportement si OpenSSL est absent ou l'opération non supportée | refus fermé, aucun repli | une bibliothèque verrouillée isolée au banc ne peut être retenue que sur décision écrite du fondateur, jamais par basculement automatique |

La grammaire et les encodages de l'attestation sont validés **avant** toute vérification cryptographique. Aucune
nouvelle dépendance de production ni modification des verrous CI n'est admise.

### 3.5 Clés Ed25519 éphémères, audiences et deux ensembles de clés

| Étape | Spécification |
|---|---|
| Audiences | `oryx-e0-1a` (phase 1a) et `oryx-e0-2` (phases 1b à 2) ; préfixe de domaine de signature `oryx-e0-attestation/v1\n`, distinct de tout futur format F04-D. Le Worker refuse de signer si le nom du secret ne correspond pas à `E0_PHASE`, ou si les deux secrets coexistent. |
| Deux ensembles de clés publiques | (1) **clés de vérification de la phase courante** (`E0_VERIFY_KEYS`), seules autorisées pour une acceptation opérationnelle (`att_policy_accepted`) ; (2) **clés publiques historiques d'audit** (`E0_AUDIT_PUBKEYS`), clés retirées comprises, utilisables **uniquement** pour reconnaître une signature historique ou une émission interdite (`att_crypto_valid`), **jamais** pour accepter. Les anciennes clés ne retrouvent jamais un pouvoir d'acceptation par leur présence dans l'archive d'audit. |
| Génération | `e0_keygen.py generate --phase 1a` ou `--phase 2` sur le poste de l'opérateur, via `e0_ed25519` (OpenSSL) ; `kid = e0-<phase>-<16 hex du SHA-256 de la clé publique>`. La clé privée PKCS#8 est écrite **uniquement sur stdout**, **refusée si stdout est un terminal**, et tubée vers `npx wrangler secret put E0_SIGNING_KEY_<PHASE> -c <rendu>`. Seul l'enregistrement public (`kid`, `aud`, clé publique, empreinte, date) est écrit dans le dossier de preuves. |
| Distribution | clé publique courante vers `E0_VERIFY_KEYS` ; clé publique retirée vers `E0_AUDIT_PUBKEYS`. Un changement de variable Railway suit la garde d'activation (§ 2.6). |
| Clôture 1a (G-1a-CL) | `wrangler secret delete E0_SIGNING_KEY_1A` ; déploiement de `p1a-close` ; génération et pose de `E0_SIGNING_KEY_2` ; `E0_PHASE=1b` puis `2` côté origine ; clé publique `oryx-e0-1a` déplacée de `E0_VERIFY_KEYS` vers `E0_AUDIT_PUBKEYS` et listée dans `E0_RETIRED_KIDS` ; inventaire (le nom `E0_SIGNING_KEY_1A` a disparu) ; IP-15 ; IP-16a. **La destruction de la clé privée 1a est obligatoire et distincte de la conservation de sa clé publique d'audit.** Limite consignée : les versions anciennes stockées par Cloudflare peuvent conserver la liaison au secret ; c'est ce que neutralisent PA15 (vérificateur de phase) et `preview_urls=false`. L'option « supprimer et recréer le script » (question 16.6 du protocole) reste activable sans code par le paramètre `script_name` du manifeste, seulement si le fondateur la retient. |
| Indépendance | `cmp_key_pre` et `cmp_key_post` sont indépendantes des clés de signature. |
| `wrangler dev` | proscrit sous toutes ses formes (local et `--remote`) ; aucune clé n'existe hors du secret déployé ; `.dev.vars` interdit (garde et `.gitignore`). |

### 3.6 Pseudonymisation et comparaison des IP

- **Origine** : HMAC-SHA256 sous une clé aléatoire **par démarrage**, jamais exportée, préfixes `g4:` et `g6:`
  (16 caractères hexadécimaux) ; IPv4 mappée traitée sur son IPv4 ; rédaction du texte libre (logique P1-Q
  réimplémentée). Les pseudonymes par démarrage ne sont **jamais** comparés à des valeurs calculées sous une autre
  clé. Les adresses non globales et forgées restent en clair : elles sont l'objet de la mesure.
- **Pré-signature (Worker)** : clé `cmp_key_pre` par `run_id` ; calcul transitoire, jamais journalisé, transmis ni
  archivé ; contrôle de refus seul (§ 2.3).
- **Post-signature** : clé `cmp_key_post` par run, provisionnée dans l'origine et détenue par le témoin ;
  comparaison hors ligne par l'analyseur ; cycle de vie, limites et réserves selon § 2.6.
- **Worker** : il voit l'IP, ce qui est inévitable, mais ne la journalise jamais ; le diagnostic contient la classe,
  pas l'adresse, et n'est pas authentifié ; seule l'attestation porte l'IP, sur le saut TLS authentifié.
- **Sonde et témoin** : n'enregistrent jamais d'adresse en clair ; seuls des engagements et des booléens circulent.
- **Inventaire** : IP globales rédigées dans les expressions de règles.
- **Aucune IP globale brute n'est persistée** (I6).

### 3.7 Redirections, cache, transmissions non authentifiées et fail open

| Risque | Contrôles |
|---|---|
| Redirection suivie (D12) | `redirect: "manual"` (test sur l'appel `fetch` simulé) ; sonde sans suivi ; `/e0/redirect` vers une cible d'origine, de sorte qu'un suivi laisserait une seconde trace (RT-06). |
| Cache | `cache: "no-store"` côté Worker ; `Cache-Control: no-store` à l'origine ; aucune règle de cache dans la zone (inventaire) ; relevé de `cf-cache-status` (RT-07). |
| Clair | schéma `https://` codé en dur, hôte validé ; requêtes visiteur `http:` refusées localement ; compteur `X-Forwarded-Proto: http` à l'origine ; témoins TLS-X2 et TLS-X3 ; HTTP-01 à HTTP-05. |
| Fail open | aucun `passThroughOnException` (contrôles statique et d'exécution) ; 502 local sur exception de `fetch()` ; RT-12. |

### 3.8 Points d'entrée alternatifs du Worker : fermeture stricte et inventaire

| Point d'entrée | Fermeture | Contrôle préventif dans le banc | Détection et essai |
|---|---|---|---|
| Route `app.<zone>/*` (figée) | seule Route admise, phases 1b et 2 | schéma du rendu | IP-15 |
| `workers.dev` | `workers_dev=false` explicite hors 1a (D25) | rendu : clé obligatoire, `true` seulement dans `p1a` | IP-15, IP-16a, IP-16b, IP-16c |
| URL de version et alias | `preview_urls=false` explicite dès 1a (D26) ; aucun `versions upload --preview-alias` (sauf témoin) | rendu ; Wrangler épinglé | IP-15 (témoin d'alias), IP-16a |
| Previews et Workers Builds | aucune intégration Git ou Builds ; `wrangler preview` interdit | enveloppe (commande absente) | IP-15 |
| `wrangler dev --remote` | proscrit ; aucune clé hors secret (U-REMOTEDEV du protocole) | garde, `.dev.vars` interdit, PA14 | procédure, preuve manuelle |
| Versions anciennes (déploiement progressif) | une seule version à 100 % | enveloppe : `deploy` seulement | IP-15 (déploiements actifs) |
| Custom Domain | seulement pendant la fenêtre IP-11d (gabarit `p2-cd`) | rendu | IP-15, IP-16a (domaine retiré) |
| Routes d'autres zones | aucune | inventaire de toutes les zones | IP-15 |
| Service bindings et Workers for Platforms | aucun hors fenêtre `e0-binder` | rendu (`services` refusé hors binder) | IP-15 ; IP-11c (L-ACC) |
| Gestionnaires non HTTP et RPC | aucun : export par défaut `{fetch}` seul | test statique d'exportation | IP-15 (Cron, Queues, Email) |
| Usurpation d'identité de script | chaque gabarit cible l'identité attendue ; substitution refusée | rendu `e0_wrangler` et `worker_identities.v1.json` | IP-15 (identité par script), `test_e0_wrangler_config.py` |
| PA14 et PA15 | hôte d'entrée exact ; clé et audience de la phase | code Worker et vérificateur | IP-16a, AT-L02 |

Le **registre des points d'entrée** (`entrypoints.v1.jsonl`) est alimenté par la sortie de chaque déploiement (dont
l'identité de script), par l'inventaire (versions, alias, identités) et par saisie manuelle (Preview éventuelle). Il
est en ajout seul. Toute version présente dans un inventaire et absente du registre rend la liste **incomplète**,
donc INCONCLUSIVE.

### 3.9 F2 et F-02A : contrôle de dérive statique

- Aucun fichier de `core/`, de `api.py`, de `alembic/`, des documents F04-A, F04-B, F04-C ou F-02A, ni de
  `requirements*` n'est modifié ; aucun nom `ORYX_F2_*` ; les en-têtes `x-oryx-e0-*` et `x-e0-cmp-*` n'ont aucune
  signification pour F2.
- **Le banc n'importe jamais `core` et n'exécute jamais le runtime.** La fidélité F-02A est contrôlée en lecture
  seule par `e0_f02a_drift.py` :
  1. **verrou d'empreinte** `f2_registry.lock.v1.json` : SHA-256 de `core/route_policy/registry.py` (fichier figé)
     et instantané des 14 couples `F2_AUTH` et 13 couples `F2_PERSONAL` ; SHA-256 courant différent du verrou →
     **INCONCLUSIVE avec alerte**, jamais PASS ;
  2. **extraction AST strictement statique** : `ast.parse` du fichier lu comme données ; résolution uniquement des
     littéraux (`ast.literal_eval`) et des concaténations de constantes chaîne (`AUTH_NAMESPACE`,
     `PERSONAL_NAMESPACE` et suffixes) par un résolveur fermé ; reconstruction des couples ; **aucun `import`, aucun
     `exec`, aucune construction de `RoutePolicyGate`** ;
  3. **PASS** uniquement si le SHA-256 concorde **et** si l'extraction reconstruit exactement l'instantané (14 + 13,
     `EXPECTED_COUNTS` cohérent) ; structure non résoluble statiquement → **INCONCLUSIVE** (U-F02A) ; aucun repli ne
     déclare PASS sans preuve de correspondance suffisante ;
  4. la **fidélité runtime** reste possédée par les tests F-02A existants (`tests/test_route_policy*.py`, contrôle
     CI déjà requis), référencés par le banc et jamais ré-exécutés par lui.
- Les gabarits paramétrés du registre (`{position_id}`, `{thesis_id}`) sont instanciés, pour RT-13, par un segment
  canonique non vide fixé dans `f2_canonical_targets.v1.json`, conformément à F-02A § 5 (tout segment canonique non
  vide est accepté).

### 3.10 Réutilisation conceptuelle de P1-Q, sans dépendance

Le banc E0 réimplémente, sous des noms E0, sans importer ni lire `diagnostics/p1q` : garde d'environnement (liste
noire, renforcée par une liste blanche à l'origine) ; catégories IANA et pseudonymisation HMAC par démarrage (objet non
sérialisable) ; client HTTP/1.1 brut ; identification avant tout envoi ; JSONL à empreinte de fin, renforcé par un
chaînage ; analyseur « INCONCLUSIVE par défaut » ; tests AST d'imports, de pins et de dormance ; TTL ;
`restart NEVER` ; `railway up --path-as-root` ; résultats hors du dépôt. Un test vérifie l'absence de toute référence `p1q`
dans `diagnostics/e0` et dans `tests/test_e0_*`. **P1-Q V0.1-R1 reste intact** (critère de diff).

### 3.11 Neutralité vis-à-vis du frontend et compatibilité avec la future application mobile

| Point | Spécification |
|---|---|
| Portée de l'attestation | l'attestation est une **provenance de transport strictement interne** au saut Cloudflare → Railway (classe V de l'IP, hôtes, cible, fraîcheur, anti-rejeu). Elle **n'authentifie pas l'utilisateur**, n'est **pas** une preuve d'identité mobile ni un jeton applicatif. Elle est orthogonale à l'authentification F2 (CSRF `oryx-f2-csrf-v1`, cookies `__Host-`, OTP), qui reste figée et inchangée. |
| Indépendance du type de client | la sécurité du transport (chemin M-X-RT, V-ALL, attestation, N2) ne dépend d'aucun navigateur ni frontend. Le classificateur signe la provenance réseau d'un visiteur, quel que soit le client (navigateur, application iOS ou Android, autre). |
| Mesures navigateur = instruments | `/e0/page` et les essais RT-04 et RT-05 utilisent un navigateur uniquement comme **instrument de mesure** de la préservation, par le transport, d'en-têtes (`Origin`, `Referer`, `Sec-Fetch-*`, `Cookie`) que la politique same-origin F2 déjà figée (F04-A § 6.1) exige. E0 n'impose pas que F2 reste web-only et ne décide pas du modèle d'authentification mobile. |
| Accès web et mobile | les clients web et mobile devront pouvoir accéder aux mêmes services backend par des interfaces API appropriées ; E0 n'impose aucun choix de framework mobile et ne modifie aucun contrat d'authentification F2. |
| Report à F04-D | l'adéquation des en-têtes de provenance F2 aux clients natifs (qui n'émettent pas nécessairement `Sec-Fetch-*` ou `Referer` comme un navigateur) relève de F2 et F04-D, hors E0 : elle est consignée dans `f04d_decisions_pending`, sans effet éliminatoire sur un essai E0. |
| Garde | `test_e0_isolation.py` vérifie qu'aucun contrat F2 (noms `ORYX_F2_*`, cookies F2, routes F2) n'est référencé ni modifié ; les cookies `__Host-e0-*` sont synthétiques et sans lien avec F2. |

## 4. Catalogue de tests : matrice de traçabilité

### 4.1 Conventions, règle commune et règle d'intégrité

- **Classes** (protocole § 10) : SEC, FAISA, IR, INFO. **Lieu** : L = local et CI ; D = distant (Cloudflare,
  Railway, IPv6 native). Les essais D sont **NON EXÉCUTÉS** à la date de ce document.
- **Composants** : O = e0-origin, E = e0-edge, P = sonde, W = témoin IP, A = analyseur, Inv = inventaire, N2 = banc N2,
  TLSN = e0-tlsneg, X = e0-cross, S = e0-samezone, B = e0-binder, T = tunnel.
- Les critères PASS et FAIL sont **ceux du protocole**, reproduits sans changement. Lorsque le protocole ne donne
  qu'un résumé, PASS = condition du résumé vérifiée sur toutes les cellules requises, FAIL = violation observée.
- **Règle commune (RC)** → INCONCLUSIVE : cellule manquante, famille indisponible, préflight non conforme, adresse
  non engagée, comparaison IP non attribuable, liste de points d'entrée incomplète, collecte non intègre, origine de
  preuve insuffisante, configuration Wrangler non reconstructible ou inventaire incomplet, contrôle F-02A non
  résoluble, garde d'activation Railway non satisfaite.
- **Défaut** : sans preuve, INCONCLUSIVE, jamais PASS. Un essai INFO ne produit qu'un **relevé**, jamais un verdict.
- **Règle d'intégrité à trois branches (B5)**, appliquée à tout FAIL candidat :

| Cas | Verdict |
|---|---|
| FAIL démontré par des preuves suffisamment fiables (intégrité et origine des preuves qui fondent ce FAIL établies) | FAIL conservé, même si d'autres preuves manquent ou sont corrompues |
| FAIL seulement suggéré par des preuves dont l'intégrité ou l'origine est insuffisante | INCONCLUSIVE, avec alerte de sécurité explicite (`security_alert`) |
| Absence de preuve suffisante, sans violation démontrée | INCONCLUSIVE |

Aucune preuve corrompue ne peut, seule, fonder un FAIL démontré. Un INCONCLUSIVE n'autorise jamais GO-CANDIDATE. Le
rapport conserve la trace des anomalies d'intégrité et des FAIL indépendamment démontrés.

### 4.2 TLS

| Id | Cl. | Ph. | Composants / prérequis | Preuves | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|---|---|
| TLS-X1 | SEC | 1a | E, O, P ; G-1a, clé 1a | réponse via `workers.dev` portant `x-e0-edge` et `x-e0-origin`, `X-Forwarded-Proto=https` à l'origine ; certificat Railway relevé en direct | sous-requête validée vers `*.up.railway.app` | échec sur un certificat valide | réseau ; RC |
| TLS-X2 | SEC | 1a/1b | TLSN, P ; G-1a et G-3P, ou G-1b | contrôle positif : certificat du témoin invalide par connexion directe ; `attacker-result` d'e0-tlsneg | témoin invalide refusé | réponse malgré le certificat invalide | aucun témoin autorisé (M-X non GO) |
| TLS-X3 | SEC | 1b | E, O, P, Inv (TLSN) ; G-1b | pour Off, Flexible et Full : instantané du réglage SSL, réponses M-X, `X-Forwarded-Proto=https`, compteur en clair nul, témoin sous chaque mode (U-X3) | la sous-requête M-X reste en HTTPS validé | en clair ou non validée | mode manquant ; RC |
| TLS-RW1 | SEC | 1b | P, Inv ; G-1b, M-RW demandé | certificat présenté pour `app.<zone>` (SNI, connexion directe à la cible Railway) ; Full (strict) ; 200 via Cloudflare | certificat valide pour `app.<zone>` en Full (strict) | 526 ou certificat non conforme | émission > 24 h |
| TLS-RW2 | SEC | 1b+ | P, Inv ; G-1b | deux relevés de certificat, dates de renouvellement | renouvellement observé, en proxifié | 526 ou expiration | non observé (M-RW non GO) |
| TLS-R1 | SEC | 1b (facultatif) | P ; G-1b, décision d'exécuter | certificat | certificat valide | 526 (attendu) | RC ; non exécuté = « non applicable » seulement s'il est déclaré au manifeste |
| TLS-T1 | SEC | 1c | T, O (TLS), P ; G-1c | HTTPS privé validé ; témoin (mauvaise CA ou mauvais `originServerName`) | HTTPS privé validé ; témoin faux refusé | témoin accepté | RC |
| TLS-T2 | SEC | 1c | T (validateur) ; G-1c | validation de la configuration `cloudflared` déployée | PASS non défini par le protocole (U-T2) | saut privé en clair : FAIL par construction | U-T2 |

### 4.3 TO

| Id | Cl. | Ph. | Composants / prérequis | Preuves | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|---|---|
| TO-C | FAISA | 1a | O, P ; G-1a | GET direct Railway `/e0/slow` à 126, 270 et 285 s, ×3 ; statut, SHA-256 du corps, durées | 200 et corps complet partout | 524 ou 5xx reproductible : **bloque tous les chemins** | non reproductible ; RC |
| TO-X | FAISA | 1a | E, O, P ; G-1a | GET sur 14 paliers de 30 à 285 s ×3, concurrence ≤ 8 ; POST de 384 Kio à 120, 126, 270 et 285 s ×3 ; SHA-256 du corps | 200 et corps complet à chaque palier | 524 ou 5xx reproductible | non reproductible ; TO-C non conclusif ; RC |
| TO-RW | FAISA | 1b | O, P ; G-1b | idem, par M-RW | idem | idem | idem |
| TO-T | FAISA | 1c | T, O, P ; G-1c | idem, par M-T-TLS | idem | idem | idem |
| TO-H | FAISA | 1a et chaque chemin | E, O, P (curl) ; G-1a | cellules TO en HTTP/1.1 et HTTP/2 | idem, par version HTTP | idem | idem |
| TO-D | INFO | 1a | O, P ; G-1a | abandon par le client ; déconnexion relevée à l'origine | relevé | — | — |

Les 14 paliers (`to_levels.v1.json`) et la définition de « reproductible » sont des paramètres d'exécution à valider
(U-TOL, U-REPRO) ; ils ne modifient aucun critère.

### 4.4 HTTP

| Id | Cl. | Ph. | Composants / prérequis | Preuves | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|---|---|
| HTTP-01 | SEC | 1a (`workers.dev`), puis 2 | E, O, P ; G-1a, G-2 | requêtes `http://` (GET, POST) ; aucune trace dans le journal de l'origine pour `x-e0-run` | rien ne parvient à l'origine, aucune attestation | trace ou attestation | RC |
| HTTP-02 | SEC | 2 | E, O, P, navigateur ; G-2 | `http://…/e0/cookies` et envoi de Cookie en clair : aucun `Set-Cookie`, aucune trace à l'origine | aucun cookie `__Host-` envoyé ni posé en clair | posé ou transmis | RC |
| HTTP-03 | SEC | L | E (tests Node), rendu ; G-E0-I puis G-L | tests nominatifs : `http://`, IP littérale, port, chemin, hôte d'entrée ; le rendu refuse | refus de démarrer ou de transmettre | `fetch()` émis | test absent |
| HTTP-04 | INFO | 1a | O, P ; G-1a | GET et POST `http://` direct Railway | relevé (301, POST converti) | — | — |
| HTTP-05 | INFO (témoin) | 1b | O, P, Inv ; G-1b, M-RW | zone en Flexible | relevé | — | — |

### 4.5 IP — provenance (phase 2, sauf IP-15 et IP-16)

IPv6 native obligatoire pour IP-01 à IP-08 (sans elle, INCONCLUSIVE). Valeurs forgées : plages de documentation ou
non routables uniquement.

**Issues de provenance adversariale**, produites par l'analyseur seul à partir de preuves croisées indépendantes ;
`x-e0-edge-diag`, le marqueur `x-e0-edge` et `attacker-result` sont des indices non authentifiés qui ne suffisent
jamais seuls :

| Issue | Preuves requises | Verdict | Essais concernés |
|---|---|---|---|
| (A) Non-invocation démontrée | preuves **suffisamment indépendantes** de non-invocation : au moins une source non autodéclarative (classe d'issue relevée aussi côté plateforme, par exemple erreur Cloudflare 1019 ou blocage WAF via inventaire ou identifiant de rayon, **ou** propriété documentaire structurelle : Route sans origine non ciblable par `fetch()` simple, D11 et D21), croisée avec l'absence de toute trace d'origine pour le `tid` **dans les deux sous-variantes**. Une autodéclaration de l'attaquant et l'absence de journal **ne suffisent jamais** | PASS | **IP-11a et IP-11b seulement** |
| (B) Invocation sans émission démontrée | trace d'origine portant le `tid` et les attributs forgés, **sans en-tête d'attestation** (`att_present = false`), corrélée au `attacker-result` ; aucune attestation `att_crypto_valid` attribuable à la tentative | PASS | IP-10a (branche unique), IP-11a, IP-11b |
| (C) Émission interdite démontrée | une attestation `att_crypto_valid` (clé courante **ou** historique) attribuable à la tentative (`tid` lié dans `t`, nonce, fenêtre), dans l'une ou l'autre sous-variante ; soumise à la règle B5 | FAIL — **jamais converti en INCONCLUSIVE** | IP-10a, IP-11a, IP-11b, IP-11c, IP-11d, IP-16 |
| (D) Preuves insuffisantes | non-invocation ou joignabilité non établie indépendamment ; issue reposant seulement sur un indice non authentifié ; `tid` non confirmé ; sous-variantes divergentes sans résolution ; non-émission attribuable seulement à un refus `cmp_*` ; attribution indéterminée | INCONCLUSIVE, sauf FAIL déjà démontré | tous les essais adversariaux |

**Attestation présente mais non authentifiable** (`att_present = true`, `att_crypto_valid = false`) : elle ne
constitue **jamais** automatiquement un PASS. Sur un chemin passant par `e0-edge`, un en-tête d'attestation que PA13
aurait dû retirer est une **violation PA13 → FAIL** ; sur un accès direct hors edge (contexte RT-08), un en-tête forgé
est attendu et relève de RT-08 (FAIL seulement en cas d'acceptation) ; chemin ou attribution indéterminés →
INCONCLUSIVE. Une attestation cryptographiquement valide mais refusée par la politique de phase prouve une émission
interdite si son attribution à la tentative est établie : le refus sémantique ne crée jamais un faux PASS. Une
ancienne attestation valide rejouée pour une autre tentative ne crée jamais un faux FAIL propre à cette tentative.
L'absence d'attestation valide observée à l'origine n'est pas une preuve cryptographique absolue de non-émission :
elle ne vaut que combinée aux preuves de l'issue (A) ou (B).

| Id | Cl. | Composants / prérequis | Preuves | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|---|
| IP-01 | SEC | E, O, P, W ; G-2, IPv4 engagée | ×5 : classe V ; contrôle pré-signature passé ; attestation vérifiée ; comparaison post-signature attribuée via une fenêtre aux extrémités corroborées, une observation rapprochée concordante et un réseau vérifié | classe V ; unique, égale à l'adresse engagée ; signée | absente, multiple, différente ; nominal non signé (FAISA) | témoin, observation rapprochée, `network_check` ou comparaison absents, discordants ou non attribuables ; RC |
| IP-02 | SEC | idem ; IPv6 native et témoin IPv6 | ×5 en IPv6 | idem en IPv6 | différente ; traduction sans preuve côté client | sans IPv6 native, sans témoin ou sans comparaison attribuable IPv6 ; IPv6 traduite ou non native |
| IP-03 | SEC | E, O, P ; G-2 | `CF-Connecting-IP` forgé (IPv4 et IPv6) | nominal signé, valeur forgée absente | valeur forgée vue ou signée | RC |
| IP-04 | SEC | E, O, P (`raw-h1`) ; G-2 | doublon, liste, vide, obs-fold, non-ASCII | nominal unique signé, ou refus explicite de l'edge (400, 413, 431 non marqué) | forgé, multiple ou illisible signé | RC |
| IP-05 | SEC | idem ; G-2 | `True-Client-IP`, `X-Real-IP`, XFF, `Forwarded`, `CF-Connecting-IPv6`, `Cf-Pseudo-IPv4`, `CF-EW-Via`, `CF-Worker` forgés par le client | nominal signé ; un marqueur forgé par le client entraîne au plus un refus de signer (consigné) | valeur forgée signée | RC |
| IP-06 | SEC | idem ; G-2 | valeurs contradictoires | nominal signé | forgé signé | RC |
| IP-07 | SEC | E, O, P (curl) ; G-2 | HTTP/2 ; HTTP/3 si disponible (U-H3) | idem | idem | RC |
| IP-08 | SEC | E, O, P, W ; G-2 | deux sessions à au moins 1 h d'intervalle ; témoin et comparaison pour chaque session | idem | idem | écart < 1 h ; témoin ou comparaison manquants ; RC |
| IP-09 | SEC | E, O, P, Inv ; G-2 (Pseudo IPv4), IPv6 | Off, Add, Overwrite ; réglage relevé par instantané | classe E traitée en F, non signée | classe E signée ou acceptée | mode manquant |
| **IP-10a** | SEC | X, E, O ; fenêtre `e0-cross`, deux sous-variantes | `attacker-result` `attempted = true`, `tid` ; journal de l'origine ; classement A/B/C/D | **issue (B) uniquement** : aucune attestation ; origine atteinte sans en-tête d'attestation, refus 503. La non-invocation ne vaut **jamais** PASS pour IP-10a | issue (C) : attestation émise, quelle que soit l'IP ; violation PA13 (en-tête présent non authentifiable sur le chemin edge) | issue (D) ; origine non atteinte ; contrôle positif absent |
| IP-10b | SEC | X, Inv ; P4 activée (G-2) | règle WAF relevée ; aucune trace à l'origine | blocage Cloudflare avant `e0-edge` ; aucune attestation | requête signée | RC ; « non applicable » seulement si P4 n'est pas testée et que c'est déclaré (U-P4) |
| IP-10c | SEC (facultatif) | P, Inv ; P5 activée | marqueur client | marqueur supprimé ou écrasé pour le trafic visiteur ; nominal signé | marqueur client accepté | RC |
| IP-10d | IR | X, E ; fenêtre `e0-cross` | classe, marqueurs et `asn` relevés dans le diagnostic d'edge | relevées (présent) | — | absent : INCONCLUSIVE |
| IP-11a | SEC | S (plain), E, O ; fenêtre `e0-samezone`, deux sous-variantes | `attempted = true`, `tid` ; journal ; classement A/B/C/D | issue (A) ou (B) : `e0-edge` non invoqué, ou invoqué sans attestation | issue (C) : attestation émise | issue (D) |
| **IP-11b** | SEC | S (strict), E, O ; fenêtre strict, deux sous-variantes | `x-real-ip` posé, puis non posé | issue (A) ou (B) : `e0-edge` non invoqué, ou invoqué sans produire d'attestation | issue (C) : toute attestation émise pour cette sous-requête, quelle que soit l'IP qu'elle porte (une IP différente de la valeur forgée ne vaut jamais provenance visiteur, C9) | issue (D) |
| IP-11c | SEC (essai de limite) | B, E, O ; fenêtre `e0-binder` (G-2, sans acceptation préalable de L-ACC) | en-têtes, URL et `cf` forgés, `tid` | aucune attestation (non attendu) | attestation émise : FAIL par construction attendu, qui matérialise L-ACC ; **le FAIL reste enregistré même sous exception** | RC ; incidence L-ACC selon § 5.5 (A-06) |
| IP-11d | SEC | S, E (Custom Domain), O ; fenêtre CD | `fetch()` simple, `x-real-ip` posé | aucune attestation, refus fondé sur une détection garantie documentairement | attestation émise | refus fondé seulement sur D19 : INCONCLUSIVE au mieux (M-X-CD non GO) |
| IP-12 | SEC | E, O, P, Inv ; Managed Transform activée | instantané et cas nominal | refus de signer | IP absente ou vide signée | RC |
| IP-14 | INFO | E, O, P ; WARP, Private Relay | classe observée (I-c si `asn` = 13335, non signée en E0) ; limites de détection consignées (R5) | relevé | — | — |
| **IP-15** | SEC | Inv, A ; G-NET ou fondateur ; toutes phases | instantanés avant et après chaque fenêtre et chaque déploiement ; identité par script ; témoins négatifs (ajout temporaire d'`e0-samezone`, alias de version temporaire) | inventaire conforme avant et après chaque fenêtre et chaque déploiement ; témoins détectés | élément inattendu, suppression non vérifiée, témoin non détecté, substitution d'identité | élément non vérifiable ; instantané manquant ; RC |
| **IP-16a** | SEC | P, O, A, registre ; clôture 1a puis début de E0-2 | pour chaque point d'entrée du registre : GET et POST, IPv4 et IPv6 ; résultat injoignable, invoqué sans attestation ou émission ; clés d'audit pour reconnaître une attestation `oryx-e0-1a` | chaque point d'entrée injoignable ou invoqué sans attestation (PA14) ; aucune attestation `oryx-e0-1a` acceptée par le vérificateur de la phase 2 (PA15) | toute attestation obtenue par un point d'entrée non figé ; attestation `oryx-e0-1a` acceptée après la clôture | registre incomplet ; famille manquante |
| **IP-16b** | SEC | Wrangler, Inv, P ; après chaque redéploiement (1b, 2) | séquence vérifiée : déploiement, puis IP-15, puis IP-16a ; `deploy-evidence.v1` | réglages inchangés ; IP-16a en PASS ; configuration reconstructible | point d'entrée réactivé ou substitution d'identité démontrés ; IP-16a en FAIL | configuration non reconstructible, preuve manquante ou inventaire incomplet sans violation démontrée ; séquence incomplète |
| IP-16c | INFO (témoin) | Wrangler (`witness-16c`, Worker principal, clés omises), Inv ; fin de E0-2 | inventaire après déploiement, puis restauration et IP-16b | relevé | — | — |

### 4.6 RT — routage et origine (phase 2)

| Id | Cl. | Composants | Preuves | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|---|
| RT-01 | FAISA | E, O, P | Host vu par l'origine : M-X `e0-origin….up.railway.app`, M-RW `app.<zone>` | unique et canonique | différent, multiple ou non canonique | RC |
| RT-03 | FAISA | E, O, P | une seule trace d'origine par (`x-e0-run`, `x-e0-seq`) ; aucun 1019 | une seule requête d'origine, aucun 1019 | doublon ou 1019 | RC |
| RT-04 | SEC | E, O, P, navigateur (`/e0/page`, instrument) | Origin, Referer, `Sec-Fetch-*` et SHA-256 de Cookie identiques à l'envoi | intacts | altérés | preuve navigateur absente |
| RT-05 | SEC | E, O, P, navigateur | trois `Set-Cookie` `__Host-` distincts, octets identiques | intacts | fusionnés ou altérés | RC |
| RT-06 | SEC | E, O, P | 302 rendu ; aucune trace `redirected=1` ; attestation jamais retransmise | 302 rendu tel quel, jamais suivi | suivi ou attestation retransmise | RC |
| RT-07 | SEC | E, O, P, Inv | `cf-cache-status` différent de HIT à chaque répétition | aucun HIT de cache | HIT | RC |
| RT-08 | SEC | O, P | accès direct au préfixe gardé, avec et sans attestation forgée ou rejouée | aucune attestation valide ; refus 503 | acceptation | RC |
| RT-09 | FAISA | P, Inv | aucune réponse sans `x-e0-origin` attribuable à un défi ; aucun `cf-mitigated` | aucun défi ni réécriture sur l'API | défi ou réécriture | RC |
| RT-10 | SEC | E, O, P | SHA-256 du corps à 0, 4 Kio et 384 Kio | corps intact | corps altéré | RC |
| RT-11 | INFO | P, O | versions HTTP, `Content-Length` ou fragmentation | relevé | — | — |
| RT-12 | SEC | E (fenêtre `p2-fault`), O, P | faute provoquée : réponse Cloudflare sans `x-e0-edge`, aucune trace à l'origine ; contrôle statique sans `passThroughOnException` ; M-X-CD : erreur Cloudflare | aucune requête non signée acceptée | fail open observé | fenêtre non exécutée |
| RT-13 | SEC et FAISA | E, O, P (`raw-h1`) | `rt13_variants.v1.json` ; 27 couples de `f2_canonical_targets.v1.json` ; cible signée comparée à la cible brute reçue | SEC : jamais d'acceptation d'une cible différente de la cible signée ; FAISA : 27 couples F2 canoniques en PASS | acceptation d'une cible divergente (SEC) ; couple F2 en échec (FAISA) | RC |

### 4.7 AT — attestation et rejeu

| Id | Cl. | Ph. | Composants | Lieu | Preuves | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|---|---|---|
| AT-L01 | SEC | L | Python (`e0_ed25519` via OpenSSL, `e0_attest`) et JS (`e0_attest.js`) | L (postgres-16, postgres-18, web) | `attestation_v1.json` reproduit octet pour octet des deux côtés ; vecteurs RFC 8032 § 7.1 ; signatures croisées OpenSSL et WebCrypto (D6) | tous les tests nominatifs PASS dans toutes les jambes | un test en échec | test ou jambe absent |
| AT-L02 | SEC | L | idem | L | violations PA2 à PA7, PA14 (hôte d'entrée exact) et PA15 (`aud` ou `kid` d'une autre phase ; `kid` retiré reconnu par l'ensemble d'audit mais jamais accepté) | idem | idem | idem |
| AT-L03 | SEC | L | `e0_attest` | L | mutation de chaque octet et de chaque champ, troncatures, base64url non canonique | idem | idem | idem |
| AT-L04 | SEC | L | `e0_attest`, `e0_origin` | L | consommation du nonce après succès seulement (PA8) | idem | idem | idem |
| AT-L05 | SEC | L | `e0_origin` (magasin en mémoire) | L | concurrence dans un processus : un seul ACCEPT par nonce | idem | idem | idem |
| AT-L06 | INFO | L | `e0_n2_bench` (N1) | L | deux magasins N1 indépendants acceptent le même nonce | relevé (limite de N1) | — | — |
| AT-L07 | SEC | L | N2 | L (PG 16 et 18) | unicité en multiprocessus : 8 processus × 200 tours, même nonce sous barrière | exactement un ACCEPT, sur les deux versions | doublon accepté | une version absente |
| AT-L08 | SEC | L | N2 et mandataire | L (PG 16 et 18) | COMMIT incertain (coupure après COMMIT) et rollback certain (coupure avant) | REFUSE, aucune nouvelle tentative ; au plus un ACCEPT sur l'ensemble des présentations | double acceptation | une version absente |
| AT-L09 | SEC | L | N2 | L (PG 16 et 18) | pool saturé (7 connexions tenues), base arrêtée, `statement_timeout` | refus fermé en temps borné | acceptation ou attente non bornée | une version absente |
| AT-L10 | SEC | L | E (Node) | L (web) | PA12 (classes I et F jamais signées) et PA13 (en-têtes `x-oryx-*` et `x-e0-edge-*` retirés) | tests PASS | un test en échec | test absent |
| AT-R01 | IR | 1a, 2 | O, A | D | distribution de `réception à l'origine − ts` (minimum, médiane, p99) ; proposition de W et S pour F04-D | présent | — | absent : INCONCLUSIVE |
| AT-R02 | SEC | 2 | O, A | D | attestations des cas V | toutes valides et uniques | invalide ou doublon | RC |
| AT-R03 | INFO | 2 | O, A | D | chevauchements | relevé (ne qualifie pas N1) | — | — |
| AT-R04 | SEC | 2 | O, A | D | nonces vus plus d'une fois à l'origine ; une réplique et un `boot_id` par fenêtre (un redémarrage ouvre une nouvelle fenêtre, consigné) | jamais deux acceptations | double acceptation | plusieurs démarrages ou répliques dans une fenêtre |

Les essais GD-01 à GD-11 relèvent de F04-D, hors E0 : le catalogue les marque `out_of_scope`. Les identifiants IP-13
et RT-02 n'existent pas dans le protocole et ne sont pas créés.

### 4.8 Applicabilité par chemin

Règle : un essai d'applicabilité douteuse est traité comme applicable (son absence donne INCONCLUSIVE).

| Chemin | Essais propres | Essais communs |
|---|---|---|
| **M-X-RT (prioritaire)** | TLS-X1, TLS-X2, TLS-X3 ; TO-X ; IP-11a, IP-11b | TO-C, TO-H, HTTP-01 à HTTP-03, IP-01 à IP-10c, IP-11c, IP-12, IP-15, IP-16a, IP-16b, RT-01 à RT-13, AT-L01 à AT-L05, AT-L07 à AT-L10, AT-R02, AT-R04 ; IR : IP-10d, AT-R01 |
| M-X-CD | TLS-X1 à TLS-X3 ; TO-X ; IP-11d ; IP-11a et IP-11b (U-CD) | idem ; RT-12 (variante CD) |
| M-RW | TLS-RW1, TLS-RW2 ; TO-RW ; IP-11a, IP-11b | idem |
| M-T-TLS | TLS-T1, TLS-T2 ; TO-T ; IP-11a et IP-11b selon le Worker frontal | idem |

## 5. Formats versionnés

Règles communes : JSON canonique (`sort_keys`, séparateurs `,` et `:`, `ensure_ascii`) ; toute ligne JSONL porte
`{"e0": "<type>", "fmt": 1, "protocol": "E0-V0.1c", "bench": "<SHA Git>"}` ; empreinte = SHA-256 de `ligne + "\n"` ;
les preuves vivent hors du dépôt (`$HOME/e0-evidence/<campagne>/`) ; les outils refusent d'écrire dans l'arbre Git.

### 5.1 Attestation v1 (en-tête `x-oryx-e0-attestation`)

- **Valeur** : `v1.` ‖ base64url(P) ‖ `.` ‖ base64url(σ), avec σ de 64 octets (86 caractères). Total ≤ 4 096 octets.
  Base64url sans remplissage et **canonique** : le réencodage doit être identique.
- **P** : onze champs dans un ordre fixe, `v, aud, kid, ts, nonce, ip, c, m, h, hi, t`, chacun codé
  `<longueur décimale sans zéro initial>:<octets ASCII>;`. Aucun octet après le dernier champ.
- **Grammaires** : `v` = `1` ; `aud` ∈ {`oryx-e0-1a`, `oryx-e0-2`} ; `kid` conforme à `e0-(1a|2)-[0-9a-f]{16}` ;
  `ts` = 13 chiffres (millisecondes) ; `nonce` = 22 caractères base64url (16 octets) ; `ip` = forme canonique (IPv4
  décimale stricte, IPv6 RFC 5952) ; `c` = `V` ; `m` conforme à `[A-Z]{1,16}` ; `h` et `hi` = noms LDH en minuscules
  ≤ 253 caractères ; `t` = `/` suivi d'ASCII imprimable sans espace, ≤ 2 048 octets, incluant `/e0/guarded/<tid>`
  lorsque le cas l'exige.
- **Message signé** : `oryx-e0-attestation/v1\n` ‖ P.
- **Vérification Ed25519 stricte via OpenSSL** (D1 à D9) ; grammaire et encodages validés avant l'appel
  cryptographique.
- **Portée** : provenance de transport interne Cloudflare → Railway ; ni authentification utilisateur, ni identité
  (§ 3.11).

### 5.2 Manifeste d'exécution (`e0.run-manifest.v1`) et preuve de déploiement

| Groupe | Contenu |
|---|---|
| Identification | `run_id`, `campaign_id`, phase, chemin, fenêtre, opérateur (rôle seulement), date UTC |
| Code | SHA Git du banc, empreinte de l'arbre `diagnostics/e0`, SHA-256 du protocole, SHA-256 du verrou F-02A |
| Cloudflare | identifiant du compte E0, zone, scripts et fenêtres avec identités, version de Wrangler, SHA-256 du verrou npm, SHA-256 du fichier Wrangler rendu, `compatibility_date` et drapeaux, type de rattachement |
| Railway | identifiants du projet et du service E0, région, réplique, réglages (builder, restart, veille, healthcheck), **noms** des variables |
| Liaison de run (FZ-02) | `active_deployment_id`, `boot_id`, `cmp_key_post_id`, `run_id`, cohérents entre eux ; preuves G1 à G4 référencées |
| Clés | `kid`, `aud`, empreinte de la clé publique courante ; empreintes des clés publiques d'audit ; `cmp_key_post_id` ; jamais de clé privée |
| Paramètres | W et S provisoires, paliers TO, budgets, TTL, liste des essais `engagement_required` |
| Client | système, versions de Python, curl et OpenSSL, fonctionnalités HTTP/2 et HTTP/3, disponibilité IPv4 et IPv6 (booléens), source du témoin IP par famille, présence des engagements (booléens) |
| Déploiement | `deploy-evidence.v1` (§ 2.4) pour chaque déploiement |
| Déclarations | essais facultatifs non exécutés, avec motif et décision référencée |

### 5.3 Enregistrements

| Type | Producteur | Champs principaux |
|---|---|---|
| `probe-result` | sonde | `run_id`, `seq`, `test_id`, `sub_id`, `tid`, sous-variante, chemin, cible (libellé et hôte E0), famille, transport, tentative, statut, durées (connexion, premier octet, total en ms), en-têtes de réponse retenus (marqueurs, `cf-cache-status`, `cf-ray`, `location`, `set-cookie` compté et haché), SHA-256 du corps, observation, TLS (version, émetteur, SAN, dates, SHA-256 de la feuille), classe d'erreur, `engagement_required` |
| `probe-digest` | sonde | `run_id`, nombre de lignes, SHA-256 |
| `ipwitness-record` | témoin IP | schéma définitif du § 2.6 (observations brutes uniquement ; aucun champ de stabilité ni de verdict) |
| `origin-record` | e0-origin | `boot_id`, `seq`, `prev`, `x-e0-run`, `x-e0-seq`, `test_id`, `tid` reçu, méthode, cible (base64url), Host, `xfp`, observations élémentaires (`att_present`, `att_crypto_valid` avec `kid`, `aud` et origine `current` ou `audit`, `att_policy_accepted`, `t`, `nonce`, `ts` observés) **sans `att_issued_evidence`**, `eng_post` et `cmp_key_post_id`, `h`, classe et pseudonyme de l'IP, diagnostic d'edge (non authentifié, codes `cmp_*`), SHA-256 du corps, compteur en clair, réplique |
| `origin-checkpoint` | e0-origin | `boot_id`, `seq`, tête de chaîne, compteurs |
| `attacker-result` | Workers de test, via la sonde | identité, `test_id`, `tid`, sous-variante, `attempted`, classe d'issue, statut, marqueur `x-e0-edge`, SHA-256 du corps |
| `witness-window-assessment` | e0-analyze (seul) | `run_id`, famille, `window_id`, `cmp_key_post_id`, `endpoints` ∈ {`corroborated`, `not_corroborated`, `incomplete`}, références (SHA-256) des enregistrements `before`, `after` et `proximal` utilisés ; pour IP-01 et IP-02 : concordance des observations rapprochées et du `network_check`. **Jamais de qualification « stabilité continue ».** |
| `attestation-attribution` | e0-analyze (seul) | `test_id`, `tid`, issue de provenance (A, B, C ou D), `att_issued_evidence` ∈ {`demonstrated`, `not_demonstrated`, `indeterminate`}, preuves croisées retenues, `integrity_class` (règle B5), `security_alert` éventuelle |
| `ipcompare-result` | e0-analyze (hors ligne) | `run_id`, `tid`, `test_id`, famille, `match`, `basis`, références d'intégrité (SHA-256 des enregistrements sources, `cmp_key_post_id`, `boot_id`) ; jamais d'adresse ni d'engagement brut après purge |
| `entrypoint` | registre | date, type (`workers_dev`, `version_url`, `alias`, `custom_domain`, `route`, `preview`), identité de script, hôte, identifiant de version, source (déploiement, inventaire, manuel), statut |
| `inventory-snapshot` et `inventory-diff` | inventaire | § 2.7 |
| `deploy-event` | `e0_wrangler` | gabarit, identité, SHA-256 du rendu, version, sortie rédigée, horodatage (lié à `deploy-evidence.v1`) |
| `manual-evidence` | opérateur | type (garde d'activation G1 à G4, destruction de la clé privée 1a, suppression de `E0_CMP_KEY_POST` appliquée et vérifiée côté Railway avec état `applied`, capture, observation navigateur, décision), horodatage, SHA-256 des pièces, déclaration |
| `phase-l-index` | opérateur | URL du run CI, SHA du commit, jambes, SHA-256 des artefacts |

**Intégrité et origine des preuves.** Un chaînage SHA-256 démontre la cohérence interne d'une collecte, non
l'authenticité de sa source : un fichier modifié peut être entièrement rechaîné. Les contrôles incluent donc : (a)
empreintes finales calculées et conservées par le collecteur indépendant ; (b) références aux exécutions, démarrages,
versions et inventaires de plateforme ; (c) rapprochement entre preuves de la sonde, de l'origine et des
configurations effectives ; (d) absence de doublons, de ruptures ou de points de contrôle manquants ; (e)
conservation hors dépôt des preuves brutes sensibles, avec contrôle d'accès. Aucune donnée fournie par le client,
aucun marqueur HTTP ni aucun journal de composant ne constitue à lui seul une preuve irréfutable. Un journal réécrit
avec un nouveau chaînage mais une empreinte externe incohérente est rejeté ; une preuve sans origine suffisamment
établie ne permet pas PASS. Chaque FAIL candidat est classé par la règle B5 (§ 4.1). La conservation et la
destruction ne promettent que ce qui est vérifiable (§ 2.6).

### 5.4 Rapport (`e0.report.v1`)

| Champ | Contenu |
|---|---|
| `test_results` | niveau 1, par essai : `class`, `phase`, `verdict` (PASS, FAIL, INCONCLUSIVE, RELEVÉ, NON-APPLICABLE-DÉCLARÉ), `reasons`, `missing`, `cells`, `evidence` (SHA-256), `integrity_class`, `security_alert` ; pour les essais d'émission, référence à `attestation-attribution` |
| `technical_eligibility` | niveau 2 : conformité de la matrice, couverture, IPv6 native, preuves, inventaires, configurations effectives et identités, exceptions |
| `path_verdict_proposal` | niveau 2 : NO-GO, INCONCLUSIVE ou GO-CANDIDATE-PROPOSÉ par chemin, `blocking`, qualificatifs `provisional` et `reevaluable` le cas échéant (A-06) ; proposition technique seule |
| `b2_global_proposal` | niveau 2 : **proposition technique favorable ou non issue d'E0** (peut valoir « GO-CANDIDATE B2 PROPOSÉ ») ; ne vaut jamais qualification formelle (FZ-01) |
| `independent_audit_status` | externe : `absent`, `réserves` ou `sans réserve bloquante` ; jamais simulé |
| `founder_decision_status` | externe : état de chaque décision requise (L-ACC, décisions du § 11) ; jamais simulé |
| `lacc_state` | `pending`, `accepted` ou `refused` (recopie de la décision écrite du fondateur) |
| `b2_global_verdict` | niveau 3 : **verdict canonique uniquement** (`NO-GO`, `GO-CANDIDATE`, `INCONCLUSIVE`) ou `null`, selon § 5.5 ; jamais une valeur « PROPOSÉ » |
| `b2_decision_status` | état administratif, distinguant le blocage décisionnel de l'échec technique : `blocage_decisionnel_lacc_provisoire`, `blocage_decisionnel_lacc_refus`, `en_attente_audit`, `en_attente_decisions_section11`, `aucun_blocage_decisionnel` ; jamais un verdict |
| `protocol_divergence` | lectures contradictoires conservées sans réconciliation : `{"§5.4": "NO-GO B2", "§11": "INCONCLUSIVE"}` au niveau B2 global ; `PROTOCOL_AMBIGUITY_LACC` au niveau IP-11c |
| `f04d_decisions_pending` | décision N2, classe I-c, règle P4, politique de fermeture des points d'entrée, hôte interne, adéquation des en-têtes F2 aux clients natifs : listées comme travaux F04-D ; jamais présentées comme prises ; jamais converties en FAIL ou en critère éliminatoire d'un essai E0 |
| `entrypoints` | liste, résultat et identité par point d'entrée |
| `warning` | texte fixe : « Proposition mécanique non décisionnelle ; P1 reste ouverte ; une matrice complète ne vaut pas GO-CANDIDATE ; la validation finale ne se déduit pas de la sortie logicielle ; l'acceptation de L-ACC ne produit jamais automatiquement GO-CANDIDATE B2 ; le FAIL d'IP-11c reste enregistré même sous exception. » |

### 5.5 Règles de verdict (§ 11 du protocole, sans modification)

1. **Par chemin — NO-GO.** Au moins un essai SEC ou FAISA applicable en FAIL démontré, à la seule exception
   d'IP-11c (point 4), donne **NO-GO (P)** ; jamais masqué. IP-11b, IP-15, IP-16a et IP-16b ne bénéficient d'aucune
   exception. TO-C en FAIL donne NO-GO sur tous les chemins. Tout FAIL candidat passe d'abord la règle B5 (§ 4.1).
2. **Provenance adversariale.** Issues A, B, C et D du § 4.5 : IP-10a admet seulement l'issue (B) pour PASS ;
   IP-11a et IP-11b admettent (A) ou (B) ; (C) donne FAIL et n'est jamais converti en INCONCLUSIVE ; (D) donne
   INCONCLUSIVE. Jamais fondé sur un indice non authentifié seul.
3. **Par chemin — GO-CANDIDATE-PROPOSÉ (niveau 2).** Tous les essais SEC et FAISA applicables PASS, sauf IP-11c
   lorsqu'il est en FAIL et que l'acceptation écrite de L-ACC permet explicitement l'exception, sous réserve de toutes
   les autres conditions du protocole : TLS-X2 (M-X), TLS-RW2 (M-RW), IP-10a, IP-11a, IP-11b (critère C9), IP-15
   étendu, IP-16a et IP-16b après le dernier déploiement, IP-11d pour M-X-CD ; IPv6 native couverte, avec témoin et
   comparaison attribuables ; essais IR présents ; liste des points d'entrée complète ; configurations reconstructibles
   et identités correctes ; collecte intègre et origine des preuves établie ; contrôle F-02A concordant. **Le FAIL
   d'IP-11c reste enregistré ; seule son incidence éliminatoire peut être levée.**
4. **IP-11c × L-ACC (décision A-06).**

   | Résultat IP-11c | `lacc_state` | Traitement |
   |---|---|---|
   | FAIL démontré | `accepted` | exception conditionnelle du protocole ; FAIL de l'essai conservé ; vérifier toutes les autres conditions |
   | FAIL démontré | `pending` | **NO-GO du chemin, provisoire et réévaluable** (clause spécifique « IP-11c en FAIL sans acceptation de L-ACC → NO-GO (P) » du § 11) ; `PROTOCOL_AMBIGUITY_LACC` conservé |
   | FAIL démontré | `refused` | NO-GO |
   | PASS | `pending` | l'exception L-ACC n'est pas nécessaire pour cet essai ; vérifier les autres conditions |
   | INCONCLUSIVE ou non exécuté | `pending` | INCONCLUSIVE |

   Un FAIL d'IP-11c ne devient jamais PASS après acceptation de L-ACC.
5. **Global B2 — proposition favorable E0 et qualification formelle (FZ-01, décision A-05).**
   - **Proposition technique favorable** (`b2_global_proposal`) : au moins un chemin en GO-CANDIDATE-PROPOSÉ. Les
     décisions reportées à F04-D ne sont pas éliminatoires pour les essais E0 : leur absence ne dégrade aucun essai
     et n'est jamais transformée en FAIL expérimental.
   - **Qualification formelle GO-CANDIDATE B2** (`b2_global_verdict = GO-CANDIDATE`) : subordonnée à
     l'**intégralité** des conditions normatives du § 11 du protocole, toutes requises : (1) au moins un chemin en
     GO-CANDIDATE ; (2) audit indépendant sans réserve bloquante ; (3) L-ACC `accepted` (§ 5.4 du protocole) ; (4)
     révisions contractuelles, décision N2, classe I-c, règle P4 et politique de fermeture des points d'entrée **à
     figer en F04-D**, effectivement figées. Comme (4) relève de F04-D, postérieur à E0, **E0 ne déclare jamais, à
     lui seul, un GO-CANDIDATE B2 formel**.
   - **L-ACC non acceptée (A-05)** : `lacc_state = pending` → `b2_global_verdict = NO-GO` avec qualificatifs
     `provisional = true` et `reevaluable = true`, `b2_decision_status = blocage_decisionnel_lacc_provisoire` ;
     `protocol_divergence` conserve la lecture du § 11 (INCONCLUSIVE). Ce NO-GO est un **blocage décisionnel**, non
     un échec technique : `technical_eligibility` et `b2_global_proposal` continuent d'exposer l'état technique.
     `lacc_state = refused` → `b2_global_verdict = NO-GO`, `b2_decision_status = blocage_decisionnel_lacc_refus`.
   - **L-ACC acceptée, condition manquante** : audit indépendant absent ou avec réserve bloquante, ou décision du
     § 11 non figée → `b2_global_verdict = null` et `b2_decision_status = en_attente_audit` ou
     `en_attente_decisions_section11`. Jamais un FAIL expérimental ; jamais un GO-CANDIDATE B2 déclaré malgré une
     condition manquante.
   - **Échec technique** : tous les chemins tentés en NO-GO, les autres exclus ou refusés →
     `b2_global_verdict = NO-GO`.
   - **Sinon** → `b2_global_verdict = INCONCLUSIVE`.
   - **L'acceptation de L-ACC ne produit jamais automatiquement GO-CANDIDATE B2.**
6. **Invariants.** Un transport d'origine non authentifié ne conduit jamais à un GO-CANDIDATE ; une émission
   démontrée et attribuable, par un Worker appelant ou un point d'entrée non figé, est toujours un FAIL de sécurité ;
   les essais INFO n'entrent dans aucune condition de GO ni de NO-GO ; IP-15 et IP-16 sont des contrôles de
   détection, ni préventifs ni cryptographiques (L-ACC) ; l'analyseur ne simule jamais l'audit ni une décision ; la
   classe I-c n'est pas signée en E0 ; P4 reste optionnelle, sans activation implicite.
7. **Intégrité.** FAIL fondé sur des preuves fiables : conservé ; FAIL d'intégrité insuffisante : INCONCLUSIVE et
   `security_alert` ; absence de preuve : INCONCLUSIVE. Une preuve manquante ou une configuration non reconstructible
   seule donne INCONCLUSIVE, jamais un FAIL démontré, et interdit toujours GO-CANDIDATE.

## 6. Tests locaux et CI

### 6.1 Inventaire exact des tests prévus (DF-2)

| Fichier | Jambe CI | Couverture propre |
|---|---|---|
| `tests/e0_support.py` | — | support : serveur Uvicorn réel en sous-processus, certificats `openssl`, garde réseau, fixture de base N2 |
| `tests/test_e0_ed25519.py` | postgres-16, postgres-18 | adaptateur OpenSSL (D1 à D9) |
| `tests/test_e0_attestation.py` | idem | AT-L01 à AT-L04, bornes temporelles |
| `tests/test_e0_keysets.py` | idem | séparation des ensembles de clés |
| `tests/test_e0_ipclass.py` | idem | classification réseau, vecteurs partagés |
| `tests/test_e0_ipcompare.py` | idem | contrat de comparaison pré- et post-signature |
| `tests/test_e0_guard.py` | idem | refus de production, gardes d'environnement et de CI |
| `tests/test_e0_origin_http.py` | idem | e0-origin réel (routes, préfixe gardé, observations, confidentialité) |
| `tests/test_e0_probe.py` | idem | sonde (octets, identification, `tid`, sous-variantes) |
| `tests/test_e0_ipwitness.py` | idem | observations brutes du témoin et schéma `ipwitness-record` |
| `tests/test_e0_inventory.py` | idem | inventaire Cloudflare en lecture seule |
| `tests/test_e0_wrangler_config.py` | idem | gabarits, identités, rendu, enveloppe de déploiement |
| `tests/test_e0_keygen.py` | idem | génération éphémère des clés |
| `tests/test_e0_catalogue.py` | idem | catalogue et dérive vis-à-vis du protocole |
| `tests/test_e0_f02a_drift.py` | idem | SHA-256 du registre F-02A et extraction AST statique |
| `tests/test_e0_manifest.py` | idem | **couverture propre** : schéma des manifestes, liaison de run, `deploy-evidence.v1`, registre des points d'entrée, déclarations |
| `tests/test_e0_analysis.py` | idem | analyseur : attribution, corroboration, verdicts, intégrité, conservation |
| `tests/test_e0_n2_pg.py` | idem | AT-L06 à AT-L09 sur PostgreSQL 16 et 18 jetables |
| `tests/test_e0_isolation.py` | idem | isolement, dormance, pins, secrets, neutralité F2 |
| `tests/test_e0_corrections.py` | idem | non-régression nominative par correction |
| `tests/web/e0/e0_attest.test.mjs` | web | WebCrypto Ed25519, vecteurs croisés |
| `tests/web/e0/e0_ipclass.test.mjs` | web | classification JS, vecteurs partagés |
| `tests/web/e0/e0_ipcompare.test.mjs` | web | contrôle pré-signature côté Worker |
| `tests/web/e0/e0_edge.test.mjs` | web | comportement d'e0-edge |
| `tests/web/e0/e0_attackers.test.mjs` | web | Workers de test |
| `tests/web/e0/e0_static.test.mjs` | web | contrôles statiques du module d'entrée |

`test_e0_manifest.py` conserve une couverture propre (schéma et liaison de run) : il figure donc dans l'inventaire.
Aucun autre fichier de test n'est prévu. En particulier, aucun fichier `test_e0_railway_cleanup.py` n'est prévu : la
vérification réelle de la suppression des variables Railway est une opération distante autorisée (§ 2.6, annexe C),
et la logique locale d'acceptation des preuves correspondantes est couverte par `test_e0_analysis.py` et
`test_e0_manifest.py`.

### 6.2 Tests Python (jambes `postgres-16` et `postgres-18`, collecte automatique)

| Fichier | Couverture détaillée |
|---|---|
| `test_e0_ed25519.py` | D1 à D9 ; vecteurs RFC 8032 § 7.1 ; refus de S ≥ L, de R ou A non canoniques et de longueurs erronées ; déterminisme ; aller-retour PKCS#8 ; détection de capacité et refus fermé si OpenSSL est absent ou l'opération non supportée (jamais de repli) ; aucun fichier de clé persistant ; clé jamais en `argv` |
| `test_e0_attestation.py` | AT-L01 (fixture croisée) ; AT-L02 (PA2 à PA7, PA14 : hôte d'entrée exact, point final, port, `workers.dev` en phase 2 ; PA15 : `aud` ou `kid` de 1a refusés en phase 2 et inversement, `kid` retiré) ; AT-L03 (mutations) ; AT-L04 (ordre PA8) ; bornes temporelles (âge −S−1, −S, W, W+1) |
| `test_e0_keysets.py` | une clé retirée est reconnue par l'ensemble d'audit (`att_crypto_valid = true`) mais jamais acceptée par la politique de phase (`att_policy_accepted = false`) ; aucune clé historique ne retrouve un pouvoir d'acceptation ; séparation `E0_VERIFY_KEYS` et `E0_AUDIT_PUBKEYS` |
| `test_e0_ipclass.py` | `ipclass_v1.json` (V, I-a, I-b, I-c, I-d, F ; mappée, classe E, documentation, CGNAT, ULA, zéros, crochets, zone, liste, vide) ; classification réseau strictement identique avec ou sans engagement de la sonde ; égalité entre la table générée JS et le JSON |
| `test_e0_ipcompare.py` | `::detect_validate_compare` : absent → no-op (étape 2 non entrée) ; présent mal formé → `cmp_malformed` (refus) ; présent valide discordant → `cmp_disagreement` (F) ; présent valide concordant → aucun effet ; étape 2 jamais émettrice ; engagement forgé auto-cohérent → aucune promotion I/F vers V ; post-signature : domaine `…/post/v1`, divergence de `cmp_key_post_id` → INCONCLUSIVE ; aucune clé ni couple clé et empreinte persistés ; vecteurs `ipcompare_v1.json` |
| `test_e0_guard.py` | refus de production (casse, point final, suffixe usurpé, punycode) ; variables Oryx ; liste blanche de l'origine (environnement de l'image officielle accepté, nom inconnu refusé) ; exception exacte `E0_CF_INVENTORY_TOKEN` pour l'inventaire seulement ; refus en CI des outils distants ; refus d'écrire dans le dépôt |
| `test_e0_origin_http.py` | vrai serveur Uvicorn en sous-processus : routes ; `/e0/guarded/<tid>` routé au vérificateur gardé, jamais au catch-all ; 503 octet pour octet identique à F04-C ; `proxy_headers=False` avec `FORWARDED_ALLOW_IPS=*` ; aucune IP globale en clair ; Cookie haché ; aucun octet anticipé sur `/e0/slow` ; POST de 384 Kio ; chaînage du journal ; AT-L05 ; observations élémentaires sans `att_issued_evidence` ; `::eng_post_is_hmac_not_ip` et `::no_raw_ip_persisted` (IP traitée en mémoire puis écartée ; seuls pseudonyme et `eng_post` sortent ; `cmp_key_post` jamais journalisée) ; aucun endpoint de comparaison ; mode TLS 1c |
| `test_e0_probe.py` | requêtes brutes octet pour octet ; identification avant envoi ; `tid` imprévisible inclus dans `/e0/guarded/<tid>` ; deux sous-variantes adversariales ; aucun suivi de redirection ; engagement jamais en clair ; chaîne sonde → edge simulé (TLS local) → origine, rendue INCONCLUSIVE faute d'IPv6, de témoin et de preuves distantes |
| `test_e0_ipwitness.py` | `::schema_fields_unambiguous` (champs obligatoires et conditionnels ; `phase` ∈ {`before`, `after`, `proximal`} ; `proximal` exige `test_id`, `tid` et `network_check`) ; `::raw_record_has_no_stability` (aucun champ de stabilité, de corroboration ou de verdict dans l'enregistrement brut) ; `::no_address_no_key` ; `::no_tid_on_window_records` (`before` et `after` sans `tid`) ; hôte unique, GET, aucune redirection ; refus en CI ; absence de source autorisée → aucun enregistrement exploitable |
| `test_e0_inventory.py` | GET seulement (AST) ; hôte unique ; jeton jamais affiché ; normalisation et rédaction ; relevé des identités de script ; l'inventaire Cloudflare ne couvre pas `E0_CMP_KEY_POST` ; comparaison (inattendu, manquant, non vérifiable → INCONCLUSIVE) ; témoins négatifs détectés sur des réponses API simulées |
| `test_e0_wrangler_config.py` | chaque gabarit rendu relu par `tomllib` : identité égale à `worker_identities.v1.json` ; `workers_dev` et `preview_urls` explicites sauf `witness-16c` ; aucun `[env]` ; clés interdites absentes ; observabilité coupée ; aucun drapeau de compatibilité sur e0-edge ; `global_fetch_strictly_public` seulement dans `e0-samezone-strict` ; `services` seulement dans `e0-binder` ; refus de substitution d'identité ; exception IP-16c limitée à sa fenêtre et au Worker principal ; refus des valeurs de production ; refus de `deploy` en CI, sur arbre sale, HEAD différent, Wrangler non épinglé, `.dev.vars` présent ou identité incorrecte ; trois branches PASS, FAIL, INCONCLUSIVE ; reproductibilité du rendu |
| `test_e0_keygen.py` | refus si stdout est un terminal ; format du `kid` et de l'`aud` par phase ; aucune clé privée écrite sur disque (contrôle du répertoire temporaire) ; passage par l'adaptateur OpenSSL ; clé publique retirée versée à l'ensemble d'audit |
| `test_e0_catalogue.py` | catalogue égal à une liste écrite indépendamment dans le test et aux lignes du § 10 du protocole (identifiants, classes, phases) ; SHA-256 du protocole figé ; champ `engagement_required` cohérent ; `out_of_scope` pour GD-01 à GD-11 |
| `test_e0_f02a_drift.py` | SHA-256 de `registry.py` égal au verrou → poursuite ; SHA-256 divergent → INCONCLUSIVE avec alerte, jamais PASS ; extraction AST statique reconstruisant 14 + 13 couples égaux à l'instantané → PASS ; structure non résoluble → INCONCLUSIVE ; aucun `import core`, aucune exécution |
| `test_e0_manifest.py` | `::run_binding_fields` (`active_deployment_id`, `boot_id`, `cmp_key_post_id` et `run_id` obligatoires et cohérents ; incohérence rejetée) ; schéma du manifeste sans champ secret ; hôtes validés contre la liste noire ; champs obligatoires de `deploy-evidence.v1` ; registre des points d'entrée en ajout seul et détection d'incomplétude ; déclarations d'essais facultatifs ; validité de `phase_manifest.template.json` |
| `test_e0_analysis.py` | **issues de provenance** : `::ip10a_header_absence_required` (PASS seulement si `att_present = false`, origine atteinte et 503) ; `::ip10a_present_but_invalid` (chemin edge → FAIL PA13 ; hors edge → RT-08, pas de PASS IP-10a ; indéterminé → INCONCLUSIVE) ; `::valid_emission_never_inconclusive` ; `::ip11_two_branches` (IP-11a et IP-11b admettent A et B, IP-10a non) ; `::noninvocation_needs_independent_evidence` (autodéclaration et absence de journal seules → INCONCLUSIVE) ; **émission** : rejeu historique sans `tid` concordant → pas de faux FAIL ; en-tête forgé direct → pas de faux FAIL ; signature valide refusée mais attribuée → FAIL ; traces incomplètes → `indeterminate` ; **témoin** : `::endpoint_corroboration_computed_by_analyzer` (résultat dans `witness-window-assessment`, valeurs `corroborated`, `not_corroborated`, `incomplete`, jamais « stabilité continue ») ; `::ip01_ip02_proximal_attribution` ; fenêtre non corroborée, essai non plaçable ou `cmp_key_post_id` divergent → INCONCLUSIVE ; **Railway** : `::run_requires_active_deploy_gate` (G1 à G4 manquants ou incohérents → INCONCLUSIVE) ; `::staged_is_not_destruction` ; `::deletion_applied_and_verified` ; `::conservation_limits_documented` (pas d'inertie absolue ; limites de vérification et copies non contrôlées affichées) ; **verdicts** : table IP-11c × L-ACC complète dont FAIL et `pending` → NO-GO du chemin provisoire et réévaluable ; FAIL et `accepted` avec tous les autres essais PASS → exception levée, FAIL conservé ; `::b2_formal_requires_all_section11` ; `::b2_formal_go_only_when_complete` ; `::f04d_absence_never_fail` ; `::technical_proposal_independent_of_b2` ; `::lacc_pending_is_provisional_decisional_nogo` (A-05 : `b2_global_verdict = NO-GO` provisoire, `b2_decision_status` décisionnel, `protocol_divergence` conservée) ; `accepted` ne produit jamais automatiquement GO-CANDIDATE ; « PROPOSÉ » jamais dans `b2_global_verdict` ; **intégrité B5** : preuve falsifiée seule → INCONCLUSIVE avec alerte ; preuve falsifiée avec corroboration fiable fondant le FAIL → FAIL conservé ; chaîne incomplète ou empreinte externe incohérente → rejet ; absence de preuve → INCONCLUSIVE ; **divers** : TO-C en FAIL bloque tous les chemins ; M-RW sans TLS-RW2 jamais GO ; essais INFO sans effet ; refus d'écrire un rapport contenant une IP globale, un secret ou un couple clé et empreinte |
| `test_e0_n2_pg.py` | AT-L06 à AT-L09 sur la base jetable de la jambe ; version du serveur relevée (16 ou 18) ; purge ; garde DSN ; nettoyage en `finally` |
| `test_e0_isolation.py` | imports AST (bibliothèque standard, uvicorn, psycopg2 dans `e0_n2*` seulement, modules `e0_*`, `subprocess` borné à OpenSSL et wrangler) ; aucune référence `p1q`, `core`, `api`, `ORYX_F2_` dans `diagnostics/e0` ni dans `tests/test_e0_*` ; aucun contrat F2 référencé ni modifié (§ 3.11) ; pins du banc inclus dans le verrou de production, empreintes identiques ; Dockerfile minimal (`USER nobody`, `--require-hashes`, COPY explicite) ; aucun fichier ajouté à la racine ; le runtime (`api.py`, `core`, `scripts`, `alembic`, `ci`, `web*`) ne mentionne pas le banc ; le workflow ne mentionne ni `wrangler`, ni `cloudflared`, ni `CLOUDFLARE`, ni `railway up` ; balayage des secrets (blocs PEM, préfixe PKCS#8 Ed25519 `MC4CAQAwBQYDK2VwBCIEI`, motif de jeton de tunnel, affectations `*_TOKEN=` ou `*_KEY=` non vides, `.dev.vars`, couple clé de comparaison et empreinte ; graines de test RFC 8032 autorisées par empreinte exacte) |
| `test_e0_corrections.py` | un cas nominatif par correction (C1 à C9, R5 à R7, C-AUD-01 à C-AUD-07, B1 à B5, R3-F01 à R3-F04, P1 à P4, A-05, A-06, FZ-01, FZ-02, DF-1, DF-2), pointant vers le test détaillé correspondant |

### 6.3 Tests Worker (job `web`, `node --test`, sans dépendance)

| Fichier | Couverture |
|---|---|
| `e0_attest.test.mjs` | AT-L01 : la signature WebCrypto Ed25519 reproduit la fixture octet pour octet ; encodage de P identique ; signature croisée avec le vecteur OpenSSL ; HMAC d'engagement selon `commitment_v1.json` |
| `e0_ipclass.test.mjs` | `ipclass_v1.json` (mêmes vecteurs que Python) ; classification identique avec ou sans en-têtes d'engagement |
| `e0_ipcompare.test.mjs` | détection, validation et comparaison : absent → no-op ; mal formé → refus ; discordant → F avant signature ; concordant → aucun effet ; étape 2 jamais émettrice ; vecteurs `ipcompare_v1.json` identiques à Python ; clé et MAC jamais journalisés |
| `e0_edge.test.mjs` | PA13 (dont non-transmission de `x-e0-cmp-pre-*`) ; PA14 (I-d) ; PA15 (secret d'une autre phase présent → refus) ; I-b ; I-a exact ; I-c ; F (classe E, mappée, doublon, valeur de documentation) ; `::no_mask_on_absence` (classe V erronée et absence d'engagement → émission exposée) ; signature réservée à V ; HTTP-03 ; `http:` refusé sans `fetch()` ; options transmises à `fetch()` (`redirect: "manual"`, `cache: "no-store"`, URL `https://`) ; un seul `fetch()` ; aucun `fetch()` vers l'hôte d'entrée ; `tid` inclus dans `t` lorsque requis ; exception de `fetch()` → 502 local sans nouvelle tentative ; faute RT-12 inactive sauf `E0_ALLOW_FAULT="1"` ; `ctx.passThroughOnException` jamais appelé |
| `e0_attackers.test.mjs` | aucune importation de signature ni de secret ; cible unique `/e0/guarded/<tid>` ; `tid` imprévisible ; valeurs forgées dans les plages de documentation ; identité différente de `e0-edge` |
| `e0_static.test.mjs` | `e0_edge.js` : exportations = {default}, clés de `default` = [fetch] ; aucun `console.`, aucun `passThroughOnException`, aucune classe `WorkerEntrypoint` ou `DurableObject` dans les sources |

### 6.4 CI ou terrain réel

| Réalisable en CI (après G-E0-I, tests locaux) | Exige Cloudflare, Railway ou IPv6 native (feux verts de phase) — NON EXÉCUTÉ |
|---|---|
| tous les AT-L (AT-L07 à AT-L09 sur PostgreSQL 16 et 18) ; HTTP-03 ; logique de classification, de signature, de vérification, de rotation et de garde ; OpenSSL D1 à D9 et croisé ; séparation des ensembles de clés ; contrôle négatif pré-signature ; comparaison hors ligne ; identités de script ; dérive F-02A statique ; formats et manifestes ; analyseur (trois niveaux, A-05, A-06, FZ-01, FZ-02, B4, B5) ; isolement ; non-régression | TLS-*, TO-*, HTTP-01, HTTP-02, HTTP-04, HTTP-05, IP-01 à IP-16 (dont témoin IP et comparaison post-signature), RT-*, AT-R* ; vérification réelle de la garde d'activation Railway et de la suppression des variables Railway (opérations distantes) |

Les tests Node ne qualifient pas le runtime workerd : sa fidélité relève des phases distantes. En CI,
`synchronous_commit=off` et `fsync=off` : la sémantique de N2 est qualifiée, pas la durabilité ni la requalification
du pool (F04-D).

**Changements de CI proposés : aucun.** Les fichiers `tests/test_e0_*.py` sont collectés par les jambes PostgreSQL
existantes ; les fichiers `tests/web/e0/*.test.mjs` par `node --test` du job `web`. Aucune nouvelle dépendance, aucun
nouveau job, aucun secret ; les six contrôles stables et l'agrégateur restent inchangés. Contrainte :
`ci/check_test_report.py` refuse tout test `skip` ou `xfail` ; OpenSSL est une précondition d'environnement testée
(échec, jamais `skip`, s'il est absent).

## 7. Livraison et exploitation

### 7.1 Fichiers de la future PR d'implémentation

Ceux de l'inventaire exact du § 2.1 (101 fichiers), tous **nouveaux**. Aucun fichier existant n'est modifié ; le
`.gitignore` racine est inchangé.

### 7.2 Dépendances propres au banc

| Usage | Paquet | Règle |
|---|---|---|
| e0-origin | uvicorn 0.27.0, h11 0.16.0, click 8.5.0 | mêmes versions et empreintes que `requirements.txt` (test) |
| N2 | psycopg2-binary 2.9.13 | idem |
| Ed25519 côté Python | OpenSSL (outil de l'image et du système) via adaptateur | aucune dépendance pip ; D1 à D9 ; refus fermé si absent ; repli éventuel soumis au fondateur |
| Worker | aucune à l'exécution (WebCrypto) | — |
| Déploiement (opérateur) | wrangler, version exacte | `diagnostics/e0/edge/package-lock.json` ; `npm ci --ignore-scripts` ; jamais en CI |
| Sonde et témoin IP | curl système ; bibliothèque standard | versions consignées |

### 7.3 Plan de tests de la future PR

1. CI verte sur les six contrôles, au commit final, avec zéro test sauté.
2. Rejeu local par l'auditeur : `pytest tests/test_e0_*.py`, avec une base jetable via `ci/start_postgres.sh`
   (16 ou 18), puis `node --test` dans `tests/web`.
3. `e0_vectors.py --check` (RFC 8032, croisé OpenSSL et WebCrypto, ipclass, ipcompare) ;
   `e0_wrangler.py render --check-all` (manifeste fictif `*.example`, vérification des identités) ;
   `e0_f02a_drift.py --check` ;
   reproduction d'une `deploy-evidence.v1` d'exemple depuis le commit, le gabarit et le manifeste.
4. Analyseur sur des jeux synthétiques : rapports attendus versionnés comme fixtures de test, couvrant toutes les
   corrections listées dans `test_e0_corrections.py`.

### 7.4 Contrôles de sécurité de la future PR

- Diff limité à `diagnostics/e0/**`, `tests/test_e0_*.py`, `tests/e0_support.py`, `tests/web/e0/**` et aux trois
  documents `docs/act_l1_f04d_p1b_e0_bench_guide_v01.md`, `docs/act_l1_f04d_p1b_e0_formats_v01.md`,
  `docs/act_l1_f04d_p1b_e0_report_template_v01.md`.
- Balayage des secrets (test et revue) selon `test_e0_isolation.py`.
- Aucune référence à la production hors `production_denylist` et des tests de la garde.
- Aucune modification de `.github/`, `ci/`, `core/`, `api.py`, `requirements*`, `diagnostics/p1q/**`,
  `tests/test_p1q_*` ni du protocole.
- Analyse de secrets GitHub sur le diff au moment de la PR.

### 7.5 Après le merge de la future PR

- Le merge sur `main` déclenche le redéploiement Railway de **production** avec un code applicatif inchangé : seuls
  des fichiers inertes, jamais importés, sont ajoutés (précédent P1-Q).
- **Rien d'E0 n'est créé** : le projet Railway E0 n'existe pas et ne sera jamais rattaché au dépôt ; aucune
  intégration Cloudflare Git ou Builds ; aucun workflow.
- **Aucune autorisation n'est implicite dans une autre** : G-E0-I couvre l'autorisation de la PR du banc, son audit
  et son merge par le fondateur, et ne crée ni ne déploie aucune ressource E0.
- La vérification post-merge du fondateur se limite à l'état habituel de l'API et du worker de production.

### 7.6 Guide d'exécution phase par phase (à détailler dans le guide de la future PR — NE PAS EXÉCUTER)

| Étape | Feu vert | Gestes | Contrôles d'arrêt |
|---|---|---|---|
| E0-0 | G-L | désigner le run CI du commit mergé ; archiver JUnit, `result.json` et `postgres.json` des jambes postgres-16, postgres-18 et web ; `e0_analyze` sur la phase L | un AT-L ou HTTP-03 non PASS : correction du banc avant toute ressource |
| E0-1a | G-1a (+ G-3P pour un témoin tiers) | compte Cloudflare dédié et Workers Paid ; projet Railway `oryx-e0-<date>`, service `e0-origin` (`railway up diagnostics/e0/py --path-as-root`, réglages consignés, une réplique, restart NEVER) ; génération de la clé 1a tubée vers `secret put` ; provisionnement de `cmp_key_post` du run puis **garde d'activation G1 à G4** ; rendu et déploiement de `p1a` (identité `e0-edge` vérifiée) avec `deploy-evidence.v1` ; inventaire (IP-15) ; registre des points d'entrée ; témoin IP ; TLS-X1, TLS-X2, TO-C, TO-X, TO-H, HTTP-01 et HTTP-04 en `workers.dev`, AT-R01 | TO-C en FAIL bloque tout ; M-X en FAIL : NO-GO M-X, décision du fondateur avant tout domaine |
| Clôture 1a | G-1a-CL | `secret delete` de la clé 1A ; déploiement de `p1a-close` avec preuve ; génération de la clé 2 tubée ; clé publique 1a versée à l'ensemble d'audit, clé privée détruite ; `E0_PHASE` et `E0_RETIRED_KIDS` (garde d'activation) ; destruction de `cmp_key_post` du run 1a, appliquée et vérifiée côté Railway, après analyse ; inventaire (secret 1A absent) ; IP-15 ; IP-16a sur tout le registre (IPv4 et IPv6) ; preuves manuelles | IP-15 ou IP-16a en FAIL : **arrêt définitif** de la campagne |
| E0-1b | G-1b | zone et domaine E0 ; déploiement de `p1b` avec preuve ; IP-15 et IP-16b ; TLS-X3 (bascule et instantané à chaque mode) ; M-RW si demandé (TLS-RW1, TO-RW, TLS-RW2, HTTP-05) ; TLS-X2 si non fait en 1a | M-X et M-RW en NO-GO : décision du fondateur sur 1c |
| E0-1c | G-1c | service `cloudflared` (image épinglée par digest), origine TLS ; TLS-T1, TLS-T2, TO-T | NO-GO : NO-GO B2 |
| E0-2 | G-2 (+ D-L-ACC pour l'incidence d'IP-11c ; + G-3P pour une source témoin tierce) | IP-15 et IP-16a d'abord ; poste IPv4 et IPv6 natif ; `cmp_key_post` par run avec garde d'activation ; témoin IP, observations rapprochées et `network_check` pour IP-01 et IP-02 ; IP-01 à IP-09, IP-12, RT-01 à RT-13, HTTP-01 et HTTP-02, AT-R ; fenêtres bornées encadrées d'inventaires et suivies de suppressions vérifiées : `e0-cross` (IP-10a, IP-10b, IP-10d), `e0-samezone` plain puis strict (IP-11a, IP-11b, deux sous-variantes), `e0-binder` (IP-11c, autorisé par G-2 seul, sans acceptation préalable de L-ACC), Custom Domain (IP-11d), `p2-fault` (RT-12), témoins IP-15 ; navigateur ; WARP et Private Relay (IP-14) ; seconde session à au moins 1 h (IP-08) ; IP-16b après chaque redéploiement ; IP-16c à la fin | IP-10, IP-11 (sauf IP-11c), IP-15 ou IP-16 en FAIL : NO-GO du chemin |
| Analyse | — | `e0_analyze verify`, `analyze` (corroboration et comparaison hors ligne), `render` ; destruction des `cmp_key_post` appliquée et vérifiée côté Railway, purge des intermédiaires ; rapport daté pseudonymisé, versionné par une PR documentaire distincte | — |
| Nettoyage | G-N | suppression des Workers, de la zone ou de ses enregistrements, du projet Railway et des jetons ; inventaire final ; preuves conservées hors dépôt | inventaire non vide : nettoyage incomplet consigné |

**Séparation de G-2 et de D-L-ACC.** G-2 autorise l'exécution d'IP-11c avec `e0-binder`. D-L-ACC est une acceptation
ou un refus écrit du risque résiduel de la capacité privilégiée de déploiement ou de service binding. Le fondateur
peut autoriser IP-11c sans accepter L-ACC. L'acceptation de L-ACC ne se déduit ni de l'exécution du test, ni de la
création d'un compte dédié, ni de la réussite d'IP-15. `lacc_state` reste `pending` jusqu'à décision écrite
explicite.

### 7.7 Opérations soumises à autorisation explicite du fondateur

| Feu vert | Couvre | Ne couvre pas |
|---|---|---|
| G-E0-I | autorisation de la PR du banc E0, son audit et son merge | aucune création ni aucun déploiement de ressource E0 |
| G-L | désignation de la preuve de phase L | toute ressource distante |
| G-1a | compte Cloudflare dédié et abonnement ; `workers.dev` ouvert pour la seule phase 1a ; projet et service Railway ; clé 1a ; `cmp_key_post` du run 1a ; tout `wrangler deploy` de la phase | clôture 1a, domaine, phase 2 |
| G-3P | site TLS tiers ; source tierce du témoin IP si elle en est une | tout autre usage |
| G-1a-CL | clôture de la phase 1a (fermeture de `workers.dev` et des URL de version, destruction de la clé 1a, clé 2, IP-15, IP-16a) | phase 1b |
| G-1b | domaine ou zone de diagnostic ; M-RW si demandé | phase 2 |
| G-1c | Tunnel et M-T-TLS | phase 2 |
| G-2 | exécution d'IP-11c ; chaque fenêtre de Worker de test ; Custom Domain temporaire ; alias témoin ; bascules de zone (SSL, Pseudo IPv4, Managed Transforms, P4 optionnelle, P5) ; injection de faute ; IP-16c ; `cmp_key_post` par run de la phase 2 | acceptation de L-ACC |
| G-NET | accès de Claude Code à `api.cloudflare.com`, jeton en lecture seule (inventaire seulement ; déploiements toujours par le fondateur) | tout effet d'écriture |
| D-L-ACC | acceptation ou refus écrit du risque L-ACC | n'est jamais impliqué par G-2 ni par un autre feu vert |
| G-N | nettoyage, dont la suppression appliquée et vérifiée des variables Railway restantes | — |

Aucune de ces autorisations n'est implicite dans une autorisation antérieure. Aucun merge ne crée ni ne déploie une
ressource E0. Toute décision attendue par le protocole (normalisation, classe I-c, règle P4, question 16.6) non
consignée reste en attente dans le manifeste ; le banc applique l'option la plus conservatrice.

## 8. Critères d'acceptation de la future PR d'implémentation (G-E0-I)

1. Diff limité aux chemins du § 7.4 ; aucun fichier existant modifié ; P1-Q, F2, F-02A, la CI, les verrous et le
   protocole inchangés.
2. CI verte sur les six contrôles, zéro test sauté ; les tests E0 s'exécutent dans les trois jambes concernées ;
   liste des tests E0 attendus (§ 6.1) vérifiée par lecture de `junit.xml`.
3. Chaque identifiant du § 10 du protocole est dans le catalogue, avec un test ou un cas de sonde ; dérive testée ;
   empreinte du protocole vérifiée.
4. OpenSSL D1 à D9 obligatoires et bloquants ; signatures croisées OpenSSL et WebCrypto identiques octet pour octet ;
   refus fermé si OpenSSL est absent ; aucune clé privée réelle archivée ; aucune dépendance nouvelle dissimulée.
5. Deux ensembles de clés : clés historiques reconnues mais jamais acceptées ; destruction de la clé privée 1a
   distincte de la conservation de sa clé publique d'audit ; AT-L07 à AT-L09 PASS sur PostgreSQL 16.15 et 18.6.
6. Identités de script ; exception IP-16c limitée ; `deploy-evidence.v1` reconstructible ; verdicts Wrangler à trois
   branches.
7. Contrôle pré-signature en trois sous-étapes (no-op sur absence, refus sur engagement mal formé, F sur discordance,
   jamais d'émission, masquage impossible) ; issues A, B, C et D ; IP-10a limité à l'issue (B) ; non-invocation
   d'IP-11a et IP-11b exigeant des preuves indépendantes.
8. Préfixe gardé `/e0/guarded/<tid>` routé au vérificateur, jamais au catch-all.
9. Témoin IP : enregistrements bruts conformes au schéma définitif, sans champ de stabilité ; corroboration des
   extrémités calculée par l'analyseur ; IP-01 et IP-02 exigeant observations rapprochées et `network_check`.
10. Comparaison post-signature sans endpoint ; cycle de vie Railway avec garde d'activation G1 à G4 ; suppression
    appliquée et vérifiée côté Railway ; staged jamais accepté comme destruction ; aucune IP brute persistée ;
    réserves de conservation affichées.
11. Contrôle F-02A par SHA-256 et AST statique, sans import de `core` ; INCONCLUSIVE si preuve insuffisante.
12. Verdicts : table IP-11c × L-ACC (A-06) ; NO-GO B2 provisoire et réévaluable si L-ACC non acceptée (A-05) ;
    divergences conservées ; distinction entre proposition favorable et qualification formelle B2 (FZ-01) ;
    acceptation de L-ACC jamais suffisante seule.
13. Neutralité frontend (§ 3.11) ; aucun contrat F2 touché.
14. Tests d'isolement, de dormance, de pins et de balayage des secrets PASS ; `test_e0_corrections.py` complet.
15. Documentation de la future PR livrée (guide, formats, modèle de rapport, README), chacune marquée « NE PAS
    EXÉCUTER sans feu vert ».
16. Audit indépendant sans réserve bloquante, puis décision écrite du fondateur.

## 9. Découpage

Une seule PR d'implémentation isolée et auditable, avec commits par composant :
1. bibliothèque commune (adaptateur OpenSSL, ensembles de clés, contrat de comparaison, garde F-02A) ;
2. e0-origin (observations élémentaires, préfixe gardé, `eng_post`) ;
3. e0-edge et Workers de test (identités, contrôle négatif) ;
4. Wrangler et keygen (preuve de déploiement, vérification d'identité) ;
5. sonde et témoin IP ;
6. inventaire ;
7. N2 ;
8. analyseur, catalogue et manifestes (attribution, corroboration, verdicts, intégrité, non-régression) ;
9. documentation.

Aucun test n'est supprimé pour réduire la portée ; aucun composant n'est superflu au regard d'un essai du protocole.
Un découpage en plusieurs PR exige la décision du fondateur et n'autorise aucune expérience distante avant que
l'ensemble des composants et contrôles requis soit complet.

## 10. Contradictions et divergences avec le protocole figé

| # | Point | Statut |
|---|---|---|
| CT-1 | « Attestation émise » s'entend d'une attestation cryptographiquement valide d'une clé de campagne, attribuable à la tentative par l'analyseur, et non de la seule présence d'un en-tête ; l'issue (D) ne vise qu'une attestation valide non attribuable et ne convertit jamais un FAIL démontré | interprétation à confirmer par l'auditeur |
| CT-2 | Le témoin IP indépendant est une précondition d'intégrité (règle commune) dont l'absence donne INCONCLUSIVE ; il ne rend jamais PASS un cas que le protocole déclarerait FAIL | intégré |
| CT-3 | Le contrôle négatif pré-signature implémente le critère F « différente de l'adresse engagée lors des essais » (§ 6.4) de façon monotone (refus seulement, no-op sur absence) | résolu sous réserve de l'existence des tests correspondants (A-01) |
| CT-4 | IP-11c en FAIL avec L-ACC en attente : NO-GO du chemin provisoire et réévaluable (A-06), conformément à la clause spécifique du § 11 ; `PROTOCOL_AMBIGUITY_LACC` conservé | décidé ; divergence documentée |
| CT-5 | La clé éphémère pré-signature est transmise au Worker pendant l'essai, traitée transitoirement, jamais journalisée, transmise ni archivée | à confirmer (D-CMP-PRE) |
| CT-6 | Divergence textuelle entre le § 5.4 (« à défaut, NO-GO B2 ») et le § 11 (INCONCLUSIVE tant que L-ACC n'est pas arbitrée) : `b2_global_verdict = NO-GO` provisoire et réévaluable (A-05, lecture du § 5.4), lecture du § 11 conservée dans `protocol_divergence` | décidé ; divergence documentée |
| CT-7 | Le § 11 attache à GO-CANDIDATE B2 des décisions « à figer en F04-D » : elles conditionnent la qualification formelle (FZ-01) sans être des critères éliminatoires des essais E0 | intégré |
| ST-1 | Le fichier du protocole porte encore la mention « Statut : PROPOSITION » alors que G-E0-F a été validé et que le commit `d39be60` le désigne comme figé | incohérence de statut signalée ; protocole non modifié |

Aucune correction ne crée de critère éliminatoire nouveau ; la règle B5 assouplit un FAIL d'intégrité insuffisante
sans autoriser un GO sur preuve insuffisante ; la règle du § 5.5 point 3 explicite la ligne GO du § 11 sans la
modifier.

## 11. Incertitudes techniques

| Id | Incertitude | Défaut conservateur |
|---|---|---|
| U-ED | opérations Ed25519 Python via `openssl` : la vérification stricte (S < L, R et A canoniques) est à confirmer côté outil | l'adaptateur recalcule les bornes avant l'appel ; refus fermé si capacité absente ; repli soumis au fondateur |
| U-ED-CI | présence d'OpenSSL avec Ed25519 sur `ubuntu-24.04` et dans `python:3.11.9-slim` | précondition testée ; échec, jamais `skip` |
| U-IPW | source du témoin IP indépendant (service tiers autorisé ou second chemin contrôlé) ; stabilité et attribuabilité à démontrer | sans source autorisée et attribuable : INCONCLUSIVE |
| U-CMP-POST | force brute sur l'espace d'adresses si une copie résiduelle de `cmp_key_post` réapparaît ; inertie conditionnelle des `eng_post` résiduels | clé de courte durée, détruite dès la fin de l'analyse ; aucune IP brute persistée |
| U-RW-STAGED | délai et visibilité du passage d'une variable Railway de `staged` à actif | garde d'activation G1 à G4 ; aucun run sans preuve d'activation |
| U-TID | `tid` inclus dans la cible sans altérer RT-13 | `tid` dans un segment distinct (`/e0/guarded/<tid>`), hors des 27 couples canoniques |
| U-F02A | registre F-02A non résoluble statiquement après une évolution future | INCONCLUSIVE ; jamais PASS |
| U-TOL | 14 paliers TO non énumérés par le protocole | 30, 60, 90, 110, 120, 125, 126, 130, 150, 180, 210, 240, 270, 285 s (valeurs proposées, à confirmer) |
| U-REPRO | définition de « reproductible » (TO) | FAIL si le même palier échoue sur au moins 2 des 3 répétitions ; 1 sur 3 donne INCONCLUSIVE et une seule reprise ×3, toutes consignées |
| U-WS | W et S provisoires avant AT-R01 | W = 30 s, S = 5 s, sans effet sur les critères |
| U-T2 | TLS-T2 n'a pas de critère PASS | « règle de construction respectée » si la configuration est https seule et TLS-T1 PASS, sinon FAIL ; à défaut de clarification, M-T-TLS reste INCONCLUSIVE |
| U-X3 | preuve de « validé » pour chaque mode SSL de TLS-X3 | témoin e0-tlsneg rejoué sous chaque mode |
| U-CD | applicabilité d'IP-11a et IP-11b à M-X-CD, M-RW et M-T-TLS | applicables (conservateur) |
| U-H3 | IP-07 « HTTP/3 si disponible » | HTTP/2 obligatoire ; absence de HTTP/3 justifiée par les capacités de curl consignées, jamais silencieuse |
| U-CANON | IP valide mais non canonique dans `CF-Connecting-IP` | analysée strictement, signée sous forme canonique, canonicité consignée |
| U-API | points d'API Cloudflare (Builds, alias, secrets, Workers for Platforms, identités de script) | vérification à l'implémentation ; sinon `api_unavailable` ou preuve manuelle |
| U-CACHE | `cache: "no-store"` et `compatibility_date` | date ≥ 2024-11-11 ; vérification par RT-07 |
| U-NPM | `npm ci --ignore-scripts` suffisant pour `wrangler deploy` | préflight à G-1a ; sinon exception soumise au fondateur |
| U-WR | valeurs de zone et d'hôtes inconnues au merge | gabarits versionnés et rendu déterministe archivé (SHA-256) ; repli : PR documentaire figeant les valeurs avant 1b |

---

## Annexe A — Décisions architecturales retenues

| # | Décision | Référence |
|---|---|---|
| A1 | M-X-RT chemin prioritaire ; M-X-CD, M-RW et M-T-TLS conditionnels | § 2.3, § 4.8 ; A-E0-03 |
| A2 | Attestation = provenance de transport interne Cloudflare → Railway ; ni authentification utilisateur ni preuve d'identité ; orthogonale aux contrats F2 figés ; neutralité frontend (web et mobile iOS/Android) sans choix de framework | § 3.11 ; I8 |
| A3 | Ed25519 : OpenSSL côté Python, WebCrypto côté Worker ; D1 à D9 obligatoires et bloquants ; aucun repli silencieux | § 3.4 |
| A4 | Deux ensembles de clés (vérification de phase et audit historique) ; clé privée 1a détruite, clé publique d'audit conservée ; classe I-c non signée en E0 | § 3.5 ; R5 |
| A5 | Comparaison IP : clés éphémères par run, indépendantes des clés de signature ; contrôle pré-signature soustractif (détection, validation, comparaison ; no-op sur absence) ; comparaison post-signature hors ligne, sans endpoint public ; témoin IP indépendant et autorisé ; corroboration des extrémités calculée par l'analyseur, jamais stabilité continue ; preuve insuffisante → INCONCLUSIVE | § 2.3, § 2.6, § 2.8 |
| A6 | Cycle Railway : staged ≠ actif ; garde d'activation G1 à G4 avant chaque run ; suppression appliquée et vérifiée côté Railway ; suppression staged ≠ destruction ; conservation et destruction limitées au vérifiable ; aucune IP brute persistée | § 2.6 ; FZ-02, P2 |
| A7 | IP-10a : PASS seulement sur origine atteinte sans en-tête d'attestation et refus 503 ; IP-11a et IP-11b : deux branches PASS, la non-invocation exigeant des preuves indépendantes ; IP-11c : FAIL attendu enregistré, exception seulement sous L-ACC acceptée ; IP-15 et IP-16 sans exception | § 4.5 ; R3-F01, P3 |
| A8 | Fermeture stricte des points d'entrée et identités de script ; P4 optionnelle sans activation implicite | § 2.4, § 3.8 |
| A9 | Anti-rejeu N2 PostgreSQL prioritaire (16 et 18), refus fermé | § 2.9 |
| A10 | Contrôle F-02A statique par SHA-256 et AST, sans import de `core` | § 3.9 |
| A11 | Verdicts : A-05 (L-ACC non acceptée → NO-GO B2 provisoire et réévaluable, blocage décisionnel distinct de l'échec technique) ; A-06 (IP-11c en FAIL avec L-ACC en attente → NO-GO du chemin provisoire et réévaluable) ; divergences des §§ 5.4 et 11 conservées ; aucune acceptation automatique de L-ACC ; FAIL d'IP-11c enregistré même sous exception ; distinction entre proposition favorable E0 et qualification formelle B2 (FZ-01) | § 5.4, § 5.5 |
| A12 | Une seule PR d'implémentation isolée, commits par composant ; CI inchangée ; vérifications Railway réelles en opérations distantes autorisées, hors CI | § 6.4, § 7.5, § 9 |

## Annexe B — Critères de tests à vérifier après implémentation

**B.1 — En CI, après G-E0-I (tests locaux).** AT-L01 à AT-L10 ; AT-L07 à AT-L09 sur PostgreSQL 16.15 et 18.6 ;
OpenSSL D1 à D9 ; croisé OpenSSL et WebCrypto octet pour octet ; contrôle pré-signature en trois sous-étapes et
anti-masquage ; dérive F-02A (SHA-256 et AST) ; identités de script, exception IP-16c et verdicts Wrangler ; schéma
`ipwitness-record` sans champ de stabilité ; corroboration des extrémités par l'analyseur ; liaison de run et garde
d'activation (logique d'acceptation des preuves) ; staged refusé comme destruction ; issues A, B, C et D ; IP-10a
limité à l'issue (B) ; verdicts A-05, A-06 et FZ-01 ; règle B5 ; isolement, dormance, pins, secrets et neutralité F2 ;
`test_e0_corrections.py`.

**B.2 — Après feu vert de phase (distant, NON EXÉCUTÉ à ce jour).** TLS-X1 à TLS-T2 (G-1a, G-1b, G-1c) ; TO-C, TO-X,
TO-RW, TO-T, TO-H (G-1a, G-1b, G-1c) ; HTTP-01, HTTP-02, HTTP-04, HTTP-05 ; IP-01 à IP-14, RT-01 à RT-13 et AT-R01 à
AT-R04 (G-2, poste IPv4 et IPv6 natif, témoin IP autorisé) ; fenêtres `e0-cross`, `e0-samezone` et `e0-binder` (IP-10a,
IP-10b, IP-10d, IP-11a à IP-11d, deux sous-variantes) ; IP-15, IP-16a, IP-16b, IP-16c ; garde d'activation Railway G1
à G4 et suppression appliquée et vérifiée de `E0_CMP_KEY_POST`.

**B.3 — Preuves requises pour toute proposition GO-CANDIDATE-PROPOSÉ (niveau 2).** Collecte intègre et origine
établie ; comparaison IP attribuée via une fenêtre aux extrémités corroborées et, pour IP-01 et IP-02, observations
rapprochées et réseau vérifié ; garde d'activation satisfaite pour chaque run ; registre des points d'entrée complet ;
configurations reconstructibles et identités correctes ; contrôle F-02A concordant ; aucune preuve corrompue fondant
un FAIL. Une telle proposition ne vaut jamais qualification formelle B2 (§ 5.5 point 5).

## Annexe C — Risques et décisions nécessitant une autorisation explicite du fondateur

| Id | Risque ou décision | Nature | Décideur |
|---|---|---|---|
| D-L-ACC | acceptation ou refus écrit du risque L-ACC (capacité privilégiée ; IP-15 et IP-16 = détection, ni prévention ni preuve cryptographique) ; jamais implicite ; FAIL d'IP-11c conservé même sous exception | décision | fondateur |
| A-05 et A-06 | NO-GO B2 et NO-GO du chemin provisoires et réévaluables ; divergences textuelles des §§ 5.4 et 11 conservées et documentées, à réévaluer avec D-L-ACC | décisions prises, réévaluables | fondateur |
| A-07 (U-OPENSSL) | confirmation d'OpenSSL après D1 à D9, ou bibliothèque verrouillée isolée au banc | décision | fondateur |
| A-02 | provisionnement et destruction de `cmp_key_post` (variable Railway par run) ; persistance réduite au booléen ; limite U-CMP-POST | risque résiduel accepté ou non | fondateur |
| A-03 | source IP indépendante du témoin (tiers sous G-3P, ou second chemin contrôlé) et sa méthode | décision et autorisation | fondateur |
| D-16.6 | clôture 1a : PA15 seul, ou suppression et recréation du script | décision | fondateur |
| D-P4 | règle P4 optionnelle ; si non testée, « non applicable déclaré » | décision | fondateur |
| D-IC | classe I-c (WARP, Private Relay) : non signée en E0 ; traitement définitif en F04-D | reportée à F04-D | fondateur |
| Décisions du § 11 à figer en F04-D | révisions contractuelles, décision N2, classe I-c, règle P4, politique de fermeture des points d'entrée, hôte interne : non éliminatoires pour les essais E0, requises pour la qualification formelle B2 | reportées à F04-D | fondateur |
| ST-1 | incohérence de statut du fichier du protocole (« PROPOSITION ») à régulariser sans modifier ses critères | décision documentaire | fondateur |
| Feux verts d'exécution | G-E0-I, G-L, G-1a, G-3P, G-1a-CL, G-1b, G-1c, G-2, G-NET, G-N ; chacun distinct, aucun implicite | autorisations | fondateur |
| A-01, A-04, CT-1, CT-5 | confirmations d'audit : levée de CT-3 après tests ; U-F02A ; interprétation de « attestation émise » ; canal pré-signature | confirmations | auditeur |

Risques techniques résiduels consignés (non des validations) : voir § 11. Aucun élément expérimental distant n'est
validé. **P1 reste ouverte ; B2 non qualifiée ; F2 dormant.**

## Annexe D — Traçabilité des corrections de la révision R3

| Correction | Objet | Sections |
|---|---|---|
| R3-F01 | IP-10a : pas de PASS sur la seule non-invocation ; deux branches réservées à IP-11a et IP-11b | § 4.5, § 5.5 point 2, § 6.2 |
| R3-F02 | engagement pré-signature : détection, validation, comparaison ; no-op sur absence | § 2.3, § 6.2, § 6.3 |
| R3-F03 | témoin IP : granularité par fenêtre, attribution, INCONCLUSIVE si non liable | § 2.6, § 2.8, § 5.3 |
| R3-F04 | conservation : copies, rétention, suppression réellement vérifiable | § 2.6, § 5.3 |
| P1 | extrémités corroborées ≠ stabilité continue ; IP-01 et IP-02 : observations rapprochées et réseau vérifié | § 2.6, § 2.8, § 4.5 |
| P2 | pas d'inertie absolue ; limites de vérification ; suppression vérifiée côté Railway ; IP transitoire ≠ IP persistée | § 2.6, § 2.7, § 3.6 |
| P3 | IP-10a : en-tête absent requis ; attestation présente non authentifiable qualifiée explicitement ; émission valide jamais INCONCLUSIVE | § 4.5 |
| P4 | prérequis E0 et décisions reportées à F04-D distingués, sans critère éliminatoire nouveau | § 5.4, § 5.5 |
| A-05 | L-ACC non acceptée → NO-GO B2 provisoire et réévaluable, blocage décisionnel | § 5.4, § 5.5 point 5, § 10 CT-6 |
| A-06 | IP-11c en FAIL avec L-ACC en attente → NO-GO du chemin provisoire et réévaluable | § 5.5 point 4, § 10 CT-4 |
| FZ-01 | proposition favorable E0 ≠ qualification formelle GO-CANDIDATE B2 subordonnée au § 11 | § 1.2, § 2.8, § 5.4, § 5.5 point 5, § 10 CT-7 |
| FZ-02 | cycle Railway : staged ≠ actif ; garde d'activation G1 à G4 ; suppression appliquée et vérifiée | § 2.6, § 5.2, § 5.3, § 7.6 |
| Présentation | schéma définitif `ipwitness-record`, champ par champ | § 2.6 |
| DF-1 | corroboration des extrémités calculée par l'analyseur ; aucun champ de stabilité dans l'enregistrement brut | § 2.6, § 2.8, § 5.3, § 6.2 |
| DF-2 | `test_e0_manifest.py` inscrit à l'inventaire exact avec sa couverture propre ; aucun `test_e0_railway_cleanup.py` | § 2.1, § 6.1, § 6.2 |
