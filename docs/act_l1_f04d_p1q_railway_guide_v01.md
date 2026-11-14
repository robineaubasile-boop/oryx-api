# ACT-L1 / F04-D / P1-Q V0.1 — Guide d'exécution Railway du banc de diagnostic

> **NE PAS EXÉCUTER sans autorisation explicite et écrite du fondateur.** Ce guide décrit les gestes ; il ne vaut
> pas autorisation. Toute étape qui touche Railway reste **NON EXÉCUTÉE** à la date de rédaction (14/11/2026).
> Protocole de référence : `docs/act_l1_f04d_p1q_protocol_v01.md` (P1-Q V0.1).

## 0. Règles impératives pendant tout l'essai

- Travailler **uniquement** dans un **projet Railway neuf** nommé `oryx-p1q-diagnostic`. Ne jamais ouvrir, lier,
  dupliquer ou modifier le projet de production, ses environnements, ses services ou ses variables.
- Ne **jamais** copier une variable de production ; ne définir **que** les variables listées ici. Aucune variable
  `ORYX_*`, `DATABASE_*`, `PG*`, aucun secret : le banc refuse de démarrer s'il en trouve (code 3).
- Ne **jamais** rattacher le projet de diagnostic au dépôt GitHub (pas de déploiement automatique) : déployer par
  `railway up` depuis un extrait local du commit audité.
- Ne cibler que le service `p1q-receiver` du projet de diagnostic. La sonde refuse tout hôte de production
  (code 4). Q6-B n'utilise que des adresses **du récepteur lui-même**, relevées dans ses propres observations :
  **aucun balayage** d'adresses.
- Les commandes CLI ci-dessous correspondent à la documentation Railway consultée le 14/11/2026 ; vérifier chaque
  option par `railway <commande> --help` au moment de l'essai et consigner toute différence dans le rapport.
- Avant chaque commande `railway`, vérifier le projet lié : `railway status` doit afficher `oryx-p1q-diagnostic`.

## 1. Prérequis (poste de l'expérimentateur)

1. Python ≥ 3.11 (bibliothèque standard seule pour la sonde publique et l'analyseur), `curl` récent (HTTP/2),
   CLI Railway installée et authentifiée (`railway whoami`).
2. Extrait du dépôt au **SHA exact** de la PR auditée :

```bash
git clone https://github.com/robineaubasile-boop/oryx-api.git oryx-p1q && cd oryx-p1q
```

```bash
git checkout <SHA_AUDITÉ_DE_LA_PR>
```

3. Tests locaux (facultatif, recommandé) avec les dépendances verrouillées de la CI :

```bash
python -m pytest -p no:cacheprovider tests/test_p1q_bench.py tests/test_p1q_receiver_http.py
```

4. Relever son adresse publique IPv4 (et IPv6 si disponible) **au moment de l'essai**, par un moyen de son choix
   (interface du routeur, service « what is my IP »). Elle ne sera **jamais** transmise en clair par la sonde.
5. Créer un dossier local de résultats **hors de l'extrait** (il ne doit jamais être envoyé à Railway par
   `railway up`) : `export RES="$HOME/p1q-results" && mkdir -p "$RES"`.
6. Convention : les commandes `railway` s'exécutent **depuis `diagnostics/p1q`** (répertoire lié au projet de
   diagnostic par `railway init`) ; les commandes `python3` de la sonde et de l'analyseur **depuis la racine** de
   l'extrait.

## 2. Création du projet vide et des deux services

```bash
cd diagnostics/p1q
```

```bash
railway init --name oryx-p1q-diagnostic
```

- Le projet neuf crée un environnement par défaut. **Recommandé** : le renommer `p1q` dans le tableau de bord
  (Settings du projet) pour éviter toute confusion avec la production. Noter la date de création (U-ENV).
- Créer deux services **vides** (tableau de bord : « + New » → « Empty Service », ou `railway add` en choisissant
  un service vide) nommés exactement `p1q-receiver` et `p1q-prober`. Aucun dépôt, aucune image, aucune base.
- Pour chacun, vérifier dans Settings : source = aucune ; Replicas = 1 ; Serverless désactivé ; aucune variable
  partagée héritée.

## 3. Variables (uniquement celles-ci)

Service `p1q-receiver` :

| Variable | Valeur |
|---|---|
| `P1Q_ROLE` | `receiver` |
| `PORT` | `8080` |
| `P1Q_UVICORN_MODE` | `strict` |
| `P1Q_TTL_SECONDS` | `14400` |

Service `p1q-prober` :

