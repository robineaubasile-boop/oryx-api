# ACT-L1 / F04-D / P1-Q V0.1 — Protocole de qualification expérimentale de la confiance réseau Railway

## 1. Référentiel et statut

| Élément | Valeur |
|---|---|
| Chantier | Activation F2 Lot 1 (ACT-L1), lot **F04-D**, réserve **P1**, protocole **P1-Q** |
| Version | **V0.1** (première version versionnée) |
| Statut du protocole | **Validé par le fondateur pour PRÉPARER les expériences** (message de lancement du 14/11/2026) ; cette validation n'autorise **aucune** modification d'infrastructure ni aucune exécution sur Railway |
| Statut de P1 | **OUVERTE** |
| Option A | **Candidate prioritaire, NON qualifiée**, non choisie |
| Essais Railway (Q1-Q7 sur Railway) | **NON EXÉCUTÉS** — exécution soumise à une autorisation explicite et distincte du fondateur |
| Source de vérité | GitHub, `robineaubasile-boop/oryx-api` |
| `main` relu au départ | `5320b928f86962a0341d84c14ab5535c6344c58a` (merge de la PR #241, F04-C V0.3 E-1) |
| Contrats amont | `docs/act_l1_f04a_architecture_v03.md` (§ 6 et § 7), `docs/act_l1_f04c_freeze_v03.md` (§ 3.6, § 10) |

Workflow : architecture / spec → freeze fondateur → implémentation → PR → audit indépendant → merge par le seul
fondateur → validation Railway. Ce lot livre un **banc de diagnostic** et un **protocole** ; il ne touche ni au
runtime de production, ni au résolveur F2, ni à Railway.

Conventions : **[constaté]** fait vérifié dans le dépôt à `5320b92` ; **[doc Railway]** affirmation de la
documentation Railway, relevée le 14/11/2026 via l'outil de recherche documentaire Railway (le site
`docs.railway.com` n'était pas joignable depuis l'environnement de préparation) — une documentation n'est **pas**
une garantie contractuelle et peut changer ; **[local]** résultat d'un test exécuté localement, sans Railway ;
**[à mesurer]** inconnue que seul un essai Railway peut renseigner.

## 2. Question posée

L'option A (F04-A § 7.3) conserve l'infrastructure Railway actuelle à condition de démontrer :

1. une **identification fiable de l'IP client** pour le trafic public passant par l'edge Railway ;
2. qu'une **requête privée** (réseau privé Railway, qui contourne l'edge) **ne peut pas usurper** cette
   identité, ou qu'elle est **refusée** (F04-A § 7.3, condition d'acceptation de l'option A).

Le banc doit établir ce que le serveur peut **observer** et ce qu'il peut **croire**, sans inventer de plage CIDR
Railway (F04-A § 7.1) et sans tenir un en-tête pour authentique du seul fait de sa présence.

### 2.1 Modèle de menace retenu

| Acteur | Chemin | Contrôle | Dans P1-Q |
|---|---|---|---|
| T1 — client Internet | edge public Railway (TLS) | tous les en-têtes de sa requête | Q1-Q4, Q7 |
| T2 — appelant du **même** environnement Railway | réseau privé (WireGuard), **sans edge** [doc Railway] | tous les en-têtes, l'en-tête Host, la famille IP | Q5, Q6 |
| T2' — même appelant passant par le domaine public | edge public | comme T1 | Q6-P |
| T3 — appelant d'un **autre** projet ou environnement | réseau privé d'un autre environnement | — | Q6-C (facultatif) |
| T4 — erreur de configuration Uvicorn / variable d'environnement | `FORWARDED_ALLOW_IPS`, `proxy_headers` | — | Q7 |
| Railway lui-même (edge, maillage, DNS) | — | — | **hors périmètre** : plateforme de confiance ; aucune attaque contre l'infrastructure Railway n'est tentée |

### 2.2 Propriétés qu'un futur résolveur pourrait utiliser

