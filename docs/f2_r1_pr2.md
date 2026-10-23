# R1 PR 2 — services F2 internes (dormants)

Référence normative : architecture C.2.1 (figée le 22/10/2026) et annexe
C.2.1-A (décisions D1 à D9, variante A de 0018, option 1 de preuve
Supabase, code mal formé). Ce document décrit ce qui est livré, les
garanties démontrées par les tests et ce qui reste à faire **avant**
toute activation.

## 1. Dormance

- Paquet `core/auth_f2/` : aucun module applicatif ne l'importe (api.py,
  workers, scripts, alembic/env.py, frontends) ; aucune route, aucun
  middleware, aucun cookie, aucun démarrage ne change
  (`tests/test_f2_dormancy.py` fige les 23 routes de main).
- Aucun chargeur de clés ni de configuration : le trousseau et les
  fournisseurs sont injectés par l'appelant. Aucune variable
  d'environnement n'est lue. `requirements.txt` est inchangé (httpx reste
  une dépendance de test, importée seulement à la construction d'un
  adaptateur HTTP).
- Tests : doubles Supabase / Resend et `httpx.MockTransport` uniquement,
  clés fictives, PostgreSQL local jetable.

## 2. Modules

| Module | Rôle |
|---|---|
| `crypto.py` | FRAME, formules F2-C2-V1 (candidat, dérivation HKDF, session, OTP, invitation, anti-abus), formats stricts, `Redacted` |
| `keys.py` | Trousseau versionné à six usages ; résolution exacte, aucune substitution |
| `normalize.py` | D1 (e-mail ASCII canonique) et D2 (IPv4, IPv6 /64, refus fermé) |
| `policy.py` | Paramètres figés (TTL, quotas, fenêtres, versions) |
| `providers.py` | Interfaces fournisseurs et validation stricte des réponses (option 1) |
| `http_providers.py` | Adaptateurs Supabase / Resend dormants (TLS, sans redirection, sans proxy d'environnement) |
| `txn.py` | Transactions F2 (`lock_timeout`, `statement_timeout`), horloge PostgreSQL |
| `abuse.py` | Sujets, verrous, quotas, blocage, marqueurs, rotation de K_abuse, purge |
| `service.py` | `F2AuthService` : connexion OTP, liaison e-mail, invitations, reprise, sessions |

## 3. Protocole transactionnel (C.2.1 §9)

1. Pré-contrôle des quotas **sans verrou** : un refus n'écrit qu'un marqueur
   borné (§4) et n'appelle aucun fournisseur.
2. Création des sujets anti-abus manquants dans une transaction courte,
   committée (`INSERT … ON CONFLICT DO NOTHING`, ordre trié).
3. Transaction principale : verrous, horloge PostgreSQL lue **après** les
   verrous, quotas revérifiés, réservation durable (demande OTP `issuing`,
   événements, ou tentative `in_flight`), COMMIT.
4. Appel fournisseur **hors transaction** (les doubles vérifient qu'aucune
   connexion n'est empruntée et qu'aucune session PostgreSQL n'est en
   transaction pendant l'appel).
5. Nouvelle transaction : mêmes verrous, revalidation complète (états,
   génération, échéances, candidat ou session, blocage e-mail), puis
   finalisation. Une reprise purement technique de cette étape (erreur de
   connexion) ne crée ni nouvelle admission ni nouvel appel.

### Ordre de verrouillage global

verrou consultatif partagé de l'état de clé anti-abus (puis lecture de
`auth_abuse_key_state`) → sujets anti-abus (une instruction, triée par
`scope_kind, hash_key_version, scope_hmac`) → invitation → profil `users`
→ opération → candidat → demande OTP → tentative → session → génération
→ identité. **L'heure de décision (`clock_timestamp()`) est lue après
tous les verrous et toute attente possible** (correctif d'audit P1) :
aucun droit n'est jugé valide à un instant antérieur à une attente de
verrou. `now()` / `transaction_timestamp()` ne sont jamais utilisés pour
décider ; seuls les pré-contrôles sans verrou, qui ne peuvent que
refuser, lisent l'heure avant.

- Aucune transaction ne verrouille un rang inférieur après un rang
  supérieur. Les contrôles de FK (FOR KEY SHARE) suivent le même ordre :
  le profil visé par une session ou une opération est pris explicitement
  avant l'opération (connexion : FOR KEY SHARE ; liaison : FOR UPDATE) ;
  les tentatives non résolues sont verrouillées avec leur demande, avant
  tout contrôle de session.
- Les insertions de sujets ont lieu dans des transactions séparées, qui ne
  tiennent que leurs propres insertions : elles ne peuvent former de cycle
  ni entre elles (ordre trié) ni avec les transactions principales.
- Les identités (`auth_identities`) sont lues sans verrou (colonnes
  `provider, issuer, subject, user_id` immuables), sauf pour la mise à
  jour de `last_authenticated_at` (FOR NO KEY UPDATE, rang identité).
  L'insertion d'une identité de liaison (`ON CONFLICT DO NOTHING`, qui
  peut attendre une insertion concurrente) a lieu AVANT la lecture de
  l'heure, dans un point de sauvegarde annulé si la revalidation échoue.
- Un marqueur de refus prend d'abord le sujet en `FOR KEY SHARE`, puis
  insère sa ligne (ordre sujet → marqueur partout). Sans cela, un refus
  sans verrou réservait la clé unique du marqueur puis attendait le sujet
  détenu en `FOR UPDATE` par une transaction qui attendait cette même clé.
  Cet interblocage a été observé par `tests/test_f2_concurrency.py`
  (rotation sous émissions concurrentes), puis corrigé et rejoué en boucle.

## 4. OTP, échecs et quotas

Fenêtres glissantes `]t − W, t]` sur l'horloge PostgreSQL ; `now ≥ expires_at`
est expiré ; à exactement 60 s, une émission est admise.

| Règle | Mise en œuvre |
|---|---|
| 8 chiffres ASCII, TTL ≤ 600 s | `generateLink` non conforme → `send_failed`, rien envoyé ; `expires_at = min(t + 600 s, fin d'opération)` |
| 5 échecs par demande | `failure_count` sous verrou de la demande ; au 5e : `exhausted`, définitif |
| 5 échecs e-mail / 15 min → blocage 15 min | événement `otp_email_block` (blocked_until = instant du 5e échec + 15 min), sous verrou des sujets e-mail ; les échecs déjà consommés par un blocage ne sont pas recomptés ; aucun prolongement ni effacement |
| 60 s entre émissions, 5/h/e-mail, 20/h/IP | événements `otp_request` comptés à la réservation (avant l'appel) |
| 30 vérifications admises / 5 min / IP | événement `otp_verification_attempt` à l'admission, quelle que soit l'issue |

Comptabilisation (D8 et décision 3) :

- mauvais code ou code mal formé sur demande et candidat (ou session)
  valides : échec de demande + échec e-mail ; un code mal formé ne crée
  aucune ligne de tentative (aucun HMAC hors contrat) ;
- demande inexistante, opération inconnue, candidat ou session
  inadmissibles : admission IP seulement ;
- code d'une génération remplacée ou expirée, demande expirée : admission
  IP seulement ;
- correspondance locale : appel `verifyOtp` ; rejet certain du fournisseur
  ou réponse incertaine : jamais un échec de saisie, jamais une session ;
- quota IP épuisé : aucun appel fournisseur, aucune admission.

### Borne des marqueurs de refus (démontrée)

Un refus insère `otp_request_refused` ou `otp_verification_refused` avec
`occurred_at = date_bin('5 minutes', t)`. Deux garanties SQL : CHECK
d'alignement (aucun marqueur hors frontière) et index unique partiel
`(subject_id, event_kind, occurred_at)`. Donc au plus **un marqueur par
sujet, type et tranche de 5 minutes**, quel que soit le nombre de
requêtes refusées, la concurrence (`ON CONFLICT DO NOTHING`, prouvé par
40 processus) ou la position dans les fenêtres glissantes (la tranche
dépend seulement de t). Un refus ne crée jamais de sujet. Un sujet n'est
refusé que s'il a déjà épuisé un quota : les marqueurs restent
proportionnels au trafic admis.

### Croissance des sujets et saturation

- Un sujet n'est créé qu'après un pré-contrôle réussi, juste avant une
  admission ; au plus deux sujets par portée pendant une rotation.
- Les événements sont limités aux admissions (bornées par les quotas de
  chaque sujet) et aux marqueurs (bornés ci-dessus). IPv6 est regroupée
  par /64.
- `purge_abuse_history` (dormante, jamais planifiée) retire les événements
  de plus de 2 h (minimum imposé) et les sujets orphelins plus anciens,
  verrouillés `SKIP LOCKED` ; un sujet purgé entre création et verrou fait
  rejouer l'opération (trois fois au plus, puis refus).
- Saturation : verrou non obtenu en 5 s, instruction de plus de 10 s,
  erreur d'écriture, état ou clé anti-abus absent : exception, **aucune
  admission, aucun appel fournisseur** (`test_database_saturation_…`).

## 5. Rotation de K_abuse

`auth_abuse_key_state` (migration 0018) fixe `current_version` (version
d'enregistrement) et `previous_version` (encore comptée). Chaque opération
compte les sujets de ces deux versions et enregistre sous la version
courante ; une instance dont le trousseau ne contient pas les deux refuse
tout.

Procédure :

1. Déployer partout un trousseau contenant la nouvelle version (l'ancienne
   reste courante en base).
2. `rotate_abuse_key(new)` : précédente ← courante, courante ← nouvelle
   (refusé si une version précédente est encore comptée).
3. Au plus tôt 90 minutes après : `retire_previous_abuse_key()` (refusé
   avant).
4. Retirer l'ancienne clé des trousseaux.

### Coordination transactionnelle (correctif d'audit P1)

Défaut corrigé : lire l'état puis verrouiller les sujets ne suffisait pas.
T1 lisait {v1} ; une rotation committait {v2, v1} ; T2 enregistrait sous
v2 et committait ; T1 verrouillait seulement les sujets v1 et ne voyait
pas l'événement de T2 (dépassement de quota, reproduit par le témoin
`test_control_the_old_unlocked_read_let_the_same_interleaving_exceed_the_quota`).

Correction : toute transaction de décision prend
`pg_advisory_xact_lock_shared(KEY_STATE_LOCK_KEY)` AVANT de lire l'état et
le garde jusqu'à son COMMIT ; `rotate_abuse_key` et
`retire_previous_abuse_key` prennent `pg_advisory_xact_lock(KEY_STATE_LOCK_KEY)`
(exclusif) avant de modifier la ligne. Donc :

- une rotation ne peut pas committer pendant une décision : chaque
  décision compte et enregistre sous un ensemble de versions stable ;
- une décision qui obtient le verrou après une rotation lit le nouvel
  état (instantané pris après le COMMIT de la rotation), qui contient
  l'ancienne version courante : rien de ce qui a été enregistré avant
  n'est oublié ;
- après la rétrogradation, plus aucune décision n'enregistre sous
  l'ancienne version ; son retrait, 90 minutes plus tard, ne peut donc
  oublier aucun événement de fenêtre active (1 h) ni aucun blocage
  (15 min).

Ordre de verrouillage : ce verrou est le rang 0, avant les sujets ; une
rotation ne prend que lui puis la ligne d'état : aucun cycle. Tests :
`test_rotation_cannot_change_versions_during_a_decision_and_quota_holds`
(entrelacement exact, suspendu par point d'interruption),
`test_concurrent_rotations_one_wins_without_deadlock`, et les tests
multi-processus de `tests/test_f2_concurrency.py`.

### Le singleton est nécessaire et suffisant

- **Nécessaire** : sans état partagé en base, une instance ne peut pas
  savoir sous quelle version les autres enregistrent ; une instance au
  trousseau périmé sous-compterait sans le savoir. La ligne unique porte
  les deux versions comptées et la date de rétrogradation qui autorise le
  retrait (CHECK : singleton, formats, version précédente ≠ courante,
  précédente ⇔ date de rétrogradation). Aucune clé n'y figure.
- **Suffisant** : avec le verrou de transaction ci-dessus, il donne à
  chaque décision un ensemble de versions stable et linéarise les
  rotations. Aucune autre table, aucun index supplémentaire.
- **Pourquoi un verrou consultatif plutôt qu'un `FOR SHARE` de la
  ligne** : les deux sont sûrs, mais un `FOR SHARE` de ligne compatible
  n'attend pas derrière une rotation déjà en attente de verrou exclusif
  (constaté pendant les tests) ; sous trafic continu, la rotation pourrait
  ne jamais aboutir. Le gestionnaire de verrous de PostgreSQL met en file
  la demande partagée derrière la demande exclusive : la rotation passe
  dès la fin des décisions en cours, les décisions suivantes l'attendent
  (au plus `lock_timeout`, puis refus fermé). Clé bigint figée, dérivée par
  sha256 (convention de `core/taxonomy_service.py`).
- **Initialisation** : `initialize_abuse_key_state` (idempotente,
  dormante) ; la migration n'écrit aucune ligne. **Downgrade** : refusé si
  la ligne existe (aucune suppression silencieuse).

## 6. Preuve Supabase (option 1)

La réponse de `verifyOtp`, reçue directement par le backend depuis le
projet configuré, n'est une preuve que si : HTTP 200, objet `user`,
`user.id` UUID canonique, `aud = authenticated`, e-mail normalisé égal à
celui de l'opération, `email_confirmed_at` présent, `access_token` présent.
Le jeton n'est jamais décodé, journalisé ni persisté. L'issuer enregistré
est celui de la configuration (`https://<projet>/auth/v1`). Identité :
`(supabase, issuer, user.id)`.

Classification des erreurs (correctif d'audit P2) : seul un couple
(statut, code) d'une **liste positive** de refus définitifs documentés est
un refus certain — `verifyOtp` : `(403, otp_expired)` ; `generateLink` :
`email_address_invalid` / `validation_failed` en 400 ou 422 ; Resend :
erreurs de validation, de clé, d'expéditeur et de limitation documentées.
Tout le reste (401, 403 sans code ou autre code, 429 de Supabase, code
inconnu, corps absent, non JSON ou mal formé, 3xx, 5xx, 200 non conforme,
délai, coupure) est **incertain** : jamais une session, jamais un échec de
saisie (`verify_uncertain`, nouvelle génération explicite requise).

Incertitude de `generateLink` (correctif d'audit P2) : une issue inconnue
donne `issue_uncertain` (jamais `send_failed`), sans code lié, sans envoi,
sans renvoi automatique et sans restitution des quotas réservés ; seule
une nouvelle génération explicite, soumise aux quotas, la remplace
(`superseded`). Un refus certain de la liste positive, ou une sortie non
conforme refusée par Oryx, donne `send_failed` (rien n'a été envoyé).
L'égalité d'e-mail ne fusionne jamais rien ; une identité inconnue ne crée
jamais de profil.

## 7. Cryptographie et versions

Formules et vecteurs officiels V1 à V4 : `tests/test_f2_crypto.py`
(implémentation livrée, référence RFC 2104 indépendante, OpenSSL).
Versions persistées : `auth_preauth_candidates.hash_key_version`
(K_candidate), `oryx_session_generations.derivation_version` =
`f2-c2-hkdf-sha256-v1:<version K_derive>` et `hash_key_version`
(K_active), `auth_otp_requests.expected_code_hmac_key_version` (K_otp),
`invitations.hash_key_version` (K_invitation),
`auth_abuse_subjects.hash_key_version` (K_abuse). Une version absente du
trousseau entraîne un refus, jamais l'emploi de la clé courante.

## 8. Sessions et reprise

- Session : 15 jours absolus ; une génération par session (N = numéro du
  candidat lié) ; première présentation valide = preuve de livraison.
- Candidat : 10 minutes au plus ; `open` seulement pour l'authentification
  initiale, `bound` seulement pour la reprise.
- Reprise : 5 minutes après le commit ; vérification du candidat, de la
  session et de la génération (actives, non expirées), recalcul du secret
  actif et de son empreinte ; rend exactement la même session, la même
  génération et le même secret ; aucune écriture.
- `result_payload` : uniquement `{"outcome": …}` ; `result_version` =
  `f2-c2-result-v1`.

## 9. Blocages AVANT toute activation de F2 (hors PR 2)

1. Routes historiques à `user_id` client : fermer ou adapter
   `GET /api/user/{user_id}`, `PUT …/level`, `GET`/`POST …/portfolio`,
   `PUT`/`DELETE …/portfolio/{position_id}`, `GET …/theses`,
   `DELETE …/theses/{ticker}` ; contrôles d'appartenance sur toute
   ressource ; aucune création implicite d'utilisateur.
2. `level_revision` : incrément atomique à chaque écriture du niveau.
3. CORS : `allow_origins=["*"]` (api.py) à remplacer par les origines
   réelles.
4. CSRF et contrôle d'origine sur toutes les mutations.
5. Proxys : chaîne de proxys approuvée pour l'IP client (D2) ; un
   `X-Forwarded-For` libre n'est jamais une preuve.
6. Cookies : `__Host-oryx-candidate-<uuid candidat>` et
   `__Host-oryx-active-<uuid génération>`, `Secure`, `HttpOnly`, `Path=/`,
   sans `Domain` ; **SameSite à décider** avec les domaines réels.
7. Chargeur de clés de production (six usages, 32 octets, versions jamais
   réattribuées) et gestion des secrets ; initialisation explicite de
   `auth_abuse_key_state`.
8. Contrat fournisseur réel : figer une réponse `generate_link` et
   `verify` d'`oryx-auth-test` (jetons retirés) en données de test ;
   projet de production configuré en OTP de 8 chiffres, 600 s ; domaine
   d'expéditeur Resend.
9. Limites de débit HTTP pour l'activation d'invitation et la reprise
   (les secrets de 256 bits rendent la force brute impraticable ; aucune
   valeur de quota n'est fixée par C.2.1 pour ces parcours).
10. Planification éventuelle de la purge anti-abus (rétention ≥ 2 h).
11. Préflight en lecture seule avant le merge de 0018 (alembic/README).
