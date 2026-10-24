# R1 PR 3 — couche HTTP d'authentification F2 (dormante)

Référence normative indiquée pour PR 3 : C.3.3, annexe C.3.3-A et
compléments A18 à A20 (validés le 23/10/2026). **Ces textes ne figurent
pas dans le dépôt** ; les textes complets C.2.1 et C.2.1-A non plus. Cette
PR s'appuie sur les décisions attestées par le code livré par PR 2
(`core/auth_f2/`), sur `docs/f2_r1_pr2.md` et sur le cahier des charges de
la mission PR 3. **Réserve** : une réconciliation littérale avec les textes
complets C.2.1 / C.2.1-A / C.3.3 / C.3.3-A reste à faire ; les choix
signalés au §13 sont ceux qu'aucun texte disponible ne fixait.

**Révision post-audit (audit indépendant du HEAD `0073663`)** : les
corrections sont récapitulées au §12, les divergences qui demandent encore
une décision au §13, les limites du moteur PR 2 au §14.

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
moteur simulé qui enregistre chaque appel) : provenance (**Origin exigé**)
→ en-tête Cookie → CSRF (`X-Oryx-CSRF`) → `Content-Type: application/json`
(charset utf-8 seul admis) → corps ≤ 4 Ko (avec ou sans `Content-Length`),
UTF-8 strict, objet JSON, clés exactes, clés dupliquées et `NaN` refusés,
UUID canoniques, e-mail ≤ 320 caractères → cookie désigné par
l'identifiant exact → IP client (start, resend, verify).

Le code OTP n'est contrôlé que comme **chaîne JSON** : sa forme est jugée
par le moteur, qui compte un code mal formé comme un échec (PR 2). Un code
`"abc"`, vide ou de 300 caractères atteint donc le moteur et incrémente
les compteurs (`test_wrong_and_malformed_codes_are_counted_by_the_engine`,
`test_wrong_or_malformed_code_does_not_depend_on_identity`) ; seules les
bornes de transport (4 Ko) s'appliquent.