| Propriété | Source | Contrôlée par l'appelant ? |
|---|---|---|
| Pair TCP (`peername` → `scope["client"]` avec `proxy_headers=False`) | noyau / Uvicorn | non (sauf usurpation d'adresse source, hors périmètre) |
| Adresse locale de la connexion (`scope["server"]`) | noyau / Uvicorn | **choisie par l'appelant** parmi les adresses joignables du conteneur |
| Famille de la socket d'écoute (IPv4 / IPv6) | noyau | idem |
| `X-Real-IP`, `X-Forwarded-For`, `Forwarded`, `X-Railway-*` | en-têtes HTTP | **oui**, sauf à démontrer qu'un intermédiaire de confiance les **remplace** et que l'appelant ne peut pas le contourner |

## 3. Architecture expérimentale

### 3.1 Composants (tous temporaires)

```
 poste de l'expérimentateur ──TLS──▶ edge public Railway ──▶ ┐
   (p1q_prober.py --role public)                              │
                                                              ▼
                                        Service A « p1q-receiver » (p1q_receiver.py, Uvicorn 0.27.0)
                                                              ▲
 Service B « p1q-prober » ──réseau privé (sans edge)──────────┘
   (p1q_prober.py --role private)       └──TLS──▶ edge public ──▶ A   (Q6-P)

 Projet Railway NEUF et indépendant ; environnement unique ; aucune base, aucun secret, aucune variable copiée.
```

| Fichier | Rôle |
|---|---|
| `diagnostics/p1q/p1q_receiver.py` | Service A : application ASGI 3 minimale servie par **Uvicorn 0.27.0** (même version, mêmes empreintes que la production) ; rend l'observation de la requête à l'appelant |
| `diagnostics/p1q/p1q_prober.py` | Sonde : client HTTP/1.1 écrit octet par octet (doublons, valeurs mal formées, obs-fold, octets non ASCII), catalogue des cas Q1-Q7, sortie JSONL avec empreinte de collecte |
| `diagnostics/p1q/p1q_analyze.py` | Analyse : faits par question, verdict **proposé** selon le § 5, intégrité de la collecte |
| `diagnostics/p1q/p1q_redact.py` | Classification IANA des adresses et pseudonymisation (§ 3.3) |
| `diagnostics/p1q/p1q_guard.py` | Garde d'isolement (variables interdites, cible de production refusée) |
| `diagnostics/p1q/p1q_main.py` | Lanceur Railway (`P1Q_ROLE=receiver|prober`), durée de vie bornée |
| `diagnostics/p1q/Dockerfile`, `railway.json`, `requirements.txt` | Image `python:3.11.9-slim` (= `runtime.txt`), dépendances verrouillées par empreinte, `restartPolicyType: NEVER` |

Observation rendue par A (jamais journalisée) : pair TCP **brut** relevé par le middleware le plus externe, avant
tout `ProxyHeadersMiddleware` ; `scope["client"]` **effectif** vu par l'application ; `scope["server"]` ; version
HTTP ; schéma ; noms de tous les en-têtes dans l'ordre (doublons compris) ; valeurs de `X-Real-IP`,
`X-Forwarded-For`, `Forwarded`, `X-Forwarded-*`, `X-Railway-*`, `Via` et en-têtes d'IP client usuels ;
**simulation** du `ProxyHeadersMiddleware` **réel** d'Uvicorn 0.27.0 appliqué au pair brut (confiance `127.0.0.1`,
défaut ; confiance `*`) ; correspondance éventuelle avec l'adresse **engagée** par l'expérimentateur (§ 3.3) ;
inventaire de démarrage (adresses locales, résolution de `RAILWAY_PRIVATE_DOMAIN`, sockets d'écoute).

A écoute sur `0.0.0.0:PORT` (comme `api.py`) **et** `[::]:PORT` en `IPV6_V6ONLY` (réseau privé IPv6) ; un échec
IPv6 est rapporté, jamais masqué. Routes : `GET /p1q/observe`, `GET /p1q/health` ; tout le reste 404 ; méthode
autre que GET / HEAD : 405 ; budget de requêtes par démarrage (`P1Q_MAX_REQUESTS`, défaut 5000) puis 503.

### 3.2 Contraintes d'isolement (impératives)

- **Aucun** accès à PostgreSQL Oryx, **aucun** secret Anthropic, EODHD, FMP, Supabase ou Resend, **aucun** appel aux
  services de production Oryx, **aucun** clonage de l'environnement `production`, **aucun** import de module Oryx.
- Garde exécutoire : A et B **refusent de démarrer** (code 3) si une variable `ORYX_*`, `DATABASE_*`, `PG*`,
  `ANTHROPIC*`, `CLAUDE*`, `EODHD*`, `FMP*`, `SUPABASE*`, `RESEND*`, `POSTGRES*`, ou contenant `SECRET`, `PASSWORD`,
  `API_KEY`, `TOKEN`, `CREDENTIAL`, est présente (noms affichés, jamais les valeurs).
- La sonde **refuse** toute cible dont l'hôte contient `oryx-api-production` (code 4).
- Le code n'importe que la bibliothèque standard, `uvicorn` et ses propres modules (test AST
  `tests/test_p1q_bench.py::test_bench_imports_only_stdlib_uvicorn_and_itself`).
- Aucun fichier du runtime (`api.py`, `core/**`, `scripts/**`) ne mentionne le banc ; aucun fichier de
  configuration Railway n'est ajouté à la racine du dépôt (le service servi reste inchangé).
- Le banc n'est **pas** destiné à l'architecture définitive : sa conservation au-delà de P1-Q exigerait une
  décision ultérieure ; le guide prévoit la suppression du projet Railway.

### 3.3 Confidentialité

- **Aucune adresse IP globale** (`ipaddress.is_global`) n'est émise en clair par A : pseudonyme
  `g4:` / `g6:` + 16 hex = HMAC-SHA256 sous une clé **aléatoire par démarrage**, jamais écrite ni exportable
  (`repr` masqué, sérialisation refusée). Stable pendant un démarrage (comparaisons possibles), non reliable entre
  démarrages. Une IPv4 mappée est pseudonymisée sur son IPv4. Dans le texte libre, tout candidat d'adresse
  globale est pseudonymisé ; un groupe IPv6 illisible est remplacé par `<opaque:N>`.
- Les adresses **non globales** (documentation, RFC 1918, ULA, CGNAT, boucle locale, lien local) restent en clair :
  ce sont des adresses d'infrastructure ou les valeurs d'essai contrôlées, objet même de la qualification.
