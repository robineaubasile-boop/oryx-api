# ACT-L1 / F04-A V0.3 — Contrat architectural de composition HTTP F2 (documentaire, dormant)

## 1. Référentiel et statut

| Élément | Valeur |
|---|---|
| Chantier | Activation F2 Lot 1 (ACT-L1), lot **F04-A** |
| Version | **V0.3** |
| Statut | **Freeze partiel validé par le fondateur, limité à la documentation F04-A** |
| Freeze complet F-04 | **Non autorisé** (F04-B à F04-E restent à concevoir, auditer et figer) |
| Activation F2 | **Aucune** |
| Source de vérité | GitHub, `robineaubasile-boop/oryx-api` |
| Référence auditée | `7224fe7d2f9c6fc3695d8e697bd30fee49a97e76` (merge de la PR #238, F-02A V0.2) |
| `main` relu avant rédaction | `7224fe7d2f9c6fc3695d8e697bd30fee49a97e76` — **identique** à la référence : aucune différence à intégrer |
| Contrats amont | Corrective architecturale Activation F2 **V1.2** (non versionnée, voir § 1.2) ; **F-02A V0.2** (`docs/f02a_routing_policy.md`, `core/route_policy/`) |

Workflow applicable à chaque lot : architecture → freeze fondateur → implémentation → PR → audit indépendant →
merge fondateur → validation Railway.

### 1.1 Nature de ce document

Ce document est un **contrat d'architecture**. Il ne contient aucun code, ne modifie aucun fichier existant,
ne crée aucun point d'entrée, aucune variable d'activation, aucun secret, aucune migration. Il fixe :

- ce que la future composition HTTP F2 (F-04) **doit** garantir ;
- ce qu'elle **ne doit jamais** faire ;
- les **réserves ouvertes** P1 (frontière réseau Railway) et P2 (arrêt et drainage C3), **non résolues** ;
- la **matrice de qualification** et les critères GO / NO-GO des lots F04-B à F04-E.

Conventions de lecture :

- **DOIT / NE DOIT PAS** : exigence figée par le présent freeze partiel ;
- **CANDIDAT** : orientation retenue pour l'audit, **non figée**, à confirmer par le freeze du lot concerné ;
- **RÉSERVE OUVERTE** : question non tranchée, bloquante pour l'activation ;
- **[constaté]** : fait vérifié dans le dépôt à la référence auditée (fichier cité) ;
- **[décision fondateur]** : décision transmise avec la demande de freeze, non versionnée dans le dépôt.

### 1.2 Références non vérifiables dans le dépôt

| Référence | État dans GitHub à `7224fe7` | Traitement ici |
|---|---|---|
| Corrective architecturale Activation F2 **V1.2** | **Absente** (seule mention : « Activation F2 Lot 1 V1.2 » dans `docs/act_l1_pr1.md`, sans le texte de la corrective) | Seules les décisions transmises avec la demande sont reprises, marquées **[décision fondateur]** ; aucun autre contenu n'est supposé |
| F04-A V0.1 / V0.2 | Absentes | V0.3 est le premier état versionné ; aucun écart avec des versions antérieures ne peut être vérifié |
| Textes littéraux F-01 à F-04 | Absents (déjà signalé par `docs/f2_r1_pr9c1a.md` § 1) | Non cités |
| Documentation Railway (en-tête `X-Real-IP`, réseau privé, signaux d'arrêt) | Externe, non versionnée | Citée comme **[décision fondateur]** ; aucune propriété n'en est déduite (§ 7) |
| Configuration du service Railway (commande de démarrage, sonde, variables) | **Non versionnée** : aucun `Procfile`, `railway.json`/`railway.toml`, `nixpacks.toml` dans le dépôt | Non vérifiée ; à relever lors de F04-B (§ 13) |

## 2. Périmètre (A)

**Dans le périmètre F04-A** : ce seul document.

**Hors périmètre, explicitement** :

- **aucune activation F2** : `api.py` reste l'unique application servie ; aucune route F2 montée ;
- **F-03** (basculement opératoire, arrêt des writers legacy, sort des données historiques) : chantier distinct ;
- **F-05** (nouveau frontend servi sur `GET /web-v2`) : chantier distinct ;
- **aucun changement** des moteurs métier, du runner (`core/authz_f2/runner.py`), des vingt commandes, des plans
  de verrouillage, de B1 / B2 / C1-B / C2 / C3, des contrats HTTP F2 existants ni de l'autorisation
  transactionnelle ;
- **aucune modification** du contrat F-02A V0.2 ni de la corrective V1.2.

**Fichiers non modifiés** (preuve au § 15) : `api.py`, `core/**`, `requirements.txt`, `alembic/**`, `tests/**`,
`ci/**`, `.github/**`, `web/**`, `web-v2/**`, configuration Railway.

## 3. État constaté du dépôt utile à F-04

| Sujet | Constat | Source |
|---|---|---|
| Porte de routage fermée | `RoutePolicyGate(*, f2_auth_app, f2_personal_app)`, arguments nommés obligatoires, `None` = non composé (404) ; aucun repli vers `api:app` | `core/route_policy/gate.py` |
| Registre | 58 couples, `POLICY_VERSION = "F-02A-V0.2"`, comptes vérifiés à l'import (A 15, B 4, C 4, D 14 + 13, doc 8) | `core/route_policy/registry.py` |
| Validation aval par la porte | inclusion (`exposed <= allowed`), pas égalité ; documentation et `CORSMiddleware` refusés | `gate.py::_validate_downstream` |
| Lifespan | acquitté par la porte seule, **jamais transmis** aux applications injectées | `gate.py::_lifespan`, `docs/f02a_routing_policy.md` § 8.5 |
| Composition d'authentification | `build_isolated_f2_app(*, engine, keyring, supabase, mailer, csrf_keyring, ip_resolver, allowed_origins, …)` : 14 routes, aucune documentation, aucun CORS, `_NoStore` ; n'initialise **pas** `auth_abuse_key_state` | `core/auth_f2_http/isolated.py` |
| Composition personnelle | `build_isolated_personal_app(*, engine, keyring, allowed_origins, csrf_keyring, …, portfolio_keyring=None, decryptage=None)` : 10 routes, **13 avec `DecryptageHttpConfig`** ; READ COMMITTED exigé ; indépendance des clés F2 / CSRF / portefeuille / Décrypter vérifiée | `core/authz_f2_http/isolated.py` |
| Résolveurs d'IP | `PeerAddressResolver` (pair TCP, valable sans proxy) ; `TrustedProxyResolver(trusted_networks, header=…)` (réseaux de proxys approuvés **obligatoires**) ; `ClientIpUnavailable` → `503 temporarily_unavailable` sans appel moteur | `core/auth_f2_http/client_ip.py`, `core/auth_f2_http/routes.py` |
| Provenance authentification | voir § 6.2 (moins stricte que la provenance personnelle) | `core/auth_f2_http/provenance.py` |
| Provenance personnelle | Host canonique unique, Origin = `https://<Host>` (obligatoire sur mutation), Sec-Fetch-Site `same-origin` seul, Referer cohérent, doublons refusés | `core/authz_f2_http/provenance.py` |
| C3 | budget coopératif **270 s**, seuil avant B2 255 s, **2** orchestrations `/turns` simultanées par instance (quota local), exécution en fil via `run_in_threadpool` | `core/authz_f2_http/decryptage.py` |
| B2 | budget **240 s** (marge 5 s) ; registre de baux consommés **en mémoire** (aucune unicité entre processus ni après crash) | `core/authz_f2/decryptage_generation.py` |
| B1 | bail **360 s**, 3 attributions, backoff 30 s / 120 s, renouvellement et purge interdits | `core/authz_f2/decryptage_reservation.py` |
| Anti-abus | `initialize_abuse_key_state` = acte explicite ; `load_key_state` = lecture **sans verrou** (refus si état absent ou trousseau incompatible) | `core/auth_f2/abuse.py` |
| Dépendances de production | 10 lignes ; **ni `httpx` ni `httpx2`** ; `anthropic>=0.25.0`, `requests`, `sqlalchemy`, `psycopg2-binary` **non épinglés** | `requirements.txt` |
| Dépendances CI | verrou complet avec empreintes : `httpx==0.28.1` (via `ci/requirements-ci.in`, outil de test), `httpx2==2.13.1` (**via `anthropic==1.11.0`**), `uvicorn==0.27.0`, `starlette==0.36.3` | `ci/requirements-ci.txt` |
| Imports fournisseurs | `core/auth_f2/http_providers.py` importe `httpx` **à la construction** ; `core/decryptage_providers.py` importe `httpx2`, `requests`, `anthropic` **au niveau module** | fichiers cités |
| Point d'entrée actuel | `api.py` : `uvicorn.run(app, host="0.0.0.0", port=port)` (réglages Uvicorn par défaut, donc `proxy_headers=True`) | `api.py` l. 1422 |
| Uvicorn 0.27.0 | `proxy_headers=True` par défaut ; `forwarded_allow_ips` lu dans `FORWARDED_ALLOW_IPS` (défaut `127.0.0.1`) ; `timeout_graceful_shutdown` disponible ; `lifespan="auto"` par défaut | `uvicorn/config.py` (version verrouillée en CI) |
| Migrations | 21, tête `0021` | `alembic/versions/` |

## 4. Architecture cible (B)

```
            Railway (edge public / réseau privé — P1)
                              │
                              ▼
                 Uvicorn (nouveau point d'entrée F-04)
                              │  scope ASGI : client = pair réel (P1), root_path = ""
                              ▼
  ┌──────────────────── Enveloppe F-04 ─────────────────────┐
  │ propriétaire du lifespan, des ressources, de l'état     │
  │ d'admission (maintenance) et du suivi des travaux actifs│
  │                         │                               │
  │                         ▼                               │
  │                 RoutePolicyGate (F-02A V0.2, inchangée) │
  │        410 / 404 / 405 / GET /health  │  transmission   │
  │                                       ▼                 │
  │        sécurité HTTP F2 (F04-D : Host, IP — P1)         │
  │              │                               │          │
  │              ▼                               ▼          │
  │      Auth F2 (14 couples)        Personnel F2 (13 couples, C3)
  │              │                               │          │
  │              ▼                               ▼          │
  │   F2AuthService / HttpAdmission   AuthorizedTransactionRunner (unique autoriseur)
  └─────────────────────────────────────────────────────────┘
                              │
                              ▼
                   PostgreSQL partagé (READ COMMITTED, pool borné)
```

Exigences figées :

1. **Nouveau point d'entrée indépendant de `api.py`** (F04-C). Il **NE DOIT** importer ni `api`, ni un module
   qui l'importe, ni aucune application legacy, **ni comme cible, ni comme repli, ni pour des ressources
   partagées**. Vérification attendue : sous-processus, `"api" not in sys.modules` après construction.
2. **Ordre de la chaîne** : Uvicorn → enveloppe F-04 → `RoutePolicyGate` → Auth F2 / Personnel F2. La porte
   reste l'**unique** décideur du routage ; l'enveloppe n'ajoute aucune route, aucun repli, aucune réécriture
   de chemin.
3. **Unique autoriseur** : `AuthorizedTransactionRunner` reste le seul autoriseur des commandes personnelles.
   L'enveloppe, la porte et la couche de sécurité HTTP F2 **ne décident jamais** d'une autorisation, ne lisent
   aucun cookie de session à des fins d'autorisation et ne court-circuitent aucun contrôle des routes F2.
4. **Composition de production complète** : application d'authentification avec ses **14** couples, application
   personnelle avec ses **13** couples **C3 inclus** (`decryptage=DecryptageHttpConfig(...)` obligatoire en
   production). Une composition personnelle sans C3 (10 couples) ou une application `None` **n'est pas** une
   composition de production : elle n'est admise que pour la cible fermée de retour arrière (§ 11).
5. **Égalité exacte avec le registre F-02A** : la porte vérifie l'inclusion ; F-04 **DOIT** vérifier en plus,
   à la construction, l'**égalité** `{(méthode, chemin)}` exposés = `registry.pairs_of(F2_AUTH)` (14) et
   `= registry.pairs_of(F2_PERSONAL)` (13). Toute différence : échec fermé de la construction (aucun démarrage).
   Ce contrôle est ajouté par F-04 **sans modifier** `core/route_policy/`.

## 5. Routage fermé (C)

La politique F-02A V0.2 est **reprise sans modification**. F-04 ne la révise pas.

| Catégorie | Couples | Décision |
|---|---|---|
| Legacy retirées (A) | 15 | **410** `{"error":"legacy_personal_api_retired"}`, toute méthode |
| Désactivées (B) | 4 (`/web`, `/analyze`, `/analyze-etf`, `/pedagogie/lookup`) | **404** |
| `GET /health` | 1 | **200** `{"status":"ok"}`, servi par la porte |
| `HEAD /health` | — | **405**, `Allow: GET` |
| Frontend `GET /web-v2` | 1 | **404** jusqu'à F-05 |
| Marché `GET /web-v2/search`, `GET /web-v2/fx-rate` | 2 | **404** jusqu'à un audit distinct (entrées, effets, quotas, délais, coûts) |
| Documentation automatique | 8 (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` × GET/HEAD) | **404** |
| F2 | 14 + 13 | transmis tels quels ; méthode non déclarée → **405** |
| Inconnu, non canonique, `root_path` non vide, `path` ≠ `raw_path` | — | **404**, jamais normalisé |

Exigences F-04 :

- **aucun CORS** émis ou hérité ; aucun `CORSMiddleware` dans l'enveloppe ni en aval ;
- **aucune redirection** (ni 3xx, ni `redirect_slashes`, ni redirection HTTP → HTTPS applicative, ni
  `Location`) ;
- **aucun middleware** monté avant la porte ne peut produire une réponse pour une requête que la porte refuse,
  à l'exception de l'état de maintenance défini au § 9.3 et limité aux routes F2 transmises ;
- Uvicorn **DOIT** être lancé avec `root_path` vide (sinon toutes les requêtes reçoivent 404) ;
- les refus F-02A restent **identiques octet pour octet** (statut, en-têtes, corps) derrière l'enveloppe.

Toute évolution de cette matrice (y compris `HEAD /health`) est une révision F-02A exigeant un freeze propre.

## 6. Host, Origin, CSRF (D)

### 6.1 Exigences figées

| Exigence | Contenu |
|---|---|
| Origine pilote candidate | `https://oryx-api-production.up.railway.app` **[décision fondateur]**, seule entrée de `allowed_origins` des deux compositions |
| Host | canonique (minuscules, sans `:443`), **présent une seule fois**, égal à l'hôte de l'origine pilote |
| Origin | **obligatoire sur toute mutation F2**, présent une seule fois, **égal à `https://<Host>`** ; `null` refusé |
| Politique | **same-origin stricte** sur les routes F2 ; aucun CORS |
| Referer | s'il est présent : une seule fois, `https`, sans identifiants, d'origine égale à l'origine attendue |
| Doublons | tout en-tête de provenance dupliqué (`Host`, `Origin`, `Sec-Fetch-Site`, `Referer`) est refusé |
| Cookies | préfixe `__Host-` conservé (`__Host-oryx-candidate-*`, `__Host-oryx-active-*`, `__Host-oryx-csrf-binding`) ; parseur strict inchangé |
| CSRF | mécanisme existant `oryx-f2-csrf-v1` conservé, jeton lié à l'origine ; aucun second mécanisme |

### 6.2 Écart constaté entre les deux provenances (évolution contractuelle)

**[constaté]** `core/authz_f2_http/provenance.py` (personnel) applique déjà la politique du § 6.1.
`core/auth_f2_http/provenance.py` (authentification, R1 PR 3) est **moins stricte** :

| Contrôle sur une mutation d'authentification | Comportement actuel | Exigence § 6.1 |
|---|---|---|
| Host lorsque Origin est présent | unicité contrôlée, mais **ni canonicité ni cohérence avec Origin** | Host canonique unique, Origin = `https://<Host>` |
| `Sec-Fetch-Site` | `same-origin` **ou `same-site`** acceptés | `same-origin` seulement |
| Referer lorsque Origin est présent | unicité contrôlée, **contenu non vérifié** | cohérent s'il est présent |
| Bootstrap `GET /csrf/bootstrap` sans Origin | Host, `Sec-Fetch-Site: same-origin`, Referer vérifiés | inchangé |

Aligner l'authentification sur le § 6.1 est un **durcissement** : c'est une **évolution contractuelle** des
réponses d'authentification (de nouvelles requêtes reçoivent `403 origin_refused`). Elle **NE DOIT PAS** être
introduite silencieusement : elle relève de F04-D, avec un freeze propre, des tests **nominatifs** (chaque
combinaison nouvellement refusée nommée et testée, chaque test historique adapté nommément), et la preuve que
les parcours navigateur same-origin nominaux restent acceptés. Tant que ce freeze n'existe pas, le contrat
d'authentification R1 PR 3 reste celui du code.

### 6.3 Non-régression F-02A

Les contrôles de provenance et d'IP F2 **NE DOIVENT PAS** modifier les refus F-02A : ils ne s'appliquent
qu'aux requêtes que la porte **transmet** à une application F2. Pour toute requête refusée par la porte (410,
404, 405) ou servie par elle (`GET /health`), statut, en-têtes et corps restent ceux de F-02A, quels que soient
Host, Origin, Referer ou l'adresse du pair. **CANDIDAT** (placement à figer en F04-D) : la couche de sécurité
HTTP F2 est située après la décision de la porte (dans ou autour des applications injectées), jamais avant.

## 7. P1 — Frontière réseau Railway : RÉSERVE OUVERTE (E)

> **P1 n'est pas résolue.** Aucune activation F2 ne peut être décidée tant que P1 n'est pas qualifiée et figée.

### 7.1 Faits et limites

- Deux trafics distincts : **public** (via l'edge Railway) et **privé** (entre services Railway). Ils ne
  portent pas les mêmes garanties et **DOIVENT** être qualifiés séparément.
- Railway documente un en-tête `X-Real-IP` **[décision fondateur, documentation externe non versionnée]**. Sa
  présence **n'est pas** une preuve cryptographique que la requête a traversé un ingress approuvé : un appelant
  du réseau privé, ou tout chemin qui contourne l'edge, peut le fournir librement.
- **Aucune liste CIDR Railway stable** n'est connue : **aucune NE DOIT être inventée**, ni codée, ni configurée.
- **[constaté]** `TrustedProxyResolver` exige des réseaux de proxys approuvés : sans CIDR fiable, il **ne peut
  pas** être configuré honnêtement. `PeerAddressResolver` n'est valable que sans proxy. **Aucun résolveur
  existant ne réalise l'option A** ; un nouveau résolveur serait une évolution contractuelle F04-D.
- **[constaté]** Uvicorn 0.27.0 active `proxy_headers` par défaut et lit `FORWARDED_ALLOW_IPS` dans
  l'environnement : avec `FORWARDED_ALLOW_IPS=*`, `scope["client"]` serait réécrit depuis `X-Forwarded-For`
  fourni par le client, et `PeerAddressResolver` lirait une valeur falsifiable.

### 7.2 Exigences figées

1. **Jamais** de confiance globale accordée aux adresses privées (RFC 1918, `fd00::/8`, boucle locale hors
   test) ni à un en-tête fourni librement (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `X-Client-IP`…).
2. Une IP **absente, ambiguë ou non fiable** entraîne un **refus fermé avant admission** (aucun appel moteur,
   aucune écriture anti-abus, aucun fournisseur) ; aujourd'hui `503 temporarily_unavailable` sur les routes
   d'authentification qui résolvent l'IP.
3. Le futur réglage Uvicorn **DOIT** préserver le **pair réel** dans `scope["client"]` (**CANDIDAT** :
   `proxy_headers` explicitement désactivé et indépendance vis-à-vis de `FORWARDED_ALLOW_IPS`), **sous réserve
   des validations d'intégration** sur Railway.
4. Aucun réglage par variable d'environnement implicite ne peut élargir la confiance.

### 7.3 Options (aucun choix figé)

| Option | Contenu | Conditions d'acceptation |
|---|---|---|
| **A** (candidate) | Infrastructure Railway actuelle, avec **acceptation explicite, écrite et limitée** de son modèle de confiance (ce que l'edge garantit, ce qu'il ne garantit pas, ce qu'un appelant privé peut forger) | acceptation fondateur nominative ; périmètre de confiance borné au trafic public ; preuve que le trafic privé ne peut pas usurper une IP admise ou est refusé ; résolveur dédié audité (F04-D) |
| **B** | **Frontière d'ingress authentifiée ou isolée** (le service F2 n'est joignable que par un ingress qui prouve son identité, ou n'est pas exposé au réseau privé) | mécanisme d'authentification de l'ingress spécifié et audité ; aucun secret généré par F-04 ; tests de contournement |

Le choix entre A et B est **reporté** à la qualification P1 (préalable à F04-D).

### 7.4 Tests requis avant activation

- **public** : requête via l'edge avec `X-Real-IP`, `X-Forwarded-For`, `Forwarded` forgés, dupliqués, multiples,
  mal formés, IPv4 / IPv6 / adresses mappées ; résultat attendu : IP retenue = celle garantie par l'option
  choisie, ou refus fermé ;
- **privé** : requête depuis un autre service Railway, avec et sans en-têtes forgés ; résultat attendu : refus
  fermé ou absence de joignabilité selon l'option ;
- **Uvicorn** : `scope["client"]` inchangé en présence d'en-têtes de proxy, avec `FORWARDED_ALLOW_IPS` défini ;
- **anti-abus** : aucune écriture `auth_abuse_*` pour une requête refusée faute d'IP fiable.

## 8. Configuration et ressources (F)

### 8.1 Configuration

- Configuration **serveur stricte** : chaque valeur est requise, typée, validée ; **aucune valeur par défaut
  de sécurité**, **aucune génération automatique de secret**, aucun repli sur une variable legacy.
- **Échec fermé** à la construction sur toute configuration absente, partielle, mal formée ou incohérente
  (aucun démarrage partiel, aucune route F2 servie).
- Les noms des variables et leur format sont **reportés à F04-B** ; F04-A n'en crée aucun. **Aucune variable
  d'activation** n'est définie ici.

### 8.2 Trousseaux

Quatre trousseaux **indépendants**, aucune clé partagée (contrôles existants conservés et réexécutés par la
composition de production) :

| Trousseau | Type | Indépendance déjà vérifiée par le code |
|---|---|---|
| F2 (6 usages : `candidate`, `derive`, `active`, `otp`, `invitation`, `abuse`) | `Keyring` | — |
| CSRF | `CsrfKeyring` | ≠ F2 (deux compositions) |
| Portefeuille | `PortfolioKeyring` | ≠ F2, ≠ CSRF |
| Décrypter | `DecryptageKeyring` | ≠ F2, ≠ portefeuille (runner), ≠ CSRF (composition) |

Le **même** trousseau F2 sert aux deux compositions (les sessions émises par l'authentification sont vérifiées
par le runner) ; le **même** trousseau CSRF sert aux deux compositions.

### 8.3 PostgreSQL

- **Un seul moteur PostgreSQL partagé** par les deux compositions (même base que l'état F2).
- Isolation **READ COMMITTED** (toute autre isolation explicite est déjà refusée par la composition personnelle).
- **Pool borné** explicitement (taille, débordement, délai d'attente, recyclage) ; valeurs reportées à F04-B et
  dimensionnées au moins pour : 2 orchestrations C3 simultanées + requêtes courtes concurrentes + transactions
  d'authentification. Aucune valeur n'est figée ici.
- Le moteur est **possédé** par l'enveloppe et **fermé** (`dispose`) au shutdown, après drainage (§ 10).

### 8.4 Fournisseurs

Configuration **complète** exigée en production (aucun fournisseur facultatif) :

| Fournisseur | Usage | Adaptateur existant |
|---|---|---|
| Supabase | OTP e-mail (authentification, liaison) | `HttpSupabaseOtpProvider` (`httpx`) |
| Resend | envoi OTP | `HttpResendMailer` (`httpx`) |
| EODHD | recherche, fondamentaux, temps réel (B2) | `EodhdTransport` (`requests`, session injectable) |
| FMP | profil, ratios (B2) | `FmpTransport` (`requests`, session injectable) |
| Claude | génération (B2), modèle **explicite** | `ClaudeTransport` (`anthropic`, `max_retries=0`) |

Les URL viennent de la configuration serveur, jamais d'une entrée client ; TLS vérifié ; aucune redirection ;
aucun retry implicite (contrats existants inchangés).

### 8.5 Dépendances de production

**[constaté]** `requirements.txt` ne déclare ni `httpx` ni `httpx2`, alors que `core/auth_f2/http_providers.py`
exige `httpx` et que `core/decryptage_providers.py` importe `httpx2` au niveau module. En CI, `httpx` est un
outil de test et `httpx2` n'arrive que **transitivement** via `anthropic==1.11.0` ; en production,
`anthropic>=0.25.0` n'est pas épinglé et pourrait résoudre une version qui n'apporte pas `httpx2`.

Exigence : F04-B **DOIT** qualifier explicitement les dépendances de production de la composition F2 —
notamment `httpx`, `httpx2`, `anthropic`, `requests`, `sqlalchemy`, `psycopg2-binary`, `uvicorn`, `fastapi`,
`starlette` — avec versions épinglées, cohérence avec le verrou CI et preuve que le serveur démarre depuis la
seule installation de production. F04-A ne modifie pas `requirements.txt`.

### 8.6 Propriété et fermeture des ressources

- L'enveloppe **possède** chaque ressource qu'elle construit : moteur PostgreSQL, clients ou transports HTTP
  Supabase / Resend, sessions `requests` EODHD / FMP, client Anthropic.
- Chaque ressource possédée est **fermée explicitement** une fois et une seule, dans l'ordre inverse de
  construction, après drainage.
- **Fin d'usage d'une ressource** : une ressource n'est réputée libre d'usage que si **tous** les travaux
  suivis qui peuvent l'utiliser (§ 10.3), y compris les fils C3 qui poursuivent après l'annulation de leur
  requête HTTP, ont **effectivement terminé**. L'expiration d'un délai de drainage **ne prouve pas** la fin des
  fils C3 et **n'autorise pas** à considérer leur usage du moteur PostgreSQL, des connexions du pool ou des
  clients fournisseurs comme terminé. Une fermeture effectuée alors que des fils sont encore actifs relève du
  cas « délai dépassé » ou « arrêt forcé » du § 10.3 et **NE DOIT PAS** être présentée comme une fermeture
  gracieuse.
- **[constaté]** Les adaptateurs actuels n'exposent pas tous de fermeture : EODHD / FMP acceptent une session
  injectée (fermable par son propriétaire) ; Supabase / Resend créent leur `httpx.Client` en interne autour d'un
  transport injectable ; `ClaudeTransport` crée son client Anthropic en interne. La manière de garantir la
  fermeture (transports possédés par l'enveloppe, ou extension contractuelle des adaptateurs) est **reportée à
  F04-B** et constitue, le cas échéant, une évolution à auditer.

### 8.7 Anti-abus

- **Aucune migration** (`alembic upgrade`) et **aucune initialisation anti-abus**
  (`initialize_abuse_key_state`) au démarrage, à l'import ou à la construction.
- Vérification **en lecture seule** de l'état anti-abus avant `startup.complete` (**CANDIDAT** :
  `load_key_state`, lecture sans verrou) : état absent ou trousseau incompatible → échec fermé du démarrage.
- **CANDIDAT** (à arbitrer en F04-B) : vérification en lecture seule que la tête Alembic en base est la tête
  attendue ; écart → échec fermé, jamais de migration automatique.

## 9. Lifespan (G)

### 9.1 Construction

- **Aucun effet externe à l'import** du point d'entrée : aucune connexion, aucun fil, aucun appel réseau,
  aucune lecture de secret au-delà de la configuration, aucune écriture.
- La construction de l'application (porte, compositions) reste sans effet externe (propriété déjà vérifiée
  pour les compositions isolées).

### 9.2 Démarrage

- `lifespan.startup.complete` n'est émis **qu'après** validation de tous les prérequis : configuration,
  dépendances importables, égalité exacte des 27 couples (§ 4.5), connexion PostgreSQL et isolation, état
  anti-abus en lecture seule, construction des fournisseurs.
- Tout échec : `lifespan.startup.failed`, fermeture des ressources **partiellement construites** (chacune
  fermée une fois, y compris si la fermeture d'une autre échoue), aucune route F2 servie.
- **Aucun lifespan aval silencieusement ignoré** : la porte acquitte le lifespan sans le transmettre (constaté).
  F-04 **DOIT** donc soit refuser à la construction toute application aval qui déclare un lifespan ou des
  gestionnaires de démarrage / arrêt, soit les exécuter explicitement et en propager l'échec. **CANDIDAT** :
  refus (les compositions actuelles n'en déclarent pas). Le lifespan est **possédé par l'enveloppe**, pas par
  la porte.
- **Détection d'un lifespan aval actif** : elle **NE DOIT PAS** confondre le mécanisme standard implicite de
  FastAPI avec des gestionnaires explicitement déclarés. **[constaté]** Avec FastAPI 0.110.0 / Starlette
  0.36.3 (versions verrouillées en CI), toute application FastAPI, y compris les compositions isolées
  actuelles, porte un contexte de lifespan **par défaut** (`_DefaultLifespan` de Starlette) dont les listes
  `on_startup` / `on_shutdown` sont vides : ce mécanisme implicite, sans gestionnaire, **n'est pas** un lifespan
  actif et ne justifie aucun refus. Est en revanche un lifespan aval **actif** : un contexte `lifespan=`
  explicitement fourni, ou au moins un gestionnaire de démarrage / arrêt déclaré (directement ou par un routeur
  inclus). Le critère exact de détection est figé en F04-C et testé dans les deux sens : compositions actuelles
  **acceptées**, application déclarant un contexte ou un gestionnaire **refusée** (ou exécutée explicitement).

### 9.3 Sonde et maintenance

- `GET /health` reste une **sonde de vie** servie par la porte (200 `{"status":"ok"}`) : elle **ne prouve
  pas** que l'admission F2 est autorisée, ni que PostgreSQL ou les fournisseurs sont disponibles, ni que P1
  ou P2 sont qualifiées. Son contrat n'est pas modifié.
- **CANDIDAT** (F04-C) : un état d'admission interne (ouvert / maintenance) possédé par l'enveloppe ; en
  maintenance, les nouvelles requêtes **transmises** aux applications F2 reçoivent un refus fermé no-store
  (statut et corps à figer en F04-C) ; les décisions F-02A (410 / 404 / 405 / `GET /health`) restent
  inchangées.

## 10. P2 — Arrêt et drainage C3 : RÉSERVE OUVERTE (H)

> **P2 n'est pas résolue.** Les paramètres Railway / Uvicorn de drainage ne sont **pas figés**.

### 10.1 Cadre du pilote

| Paramètre | Valeur | Nature |
|---|---|---|
| Processus | 1 | **[décision fondateur]** |
| Répliques | 1 | **[décision fondateur]** |
| Routeur C3 | 1 (un seul `_TurnCapacity` par application personnelle) | **[décision fondateur]**, cohérent avec le code |
| Orchestrations `/turns` simultanées | **2** par instance, quota **local, non distribué** | **[constaté]** `MAX_CONCURRENT_TURNS = 2` |
| Budget C3 | **270 s**, coopératif | **[constaté]** `C3_BUDGET_SECONDS` |
| Budget B2 | **240 s** (marge 5 s) | **[constaté]** `TOTAL_BUDGET_SECONDS` |
| Bail B1 | **360 s** | **[constaté]** `LEASE_DURATION` |

Plusieurs processus ou répliques **multiplieraient** le quota local et le registre B2 en mémoire : ce n'est pas
couvert par ce contrat et exigerait une révision.

### 10.2 Ce que les budgets ne garantissent pas

- Les budgets sont des frontières **coopératives** : ils ne garantissent **aucune terminaison physique** d'un fil,
  d'un appel fournisseur ou d'une transaction ; ils n'interrompent jamais C1-B ni son COMMIT.
- L'orchestration `/turns` s'exécute dans un fil (`run_in_threadpool`) qui **n'est pas annulable** : une
  déconnexion client ou une annulation de coroutine ne l'arrête pas.

### 10.3 Exigences d'arrêt

1. **Maintenance avant arrêt** : fermeture des **nouvelles** admissions F2 avant tout arrêt volontaire.
2. **Suivi des travaux actifs** par l'enveloppe, y compris requêtes d'authentification, `/turns`, `/replay` et
   `/ack` (pas seulement les places C3). Le suivi porte sur les travaux **réellement engagés**, et non sur les
   requêtes HTTP ouvertes : un fil C3 qui poursuit après l'annulation ou la déconnexion de sa requête reste
   suivi jusqu'à sa fin effective. Ce suivi relève du contrat de conception **F04-C** (§ 13).
3. **Drainage avant fermeture des ressources :** la fermeture gracieuse du moteur PostgreSQL et des clients
   fournisseurs n'est autorisée qu'après la fin effectivement constatée des travaux suivis. Si le délai expire
   alors que des travaux restent actifs, le processus entre dans une situation de délai dépassé ou d'arrêt
   forcé, dont le comportement exact reste à qualifier en F04-E. Cette situation ne constitue jamais une
   fermeture gracieuse et ne présume pas l'issue des transactions.
4. **Trois issues d'arrêt distinctes**, jamais confondues (dans les journaux, les rapports de qualification ni
   la documentation d'exploitation) :

   | Issue | Définition | Ce qui est établi | Ce qui ne l'est pas |
   |---|---|---|---|
   | **Fermeture gracieuse** | tous les travaux suivis, fils C3 compris, ont **effectivement terminé** avant l'échéance ; les ressources sont fermées ensuite | aucun travail suivi n'utilise plus les ressources au moment de leur fermeture | rien au-delà : l'issue métier de chaque tour reste celle que B1 / C1-B / C2 ont durablement enregistrée |
   | **Délai dépassé** | l'échéance de drainage expire alors qu'au moins un travail suivi n'a pas terminé | seulement que l'échéance est atteinte | **ni** la fin des fils C3, **ni** la fin de leur usage des ressources, **ni** l'issue de leurs transactions ou appels fournisseurs ; les fils restants sont réputés **encore actifs** |
   | **Arrêt forcé** | le processus est interrompu sans attendre la fin des travaux (`SIGKILL`, perte de conteneur, crash, arrêt après délai dépassé) | aucun fil ne survit au processus | **ni** l'issue des transactions en cours, **ni** celle des appels fournisseurs engagés |

   Dans les deux derniers cas, l'issue de chaque travail non terminé est **inconnue** ; elle ne se déduit
   que de l'état durable PostgreSQL, relu par les mécanismes B1 / C2 lors d'une reprise.
5. **COMMIT incertain** : un COMMIT dont l'acquittement n'a pas été observé (connexion coupée, délai dépassé,
   arrêt forcé, fermeture du pool pendant l'attente) **n'est jamais présumé annulé**, ni présumé réussi. Aucune
   compensation, aucun rejeu automatique et aucune écriture correctrice ne sont déclenchés sur la base d'une
   telle présomption ; seule la relecture de l'état durable (B1 / C2) fait foi.
6. **Aucune reprise automatique** d'un fil perdu (crash, `SIGKILL`, perte de conteneur).
7. **Reprise par la même intention** : l'utilisateur rejoue la même intention ; B1 (bail, attributions,
   backoff) et C2 (replay canonique) sont les seuls mécanismes durables de reprise.
8. **Aucune promesse d'exécution fournisseur exactement une fois** : après une interruption antérieure à la
   finalisation, un appel fournisseur supplémentaire (EODHD, FMP, Claude) peut être nécessaire ; seule la
   livraison canonique est unique (C1-B / C2).

### 10.4 Tests requis (F04-E)

`SIGTERM` pendant chaque étape de `/turns` (avant B1, après B1, pendant B2, pendant C1-B, pendant le COMMIT,
pendant C2) ; `SIGKILL` aux mêmes points ; annulation et déconnexion client ; **COMMIT incertain** (connexion
coupée après envoi du COMMIT) ; reprise de la même intention après chacun de ces cas ; vérification de l'absence
de double livraison, de verrous orphelins et de fuite de place locale.

### 10.5 Non figé

Délai de grâce Uvicorn (`timeout_graceful_shutdown`), délai d'arrêt Railway, signal envoyé par Railway, ordre
exact maintenance → drainage → fermeture, comportement au-delà de l'échéance. Ces paramètres **DOIVENT** être
mesurés puis figés en F04-E ; aucune valeur n'est proposée ici. Le budget C3 (270 s) et le bail B1 (360 s)
sont des données d'entrée de ce dimensionnement, pas des garanties.

## 11. Déploiement et retour arrière (I)

- Les PR **F04-A à F04-E restent dormantes** : aucune ne change le point d'entrée servi.
- Railway **conserve `api`** comme service servi pendant tout le développement F-04.
- **Aucune coexistence non contrôlée** de writers legacy et F2 sur la même base lors de F-03.
- Arrêt des anciens traitements et du worker **selon la procédure F-03** (hors de ce document).
- **Sauvegarde et restauration PostgreSQL vérifiées** (restauration effectivement exécutée et contrôlée) avant
  tout basculement.
- **Retour arrière** : vers une **cible fermée** (porte F-02A avec `f2_auth_app=None, f2_personal_app=None` :
  410 / 404 / 405 et `GET /health` seulement) ou vers une **maintenance indisponible**.
- **Jamais de retour automatique à `api` public** après le retrait des anciennes routes. Tout retour à `api`
  serait une décision fondateur explicite et distincte, hors du présent contrat.

## 12. Séquençage (J)

| Lot | Objet | Dépend de |
|---|---|---|
| **F04-A** | ce contrat documentaire | F-02A V0.2 |
| **F04-B** | configuration, dépendances de production, ressources et leur fermeture | F04-A figé |
| **F04-C** | composition ASGI et lifespan **dormants** (nouveau point d'entrée non servi) | F04-B |
| **F04-D** | sécurité HTTP et IP (provenance alignée, résolveur, réglage Uvicorn) | F04-C **et qualification P1** |
| **F04-E** | tests d'intégration, arrêt, reprise, **qualification P2** | F04-D |
| **F-05** | frontend séparé (`GET /web-v2`) | chantier distinct |
| **F-03** | basculement opératoire | chantier distinct, après F04-E et F-05 |

Chaque lot suit le workflow complet (§ 1) et fait l'objet d'un freeze propre.

## 13. Matrice de qualification et critères GO / NO-GO (K)

Légende des preuves : **Doc** = preuve documentaire (lecture, diff, revue) ; **Exp** = preuve expérimentale
(test exécuté, mesure, essai sur Railway). Une preuve documentaire **ne remplace jamais** une preuve
expérimentale exigée.

### F04-A — Contrat documentaire

| Rubrique | Contenu |
|---|---|
| Préconditions | `main` = référence auditée ; F-02A V0.2 fusionnée et dormante |
| Tests requis | aucun test nouveau ; suites existantes sans base inchangées (non-régression) |
| Preuves attendues | **Doc** : diff = un fichier ajouté sous `docs/`, aucun fichier modifié ; **Exp** (facultative) : suites existantes vertes |
| Refus attendus | sans objet (aucun comportement) |
| Blocage (NO-GO) | tout fichier existant modifié ; tout code ; P1 ou P2 présentée comme résolue ; F-04 déclaré entièrement figé ; contrat inventé hors des décisions transmises |
| Frontière | `docs/act_l1_f04a_architecture_v03.md` uniquement |

### F04-B — Configuration, dépendances, ressources

| Rubrique | Contenu |
|---|---|
| Préconditions | F04-A fusionné ; freeze F04-B (noms et formats de configuration, dépendances, pool) |
| Tests requis | chargement de configuration (absente, partielle, mal formée, doublons, clés partagées entre trousseaux) ; installation de production seule ; import des adaptateurs ; propriété et fermeture uniques des ressources, y compris en échec partiel ; lecture seule de l'état anti-abus ; aucune écriture en base |
| Preuves attendues | **Exp** : tests nominatifs ; installation depuis `requirements.txt` seul ; **Doc** : verrou des dépendances justifié |
| Refus attendus | toute configuration incohérente → échec fermé, sans connexion ni écriture |
| Blocage | génération de secret ; valeur par défaut de sécurité ; migration ou initialisation au démarrage ; `httpx` / `httpx2` non qualifiés ; `api.py` modifié ; point d'entrée servi changé |
| Frontière | nouveaux modules de configuration / ressources, `requirements.txt` (qualifié), tests ; aucun moteur métier ni contrat HTTP F2 |

### F04-C — Composition ASGI et lifespan dormants

| Rubrique | Contenu |
|---|---|
| Préconditions | F04-B fusionné ; freeze F04-C (enveloppe, état de maintenance, contrat du refus de maintenance, **contrat de suivi des travaux engagés**, critère de détection d'un lifespan aval actif) |
| Contrat de conception | Le **suivi des travaux réellement engagés** appartient à F04-C : l'enveloppe connaît à tout instant les travaux en cours (requêtes d'authentification, `/turns`, `/replay`, `/ack`), **y compris les fils C3 qui continuent après l'annulation ou la déconnexion de leur requête HTTP** ; un travail n'est retiré du suivi qu'à sa fin effective, jamais à la fin de sa requête ni à l'expiration d'un délai. Ce suivi alimente les trois issues d'arrêt du § 10.3 (fermeture gracieuse, délai dépassé, arrêt forcé). |
| Tests requis | égalité exacte 14 / 13 avec le registre ; absence de `api` dans `sys.modules` ; aucun effet à l'import ; `startup.complete` seulement après prérequis ; `startup.failed` et fermeture des ressources partielles ; détection du lifespan aval : compositions actuelles (lifespan implicite FastAPI) acceptées, contexte ou gestionnaire explicite refusé ; suivi des travaux engagés en fonctionnement nominal et après annulation d'une requête (fil toujours suivi) ; refus F-02A identiques octet pour octet derrière l'enveloppe ; dormance : `api:app` inchangé |
| Preuves attendues | **Exp** : sous-processus avec gardes réseau / base ; vrai serveur Uvicorn local |
| Refus attendus | composition incomplète, application legacy, documentation, CORS → échec de construction |
| Blocage | repli vers `api` ; démarrage partiel ; lifespan ignoré ; Railway modifié ; variable d'activation |
| Frontière | nouveau point d'entrée et enveloppe (non servis), tests ; `core/route_policy/` inchangé |

### F04-D — Sécurité HTTP et IP

| Rubrique | Contenu |
|---|---|
| Préconditions | F04-C fusionné ; **P1 qualifiée** et option A ou B **figée** par le fondateur ; freeze du durcissement de provenance d'authentification |
| Tests requis | combinaisons Host / Origin / Referer / Sec-Fetch-Site / doublons, **nominatives** pour chaque refus nouveau ; parcours navigateur same-origin acceptés ; spoofing `X-Real-IP` / `X-Forwarded-For` / `Forwarded` ; `scope["client"]` préservé par Uvicorn avec `FORWARDED_ALLOW_IPS` défini ; refus fermé avant admission sans écriture anti-abus ; refus F-02A inchangés quelle que soit la provenance |
| Preuves attendues | **Exp** : tests locaux **et** essais publics et privés sur Railway (environnement non servi) ; **Doc** : acceptation écrite du modèle de confiance (option A) ou spécification de l'ingress (option B) |
| Refus attendus | IP non fiable → refus fermé ; provenance non conforme → `403 origin_refused` |
| Blocage | CIDR Railway inventé ; confiance globale aux adresses privées ou à un en-tête libre ; durcissement non testé nominativement ; modification d'un refus F-02A |
| Frontière | couche de sécurité HTTP F2, résolveur, réglage Uvicorn, provenance d'authentification (évolution figée), tests ; runner et moteurs inchangés |

### F04-E — Intégration, arrêt, reprise, qualification P2

| Rubrique | Contenu |
|---|---|
| Préconditions | F04-D fusionné ; pilote 1 processus / 1 réplique / 1 routeur C3 |
| Tests requis | parcours complets (authentification, profil, portefeuille, consultations, C3) derrière Uvicorn ; `SIGTERM`, `SIGKILL`, annulation, déconnexion, COMMIT incertain (§ 10.4) ; **comportement du suivi F04-C lors des arrêts et interruptions** (fils C3 poursuivant après annulation de leur requête toujours suivis, ressources non fermées comme si ces fils avaient terminé) ; distinction observable des trois issues fermeture gracieuse / délai dépassé / arrêt forcé ; COMMIT incertain jamais présumé annulé ; reprise de la même intention ; maintenance avant arrêt ; drainage avant fermeture ; mesure des délais Railway / Uvicorn |
| Preuves attendues | **Exp** : rapports de tests et mesures ; **Doc** : paramètres de drainage figés avec leur justification |
| Refus attendus | nouvelles admissions F2 refusées en maintenance ; aucune double livraison |
| Blocage | reprise automatique d'un fil perdu ; promesse d'exécution fournisseur exactement une fois ; fermeture des ressources avant drainage ; délai dépassé ou arrêt forcé présenté comme une fermeture gracieuse ; COMMIT incertain présumé annulé ; paramètres non mesurés |
| Frontière | tests d'intégration, scripts de qualification, documentation ; aucune modification Railway servie |

### Critère global d'activation (hors F-04)

**NO-GO** tant que l'une des conditions suivantes n'est pas remplie : F04-B à F04-E fusionnés ; P1 et P2
qualifiées et figées ; F-05 validé pour le frontend ; procédure F-03 exécutable (arrêt des writers legacy et du
worker, sauvegarde et restauration vérifiées, retour arrière vers cible fermée) ; décision fondateur explicite.

## 14. Incohérences et points relevés pour l'audit

1. **Dépendances de production** : `httpx` et `httpx2` absents de `requirements.txt` alors que les
   adaptateurs F2 les exigent ; `httpx2` n'arrive en CI que transitivement via `anthropic==1.11.0`, et
   `anthropic>=0.25.0` n'est pas épinglé en production (§ 8.5). Sans effet aujourd'hui (F2 dormant) ; bloquant
   pour F04-B.
2. **Provenance d'authentification** moins stricte que la politique same-origin exigée (Host non comparé à
   Origin, `same-site` accepté, contenu du Referer non vérifié lorsque Origin est présent) (§ 6.2). Évolution contractuelle F04-D.
3. **Résolveur d'IP** : aucun résolveur existant ne réalise l'option A sans liste CIDR (§ 7.1).
4. **Réglage Uvicorn** : `api.py` démarre Uvicorn avec ses défauts (`proxy_headers=True`, `FORWARDED_ALLOW_IPS`
   lu dans l'environnement) ; le nouveau point d'entrée devra le fixer explicitement (§ 7.2).
5. **Validation aval de la porte** par inclusion seulement : l'égalité exacte 14 / 13 est une exigence F-04
   supplémentaire (§ 4.5), sans modification de `core/route_policy/`.
6. **Lifespan** non transmis par la porte (§ 9.2), conforme à la limite déjà documentée par F-02A § 8.5.
7. **Fermeture des clients** : Supabase, Resend et Claude créent leur client en interne (§ 8.6).
8. **Références non versionnées** : corrective V1.2, versions F04-A antérieures, documentation et configuration
   Railway (§ 1.2).
9. **Branche** : la demande prévoit « une branche F04-A dédiée » ; la session impose la branche de travail
   `claude/ecstatic-heisenberg-vjje59`, utilisée exclusivement pour ce lot.

Aucune de ces observations n'est corrigée ici : F04-A ne modifie aucun fichier existant.

## 15. Preuve de dormance de ce lot

- Diff contre `7224fe7` : **un seul fichier ajouté**, `docs/act_l1_f04a_architecture_v03.md` ; **aucun fichier
  existant modifié, renommé ou supprimé**.
- Aucun fichier Python, aucune migration, aucune dépendance, aucun test, aucune CI, aucun frontend, aucune
  configuration Railway.
- Aucun point d'entrée modifié : `api.py` reste l'application servie ; aucune route F2 montée ; aucune variable
  d'activation ; aucun secret ; aucun déploiement.
- La documentation n'est lue par aucun code ni aucun test : aucun changement d'exécution n'est possible.