Réponses : JSON, `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (y
compris 404 / 405 de l'application isolée). Succès : `{"status": …}` +
sélecteurs non secrets ; erreurs : `{"error": <code>}`, et pour
`delivery_uncertain` / `delivery_failed` (503) aussi `operation_id` et
`candidate_id`.

| Refus de transport | HTTP | Code |
|---|---|---|
| provenance refusée (dont POST sans Origin) | 403 | `origin_refused` |
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
- **Candidat** : posé **une seule fois**, par `start` (issued,
  issue_uncertain, send_failed), avec `Max-Age = 15 min − durée écoulée
  depuis l'entrée de la requête` (horloge monotone ; le candidat est créé
  après cette entrée, donc le cookie expire au plus 15 min après sa
  création). **Il n'est ni renouvelé ni effacé** par un renvoi, une
  vérification, une reprise ou une déconnexion, y compris après un succès
  (correctif P1.1) : la reprise canonique reste possible si la réponse de
  succès est perdue ou partiellement exploitée. Sa présence n'étend aucune
  échéance PostgreSQL : le moteur juge seul (10 min d'authentification,
  reprise jusqu'à 5 min après le commit, session active et non révoquée).
  Après ces échéances, le cookie subsiste jusqu'à son `Max-Age` sans plus
  rien permettre (`test_expired_resume_window_grants_nothing_more`).
- **Actif** : posé seulement par `authenticated` / `resumed` (IssuedSession
  complète), `Max-Age = min(15 j − écoulé, absolute_expires_at − heure
  murale)` pour `authenticated` ; `min(15 j − 5 min − écoulé, …)` pour
  `resumed` (la reprise n'est admise que moins de 5 min après le commit) :
  jamais au-delà de l'expiration absolue en base, sans dépendre de l'accord
  des horloges.
- **Nettoyage ciblé** : une session invalide (`resolve`, `logout`) efface
  le seul cookie actif désigné ; `logout` révoque la seule session
  authentifiée par ce cookie. Aucune autre session, d'aucun appareil,
  n'est touchée.
- **Limite de 16 cookies, garantie par construction** : seuls `bootstrap`
  (liaison) et `start` (candidat) ajoutent un cookie sans en désigner un
  existant ; `verify` / `resume` ajoutent au plus UN cookie actif par
  candidat (une génération par candidat, même nom à la reprise). Comme les
  candidats sont désormais conservés après succès, `start` n'est admis que
  si `cookies présents + 1 + (candidats présents + 1) ≤ 16`, et `bootstrap`
  (liaison absente) que si `cookies présents + 1 + candidats présents ≤ 16`
  (chaque candidat présent est compté comme pouvant encore produire un
  actif). Sinon 409 `credential_limit`, sans appel moteur ; `verify`,
  `resume`, `resolve` et `logout` restent possibles
  (`test_kept_candidates_can_never_push_the_browser_beyond_16_cookies`).
  Conséquence : un navigateur fraîchement initialisé admet 4 connexions
  réussies sans déconnexion dans une fenêtre de 15 minutes (les candidats
  expirent ensuite) ; une déconnexion libère un cookie actif.

## 6. CSRF (`oryx-f2-csrf-v1`)

Cookie de liaison `__Host-oryx-csrf-binding` : 32 octets aléatoires,
HttpOnly. **Durée de liaison : 30 minutes** (valeur proposée par
l'architecture), constante distincte de celle du jeton (30 minutes
également). Chaque bootstrap repose le **même** secret pour 30 minutes :
la liaison survit toujours au dernier jeton émis, et donc à tous les jetons
encore valides des autres onglets ; elle disparaît au plus 30 minutes après
le dernier bootstrap (`test_csrf_binding_lifetime_covers_the_token_it_signs`).

Jeton `version.exp.nonce.mac` : version de clé CSRF
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

- **Toute mutation POST : `Origin` exigé** (correctif P2), présent une
  seule fois et égal à une origine autorisée ; `null`, autre schéma, autre
  hôte, barre finale, casse différente : refus ; `Sec-Fetch-Site`, s'il est
  présent, vaut `same-origin` ou `same-site`. Un POST sans `Origin` est
  refusé même avec `Sec-Fetch-Site: same-origin` et un `Referer` de même
  origine (`test_every_mutation_requires_origin`). Les navigateurs actuels
  envoient `Origin` sur tout POST, y compris de même origine.
- **`GET /csrf/bootstrap`** : même règle si `Origin` est présent. `Origin`
  absent (cas normal d'un fetch GET de même origine) : accepté seulement si
  `Sec-Fetch-Site: same-origin`, `https://<Host>` autorisé, et `Referer`
  (s'il est présent) de cette même origine. Sinon (absent, `cross-site`,
  `none`, Referer étranger ou illisible, en-têtes dupliqués) : refus.

Évaluation de `Sec-Fetch-Site` (non supposée) : c'est un en-tête
« forbidden » qu'aucun script de page ne peut poser ; un navigateur qui ne
l'envoie pas est refusé (refus fermé). Un client hors navigateur peut le
forger, mais il n'en tire qu'un jeton lié à SA propre liaison, inutilisable
par une page intersite qui ne peut ni lire la réponse (aucun CORS) ni
poser le cookie `__Host-` de liaison. L'exception ne vaut que pour le
bootstrap, qui ne modifie aucun état.

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
| start | issued | — | **202** | `{"status": "code_sent"}` + sélecteurs + cookie candidat |
| start | issue_uncertain | generate_uncertain, — | **503** | `{"error": "delivery_uncertain"}` + sélecteurs + cookie candidat |
| start | send_failed | send_failed, superseded, — | **503** | `{"error": "delivery_failed"}` + sélecteurs + cookie candidat |
| start | rejected | invalid_email | 422 | `invalid_email` |
| resend | issued / issue_uncertain / send_failed | (idem) | 202 / 503 / 503 | idem, **sans** cookie |
| resend | rejected | operation_unavailable, credential_invalid | 409 | `operation_unavailable` |
| start, resend, verify | rejected | ip_unavailable | 503 | `temporarily_unavailable` |
| start, resend | refused | email_blocked, issue_interval, quota_email_issue, quota_ip_issue | 429 | `rate_limited` |
| verify | authenticated | — | 200 | `authenticated` + cookie actif (candidat conservé) |
| verify | resumed (rejeu technique) | — | 200 | `resumed` + cookie actif (candidat conservé) |
| verify | refused | quota_ip_verification, email_blocked | 429 | `rate_limited` |
| verify | rejected | email_blocked | 429 | `rate_limited` |
| verify | rejected | operation_unknown, operation_closed, operation_expired, credential_invalid | 409 | `operation_unavailable` |
| verify | rejected | no_active_code | 409 | `code_unavailable` |
| verify | rejected | code_expired | 409 | `code_expired` |
| verify | rejected | request_exhausted | 409 | `code_exhausted` |
| verify | rejected | code_not_ready | 409 | `code_not_ready` |
| verify | rejected | verification_in_progress | 409 | `verification_in_progress` |
| verify | uncertain / rejected | provider_uncertain / verification_uncertain | **503** | `verification_uncertain` |
| verify | rejected | code_malformed, code_incorrect | 401 | `code_incorrect` |
| verify | rejected | code_replaced | 401 | `code_replaced` |
| verify | rejected | key_unavailable | 503 | `temporarily_unavailable` |
| verify | rejected | provider_rejected, proof_not_applicable, identity_not_linked, verification_superseded, resume_unavailable | 401 | `sign_in_failed` |
| resume | resumed | — | 200 | `resumed` + cookie actif (candidat conservé) |
| resume | rejected | resume_unavailable | 409 | `resume_unavailable` |
| resume | rejected | key_unavailable | 503 | `temporarily_unavailable` |
| tous | tout autre triplet | | 503 | `temporarily_unavailable` |

Aucune réponse incertaine ou en échec n'annonce « envoyé » : `code_sent`
n'apparaît que pour `issued` (vérifié par entrée de matrice).

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

## 10. Confidentialité et anti-énumération

### 10.1 Analyse des séquences réelles (moteur PR 2 réel)

| Séquence | Moment de la décision | Réponse publique | Dépend de l'identité ? |
|---|---|---|---|
| démarrage (`start`) | avant toute lecture d'identité | 202 `code_sent` | non (identique, test) |
| code incorrect | HMAC local, **avant** verifyOtp et toute lecture d'identité | 401 `code_incorrect` | non (identique, état et renvoi identiques) |
| code mal formé | contrôle local, avant verifyOtp | 401 `code_incorrect` | non (idem) |
| réponse fournisseur incertaine | après verifyOtp, avant toute lecture d'identité | 503 `verification_uncertain` | non (identique) |
| rejet fournisseur certain | après verifyOtp, avant toute lecture d'identité | 401 `sign_in_failed` | non (identique) |
| preuve non applicable (autre e-mail) | revalidation, avant toute lecture d'identité | 401 `sign_in_failed` | non (identique) |
| code correct, identité inconnue | après la preuve | 401 `sign_in_failed` | réponse immédiate identique à un rejet fournisseur |
| code correct, identité liée | après la preuve | 200 `authenticated` | oui, par nature (succès) |

Tests : `test_wrong_or_malformed_code_does_not_depend_on_identity`,
`test_uncertain_provider_answer_does_not_depend_on_identity`,
`test_certain_provider_refusals_do_not_depend_on_identity`,
`test_correct_code_without_identity_matches_a_provider_refusal_then_diverges`
comparent, pour une adresse liée et une adresse inconnue : statut, corps
(UUID masqués), taille, en-têtes (hors `date`), cookies (noms sans
identifiant, attributs), état de l'opération et de la demande, compteur
d'échecs, et réponse d'un renvoi ultérieur.

### 10.2 Conclusion

- `code_incorrect` (et `code_malformed`) est jugé **avant** tout appel
  fournisseur et toute lecture d'`auth_identities` : sa réponse, l'état
  qu'il laisse et le renvoi qui suit sont identiques que l'identité existe
  ou non (prouvé). Le fusionner avec `sign_in_failed` n'améliorerait pas
  la confidentialité : il ne distingue que « code juste / code faux »,
  information que seul le détenteur du code possède. Il est **conservé**
  distinct (l'utilisateur doit savoir qu'il peut ressaisir le code ;
  `sign_in_failed` invite à recommencer). Les compteurs d'échecs, quotas,
  blocages e-mail, l'absence de profil implicite et de session sans
  identité liée sont inchangés (tests).
- Quiconque ne détient pas un code OTP valide (c'est-à-dire n'a pas accès
  à la boîte aux lettres) ne peut atteindre aucune branche dépendant de
  l'identité : aucune réponse ne lui révèle l'existence d'un compte Oryx.
- **Limite non résolue (moteur PR 2, §13 D1)** : pour le détenteur d'un
  code correct, la réponse immédiate est identique, mais l'opération sans
  identité est close (`failed`) alors qu'après un rejet fournisseur elle
  reste ouverte ; le renvoi qui suit diffère (409 `operation_unavailable`
  contre 202). Ce n'est pas masquable dans la couche HTTP sans annoncer un
  envoi qui n'a pas lieu. Le moteur n'est pas modifié ; arbitrage au §13.
- Aucune indistinguabilité temporelle n'est revendiquée (le chemin
  « identité liée » écrit une session ; aucune mesure n'a été faite).

## 11. Incertitudes et reprise

- `issue_uncertain` / `send_failed` : **503** `delivery_uncertain` /
  `delivery_failed`, avec les sélecteurs `operation_id` / `candidate_id`
  et (au démarrage) le cookie candidat ; jamais « envoyé » ; aucune
  relance automatique ; seul un `resend` explicite (soumis aux quotas, dont
  60 s) émet de nouveau.
- Vérification incertaine (réponse fournisseur ou état `verify_uncertain`
  persisté) : **503** `verification_uncertain`, jamais de session.
- Reprise après perte de réponse : `resume` rend **la même** session, la
  même génération et le même secret (cookie identique, octet pour octet),
  sans aucun appel Supabase ni Resend, de façon répétable, dans la fenêtre
  de 5 min (frontières 5 min − 1 µs et 5 min, horloge contrôlée). Le
  cookie candidat étant conservé après un succès, la reprise reste
  possible même si la réponse de succès est perdue ou si le cookie actif
  n'a pas été retenu (`test_partially_used_success_response_can_still_be_resumed`).
  Après la fenêtre, ou après déconnexion, plus rien n'est accordé.
- Une réponse tardive ou une reprise ne **sélectionne** aucun compte :
  aucun état « compte courant » n'existe côté serveur ; la session n'est
  utilisée que sur désignation explicite de sa génération. L'adoption
  atomique d'un compte entre onglets relève du lot frontend (modèle A).

## 12. Corrections post-audit implémentées

| Constat | Avant (`0073663`) | Après | Tests |
|---|---|---|---|
| P1.1 cookie candidat supprimé au succès | effacé par `authenticated` / `resumed` | conservé, jamais renouvelé ni effacé | `test_successful_verification_keeps_the_candidate_cookie_unchanged`, `test_partially_used_success_response_can_still_be_resumed`, `test_successful_resume_keeps_the_candidate_and_repeats_exactly`, `test_expired_resume_window_grants_nothing_more`, `test_candidate_cookie_is_set_once_and_never_extended`, entrées SESSION de la matrice |
| P1.2 confidentialité | `sign_in_failed` unifié ; aucun test comparatif liée / inconnue | analyse §10, tests comparatifs ; mapping inchangé (justifié §10.2) | tests §10.1 |
| P2 démarrage réussi | 200 | 202 | matrice, parcours |
| P2 émission incertaine | 202 | 503 + sélecteurs (+ cookie candidat au démarrage) | matrice, parcours |
| P2 échec d'envoi | 202 | 503 + sélecteurs (+ cookie candidat au démarrage) | matrice, parcours |
| P2 vérification incertaine | 409 | 503 (`provider_uncertain` et état `verification_uncertain`) | matrice, parcours |
| P2 POST sans Origin | admis si `Sec-Fetch-Site: same-origin` | refusé (403) | `test_every_mutation_requires_origin`, provenance |
| P2 liaison CSRF | 24 h | 30 min, distincte du jeton, reposée à chaque bootstrap | `test_csrf_binding_lifetime_covers_the_token_it_signs`, bootstrap |
| Effet induit de P1.1 : borne de 16 cookies | un succès remplaçait le candidat (solde nul) | admission prudente de `start` / `bootstrap` (§5) | `test_kept_candidates_can_never_push_the_browser_beyond_16_cookies`, `test_start_counts_the_active_cookie_each_kept_candidate_may_still_produce` |

## 13. Divergences et choix qui demandent une décision

**D1 — Comportement après `identity_not_linked` (non résolu, arbitrage
architectural).** Constat : §10.2. Options :

- **A. Statu quo (recommandé)** : la protection est complète contre qui ne
  détient pas de code ; le détenteur de la boîte aux lettres peut déduire
  l'absence de compte du renvoi refusé. Dans un parcours de connexion sans
  inscription, il le peut de toute façon par essais répétés (un compte
  existant finit par se connecter, un compte absent jamais) : l'option B
  ne retire qu'un signal parmi d'autres. Aucun changement de code ;
  tests figeant le comportement en place.
- **B. Modifier le moteur PR 2** pour que `identity_not_linked` laisse
  l'opération ouverte comme `provider_rejected` (renvoi admis, échecs non
  comptés). Avantage : réponses immédiate et ultérieure alignées.
  Risques : touche un moteur audité (transactions, états `failed`,
  `failure_code`, rejeu), envois d'OTP supplémentaires vers des adresses
  sans compte, nouveaux tests PR 2. Ne supprime pas l'inférence par
  essais répétés.
- **C. Changement de parcours** (par ex. ne jamais émettre d'OTP vers une
  adresse sans identité) : inacceptable en l'état, car cela crée au
  démarrage un canal d'énumération accessible à tous.

**D2 — Liaison CSRF de 30 minutes reposée à chaque bootstrap.** La norme
« propose » 30 minutes ; renouveler le même secret à chaque bootstrap est
nécessaire pour qu'une liaison ne meure pas avant un jeton qu'elle a
signé. À confirmer (sinon : liaison fixe de 30 min, mais alors un jeton
émis tard dans la vie de la liaison expirerait avant ses 30 minutes).

**D3 — `verification_uncertain` persisté → 503.** La norme fixe 503 pour
une vérification fournisseur incertaine ; l'état persisté `verify_uncertain`
(nouvelle tentative sur une demande dont la vérification est restée
incertaine, ou tentative en vol requalifiée) est traité comme le même fait.
À confirmer.

**D4 — `Origin` absent sur le bootstrap.** Conservé selon le cahier des
charges PR 3 (« Origin absent : accepter uniquement une provenance même
origine suffisamment vérifiée ») avec `Sec-Fetch-Site: same-origin` comme
critère (§7). À confirmer.

**D5 — Choix non fixés par un texte disponible** : regroupements de codes
du §9 ; `resolve` sans `user_id` ; `credential_limit` et sa règle
prudente (§5, conséquence directe de la conservation du candidat) ;
`logout` d'un
cookie invalide → 401 et effacement de ce seul cookie ; `resolve` et
`logout` confirment la livraison (`authenticate_session`, sémantique
PR 2).

**D6 — Énumération possible au démarrage via le fournisseur (à vérifier
avant activation).** Si `generateLink` (type `magiclink`) répondait
différemment pour une adresse inconnue de Supabase, `start` rendrait
`delivery_uncertain` au lieu de `code_sent` et révélerait l'existence d'un
utilisateur **Supabase** (pas d'une identité Oryx). Non vérifiable sans le
contrat réel d'`oryx-auth-test` (blocage 8 de `docs/f2_r1_pr2.md` §9).

**Outil de test.** Le `TestClient` de Starlette 0.36.3 n'est pas
compatible avec httpx 0.28.1 (verrou de la CI) : les tests pilotent
l'application par `httpx.ASGITransport`, sans changer les dépendances.

## 14. Dépendances et limites du moteur PR 2

La sécurité de PR 3 repose sur : la validation des justificatifs par
`F2AuthService` (candidat, session, génération, HMAC à clé versionnée) ;
les quotas et compteurs d'échec ; la preuve Supabase (option 1) ; la
reprise exacte sans écriture ; `authenticate_session` / `revoke_session` ;
la normalisation D1 / D2 ; le refus fermé du moteur (exceptions → 503).

Limites du moteur PR 2 constatées par PR 3 (moteur non modifié) :

- clôture de l'opération après `identity_not_linked` (§13 D1) ;
- `request_exhausted` : le 5ᵉ échec d'une demande (TTL ≤ 10 min) implique
  5 échecs e-mail en moins de 15 min, donc un blocage, et le moteur rend
  alors `email_blocked` (429) ; la branche `code_exhausted` reste mappée
  mais n'est pas atteinte dans les parcours observés ;
- `resume_result` ne compte aucun quota IP (secrets de 256 bits) ; une
  limite de débit HTTP reste à définir avant activation (blocage 9 de
  `docs/f2_r1_pr2.md` §9).

## 15. Tests et preuves

| Fichier | Contenu | PostgreSQL |
|---|---|---|
| `tests/test_f2_http_contract.py` | matrice = table de référence ; inventaire syntaxique des Outcome ; chaque entrée par la route réelle (statut, corps, cookies, candidat conservé, jamais « envoyé » si incertain) ; aucune branche non-session ne pose de cookie actif ; incohérences et triplets inconnus → 503 ; refus comparables | non |
| `tests/test_f2_http_transport.py` | CSRF (forme, frontières d'expiration, liaison 30 min reposée, origine, altérations, encodages non canoniques, version inconnue, rotation, trames) ; provenance légitime et hostile, Origin exigé sur chaque POST ; cookies (taille, nombre, doublons, noms, valeurs) ; corps (types, tailles, chunked, doublons, NaN, BOM) ; sélecteurs sans repli ; IP (pair, proxys, en-têtes forgés) ; durées des cookies (horloges injectées) ; journaux sans secret | non |
| `tests/test_f2_http_flows.py` | parcours réels : connexion, candidat conservé après succès, reprise après réponse perdue ou partiellement exploitée (même session, génération, secret, sans appel fournisseur, répétable), fenêtre de reprise expirée, cookie candidat posé une seule fois ; codes incorrects / mal formés comptés, quotas, incertitudes (503), renvoi explicite ; frontières (reprise, opération, session) ; absence de profil implicite ; comparaisons liée / inconnue (§10) ; deux comptes dans un navigateur, multi-appareils, révocation ciblée, cookies forgés ; concurrence (interleaving déterministe et 4 requêtes parallèles) ; composition sans effet ; état anti-abus non initialisé ; adaptateurs HTTP réels sur `httpx.MockTransport` | oui |
| `tests/test_f2_http_dormancy.py` | dormance du vrai `api.py` sous trois environnements, garde réseau, sources, dépendances | non |
| `tests/test_f2_dormancy.py` | garanties PR 2 conservées (adaptation du seul balayage) | non |

Non-régression : les suites PR 2 (`test_f2_service`, `test_f2_crypto`,
`test_f2_normalize_providers`, `test_f2_audit_corrections`,
`test_f2_concurrency`) passent sans modification ; le moteur n'est pas
touché. Aucun test n'utilise de secret de production.

## 16. Garanties reportées à l'activation publique (hors PR 3)

1. Activation publique : aucune route F2 dans `api.py` ; chargeur de clés
   et de configuration de production ; initialisation explicite de
   `auth_abuse_key_state`.
2. Chaîne de proxys Railway approuvée (§8).
3. Blocages listés dans `docs/f2_r1_pr2.md` §9 : routes historiques à
   `user_id` client, `level_revision`, CORS `*` d'`api.py`, SameSite avec
   les domaines réels, contrat fournisseur réel (dont §13 D6), limites de
   débit HTTP pour la reprise et les invitations, purge.
4. Parcours invitation et liaison e-mail en HTTP.
5. Lot frontend : adoption atomique d'un compte entre onglets,
   déconnexion avant une nouvelle connexion (sinon accumulation de cookies
   actifs jusqu'à la limite), contrat d'identité.
6. Décision D1 (§13) sur le comportement après `identity_not_linked`.
7. Réconciliation littérale avec C.2.1 / C.2.1-A / C.3.3 / C.3.3-A.