| Variable | Valeur |
|---|---|
| `P1Q_ROLE` | `prober` |
| `P1Q_TARGET` | `http://${{p1q-receiver.RAILWAY_PRIVATE_DOMAIN}}:8080` |
| `P1Q_PUBLIC_TARGET` | `https://${{p1q-receiver.RAILWAY_PUBLIC_DOMAIN}}` (après l'étape 4) |
| `P1Q_FAMILY` | `both` |
| `P1Q_TTL_SECONDS` | `10800` |

Commande indicative (une variable par option, à adapter à la version de la CLI) :

```bash
railway variables --service p1q-receiver --set "P1Q_ROLE=receiver" --set "PORT=8080" --set "P1Q_UVICORN_MODE=strict" --set "P1Q_TTL_SECONDS=14400"
```

Contrôle : `railway variables --service p1q-receiver` puis `--service p1q-prober` ne doivent lister que ces
variables et les variables `RAILWAY_*` fournies par la plateforme.

## 4. Déploiement du récepteur (A) et domaine public

```bash
railway up . --path-as-root --service p1q-receiver --detach
```

- Le répertoire envoyé est `diagnostics/p1q` seul (archive racine = ce dossier) : `Dockerfile` et `railway.json`
  y sont lus. Vérifier dans les journaux de build : builder Dockerfile, `pip install --require-hashes` réussi.
  Vérifier dans Settings → Deploy : start command `python p1q_main.py`, restart policy **Never**.
- Générer le domaine public du seul récepteur, port cible 8080 :

```bash
railway domain --service p1q-receiver
```

- Journal de démarrage : une ligne `P1Q-STARTUP {...}` (mode, réglages Uvicorn, sockets d'écoute IPv4 / IPv6,
  adresses locales, résolution du domaine privé). L'archiver :

```bash
railway logs --service p1q-receiver -n 2000 > $RES/receiver_boot1.log
```

- Contrôle de santé : `curl -sS https://<domaine>/p1q/health` → `{"status":"ok"}`.
- Si le récepteur refuse de démarrer (code 3, « environnement NON isolé ») : lire les **noms** affichés. Une
  variable non définie par ce guide signale une erreur de projet (arrêter l'essai) ; une variable `RAILWAY_*`
  injectée par la plateforme et prise à tort pour un secret est un écart à consigner et à arbitrer, jamais à
  contourner en modifiant la garde sur place.

## 5. Session S2 — essais publics Q1 à Q4 et Q7 (mode strict)

Depuis la racine de l'extrait :

```bash
python3 diagnostics/p1q/p1q_prober.py --role public --target https://<domaine-public-de-p1q-receiver> --family both --self-ip <IPV4_PUBLIQUE> --self-ip <IPV6_PUBLIQUE_SI_DISPONIBLE> --out $RES/public_strict_boot1.jsonl
```

- Sans IPv6 locale : `--family 4` et un seul `--self-ip` ; consigner l'absence d'IPv6 (Q1-Q4 IPv6 resteront
  INCONCLUSIVE).
- Contrôle immédiat : `python3 diagnostics/p1q/p1q_analyze.py $RES/public_strict_boot1.jsonl`
  (collecte « complète » attendue).

## 6. Variante HTTP/2 (curl)

Chaque commande enregistre l'observation (déjà pseudonymisée) dans un fichier JSON :

```bash
curl -sS --http2 -H "X-P1Q-Case: curl-h2-Q1" https://<domaine>/p1q/observe -o $RES/curl_h2_q1.json
```

```bash
curl -sS --http2 -H "X-P1Q-Case: curl-h2-Q2" -H "X-Real-IP: 203.0.113.7" https://<domaine>/p1q/observe -o $RES/curl_h2_q2.json
```

```bash
curl -sS --http2 -H "X-P1Q-Case: curl-h2-Q4" -H "X-Real-IP: 203.0.113.7" -H "X-Real-IP: 198.51.100.23" https://<domaine>/p1q/observe -o $RES/curl_h2_q4.json
```

Si curl le permet : mêmes commandes avec `--http3`. Ces fichiers sont passés à l'analyseur par `--curl-public`
(sans engagement HMAC : comparaison manuelle au pseudonyme nominal du même démarrage).

## 7. Session S3 — sonde privée B (Q5, Q6, rejeu Q2-Q4 / Q7, Q6-P)

1. Définir les variables de `p1q-prober` (§ 3), puis déployer le **même** dossier (depuis `diagnostics/p1q`) :

```bash
railway up . --path-as-root --service p1q-prober --detach
```

2. B exécute la matrice privée au démarrage, en IPv4 et en IPv6, puis Q6-P (vers le domaine public), écrit
   `P1Q-PROBER-EXIT 0` et reste inactif jusqu'à son TTL. **Ne pas** lui générer de domaine public.
3. Collecter :

```bash
railway logs --service p1q-prober -n 2000 > $RES/private_strict_boot1.log
```

   Sortie texte par défaut (ne pas utiliser de sortie JSON de la CLI : les lignes `P1Q-RESULT` seraient
   échappées). Railway limite le débit à 500 lignes / s par réplique [doc Railway] ; la sonde émet une ligne
   toutes les 0,2 s environ.
4. Vérifier l'intégrité (ligne `P1Q-DIGEST`, collecte « complète »). En cas de collecte incomplète (lignes de
   journal tronquées), relancer dans le conteneur et récupérer un fichier :

```bash
railway ssh --service p1q-prober
```

   puis, dans le conteneur : `python p1q_prober.py --role private --target "$P1Q_TARGET" --public-target "$P1Q_PUBLIC_TARGET" --out /tmp/private.jsonl`,
   et `cat /tmp/private.jsonl` (sortie copiée dans `$RES/`).

## 8. Sessions complémentaires

### 8.1 Q6-B — contournement vers les adresses observées côté edge

1. Lancer l'analyse de S2 + S3 et relever `Q6.facts.discriminators.D2_server_address.public` : adresses
   **locales de A** atteintes par l'edge.
2. Si l'une d'elles n'est pas déjà une adresse privée atteinte par B, la fournir à B (adresses de A uniquement) :
   `P1Q_EXTRA_TARGETS=http://<adresse>:8080` (IPv6 entre crochets : `http://[<adresse>]:8080`), puis redéployer B
   ou relancer par `railway ssh` avec `--extra-target`.
3. Collecter comme au § 7.

### 8.2 Second démarrage (stabilité, exigée pour un PASS Q6)

Redéployer A (`railway redeploy --service p1q-receiver`, ou `railway up` à nouveau), attendre la fin du
recouvrement (un seul déploiement actif), puis rejouer S2 (`public_strict_boot2.jsonl`) et S3
(`private_strict_boot2.log`, redéploiement de B).

### 8.3 Q7 — modes Uvicorn (variables du seul récepteur)

Pour chaque ligne : modifier les variables de `p1q-receiver`, redéployer A, rejouer
`p1q_prober.py --role public ... --only Q7 --out $RES/public_<mode>.jsonl`, puis redéployer B (ou `railway ssh`)
pour le rejeu privé `--only Q7`.

| Étape | `P1Q_UVICORN_MODE` | `FORWARDED_ALLOW_IPS` |
|---|---|---|
| (a) | `strict` | absente (déjà couverte par S2 / S3) |
| (b) | `strict` | `*` |
| (c) | `uvicorn_defaults` | absente |
| (d) | `trust_all_negative_control` | absente |

Après (d) : remettre `P1Q_UVICORN_MODE=strict` et **supprimer** `FORWARDED_ALLOW_IPS`.

### 8.4 Q6-C (facultatif) — isolement entre environnements

Créer dans le **projet de diagnostic** un second environnement vide `p1q-iso` (jamais par duplication d'un
environnement existant), y créer un service `p1q-prober` avec `P1Q_ROLE=prober`, `P1Q_FAMILY=both` et
`P1Q_TARGET=http://<domaine privé littéral de p1q-receiver>:8080`. Attendu : `family_unavailable` ou
`connect_error` pour toutes les familles. Archiver dans `$RES/q6c_isolation.log` **sans** le fournir à
l'analyseur principal (le rapport le traite à part).

## 9. Analyse, collecte et suppression

1. Analyse :

```bash
python3 diagnostics/p1q/p1q_analyze.py $RES/*.jsonl $RES/private_*.log --curl-public $RES/curl_h2_q1.json --out $RES/p1q_report.json
```

2. Remplir `docs/act_l1_f04d_p1q_report_template_v01.md` (copie datée) avec les faits, les verdicts proposés, les
   écarts et les limites ; joindre les fichiers bruts pseudonymisés à l'audit (hors Git, ou dans une PR de rapport
   distincte si le fondateur le décide).
