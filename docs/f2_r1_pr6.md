# R1 PR 6 — profil F2 versionné et `level_revision` (dormant)

Référence normative : **architecture R1 PR 6 figée par le fondateur le
26 octobre 2026** (« Architecture candidate consolidée — audit de
fermeture »). Ce document n'est pas versionné dans le dépôt ; il n'était pas
accessible dans l'environnement d'implémentation : le contrat appliqué est
celui reproduit dans la demande d'implémentation (sections 2 à 13), sans
décision architecturale nouvelle.

Référence de compatibilité : `main` au SHA
`07149afdbd8351e0cb45cbd2110363518f272de9` (PR #225, R1 PR 5 fusionnée),
identique au `main` au moment de l'implémentation. Tête Alembic inchangée :
`0019_f2_http_admission`. **Aucune migration, aucun trigger, aucune table
d'idempotence, aucune dépendance de production, aucune variable
d'environnement.**

> **PR 6 ajoute un contrat versionné pour le profil F2 isolé. Elle ne
> sécurise pas les écritures historiques et n'autorise pas l'activation
> publique F2.**

## 1. Périmètre livré

| Élément | Fichier |
|---|---|
| `ProfileView(user_id, level, level_revision)`, `UpdateProfileLevel(level, expected_revision)`, plan `PROFILE_MUTATION`, bornes et validateurs | `core/authz_f2/commands.py` |
| Lecture du couple en une requête, CAS (`_update_profile_level`), `CONFLICT`, `RevisionExhausted`, registre fermé à neuf commandes | `core/authz_f2/_adapters.py` |
| Résultat fermé `REVISION_CONFLICT`, alerte interne de révision épuisée | `core/authz_f2/runner.py` |
| Provenance des mutations personnelles (Origin obligatoire) | `core/authz_f2_http/provenance.py` |
| `GET /api/v2/me` étendu, `PUT /api/v2/me/level` | `core/authz_f2_http/routes.py` |
| Injection stricte du trousseau CSRF et de l'horloge murale | `core/authz_f2_http/isolated.py` |
| Outils de test (CSRF, PUT, appels ASGI bruts, profil observé avec `xmin`) | `tests/authz_f2_support.py` |

Inchangés : `api.py`, routes historiques, `web-v2/index.html` et tout
frontend, migrations, modèles et contrats pédagogiques C1–C12, preuves,
observations, inférences, `core/auth_f2*` (PR 2 à PR 4), politique de
provenance de PR 3, plans `READ` et `USER_MUTATION`, ordre des contrôles
de `GET /api/v2/me`, délais `lock_timeout = 5s` / `statement_timeout = 10s`.

Le niveau déclaré (`debutant` | `avance`) est une **préférence de
personnalisation**, jamais un stade pédagogique.

## 2. Lecture du profil (`ReadProfile`)

- `ProfileView` : dataclass gelée `user_id: str`, `level: str`,
  `level_revision: int` ; elle refuse toute valeur hors contrat (niveau
  inconnu, révision non entière, booléenne, négative ou > 2^63 − 1).
- Une seule requête, après l'autorisation définitive :
  `SELECT id, level, level_revision FROM users WHERE id = :u`
  (`:u` = `principal.user_id`).
- Profil absent ou incohérent (niveau inconnu, révision invalide, autre
  identifiant) avec une session valide : `AdapterInvariantBroken` →
  `UNAVAILABLE` → **503**. Aucune correction silencieuse, aucune création.
- Aucune écriture, aucun incrément, aucune confirmation de livraison,
  aucun `Set-Cookie`.

Réponse HTTP 200 (clés triées, JSON compact) :

```json
{"level":"debutant","level_revision":"7","user_id":"user-a"}
```

`level_revision` est un entier interne transporté en **chaîne décimale
canonique** (`str(int)`), sans perte au-delà de 2^53.

## 3. Commande `UpdateProfileLevel` (neuvième commande du registre fermé)

- Dataclass gelée, `slots`, sans `user_id`, sans callback, sans SQL.
- `level` : `str` **exact** (pas de sous-classe) valant `debutant` ou
  `avance`.
- `expected_revision` : `int` **exact** (ni `bool`, ni sous-classe),
  `0 ≤ r ≤ 9223372036854775807`.
- Registre (`MappingProxyType`, type exact) : `UpdateProfileLevel →
  Spec(PROFILE_MUTATION, _no_business_lock, _update_profile_level)`. Les
  validations des huit commandes de PR 5 sont inchangées.

## 4. CAS et versionnement

Plan **`PROFILE_MUTATION`** = `LockPlan("profile_mutation", "FOR UPDATE",
())` :

1. `users FOR UPDATE` (identité serveur de la phase A) ;
2. `oryx_sessions FOR SHARE` ;
3. `oryx_session_generations FOR SHARE` ;
4. aucun verrou métier (le plan `USER_MUTATION`, qui verrouille le
   portefeuille, n'est pas réutilisé).

Après `T_authorization` et la décision (propriété du profil confirmée :
`session.user_id = candidate_user_id`, profil verrouillé), sous le verrou
du profil, `_update_profile_level` :

1. relit `level` et `level_revision` **ensemble** et juge leur cohérence
   (sinon 503) ;
2. compare `expected_revision` à la révision courante — **la révision
   précède toujours le niveau** ;
3. différente → `CONFLICT` → `REVISION_CONFLICT` (ROLLBACK, aucune
   écriture, ni profil courant ni principal transportés) ;
4. identique et niveau inchangé → succès **sans UPDATE** ni incrément ;
5. identique et niveau différent → une instruction atomique :

   ```sql
   UPDATE users SET level = :l, level_revision = level_revision + 1
   WHERE id = :u AND level_revision = :r
   RETURNING id, level, level_revision
   ```

6. valeurs relues par `RETURNING`, vérifiées (niveau demandé, révision
   + 1 exacte) ;
7. le succès n'est rendu qu'après le COMMIT confirmé (sortie du bloc
   transactionnel du runner).

Une écriture conditionnelle sans ligne retournée après ces contrôles sous
verrou est une **violation d'invariant** (ROLLBACK, 503), jamais un faux
conflit.

| Révision | Niveau demandé | Résultat |
|---|---|---|
| exacte | différent | 200, révision + 1 |
| exacte | identique | 200, révision inchangée (aucun UPDATE) |
| périmée (inférieure ou supérieure) | différent | 409, aucune écriture |
| périmée (inférieure ou supérieure) | identique | 409, aucune écriture |

**Révision maximale** `9223372036854775807` : demande identique à révision
exacte → 200 sans écriture ; changement réel → `RevisionExhausted` →
`UNAVAILABLE` (503) et journal `ERROR`
`authz_f2 UpdateProfileLevel status=unavailable alert=level_revision_exhausted`
(sans valeur, sans identifiant). Aucun débordement, recyclage ni remise à
zéro (le contrôle précède l'UPDATE ; `next_revision` n'a pas de suivant
au maximum).

## 5. Sessions, autorisation et temps

Protocole de PR 5 inchangé (docs/f2_r1_pr5.md §3, §6) : phase A courte
sans verrou ni écriture (livraison confirmée exigée) ; phase B : verrous
du plan, `T_authorization = clock_timestamp()` lu après eux, revalidation
complète (cryptographie, version exacte de `K_active`, révocation,
`T_authorization < absolute_expires_at`, l'égalité refuse, identité,
génération, livraison), opération, COMMIT ou ROLLBACK par le runner seul.
Une expiration pendant l'attente du profil, de la session ou de la
génération est jugée à `T_authorization` : 401, rien n'est écrit. Une
session devenue non admissible prime sur le résultat CAS. Aucun réseau
dans une transaction. COMMIT en échec ou incertain : 503
`temporarily_unavailable`, jamais « annulé », aucun rejeu automatique.

## 6. `PUT /api/v2/me/level`

Corps exact : `{"level": "avance", "expected_revision": "7"}` — aucun
autre champ, aucun `user_id`.

Ordre des contrôles (le premier échec répond ; aucun contrôle suivant ne
s'exécute ; aucun refus de transport n'ouvre de transaction) :

| # | Contrôle | Refus |
|---|---|---|
| 1 | méthode / chemin (routeur) | 405 / 404 |
| 2 | provenance de mutation personnelle | 403 `origin_refused` |
| 3 | en-tête Cookie (parseur strict PR 3) | 400 `invalid_request` |
| 4 | CSRF | 503 `temporarily_unavailable` / 403 `csrf_invalid` |
| 5 | `X-Oryx-Generation-Id` unique, UUID canonique | 400 `invalid_request` |
| 6 | aucune query | 400 `invalid_request` |
| 7 | Content-Type | 415 `invalid_request` |
| 8 | framing | 400 `invalid_request` |
| 9 | taille annoncée puis flux reçu | 413 `request_too_large` (discordance : 400) |
| 10 | JSON, puis `level`, puis `expected_revision` | 400 `invalid_request` |
| 11 | cookie `__Host-oryx-active-<generation_id>` EXACT | 401 `session_invalid` |
| 12 | runner disponible | 503 `temporarily_unavailable` |
| 13 | runner + `UpdateProfileLevel`, résultat vérifié puis sérialisé | 200 / 401 / 409 / 503 |

**Provenance** (`personal_mutation_origin`) : Host unique, canonique
(minuscules, sans `:443`), `https://<Host>` parmi les origines autorisées ;
**Origin obligatoire**, unique, exactement égal à `https://<Host>` (une
autre origine autorisée est refusée) ; `Sec-Fetch-Site` présent →
`same-origin` seulement ; Referer présent → https, sans identifiants, même
origine ; tout doublon refusé. La politique GET (Origin facultatif si
`Sec-Fetch-Site: same-origin`) et celle des routes PR 3 sont inchangées.

**CSRF** : protocole `oryx-f2-csrf-v1` de PR 3 réutilisé tel quel
(`csrf.verify_token`), en-tête unique `X-Oryx-CSRF`, cookie
`__Host-oryx-csrf-binding`, trousseau `CsrfKeyring` versionné, origine
validée à l'étape 2, instant `floor(wall_clock())` (horloge murale
injectée, distincte de l'horloge PostgreSQL).

- composition invalide (`csrf_keyring` absent ou d'un autre type,
  `wall_clock` non appelable, clé CSRF égale à une clé F2, quel que soit
  l'usage) → `PersonalHttpConfigError` à la construction ;
- dépendance CSRF absente sur une application construite par
  `make_personal_app` → 503, avant tout contrôle suivant (sélecteur
  compris) ;
- horloge en panne (exception, NaN, infini, type invalide) → 503, même
  sans jeton ;
- jeton absent, dupliqué, incorrect, mal lié, d'une autre origine, expiré,
  au-delà de la durée d'émission ou de version inconnue, liaison absente,
  avec une configuration valide → 403 `csrf_invalid`.

Le bootstrap CSRF reste `GET /api/auth/f2/csrf/bootstrap` (application F2
isolée). La composition auth + personnel n'existe que dans les tests
(même trousseau CSRF des deux côtés).

**Content-Type** : un seul en-tête ; `application/json`, éventuellement
suivi d'un unique paramètre `charset=utf-8` (casse et blancs autour des
`;` ignorés) ; toute autre valeur → 415. Politique propre à PR 6 :
`read_json` de PR 3 n'est pas réutilisé.

**Framing** (en-têtes effectivement transmis à l'application ASGI) : tout
`Transfer-Encoding` (quelle que soit sa valeur, y compris avec
`Content-Length`) → 400 ; `Content-Length` dupliqué (même égal) → 400 ;
vide, signé, avec blanc, non ASCII, hexadécimal, décimal ou exponentiel →
400 (seul `1*DIGIT` ASCII est admis, `[0-9]+`) ; longueur annoncée valide
> 4 096 → 413 sans lire le corps ; flux dépassant la longueur annoncée,
flux tronqué ou déconnexion → 400 (anomalie de framing, prioritaire sur
la taille) ; sans `Content-Length`, flux > 4 096 → 413. Le corps n'est
jugé qu'après Content-Type et framing.

**JSON** : UTF-8 strict (BOM, UTF-16, octets invalides refusés), objet,
clés exactement `level` et `expected_revision`, aucune clé dupliquée,
aucune constante `NaN` / `Infinity`, rien après le document. `level` :
chaîne `debutant` | `avance`. `expected_revision` : chaîne
`0|[1-9][0-9]{0,18}` (chiffres ASCII seulement, jamais `str.isdigit()` ni
`\d`), ≤ 9223372036854775807, puis conversion en entier. Refusés : nombres
JSON, booléens, `null`, signes, blancs, zéros initiaux, exposants,
séparateurs, chiffres Unicode (arabo-indiens, pleine chasse, devanagari,
exposants).

**Vérification avant sérialisation** : 200 seulement si le résultat est
`AUTHORIZED`, de type `ProfileView`, pour la génération désignée, le
`user_id` du principal, le niveau demandé, une révision ∈ {attendue,
attendue + 1} ; sinon 503.

## 7. Codes HTTP

| HTTP | Corps |
|---|---|
| 200 | `{"level", "level_revision", "user_id"}` |
| 400 | `{"error":"invalid_request"}` |
| 401 | `{"error":"session_invalid"}` |
| 403 provenance | `{"error":"origin_refused"}` |
| 403 CSRF | `{"error":"csrf_invalid"}` |
| 409 | `{"error":"level_revision_conflict"}` |
| 413 | `{"error":"request_too_large"}` |
| 415 | `{"error":"invalid_request"}` |
| 503 | `{"error":"temporarily_unavailable"}` |

404 / 405 : refus de routage de l'application isolée. Toute réponse, y
compris 404 / 405 : `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, aucun
`Set-Cookie`, aucun CORS. Les journaux ne portent que route, statut et
code (ou type d'exception) : ni secret, ni jeton CSRF, ni corps, ni SQL.

## 8. Concurrence, retries et idempotence

- Deux changements partant de la même révision : le second attend le
  verrou `users FOR UPDATE`, relit la révision committée et obtient 409 ;
  jamais deux changements d'état.
- Deux demandes identiques sans changement : toutes deux 200 à révision
  égale, aucune écriture (`xmin` inchangé).
- `debutant → avance → debutant` : deux incréments.
- Changement committé dont la réponse est perdue (ou COMMIT incertain,
  503) : le rejeu avec l'ancienne révision rencontre 409. **Aucune
  restitution du résultat précédent, aucune idempotence durable** ; le
  client relit `GET /api/v2/me` et décide.
- Les résultats supposent des sessions admissibles : une expiration, une
  révocation ou une panne peut légitimement primer sur le résultat CAS.

## 9. Compatibilité historique et dormance

**Contournement connu, non corrigé par PR 6** : la route historique
`PUT /api/user/{user_id}/level` du vrai `api:app` écrit `users.level`
**sans** incrémenter `level_revision`, pour un `user_id` fourni par le
client. Le CAS F2 ne peut pas détecter cette écriture intermédiaire :
profil `(debutant, 7)` → écriture historique `(avance, 7)` → une requête F2
fondée sur la révision 7 réussit (`tests/test_authz_f2_http.py::
test_legacy_level_route_bypasses_the_versioned_contract`, test négatif).
Les garanties d'identité et de versionnement de PR 6 ne valent qu'à la
frontière F2.

**L'activation publique de F2 reste interdite** tant que les routes
`/api/user/{user_id}/*` (dont celle-ci) existent sans adaptation.

Dormance (`tests/test_authz_f2_dormancy.py`, `tests/test_f2_dormancy.py`,
`tests/test_f2_http_dormancy.py`) : sur le vrai `api:app`, en
sous-processus, avec ou sans variables évoquant une activation,
`GET /api/v2/me`, `PUT /api/v2/me/level` et `GET /api/v2/me/level` → 404,
aucun cookie, aucun module `core.auth_f2*` / `core.authz_f2*` chargé,
aucun accès réseau, routes publiques exactement celles de `main` ;
`api.py` ne mentionne ni `level_revision`, ni `UpdateProfileLevel`, ni
`X-Oryx-CSRF` ; matrices fermées d'imports étendues des seules entrées
nouvelles (`core.auth_f2_http.csrf` : `csrf`, `CsrfKeyring` ;
`core.auth_f2.keys` : `PURPOSES`) ; tête Alembic `0019_f2_http_admission`,
19 migrations, `requirements.txt` inchangé.

## 10. Tests

| Suite | Ajouts PR 6 |
|---|---|
| `tests/test_authz_f2_runner.py` | lecture du couple en une requête après `T_authorization` ; matrice CAS (y compris révision future) avec preuve d'absence d'écriture par `xmin` ; conflit après autorisation, sans `executed` ; session invalide prioritaire ; cycle et rejeu ; 2^53 ± 1 et 2^63 − 2 ; maximum (identique 200, changement 503 + alerte) ; niveaux inconnus en base ; lignes incohérentes (doubles de connexion) ; écriture conditionnelle sans ligne = invariant ; validation de la commande et de la vue ; profil absent jamais créé ; profil d'autrui intact ; panne à chaque point de la phase B ; rollback après exécution ; COMMIT refusé ; COMMIT incertain puis rejeu → conflit ; délai de verrou ; ordre exact des verrous et de `T_authorization` |
| `tests/test_authz_f2_concurrency.py` | deux sessions d'un même utilisateur (trois paires de niveaux, deux ordres) ; deux demandes identiques ; deux utilisateurs ; profil et portefeuille dans les deux ordres ; expiration pendant l'attente du profil, de la session, de la génération ; révocation avant, pendant, après ; transactions F2 (résolution, connexion OTP) dans les deux sens ; écriture historique concurrente ; charge mixte (bascules concurrentes, portefeuille, résolution, écritures historiques, lectures) avec `pg_stat_database.deadlocks` inchangé |
| `tests/test_authz_f2_http.py` | succès et matrice HTTP ; cycle, rejeu ; 2^53 ; maximum ; profil incohérent (GET et PUT) ou absent ; `user_id` client ignoré ; GET sans effet après mutation ; provenance (16 refus, doublons, Origin autorisée ≠ Host) ; CSRF (9 refus, rotation, horloge défaillante, dépendance absente avec sélecteur incorrect → 503, composition invalide) ; sélecteur ; cookies ; query ; Content-Type (10 refus, 3 acceptations) ; framing par appels ASGI bruts (Content-Length mal formés dont chiffres Unicode, doublons, `Transfer-Encoding`, discordances, déconnexion) ; borne 4 096 / 4 097 ; 413 annoncé ou en flux ; 415 et 400 prioritaires sur 413 ; JSON (18 corps) ; niveaux (12) ; révisions non canoniques (31) ; cookie désigné ; 401 ; runner absent, en panne, résultats inattendus ou incohérents → 503 ; ordre exact des contrôles par instrumentation ; `level` avant `expected_revision` ; routage 404 / 405 ; absence de CORS ; absence de fuite (réponses et journaux) ; parcours bout en bout bootstrap PR 3 → résolution → GET → PUT → 409 → logout ; contournement historique |
| `tests/test_authz_f2_boundary.py` | inventaire SQL étendu (UPDATE `users` filtré `id` + révision), registre de neuf commandes, plan `PROFILE_MUTATION` sans verrou métier |
| `tests/test_authz_f2_dormancy.py`, `tests/test_f2_dormancy.py` | sonde `PUT /api/v2/me/level`, matrices d'imports |

Les tests de PR 2 à PR 5 sont conservés ; seules les assertions portant
sur l'ancien corps de `GET /api/v2/me` (désormais avec `level_revision`),
sur `ProfileView` à deux champs, sur le registre à huit commandes, sur les
matrices fermées et sur la signature de `build_isolated_personal_app`
(trousseau CSRF désormais obligatoire) ont été adaptées au contrat PR 6,
sans relâchement.

## 11. Limites et points d'audit

- Contournement historique ci-dessus : **bloquant pour l'activation
  publique**.
- `Content-Length` à zéros initiaux (`0004097`) : admis comme `1*DIGIT`
  (RFC 9110) et jugé sur sa valeur.
- Requête sans `Content-Length` ni `Transfer-Encoding` (HTTP/2 par
  exemple) : admise, bornée par la lecture du flux (413 au-delà de
  4 096).
- Le conflit est rendu par ROLLBACK (aucune écriture n'a eu lieu) ; le
  succès sans changement est committé (verrou seul, aucune ligne
  modifiée).
- Le plan `READ` de `GET /api/v2/me` prend `users FOR KEY SHARE` : une
  lecture attend une mutation de profil en cours (borné par
  `lock_timeout`).
- Garanties logiques (registre fermé, inventaire audité, tests négatifs),
  pas un bac à sable Python ; concurrence testée en threads à connexions
  PostgreSQL distinctes, pas en processus.
- Exécution locale : PostgreSQL 16 ; PostgreSQL 16 et 18 par la CI.
