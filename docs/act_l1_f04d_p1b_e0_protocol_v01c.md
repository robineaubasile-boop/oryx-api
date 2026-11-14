# ACT-L1 / F04-D / P1-B — Protocole E0 V0.1 révision c : faisabilité expérimentale de B2

| Révision | Date | Objet |
|---|---|---|
| V0.1 | 14/11/2026 | première proposition |
| V0.1 a | 14/11/2026 | premier audit : C1 TLS, C2 anti-rejeu N2, C3 verdicts, C4 couverture, C5 ordre (acceptées) |
| V0.1 b | 14/11/2026 | second audit : C6 sous-requêtes même zone et même compte, C7 sous-requêtes entre zones (acceptées sur le fond) |
| **V0.1 c** | 14/11/2026 | troisième audit (REQUEST CHANGES ciblé) : **C8** points d'entrée alternatifs du Worker, **C9** critère strict d'IP-11b (`global_fetch_strictly_public`) ; arbitrage L-ACC maintenu |

> **Statut : PROPOSITION soumise à la validation finale du freeze expérimental (audit ChatGPT puis fondateur).
> Rien n'est exécuté.** Aucune ressource, aucun secret, aucun code, aucune PR, aucune action GitHub, Railway ou
> Cloudflare. P1-Q V0.1-R1 non modifié. **P1 reste OUVERTE**, l'option A reste INCONCLUSIVE, F2 reste dormant. Un
> GO-CANDIDATE E0 n'autoriserait **que** la rédaction de la spécification F04-D-B2.

## 0. Référentiel

| Élément | Valeur |
|---|---|
| `main` relu (14/11/2026) | `1f0aa3e713722fa041f7c6ffff1e1dfec0cadd23`, inchangé |
| Contrats | F04-A V0.3, F04-B V0.3, F04-C V0.3, P1-Q V0.1-R1, rapport de la campagne 1, plan de la campagne 2 V0.1a |
| Code de référence | `core/f2_runtime/entrypoint.py`, `core/route_policy/gate.py`, `core/auth_f2_http/client_ip.py`, `routes.py`, les deux modules `provenance.py`, `core/f2_runtime/config.py` |
| Orientation | B2 candidate prioritaire ; B1 écartée comme solution autonome ; B3a reportée (repli de transport M-T-TLS) ; B3b non prioritaire |

## 1. Traçabilité des corrections de la révision c

Les corrections C1 à C7 sont acceptées et restent tracées en V0.1 a et V0.1 b. Seules C8 et C9 sont introduites ici ;
aucune autre décision validée n'est modifiée.

