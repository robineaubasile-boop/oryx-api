# R1 PR 3 — couche HTTP d'authentification F2 (dormante)

Référence normative indiquée pour PR 3 : C.3.3, annexe C.3.3-A et
compléments A18 à A20 (validés le 23/10/2026). **Ces textes ne figurent
pas dans le dépôt** ; les textes complets C.2.1 et C.2.1-A non plus. Cette
PR s'appuie sur les décisions attestées par le code livré par PR 2
(`core/auth_f2/`), sur `docs/f2_r1_pr2.md` et sur le cahier des charges de
la mission PR 3. **Réserve** : une réconciliation littérale avec les textes
complets C.2.1 / C.2.1-A / C.3.3 / C.3.3-A reste à faire ; les choix
signalés au §12 sont ceux qu'aucun texte disponible ne fixait.

## 1. Architecture livrée

Une couche HTTP séparée (`core/auth_f2_http/`) entoure `F2AuthService`.
Elle ne réimplémente ni l'OTP, ni les quotas, ni la cryptographie F2, ni
les sessions, ni les transactions de PR 2 : elle valide le transport,
désigne explicitement un justificatif, appelle le moteur, puis traduit son
résultat par une matrice fermée.

| Module | Rôle |
|---|---|
| `cookies.py` | noms `__Host-oryx-*`, lecture stricte de `Cookie`, écriture de `Set-Cookie` |
| `csrf.py` | protocole de test `oryx-f2-csrf-v1` (trousseau propre, versionné) |
| `provenance.py` | contrôle d'origine (Origin exact, ou même origine attestée) |
| `client_ip.py` | résolveurs d'IP injectables (pair TCP, proxys approuvés) |
| `contract.py` | matrice `(endpoint, Outcome.status, Outcome.reason)` |
| `routes.py` | routes `/api/auth/f2/*` |
| `isolated.py` | composition d'application **isolée** (tests uniquement) |

`core/auth_f2/` n'est **pas modifié**. Aucune migration, aucune dépendance
de production (`requirements.txt` inchangé ; httpx reste la dépendance de
test explicite de `ci/requirements-ci.in`, verrouillée par empreinte).

## 2. Dormance de production

`api.py` n'est pas modifié et n'importe ni `core.auth_f2` ni
`core.auth_f2_http`. `tests/test_f2_http_dormancy.py` importe le **vrai**
`api.py` dans un sous-processus à environnement contrôlé, avec une garde
réseau qui fait échouer toute connexion sortante, dans trois environnements
(aucune variable ; variables F2 « valides » et drapeaux d'activation
`ORYX_F2_ENABLED`, `ORYX_AUTH_F2`, `F2_ENABLED`, `ORYX_F2_HTTP` ; variables
F2 invalides) et vérifie :

- routes exactement celles de `main` (liste figée `MAIN_ROUTES`), aucune
  sous `/api/auth` ; middlewares : `CORSMiddleware` seul ;
- `GET /api/auth/f2/csrf/bootstrap`, `POST …/sign-in/start`, `…/verify`,
  `…/session/resolve`, `…/logout` → 404, sans `Set-Cookie` ; `/health`
  inchangé ;
- aucun module `core.auth_f2*` chargé ; aucune tentative de connexion
  réseau (aucun appel fournisseur, aucune base) ;
- `api.py` ne mentionne ni `auth_f2`, ni `/api/auth`, ni `SUPABASE`,
  `RESEND`, `ORYX_F2`, `csrf`, `include_router`, `mount(` ;
- seul `tests/` compose l'application isolée (balayage de tout le dépôt) ;
- `core/auth_f2_http` ne lit aucune variable d'environnement, n'importe ni
  `os`, ni `api`, ni `core.db`, ni httpx, n'appelle ni
  `initialize_abuse_key_state` ni `rotate_abuse_key`, n'a ni `on_event` ni
  `lifespan` ; son import ne charge ni `api`, ni httpx, ni `core.db`.

`tests/test_f2_dormancy.py` (PR 2) est conservé ; seule adaptation :
`core/auth_f2_http` est exclu, comme `core/auth_f2`, du balayage « aucun
module applicatif ne référence le moteur ». Ses autres garanties (routes et
hooks d'api.py, absence de configuration dans le moteur, import sans client
réseau, `requirements.txt`) sont inchangées.