- Les valeurs de `Cookie`, `Authorization`, `Proxy-Authorization`, `X-Api-Key` et des en-têtes hors liste ne sont
  **jamais** rendues (nom, nombre, longueur seulement). Journal d'accès Uvicorn désactivé ; A n'écrit qu'une ligne
  de démarrage sans donnée personnelle ; B n'écrit que des observations déjà pseudonymisées.
- **Adresse de l'expérimentateur** : `--self-ip` n'est jamais transmise en clair. La sonde envoie une clé
  aléatoire par exécution (`X-P1Q-Self-Key`) et l'engagement HMAC-SHA256 de chaque adresse (`X-P1Q-Self-Mac`) ; A
  rend uniquement des booléens. Limite : l'edge Railway voit ces deux en-têtes ; il voit de toute façon l'adresse
  source réelle. Les journaux HTTP de la plateforme Railway relèvent l'adresse source de toute requête publique :
  ce traitement appartient à Railway, comme pour la production, et n'est pas contrôlé par le banc.
- Les fichiers de résultats sont conservés **hors de l'extrait du dépôt** (`$HOME/p1q-results`, guide § 1), donc
  jamais envoyés à Railway ni commités ; `diagnostics/p1q/.gitignore` exclut en garde-fou `*.jsonl` et
  `results/`. Le rapport versionné ne contient que des données pseudonymisées.

### 3.4 Faits documentaires Railway [doc Railway] et ce qu'ils ne prouvent pas

| Affirmation documentaire | Page | Ce que P1-Q doit encore établir |
|---|---|---|
| L'edge ajoute `X-Real-IP` (« for identifying client's remote IP »), `X-Forwarded-Proto` (« always indicates https »), `X-Forwarded-Host`, `X-Railway-Edge`, `X-Request-Start`, `X-Railway-Request-Id` | `docs.railway.com/networking/public-networking/specs-and-limits` | si une valeur fournie par le client est **remplacée**, **conservée**, **ajoutée** ou **dupliquée** (Q2-Q4) ; si `X-Real-IP` dérive d'un `X-Forwarded-For` client (Q3) ; `X-Forwarded-For` n'est pas mentionné |
| « Private networking (`*.railway.internal`) bypasses the edge entirely » | `docs.railway.com/deployments/troubleshooting/slow-deployments` | ce que voit A pour un appel privé (Q5) ; si un appel privé est **distinguable** d'un appel edge par une propriété non contrôlable (Q6) |
| Maillage WireGuard et DNS « scoped to your project and environment » ; services d'un autre projet / environnement injoignables | `docs.railway.com/platform/advanced-concepts`, `docs.railway.com/networking/domains/working-with-domains` | isolement effectif (Q6-C, facultatif) |
| Environnements créés **après le 16/10/2025** : DNS privé IPv4 **et** IPv6 ; environnements antérieurs (« legacy ») : **IPv6 seulement** | `docs.railway.com/networking/private-networking/how-it-works` | voir U-ENV (§ 10) : le projet de diagnostic sera récent ; l'âge de l'environnement de production n'est pas connu |

Aucune page relevée ne décrit les adresses source de l'edge vers le conteneur, ni une garantie de réécriture des
en-têtes fournis par le client. **Aucune plage CIDR Railway n'est utilisée par le banc.**

### 3.5 Constats locaux déjà acquis [local] (Uvicorn 0.27.0, h11 0.16.0, sans Railway)

Établis par `tests/test_p1q_receiver_http.py` contre un vrai serveur Uvicorn en boucle locale :

1. `proxy_headers=False` : `scope["client"]` = pair TCP, quels que soient `X-Forwarded-For`, `X-Real-IP`,
   `Forwarded`, `X-Forwarded-Proto`, **y compris avec `FORWARDED_ALLOW_IPS=*`** dans l'environnement.