| Réserve | Correction | Sections modifiées |
|---|---|---|
| **C8** — points d'entrée alternatifs | Inventaire de **tous** les points d'entrée d'`e0-edge` (Route, Custom Domain, `workers.dev`, URL de version et URL de version aliasées, Previews Workers Builds et `wrangler preview`, sessions `wrangler dev --remote`, versions anciennes d'un déploiement progressif, service bindings, Workers for Platforms, gestionnaires non HTTP). `workers.dev` limité à la **phase E0-1a**. Fermeture explicite (`workers_dev = false`, `preview_urls = false`, Previews et Builds désactivés, aucun alias, une seule version à 100 %) **avant** toute phase ultérieure. **Contrôle de l'hôte d'entrée** (PA14) et **clé et audience par phase** (PA15). Essais **IP-16a** (aucun ancien point d'entrée n'obtient d'attestation), **IP-16b** (un redéploiement ne réactive rien), IP-16c (témoin de configuration). Extension d'**IP-15**, des preuves et des critères de GO. | § 3 (D25 à D29), § 4, § 5.4, § 6.1, § 6.4, § 9.1, § 9.2, § 10.4, § 11, § 12, § 13, § 15, § 16 |
| **C9** — IP-11b | Critère permissif **supprimé**. Toute invocation initiée par un autre Worker, y compris par `global_fetch_strictly_public`, ne doit **jamais** produire d'attestation. PASS : `e0-edge` non invoqué, ou invoqué sans attestation. FAIL : toute attestation émise. Une IP différente de la valeur forgée n'est **jamais** assimilée à une provenance visiteur. | § 4, § 5.4, § 6.4, § 10.4, § 11, § 16 |
| L-ACC (maintenu) | Rappel explicite : capacité de déploiement = capacité privilégiée, acceptation écrite du fondateur ; IP-15 et IP-16 sont des **contrôles de détection**, **ni préventifs ni cryptographiques**. | § 5.4, § 15 |

## 2. Objet

### 2.1 Questions

| Id | Question | Nature |
|---|---|---|
| QE1 | Existe-t-il un chemin Worker → Railway dont l'origine est authentifiée par TLS, sans chemin en clair ? | éliminatoire |
| QE2 | Une sous-requête peut-elle attendre 270 s une réponse non diffusée ? | éliminatoire |
| QE3 | L'IP signée est-elle attribuable au visiteur (classe V), jamais à un intermédiaire Cloudflare (I) ni à une valeur falsifiée (F), en IPv4 et IPv6 natives, y compris face à des sous-requêtes de la même zone, du même compte, d'une autre zone, **et par aucun point d'entrée autre que le point d'entrée public figé** ? | sécurité |
| QE4 | Le chemin préserve-t-il Host (ou son substitut contractuel), Origin, Referer, `Sec-Fetch-Site`, les cookies, le corps et la cible, sans récursion, cache, redirection suivie ni fail open ? | sécurité, faisabilité |
| QE5 | Une requête qui contourne le Worker arrive-t-elle sans attestation valide ? | sécurité |
| QE6 | Attestation intacte et unique ; écarts d'horloge, doublons de transport, chevauchements | sécurité, mesure |

### 2.2 Ce qu'E0 mesure et ce que F04-D devra garantir

| Sujet | E0 **mesure** | F04-D **garantit** |
|---|---|---|
| Transport | validation TLS effective, comportement en clair | chemin figé, réglages verrouillés, surveillance |
| Délai | tenue de 270 s à une date | marge et requalification |
| IP | classification V, I ou F observée, invocations par sous-requête | règles de signature, résolveur, règles de refus R |
| Points d'entrée | fermeture effective et persistance après redéploiement | configuration versionnée, contrôle à chaque déploiement |
| Isolement du compte | efficacité d'un inventaire (témoin négatif) | inventaire continu, contrôle d'accès, procédure |
| Rejeu | doublons, chevauchements, écart d'horloge | unicité stricte N2, W et S, panne de base |
| Garde | rien | V-ALL ou V-IP, refus octet pour octet, ordre F04-C |

## 3. Faits documentaires (vérifiés le 14/11/2026) et hypothèses

Railway : outil de recherche documentaire Railway (extraits officiels). Cloudflare : recherche web restreinte à
`developers.cloudflare.com`, résumés d'extraits ; le site n'était pas joignable directement depuis l'environnement de
préparation. **L'audit doit relire les pages.**

| # | Énoncé | Statut | Source |
|---|---|---|---|
| D1 | Railway recommande Full (non strict) pour les domaines proxifiés ; « Strict mode will not work as intended » | FAIT DOC | [Railway SSL](https://docs.railway.com/networking/troubleshooting/ssl#error-526-invalid-ssl-certificate) |
| D2 | Domaine proxifié : certificat pas toujours émis, repli sur `*.up.railway.app` ; un enregistrement proxifié bloque ACME | FAIT DOC | [specs SSL](https://docs.railway.com/networking/public-networking/specs-and-limits#ssl-certificates) |
| D3 | Full ne valide pas l'origine ; Full (strict) exige une chaîne valide et le bon nom | FAIT DOC | [encryption modes](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/) |
| D4 | Sous-requête Worker vers un hôte hors zone non proxifié : toujours Full (strict) | FAIT DOC (témoin TLS-X2) | [error 526](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-526/) |
| D5 | Host = hôte de l'URL ; `resolveOverride` limité à la zone | FAIT DOC | [Request](https://developers.cloudflare.com/workers/runtime-apis/request/) |
| D6 | Remplacements Host, SNI et DNS par Origin Rules : Enterprise | FAIT DOC | [Origin Rules](https://developers.cloudflare.com/rules/origin-rules/) |
| D7 / D7' | Domaine générique Railway avec `_acme-challenge` / validité en Full (strict) une fois proxifié | FAIT DOC / **HYPOTHÈSE** | [wildcard](https://docs.railway.com/networking/domains/working-with-domains#nested-subdomains) |
| D8 | HTTP simple vers Railway : GET redirigé en 301, POST converti en GET ; Flexible = clair vers Railway | FAIT DOC | [traffic types](https://docs.railway.com/networking/public-networking/specs-and-limits#traffic-types) |
| D9 / D9' | `passThroughOnException` = fail open / comportement des Routes au quota | FAIT DOC / **NON TROUVÉ** | [context](https://developers.cloudflare.com/workers/runtime-apis/context/) |
| D10 | Normalisation des URL entrantes activée par défaut (RFC 3986), vers l'origine désactivée | FAIT DOC | [URL normalization](https://developers.cloudflare.com/rules/normalization/settings/) |
| D11 | Route non ciblable par `fetch()` de même zone ; `global_fetch_strictly_public` passe par la porte d'entrée (boucle possible, 1019) | FAIT DOC | [Routes](https://developers.cloudflare.com/workers/configuration/routing/routes/), [errors](https://developers.cloudflare.com/workers/observability/errors/) |
| D12 | `redirect: "follow"` par défaut ; une redirection suivie transmet les en-têtes et Cookie | FAIT DOC | [Request](https://developers.cloudflare.com/workers/runtime-apis/request/) |
| D13 | Pseudo IPv4 Off par défaut, Overwrite = classe E ; IPv6 Compatibility activée par défaut | FAIT DOC | [Pseudo IPv4](https://developers.cloudflare.com/network/pseudo-ipv4/), [IPv6](https://developers.cloudflare.com/network/ipv6-compatibility/index.md) |
| D14 / D14' | 125 s par défaut, Enterprise jusqu'à 6 000 s ; Workers HTTP sans limite murale / application aux sous-requêtes | FAIT DOC / **HYPOTHÈSE** | [524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/), [limits](https://developers.cloudflare.com/workers/platform/limits/) |
| D15 / D15' | Tunnel sortant, 25 répliques / authentification du connecteur et validation HTTPS de l'origine | FAIT DOC / **HYPOTHÈSE** | [Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) |
| D16 | Réseau privé Railway en WireGuard, sans filtrage d'appelants documenté | FAIT DOC | [Railway security](https://docs.railway.com/enterprise/security#encryption-at-rest-and-in-transit) |
| D17 | Custom Domain invocable par `fetch()` depuis la même zone ; WAF custom et managed non évaluées pour ces sous-requêtes | FAIT DOC | [Custom Domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/) |
| D18 | Même zone : `CF-Connecting-IP` reflète `x-real-ip` (modifiable par l'appelant) ; entre zones : `2a06:98c0:3600::103` | FAIT DOC | [HTTP headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/) |
| D19 | `CF-Worker` ajouté à toutes les sous-requêtes `fetch()`, après le WAF ; non-retrait par le code non confirmé | FAIT DOC / **HYPOTHÈSE** | [HTTP headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/) |
| D20 | `cf.worker.upstream_zone` : règle WAF (dès le plan Free) bloquant les sous-requêtes Worker d'autres zones ; sans effet sur la même zone | FAIT DOC | [block Worker subrequests](https://developers.cloudflare.com/waf/custom-rules/use-cases/block-worker-subrequests/) |
| D21 | Route sans origine : AAAA proxifié `100::` ; Cloudflare recommande le Custom Domain pour un Worker servant d'origine | FAIT DOC | [Routes](https://developers.cloudflare.com/workers/configuration/routing/routes/) |
| D22 | `CF-Connecting-IP` et marqueurs vus par un Worker de Route invoqué via `global_fetch_strictly_public` | **HYPOTHÈSE** (non documenté) | à mesurer (IP-11b) |
| D23 | Service bindings : invocation par un Worker du même compte ; contrôle des en-têtes et de `cf` par l'appelant non documenté | FAIT DOC / **HYPOTHÈSE** | [Wrangler config](https://developers.cloudflare.com/workers/wrangler/configuration/) |
| D24 | `request.cf.asn` : métadonnée Cloudflare ; aucune détection documentée des sous-requêtes | FAIT DOC / limite | [Request](https://developers.cloudflare.com/workers/runtime-apis/request/) |
| **D25** | Route `workers.dev` par Worker. `workers_dev` vaut `true` par défaut si la configuration n'a **aucune** Route, `false` sinon. Une désactivation faite **seulement dans le tableau de bord** est **réactivée au déploiement Wrangler suivant** si le fichier Wrangler ne porte pas `workers_dev = false`. | FAIT DOC | [workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/), [Wrangler config](https://developers.cloudflare.com/workers/wrangler/configuration/) |
| **D26** | **URL de version** (anciennement URL de prévisualisation), du type `<préfixe>-<worker>.<sous-domaine>.workers.dev`, et **URL de version aliasées** (`wrangler versions upload --preview-alias`). `workers_dev = false` **ne les désactive pas** : réglage distinct `preview_urls`. Valeur par défaut selon la version de Wrangler (≥ 4.44.0 : alignée sur `workers.dev` ; 4.34.0 à 4.43.x : désactivée ; < 4.34.0 : activée). Clé omise : **réglage existant inchangé**. Désactivées : le routage des URL de version et des alias est coupé. | FAIT DOC | [Version URLs](https://developers.cloudflare.com/workers/versions-and-deployments/version-urls/), [changelog 2025-09-17](https://developers.cloudflare.com/changelog/post/2025-09-17-update-preview-url-setting/), [changelog 2025-10-23](https://developers.cloudflare.com/changelog/post/2025-10-23-preview-url-default-behavior/) |
| **D27** | **Previews** Workers Builds : les poussées sur les branches non productives exécutent `wrangler preview` (par défaut) et créent une Preview avec URL ; réglage `previews_enabled`. Commande `wrangler preview` disponible. | FAIT DOC | [Builds configuration](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/), [Previews](https://developers.cloudflare.com/workers/previews/index.md), [Wrangler commands](https://developers.cloudflare.com/workers/wrangler/commands/workers/) |
| **D28** | `wrangler dev --remote` exécute le Worker dans un environnement de prévisualisation sur Cloudflare. Les requêtes sortantes portent `cf-workers-preview-token` ; les requêtes vers **d'autres zones Cloudflare** sont écartées ; le sort des requêtes vers une destination **hors Cloudflare** (Railway) n'est pas précisé. | FAIT DOC / **HYPOTHÈSE** (destination hors Cloudflare) | [development-testing](https://developers.cloudflare.com/workers/development-testing/), [known issues](https://developers.cloudflare.com/workers/platform/known-issues/) |
| **D29** | Déploiements progressifs : un déploiement peut répartir le trafic entre plusieurs versions (affinité de version). | FAIT DOC (existence) | [version affinity](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/version-affinity/) |

## 4. Modèle de menace

| Acteur | Capacité | Exigence B2 |
|---|---|---|
| T1 — visiteur | tous les en-têtes, IPv4 ou IPv6, HTTP/1.1, 2 ou 3, HTTP ou HTTPS | IP signée de classe V seulement ; jamais d'attestation en clair ; tout en-tête d'attestation entrant est retiré |
| T1' — accès direct à l'origine Railway | contourne Cloudflare | refus (aucune attestation) |
| T1''a — Worker d'une autre zone | `fetch()` vers l'hôte public, en-têtes forgés | classe I : jamais signée |
| **T1''b — Worker de la même zone**, par `fetch()` simple **ou via `global_fetch_strictly_public`** | `x-real-ip` reflété (D18), passage par la porte d'entrée (D11, D22) | **aucune invocation initiée par un Worker ne produit d'attestation (C9)**, quelle que soit l'IP observée |
| T1''c — Worker du même compte par service binding (ou dispatch Workers for Platforms) | invocation directe, en-têtes vraisemblablement contrôlés (D23) | aucun mécanisme technique documenté : limite L-ACC |
| **T1''d — point d'entrée alternatif** : `workers.dev`, URL de version ou alias, Preview, session `wrangler dev --remote`, version ancienne d'un déploiement progressif, Custom Domain résiduel, Route résiduelle d'une autre zone du compte | invoque `e0-edge` hors du point d'entrée public figé | **aucune attestation** hors de l'hôte d'entrée figé et de la clé de la phase en cours (PA14, PA15) ; points d'entrée fermés et inventoriés (C8) |
| T2 — service privé Railway | en-têtes libres, sans edge | refus |
| T-NET — intermédiaire actif Cloudflare → Railway | interception, modification, rejeu | origine authentifiée par TLS |
| T-MIS — mauvais acheminement privé | reçoit le trafic de F2 | M-T seulement en M-T-TLS |
| T4 — erreur de configuration, y compris un Worker ou **un point d'entrée réactivé par un redéploiement** (D25, D26) | — | refus fermé, configuration explicite versionnée, détection par IP-15 et IP-16 |
| T5 — attestation capturée | rejeu, éventuellement avec un autre corps | N2 ; corps non signé en risque résiduel |
| T5' — clé privée du Worker | forge | révocation (F04-D) ; clé distincte par phase en E0 (PA15) |
| T6 — compromission des comptes | — | hors périmètre technique ; tout principal capable de déployer un Worker dans le compte F2 est assimilé à T6 |

**Frontières de confiance.** Confiance accordée à l'edge Cloudflare au point de terminaison TCP du visiteur, à
l'exécution d'`e0-edge` **atteint par son seul point d'entrée figé**, et à Railway en tant que plateforme. Aucune confiance
dans le chemin Internet Cloudflare → Railway (TLS requis), dans toute requête issue d'un autre Worker, dans tout autre
point d'entrée, ni dans tout en-tête non prouvé.

## 5. Chemins de transport, authentification de l'origine et sous-requêtes

### 5.1 Chemins

| Chemin | Description | Validation de l'origine | Host vu par F2 | Contrat F04-A § 6.1 | Sous-requêtes même zone | Statut |
|---|---|---|---|---|---|---|
| **M-X-RT** | Worker sur Route `app.<zone>/*`, AAAA proxifié `100::` ; `fetch("https://<svc>.up.railway.app/…", {redirect:"manual"})` ; **`workers.dev`, URL de version et Previews fermés** | Full (strict) imposé (D4) | `<svc>.up.railway.app` | révision (§ 5.2) | non ciblable par `fetch()` simple (D21) ; `strictly_public` : aucune attestation exigée (IP-11b) | **candidat prioritaire** |
| M-X-CD | idem, Worker sur Custom Domain | idem | idem | idem | invocable (D17), IP reflétée (D18) | à tester ; NO-GO attendu sauf mécanisme démontré |
| M-RW | Route proxifiée, domaine générique Railway, Full (strict) | si D7' | `app.<zone>` | inchangé dans sa forme | comme M-X-RT | conditionnel ; GO impossible en E0 sans TLS-RW2 |
| M-T (M-T-TLS) | Tunnel ; `cloudflared` ; saut privé HTTPS validé | D15', plus TLS privé | configurable | à préciser | selon le Worker frontal | repli |
| M-R | Route, domaine simple, Full (strict) | contraire à D1 et D2 | `app.<zone>` | inchangé | — | non retenu (TLS-R1 facultatif) |
| M-R Full, M-O, `resolveOverride` | — | aucune / Enterprise / sans effet | — | — | — | exclus |

### 5.2 M-X : conséquences contractuelles (inchangées)

- F2 recevrait Host = `<svc>.up.railway.app`, tandis qu'Origin et Referer valent `https://app.<zone>`.
- Révisions nécessaires : F04-A § 6.1 (hôte interne distinct de l'origine publique), les deux provenances (dont le
  bootstrap), F04-B § 2.1 (variable d'hôte interne), hôte public `h` et hôte interne signés.
- Inchangés : CSRF, cookies `__Host-`, F-02A. V-ALL indispensable.

### 5.3 Chemins non chiffrés (inchangés)

- M-X : URL `https://` codée en dur et Full (strict) imposé (TLS-X3).
- M-RW : dépend du réglage de zone ; verrouillage et surveillance obligatoires.
- M-T : saut privé chiffré seulement en M-T-TLS. Cookies `Secure` jamais envoyés en clair (HTTP-02).

### 5.4 Sous-requêtes et points d'entrée : protections comparées

| Protection | Vecteurs couverts | Nature | Démontrable ? | Limites | Statut |
|---|---|---|---|---|---|
| P1 — Route sans origine (M-X-RT) | T1''b par `fetch()` simple | technique, documentée | oui (IP-11a) | ne couvre pas T1''c ; contraire à la recommandation Cloudflare (D21) | retenue |
| P2 — isolement du compte et de la zone (compte dédié, une zone, **un seul script**, aucun service binding, Snippet, projet Pages ni espace Workers for Platforms, jetons limités, MFA, journal d'audit) | T1''b, T1''c, T4 | organisationnelle, plus **détection** par inventaire (IP-15) | inventaire : oui (témoin négatif) ; absence d'acteur malveillant : non | **détection a posteriori, ni préventive ni cryptographique** | retenue, obligatoire |
| P3 — non-signature des classes I et F, **et de toute invocation présentant un marqueur de sous-requête Worker** | T1''a, T1''b (`strictly_public`) | technique, dans `e0-edge` | oui (IP-10, IP-11b) | marqueurs (D19) : **indice de refus seulement**, jamais preuve inverse | retenue |
| P4 — règle WAF `cf.worker.upstream_zone` | T1''a | champ de règles | oui (IP-10b) | sans effet sur la même zone ; 403 Cloudflare au lieu des réponses F-02A pour cette classe | option |
| P5 — marqueur posé par Transform Rule | T1''a | technique | à prouver (IP-10c) | redondant | facultatif |
| P6 — `request.cf.asn` | — | métadonnée | non | — | non retenue (INFO) |
| P7 — `CF-EW-Via` ou `CF-Worker` comme preuve d'absence de sous-requête | — | en-tête | non | jamais une preuve | exclue |
| **P8 — fermeture des points d'entrée alternatifs (C8)** : `workers_dev = false` et `preview_urls = false` **explicites** dans le fichier Wrangler versionné (source de vérité, D25, D26) ; Previews et Workers Builds désactivés, aucune intégration Git ; aucun alias de version ; une seule version servie à 100 % (aucun déploiement progressif, D29) ; aucun Custom Domain ni Route hors de ceux figés ; aucun gestionnaire autre que `fetch` | T1''d, T4 | configuration, plus détection (IP-15, IP-16) | oui (IP-16a, IP-16b, IP-16c) | dépend de la discipline de déploiement ; une modification par le tableau de bord seul peut être annulée ou annuler (D25) | **retenue, obligatoire** |
| **P9 — contrôle de l'hôte d'entrée (PA14)** : `e0-edge` ne signe que si l'hôte de `request.url` est **exactement** l'hôte public figé de la phase | T1''d (`workers.dev`, URL de version, Preview) | technique, dans `e0-edge` | oui (IP-16a) | inopérant contre un appelant qui construit lui-même l'URL (service binding, T1''c) : L-ACC | **retenue** |
| **P10 — clé et audience par phase (PA15)** : clé E0-1a (`aud = oryx-e0-1a`) détruite à la clôture de la phase 1a ; clé E0-2 (`aud = oryx-e0-2`) seule acceptée ensuite par le vérificateur | T1''d (versions et points d'entrée de la phase 1a), T5 | technique et procédurale | oui (IP-16a) | dépend de la destruction effective (preuve § 12) | **retenue** |

**Limite explicite L-ACC (maintenue).**
- Aucun mécanisme technique documenté ne permet à `e0-edge` de distinguer une invocation par service binding d'un Worker
  du même compte (T1''c) d'une requête visiteur ; P9 n'y change rien, l'appelant pouvant fabriquer l'URL.
- La capacité de déployer un Worker, de modifier les points d'entrée ou de lier un service dans le compte dédié est une
  **capacité privilégiée**, assimilée à T6.
- **IP-15 et IP-16 sont des contrôles de détection**, exécutés à des instants donnés. Ils ne préviennent pas une
  compromission du compte et n'en apportent **aucune preuve cryptographique**.
- **Conséquences :** GO-CANDIDATE B2 seulement avec une **acceptation écrite du fondateur** de L-ACC (compte dédié,
  inventaire, contrôle d'accès) ; à défaut, **NO-GO B2**. M-X-CD reste en NO-GO sans détection garantie
  documentairement (IP-11d).

## 6. Propriétés de l'attestation

### 6.1 Propriétés exigées

| Id | Propriété | Conception candidate |
|---|---|---|
| PA1 | authenticité | Ed25519 ; clés publiques seules côté F2 |
| PA2 | algorithme fixe | `v1` implique Ed25519 |
| PA3 | liaison | `v`, `aud`, `kid`, `ts`, `nonce`, `ip`, classe `c` (V seulement), méthode, hôte public `h`, hôte interne (M-X), cible brute transmise ; longueurs préfixées, ASCII |
| PA4 | audience | `aud` et clé par environnement |
| PA5 | fraîcheur | W et S fixés à partir d'AT-R01 |
| PA6 | unicité stricte | N2 ou équivalent |
| PA7 | non-ambiguïté | en-tête unique, grammaire stricte, taille bornée |
| PA8 | refus fermé | consommation du nonce seulement après les vérifications cryptographiques et temporelles |
| PA9 | pas d'inférence abusive | ne prouve ni le corps, ni l'utilisateur, ni la session, ni le CSRF |
| PA10 | indépendance | aucun en-tête d'IP lu par F2 ; `proxy_headers=False` |
| PA11 | jamais en clair | aucune attestation pour une requête visiteur en `http://`, ni sur un saut non authentifié |
| PA12 | signature réservée à la classe V | classes I et F jamais signées |
| PA13 | attestation entrante neutralisée | tout en-tête d'attestation reçu est retiré avant transmission |
| **PA14** | **hôte d'entrée exact (C8)** | signature seulement si l'hôte de `request.url` est égal à l'hôte public figé de la phase (`app.<zone>` en phases 1b et 2 ; hôte `workers.dev` d'`e0-edge` en phase 1a seulement) ; l'hôte d'entrée est inclus dans `h` |
| **PA15** | **clé et audience par phase (C8)** | `aud = oryx-e0-1a` (phase 1a, clé détruite à la clôture) ; `aud = oryx-e0-2` (phases 1b à 2) ; le vérificateur de chaque phase n'accepte que la clé et l'audience de la phase en cours |

### 6.2 Cible signée et normalisation (inchangée)

- Signature sur la cible transmise ; vérification sur la cible brute reçue ; toute divergence entraîne un refus.
- Normalisation de l'edge altérant F-02A : à arbitrer, (a) tolérance documentée ou (b) cible brute stricte (RT-13).

### 6.3 Corps HTTP non signé (inchangé)

- Risque résiduel explicite ; `Content-Length` n'est pas une preuve d'intégrité.
- Intégrité fondée sur le TLS authentifié des sauts. Signer le corps reste une option F04-D, à arbitrer.

### 6.4 Classification de l'IP avant signature

| Classe | Définition | Critère (cumulatif pour V) | Décision |
|---|---|---|---|
| **V** — attribuable au visiteur | pair TCP d'une connexion visiteur à l'edge Cloudflare | `CF-Connecting-IP` unique, nue, globale, différente de l'adresse fixe des Workers ; **aucun marqueur de sous-requête** (`CF-Worker`, `CF-EW-Via`) ; non bloquée par P4 si activée ; pas de classe E ; requête reçue en HTTPS ; **hôte d'entrée exact (PA14)** | **signée** |
| I-a — substitution Cloudflare | `2a06:98c0:3600::103`, ou autre adresse de substitution documentée (liste versionnée, jamais inventée) | égalité exacte | non signée |
| **I-b — invocation initiée par un Worker** (inter-zones, même zone, y compris `global_fetch_strictly_public`) | marqueur de sous-requête présent, blocage P4, **ou** invocation produite par un Worker de test dans IP-10 ou IP-11 | présence, ou provenance de l'essai | **non signée, quelle que soit l'IP observée (C9)** |
| I-c — relais opéré par Cloudflare (WARP, Zero Trust Gateway, Private Relay selon l'opérateur) | pair TCP réel mais partagé | IP-14 | à arbitrer ; en E0 : non signée, mesurée |
| **I-d — point d'entrée non figé** (C8) | hôte d'entrée différent de l'hôte public figé de la phase | PA14 | **non signée** |
| F — falsifiée ou ambiguë | absente, multiple, illisible, non globale, mappée, classe E, différente de l'adresse engagée lors des essais, ou valeur forgée observée | — | non signée |

**Règle C9.** Une IP **simplement différente** de la valeur forgée par un appelant ne prouve pas une provenance visiteur. Toute
invocation initiée par un Worker est de classe I-b et n'est jamais signée.

**Mode de refus (inchangé).** Transmission **sans attestation** et retrait de tout en-tête d'attestation entrant ; l'origine
applique V-ALL (503 octet pour octet) ; les réponses F-02A restent inchangées pour les routes non F2. Option P4 : 403
Cloudflare, décision fondateur. Ne jamais signer une adresse I (compte anti-abus mutualisé, épuisable par un tiers).

## 7. Anti-rejeu (inchangé, C2 accepté)

- N1 non qualifiable ; N3 non équivalent. **N2** prioritaire : table dédiée (`aud`, `kid`, `nonce_digest`,
  `expires_at`, clé unique), sans IP ni chemin ; `INSERT … ON CONFLICT DO NOTHING RETURNING` dans une transaction courte
  en `READ COMMITTED`, avant la transmission à la porte et après les contrôles purs ; COMMIT incertain, base indisponible
  ou pool saturé : refus (503), jamais de réinsertion.
- Conséquences : migration dans un lot distinct (F04-A § 8.7, F04-B § 9, F04-C § 1) ; précontrôle F04-B § 7 ; nouvelle
  catégorie d'écriture sur acceptation, à autoriser explicitement ; requalification du pool de 7 connexions sur
  PostgreSQL 16 et 18 ; 8e point de soumission F04-C ; P2 (consommation suivie sous l'admission) ; purge hors chemin de
  requête ; latence d'un aller-retour ; empreinte du nonce seulement.
- E0 : mécanisme sur PostgreSQL 16 et 18 jetables (AT-L07 à AT-L09) ; doublons (AT-R04) ; chevauchements (AT-R03,
  informatif).

## 8. Périmètre de la garde (inchangé)

- **V-ALL** candidate privilégiée, indispensable avec M-X : `classify`, admission (503 de maintenance d'abord),
  vérification cryptographique, consommation N2, porte. Refus octet pour octet identique à la maintenance. F-02A inchangé.
- Révisions : F04-A § 6.1, § 6.3, § 5, § 7.2 ; F04-B § 2.1, § 2.2, § 7 ; F04-C § 4 et § 5 ; les deux provenances (M-X) ;
  F-02A ou F04-A § 5 si la normalisation est tolérée. V-IP : repli minimal, insuffisant avec M-X.

## 9. Architecture expérimentale et ordre

### 9.1 Composants (temporaires, PR distincte après le freeze)

| Composant | Rôle | Phase |
|---|---|---|
| `e0-origin` | ASGI sous Uvicorn 0.27.0, `proxy_headers=False` : `/e0/slow`, `/e0/observe`, `/e0/cookies`, `/e0/redirect`, vérificateur de diagnostic **par phase** (PA15), compteur de requêtes en clair, **journal des hôtes d'entrée `h` des attestations reçues** | 1a |
| `e0-edge` | Worker : classification V, I ou F ; signature de la classe V seulement ; PA13, **PA14**, **PA15** ; booléens d'engagement ; origine `https://` codée en dur ; `redirect: "manual"` ; sans `passThroughOnException` ; aucun `fetch()` vers son propre hôte ; **seul gestionnaire exporté : `fetch`** | 1a (`workers.dev`), puis 1b et 2 (Route sans origine, M-X-RT ; Custom Domain **temporaire** pour IP-11d seulement) |
| **configuration `e0-edge`** (versionnée dans la PR du banc) | `workers_dev` : `true` en phase 1a, puis **`false` explicite** ; `preview_urls = false` **explicite** dès la phase 1a ; aucune intégration Git ni Workers Builds ; Previews désactivées ; aucun alias ; version de Wrangler épinglée (D26) ; déploiement à 100 % d'une seule version ; **aucune session `wrangler dev --remote`** avec une clé E0, la clé n'existant que comme secret déployé | toutes |
| `e0-tlsneg` | témoin négatif TLS | 1a ou 1b |
| `e0-cross` | Worker « attaquant » sur `workers.dev` (autre zone) | 2 |
| `e0-samezone` | Worker « attaquant » sur une autre Route de la même zone : `fetch()` simple, puis `global_fetch_strictly_public` | 2, fenêtre bornée, puis suppression |
| `e0-binder` | Worker « attaquant » du même compte avec service binding | 2, fenêtre bornée, puis suppression |
| `e0-inventory` | inventaire **en lecture seule** du compte : scripts et leurs gestionnaires, **réglage `workers.dev` par script**, **réglage des URL de version**, **alias de version**, **réglage des Previews et Previews existantes**, **intégrations Builds et Git**, **déploiements actifs (versions et pourcentages)**, Routes de **toutes** les zones du compte, Custom Domains, service bindings déclarés, Snippets, projets Pages, espaces Workers for Platforms, règles WAF et Transform, membres et jetons | toutes, avant et après chaque fenêtre et chaque déploiement |
| `e0-tunnel` | `cloudflared` (M-T-TLS) | 1c seulement |
| `e0-probe`, `e0-analyze` | sonde (dont la liste des **points d'entrée historiques** de la campagne) et analyse | toutes |
| banc N2 | PostgreSQL 16 et 18 jetables | L |

Isolement et confidentialité repris de P1-Q § 3.2 et § 3.3. Configuration Railway sans `railway.json`. Compte Cloudflare E0
dédié ; Workers « attaquants » présents seulement pendant leur fenêtre, suppression vérifiée par IP-15.

### 9.2 Phases et règles d'arrêt

| Phase | Prérequis | Contenu | Arrêt |
|---|---|---|---|
| E0-0 | aucun | relecture documentaire ; phase L : AT-L01 à AT-L10, HTTP-03 | défaut du banc : correction avant toute ressource |
| E0-1a | compte Cloudflare dédié (`workers.dev`, **seule phase où il est ouvert**), un service Railway, clé `oryx-e0-1a` | M-X : TLS-X1, TLS-X2, TO-C, TO-X, TO-H ; **relevé de tous les points d'entrée créés** (hôte `workers.dev`, identifiants de versions) | FAIL : M-X NO-GO, décision fondateur avant tout domaine |
| **Clôture 1a (C8)** | — | `workers_dev = false` et `preview_urls = false` déployés depuis le fichier versionné ; **destruction de la clé `oryx-e0-1a`** ; vérificateur limité à `oryx-e0-2` ; IP-15 et **IP-16a** | IP-15 ou IP-16a en FAIL : **arrêt**, aucune phase ultérieure |
| E0-1b | zone et domaine (si M-X a passé E0-1a, ou si M-RW est demandé) ; clé `oryx-e0-2` | TLS-X3 ; M-RW : TLS-RW1, TO-RW, TLS-RW2 ; **IP-16b après chaque déploiement** | M-X et M-RW en NO-GO : décision sur 1c |
| E0-1c | Tunnel (décision fondateur) | TLS-T1, TLS-T2, TO-T | NO-GO : NO-GO B2 |
| E0-2 | chemin(s) GO à l'issue d'E0-1 ; poste IPv4 et IPv6 natif | IP-15 et IP-16a d'abord ; puis IP, RT, HTTP, AT-R ; fenêtres « attaquants » bornées suivies d'IP-15 ; **IP-16b après chaque redéploiement** ; IP-16c (témoin) en fin de phase | IP-10, IP-11 (sauf IP-11c, voir § 11), IP-15 ou IP-16 en FAIL : NO-GO du chemin |

## 10. Matrice des essais

**Classes :** SEC (FAIL ⇒ NO-GO du chemin, sans compensation) ; FAISA (FAIL ⇒ NO-GO du chemin) ; IR (absent ⇒ INCONCLUSIVE) ;
INFO (aucun verdict). Règle commune : cellule manquante, famille indisponible, préflight non conforme, adresse non engagée,
**liste de points d'entrée incomplète** ou collecte non intègre donnent INCONCLUSIVE.

### 10.1 TLS (inchangé)

| Id | Classe | Phase | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|---|---|
| TLS-X1 | SEC | 1a | sous-requête validée vers `*.up.railway.app` | échec sur un certificat valide | réseau |
| TLS-X2 | SEC | 1a/1b | témoin invalide refusé | réponse malgré le certificat invalide | aucun témoin autorisé (M-X non GO) |
| TLS-X3 | SEC | 1b | zone en Off, Flexible ou Full : la sous-requête M-X reste en HTTPS validé | en clair ou non validée | — |
| TLS-RW1 | SEC | 1b | certificat valide pour `app.<zone>` en Full (strict) | 526 ou certificat non conforme | émission > 24 h |
| TLS-RW2 | SEC | 1b+ | renouvellement observé, en proxifié | 526 ou expiration | non observé (M-RW non GO) |
| TLS-R1 | SEC | 1b (facultatif) | certificat valide | 526 (attendu) | — |
| TLS-T1 | SEC | 1c | HTTPS privé validé ; témoin faux refusé | témoin accepté | — |
| TLS-T2 | SEC | 1c | — | saut privé en clair : FAIL par construction | — |

### 10.2 TO (inchangé)

- TO-C (contrôle Railway direct à 126, 270 et 285 s, ×3) : FAISA ; un FAIL bloque tous les chemins.
- TO-X, TO-RW, TO-T : GET de 30 à 285 s en 14 paliers, ×3, concurrence ≤ 8 ; POST de 384 Kio à 120, 126, 270 et 285 s, ×3.
  FAISA. PASS : 200 et corps complet à chaque palier. FAIL : 524 ou 5xx reproductible. INCONCLUSIVE : non reproductible, ou
  TO-C non conclusif.
- TO-H : HTTP/1.1 et HTTP/2, FAISA. TO-D : abandon par le client, INFO. Aucune modification de C3, aucun octet anticipé.

### 10.3 HTTP (inchangé)

| Id | Classe | Résumé |
|---|---|---|
| HTTP-01 | SEC | `http://` visiteur : rien ne parvient à l'origine, aucune attestation |
| HTTP-02 | SEC | aucun cookie `__Host-` envoyé ni posé en clair |
| HTTP-03 | SEC (L) | configuration du Worker en `http://` : refus de démarrer ou de transmettre |
| HTTP-04 | INFO | Railway direct en `http://` : 301, POST converti |
| HTTP-05 | INFO (témoin) | zone en Flexible : extraction en clair démontrée (exigence de verrouillage pour M-RW) |

### 10.4 IP — provenance

IPv6 native obligatoire pour IP-01 à IP-08 (sans elle, INCONCLUSIVE). Valeurs forgées : plages de documentation ou non
routables uniquement.

| Id | Classe | Phase | Cas | PASS | FAIL |
|---|---|---|---|---|---|
| IP-01 | SEC | 2 | IPv4 nominal ×5 | classe V ; unique, égale à l'adresse engagée ; signée | absente, multiple, différente ; nominal non signé (FAISA) |
| IP-02 | SEC | 2 | IPv6 native ×5 | idem en IPv6 | différente ; traduction sans preuve côté client |
| IP-03 | SEC | 2 | `CF-Connecting-IP` forgé (IPv4 et IPv6) | nominal signé, valeur forgée absente | valeur forgée vue ou signée |
| IP-04 | SEC | 2 | doublon, liste, vide, obs-fold, non ASCII | nominal unique signé, ou refus explicite de l'edge (400, 413, 431 non marqué) | forgé, multiple ou illisible signé |
| IP-05 | SEC | 2 | `True-Client-IP`, `X-Real-IP`, XFF, `Forwarded`, `CF-Connecting-IPv6`, `Cf-Pseudo-IPv4`, `CF-EW-Via`, `CF-Worker` forgés par le client | nominal signé ; un marqueur forgé par le client entraîne au plus un refus de signer (consigné) | valeur forgée signée |
| IP-06 | SEC | 2 | valeurs contradictoires | nominal signé | forgé signé |
| IP-07 | SEC | 2 | HTTP/2, HTTP/3 si disponible | idem | idem |
| IP-08 | SEC | 2 | 2 sessions à au moins 1 h d'intervalle | idem | idem |
| IP-09 | SEC | 2 | Pseudo IPv4 Off, Add, Overwrite, avec client IPv6 | classe E en classe F, non signée | classe E signée ou acceptée |
| IP-10a | SEC | 2 | `e0-cross` (autre zone) avec `CF-Connecting-IP`, `X-Real-IP`, `True-Client-IP` forgés | aucune attestation ; origine atteinte sans attestation, refus | attestation émise, quelle que soit l'IP |
| IP-10b | SEC | 2 | idem avec P4 | blocage Cloudflare avant `e0-edge` ; aucune attestation | requête signée |
| IP-10c | SEC | 2 (facultatif, si P5) | marqueur de Transform Rule envoyé par le client | marqueur supprimé ou écrasé pour le trafic visiteur ; nominal signé | marqueur client accepté |
| IP-10d | IR | 2 | valeurs de `CF-Connecting-IP`, `CF-Worker` et `asn` pour les sous-requêtes inter-zones | relevées | — |
| IP-11a | SEC | 2 | `e0-samezone`, `fetch()` simple, `x-real-ip` posé. M-X-RT | `e0-edge` non invoqué, ou invoqué sans attestation | attestation émise |
| **IP-11b** | **SEC** | 2 | `e0-samezone` avec **`global_fetch_strictly_public`** vers l'hôte public, `x-real-ip` posé, puis non posé. M-X-RT | **`e0-edge` non invoqué, ou invoqué sans produire d'attestation** | **toute attestation émise pour cette sous-requête**, quelle que soit l'IP qu'elle porte (une IP différente de la valeur forgée ne vaut **jamais** provenance visiteur, C9) |
| IP-11c | SEC (essai de limite) | 2 | `e0-binder` par service binding, en-têtes, URL et `cf` forgés | aucune attestation (non attendu) | attestation émise : FAIL par construction attendu, qui matérialise L-ACC (§ 11) |
| IP-11d | SEC | 2 | comparatif M-X-CD : `e0-samezone`, `fetch()` simple, `x-real-ip` posé | aucune attestation, refus fondé sur une détection garantie documentairement | attestation émise ; refus fondé seulement sur D19 : INCONCLUSIVE au mieux (M-X-CD non GO) |
| IP-12 | SEC | 2 | « Remove visitor IP headers » activé | refus de signer | IP absente ou vide signée |
| IP-14 | INFO | 2 | clients WARP et Private Relay | classe observée consignée | — |
| **IP-15** | **SEC** | toutes | inventaire du compte (§ 9.1, `e0-inventory`) : exactement `e0-edge` et ses rattachements figés de la phase ; **`workers.dev` désactivé** hors phase 1a ; **URL de version désactivées** ; **aucun alias** ; **Previews désactivées et aucune Preview existante** ; **aucune intégration Builds ni Git** ; **une seule version déployée à 100 %** ; aucun Custom Domain hors fenêtre IP-11d ; aucune Route d'une autre zone vers `e0-edge` ; aucun service binding hors fenêtre `e0-binder` ; aucun Snippet, projet Pages ni espace Workers for Platforms ; seul gestionnaire `fetch` ; règles WAF et Transform attendues ; **témoin négatif** : l'ajout temporaire d'`e0-samezone` et l'activation temporaire d'un alias de version sont détectés | inventaire conforme avant et après chaque fenêtre et chaque déploiement ; témoins détectés | élément inattendu, suppression non vérifiée, ou témoin non détecté |
| **IP-16a** | **SEC** | clôture 1a, puis début de E0-2 | **aucun ancien point d'entrée n'obtient d'attestation** : requêtes de la sonde (GET et POST, IPv4 et IPv6) vers **chaque** point d'entrée relevé pendant la campagne (hôte `workers.dev` d'`e0-edge`, URL de chaque version connue, alias éventuels, Custom Domain retiré d'IP-11d, Preview éventuelle) ; contrôle à l'origine des attestations reçues et de leur `h` et `aud` | chaque point d'entrée **injoignable** (erreur Cloudflare, `e0-edge` non invoqué) **ou** invoqué **sans** attestation (PA14) ; aucune attestation `oryx-e0-1a` acceptée par le vérificateur de la phase 2 (PA15) | **toute** attestation obtenue par un point d'entrée non figé, ou attestation `oryx-e0-1a` acceptée après la clôture |
| **IP-16b** | **SEC** | après **chaque** redéploiement d'`e0-edge` (1b, 2) | redéploiement depuis le fichier versionné, puis IP-15 et IP-16a | réglages inchangés (`workers.dev` et URL de version désactivés, aucune Preview ni alias) ; IP-16a en PASS | **un point d'entrée réactivé**, ou IP-16a en FAIL |
| IP-16c | INFO (témoin) | fin de E0-2 | redéploiement d'essai avec un fichier **omettant** `workers_dev` et `preview_urls`, sur la version de Wrangler épinglée, puis inventaire, puis restauration et IP-16b | comportement relevé (justifie l'exigence de clés explicites, D25 et D26) | — |

### 10.5 RT — routage et origine, phase 2 (inchangé)

| Id | Classe | Résumé |
|---|---|---|
| RT-01 | FAISA | Host : M-X `<diag>.up.railway.app` ; M-RW `app.<zone>` ; unique et canonique |
| RT-03 | FAISA | une seule requête d'origine, aucun 1019 |
| RT-04 | SEC | Origin, Referer, `Sec-Fetch-*` et Cookie intacts |
| RT-05 | SEC | trois `Set-Cookie` `__Host-` distincts, intacts |
| RT-06 | SEC | 302 rendu tel quel, jamais suivi, attestation jamais transmise |
| RT-07 | SEC | aucun `HIT` de cache |
| RT-08 | SEC | accès direct T1' : aucune attestation valide, refus |
| RT-09 | FAISA | aucun défi ni réécriture sur l'API |
| RT-10 | SEC | corps intact (0, 4 Kio, 384 Kio) |
| RT-11 | INFO | versions HTTP, `Content-Length` ou fragmentation |
| RT-12 | SEC | fail open et fail closed : M-X-RT en erreur vers `100::`, aucune requête non signée acceptée ; M-X-CD : erreur Cloudflare ; aucun `passThroughOnException` |
| RT-13 | SEC et FAISA | cibles encodées, normalisation, paramètres répétés : jamais d'acceptation d'une cible différente de la cible signée ; 27 couples F2 canoniques en PASS |

### 10.6 AT — attestation et rejeu (inchangé, PA14 et PA15 ajoutées à AT-L02)

| Id | Classe | Résumé |
|---|---|---|
| AT-L01 à AT-L05 | SEC | vecteurs croisés ; violations PA2 à PA7, **PA14 et PA15** ; mutations ; consommation après succès ; concurrence dans un processus |
| AT-L06 | INFO (témoin) | limite de N1 |
| AT-L07 à AT-L09 | SEC | N2 sur PostgreSQL 16 et 18 : unicité multiprocessus, COMMIT incertain, saturation (refus fermé) |
| AT-L10 | SEC | PA12 et PA13 : en-tête entrant retiré ; classes I et F jamais signées |
| AT-R01 | IR | écart d'horloge |
| AT-R02 | SEC | attestations valides et uniques |
| AT-R03 | INFO | chevauchement (ne qualifie pas N1) |
| AT-R04 | SEC | doublons de livraison jamais acceptés deux fois |

### 10.7 GD (F04-D, hors E0, inchangé)

GD-01 à GD-11.

## 11. Règles de verdict

**Par chemin P** (M-X-RT, M-X-CD, M-RW, M-T-TLS) :

| Verdict | Condition |
|---|---|
| **NO-GO (P)** | au moins un essai SEC ou FAISA applicable en FAIL démontré, **à la seule exception d'IP-11c** (traité ci-dessous) ; jamais masqué. **IP-11b, IP-15, IP-16a et IP-16b ne bénéficient d'aucune exception.** |
| **GO-CANDIDATE (P)** | tous les essais SEC et FAISA applicables en PASS, dont TLS-X2 (M-X), TLS-RW2 (M-RW), IP-10a, IP-11a, **IP-11b (critère C9)**, **IP-15 étendu**, **IP-16a et IP-16b après le dernier déploiement**, et IP-11d pour M-X-CD ; IPv6 native couverte ; essais IR présents ; liste des points d'entrée complète ; collecte intègre ; audit sans réserve bloquante ; **et**, si IP-11c est en FAIL, acceptation écrite de L-ACC par le fondateur |
| **INCONCLUSIVE (P)** | aucun FAIL bloquant, au moins une condition de GO manquante (dont L-ACC non encore arbitrée, ou liste de points d'entrée incomplète) |
| IP-11c en FAIL sans acceptation de L-ACC | NO-GO (P) |

**Global B2 :**

| Verdict | Condition |
|---|---|
| **GO-CANDIDATE B2** | au moins un chemin en GO-CANDIDATE, avec ses révisions contractuelles, la décision N2, la classe I-c, la règle P4 et **la politique de fermeture des points d'entrée** à figer en F04-D |
| **NO-GO B2** | tous les chemins tentés en NO-GO, les autres exclus ou refusés ; en particulier, refus de L-ACC par le fondateur |
| **INCONCLUSIVE** | sinon |

**Invariants.**
- Un transport d'origine non authentifié ne conduit jamais à un GO-CANDIDATE.
- Une attestation valide portant une IP contrôlée par un Worker appelant, une adresse de classe I, ou **émise pour une
  invocation initiée par un Worker (y compris `global_fetch_strictly_public`)** est toujours un FAIL de sécurité.
- **Toute attestation obtenue par un point d'entrée autre que le point d'entrée public figé de la phase est un FAIL de
  sécurité.**
- Les essais INFO ne produisent jamais de verdict.
- IP-15 et IP-16 sont des contrôles de détection : leur PASS ne vaut **ni** prévention **ni** preuve cryptographique
  d'absence de compromission du compte (L-ACC).

**Contrôle de cohérence de la révision c.**
1. Chaque essai SEC ou FAISA a un PASS, un FAIL et un cas INCONCLUSIVE définis (règle commune comprise).
2. IP-11c est la seule exception au NO-GO, et elle est conditionnée à L-ACC.
3. IP-11b ne comporte plus de branche « IP non contrôlée » : il n'existe plus de PASS avec attestation.
4. Les essais INFO (AT-L06, AT-R03, TO-D, RT-11, HTTP-04, HTTP-05, IP-14, IP-16c) n'entrent dans aucune condition de GO
   ni de NO-GO.
5. L'ancienne question 6 de la V0.1b (IP-11b) est close par C9 et retirée du § 16.

## 12. Preuves attendues

| Preuve | Contenu |
|---|---|
| Brutes | JSONL, journaux de `e0-origin` (sans IP globale en clair), sorties curl, certificats par chemin, captures du navigateur |
| Intégrité | `SHA256SUMS`, empreintes de collecte, recalcul indépendant |
| Configuration | réglages de zone (SSL, normalisation, Pseudo IPv4, IPv6, Managed Transforms, WAF P4, Transform P5), type de rattachement d'`e0-edge`, drapeaux de compatibilité, absence de `passThroughOnException`, SHA du banc, réglages Railway ; **fichier Wrangler versionné de chaque déploiement** (`workers_dev`, `preview_urls`, Routes) et **version de Wrangler épinglée** |
| Sous-requêtes | pour IP-10 et IP-11 : invocation ou non d'`e0-edge`, valeurs observées (classe seulement), marqueurs, `asn`, décision, présence ou absence d'attestation à l'origine |
| **Points d'entrée (C8)** | **liste horodatée de tous les points d'entrée créés pendant la campagne** (hôte `workers.dev`, identifiants et URL de versions, alias, Custom Domain temporaire, Preview éventuelle) ; pour IP-16a et IP-16b : résultat par point d'entrée (injoignable, invoqué sans attestation, ou FAIL) ; **preuve de destruction de la clé `oryx-e0-1a`** (procédure, horodatage, absence du secret dans l'inventaire) ; résultat du témoin IP-16c |
| Inventaires (IP-15) | exports horodatés avant et après chaque fenêtre et chaque déploiement, incluant les réglages `workers.dev`, URL de version, alias, Previews, Builds et Git, déploiements actifs ; résultat des témoins négatifs ; preuve de suppression des Workers « attaquants » |
| TLS | émetteur, SAN, dates ; témoin négatif ; renouvellement M-RW |
| N2 local | journaux multiprocessus sur PostgreSQL 16 et 18 |
| Analyse | `e0_report.json` (verdicts par chemin puis global, `blocking`, état de L-ACC, **état des points d'entrée**), rapport daté |
| Nettoyage | suppression des Workers, de la zone ou de ses enregistrements, du projet Railway, des jetons, des résultats |

## 13. Risques et incertitudes

| Id | Risque | Traitement |
|---|---|---|
| U-TLS | M-RW et M-R contredits par D1 | M-X en premier |
| U-TLSNEG | pas de témoin négatif autorisé | M-X non GO |
| U-TO | 125 s appliqué aux sous-requêtes | TO dès E0-1a |
| U-HOST | révision de provenance pour M-X | arbitrage avant F04-D |
| U-NORM | normalisation de l'edge | RT-13, arbitrage |
| U-N2 | pool, migration, nouvelle écriture | F04-D, F04-B, F04-E |
| U-V6 | pas d'IPv6 native | IPv6 de la box ou poste distinct ; sinon INCONCLUSIVE |
| U-SAMEZONE | Custom Domain invocable depuis la même zone | M-X-RT préféré ; M-X-CD NO-GO sans détection garantie |
| U-STRICT | `strictly_public` non documenté | IP-11b strict (C9) : aucune attestation admise |
| U-BIND (L-ACC) | service binding du même compte indétectable | compte dédié, inventaire (détection seulement), acceptation fondateur ; sinon NO-GO B2 |
| U-D19 | `CF-Worker` peut-être retirable | jamais une preuve ; indice de refus seulement |
| U-RELAY | WARP et Private Relay classés I-c | IP-14, arbitrage |
| U-REC | recommandation Cloudflare favorable au Custom Domain | Route sans origine justifiée par la sécurité |
| **U-ENTRY** | point d'entrée oublié (version, alias, Preview, Route d'une autre zone) | P8, P9, P10 ; IP-15 et IP-16a ; liste complète exigée, sinon INCONCLUSIVE |
| **U-REDEPLOY** | réactivation par redéploiement ou divergence entre tableau de bord et fichier (D25, D26) | clés explicites dans le fichier versionné, Wrangler épinglé, IP-16b après chaque déploiement, témoin IP-16c |
| **U-REMOTEDEV** | session `wrangler dev --remote` capable d'émettre vers une origine hors Cloudflare (D28) | clé jamais présente hors secret déployé ; interdiction procédurale ; PA14 (hôte d'entrée) ; clé par phase |
| **U-WRANGLER** | comportement par défaut variable selon la version de Wrangler (D26) | version épinglée, consignée dans les preuves |
| U-DOC | sources Cloudflare lues par résumés | relecture par l'audit |
| U-TUN | jeton de tunnel, F-DNS | M-T-TLS seulement |
| R-RELAY | Worker de diagnostic en relais | cible unique, budget, durée de vie bornée |
| R-3P | site de test tiers | autorisation |
| R-ATK | Workers « attaquants » laissés en place | fenêtres bornées, IP-15 avant et après |

## 14. Coûts indicatifs (inchangés)

| Poste | Estimation |
|---|---|
| Phase L | 0 $ |
| Workers Paid | 5 $ pour un mois ; compte dédié (abonnement distinct si un compte existe déjà) |
| Railway, `e0-origin` | < 0,10 $, marge ×10 < 1 $ |
| Zone | Free à 0 $ ; domaine inconnu |
| Règles WAF (P4) | incluses dans le plan Free (5 règles custom) selon la documentation |
| M-RW, M-T-TLS, Enterprise | voir V0.1a ; Enterprise non chiffré |
| Temps | E0-1a : environ 1 h ; clôture 1a : environ 20 min ; E0-2 : environ 4 h (fenêtres « attaquants », inventaires et IP-16 inclus) |

Sources : [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [Railway pricing](https://docs.railway.com/platform/compare-to-northflank#railway-2), [WAF](https://developers.cloudflare.com/waf/).

## 15. Points d'autorisation du fondateur

| Feu vert | Couvre |
|---|---|
| G-E0-F | freeze de ce protocole (après la validation finale) |
| G-E0-I | PR du banc E0 (code de diagnostic et **fichier Wrangler versionné**), audit, merge |
| G-L | phase L |
| G-1a | compte Cloudflare dédié et Workers Paid ; **`workers.dev` ouvert pour la seule phase 1a** ; un service Railway ; clé Ed25519 éphémère `oryx-e0-1a` ; témoin TLS (G-3P si site tiers) |
| **G-1a-CL** | **clôture de la phase 1a** : fermeture de `workers.dev` et des URL de version, **destruction de la clé `oryx-e0-1a`**, création de la clé `oryx-e0-2`, IP-15 et IP-16a |
| G-1b | domaine ou zone de diagnostic ; M-RW si demandé |
| G-1c | Tunnel et M-T-TLS |
| G-2 | phase 2 ; déploiement temporaire d'`e0-cross`, `e0-samezone` (dont `global_fetch_strictly_public`) et `e0-binder` dans des fenêtres bornées ; Custom Domain temporaire pour IP-11d ; alias de version temporaire (témoin IP-15) ; redéploiement d'essai IP-16c ; bascules de réglages de zone, dont P4 et P5 ; poste IPv6 natif ; navigateur réel ; clients WARP et Private Relay |
| G-NET | accès de Claude Code à `api.cloudflare.com`, jeton à portée limitée, inventaire en lecture seule ; sinon, exécution par le fondateur |
| G-N | nettoyage |
| **D-L-ACC (maintenu)** | acceptation écrite, ou refus, de la limite L-ACC : capacité de déployer un Worker, de modifier les points d'entrée ou de lier un service dans le compte dédié = **capacité privilégiée** assimilée à T6 ; IP-15 et IP-16 = **détection, ni prévention ni preuve cryptographique** ; condition de tout GO-CANDIDATE B2 si IP-11c est en FAIL |
| Décisions F04-D (après E0) | chemin retenu ; hôte interne (M-X) ; N2 et migration ; V-ALL ; normalisation ; signature du corps ; classe I-c ; règle P4 ; **politique permanente de fermeture des points d'entrée en production** |

## 16. Questions ouvertes pour la validation finale

1. Questions 1 à 6 de la V0.1a maintenues (révision M-X, témoin tiers, TLS-RW2, N2, normalisation, purge).
2. L-ACC : assimilation à T6 recevable, ou NO-GO de principe ?
3. M-X-RT plutôt que M-X-CD (contraire à la recommandation Cloudflare) : acceptable ?
4. Classe I-c (WARP, Private Relay) : refus, ou signature comme adresse partagée d'un réseau ?
5. Règle P4 (403 Cloudflare au lieu des réponses F-02A pour le trafic Worker inter-zones) : acceptée ?
6. **C8 :** la clé et l'audience par phase (PA15) sont-elles suffisantes pour neutraliser les points d'entrée de la phase
   1a, ou faut-il en plus supprimer le script et le recréer sous un autre nom à la clôture 1a ?
7. **C8 :** faut-il exiger pour la production F04-D l'absence permanente de `workers.dev` et des URL de version (et pas
   seulement leur désactivation), par exemple par un compte sans sous-domaine `workers.dev` actif, si Cloudflare le
   permet (non vérifié) ?

---

**Ce qui a été fait :** relecture de `main` (`1f0aa3e`, inchangé) et vérification documentaire en lecture seule
(`workers.dev`, URL de version, Previews, `wrangler dev --remote`, déploiements progressifs). Aucun code, aucune PR, aucun
secret, aucune ressource, aucune action GitHub, Railway ou Cloudflare. Protocole P1-Q et F2 inchangés.