La construction de l'application isolée n'ouvre aucune connexion,
n'appelle aucun fournisseur, n'écrit rien et **n'initialise pas**
`auth_abuse_key_state` : sans initialisation explicite, toute opération est
refusée en 503 (`test_abuse_key_state_is_never_initialized_implicitly`).

## 3. Composition isolée

`build_isolated_f2_app(engine, keyring, supabase, mailer, csrf_keyring,
ip_resolver, allowed_origins, clock=None, fault_hook=None, wall_clock,
monotonic)` construit une application FastAPI **neuve** (sans OpenAPI ni
CORS) contenant les seules routes F2. Validation stricte : moteur
PostgreSQL, `Keyring` à six usages, fournisseurs dotés des méthodes
attendues (doubles ou `HttpSupabaseOtpProvider` / `HttpResendMailer` avec
transport injecté), trousseau CSRF dont **aucune clé n'est une clé F2**,
résolveur d'IP, origines `https://<hôte>[:port]` canoniques, sans doublon.
Aucune valeur secrète n'est journalisée (repr masqués, journaux limités à
l'endpoint, au statut HTTP et au code de réponse).

## 4. Routes et contrats

Espace de noms `/api/auth/f2`.

| Route | Corps JSON (clés exactes) | Moteur |
|---|---|---|
| `GET /csrf/bootstrap` | — | aucun |
| `POST /sign-in/start` | `email` | `start_sign_in` |
| `POST /sign-in/resend` | `operation_id`, `candidate_id` | `resend_sign_in_otp` |
| `POST /sign-in/verify` | `operation_id`, `candidate_id`, `code` | `verify_sign_in` |
| `POST /sign-in/resume` | `operation_id`, `candidate_id` | `resume_result` |
| `POST /session/resolve` | `generation_id` | `authenticate_session` |
| `POST /logout` | `generation_id` | `authenticate_session` puis `revoke_session(…, "logout")` |

Contrôles de chaque mutation, **tous avant le moteur** (prouvé par un
moteur simulé qui enregistre chaque appel) : provenance → en-tête Cookie →
CSRF (`X-Oryx-CSRF`) → `Content-Type: application/json` (charset utf-8
seul admis) → corps ≤ 4 Ko (avec ou sans `Content-Length`), UTF-8 strict,
objet JSON, clés exactes, clés dupliquées et `NaN` refusés, UUID
canoniques, e-mail ≤ 320 caractères → cookie désigné par l'identifiant
exact → IP client (start, resend, verify).

Le code OTP n'est contrôlé que comme **chaîne JSON** : sa forme est jugée
par le moteur, qui compte un code mal formé comme un échec (PR 2). Un code
`"abc"` ou de 300 caractères atteint donc le moteur et incrémente les
compteurs (`test_wrong_and_malformed_codes_are_counted_by_the_engine`) ;
seules les bornes de transport (4 Ko) s'appliquent.

