# ACT-L1 / F04-D / P1-Q — Plan de la campagne 2 (V0.1, PROPOSITION, NON EXÉCUTÉE)

> **Statut : plan à valider par le fondateur. Rien n'est exécuté.** Aucun déploiement, redéploiement, nettoyage
> ni changement de variable sans feu vert explicite, étape par étape (§ 9). Le protocole figé
> `docs/act_l1_f04d_p1q_protocol_v01.md` (P1-Q V0.1-R1) et le guide `docs/act_l1_f04d_p1q_railway_guide_v01.md`
> ne sont **pas** modifiés. **Aucun GO de l'option A n'est envisagé ni implicite ; P1 reste ouverte.**

## 0. État vérifié au 14/11/2026, 10:05 UTC

| Élément | Constat | Source |
|---|---|---|
| `main` | `155e37706668757a105e5cc7ff6d430c3b776890` (merge PR #242), inchangé depuis la campagne 1 | `git fetch` |
| Rapport de campagne 1 | `docs/act_l1_f04d_p1q_report_20261114.md`, **sur la branche `claude/modest-rubin-lnzgyi` seulement** (commit `9cc0b83`), non fusionné, aucune PR | dépôt |
| Banc | `diagnostics/p1q/**` identique à `155e377` | dépôt |
| Projet de diagnostic | `oryx-p1q-diagnostic` / `p1q` : `p1q-receiver` actif (démarrage strict 3, `1545ba0efb49d123`, TTL ≈ 13:20 UTC), `p1q-prober` actif (TTL ≈ 12:20 UTC) ; arrêt automatique, restart `NEVER` | `railway service list`, lecture seule |
| Accès de Claude Code | jeton **de projet** (diagnostic seulement) ; hôtes autorisés `backboard.railway.com`, `p1q-receiver-p1q.up.railway.app` | environnement Claude Code |
| Mac du fondateur | HTTP 200 IPv4, TLS valide vers le récepteur ; **aucun AAAA** pour le domaine ; **pas d'IPv6 native** (échec vers `ipv6.google.com`) | fondateur |

## 1. Exigences exactes du protocole et prérequis manquants

### 1.1 Ce que le code exige réellement (lu dans `p1q_analyze.py` à `155e377`)

| Exigence | Où | Conséquence |
|---|---|---|
| Familles requises : public **(4, 6)**, privé (4, 6), Q6-P (4) | `REQUIRED_FAMILIES` | toute cellule publique IPv6 `family_unavailable` ⇒ INCONCLUSIVE |
| Q1 : 5 × Q1-01 par famille, `X-Real-IP` unique, global, égal à l'adresse **engagée** (`--self-ip`) pour la version de la valeur | `q1_session` | un `--self-ip` par famille ; adresse non engagée ⇒ INCONCLUSIVE ; adresse différente ⇒ **FAIL** |
| Q2-Q4 : chaque cas × chaque famille requise, nominal ou refus edge 400/413/414/431 non marqué | `header_session` | idem |
| Q6 : **≥ 2 démarrages stricts qualifiants** ; qualifiant = session publique **complète** Q1-Q4 (donc IPv6 comprise) + session privée complète + Q6-P IPv4 + Q6-B + discriminateur commun | `analyze_q6`, `public_matrix_complete` | sans IPv6 publique, **aucun démarrage ne peut qualifier** |
| Q7 : configurations (a)-(d), chacune avec Q7-01..05 en public **et** privé × familles requises ; témoin (d) démontré | `analyze_q7` | sans IPv6 publique, Q7 reste INCONCLUSIVE |
| Collecte : une empreinte `P1Q-DIGEST` par exécution, aucune ligne dupliquée, un seul démarrage par exécution | `verify_collection` | chaque fichier fourni **une seule fois** à l'analyseur |

### 1.2 Prérequis manquants

| # | Prérequis | État | Bloquant pour |
|---|---|---|---|
| P-A | Poste public **sans proxy TLS**, IPv4 publique stable, Python ≥ 3.11 avec un magasin de certificats valide | **disponible** (Mac du fondateur, à contrôler au § 3) | S2 IPv4 |
| P-B | **Entrée IPv6 publique du récepteur** (enregistrement AAAA sur `*.up.railway.app`) | **absente** : aucun AAAA constaté ; la documentation Railway consultée le 14/11/2026 ne décrit que l'IPv6 **sortante** (opt-in) et le réseau **privé** IPv4/IPv6, aucune entrée publique IPv6 | **Q1-Q4, Q6, Q7 : bloquant structurel** (§ 4) |
| P-C | IPv6 publique native sur le poste | absente | utile seulement si P-B existe ; sinon sert à **attribuer** l'absence au récepteur et non au poste (§ 4.3) |
| P-D | Décision du fondateur sur P-B (§ 4.4) | **à prendre** | conditionne l'utilité décisionnelle de la campagne 2 |
| P-E | `railway.json` encore accepté par Railway | jusqu'au **2026-12-01** (coupure ferme) | toute exécution après cette date (§ 7.1) |
| P-F | Moyen de transmettre les fichiers du Mac à Claude Code | pièce jointe dans la session Claude Code (archive `.tar.gz` + `SHA256SUMS`) | analyse croisée |

## 2. Point bloquant principal : l'IPv6 publique

Sans AAAA, la sonde publique obtient, pour `--family both`, une ligne `family_unavailable` (erreur `gaierror`)
pour toute la famille 6. Ce n'est **pas** un FAIL, mais c'est une preuve manquante. **Sous le protocole figé
V0.1-R1, la campagne 2 ne peut donc produire au mieux que : Q5 PASS, Q1-Q4 / Q6 / Q7 INCONCLUSIVE, ou un FAIL
démontré en IPv4** (contre-exemple ⇒ NO-GO). Aucun réglage du Mac ne lève ce blocage : il est côté Railway.

## 3. Exécuter S2 depuis le Mac, directement sur Internet

### 3.1 Pourquoi le Mac convient (et à quelles conditions)

La sonde ouvre des **sockets TCP directes** (`socket.connect`) et vérifie TLS avec le magasin de certificats de
**Python** (pas le trousseau macOS) : elle ignore `HTTPS_PROXY` et toute configuration proxy du système, et un
proxy TLS intercepteur (antivirus « protection web », boîtier d'entreprise) ferait **échouer** la vérification
(`tls_error`, donc INCONCLUSIVE, jamais une fausse observation). Restent à exclure les intermédiaires
**transparents au niveau IP** : VPN, iCloud Private Relay, NAT64 / partage de connexion mobile.

### 3.2 Préparation du Mac (commandes du fondateur, à lancer après feu vert « M0 »)

Python 3.11 (Homebrew, qui embarque son propre magasin de certificats) :

```bash
brew install python@3.11
```

```bash
export PY="$(brew --prefix python@3.11)/bin/python3.11" && "$PY" --version
```

Extrait au SHA audité et dossier de résultats **hors** de l'extrait :

```bash
git clone https://github.com/robineaubasile-boop/oryx-api.git "$HOME/oryx-p1q" && cd "$HOME/oryx-p1q"
```

```bash
git checkout 155e37706668757a105e5cc7ff6d430c3b776890 && git status --porcelain diagnostics/p1q
```

```bash
export RES="$HOME/p1q-results-c2" && mkdir -p "$RES"
```

### 3.3 Contrôles « aucun intermédiaire » (à archiver dans `$RES/preflight.txt`)

Désactiver au préalable : VPN, iCloud Private Relay (Réglages → identifiant Apple → iCloud → Relais privé),
« Limiter le suivi de l'adresse IP » sur l'interface, toute protection web d'antivirus. Utiliser la **box fixe**
(Ethernet ou Wi-Fi), **jamais** un partage de connexion mobile (NAT64 / CGNAT à rotation, § 4.2).

```bash
{ date -u; env | grep -i proxy; scutil --proxy; scutil --nc list; } > "$RES/preflight.txt" 2>&1; cat "$RES/preflight.txt"
```

Attendu : aucune variable `*proxy*`, `HTTPEnable/HTTPSEnable/SOCKSEnable : 0`, aucun service VPN `(Connected)`.

```bash
"$PY" -c "import ssl,socket;h='p1q-receiver-p1q.up.railway.app';s=ssl.create_default_context().wrap_socket(socket.create_connection((h,443),timeout=10),server_hostname=h);print(s.version(),dict(x[0] for x in s.getpeercert()['issuer']))" >> "$RES/preflight.txt" 2>&1; tail -1 "$RES/preflight.txt"
```

Attendu : `TLSv1.3` (ou 1.2) et un émetteur public reconnu ; une erreur de certificat ⇒ arrêter (intermédiaire
ou magasin de certificats absent).

Adresse publique IPv4 (deux sources indépendantes, **avant et après** chaque session ; elles doivent concorder) :

```bash
curl -4 -sS https://api.ipify.org; echo; curl -4 -sS https://ifconfig.me; echo
```

Ne **pas** consigner cette adresse dans `$RES` ni la transmettre à Claude Code : elle ne sert qu'à `--self-ip`.
En cas de discordance ou de changement entre avant / après : session non exploitable (U-NAT), à rejouer.

### 3.4 Variante HTTP/2 (curl du Mac)

```bash
curl -V | grep -o 'HTTP2' | head -1
```

Les commandes du guide § 6 s'appliquent telles quelles (fichiers suffixés par le démarrage, § 5). HTTP/3 n'est
pas disponible dans le curl de macOS : consigné, sans contournement.

## 4. Couverture IPv6 publique

### 4.1 Distinguer une vraie connexion IPv6 d'une IPv4 déguisée

| Situation | Comment elle se manifeste | Effet sur l'analyse | Contrôle |
|---|---|---|---|
| IPv6 de bout en bout (AAAA réel, client IPv6 natif) | la sonde résout un AAAA global ; l'edge voit une IPv6 ; `X-Real-IP` attendu en IPv6 (`g6:`) | cellule exploitable | `X-Real-IP` de version 6 dans une cellule de famille 6 |
| **NAT64 / DNS64** (réseau IPv6 seul, typiquement partage mobile) | `getaddrinfo(AF_INET6)` rend une adresse **synthétisée** (`64:ff9b::/96`, `64:ff9b:1::/48` ou préfixe d'opérateur embarquant l'IPv4 du A) ; l'edge voit l'**IPv4 de la passerelle** | cellule « famille 6 » portant un `X-Real-IP` **IPv4** ; l'analyseur classe selon la version de la **valeur** : avec un `--self-ip` IPv4 différent de la passerelle ⇒ **`not_self` = FAIL artificiel** | refuser toute adresse résolue dans ces préfixes ; comparer `dig AAAA` d'un résolveur public (aucun) et du résolveur local (AAAA synthétique = DNS64) |
| IPv4 mappée (`::ffff:a.b.c.d`) | n'apparaît qu'avec `AI_V4MAPPED` ; la sonde n'utilise pas ce drapeau ; socket IPv6 transportant de l'IPv4 | même risque que NAT64 si elle apparaissait | la sonde résout sans drapeau ; contrôle ci-dessous |
| 464XLAT / CLAT (iPhone en partage) | le Mac reçoit une IPv4 privée traduite ; famille 4 par une passerelle d'opérateur, souvent avec **rotation d'adresse** | X-Real-IP instable ⇒ INCONCLUSIVE ou `not_self` | interdire le partage mobile |

Contrôle préalable (Mac), à archiver :

```bash
for r in 1.1.1.1 8.8.8.8 9.9.9.9; do echo "== $r A: $(dig +short A p1q-receiver-p1q.up.railway.app @$r | tr '\n' ' ') AAAA: $(dig +short AAAA p1q-receiver-p1q.up.railway.app @$r | tr '\n' ' ')"; done | tee "$RES/dns_proof.txt"; echo "== local AAAA: $(dig +short AAAA p1q-receiver-p1q.up.railway.app | tr '\n' ' ')" | tee -a "$RES/dns_proof.txt"
```

```bash
"$PY" -c "import socket;print(socket.getaddrinfo('p1q-receiver-p1q.up.railway.app',443,socket.AF_INET6,socket.SOCK_STREAM))" >> "$RES/dns_proof.txt" 2>&1; tail -1 "$RES/dns_proof.txt"
```

Attendu aujourd'hui : AAAA vide partout et `socket.gaierror` ; la famille 6 sera `family_unavailable`. **Si**
une adresse IPv6 est rendue : la conserver, vérifier qu'elle n'est ni dans `64:ff9b::/96`, ni dans
`64:ff9b:1::/48`, ni dans `::ffff:0:0/96`, et que le résolveur public la rend aussi ; sinon **arrêter**.

### 4.2 Options comparées pour l'IPv6 côté client

| Option | Infra nouvelle ? | Intermédiaire ajouté | Risque de fausse IPv6 / FAIL artificiel | Lève P-B ? | Appréciation |
|---|---|---|---|---|---|
| 1. IPv6 native de la box (activation dans l'interface du FAI) | non | aucun | faible (vérifiable) | **non** | **simple et sûre** ; utile pour la preuve d'attribution (§ 4.3) |
| 2. Partage de connexion mobile 4G/5G | non | passerelle d'opérateur (NAT64, 464XLAT, CGNAT) | **élevé** | non | **à exclure** |
| 3. Tunnel IPv6 (tunnel broker 6in4) | compte tiers | serveur de tunnel | moyen (protocole 41 souvent filtré par la box) | non | complexe, sans bénéfice |
| 4. VPN offrant l'IPv6 | abonnement tiers | sortie VPN | moyen | non | sans bénéfice ; contredit § 3.3 |
| 5. Machine virtuelle cloud double pile | **oui** | aucun (poste distinct) | faible | non | création d'infrastructure, accord requis, sans bénéfice tant que P-B subsiste |
| 6. Domaine personnalisé sur le récepteur | modification Railway | — | — | **non** (le domaine pointe en CNAME vers `*.up.railway.app`) ; la sonde refuse d'ailleurs tout hôte hors `*.up.railway.app` | sans objet |

**Recommandation** : aucune solution côté client ne rend l'IPv6 publique testable tant que le récepteur n'a pas
d'AAAA. La plus simple et la plus sûre est l'**option 1** (IPv6 native de la box, si le FAI la propose), pour une
seule raison : prouver que l'absence de cellules IPv6 tient à Railway et non au poste. Exclure les options 2 à 5.
Ne jamais cibler une adresse IPv6 littérale de l'edge (contournement, et refusé par la sonde).

### 4.3 Preuve d'attribution (à joindre au rapport de campagne 2)

`dns_proof.txt` (AAAA vide sur trois résolveurs publics + résolveur local, `gaierror`) ; si l'option 1 est
active, `curl -6 -sS -m 10 -o /dev/null -w '%{http_code} %{remote_ip}\n' https://ipv6.google.com` réussi le même
jour ; lignes `family_unavailable` (erreur `gaierror`) de chaque session publique.

### 4.4 Décision du fondateur requise (P-D) — deux voies, aucune ne vaut GO

| Voie | Contenu | Effet |
|---|---|---|
| **V1 — protocole figé** | campagne 2 IPv4 telle quelle | résultat **attendu INCONCLUSIVE** (Q1-Q4, Q6, Q7) sauf FAIL ; apporte les faits IPv4 (Q1-Q4, D1 public, Q6-B, Q7 public) ; ne peut pas qualifier l'option A |
| **V2 — révision du protocole (R2) avant la campagne** | spécification → freeze → implémentation → PR → audit ChatGPT → merge, d'une règle explicite : « famille publique 6 **non applicable** si, le jour de l'essai, l'absence d'AAAA est prouvée (§ 4.3) et que toutes les cellules publiques IPv6 sont `family_unavailable` par échec de résolution » ; conséquences à figer : R10 (IPv6 globale acceptée) **non qualifiée** ⇒ refus ou « à figer », et requalification obligatoire dès qu'un AAAA apparaît | rend la campagne 2 potentiellement **décisive** pour Q1-Q4 / Q6 / Q7 ; c'est un **assouplissement** de couverture (protocole § 10, U-COVER) : relève de l'audit et du fondateur, jamais d'une option de l'analyseur |

Recommandation : trancher **avant** la campagne 2. Si une campagne décisive est recherchée, V2 d'abord ; sinon
V1 reste utile pour détecter tôt un FAIL IPv4. La rédaction de R2 n'est pas engagée sans demande.

## 5. Adaptation du banc

| Besoin | Nécessaire ? | Nature | Workflow |
|---|---|---|---|
| Exécuter S2 IPv4 depuis le Mac (V1) | **non** | banc utilisable tel quel (bibliothèque standard, Python ≥ 3.11) | — |
| Garde NAT64 / mappée dans la sonde : marquer `family_translated` (non exploitable) toute résolution IPv6 publique dans `64:ff9b::/96`, `64:ff9b:1::/48`, `::ffff:0:0/96` | **recommandé**, non bloquant si le contrôle manuel § 4.1 est appliqué | code de la sonde + tests | PR distincte, audit, merge |
| Garde dans l'analyseur : cellule de famille 6 portant un `X-Real-IP` IPv4 (ou l'inverse) ⇒ statut « famille traduite », INCONCLUSIVE au lieu de `not_self` | **recommandé** (supprime le FAIL artificiel du § 4.1) | code de l'analyseur + tests de mutation | idem |
| Règle « IPv6 publique non applicable » (V2) | seulement si V2 | protocole + analyseur (version de protocole à définir : un changement de `PROTOCOL_VERSION` rendrait les observations de la campagne 1 non combinables) | spécification → freeze → PR → audit → merge |
| Migration `railway.json` → `.railway/railway.ts` | non avant le 2026-12-01 ; **oui** après (§ 7.1) | fichiers du banc, test `test_p1q_bench.py` (assertion sur `railway.json`) | PR distincte |

Aucune de ces adaptations n'est commencée.

## 6. Séquence complète de la campagne 2 (après feu vert)

Notation : **[F]** commande du fondateur sur le Mac ; **[C]** commande de Claude Code (Railway CLI, projet
`-p 63a7e381-098d-498a-8b6e-5e09dda71012 -e 5b5887ec-fc60-4a1c-a9c0-599f16ec7cbe`, jeton de projet) ; **[G]**
point d'arrêt, feu vert requis. Chaque étape [C] modifiant Railway est listée au § 9.

### 6.0 Préalables

1. **[G0]** Décision V1 / V2 (§ 4.4) ; si V2 : révision fusionnée et SHA audité noté ; date ≤ 2026-11-30.
2. **[F]** M0 : § 3.2-3.4 et § 4.1 ; envoi de `preflight.txt` et `dns_proof.txt` (sans l'adresse IPv4).
3. **[C]** Contrôle lecture seule : `railway status`, `railway variable list` des deux services (attendu : guide
   § 3, `P1Q_UVICORN_MODE=strict`, pas de `FORWARDED_ALLOW_IPS`, `P1Q_EXTRA_TARGETS` absente),
   configuration des services (Dockerfile, `python p1q_main.py`, restart `NEVER`, 1 réplique, IPv6 sortante
   désactivée), SHA de l'extrait local = SHA retenu.

### 6.1 Démarrage strict S-1

1. **[C]** `railway redeploy --service p1q-receiver -p … -e … -y` (si le SHA retenu ≠ code déjà déployé :
   `railway up . --path-as-root --service p1q-receiver -p … -e … --detach` depuis `diagnostics/p1q`) ; attendre
   `SUCCESS` du nouveau déploiement **et** `REMOVED` de l'ancien ; archiver `receiver_c2_s1.log` ; annoncer au
   fondateur le `boot_id` et l'heure de fin de TTL (4 h).
2. **[F]** relever l'IPv4 publique (§ 3.3), puis S2 complète :

```bash
"$PY" diagnostics/p1q/p1q_prober.py --role public --target https://p1q-receiver-p1q.up.railway.app --family both --self-ip <IPV4_PUBLIQUE> --out "$RES/public_strict_s1.jsonl"
```

3. **[F]** contrôle immédiat et `boot_id` :

```bash
"$PY" diagnostics/p1q/p1q_analyze.py "$RES/public_strict_s1.jsonl" | head -40; grep -o '"boot_id":"[0-9a-f]*"' "$RES/public_strict_s1.jsonl" | sort -u
```

   Attendu : collecte complète ; un seul `boot_id`, égal à celui annoncé ; cellules IPv6 `family_unavailable`
   (V1). Un `aborted` / `target_not_receiver` ⇒ arrêter.
4. **[F]** curl HTTP/2 (guide § 6), sorties `curl_h2_q1_s1.json`, `curl_h2_q2_s1.json`, `curl_h2_q4_s1.json` ;
   puis IPv4 publique relevée de nouveau (doit être identique).
5. **[F]** envoi à Claude Code des fichiers `public_strict_s1.jsonl` et `curl_*_s1.json` (+ `sha256sum`).
6. **[C]** analyse de S2 : `Q6.facts.strict_boots.<boot>.bypass` (adresses locales de A atteintes par l'edge).
   - adresse déjà atteinte par la matrice privée (cas attendu d'après Q6-P de la campagne 1 : même IPv4 privée) :
     aucune cible Q6-B ;
   - adresse non globale **absente** de la matrice privée : **[G]** puis
     `railway variable set "P1Q_EXTRA_TARGETS=http://<adresse>:8080" --service p1q-prober --skip-deploys`
     (IPv6 entre crochets) ;
   - adresse pseudonymisée (`g4:` / `g6:`) : `untestable_pseudonymised`, consigné, soumis à arbitrage, **aucune**
     recherche de l'adresse réelle.
7. **[C]** atténuation F-DNS : attendre ≥ 60 s après le retrait de l'ancien déploiement de A ; puis
   `railway redeploy --service p1q-prober -p … -e … -y` ; collecter `private_c2_s1.log`
   (`railway logs --service p1q-prober -n 2000`, sortie standard seule) ; **vérifier** que le `boot_id` des
   observations de B est celui de S-1 et que `P1Q-DIGEST` concorde. Si B a joint un autre démarrage ou si une
   famille est en échec de connexion : **[G]** un seul nouveau redéploiement de B, consigné.
8. **[C]** analyse croisée S-1 : `public_complete`, `private_complete`, `q6p_missing`, `bypass_missing`,
   discriminateurs D1-D3.

### 6.2 Démarrage strict S-2 (distinct, exigé pour la stabilité)

Rejouer 6.1 à l'identique (`receiver_c2_s2.log`, `public_strict_s2.jsonl`, `curl_*_s2.json`,
`private_c2_s2.log`) ; les adresses de A **changent** à chaque démarrage : relire Q6-B pour **ce** démarrage et
mettre à jour ou **supprimer** `P1Q_EXTRA_TARGETS` avant de redéployer B (**[G]**).

### 6.3 Q7 — configurations (b), (c), (d) (variables du seul récepteur)

Pour chaque ligne, dans cet ordre : **[C]** variables avec `--skip-deploys`, redéploiement de A, lecture de la
configuration rapportée dans `P1Q-STARTUP` ; **[F]** session publique `--only Q7` ; **[C]** redéploiement de B
(matrice privée complète, qui contient Q7).

| Étape | Commandes [C] | Commande [F] |
|---|---|---|
| (b) | `railway variable set "FORWARDED_ALLOW_IPS=*" --service p1q-receiver --skip-deploys` | `"$PY" diagnostics/p1q/p1q_prober.py --role public --target https://p1q-receiver-p1q.up.railway.app --family both --self-ip <IPV4_PUBLIQUE> --only Q7 --out "$RES/public_q7b.jsonl"` |
| (c) | `railway variable delete FORWARDED_ALLOW_IPS --service p1q-receiver` (pas de `--skip-deploys` en CLI 5.74.0 : vérifier l'absence de déploiement automatique), puis `railway variable set P1Q_UVICORN_MODE=uvicorn_defaults --service p1q-receiver --skip-deploys` | idem, `--out "$RES/public_q7c.jsonl"` |
| (d) | `railway variable set P1Q_UVICORN_MODE=trust_all_negative_control --service p1q-receiver --skip-deploys` | idem, `--out "$RES/public_q7d.jsonl"` |
| retour | `railway variable set P1Q_UVICORN_MODE=strict --service p1q-receiver --skip-deploys` ; vérifier l'absence de `FORWARDED_ALLOW_IPS` | — |

(a) est couverte par S-1 et S-2 (sessions complètes). Le témoin (d) doit montrer la réécriture de Q7-01 en public
(client effectif ≠ pair brut) et en privé (client effectif = `203.0.113.7`), par famille requise.

### 6.4 Analyse finale

**[C]** une seule analyse, chaque fichier **une seule fois** :

```bash
python3 diagnostics/p1q/p1q_analyze.py $RES/public_*.jsonl $RES/private_c2_*.log --curl-public $RES/curl_h2_q1_s1.json --curl-public $RES/curl_h2_q2_s1.json --curl-public $RES/curl_h2_q4_s1.json --curl-public $RES/curl_h2_q1_s2.json --curl-public $RES/curl_h2_q2_s2.json --curl-public $RES/curl_h2_q4_s2.json --out $RES/p1q_report_c2.json
```

Puis : recalcul indépendant des empreintes (`P1Q-DIGEST`), scan d'adresses globales en clair, extraction des faits
par démarrage, rapport `docs/act_l1_f04d_p1q_report_<AAAAMMJJ>.md` (modèle V0.1). Les journaux de la campagne 1
peuvent être joints dans une **seconde** analyse distincte (jamais mêlés à la première), pour comparaison.

## 7. Contraintes Railway

### 7.1 `railway.json` (Config as Code)

[doc Railway, relevée le 14/11/2026] : Config as Code est **dépréciée** ; les fichiers existants continuent de
fonctionner pour les services qui les utilisent déjà **jusqu'au 2026-12-01 (coupure ferme)** ; aucun nouveau
service ne peut l'adopter ; migration par `railway config migrate` (aperçu) puis `--apply` (écrit
`.railway/railway.ts` **et** efface les réglages « Railway Config File » du service).

Conséquences : (1) **campagne 2 au plus tard le 2026-11-30** pour rester dans la configuration auditée ; (2) la
configuration vivante des deux services porte déjà builder Dockerfile, `python p1q_main.py` et restart `NEVER`
(relevé du 14/11/2026), ce qui réduit le risque, mais le comportement après la coupure n'est pas documenté : à
revérifier en lecture seule avant toute exécution ; (3) **ne pas** lancer `railway config migrate --apply` sans
PR : la commande modifie le dépôt et les réglages du service, et le test `tests/test_p1q_bench.py` vérifie
`railway.json` ; (4) une campagne après le 2026-12-01 exige d'abord une PR de migration auditée.

### 7.2 Durées de vie

| Service | TTL | Effet |
|---|---|---|
| `p1q-receiver` | `P1Q_TTL_SECONDS=14400` (4 h) **par démarrage** | S2 et B doivent tenir dans ce délai pour un même démarrage ; chaque redéploiement relance le compteur |
| `p1q-prober` | `P1Q_TTL_SECONDS=10800` (3 h) | la matrice privée s'exécute au démarrage (≈ 1 min) ; au-delà, plus de relance par `railway ssh` |
| Les deux | restart `NEVER`, borne 86 400 s | un service arrêté ne repart que par redéploiement (= nouveau démarrage, nouveau `boot_id`) |

Durée indicative de la campagne 2 : 1 h 30 à 2 h (≈ 8 redéploiements de A et de B, 6 sessions publiques).

### 7.3 Autres contraintes

- **F-DNS** (campagne 1) : la résolution privée peut rester sur l'ancien conteneur après un redéploiement ;
  atténuation § 6.1-7 ; une règle D2 fondée sur l'auto-découverte reste fragile (constat à auditer).
- Débit des journaux (500 lignes / s) et longueur de ligne : sans incident en campagne 1 (103 lignes / exécution,
  lignes ≤ 15,6 Ko) ; repli `railway ssh` du guide § 7 si une empreinte ne concorde pas.
- Sortie IPv6 de B : laissée **désactivée** (Q6-P IPv6 non exigé) ; son activation serait une décision du fondateur.
- Q6-C : **non prévu** (exigerait un nouvel environnement et un nouveau service, donc une nouvelle infrastructure).
- U-ENV : le mode du réseau privé de **production** ne peut être relevé que par le fondateur, en lecture seule dans
  le tableau de bord ; Claude Code n'y a pas accès et ne le demandera pas.

## 8. Ressources, coûts et nettoyage

### 8.1 Avant la campagne 2

- Les services de la campagne 1 s'arrêtent seuls (≈ 12:20 et 13:20 UTC le 14/11/2026). Aucune action requise.
- **[F]** relever l'usage du projet de diagnostic dans l'onglet d'usage le 15/11/2026 (comparaison à
  l'estimation du protocole § 9 : ≈ 0,05 $) ; Claude Code ne consulte pas l'usage de l'espace de travail
  (il inclut la production).

### 8.2 Pendant

- Un seul déploiement actif par service à tout instant (vérifié par `railway deployment list`).
- Aucune nouvelle ressource : ni service, ni environnement, ni domaine, ni volume, ni base.

### 8.3 Après l'audit (feu vert « NETTOYAGE » distinct)

1. **[F]** suppression du projet entier `oryx-p1q-diagnostic` (tableau de bord → Settings → Danger) ; le jeton de
   projet ne permet pas à Claude Code de le faire et ce n'est pas souhaitable.
2. **[F]** révocation du Project Token ; retrait de `RAILWAY_TOKEN` de l'environnement Claude Code.
3. **[C]** proposition (validée par le fondateur sur carte d'approbation) de retirer `backboard.railway.com` et
   `p1q-receiver-p1q.up.railway.app` des hôtes autorisés de l'environnement Claude Code.
4. Vérifications consignées : `railway list` (côté fondateur) sans le projet ; `curl -sS https://p1q-receiver-p1q.up.railway.app/p1q/health`
   en échec ; projet de production inchangé (contrôle visuel en lecture seule) ; usage du lendemain.
5. Effacement des fichiers de résultats locaux (Mac et conteneur) **après** archivage auprès de l'audit.

## 9. Points d'autorisation (rien n'est exécuté sans le feu vert correspondant)

| Feu vert | Couvre | Acteur |
|---|---|---|
| G0 | choix V1 / V2 ; si V2, la révision passe d'abord par le workflow complet | fondateur |
| M0 | préparation et contrôles du Mac (§ 3, § 4.1), aucune requête d'essai | fondateur |
| R1 | redéploiements de A et de B pour S-1 et S-2 | Claude Code |
| R2 | modification de `P1Q_EXTRA_TARGETS` (Q6-B), si nécessaire | Claude Code |
| R3 | modifications de variables et redéploiements Q7 (b)-(d), puis retour en strict | Claude Code |
| N | nettoyage final (§ 8.3) après sauvegarde et présentation des résultats pour audit | fondateur, puis Claude Code |

## 10. Critères de validation de la campagne 2

| Critère | Condition |
|---|---|
| Collecte | `collection.proven = true` ; recalcul indépendant des empreintes concordant ; aucun fichier fourni deux fois |
| Homogénéité | chaque exécution porte un seul `boot_id` et une seule configuration ; S2 et B du même démarrage pour S-1 et S-2 |
| Absence d'intermédiaire | `preflight.txt` conforme ; IPv4 publique identique avant / après chaque session |
| IPv6 | `dns_proof.txt` ; aucune adresse NAT64 / mappée ; cellules IPv6 `family_unavailable` par `gaierror` (V1) |
| Q6-B | pour chaque démarrage strict, chaque adresse edge `observed` ou `refused:<classe>` ; sinon consigné |
| Q7 | quatre configurations lues dans les observations ; témoin (d) démontré public et privé |
| Verdicts | propositions de l'analyseur reportées sans amélioration ; un FAIL reste affiché ; **aucun GO** ; P1 ouverte ; audit ChatGPT avant toute décision |

## 11. Blocages identifiés

| # | Blocage | Levée possible |
|---|---|---|
| B1 | Pas d'entrée IPv6 publique sur le récepteur (pas d'AAAA) | côté Railway seulement ; à défaut, décision V2 (§ 4.4) |
| B2 | Coupure Config as Code le 2026-12-01 | campagne avant cette date, ou PR de migration auditée |
| B3 | Transfert des fichiers Mac → Claude Code | pièce jointe dans la session ; empreintes SHA-256 jointes |
| B4 | U-ENV (production) | relevé visuel du fondateur, hors Claude Code |
| B5 | Risque FAIL artificiel NAT64 dans l'analyseur | contrôle manuel § 4.1 (immédiat) ; garde de code (PR, § 5) |