3. **Suppression des ressources** (même jour) : tableau de bord du projet `oryx-p1q-diagnostic` → Settings →
   Danger → supprimer le projet entier (services, domaine, variables, environnements `p1q` / `p1q-iso`).
4. Vérifications de suppression, consignées dans le rapport :
   - `railway list` n'affiche plus `oryx-p1q-diagnostic` ;
   - `curl -sS https://<domaine>/p1q/health` échoue (domaine libéré) ;
   - le projet de production est inchangé : aucune variable, aucun service, aucun déploiement ajouté (contrôle
     visuel en lecture seule) ;
   - le lendemain : usage facturé du projet de diagnostic relevé dans l'onglet d'usage (comparaison à
     l'estimation du protocole § 9).
5. Effacer les fichiers de résultats locaux après archivage auprès de l'audit.

## 10. Durée et chronologie indicatives

| Session | Contenu | Durée indicative |
|---|---|---|
| S1 | projet, services, variables, déploiement A, domaine | 20-30 min |
| S2 + § 6 | essais publics strict, HTTP/2 | 10 min |
| S3 | sonde privée | 10-15 min |
| 8.1 / 8.2 | contournement, second démarrage | 20-30 min |
| 8.3 | quatre modes Q7 | 30-45 min |
| 9 | analyse, suppression, vérifications | 20 min |

Durée de vie des services bornée par leur TTL (4 h / 3 h) ; un service arrêté n'est pas relancé (restart policy
Never).
