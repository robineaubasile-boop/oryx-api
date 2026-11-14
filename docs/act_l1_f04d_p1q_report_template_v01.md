# ACT-L1 / F04-D / P1-Q — Rapport des résultats Q1 à Q8 (MODÈLE V0.1)

> Modèle à copier et dater (`docs/act_l1_f04d_p1q_report_<AAAAMMJJ>.md`). Toute case non renseignée vaut
> « NON EXÉCUTÉ ». Aucune adresse globale en clair : reporter les pseudonymes produits par le banc.
> Un verdict du rapport reste une **proposition** soumise à l'audit indépendant et à la décision du fondateur.

## 1. Identification

| Élément | Valeur |
|---|---|
| Date et plage horaire (UTC) de l'essai | |
| Autorisation du fondateur (référence du message) | |
| SHA du dépôt déployé (PR auditée) | |
| Protocole | P1-Q V0.1 (`docs/act_l1_f04d_p1q_protocol_v01.md`) |
| Projet Railway / environnement(s) | `oryx-p1q-diagnostic` / |
| Date de création de l'environnement de diagnostic | |
| Mode du réseau privé de production (U-ENV), relevé en lecture seule | |
| Région de déploiement de A (`RAILWAY_REPLICA_REGION`) | |
| POP edge observé(s) (`X-Railway-Edge`) | |
| Version CLI Railway ; écarts de commandes constatés | |
| Réseau de l'expérimentateur (FAI / NAT / VPN, IPv6 disponible ?) | |
| Démarrages de A (`boot_id`, mode, `FORWARDED_ALLOW_IPS`) | |
| Exécutions de la sonde (`run_id`, rôle, démarrage, configuration, collecte complète ?) | |
| Collecte prouvée (`collection.proven`) ; anomalies de lecture (`collection.notes`) | |
| Liste `blocking` du rapport de l'analyseur | |

## 2. Inventaire de démarrage de A (ligne `P1Q-STARTUP`)

| Élément | Démarrage 1 | Démarrage 2 |
|---|---|---|
| Sockets d'écoute (IPv4 / IPv6, erreurs) | | |
| Adresses locales IPv4 / IPv6 (catégories) | | |
| Résolution du domaine privé (A / AAAA) | | |
| Réglages Uvicorn (`proxy_headers`, `forwarded_allow_ips`) | | |

## 3. Résultats par question

Pour chaque question : faits observés (avec identifiants de cas et nombres), verdict **proposé** par
l'analyseur, verdict **retenu par le rapport** et justification de tout écart, limites, et **couverture** :
sessions concernées / complètes, cellules manquantes (`missing`, avec leur statut : `not_run`,
`family_unavailable`, `aborted`, `not_attempted`, `refused:<classe>`…). Un verdict retenu ne peut pas être plus
favorable que le verdict proposé sans nouvelle collecte.

### Q1 — Public nominal

| Fait | IPv4 | IPv6 |
|---|---|---|
| Observations / requêtes | | |
| `X-Real-IP` : nombre, forme, catégorie, canonique ? | | |
| Égal à l'adresse engagée (`self_match`) | | |
| Stable sur le démarrage (pseudonyme unique) | | |
| Pair brut : catégorie(s), stabilité | | |
| `scope["server"]` atteint | | |
| En-têtes ajoutés par l'edge (`X-Forwarded-For` ? `Forwarded` ? `X-Railway-*`) | | |
| Simulation « confiance 127.0.0.1 » : réécriture ? | | |
| HTTP/2 (curl) : écart avec HTTP/1.1 ? | | |

Verdict proposé : … · Verdict retenu : … · Limites : …

### Q2 — Falsification de `X-Real-IP`

| Cas | IPv4 : résultat (`nominal` / `client_controlled` / `multiple` / `edge_rejected` / …) | IPv6 |
|---|---|---|
| Q2-01 … Q2-08 | | |

Verdict proposé : … · Verdict retenu : … · Limites : …

### Q3 — Autres en-têtes

| Cas | `X-Real-IP` | XFF reçu | `Forwarded` reçu | `X-Forwarded-Host/Proto` | `X-Railway-*` forgés conservés ? | autres en-têtes d'IP client |
|---|---|---|---|---|---|---|
| Q3-01 … Q3-09 | | | | | | |