2. Réglages par défaut (ceux d'`api.py`, `uvicorn.run(app, host, port)`) : si le pair vaut `127.0.0.1`,
   `scope["client"]` est **réécrit** depuis un `X-Forwarded-For` forgé — y compris une valeur **non IP**
   (`attacker`, port 0) — et, en cas de doublon, le **dernier** en-tête l'emporte (`dict(scope["headers"])`) ;
   `X-Forwarded-Proto` réécrit le schéma. Pair non approuvé : aucune réécriture.
3. `forwarded_allow_ips="*"` : le **premier** élément de `X-Forwarded-For` devient le client (témoin négatif).
4. h11 accepte l'obs-fold (`X-Real-IP: a` + ligne ` b`) et joint les lignes par une espace ; accepte un octet
   non ASCII ; retire les espaces autour d'une valeur ; conserve les doublons et leur ordre dans `scope["headers"]`.

Ces constats qualifient **Uvicorn**, pas Railway. Le point 2 ne prouve aucune faille d'`api.py` sur Railway : il
dépend du pair réel, mesuré par Q1 / Q7 (simulation « confiance 127.0.0.1 »). `api.py` n'est pas modifié.

## 4. Matrice expérimentale

Familles : chaque cas est exécuté en IPv4 et en IPv6 (`--family both`) ; une famille indisponible est consignée
(`family_unavailable`), jamais ignorée. Valeurs forgées : **plages de documentation uniquement** (RFC 5737
`203.0.113.7`, `198.51.100.23`, `192.0.2.44` ; RFC 3849 `2001:db8::7`, `2001:db8:abcd::23`) ou non routables
(`10.1.2.3`, `127.0.0.1`, `::1`, `100.64.0.9`, `fd00::9`, `fe80::1`) : aucune adresse réelle d'un tiers n'est
usurpée. Le catalogue exact est affiché par `python3 diagnostics/p1q/p1q_prober.py --list`.

Notation : « nominal » = valeur `X-Real-IP` du même démarrage et de la même famille en Q1-01, ou, mieux, valeur
reconnue par l'engagement HMAC de `--self-ip`.

### Q1 — Public nominal

| Rubrique | Contenu |
|---|---|
| Procédure | Depuis le poste de l'expérimentateur : `p1q_prober.py --role public --target https://<domaine public de A> --self-ip <IPv4 publique> [--self-ip <IPv6 publique>] --only Q1` ; cas `Q1-01` ×5 par famille ; variante HTTP/2 par curl (guide § 6) |
| Entrées | aucune en-tête de proxy fourni |
| À observer | pair brut (catégorie, stabilité), `scope["server"]`, `X-Real-IP` (nombre, forme, catégorie, égalité avec l'adresse engagée), `X-Forwarded-For` / `Forwarded` ajoutés ou non, en-têtes `X-Railway-*`, version HTTP edge → A, simulation « confiance 127.0.0.1 » |
| PASS | ≥ 5 observations ; `X-Real-IP` présent **une seule fois**, IP nue, **globale**, **égale à l'adresse engagée** à chaque requête, stable par famille |
| FAIL | `X-Real-IP` absent, multiple, illisible, non global, ou différent de l'adresse engagée |
| INCONCLUSIVE | pas d'engagement `--self-ip`, moins de 5 observations, valeur instable sans explication |
| Limites | ne dit rien du trafic privé ; ne prouve pas que l'edge **remplace** une valeur fournie (Q2) ; un réseau de l'expérimentateur derrière NAT / CGNAT / VPN change l'adresse attendue |

### Q2 — Falsification de `X-Real-IP` (public)

| Rubrique | Contenu |
|---|---|
| Procédure | `--role public --only Q2` |
| Entrées | `Q2-01` IPv4 forgée ; `Q2-02` IPv6 ; `Q2-03` `127.0.0.1` ; `Q2-04` RFC 1918 ; `Q2-05` casse du nom ; `Q2-06` espaces ; `Q2-07` liste ; `Q2-08` vide |
| À observer | `X-Real-IP` reçu par A : remplacé (nominal), conservé (valeur forgée), ajouté (liste), dupliqué, supprimé ; réponse de l'edge (statut) si refus |
| PASS | pour **chaque** cas : `X-Real-IP` unique et nominal, **ou** requête refusée par l'edge (4xx, A non atteint) ; au moins un cas nominal |
| FAIL | une seule valeur forgée, ajoutée, multiple, illisible ou absente reçue par A |
| INCONCLUSIVE | aucune référence nominale ; cas non observés sans refus explicite |
| Limites | comportement **observé** à une date, une région edge, un protocole ; pas un engagement de Railway |

### Q3 — Falsification des autres en-têtes (public)

| Rubrique | Contenu |
|---|---|
| Procédure | `--role public --only Q3` |
| Entrées | `X-Forwarded-For` simple / chaîne ; `Forwarded` IPv4 / IPv6 avec port, `proto`, `by` ; valeurs **contradictoires** `X-Real-IP` ≠ `X-Forwarded-For` ≠ `Forwarded` ; `X-Forwarded-Proto/Host/Port` ; `True-Client-IP`, `CF-Connecting-IP`, `X-Client-IP`, `X-Original-Forwarded-For`, `X-Envoy-External-Address`, `X-Cluster-Client-IP` ; `X-Railway-Edge`, `X-Railway-Request-Id`, `X-Request-Start` forgés ; `X-Forwarded-For` sans `X-Real-IP` (dérivation éventuelle) |
| À observer | `X-Real-IP` (critère) ; ce que deviennent XFF, `Forwarded`, `X-Forwarded-*`, `X-Railway-*` (faits) |
| PASS | `X-Real-IP` reste unique et nominal dans **tous** les cas ; les autres en-têtes sont seulement documentés |
| FAIL | `X-Real-IP` dérive d'une valeur fournie par le client (XFF, `Forwarded`, autre) |
| INCONCLUSIVE | référence nominale absente |
| Limites | la conservation d'un XFF ou d'un `Forwarded` forgé n'est **pas** un FAIL : le futur résolveur ne les lit jamais (§ 7) ; elle confirme qu'ils ne sont pas fiables |

### Q4 — Entrées ambiguës (public)

| Rubrique | Contenu |
|---|---|
| Procédure | `--role public --only Q4` |
| Entrées | doublons `X-Real-IP` / XFF / `Forwarded` ; octet hors bornes ; non IP ; port ; crochets ; zone `%eth0` ; IPv4 mappée ; IPv6 non canonique ; zéros non significatifs ; entier décimal ; XFF de 100 entrées ; obs-fold ; octet non ASCII ; CGNAT / ULA / lien local |
| À observer | idem Q2 ; forme exacte de `X-Real-IP` pour un client IPv6 (canonique ? mappée ?) ; refus edge |
| PASS | `X-Real-IP` unique et nominal, ou refus edge, pour **chaque** cas |
| FAIL | une valeur ambiguë, multiple ou contrôlée par le client atteint A dans `X-Real-IP` |
| INCONCLUSIVE | référence nominale absente |
| Limites | l'edge peut normaliser différemment selon HTTP/1.1, HTTP/2, HTTP/3 : la variante curl HTTP/2 n'est qu'un échantillon |

### Q5 — Trafic privé nominal

| Rubrique | Contenu |
|---|---|
| Procédure | Service B, au démarrage : `--role private --target http://${{p1q-receiver.RAILWAY_PRIVATE_DOMAIN}}:8080` (IPv4 et IPv6) ; cas `Q5-01` ×3 |
| À observer | joignabilité par famille ; pair brut ; `scope["server"]` (adresse locale atteinte) ; présence ou absence de `X-Real-IP` et des en-têtes `X-Railway-*` |
| PASS | A est joint en privé ; **aucun** `X-Real-IP` ni en-tête edge n'est ajouté (contournement de l'edge confirmé) |
| FAIL | — (Q5 décrit ; il ne qualifie pas à lui seul) |
| INCONCLUSIVE | A injoignable en privé, ou en-têtes edge présents sur le trafic privé |
| Limites | caractérise un environnement **récent** (U-ENV) ; ne dit rien d'un autre environnement |

### Q6 — Trafic privé avec usurpation (test critique)

| Rubrique | Contenu |
|---|---|
| Procédure | Service B : `Q6-01` (×3) imitation **complète** d'une requête edge (`X-Real-IP` et XFF forgés, `X-Forwarded-Proto: https`, `X-Forwarded-Host` = domaine public de A, `X-Railway-Edge`, `X-Railway-Request-Id`, `X-Request-Start` forgés) ; `Q6-02` idem en IPv6 forgée ; `Q6-03` idem avec `Host` = domaine public ; rejeu privé de tous les cas Q2-Q4 et Q7 ; **Q6-B** : mêmes cas vers les adresses locales de A observées côté edge en Q1 (`P1Q_EXTRA_TARGETS`, adresses **de A seulement**, aucun balayage) ; **Q6-P** : B vers A par le domaine public ; **Q6-C** (facultatif) : depuis un second environnement du projet de diagnostic, tentative de joindre le domaine privé de A |
| À observer | la valeur forgée atteint-elle A ? existe-t-il une propriété **non contrôlable par l'appelant** qui sépare **toutes** les requêtes edge (Q1-Q4) de **toutes** les requêtes privées (Q5-Q6) : D1 catégorie IANA du pair brut ; D2 adresse locale `scope["server"]`, en particulier appartenance aux adresses **auto-découvertes** du domaine privé de A ; D3 famille de la socket d'écoute ; D4 valeurs exactes (rapportée, **jamais retenue** : non généralisable sans CIDR) ; un contournement Q6-B présente-t-il les propriétés edge ? |
| PASS | un discriminateur D1, D2 ou D3 sépare les deux trafics **sur ≥ 2 démarrages de A** de chaque côté, **et** au moins une tentative Q6-B a été exécutée **sans** obtenir les propriétés edge, **et** Q6-P ne conserve pas de valeur forgée. PASS signifie « discriminateur **observé** de manière stable », jamais « garanti par Railway » |
| FAIL | `X-Real-IP` forgé reçu en privé **et** aucun discriminateur ; **ou** contournement Q6-B réussi ; **ou** l'edge conserve une valeur forgée pour un appelant interne (Q6-P) |
| INCONCLUSIVE | discriminateur observé sur un seul démarrage, ou sans essai de contournement ; observations publiques ou privées absentes |
| Limites | un discriminateur observé repose sur un comportement **non documenté** de Railway : il peut changer sans préavis (nouvelle architecture réseau, migration de région, Railway Metal) ; il exigerait une requalification périodique et une sonde de non-régression ; l'usurpation d'adresse source (paquets bruts) est **hors périmètre** et n'est pas tentée |

Conséquence contractuelle : par F04-A § 7.3, l'option A exige la « preuve que le trafic privé ne peut pas usurper
une IP admise ou est refusé ». **Q6 FAIL ⇒ option A non acceptable au sens du § 7.3.** Une acceptation fondée sur
l'hypothèse « aucun appelant privé n'existe dans l'environnement F2 » serait une **révision contractuelle** de
F04-A (modèle de confiance organisationnel), hors du présent protocole, et devrait être explicitement arbitrée.

### Q7 — Configuration Uvicorn

| Rubrique | Contenu |
|---|---|
| Procédure | Redéploiements successifs de A (variables du **seul** service de diagnostic) : (a) `P1Q_UVICORN_MODE=strict`, `FORWARDED_ALLOW_IPS` absent ; (b) `strict` + `FORWARDED_ALLOW_IPS=*` ; (c) `uvicorn_defaults`, `FORWARDED_ALLOW_IPS` absent (réglages d'`api.py`) ; (d) `trust_all_negative_control` ; chaque fois `--only Q7` public et rejeu privé (redéploiement de B ou `railway ssh`) |
| Entrées | XFF simple, terminé par `127.0.0.1`, non IP, dupliqué ; `X-Forwarded-Proto` forgé |
| À observer | `effective_equals_raw` ; pair brut sur Railway (vaut-il jamais `127.0.0.1` ?) ; simulation « confiance 127.0.0.1 » (ce que feraient les réglages d'`api.py`) |
| PASS | (a) et (b) : aucune réécriture ; (d) : réécriture observée (pouvoir de détection du banc démontré) ; complété par les constats locaux § 3.5 |
| FAIL | une réécriture en mode `strict` |
| INCONCLUSIVE | (b) ou (d) non exécutés |
| Limites | qualifie Uvicorn 0.27.0 / h11 0.16.0 ; toute montée de version exige de rejouer Q7 ; (c) est un **constat** sur la configuration d'`api.py`, sans modification |

### Q8 — Qualification du futur résolveur (normatif, non expérimental)

Voir § 7. Aucune implémentation dans ce lot : `core/auth_f2_http/client_ip.py` reste inchangé.

## 5. Critères mécaniques de l'analyseur

`p1q_analyze.py` applique les critères ci-dessus et rend, par question, `PASS`, `FAIL` ou `INCONCLUSIVE` avec les
faits et les raisons, puis une proposition globale :

- `NO-GO` si une question Q1-Q7 est `FAIL` ;
- `GO-CANDIDATE (soumis à l'audit indépendant et à la décision du fondateur)` si Q1-Q7 sont toutes `PASS` ;
- `INCONCLUSIVE` sinon.

L'analyseur ne produit **jamais** un « GO » nu. Il vérifie l'intégrité de chaque collecte (nombre de lignes et
empreinte SHA-256 annoncés par la ligne `P1Q-DIGEST`). Ses verdicts sont des **propositions** : l'audit
indépendant relit les faits bruts, et seul le fondateur décide.

## 6. Matrice finale GO / NO-GO / INCONCLUSIVE pour l'option A

| Résultat | Condition | Conséquence |
|---|---|---|
| **NO-GO A** | Q1, Q2, Q3 ou Q4 FAIL (l'edge ne fournit pas un `X-Real-IP` unique, non falsifiable par un client Internet) | option A abandonnée ; option B (ingress authentifié / isolé) à spécifier |
| **NO-GO A** | Q6 FAIL (usurpation privée indistinguable, ou contournement réussi) | option A non conforme au § 7.3 F04-A ; option B, ou révision contractuelle explicite (hors P1-Q) |
| **NO-GO** de la configuration proposée | Q7 FAIL | réglage Uvicorn à reprendre ; Q1-Q6 à rejouer |
| **INCONCLUSIVE** | au moins une question INCONCLUSIVE, aucune FAIL | essais complémentaires (autre démarrage, autre région edge, IPv6, HTTP/2) ; aucune décision |
| **GO-CANDIDATE A** | Q1-Q7 PASS, collectes complètes, audit indépendant sans réserve bloquante | **proposition** au fondateur : acceptation écrite, nominative et limitée du modèle de confiance (ce que l'edge garantit, ce qu'il ne garantit pas, discriminateur retenu, conditions de requalification) ; puis freeze F04-D. **P1 reste ouverte jusqu'à ce freeze.** |

Même en GO-CANDIDATE, l'option A ne devient jamais une garantie : elle repose sur un comportement observé de
Railway, à requalifier à chaque changement de plateforme, de région, de version d'Uvicorn ou de topologie.

## 7. Q8 — Matrice d'acceptation et de refus attendue pour F04-D (candidat, non implémenté)

Résolveur candidat « edge Railway » (nom provisoire `RailwayEdgeResolver`), **conditionné** à un GO-CANDIDATE.
Principes : refus fermé (`ClientIpUnavailable` → `503 temporarily_unavailable`, aucun appel moteur, aucune
écriture `auth_abuse_*`, aucun fournisseur — F04-A § 7.2) ; Uvicorn en `proxy_headers=False` explicite,
indépendant de `FORWARDED_ALLOW_IPS` ; **aucune** lecture de `X-Forwarded-For`, `Forwarded`, `True-Client-IP`,
`CF-Connecting-IP`, `X-Client-IP` ni d'aucun autre en-tête ; aucune liste CIDR ; la normalisation D2 existante
(`core/auth_f2/normalize.py` : IPv4, ou préfixe IPv6 /64, IPv4 mappée ramenée à IPv4) reste celle du moteur.

| # | Condition d'entrée | Décision attendue |
|---|---|---|
| R1 | discriminateur retenu en Q6 **non satisfait** (requête non attribuable à l'edge) | **refus** |
| R2 | discriminateur satisfait, `X-Real-IP` **absent** | **refus** |
| R3 | `X-Real-IP` présent **plus d'une fois** | **refus** |
| R4 | valeur avec espace, liste, port, crochets, zone `%`, obs-fold, octet non ASCII, vide | **refus** |
| R5 | valeur non IP, octet hors bornes, zéros non significatifs, entier décimal | **refus** |
| R6 | IP nue **non globale** (documentation, RFC 1918, ULA, CGNAT, boucle locale, lien local, non spécifiée, multicast, réservée) | **refus** |
| R7 | IPv4 mappée IPv6 | **à figer** : refus par défaut ; acceptation (ramenée à IPv4) seulement si Q4 montre que l'edge l'émet pour des clients IPv4 réels |
| R8 | IPv6 globale non canonique | **à figer** : refus par défaut ; acceptation seulement si Q4 montre une forme non canonique émise par l'edge |
| R9 | IPv4 globale nue unique | **acceptée** → normalisation D2 `ipv4:<a.b.c.d>` |
| R10 | IPv6 globale canonique unique | **acceptée** → normalisation D2 `ipv6:<préfixe>/64` |
| R11 | `X-Forwarded-For` / `Forwarded` présents, quelles que soient leurs valeurs (y compris contradictoires) | **ignorés** : la décision dépend des seules lignes R1-R10 |
| R12 | pair TCP absent ou illisible | **refus** |
| R13 | `FORWARDED_ALLOW_IPS` défini dans l'environnement | **sans effet** (test : `scope["client"]` inchangé) |
| R14 | requête refusée par F-02A (410 / 404 / 405) ou `GET /health` | **aucun appel** du résolveur ; réponse F-02A octet pour octet (F04-A § 6.3) |

Tests F04-D attendus (nominatifs, à écrire dans le lot F04-D, pas ici) : une ligne de test par ligne R1-R14, en
IPv4 et IPv6, et la preuve qu'aucune écriture anti-abus n'a lieu sur refus.

## 8. Tests

### 8.1 Exécutés localement (sans Railway) — voir le compte rendu de la PR

| Fichier | Objet |
|---|---|
| `tests/test_p1q_bench.py` | pseudonymisation (catégories, adresses globales jamais en clair, IPv4 mappée, texte libre, clé non exportable, engagement HMAC) ; garde d'isolement ; refus de la cible de production ; catalogue (couverture Q1-Q7, valeurs de documentation seulement, octets exacts, imitation edge, adresse de l'expérimentateur jamais en clair) ; exécution privée de bout en bout contre un récepteur local et intégrité de collecte (dont altération détectée) ;
chaîne publique TLS complète contre un « edge » **simulé** (réécriture de `X-Real-IP` → Q1-Q4 PASS ; conservation
d'une valeur forgée → Q2 / Q4 FAIL, proposition NO-GO) — ce simulateur n'est pas un modèle de Railway ; critères de l'analyseur (Q1, Q2/Q4, Q6, Q7, proposition) ; isolement du code (imports, absence de variables Oryx, empreintes identiques à la production, image et `railway.json`, aucune référence du runtime au banc) |
| `tests/test_p1q_receiver_http.py` | vrai serveur Uvicorn 0.27.0 : constats § 3.5, simulation conforme au middleware réel, entrées ambiguës, confidentialité (aucune adresse globale, aucun cookie ni jeton dans la réponse ni dans la sortie du processus), stabilité des pseudonymes, engagement HMAC, routes / méthodes / no-store / budget, rapport honnête de l'écoute IPv6, refus d'un environnement non isolé |

Image : le `Dockerfile` n'a **pas** pu être construit dans l'environnement de préparation (aucun démon Docker).
Son contenu a été reproduit à la place : venv Python 3.11 neuf, `pip install --require-hashes --no-deps -r
diagnostics/p1q/requirements.txt` (click 8.5.0, h11 0.16.0, uvicorn 0.27.0 installés), exécution du récepteur et
de la sonde depuis un dossier ne contenant **que** les fichiers copiés par le `Dockerfile`, avec les variables
d'environnement de l'image Python officielle (garde d'isolement non déclenchée). La construction réelle sera
observée dans les journaux de build Railway (guide § 4).

### 8.2 NON EXÉCUTÉS — exigent Railway et l'autorisation explicite du fondateur

Q1, Q2, Q3, Q4 (edge public, IPv4 / IPv6, HTTP/1.1 et HTTP/2) ; Q5 et Q6 (réseau privé, contournement, Q6-P,
Q6-C) ; Q7 sur Railway (pair réel, modes a-d) ; second démarrage de A (stabilité) ; IPv6 du récepteur (indisponible
dans l'environnement de préparation : l'échec de l'écoute IPv6 y est rapporté, pas testé positivement).

## 9. Coût expérimental estimé

Tarifs relevés le 14/11/2026 [doc Railway] : mémoire **10 $ / Go-mois**, CPU **20 $ / vCPU-mois**, facturés à la
minute sur la consommation réelle (`docs.railway.com/platform/compare-to-northflank`) ; sortie réseau
**0,05 $ / Go** (`docs.railway.com/pricing/understanding-your-bill`) ; **builds gratuits**
(`docs.railway.com/pricing/plans`) ; offre Hobby 5 $ / mois incluant 5 $ d'usage, Pro incluant 20 $
(`docs.railway.com/pricing`). L'usage du projet de diagnostic s'ajoute à celui de la production sur le même compte.

| Hypothèse | Valeur retenue (prudente) |
|---|---|
| Mémoire par service (Python 3.11 + Uvicorn, ou sonde au repos) | 0,15 Go |
| CPU moyen par service | 0,05 vCPU |
| Durée de vie cumulée | 2 services × 6 h (trois démarrages de A pour Q6 / Q7, TTL 4 h par démarrage, arrêt anticipé dès la fin) = 12 service-heures |
| Trafic sortant | < 10 Mo (≈ 500 réponses de ≈ 8 Ko, images non comptées : builds gratuits) |

| Poste | Calcul | Montant |
|---|---|---|
| Mémoire | 0,15 Go × 12 h × 10 $ / 730 h | ≈ 0,025 $ |
| CPU | 0,05 vCPU × 12 h × 20 $ / 730 h | ≈ 0,016 $ |
| Sortie réseau | 0,01 Go × 0,05 $ | ≈ 0,001 $ |
| **Total estimé** | | **≈ 0,05 $** (marge ×10 : **< 0,50 $**) |
| Pire cas « oubli » (sans TTL) | 2 × (0,15 × 10 $ + 0,05 × 20 $) par mois | ≈ 5 $ / mois |

Mesures de maîtrise : TTL par service (`P1Q_TTL_SECONDS`, défaut 4 h, borné à 24 h) suivi d'un arrêt définitif
(`restartPolicyType: NEVER`) ; suppression du projet en fin d'essai (guide § 9) ; vérification de l'usage le
lendemain. **Ne pas** utiliser les « Usage Limits » de Railway pour borner l'essai : elles arrêtent les charges du
compte, donc **aussi la production** (`docs.railway.com/pricing/cost-control`).

Limites de l'estimation : consommation réelle non mesurée (à relever dans l'onglet d'usage pendant l'essai) ;
facturation d'un déploiement arrêté supposée nulle ; tarif et plan du compte non vérifiés dans le compte
lui-même ; image de base `python:3.11.9-slim` non épinglée par empreinte (taille de build sans effet sur le coût).

## 10. Risques et incertitudes ouverts

| Id | Incertitude | Effet | Traitement |
|---|---|---|---|
| U-ENV | L'environnement `production` d'Oryx a-t-il été créé avant le 16/10/2025 (réseau privé IPv6 seul) ? Le projet de diagnostic sera récent (IPv4 + IPv6) | les résultats Q5 / Q6 peuvent ne pas se transposer ; `api.py` n'écoute qu'en IPv4 (`0.0.0.0`) | relever (lecture seule, sans modification) le mode du réseau privé de production avant tout freeze ; à défaut, qualification limitée à un environnement récent |
| U-STAB | Un discriminateur Q6 dépend d'un comportement non documenté | peut disparaître sans préavis | requalification périodique ; sonde de non-régression à prévoir en F04-E |
| U-PROTO | L'edge peut traiter différemment HTTP/1.1, HTTP/2, HTTP/3 | Q2-Q4 partiels | variante curl HTTP/2 ; HTTP/3 si l'outil de l'expérimentateur le permet |
| U-REGION | Comportement par POP edge / région | résultats locaux à une région | noter `X-Railway-Edge` ; idéalement un second point d'entrée (autre réseau, VPN) |
| U-NAT | Réseau de l'expérimentateur derrière CGNAT / proxy | `--self-ip` erroné → Q1 FAIL apparent | vérifier l'adresse publique au moment de l'essai ; en cas d'écart, INCONCLUSIVE et non FAIL après analyse |
| U-REPLICA | Une seule réplique de A ; topologie multi-réplique non testée | discriminateur non qualifié en multi-réplique | le pilote est mono-réplique (F04-A § 10.1) ; requalifier avant tout passage multi-réplique |
| U-LOGS | Limite de taille des lignes de journal Railway | collecte tronquée | empreinte `P1Q-DIGEST` ; en cas d'écart, relancer via `railway ssh` avec `--out` puis récupérer le fichier |
| U-DOC | Documentation Railway lue par l'outil de recherche, non par le site | citations partielles | relire les pages citées au moment de l'essai |
| R-PRIV | L'edge voit les en-têtes d'engagement HMAC ; les journaux HTTP Railway relèvent l'adresse source | traitement par Railway, comme pour la production | consigné § 3.3 ; aucune autre exposition |

## 11. Frontière de ce lot et dormance

- **Ajoutés** : `diagnostics/p1q/**`, `tests/p1q_support.py`, `tests/test_p1q_bench.py`,
  `tests/test_p1q_receiver_http.py`, `docs/act_l1_f04d_p1q_protocol_v01.md`,
  `docs/act_l1_f04d_p1q_railway_guide_v01.md`, `docs/act_l1_f04d_p1q_report_template_v01.md`.
- **Aucun fichier existant modifié** : `api.py` et `api:app`, `core/**` (dont `client_ip.py`, les deux
  `provenance.py`, `f2_runtime/entrypoint.py`, `route_policy/registry.py`), `requirements*.txt`, `alembic/**`,
  `ci/**`, `.github/**`, `web/**`, `web-v2/**` sont inchangés.
- Aucune variable `ORYX_F2_*`, aucune activation F2, aucun changement de la politique F-02A, aucune modification
  de l'authentification, du CSRF, des transactions ou des moteurs.
- Aucun projet, environnement, service, domaine ou variable Railway créé ; aucun déploiement.
- **P1 reste ouverte ; l'option A n'est ni qualifiée ni choisie ; F04-D n'est pas commencé.**
