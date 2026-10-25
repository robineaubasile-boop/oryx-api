# R1 PR 5 — frontière d'autorisation F2 et route personnelle isolée (dormantes)

Références normatives : **C.5.2** (frontière d'autorisation, identités
serveur, appartenance, HTTP personnel isolé, transactions, dormance),
**C.5.2-A** (classification des erreurs, instant `T_authorization`,
expiration, tests), **C.5.2-B** (dormance des tests historiques,
composition d'intégration isolée, runner transactionnel), **BF1** (runner
à commandes typées en remplacement de toute interface
`operation(conn, principal)`) et **BF2** (plan de verrous explicite,
`T_authorization` lu après les verrous). Priorité en cas de divergence :
BF1/BF2 → C.5.2-B → C.5.2-A → C.5.2. Les documents normatifs ne sont pas
versionnés dans le dépôt.

Référence de compatibilité : `main` au SHA
`95877a7c90fbaa8891cda9a5667024535a1e6e21` (PR #224, R1 PR 4 fusionnée),
identique au `main` au moment de l'implémentation. Tête Alembic inchangée :
`0019_f2_http_admission`. **Aucune migration, aucune dépendance de
production, aucun secret, aucune variable d'environnement.**

**PR 5 n'est pas une authentification publique opérationnelle.** F2 reste
non monté dans `api:app`. Les anciennes routes `/api/user/{user_id}` ne
sont **pas** sécurisées par cette PR : elles restent inchangées, fondées sur
un `user_id` fourni par le client.

## 1. Périmètre livré

| Élément | Fichier |
|---|---|
| Interface interne de validation de session (extraite du moteur F2, sans changement de comportement) | `core/auth_f2/sessions.py` (nouveau), `core/auth_f2/service.py` (délégation) |
| Principal serveur immuable et contexte autorisé | `core/authz_f2/principal.py` |
| Commandes typées, plans de verrous, vues | `core/authz_f2/commands.py` |
| Adaptateurs SQL privés audités et registre fermé | `core/authz_f2/_adapters.py` |
| `AuthorizedTransactionRunner` | `core/authz_f2/runner.py` |
| Provenance des GET personnels | `core/authz_f2_http/provenance.py` |
| `GET /api/v2/me` | `core/authz_f2_http/routes.py` |
| Application personnelle isolée | `core/authz_f2_http/isolated.py` |
| Composition de test F2 + PR 5, fixtures A / B | `tests/authz_f2_support.py` |

Non livré (PR 6 à PR 9) : CRUD des endpoints personnels, créations et
mutations de profil, portefeuille, thèses, Décrypter (réservation /
finalisation), ACK HTTP, `level_revision`, activation publique.

## 2. Frontière d'autorisation

- **Justificatif** : sélecteur non secret `X-Oryx-Generation-Id` (UUID
  canonique) + cookie EXACT `__Host-oryx-active-<generation_id>`, lu par le
  parseur strict de PR 3 (`core/auth_f2_http/cookies.py`). Un cookie n'est
  jamais choisi implicitement ; un autre cookie actif présent n'est jamais
  utilisé.
- **Validation** : `core.auth_f2.sessions` (`read_session`,
  `lock_session`, `session_valid`) — le code même de
  `F2AuthService._lock_session` / `_session_valid` /
  `_session_prevalidated`, extrait et partagé : aucun second validateur
  HMAC, version EXACTE de `K_active` (aucune substitution).
- **Livraison confirmée obligatoire** : `delivery_status = 'confirmed'`.
  Le runner n'appelle jamais `authenticate_session()` et n'écrit jamais
  `delivery_status`, `delivery_confirmed_at` ni `delivery_proof_version` :
  seule `/api/auth/f2/session/resolve` (PR 3) confirme.
- **Principal** : `AuthorizedPrincipal(user_id, session_id, generation_id,
  auth_method)`, dataclass gelée, construite par le runner depuis les
  lignes PostgreSQL verrouillées et jugées à `T_authorization`. Aucun
  `user_id` client n'est accepté ni lu.

## 3. Protocole (lecture et mutation)

Chaque appel `runner.run(credential, command)` :

**Phase A — pré-validation.** Transaction courte (`core.auth_f2.txn`,
`lock_timeout = 5s`, `statement_timeout = 10s`), sans verrou ni écriture :
lignes de session relues, validateur F2, livraison confirmée →
`candidate_user_id`. Fin de transaction. Aucune autorisation définitive.

**Phase B — autorisation définitive.** Nouvelle transaction :

1. `users` selon le plan (`FOR KEY SHARE` en lecture, `FOR UPDATE` en
   mutation), filtre `candidate_user_id` ;
2. `oryx_sessions FOR SHARE` puis `oryx_session_generations FOR SHARE` ;
3. verrous métiers de la commande (lignes filtrées par id **et** user_id) ;
4. `T_authorization = clock_timestamp()` ;
5. session/génération actives, non révoquées, `T_authorization <
   absolute_expires_at`, secret, livraison confirmée ; `session.user_id =
   candidate_user_id` ; profil présent ;
6. appartenance de la ressource ;
7. adaptateur privé, puis COMMIT ; toute erreur → ROLLBACK intégral.

Le même protocole sert aux lectures (plan `READ`, aucune écriture) et à la
mutation de démonstration (plan `USER_MUTATION`). Chaque GET `/me`
réauthentifie le justificatif dans ses propres transactions : un succès
antérieur de `/session/resolve` ne confère aucun droit réutilisable.

## 4. `AuthorizedTransactionRunner` et commandes typées (BF1)

- Seul propriétaire des transactions : ouverture, garde-fous, verrous,
  décision, COMMIT, ROLLBACK. Interface publique unique :
  `run(credential, command) -> AuthorizationOutcome(status, value,
  principal)`.
- **Registre fermé** (`_adapters.REGISTRY`, `MappingProxyType`) : type
  EXACT de commande → plan + adaptateurs. Toute autre valeur (fonction,
  lambda, sous-classe, chaîne, classe) → `UnknownCommand`, avant toute
  transaction. L'ancienne interface `operation(conn, principal)` n'existe
  plus comme contrat (test négatif : une telle fonction est refusée).
- **Commandes = données** gelées, sans `user_id` : `ReadProfile`,
  `ListPositions`, `ReadPosition(position_id)`, `ListTheses`,
  `ListThesesForTicker(ticker)`, `ReadDelivery(assistant_turn_id)`,
  `ListConversationDeliveries(conversation_key)`, et la mutation de
  démonstration interne `UpdatePositionQuantity(position_id, quantity)`
  (aucune route HTTP).
- **Accès restreint** : après la décision, les adaptateurs reçoivent
  `AuthorizedContext(principal, authorized_at)` — ni connexion, ni
  `Session`, ni `Engine`, ni `SessionLocal`, ni DBAPI, ni `commit` /
  `rollback` / `begin`, ni SQL arbitraire. Seuls les adaptateurs privés de
  `_adapters.py` reçoivent la connexion, pour leurs requêtes constantes.
- **Inventaire** : tableau en tête de `core/authz_f2/_adapters.py`, vérifié
  par `tests/test_authz_f2_boundary.py` (SQL seulement dans ce module, en
  constantes, sans instruction transactionnelle ni composition dynamique,
  toujours filtré par `:u`, individuel = `id = :id AND user_id = :u`).
- **Limite assumée** : la garantie repose sur l'interface restreinte, le
  registre fermé, l'inventaire audité et les tests négatifs ; elle
  n'isole pas le processus contre du code Python hostile exécuté dans le
  même interpréteur.

## 5. Plan de verrous (BF2)

| Plan | Ordre |
|---|---|
| `READ` | `users FOR KEY SHARE` → `oryx_sessions FOR SHARE` → `oryx_session_generations FOR SHARE` |
| `USER_MUTATION` | `users FOR UPDATE` → `oryx_sessions FOR SHARE` → `oryx_session_generations FOR SHARE` → `portfolio_positions FOR UPDATE` (id ET user_id) |

Compatibilité avec l'ordre global F2 (`état de clé → sujets → invitation →
profil users → opération → candidat → demande OTP → tentative → session →
génération → identité`) : PR 5 ne prend que les rangs `users`, `session`,
`génération` dans cet ordre, puis des tables métiers qu'aucune transaction
F2 ne verrouille. Verrous de clés étrangères : la connexion OTP prend
`users FOR KEY SHARE` explicitement avant ses insertions (session, FK) ;
elle attend une mutation PR 5 (`FOR UPDATE`) sans détenir de rang
supérieur. La résolution (génération `FOR UPDATE`) et la révocation
(session puis générations `FOR UPDATE`) ne prennent pas `users` : aucun
cycle. Prouvé par des tests sur connexions réelles dans les deux sens
(`tests/test_authz_f2_concurrency.py`) et une charge mixte sans
interblocage.

Une commande ne peut verrouiller qu'une table de son plan (test statique) ;
une lecture n'en verrouille aucune. Un verrou hors plan exige un nouveau
plan soumis à audit.

## 6. `T_authorization`

- Instant faisant autorité : `clock_timestamp()` lu **après** les verrous
  de décision (étape 4), jamais l'heure de pré-validation, du frontend ni
  d'entrée HTTP.
- Valide si `T_authorization < absolute_expires_at` ; l'égalité et les
  instants postérieurs refusent.
- Expiration pendant l'attente d'un verrou (users ou métier) : jugée à
  `T_authorization`, refus, aucune mutation.
- Expiration après `T_authorization` : la transaction autorisée se
  termine et committe ; la requête suivante est refusée.
- Révocation committée avant l'étape 2 : vue par `FOR SHARE` (dernière
  version), refus. Révocation commencée après l'étape 2 : son `FOR UPDATE`
  sur `oryx_sessions` attend le COMMIT ; la mutation peut committer avant
  elle, aucune mutation n'est autorisée ensuite.
- Aucun appel externe (Claude, EODHD, Supabase, Resend, réseau) dans une
  transaction du runner ; la frontière n'importe aucun client réseau.

## 7. Classification 401 / 503 (C.5.2-A)

| Résultat | Cas |
|---|---|
| `SESSION_INVALID` → 401 `session_invalid` | justificatif absent ou mal formé, cookie désigné absent, secret incorrect, génération inconnue, session ou génération révoquée, expiration (`≥`), livraison `pending`, version historique de `K_active` inconnue |
| `UNAVAILABLE` → 503 `temporarily_unavailable` | panne PostgreSQL, délai de verrou ou d'instruction, COMMIT en échec, erreur imprévue, autoriseur non configuré ou impossible à construire, profil absent ou incohérent avec une session valide |
| `NOT_FOUND` (interne) | ressource étrangère ou inexistante : même résultat, rien n'écrit |

Aucune version de clé, aucun secret, aucune raison interne n'est exposé ;
les journaux ne portent que le nom de commande, le statut et le type
d'exception.

## 8. Contrat `GET /api/v2/me`

Présent **uniquement** dans l'application personnelle isolée
(`build_isolated_personal_app`) et dans la composition de test.

Ordre des contrôles (le premier échec répond) :

| # | Contrôle | Refus |
|---|---|---|
| 1 | méthode / chemin | 405 / 404 |
| 2 | provenance GET personnelle | 403 `origin_refused` |
| 3 | en-tête Cookie (parseur strict PR 3) | 400 `invalid_request` |
| 4 | aucune query | 400 `invalid_request` |
| 5 | aucun corps (Content-Length ou flux > 4096 octets → 413 `request_too_large` ; tout autre corps, Content-Length dupliqué ou illisible → 400) | 413 / 400 |
| 6 | `X-Oryx-Generation-Id` unique, UUID canonique | 400 `invalid_request` |
| 7 | cookie actif EXACT désigné | 401 `session_invalid` |
| 8 | runner + `ReadProfile` | 401 / 503 |

Provenance : Host unique, canonique, `https://<Host>` parmi les origines
autorisées ; Origin présent → unique et identique ; Origin absent →
`Sec-Fetch-Site: same-origin` obligatoire ; `Sec-Fetch-Site` présent →
`same-origin` seulement ; Referer présent → https, sans identifiants, même
origine ; en-têtes dupliqués refusés ; aucun CORS. La politique de PR 3 est
inchangée.

Réponse 200 : `{"level": "<users.level>", "user_id": "<principal.user_id>"}`
(sans `level_revision`). Toutes les réponses : `Cache-Control: no-store`,
`Pragma: no-cache`, `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, **aucun `Set-Cookie`** (nettoyage ciblé : routes PR 3). Aucun
profil créé, aucune confirmation de livraison.

## 9. Matrice de propriété des ressources

| Ressource | Commande | Filtre SQL | Étrangère / inexistante |
|---|---|---|---|
| `users` | `ReadProfile` | `id = principal.user_id` | — (absence : 503, jamais créée) |
| `portfolio_positions` | `ListPositions` | `user_id` | liste vide |
| `portfolio_positions` | `ReadPosition` | `id AND user_id` | `NOT_FOUND` identique |
| `portfolio_positions` | `UpdatePositionQuantity` | verrou `id AND user_id FOR UPDATE` | `NOT_FOUND`, aucune écriture, ligne d'autrui jamais verrouillée |
| `investment_theses` | `ListTheses`, `ListThesesForTicker` | `user_id` (+ `ticker`) | ticker de B : vide |
| `assistant_deliveries` | `ReadDelivery` | `id AND user_id` | `NOT_FOUND` identique |
| `assistant_deliveries` | `ListConversationDeliveries` | `user_id AND conversation_key` | `conversation_key` de B : vide |

Les vues ne portent ni `private_metadata`, ni `request_fingerprint`, ni
`response_payload`. Ces fonctions sont internes (pas d'endpoint en PR 5).

## 10. Dormance publique

- `api.py` inchangé : aucun import F2 / PR 5, aucun `include_router`,
  `mount`, middleware, lifespan, aucune variable lue, aucun `Set-Cookie`.
- Sonde du vrai `api:app` en sous-processus (garde réseau, environnements
  contrôlés, avec drapeaux F2 / PR 5 valides et invalides) :
  `GET /api/v2/me` → 404, aucun cookie, aucun module `core.auth_f2`,
  `core.auth_f2_http`, `core.authz_f2`, `core.authz_f2_http` chargé, aucun
  accès réseau (`tests/test_f2_http_dormancy.py` étendu,
  `tests/test_authz_f2_dormancy.py`).
- Tests historiques adaptés **sans relâchement** : la mention de F2 hors
  des paquets F2 n'est admise que par la matrice fermée
  `F2_REFERENCE_MATRIX` (fichier → modules F2 exacts, noms importables
  listés, jamais `F2AuthService`, `build_isolated_f2_app`, fournisseurs ni
  initialisation anti-abus) ; matrice fermée des imports `core.*` de la
  frontière ; aucun paquet exempté en bloc.

## 11. Tests et limites des garanties

| Suite | Contenu |
|---|---|
| `tests/test_authz_f2_runner.py` | classification cryptographique (dont non-substitution de `K_active`), pending / confirmation, absence d'effet de bord, générations croisées, appartenance A / B, registre fermé, contexte restreint, rollback après écriture, `T_authorization` (ordre, égalité, expiration postérieure), délai de verrou, absence de réseau |
| `tests/test_authz_f2_concurrency.py` | révocation entre phases, pendant une attente, après autorisation ; expiration pendant l'attente du verrou users et d'un verrou métier ; compatibilité avec F2 dans les deux sens ; charge mixte sans interblocage |
| `tests/test_authz_f2_http.py` | provenance, Host, Origin, Sec-Fetch-Site, Referer, doublons, cookies, query, corps, tailles, sélecteur, priorités, statuts exacts, no-store, aucun Set-Cookie, 503 ; parcours `pending → resolve → /me`, révocation, expiration, autre génération, CSRF de `/session/resolve` préservé |
| `tests/test_authz_f2_boundary.py` | inventaire SQL, absence de contrôle transactionnel, plans, ordre des verrous, absence de client réseau, disparition de `operation(conn, principal)` |
| `tests/test_authz_f2_dormancy.py` + tests historiques | dormance de `api:app`, matrices fermées, aucune migration ni dépendance |

Limites : isolation logique (pas de bac à sable Python) ; plan `READ` qui
prend `users FOR KEY SHARE` (une lecture attend une mutation PR 5 ou une
liaison e-mail en cours du même profil, borné par `lock_timeout`) ; mutation
de démonstration interne seulement ; tests de concurrence en threads à
connexions distinctes (pas en processus) ; PostgreSQL 16 exécuté
localement, 16 et 18 par la CI.

## 12. Étapes reportées

- **PR 6 à PR 9** : endpoints personnels complets (profil, portefeuille,
  thèses, Décrypter avec réservation / finalisation hors transaction
  verrouillée, ACK HTTP), plans de verrous spécifiques soumis à audit pour
  chaque nouvelle commande, `level_revision`, puis activation publique et
  retrait des routes `/api/user/{user_id}` — chacune hors du périmètre de
  PR 5.