Verdict proposé : … · Verdict retenu : … · Limites : …

### Q4 — Entrées ambiguës

| Cas | IPv4 | IPv6 | Observation (refus edge, normalisation, forme reçue) |
|---|---|---|---|
| Q4-01 … Q4-16 | | | |

Verdict proposé : … · Verdict retenu : … · Limites : …

### Q5 — Privé nominal

| Fait | IPv4 | IPv6 |
|---|---|---|
| Joignabilité | | |
| Pair brut (catégorie, valeur si non globale) | | |
| `scope["server"]` atteint ; appartient au domaine privé auto-découvert ? | | |
| `X-Real-IP` / en-têtes edge présents ? | | |

Verdict proposé : … · Verdict retenu : … · Limites : …

### Q6 — Privé avec usurpation (critique)

| Fait | Valeur |
|---|---|
| `X-Real-IP` forgé reçu par A en privé (nombre) | |
| D1 catégorie du pair : public / privé / sépare ? | |
| D2 adresse locale : public / privé / sépare ? règle auto-découverte ? | |
| D3 famille d'écoute : public / privé / sépare ? | |
| D4 valeurs exactes (non retenu comme règle) | |
| Démarrages couverts (public / privé) | |
| Q6-B : par démarrage, statut de chaque adresse edge (`observed`, `refused:<classe>`, `not_attempted`, `family_unavailable`, `refused_by_guard`, `untestable_pseudonymised`) | |
| Démarrages qualifiants (`Q6.facts.qualifying_boots`) / requis : 2 | |
| Imitations Q6-01 ×3, Q6-02, Q6-03 observées (IPv4 / IPv6) | |
| Q6-P : `X-Real-IP` d'un appelant interne via l'edge (forgé conservé ?) | |
| Q6-C (facultatif) : isolement entre environnements | |

Verdict proposé : … · Verdict retenu : … · Limites (comportement non documenté, requalification) : …

### Q7 — Configuration Uvicorn

| Mode | `FORWARDED_ALLOW_IPS` | Observations (public / privé × IPv4 / IPv6) | Réécritures de `scope["client"]` | Pair brut = `127.0.0.1` ? |
|---|---|---|---|---|
| (a) strict | absente | | | |
| (b) strict | `*` | | | |
| (c) uvicorn_defaults | absente | | | |
| (d) trust_all_negative_control | absente | | | |

Constats locaux du protocole § 3.5 confirmés ou infirmés sur Railway : …

Verdict proposé : … · Verdict retenu : … · Limites : …

### Q8 — Matrice du futur résolveur

Pour R7 (IPv4 mappée) et R8 (IPv6 non canonique) : forme effectivement émise par l'edge (Q4) et proposition de
figement. Discriminateur proposé pour R1 (issu de Q6), ou constat qu'aucun n'existe. Aucune implémentation.

## 4. Synthèse GO / NO-GO / INCONCLUSIVE (protocole § 6)

| Question | Proposé | Retenu |
|---|---|---|
| Q1 | | |
| Q2 | | |
| Q3 | | |
| Q4 | | |
| Q5 | | |
| Q6 | | |
| Q7 | | |
| **Option A** | | |

Rappel : aucun GO n'est prononcé par ce rapport. Un GO-CANDIDATE n'est qu'une proposition au fondateur ; **P1
reste ouverte jusqu'au freeze F04-D**.

## 5. Coût et ressources constatés

| Poste | Estimé (protocole § 9) | Constaté |
|---|---|---|
| Service-heures | 12 | |
| Mémoire / CPU moyens | 0,15 Go / 0,05 vCPU par service | |
| Montant | ≈ 0,05 $ (< 0,50 $) | |

## 6. Suppression des ressources

| Vérification | Résultat (date / heure) |
|---|---|
| Projet `oryx-p1q-diagnostic` supprimé (`railway list`) | |
| Domaine public de A injoignable | |
| Projet de production inchangé (lecture seule) | |
| Fichiers de résultats locaux archivés puis effacés | |

## 7. Écarts au protocole, incidents et réserves

| Écart / incident | Effet sur les conclusions |
|---|---|
| | |