Réponses : JSON, `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (y
compris 404 / 405 de l'application isolée). Succès :
`{"status": …}` + sélecteurs non secrets ; erreurs : `{"error": <code>}`
seulement.

| Refus de transport | HTTP | Code |
|---|---|---|
| provenance refusée | 403 | `origin_refused` |
| CSRF absent, invalide, expiré, autre liaison | 403 | `csrf_invalid` |
| Cookie hors contrat, JSON invalide, schéma | 400 | `invalid_request` |
| Content-Type | 415 | `invalid_request` |
| corps > 4 Ko | 413 | `request_too_large` |
| cookie candidat désigné absent | 401 | `credential_missing` |
| cookie actif désigné absent ou session invalide | 401 | `session_invalid` |
| limite de 16 cookies atteinte (start, bootstrap) | 409 | `credential_limit` |
| IP non fiable, exception du moteur, état inattendu | 503 | `temporarily_unavailable` |

## 5. Cookies

Formats de PR 2 : `__Host-oryx-candidate-<candidate_id>` (secret du
`CandidateCredential` seul), `__Host-oryx-active-<generation_id>` (secret
du `SessionCredential` seul), plus `__Host-oryx-csrf-binding`. Attributs :
`Secure; HttpOnly; Path=/; SameSite=Lax`, sans `Domain`.

- **Sélection explicite** : la route reçoit l'identifiant exact dans le
  corps, lit le cookie de ce nom et transmet le justificatif au moteur, qui
  le juge. Aucun repli (premier, dernier, unique cookie présent) ; un
  identifiant seul n'est jamais une preuve.
- **Lecture stricte** : un seul en-tête `Cookie`, ≤ 8 Ko ; tout nom
  commençant par `__host-oryx-` (casse ignorée) doit être exactement l'un
  des trois formats, UUID canonique ; ≤ 16 cookies `__Host-oryx-*` ; nom
  dupliqué refusé ; valeur = 43 caractères base64url canoniques. Les
  autres cookies sont ignorés.
- **Candidat** : posé par `start` (issued, issue_uncertain, send_failed)
  avec `Max-Age = 15 min − durée écoulée depuis l'entrée de la requête`
  (horloge monotone ; le candidat est créé après cette entrée, donc le
  cookie expire au plus 15 min après sa création). Jamais renouvelé par un
  renvoi. N'étend aucune échéance PostgreSQL : le moteur juge seul (10 min
  d'authentification, reprise 5 min après le commit).
