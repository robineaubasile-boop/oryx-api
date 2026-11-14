# ACT-L1 / F04-D / P1-Q — Rapport des résultats Q1 à Q8 (14/11/2026, campagne 1)

> Copie datée de `docs/act_l1_f04d_p1q_report_template_v01.md`. Toute case non renseignée vaut « NON EXÉCUTÉ ».
> Aucune adresse globale en clair : seuls les pseudonymes `g4:` / `g6:` produits par le banc sont reportés.
> Les verdicts sont des **propositions** de l'analyseur `p1q_analyze.py`, soumises à l'**audit indépendant
> (ChatGPT)** et à la décision du fondateur. **Aucun GO n'est prononcé. P1 reste OUVERTE. L'option A n'est ni
> qualifiée ni choisie.**

## 0. Résumé

| Élément | Constat |
|---|---|
| Proposition globale de l'analyseur | **INCONCLUSIVE** (`option_a_proposal`) |
| Collecte | **prouvée** (`collection.proven = true`) : 6 exécutions privées intègres, 618 lignes, 0 anomalie ; recalcul indépendant des empreintes concordant |
| Questions | Q5 **PASS** (proposé) ; Q1, Q2, Q3, Q4, Q6, Q7 **INCONCLUSIVE** ; aucune FAIL ; Q8 normatif |
| Cause principale | **prérequis non satisfait** : aucune session publique S2 (Q1-Q4, Q7 public, variante curl) n'a pu être exécutée depuis le poste d'exécution (§ 7, E1). Aucun contournement n'a été improvisé |
| Ressources Railway | **non supprimées** (attente de l'audit, instruction du fondateur) ; démarrage strict 3 de A actif jusqu'à son TTL |

## 1. Identification

| Élément | Valeur |
|---|---|
| Date et plage horaire (UTC) de l'essai | 14/11/2026, 09:08:48 → 09:22 UTC (premier `railway up` → dernière collecte) |
| Autorisation du fondateur (référence du message) | messages du fondateur dans la session Claude Code du 14/11/2026 : « J'ai autorisé les expériences Railway » (projet et services préparés par ChatGPT) ; puis fourniture d'un Project Token limité au projet `oryx-p1q-diagnostic` / environnement `p1q` |
| SHA du dépôt déployé (PR auditée) | `155e37706668757a105e5cc7ff6d430c3b776890` (merge de la PR #242 ; = `origin/main` vérifié le 14/11/2026) ; arbre `diagnostics/p1q` sans modification locale |
| Protocole | P1-Q V0.1-R1 (`docs/act_l1_f04d_p1q_protocol_v01.md`) ; guide `docs/act_l1_f04d_p1q_railway_guide_v01.md` |
| Projet Railway / environnement(s) | `oryx-p1q-diagnostic` (`63a7e381-098d-498a-8b6e-5e09dda71012`) / `p1q` (`5b5887ec-fc60-4a1c-a9c0-599f16ec7cbe`) ; services existants `p1q-receiver` (`fb12379c-6960-457d-903a-80b689fca6d5`) et `p1q-prober` (`98778314-8ace-481c-b3df-8e9442d467ff`) ; **aucun service, environnement ou domaine créé** ; aucun dépôt GitHub rattaché |
| Date de création de l'environnement de diagnostic | non exposée par `railway status --json` (champ absent) ; environnement créé le 14/11/2026 par ChatGPT selon le fondateur (donc postérieur au 16/10/2025) ; réseau privé IPv4 **et** IPv6 constaté |
| Mode du réseau privé de production (U-ENV), relevé en lecture seule | **NON RELEVÉ** : le jeton n'a accès qu'au projet de diagnostic ; consigne « ne jamais toucher à la production » |
| Région de déploiement de A (`RAILWAY_REPLICA_REGION`) | `us-west2` (1 réplique) |
| POP edge observé(s) (`X-Railway-Edge`) | `lax1`, `sjc1` (Q6-P, appelant B) ; aucun POP observé pour un client Internet (pas de S2) |
| Version CLI Railway ; écarts de commandes constatés | `railway 5.74.0` ; écarts au § 7 (E3-E6) |
| Réseau de l'expérimentateur (FAI / NAT / VPN, IPv6 disponible ?) | poste d'exécution = conteneur Claude Code distant : **pas d'IPv6**, **pas de résolution DNS externe ni de sortie TCP directe**, sortie HTTPS uniquement par un proxy qui **ré-termine TLS** ; adresse publique inconnue → S2 impossible (§ 7, E1) |
| Démarrages de A (`boot_id`, mode, `FORWARDED_ALLOW_IPS`) | voir tableau 1.1 |
| Exécutions de la sonde | voir tableau 1.1 |
| Collecte prouvée ; anomalies de lecture | `proven = true` ; `notes = []` |
| Liste `blocking` du rapport de l'analyseur | `Q1: INCONCLUSIVE`, `Q2: INCONCLUSIVE`, `Q3: INCONCLUSIVE`, `Q4: INCONCLUSIVE`, `Q6: INCONCLUSIVE`, `Q7: INCONCLUSIVE` |

### 1.1 Démarrages de A et exécutions de B

| # | Déploiement A | `boot_id` | `P1Q_UVICORN_MODE` | `FORWARDED_ALLOW_IPS` (rapportée) | Uvicorn `proxy_headers` / `forwarded_allow_ips` | Exécution B (`run_id`) | Lignes / empreinte | Fichier |
|---|---|---|---|---|---|---|---|---|
| 1 | `b50e280d…` | `61456a136827dc0a` | strict | absente | false / 127.0.0.1 | `499d9a4ae184` | 103/103, OK | `private_strict_boot1.log` |
| 2 | `475bd770…` | `4fef51551371cd71` | strict | absente | false / 127.0.0.1 | `f30cb2c10562` | 103/103, OK | `private_strict_boot2.log` |
| Q7 (b) | `747ea1d4…` | `05ab43246df6341f` | strict | `*` | false / * | `a918200756f3` | 103/103, OK | `private_q7b_strict_fai_star.log` |
| Q7 (c) | `30a53dd3…` | `b5fff3e9380decea` | uvicorn_defaults | absente | true / 127.0.0.1 | `e733f044d7ff` | 103/103, OK | `private_q7c_uvicorn_defaults.log` |
| Q7 (d) | `414324f0…` | `75bf412d3605e680` | trust_all_negative_control | absente | true / * | `782c4865046a` | 103/103, OK | `private_q7d_trust_all.log` |
| 3 | `313ed55a…` | `1545ba0efb49d123` | strict | absente | false / 127.0.0.1 | `f4fa3be5cd12` | 103/103, OK | `private_strict_boot3.log` |

Chaque exécution porte un seul `boot_id` (homogène) ; chaque redéploiement de A n'a été suivi de B qu'après
retrait du déploiement précédent (`REMOVED` / `REMOVING`). Toutes les sorties de B : `P1Q-PROBER-EXIT 0`.

## 2. Inventaire de démarrage de A (ligne `P1Q-STARTUP`)

| Élément | Démarrage 1 (`61456a13…`) | Démarrage 2 (`4fef5155…`) | Démarrage 3 (`1545ba0e…`) |
|---|---|---|---|
| Sockets d'écoute | `0.0.0.0:8080` IPv4 et `[::]:8080` IPv6 `v6only`, aucune erreur | idem | idem |
| Adresses locales | `10.152.169.194` (RFC 1918), `fd12:b301:73a:1:d000:1a8:ab98:a9c2` (ULA), lien local, boucles locales | adresses différentes, mêmes catégories | idem |
| Résolution du domaine privé (A / AAAA) | `10.152.169.194` / `fd12:…:1a8:ab98:a9c2` = adresses locales | `10.254.74.83` / `fd12:…:a9:e5fe:4a53` = adresses locales | **`10.232.8.80` / `fd12:…:e9:bde8:850` = adresses du démarrage précédent (Q7 d)**, alors que B atteint `10.207.235.89` / `fd12:…:18e:bcf:eb59` |
| Réglages Uvicorn | `proxy_headers=false`, `forwarded_allow_ips=127.0.0.1` (défaut), variable absente | idem | idem |

Fait notable (F-DNS) : la résolution du domaine privé relevée **au démarrage** était périmée (adresse du conteneur
précédent) pour 2 démarrages sur 6 (Q7 d et strict 3). Les adresses privées de A changent à chaque démarrage.

## 3. Résultats par question

Couverture : 6 sessions privées complètes ; **0 session publique** (S2) ; 0 observation curl.

### Q1 — Public nominal

NON EXÉCUTÉ (prérequis E1). `missing` : « Q1 : aucune session ».

Verdict proposé : **INCONCLUSIVE** · Verdict retenu : **INCONCLUSIVE** · Limites : les observations Q6-P
(appelant B via l'edge, ci-dessous) ne remplacent pas Q1 : autre appelant (infrastructure Railway), IPv4 seule,
aucune adresse engagée par `--self-ip`.

### Q2 — Falsification de `X-Real-IP`

NON EXÉCUTÉ côté public (E1). `missing` : « Q2 : aucune session ». Rejeu privé des cas Q2 : observé dans les 6
sessions (fait relevant de Q6).

Verdict proposé : **INCONCLUSIVE** · Verdict retenu : **INCONCLUSIVE**.

### Q3 — Autres en-têtes

NON EXÉCUTÉ côté public (E1). `missing` : « Q3 : aucune session ».

Verdict proposé : **INCONCLUSIVE** · Verdict retenu : **INCONCLUSIVE**.

### Q4 — Entrées ambiguës

NON EXÉCUTÉ côté public (E1). `missing` : « Q4 : aucune session ». Aucune donnée sur la forme d'`X-Real-IP` émise
par l'edge pour un client IPv6 ou IPv4 mappé.

Verdict proposé : **INCONCLUSIVE** · Verdict retenu : **INCONCLUSIVE**.

### Q5 — Privé nominal

| Fait | IPv4 | IPv6 |
|---|---|---|
| Joignabilité | A joint par `p1q-receiver.railway.internal:8080` dans les 6 sessions (47 observations / session) | idem (47 / session) |
| Pair brut (catégorie, valeur si non globale) | `private_rfc1918` (p. ex. `10.172.131.176`, change à chaque démarrage de B) | `unique_local` (`fd12:b301:73a:1:…`) |
| `scope["server"]` atteint ; domaine privé auto-découvert ? | adresse privée IPv4 de A ; égale à la résolution de démarrage pour 4 démarrages sur 6 (cf. F-DNS) | adresse ULA de A ; idem |
| `X-Real-IP` / en-têtes edge présents (Q5-01) ? | **aucun** (0 / 18 requêtes Q5-01 IPv4 sur 6 sessions) | **aucun** (0 / 18 requêtes Q5-01 IPv6) |

Verdict proposé : **PASS** (6 sessions, 6 complètes) · Verdict retenu : **PASS (proposé, à auditer)** ·
Limites : environnement **récent** uniquement (U-ENV non relevé) ; Q5 seul ne prouve rien pour Q6.

### Q6 — Privé avec usurpation (critique)

| Fait | Valeur |
|---|---|
| `X-Real-IP` forgé reçu par A en privé | **60** observations (`spoofed_x_real_ip_reaches_application`) ; p. ex. Q6-01 : `X-Real-IP: 203.0.113.7` reçu tel quel, en IPv4 et IPv6, dans chaque session — l'appel privé contourne l'edge, comme documenté |
| D1 catégorie du pair | privé : `private_rfc1918`, `unique_local` ; public (client Internet) : **non observé** ; `separates: false` (faute de public). Fait Q6-P : pair brut **`shared_cgnat`** (`100.64.0.1` à `100.64.0.8`) pour tout trafic edge → A observé |
| D2 adresse locale | privé : adresses privées de A ; public : non observé ; `separates: false`, `rule_self_discovered: false`. Fait Q6-P : l'edge atteint la **même** adresse IPv4 privée de A que le trafic privé (p. ex. `10.152.169.194` au démarrage 1) → D2 ne semble pas discriminant (à confirmer par S2) |
| D3 famille d'écoute | privé : 4 et 6 ; public : non observé ; Q6-P : 4 |
| D4 valeurs exactes | non retenu (pas de règle sans CIDR) |
| Démarrages couverts | privé complet : 4 démarrages stricts (`61456a13…`, `4fef5155…`, `05ab4324…`, `1545ba0e…`) ; public : **0** |
| Q6-B | `bypass = {}` pour chaque démarrage : aucune adresse edge relevée faute de session publique → § 8.1 **non exécutable** (aucune cible à fournir) |
| Démarrages qualifiants / requis | **0 / 2** |
| Imitations Q6-01 ×3, Q6-02, Q6-03 | observées en IPv4 **et** IPv6 dans les 6 sessions (`private_missing = []`) |
| Q6-P (B → domaine public → edge → A) | IPv4 complet dans chaque session (`q6p_missing = []`) : Q0-ID, Q1-01 ×5, Q2-01, Q3-05 ; `X-Real-IP` **unique**, global (pseudonyme constant par démarrage, p. ex. `g4:a13ef815ff399f49` au démarrage 1) ; Q2-01 : la valeur forgée `X-Real-IP` **n'atteint pas** A (remplacée) ; Q3-05 : XFF forgé remplacé par `"<pseudonyme X-Real-IP>, <second pseudonyme global variable>"` ; **`Forwarded: for=192.0.2.44` forgé conservé** ; HTTP/1.1 edge → A ; IPv6 : `family_unavailable` (sortie IPv6 de B désactivée, non activée conformément au guide) |
| Q6-C (facultatif) | NON EXÉCUTÉ (exigerait la création d'un environnement `p1q-iso` ; hors consigne « ne pas créer de nouveaux services ») |

Verdict proposé : **INCONCLUSIVE** · Verdict retenu : **INCONCLUSIVE** · `missing` : pour chaque démarrage
strict, « session publique Q1-Q4 complète absente » et « aucun discriminateur commun » ; « 0/2 démarrage(s)
strict(s) complet(s) ». Limites : un discriminateur D1 (CGNAT côté edge / RFC 1918-ULA côté privé) **n'est qu'une
hypothèse** tirée de Q6-P ; il n'est pas établi pour des clients Internet et reposerait sur un comportement non
documenté de Railway. F-DNS fragilise toute règle D2 fondée sur la résolution de démarrage.

### Q7 — Configuration Uvicorn

| Mode | `FORWARDED_ALLOW_IPS` | Observations (privé : IPv4 / IPv6 ; public : —) | Réécritures de `scope["client"]` (privé) | Pair brut = `127.0.0.1` ? |
|---|---|---|---|---|
| (a) strict | absente | 30 (3 sessions × Q7-01..05 × 2 familles) ; public **0** | **0** | non (RFC 1918 / ULA) |
| (b) strict | `*` | 10 ; public **0** | **0** | non |
| (c) uvicorn_defaults | absente | 10 ; public **0** | **0** (pair non approuvé par la confiance 127.0.0.1) | non |
| (d) trust_all_negative_control | absente | 10 ; public **0** | **8** : Q7-01 → `203.0.113.7`, Q7-02 → `203.0.113.7`, Q7-03 → `attacker` (non IP), Q7-04 → `198.51.100.23` (dernier doublon), en IPv4 et IPv6 ; Q7-05 sans réécriture du client (schéma seul) | non |

Constats locaux du protocole § 3.5 : point 1 (`proxy_headers=False` insensible à `FORWARDED_ALLOW_IPS=*`)
**confirmé en privé sur Railway** (mode b) ; point 3 (témoin `*`) **confirmé en privé**, avec réécriture vers une
valeur non IP ; point 2 : sur Railway, le pair privé n'est jamais `127.0.0.1`, donc aucune réécriture en mode (c)
pour le trafic privé ; le pair edge (Q6-P) n'est pas non plus `127.0.0.1` (CGNAT) — pour le trafic public réel,
**à mesurer** (S2).

Verdict proposé : **INCONCLUSIVE** · Verdict retenu : **INCONCLUSIVE** · `missing` : « configuration (a)…(d) :
aucune session public ». Aucune réécriture en mode strict (aucune FAIL).

### Q8 — Matrice du futur résolveur

Normatif (`DEFINED`), non expérimental. R7 / R8 : **non figeables** (Q4 non exécuté : forme émise par l'edge
inconnue) → refus par défaut maintenu. R1 : **aucun discriminateur établi** (Q6 INCONCLUSIVE) ; D1 reste une
hypothèse à éprouver. Aucune implémentation.

## 4. Synthèse GO / NO-GO / INCONCLUSIVE (protocole § 6)

| Question | Proposé | Retenu |
|---|---|---|
| Q1 | INCONCLUSIVE | INCONCLUSIVE |
| Q2 | INCONCLUSIVE | INCONCLUSIVE |
| Q3 | INCONCLUSIVE | INCONCLUSIVE |
| Q4 | INCONCLUSIVE | INCONCLUSIVE |
| Q5 | PASS | PASS (à auditer) |
| Q6 | INCONCLUSIVE | INCONCLUSIVE |
| Q7 | INCONCLUSIVE | INCONCLUSIVE |
| **Option A** | **INCONCLUSIVE** | **INCONCLUSIVE — aucune décision** |

Aucun GO n'est prononcé par ce rapport. **P1 reste ouverte.** Résultats soumis à l'audit indépendant ChatGPT.

## 5. Coût et ressources constatés

| Poste | Estimé (protocole § 9) | Constaté |
|---|---|---|
| Service-heures | 12 | à relever ; A et B actifs depuis 09:08 / 09:10 UTC, arrêt automatique au TTL (A : 4 h après le dernier démarrage, ≈ 13:20 UTC ; B : 3 h, ≈ 12:20 UTC) |
| Mémoire / CPU moyens | 0,15 Go / 0,05 vCPU | NON RELEVÉ (limites configurées : 0,5 vCPU / 500 Mo par service) |
| Montant | ≈ 0,05 $ | NON RELEVÉ (onglet d'usage, le lendemain ; `railway usage` est au niveau de l'espace de travail, non consulté) |

## 6. Suppression des ressources

| Vérification | Résultat |
|---|---|
| Projet `oryx-p1q-diagnostic` supprimé | **NON** — conservé sur instruction du fondateur jusqu'à sauvegarde et présentation pour audit |
| Domaine public de A injoignable | sans objet (projet conservé) |
| Projet de production inchangé | aucune commande n'a visé la production ; le jeton utilisé est un jeton **de projet** limité à `oryx-p1q-diagnostic` / `p1q` ; contrôle visuel à faire par le fondateur |
| Fichiers de résultats | archivés hors dépôt (`p1q-results`, archive remise au fondateur) ; à effacer après audit |

## 7. Écarts au protocole, incidents et réserves

| # | Écart / incident | Effet sur les conclusions |
|---|---|---|
| E1 | **Prérequis public non satisfait** : le poste d'exécution n'a ni IPv6, ni DNS externe, ni sortie TCP directe ; sa seule sortie HTTPS est un proxy qui ré-termine TLS. La sonde publique (sockets directes) ne peut pas joindre l'edge ; passer par ce proxy introduirait un intermédiaire hors modèle de menace (réémission des requêtes, en-têtes potentiellement normalisés) : **non tenté** (consigne : pas de contournement). Variante curl HTTP/2 non exécutée pour la même raison | Q1-Q4 INCONCLUSIVE ; Q6 et Q7 incomplets ; aucune décision possible |
| E2 | Q6-B (§ 8.1) non exécutable : aucune adresse edge relevée sans S2 | Q6 INCONCLUSIVE |
| E3 | `railway up` lancé avec `-p` / `-e` explicites (projet / environnement de diagnostic) en plus de `--service`, et `-m` (message) ; répertoire non lié par `railway link` | aucun (même archive `diagnostics/p1q`, `--path-as-root`) |
| E4 | `railway variable delete` (CLI 5.74.0) n'accepte pas `--skip-deploys` ; aucun déploiement automatique constaté ; redéploiement explicite ensuite | aucun |
| E5 | Avertissement CLI : « Config as Code (`railway.json`) is deprecated … keep working until 2026-12-01 » | `railway.json` lu et appliqué (start command, restart NEVER) ; à reprendre avant décembre 2026 pour toute nouvelle campagne |
| E6 | Variable supplémentaire `P1Q_PROBE_ON_START=1` sur `p1q-prober` (non listée au guide § 3), valeur = défaut du lanceur ; non modifiée | aucun (comportement identique ; garde d'isolement non déclenchée) |
| E7 | Second démarrage strict et démarrage 3 réalisés, mais sans S2 | ne comptent pas comme démarrages qualifiants |
| E8 | F-DNS : résolution du domaine privé périmée au démarrage (2/6) | réserve sur une règle D2 « auto-découverte » ; à examiner en audit |
| E9 | Contrôle de santé (`/p1q/health` → `{"status":"ok"}`, marque `x-p1q-receiver`) passé **par le proxy de session** | contrôle de disponibilité seulement, non une preuve |
| E10 | U-ENV non relevé (pas d'accès lecture production) | transposition des résultats privés à la production non établie |
| E11 | Scan de confidentialité : aucune adresse globale en clair dans les preuves ; seul faux positif : `::ffff:203.0.113.7` (IPv4 mappée de documentation, cas Q4) | aucun |

## 8. Preuves remises pour audit (hors Git)

Archive `p1q_results_20261114.tar.gz` (empreinte SHA-256 dans `SHA256SUMS`) : journaux bruts de B
(`private_*.log`), lignes de démarrage de A (`receiver_*.log`), journal de build, rapport de l'analyseur
(`p1q_report.json`), vérification indépendante des empreintes (`integrity_independent.txt`), extraction de faits
(`facts_extract.txt`), scan de confidentialité, journal des opérations. Empreintes SHA-256 des journaux de B :

| Fichier | SHA-256 du fichier | Empreinte `P1Q-DIGEST` |
|---|---|---|
| `private_strict_boot1.log` | `706cf12efc5d1eede0022275ced442194616fad6ac549f62b1652b8dcf3521f5` | `b624fb1874385434f657cbc050ee242ac439e8aee529850f2d10243ce5ad3a17` |
| `private_strict_boot2.log` | `0a7d80d943817ef14d61b16781ed59b83754044ff198735a145a645d7a49588a` | `aa0c818ccbc6771b6422ce1e77201ccbf7c7c7796089714f43b7d647a8f2dbc0` |
| `private_q7b_strict_fai_star.log` | `55c7f37aea87bc09e1fa58bc78e9d8f9508b0028dd4c76d7dcdc562683f33124` | `0e2f873bce2b2a51db050343781762639893242cf70e604e6ea35d0561a8597f` |
| `private_q7c_uvicorn_defaults.log` | `a3422043d66e9654973f80c2b7f410b4e9b7b6c1c6bcc7234892e0f49cb128fe` | `93be5d5d0599796af05f20880eee7e94195fe588316228c32c34bbdc018ef43a` |
| `private_q7d_trust_all.log` | `917427ca6e24902e916a498d230c787424ae38013ce4608bfdb4533e47c8149e` | `e1d224f1a9c32b0b5e357fe21c9d78c2332f1222a94d87cafe8fd48a49e034f9` |
| `private_strict_boot3.log` | `cae3b42c9cd85931614c1b3a004f321252cd00698ff67c4e01ff054c996eac26` | `deb5b894233cfe7cf82fdd59ef92935236a0ab3d459bd4ad4edb78b08005e8f3` |

Rejouer l'analyse : `python3 diagnostics/p1q/p1q_analyze.py private_*.log --out p1q_report.json` (chaque
fichier une seule fois). Tests locaux au SHA audité : 182 passés (Python 3.11, `ci/requirements-ci.txt`).

## 9. Suite possible (décision du fondateur, non engagée)

Pour lever E1, S2 doit être exécutée depuis un poste disposant d'IPv4 **et** d'IPv6 publics, sans proxy TLS, selon
le guide § 5-6, sur un démarrage strict de A **suivi d'une nouvelle exécution de B sur ce même démarrage**, puis
répétée sur un second démarrage, et pour les quatre configurations Q7 (`--only Q7`). Les fichiers publics
s'ajoutent aux journaux privés ci-dessus pour une nouvelle analyse unique.