- **Actif** : posé seulement par `authenticated` / `resumed` (IssuedSession
  complète), `Max-Age = min(15 j − écoulé, absolute_expires_at − heure
  murale)` pour `authenticated` ; `min(15 j − 5 min − écoulé, …)` pour
  `resumed` (la reprise n'est admise que moins de 5 min après le commit) :
  jamais au-delà de l'expiration absolue en base, sans dépendre de l'accord
  des horloges. Le cookie candidat correspondant est effacé dans la même
  réponse.
- **Nettoyage ciblé** : un succès efface le seul cookie candidat utilisé ;
  une session invalide (`resolve`, `logout`) efface le seul cookie actif
  désigné ; `logout` révoque la seule session authentifiée par ce cookie.
  Aucune autre session, d'aucun appareil, n'est touchée.
- **Limite** : `start` (et `bootstrap` s'il doit créer la liaison) refuse
  quand le navigateur porte déjà 16 cookies Oryx ; `verify` / `resume`
  remplacent un candidat par un actif (solde nul). La couche HTTP ne peut
  donc jamais faire dépasser la borne.

## 6. CSRF (`oryx-f2-csrf-v1`)

Cookie de liaison `__Host-oryx-csrf-binding` : 32 octets aléatoires,
HttpOnly, 24 h, réutilisé par chaque bootstrap (les onglets gardent leurs
jetons). Jeton `version.exp.nonce.mac` : version de clé CSRF
(`[A-Za-z0-9_-]{1,32}`), `exp` = t + 30 min (secondes, décimal canonique),
nonce de 16 octets, `HMAC-SHA256(K_csrf[version], M)` où `M` =
`"ORYX-F2-CSRF\0" ‖ u16(len tag) ‖ "oryx-f2-csrf-v1" ‖` pour chaque champ
`u32(len) ‖ champ` (version, exp, nonce, secret de liaison, **origine**).
Base64url canonique partout (bits de remplissage parasites refusés),
comparaison en temps constant, `now ≥ exp` refusé, `exp − now > 30 min`
refusé, version inconnue refusée **sans substitution**. Rotation explicite :
trousseau {ancienne, nouvelle (courante)} puis {nouvelle}. Clés propres,
indépendantes des six clés F2 (vérifié à la composition). Un jeton CSRF
n'est jamais une preuve d'authentification (`test_a_csrf_token_is_not_an_authentication`).

`GET /csrf/bootstrap` ne crée ni opération, ni session, ni appel moteur.

## 7. Provenance

Même règle pour le bootstrap et les mutations :

- `Origin` présent (une fois) : égal à une origine autorisée ; `null`,
  autre schéma, autre hôte, barre finale, casse différente : refus ;
  `Sec-Fetch-Site`, s'il est présent, vaut `same-origin` ou `same-site`.
- `Origin` absent : accepté seulement si `Sec-Fetch-Site: same-origin`
  (posé par le navigateur), `https://<Host>` autorisé, et `Referer` (s'il
  est présent) de cette même origine. Sinon (absent, `cross-site`, `none`,
  Referer étranger ou illisible, en-têtes dupliqués) : refus.

## 8. IP client

`PeerAddressResolver` (pair TCP, application exposée sans proxy) et
`TrustedProxyResolver(réseaux)` (pair TCP approuvé exigé, `X-Forwarded-For`
unique lu de droite à gauche en sautant les proxys approuvés ; valeurs non
IP, ports, crochets, en-tête dupliqué, chaîne > 16 entrées : refus). Un
`X-Forwarded-For` envoyé directement par un client non approuvé est ignoré
et la requête refusée (503) **avant** le moteur.

**Prérequis bloquant avant activation réelle** : la chaîne de proxys de
Railway (adresses des pairs, nombre de sauts, en-tête fiable) n'est pas
établie. PR 3 ne fournit ni ne suppose aucune configuration de confiance
Railway ; les essais restent limités aux contextes isolés contrôlés.

## 9. Matrice des résultats

Fermée (`contract.MATRIX`, figée par `tests/test_f2_http_contract.py`).
Tout triplet absent, tout motif non chaîne, et toute valeur incohérente
(candidat ou session inattendus, identifiants ou opération différents,
secret non canonique, `resumed` incohérent, expiration naïve ou passée)
donne **503 `temporarily_unavailable` sans cookie**. Seuls `authenticated`
et `resumed` avec une `IssuedSession` complète posent un cookie actif.

| Endpoint | status | reason | HTTP | Réponse |
|---|---|---|---|---|
| start | issued | — | 200 | `code_sent` + cookie candidat |
| start | issue_uncertain | generate_uncertain, — | 202 | `delivery_uncertain` + cookie candidat |
| start | send_failed | send_failed, superseded, — | 202 | `delivery_failed` + cookie candidat |
| start | rejected | invalid_email | 422 | `invalid_email` |
| resend | issued / issue_uncertain / send_failed | (idem) | 200 / 202 / 202 | idem, **sans** cookie |
| resend | rejected | operation_unavailable, credential_invalid | 409 | `operation_unavailable` |
| start, resend, verify | rejected | ip_unavailable | 503 | `temporarily_unavailable` |
| start, resend | refused | email_blocked, issue_interval, quota_email_issue, quota_ip_issue | 429 | `rate_limited` |
| verify | authenticated | — | 200 | `authenticated` + cookie actif |
| verify | resumed (rejeu technique) | — | 200 | `resumed` + cookie actif |
| verify | refused | quota_ip_verification, email_blocked | 429 | `rate_limited` |
| verify | rejected | email_blocked | 429 | `rate_limited` |
| verify | rejected | operation_unknown, operation_closed, operation_expired, credential_invalid | 409 | `operation_unavailable` |
| verify | rejected | no_active_code | 409 | `code_unavailable` |
| verify | rejected | code_expired | 409 | `code_expired` |
| verify | rejected | request_exhausted | 409 | `code_exhausted` |
| verify | rejected | code_not_ready | 409 | `code_not_ready` |
| verify | rejected | verification_in_progress | 409 | `verification_in_progress` |
| verify | rejected / uncertain | verification_uncertain / provider_uncertain | 409 | `verification_uncertain` |
| verify | rejected | code_malformed, code_incorrect | 401 | `code_incorrect` |
| verify | rejected | code_replaced | 401 | `code_replaced` |
| verify | rejected | key_unavailable | 503 | `temporarily_unavailable` |
| verify | rejected | provider_rejected, proof_not_applicable, identity_not_linked, verification_superseded, resume_unavailable | 401 | `sign_in_failed` |
| resume | resumed | — | 200 | `resumed` + cookie actif |
| resume | rejected | resume_unavailable | 409 | `resume_unavailable` |
| resume | rejected | key_unavailable | 503 | `temporarily_unavailable` |
| tous | tout autre triplet | | 503 | `temporarily_unavailable` |

Inventaire des motifs : analyse syntaxique de `core/auth_f2/service.py`
(chaque `Outcome(...)`, constantes et expressions dynamiques) et de
`abuse.issue_refusals`. Valeurs dynamiques couvertes : statut de
`record_send` ; `bind_code` (`send_failed` | `superseded`) ;
`_certain_failure` (`code_malformed`, `code_incorrect`, `email_blocked`,
`request_exhausted`) ; `code_not_ready` / `verification_uncertain` ;
`failure_code` **persisté** de l'opération lors d'un rejeu (pour la
connexion : `identity_not_linked` seulement ; toute autre valeur → 503).
Les motifs des parcours invitation et liaison (`session_invalid`,
`profile_already_linked`, `conflict_other_profile`,
`invitation_unavailable`, `email_linked`, `invitation_activated`,
`candidate_issued`) ne sont exposés par aucune route PR 3 et donnent 503.
Une nouvelle expression dynamique dans le moteur fait échouer le test
d'inventaire.

Remarque sur `request_exhausted` : dans PR 2, le 5ᵉ échec d'une demande
(TTL ≤ 10 min) implique 5 échecs e-mail en moins de 15 min, donc un
blocage, et le moteur rend alors `email_blocked` (429). La branche
`code_exhausted` reste mappée mais n'est pas atteinte dans les parcours
observés.

## 10. Confidentialité

- Aucune réponse ne contient `user_id`, identifiant Supabase, jeton
  fournisseur, OTP, e-mail ou secret, sauf les secrets transportés par
  leur propre `Set-Cookie` HttpOnly (balayages dans les tests de parcours
  et de transport, y compris des journaux).
- `identity_not_linked` n'est jamais exposé : `provider_rejected`,
  `proof_not_applicable`, `identity_not_linked`, `verification_superseded`
  rendent la même réponse (statut, corps, taille, en-têtes hors `date`),
  vérifié avec le vrai moteur et avec le moteur simulé. Aucun profil n'est
  créé (`test_unknown_identity_never_creates_a_profile`).
- **Limite connue (héritée de PR 2)** : après `identity_not_linked`
  l'opération est close (`failed`), alors qu'après `provider_rejected`
  elle reste ouverte ; un renvoi ultérieur sur la même opération ne répond
  donc pas pareil (409 `operation_unavailable` contre émission). Le
  contrat client recommandé après `sign_in_failed` est de recommencer par
  `start`. Rendre ce comportement indistinguable exigerait de modifier le
  moteur PR 2 (hors périmètre ; décision à prendre).
- Aucune indistinguabilité temporelle n'est revendiquée.

## 11. Incertitudes et reprise

- `issue_uncertain` / `send_failed` : le cookie candidat est posé, les
  sélecteurs `operation_id` / `candidate_id` sont rendus ; la réponse
  n'annonce jamais « envoyé » (`delivery_uncertain` / `delivery_failed`) ;
  aucune relance automatique ; seul un `resend` explicite (soumis aux
  quotas, dont 60 s) émet de nouveau.
- Vérification incertaine : jamais de session, `verification_uncertain`.
- Reprise après perte de réponse : `resume` rend **la même** session, la
  même génération et le même secret (cookie identique, octet pour octet)
  sans aucun appel Supabase ni Resend, dans la fenêtre de 5 min
  (frontières testées à 5 min − 1 µs et 5 min, horloge contrôlée). Une
  vérification tardive d'un autre onglet après le succès rend 409 sans
  cookie et ne crée pas de seconde session.
- Une réponse tardive ou une reprise ne **sélectionne** aucun compte :
  aucun état « compte courant » n'existe côté serveur ; la session n'est
  utilisée que sur désignation explicite de sa génération. L'adoption
  atomique d'un compte entre onglets relève du lot frontend.

## 12. Écarts et choix à valider

Aucun écart à l'architecture imposée connu. Choix faits faute de texte
disponible (à confirmer lors de la réconciliation) :

1. Codes de réponse et regroupements du §9 (ex. `operation_unknown`,
   `operation_closed`, `operation_expired`, `credential_invalid` →
   `operation_unavailable` ; `code_malformed` → `code_incorrect` ; IP non
   fiable → 503).
2. Le jeton CSRF est lié à l'origine ; liaison CSRF de 24 h, réutilisée.
3. `Origin` absent accepté aussi pour les mutations, aux mêmes conditions
   que pour le bootstrap ; `Sec-Fetch-Site: same-site` accepté avec une
   origine exactement autorisée.
4. `resolve` ne rend que `{"status": "active", "generation_id"}` (aucun
   `user_id`) ; le lot frontend pourra demander un contrat d'identité.
5. Cookies candidats effacés seulement en cas de succès (jamais sur un
   échec, pour ne pas perdre une reprise possible) ; limite de 16 cookies
   appliquée par refus de `start` (`credential_limit`).
6. `logout` d'un cookie invalide : 401 `session_invalid` et effacement de
   ce seul cookie ; `resolve` et `logout` confirment la livraison
   (`authenticate_session`, sémantique PR 2).
7. Le `TestClient` de Starlette 0.36.3 n'est pas compatible avec httpx
   0.28.1 (verrou de la CI) : les tests pilotent l'application par
   `httpx.ASGITransport`, sans changer les dépendances.

## 13. Tests et preuves

| Fichier | Contenu | PostgreSQL |
|---|---|---|
| `tests/test_f2_http_contract.py` | matrice = table de référence ; inventaire syntaxique des Outcome ; chaque entrée par la route réelle ; aucune branche non-session ne pose de cookie actif ; incohérences et triplets inconnus → 503 ; refus comparables | non |
| `tests/test_f2_http_transport.py` | CSRF (forme, frontières d'expiration, liaison, origine, altérations, encodages non canoniques, version inconnue, rotation, trames) ; provenance légitime et hostile ; cookies (taille, nombre, doublons, noms, valeurs) ; corps (types, tailles, chunked, doublons, NaN, BOM) ; sélecteurs sans repli ; IP (pair, proxys, en-têtes forgés) ; durées des cookies (horloges injectées) ; journaux sans secret | non |
| `tests/test_f2_http_flows.py` | parcours réels : connexion, codes incorrects / mal formés comptés, quotas, incertitudes, renvoi explicite, reprise exacte sans appel fournisseur, frontières (reprise, opération, session), absence de profil implicite, refus comparables, deux comptes dans un navigateur, multi-appareils, révocation ciblée, cookies forgés, concurrence (interleaving déterministe et 4 requêtes parallèles), composition sans effet, état anti-abus non initialisé, adaptateurs HTTP réels sur `httpx.MockTransport` | oui |
| `tests/test_f2_http_dormancy.py` | dormance du vrai `api.py` sous trois environnements, garde réseau, sources, dépendances | non |
| `tests/test_f2_dormancy.py` | garanties PR 2 conservées (adaptation du seul balayage) | non |

Non-régression : les suites PR 2 (`test_f2_service`, `test_f2_crypto`,
`test_f2_normalize_providers`, `test_f2_audit_corrections`,
`test_f2_concurrency`) passent sans modification ; le moteur n'est pas
touché. Aucun test n'utilise de secret de production.

## 14. Dépendances envers PR 2

La sécurité de PR 3 repose sur : la validation des justificatifs par
`F2AuthService` (candidat, session, génération, HMAC à clé versionnée) ;
les quotas et compteurs d'échec ; la preuve Supabase (option 1) ; la
reprise exacte sans écriture ; `authenticate_session` / `revoke_session` ;
la normalisation D1 / D2 ; le refus fermé du moteur (exceptions → 503).

## 15. Hors PR 3 (travaux reportés)

1. Activation publique : aucune route F2 dans `api.py` ; chargeur de clés
   et de configuration de production ; initialisation explicite de
   `auth_abuse_key_state`.
2. Chaîne de proxys Railway approuvée (§8).
3. Blocages listés dans `docs/f2_r1_pr2.md` §9 : routes historiques à
   `user_id` client, `level_revision`, CORS `*` d'`api.py`, SameSite avec
   les domaines réels, contrat fournisseur réel, limites de débit HTTP
   pour la reprise et les invitations, purge.
4. Parcours invitation et liaison e-mail en HTTP.
5. Lot frontend : adoption atomique d'un compte entre onglets,
   déconnexion avant une nouvelle connexion (sinon accumulation de cookies
   actifs jusqu'à la limite), contrat d'identité.
6. Indistinguabilité du comportement après `identity_not_linked` (§10).
7. Réconciliation littérale avec C.2.1 / C.2.1-A / C.3.3 / C.3.3-A.
