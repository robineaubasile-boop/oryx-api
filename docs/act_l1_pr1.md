# ACT-L1-PR1 — Identité e-mail liée et vérifiée exigée par le runner F2 (dormant)

## 1. Référentiel et commit de référence

Première PR du chantier **Activation F2 Lot 1 V1.2** (architecture figée par le fondateur). Aucune décision
architecturale nouvelle n'a été prise ; les points d'implémentation qui ont exigé un choix sont signalés au
§ 13 pour l'audit.

`main` relu avant toute modification : `564a19a36deb1f530b657fef88a8e45b5945dc74` (merge de la PR #235,
R1 PR 9-C3), **identique** à la référence vérifiée avant la demande : aucune différence à intégrer.

| Élément | Avant | Après |
|---|---|---|
| Statuts fermés du runner (`runner.STATUSES`) | 6 | **7** (`identity_link_required`) |
| Commandes F2 (`_adapters.REGISTRY`) | 20 | 20 (inchangé) |
| Plans de verrouillage | 4 | 4 (inchangés ; un rang d'identité commun ajouté) |
| Routes HTTP F2 sur `api:app` | 0 | 0 |
| Migrations | 21 | 21 (aucune) |

## 2. Périmètre exact

**Adapté (production, paquets isolés seulement)** :

- `core/authz_f2/runner.py` : statut `IDENTITY_LINK_REQUIRED`, rang d'identité de la phase B, décision ;
- `core/authz_f2/_adapters.py` : instruction fermée `_LOCK_LINKED_IDENTITIES`, `lock_linked_identities`,
  `LINKED_IDENTITY_PROVIDERS` ;
- `core/authz_f2_http/routes.py` (`/me`, `/me/level`, constantes `IDENTITY_LINK_HTTP_STATUS` /
  `IDENTITY_LINK_CODE`), `portfolio.py`, `consultation.py`, `decryptage.py` : réponse 403 ;
- `core/authz_f2/decryptage_generation.py` (B2) : motif fermé `IDENTITY_REQUIRED`.

**Créé** : `tests/test_act_l1_pr1_identity.py` (contre-épreuves), cette documentation.

**Tests historiques adaptés nominativement** (§ 11) : `tests/test_authz_f2_boundary.py`,
`tests/test_authz_f2_runner.py`, `tests/test_authz_f2_dormancy.py`,
`tests/test_authz_f2_portfolio_concurrency.py` ; outils communs dans `tests/authz_f2_support.py`.

**Inchangé** : `core/auth_f2/*` (moteur F2 : invitation, liaison e-mail, connexion OTP, sessions),
`core/auth_f2_http/*` (routes d'authentification et de liaison), les vingt commandes, leurs adaptateurs
d'exécution, le pont ORM privé, B1, C1-B, C2, les moteurs pédagogiques, les données financières, les schémas
de preuve, la progression, les règles d'analyse, `core/models.py`, `alembic/`, `api.py`, `web/`, `web-v2/`,
Railway. Aucune dépendance nouvelle.

## 3. Contrat

```python
IDENTITY_LINK_REQUIRED = "identity_link_required"   # core.authz_f2.runner, 7e statut fermé
```

**Sens** : justificatif F2 valide à `T_authorization` (session et génération actives, non expirées, secret
conforme, livraison confirmée), profil propriétaire présent et cohérent avec la session, mais **aucune
identité fournisseur liée et vérifiée admissible** dans `auth_identities` pour ce `user_id`.

**Distinct** de `SESSION_INVALID`, `NOT_FOUND`, `UNAVAILABLE`, `REVISION_CONFLICT`, `IDEMPOTENCY_CONFLICT`.

**Priorité** (inchangée pour les refus existants) :

1. `SESSION_INVALID` — phase A (pré-validation) ou à `T_authorization` (révocation, expiration, génération,
   secret, livraison) ;
2. `UNAVAILABLE` — profil absent, ou session dont le titulaire diffère du candidat de la phase A ;
3. **`IDENTITY_LINK_REQUIRED`** ;
4. exécution par l'adaptateur (`NOT_FOUND`, `REVISION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `AUTHORIZED`).

**Effet** : `ROLLBACK` avant toute exécution — aucun verrou métier pris, aucune écriture, aucun point
`authorized` / `executed` atteint. `AuthorizationOutcome("identity_link_required")` : `value=None`,
`principal=None` (ni principal, ni donnée personnelle, ni résultat métier, ni information sur l'identité).

**Portée** : les **vingt** commandes du registre, quel que soit leur plan (`READ`, `PROFILE_MUTATION`,
`PORTFOLIO_MUTATION`, `DECRYPTAGE_MUTATION`) — le contrôle est dans `_run`, pas dans les adaptateurs : aucune
commande ne peut l'éviter. Le runner reste l'**unique** autoriseur ; aucune dépendance HTTP, aucun contrôle
frontend.

## 4. Preuve de liaison : règle d'admissibilité

`_adapters.lock_linked_identities(conn, user_id)` lit, pour le `user_id` serveur (candidat de la phase A,
confirmé par la décision) :

```sql
SELECT id, provider, email_verified_at, verified_email_normalized IS NOT NULL AS email_present,
       pii_redacted_at IS NOT NULL AS pii_redacted
FROM auth_identities WHERE user_id = :u ORDER BY id FOR KEY SHARE
```

Aucune donnée personnelle n'est projetée (ni adresse, ni issuer, ni subject : seulement des booléens).
Une ligne est **admissible** si et seulement si :

- `provider` ∈ `("supabase",)` (liste fermée, `ck_auth_identities_provider`) ;
- `email_verified_at IS NOT NULL` (vérification e-mail durable) ;
- et la preuve est encore portée par la ligne : adresse vérifiée présente **ou** expurgée pour
  confidentialité (`pii_redacted_at IS NOT NULL`).

Conséquences explicitement testées :

| Ligne | Résultat |
|---|---|
| aucune ligne (invitation activée sans liaison) | `IDENTITY_LINK_REQUIRED` |
| ligne sans vérification (`email_verified_at` NULL) | `IDENTITY_LINK_REQUIRED` |
| ligne expurgée jamais vérifiée | `IDENTITY_LINK_REQUIRED` |
| ligne vérifiée, adresse présente (liaison OTP F2, connexion OTP) | admise |
| ligne vérifiée **puis expurgée** (`verified_email_normalized` NULL, `pii_redacted_at` renseigné) | **admise** : l'expurgation n'invalide pas la vérification durable |
| ligne vérifiée sans adresse **ni** expurgation (état jamais écrit par F2) | `IDENTITY_LINK_REQUIRED` (refus fermé) |

**Jamais une preuve** : l'adresse déclarée dans l'invitation (`recipient_email_normalized`), un `user_id`
transmis par le client (il n'en existe aucun), une clé de stockage navigateur, le seul `auth_method` de la
session (une session de connexion OTP dont l'identité perd sa vérification est refusée ; une session
d'invitation devient admissible après liaison sans que son `auth_method` change), une ligne sans vérification.

## 5. Placement transactionnel et ordre des verrous

Phase B du runner (nouvelle étape **2 bis**, insérée sans déplacer aucun rang existant) :

```
[READ COMMITTED vérifié]  -> users (plan) -> oryx_sessions FOR SHARE -> oryx_session_generations FOR SHARE
  -> auth_identities FOR KEY SHARE (si profil présent)          <- ACT-L1-PR1
  -> verrous métier spec.lock (si profil présent ET identité admissible)
  -> hook "locked" -> T_authorization = clock_timestamp() -> décision -> exécution -> COMMIT
```

- **Ordre global F2** (`core/auth_f2/service.py`) : `… -> profil users -> … -> session -> génération ->
  identité`. Le rang identité vient après génération : aucun rang n'est remonté. Les tables métier restent
  après tous les rangs F2 (aucune transaction de `core/auth_f2` ne les touche, test existant).
- **Pourquoi `FOR KEY SHARE`** : il bloque jusqu'au COMMIT toute suppression ou tout changement de clé
  (`id`, `user_id`, `provider`, `issuer`, `subject`) d'une identité lue — aucun transfert d'identité ne peut
  survenir entre la lecture et l'exécution — tout en restant **compatible** avec la finalisation de la
  connexion OTP (`FOR NO KEY UPDATE` puis `UPDATE last_authenticated_at`) et avec une expurgation (colonnes non
  clés). Aucune attente nouvelle n'est introduite pour les transactions F2 existantes (testé).
- **Verrous métier non pris sur refus** : comme pour un profil absent (comportement existant), aucun verrou
  métier n'est pris si l'identité n'est pas admissible ; la décision reste rendue à `T_authorization`, après
  tous les verrous de la décision.
- Les revalidations à `T_authorization` (session, génération, révocation, expiration, secret, livraison,
  titulaire, profil) sont **inchangées** et précèdent la règle d'identité.

## 6. Concurrence avec la liaison e-mail

La finalisation de la liaison (`_finalize_verification`, inchangée) prend `users FOR UPDATE` du profil cible
**avant** d'insérer l'identité, et garde ce verrou jusqu'à son COMMIT. `FOR UPDATE` est en conflit avec chacun
des trois verrous de plan du runner (`FOR KEY SHARE`, `FOR NO KEY UPDATE`, `FOR UPDATE`). Donc :

| Ordre | Résultat (testé pour les quatre plans) |
|---|---|
| liaison finalisée en premier (verrou users détenu, identité insérée non committée) | le runner attend au rang users ; après le COMMIT de la liaison, l'instruction d'identité (postérieure à l'attente, nouvel instantané READ COMMITTED) voit l'identité : `AUTHORIZED` |
| runner en premier (tous les verrous de décision pris) | la liaison attend au rang users ; le runner rend `IDENTITY_LINK_REQUIRED` sans écrire ; la liaison committe ensuite ; la commande suivante est autorisée |

Aucun interblocage (compteur `pg_stat_database.deadlocks` inchangé dans chaque test). L'insertion d'une
identité n'existe que dans la liaison (`core/auth_f2/service.py`), toujours sous `users FOR UPDATE`.
Sous un niveau d'isolation plus strict qu'READ COMMITTED (instantané antérieur à l'attente), le contrôle ne
peut que **refuser** : jamais une autorisation sans identité (les plans de mutation exigent déjà READ
COMMITTED ; la composition HTTP refuse tout autre niveau configuré).

## 7. Réponses HTTP (routes personnelles isolées)

`IDENTITY_LINK_REQUIRED` → **`403 {"error":"identity_link_required"}`** (corps exact
`{"error":"identity_link_required"}`, en-têtes no-store / nosniff / no-referrer, aucun `Set-Cookie`, aucun
CORS), jamais transformé en 401, 404, 409 ni 503 :

| Route | Commande(s) |
|---|---|
| `GET /api/v2/me`, `PUT /api/v2/me/level` | `ReadProfile`, `UpdateProfileLevel` |
| `GET/POST /api/v2/me/portfolio`, `GET/PUT/DELETE /api/v2/me/portfolio/{id}` | commandes portefeuille |
| `GET /api/v2/me/analysis-overview`, `GET /api/v2/me/theses`, `GET /api/v2/me/theses/{id}` | consultation |
| `POST /api/v2/me/decryptage/turns` | B1 (`_B1_REFUSALS`), B2 (motif `IDENTITY_REQUIRED`), C1-B (`_C1B_REFUSALS`), C2 |
| `POST /api/v2/me/decryptage/replay`, `POST /api/v2/me/decryptage/ack` | C2 (`_C2_REFUSALS`) |

Les treize routes personnelles sont couvertes (inventaire exact comparé aux routes de l'application). Les refus
d'admission (provenance, cookie, CSRF, sélecteur, corps) restent antérieurs au runner et inchangés ; un cookie
absent ou une session invalide gardent leur `401 session_invalid`.

## 8. Décrypter (B2, C1-B, C2)

- **B1** : refus avant toute réservation ; aucun fournisseur.
- **B2** (`decryptage_generation._F2_REFUSALS`) : `IDENTITY_LINK_REQUIRED` → motif fermé
  `IDENTITY_REQUIRED = "identity_link_required"` (ajouté à `REFUSAL_REASONS`), jamais
  `AUTHORIZATION_UNAVAILABLE` ni `SESSION_REFUSED`. Le refus est levé par `_f2` **avant l'étape suivante** :
  réautorisation `ReadProfile` refusée → aucun appel (recherche, EODHD, FMP, Claude) ; lecture de contexte
  refusée → aucun appel ultérieur, aucune déclaration d'échec. Une déclaration d'échec refusée pour ce motif
  reste `DECLARATION_REFUSED` (bail jamais présumé libéré) et C3 répond, selon sa matrice figée
  `_PROVIDER_REPLIES`, `503 temporarily_unavailable` (jamais `provider_unavailable`, réservé à une déclaration
  confirmée) ; la même intention retrouve ensuite le bail actif par B1 (`202 in_progress`). Ce cas n'est
  atteignable que si l'identité devient non admissible entre la réautorisation B2 et la déclaration (aucune
  écriture F2 ne le produit) ; il est signalé au § 15.4 (réserve R-2).
- **C1-B** : refus sans livraison écrite ; la réservation reste `reserved` (B1 gouverne la reprise).
- **C2** : replay et ACK refusés sans effet ; une fois l'identité admissible, l'ACK s'applique (idempotence
  inchangée).
- Une livraison d'autrui reste `404 not_found` une fois le demandeur lié (protection inter-utilisateurs
  inchangée) ; avant liaison, le demandeur reçoit 403 sans que la ressource soit lue.

## 9. Contrats préservés

- **Réponses canoniques** : aucune sérialisation modifiée ; un refus n'est jamais un résultat métier.
- **COMMIT incertain** : inchangé (`UNAVAILABLE`, jamais de rollback prétendu, aucune réexécution).
- **Idempotence portefeuille** : un refus n'écrit aucun reçu ; un rejeu est soumis exactement aux mêmes
  contrôles qu'une première exécution (le reçu n'est examiné qu'après l'autorisation).
- **Inter-utilisateurs** : la liaison d'un profil ne bénéficie jamais à un autre ; une identité appartenant à
  un autre profil n'est ni transférée ni fusionnée (`conflict_other_profile`, inchangé), le profil demandeur
  reste refusé.
- **Parcours d'authentification** : invitation (profil et session créés comme avant), liaison e-mail depuis
  une session d'invitation non liée, connexion OTP ultérieure retrouvant le même profil (aucune création,
  aucune fusion) — testés par le moteur F2 et par les routes HTTP F2 isolées.

## 10. Dormance

Aucun montage dans `api.py`, aucune activation de route, aucun changement Railway, aucun frontend :
`api:app` n'expose aucune route `/api/v2` ni `/api/auth/f2` ; l'inventaire des marqueurs interdits dans
`api.py` (`test_authz_f2_dormancy`) est étendu aux symboles de ce contrat. Les anciennes routes publiques sont
inchangées (suite historique complète).

## 11. Adaptations nominatives des tests historiques

Aucune assertion de sécurité supprimée ; chaque inventaire est étendu avec une assertion fermée.

| Test | Adaptation |
|---|---|
| `test_authz_f2_boundary::test_adapter_statements_are_owner_scoped_and_non_transactional` | + `_LOCK_LINKED_IDENTITIES` (soumise aux mêmes règles : `SELECT`, `WHERE user_id = :u`, aucun SQL interdit) |
| `test_authz_f2_boundary::test_business_locks_match_the_announced_plan` | + `_LOCK_LINKED_IDENTITIES` dans les instructions verrouillantes, comme verrou d'identité (jamais `spec.lock`) |
| `test_authz_f2_boundary::test_no_operation_conn_principal_contract_remains` | adaptateurs publics + `lock_linked_identities` |
| `test_authz_f2_boundary::test_runner_lock_order_and_t_authorization` | ordre vérifié : session -> génération -> **identité** -> métier -> T_authorization |
| `test_authz_f2_runner::test_revision_conflict_is_a_distinct_closed_status` | 7 statuts distincts |
| `test_authz_f2_runner::test_profile_mutation_lock_order_and_t_authorization` | rang identité inséré ; seul `KEY SHARE` admis : le verrou d'identité ; 4 verrous au lieu de 3 |
| `test_authz_f2_dormancy::test_api_source_never_mentions_the_authorization_boundary` | + marqueurs ACT-L1-PR1 |
| `test_authz_f2_portfolio_concurrency::test_email_linking_finalization_and_portfolio_mutation_in_both_orders` | voir § 12 |
| `test_authz_f2_pr9c3::B2_EXPECTED` (`test_the_closed_b2_refusal_matrix_never_declares`) | + `IDENTITY_REQUIRED` -> `403 identity_link_required` (vocabulaire B2 toujours entièrement inventorié, aucune déclaration) |
| `test_authz_f2_pr9c3_boundary::test_the_closed_matrices_cover_exactly_the_frozen_vocabularies` | matrices B1 / C1-B / C2 + `identity_link_required` ; codes émis + `identity_link_required` ; 403 vérifié dans chaque matrice |

## 12. Contre-épreuves (`tests/test_act_l1_pr1_identity.py`)

Statut fermé ; instruction sans PII ; refus des **vingt** commandes par une session d'invitation non liée
(capacités Décrypter réelles d'un autre utilisateur, état durable et xmin inchangés, aucun fournisseur, hooks
`prevalidated`, `locked` seulement) ; e-mail d'invitation jamais une preuve ; aucun verrou métier sur refus,
ordre des instructions observé ; liaison OTP puis même `user_id` admis (session d'invitation inchangée),
connexion OTP ultérieure sans création ni fusion ; liaison au profil d'autrui sans transfert ; jugement par
utilisateur ; session révoquée, expirée, non confirmée, révoquée pendant l'attente → `SESSION_INVALID` sans
fuite ; profil absent et titulaire incohérent → `UNAVAILABLE` prioritaire ; matrice d'admissibilité ; liaison
puis expurgation ; session de connexion non admise sur son seul `auth_method` ; concurrence liaison / runner
dans les deux ordres pour les quatre plans ; connexion OTP concurrente sans attente ; 403 exact sur les treize
routes (inventaire comparé), puis 200 / 201 après liaison ; 401 conservé ; parcours HTTP complet invitation ->
403 -> liaison HTTP -> 200 -> connexion HTTP -> même profil ; matrices C3 / B2 ; B2 sans fournisseur après
refus de réautorisation ou de lecture de contexte ; C1-B sans livraison ; replay / ACK refusés puis ACK appliqué
après rétablissement ; ressource d'autrui 403 puis 404 ; dormance.

`test_email_linking_finalization_and_portfolio_mutation_in_both_orders` (R1 PR 7) est adapté
**nominativement** : la mutation concurrente d'une liaison reste exercée dans les deux ordres, sans cycle ;
le résultat attendu de l'ordre « mutation d'abord » devient `IDENTITY_LINK_REQUIRED` (aucune position écrite),
puis la même mutation, rejouée après la liaison, est autorisée — même nombre final de positions et
d'identités.

## 12 bis. Résultats des tests (mesurés)

Commit testé : `5ae727a` (code identique au commit final, qui n'ajoute que cette section).

| Exécution | Serveur | Résultat |
|---|---|---|
| CI `ci / postgres-16` (run 42462642159, Python 3.11.9) | PostgreSQL 16.15 (image épinglée) | **13 047 passed**, 0 failed, 0 skipped, 1 warning (préexistant : `SAWarning` de `test_authz_f2_pr9c1a.py`) |
| CI `ci / postgres-18` | PostgreSQL 18.6 (image épinglée) | job `success` (contrôle `check_test_report`) |
| CI `ci / migrations-16`, `ci / migrations-18`, `ci / web`, `ci / required` | — | `success` |
| locale, suite complète, Python 3.13 | PostgreSQL 16.15 | **13 047 passed**, 0 failed |
| locale, suite complète, Python 3.13 | PostgreSQL 18.6 (même image par digest, via `mirror.gcr.io`) | 13 046 passed, **1 failed** : `test_migration_0016_portfolio_idempotency.py::test_pg_concurrent_same_key_after_rollback_inserts_once` |

L'échec local PG 18 est un test de migration sur SQL brut, sans lien avec le code modifié (« la seconde
transaction n'a jamais attendu le verrou » : fenêtre d'observation de 10 s), survenu pendant que deux suites
complètes partageaient 4 CPU ; rejoué isolément trois fois sur le même serveur : 10/10 passed à chaque fois ;
vert en CI. Les ~2 000 avertissements locaux sont des `DeprecationWarning` de `datetime.utcnow()` propres à
Python 3.13 (CI : 3.11.9, un seul avertissement).

Suites ciblées : `tests/test_act_l1_pr1_identity.py` 41 passed (PG 16 et 18) ; frontières, concurrence F2,
dormance, runner, B2, C3 : verts dans les deux suites complètes. Contre-épreuves vérifiées discriminantes :
règle d'identité neutralisée (toujours admise) → 22 échecs + 1 erreur ; mapping HTTP retiré d'une route → la
contre-épreuve des treize routes échoue (mutations temporaires, restaurées).

## 13. Points d'implémentation signalés à l'audit

1. **Issuer non comparé** : le runner ne reçoit aucune configuration Supabase (dépendances figées :
   trousseaux, horloge) ; la règle porte sur `provider` (liste fermée), la vérification durable et
   l'appartenance au `user_id`. Une identité est toujours créée par la liaison F2 avec l'issuer configuré du
   moteur F2. Si l'architecture exige de restreindre à l'issuer courant, il faudra une dépendance nouvelle du
   runner : décision à prendre, non improvisée ici.
2. **`email_verified_at` non comparé à `T_authorization`** : l'identité lue est committée avant notre
   instruction (même horloge PostgreSQL) ; la comparaison n'ajoute aucune garantie et fragiliserait les
   horloges de test.
3. **Ligne d'identité incohérente** (vérifiée sans adresse ni expurgation, état jamais écrit par F2) :
   traitée comme non admissible (`IDENTITY_LINK_REQUIRED`), pas comme `UNAVAILABLE` : le refus reste fermé et
   n'expose rien. Corollaire : `start_email_linking` refuse un profil ayant déjà une ligne
   (`profile_already_linked`) — un tel état forcé exigerait une intervention d'exploitation.
4. **Verrou `FOR KEY SHARE`** plutôt qu'une lecture simple (défense contre tout futur transfert / suppression)
   ou `FOR SHARE` (qui ferait attendre la connexion OTP).

## 14. Risques restants (concurrence, autorisation, activation)

- La garantie concurrente repose sur l'invariant « toute insertion d'identité a lieu sous `users FOR
  UPDATE` » (seule écriture actuelle : la liaison F2). Toute future voie d'écriture d'identité (administration,
  import, expurgation qui viderait `email_verified_at`) devra respecter ce rang ou être revue.
- Une future « déliaison » devra prendre un verrou incompatible avec `FOR KEY SHARE` (suppression ou
  changement de clé : c'est le cas) et être jugée contre ce contrat.
- Le frontend F2 (hors PR) devra traiter `403 identity_link_required` en dirigeant vers le parcours de liaison
  e-mail ; aucune logique d'autorisation ne doit y être reportée.
- Activation publique, montage dans `api.py` et validation Railway : PR ultérieures du chantier.

## 15. Fermeture de l'audit indépendant (SHA audité `e585c4f`)

### 15.1 Référence architecturale V1.2 : origine et limite

Recherche effectuée (8 novembre 2026) : dépôt complet (`git grep` sur `ACT-L1`, `Activation F2`, `V1.2`,
`Lot 1`, `identity_link`), issues et pull requests GitHub (seule la #236 mentionne ACT-L1, sans commentaire ni
revue publiés), artefacts claude.ai accessibles, sessions Claude Code antérieures (aucune session
d'architecture « Activation F2 Lot 1 »). **Le document V1.2 intégral n'est accessible dans aucune de ces
sources.**

Référence réellement utilisée, et seule source normative disponible : **le cahier des charges transmis par le
fondateur au lancement de l'implémentation** (message d'ouverture de la session Claude Code
`session_01XArXqoVqvNudSH6VtbRKXU`, 8 novembre 2026), qui déclare l'architecture « Activation F2 Lot 1
V1.2 figée » et en reprend le contrat cible (sections OBJECTIF, FRONTIÈRES À PRÉSERVER, CONTRAT CIBLE,
IMPLÉMENTATION ATTENDUE, CONTRE-ÉPREUVES OBLIGATOIRES). Il complète les contrats R1 figés du dépôt que la
V1.2 déclare préserver (`docs/f2_r1_pr2.md` § 6, `docs/f2_r1_pr4.md`, `docs/f2_r1_pr9b2.md`,
`docs/f2_r1_pr9c3.md`). La description de la PR n'a servi de référence pour aucune exigence ci-dessous.

Conséquence : la conformité est démontrée **à ce cahier des charges**. Toute décision de la V1.2 qui n'y
figurerait pas est **non vérifiable** ici (§ 15.4, R-1).

### 15.2 Matrice exigences → implémentation → tests

Exigences citées du cahier des charges (abrégées) ; tests de `tests/test_act_l1_pr1_identity.py` sauf mention.

| # | Exigence | Implémentation | Tests |
|---|---|---|---|
| E1 | Statut fermé `IDENTITY_LINK_REQUIRED = "identity_link_required"`, distinct de SESSION_INVALID, NOT_FOUND, UNAVAILABLE, REVISION_CONFLICT, IDEMPOTENCY_CONFLICT | `runner.IDENTITY_LINK_REQUIRED`, `STATUSES` (7) | `test_identity_link_required_is_a_seventh_distinct_closed_status` ; `test_authz_f2_runner::test_revision_conflict_is_a_distinct_closed_status` |
| E2 | Règle dans le runner F2, unique autoriseur ; aucun contrôle frontend ni dépendance HTTP comme seule protection | `_run` / `_decide` ; aucune route ne décide | `test_an_unlinked_invitation_session_is_refused_for_all_twenty_commands` (runner direct, sans HTTP) |
| E3 | Contrôle serveur, transactionnel, dans la transaction d'autorisation | rang identité de la phase B, avant `T_authorization` | `test_the_refusal_takes_no_business_lock_and_decides_after_the_identity_rank` ; `test_a_linked_session_uses_the_identity_rank_before_business_locks` |
| E4 | Ordre des verrous non modifié arbitrairement ; BF1/BF2 et revalidations à T_authorization conservés | insertion après génération (ordre global F2 `… génération -> identité`), aucun rang remonté | `test_authz_f2_boundary::test_runner_lock_order_and_t_authorization` ; `test_authz_f2_runner::test_profile_mutation_lock_order_and_t_authorization` ; suites boundary existantes |
| E5 | Session invalide, expirée ou révoquée : priorité et résultat existants (401), sans fuite sur l'identité | `_decide` : validité jugée en premier | `test_an_invalid_session_keeps_session_invalid_whatever_the_identity` ; `test_an_invalid_session_keeps_its_401_over_http` |
| E6 | Absence de profil / état incohérent : refus fermé selon la priorité existante | `_INCOHERENT` avant la règle d'identité | `test_a_missing_profile_stays_unavailable_and_is_never_created` ; `test_a_profile_mismatch_stays_unavailable_before_the_identity_rule` |
| E7 | Refus sans principal, donnée personnelle, résultat métier ni information sur l'identité | `AuthorizationOutcome(IDENTITY_LINK_REQUIRED)` ; instruction sans PII | `refused()` dans tous les tests ; `test_the_identity_statement_reads_no_personal_data_and_is_owner_scoped` ; corps HTTP exact vérifié octet pour octet |
| E8 | Preuve = `auth_identities` durable du `user_id` autorisé, vérification fournisseur/e-mail établie | `lock_linked_identities` (fournisseur `supabase`, `email_verified_at`) | `test_identity_rows_are_admitted_only_with_a_durable_verification` ; `test_otp_linking_admits_the_same_profile_and_a_later_sign_in_finds_it` |
| E9 | Jamais une preuve : e-mail d'invitation, `user_id` navigateur, localStorage, seul `auth_method`, ligne sans vérification | aucune de ces sources n'est lue ; aucun `user_id` client n'existe dans les commandes | `test_an_invitation_email_is_never_a_proof_of_link` ; `test_a_sign_in_session_is_not_admitted_on_its_auth_method_alone` ; `test_identity_rows_are_admitted_only_with_a_durable_verification[False-False-False]` ; `test_authz_f2_runner::test_commands_carry_no_owner_and_validate_their_fields` |
| E10 | E-mail effacé pour confidentialité : n'invalide pas automatiquement une vérification durable prouvée | admise si `email_verified_at` ET `pii_redacted_at` | `test_identity_rows_are_admitted_only_with_a_durable_verification[True-True-True]` ; `test_redacting_a_linked_identity_keeps_access` ; refus si jamais vérifiée : `[False-True-False]` |
| E11 | Invitation : profil et session créés selon les contrats existants ; liaison e-mail fonctionnelle | `core/auth_f2*` inchangés | `test_http_invitation_then_http_linking_then_http_sign_in_reach_the_same_profile` ; suites `test_f2_pr4_*` inchangées |
| E12 | Après liaison OTP : même profil admissible ; connexion OTP ultérieure retrouve le profil sans création ni fusion | — (effet de la règle) | `test_otp_linking_admits_the_same_profile_and_a_later_sign_in_finds_it` ; test HTTP E11 |
| E13 | Liaison au profil d'autrui : refus sans transfert | `conflict_other_profile` (inchangé) | `test_linking_an_identity_owned_by_another_profile_transfers_nothing` |
| E14 | HTTP 403 `{"error":"identity_link_required"}` sur profil/niveau, portefeuille, consultation, Décrypter B1/C1-B/C2 ; jamais 401/404/409/503 | `routes.py`, `portfolio.py`, `consultation.py`, `decryptage.py` | `test_every_personal_route_answers_403_without_a_linked_identity` (13 routes, inventaire comparé) ; `test_http_matrices_map_identity_link_required_to_403` ; `test_authz_f2_pr9c3_boundary::test_the_closed_matrices_cover_exactly_the_frozen_vocabularies` |
| E15 | B2 : sens préservé à la réautorisation ReadProfile et aux lectures de contexte, aucun fournisseur après un refus | motif `IDENTITY_REQUIRED` ; `_f2` lève avant l'étape suivante | `test_b2_reauthorization_refusal_calls_no_provider` ; `test_b2_context_read_refusal_stops_before_any_further_provider` ; `test_http_turn_refusals_from_b1_b2_and_c1b_are_403` ; `test_authz_f2_pr9c3::test_the_closed_b2_refusal_matrix_never_declares[identity_link_required]` |
| E16 | Réponses canoniques, COMMIT incertain, idempotence, protection inter-utilisateurs conservés | aucun code de résultat modifié | suites R1 PR5–PR9-C3 vertes ; `test_an_invited_user_cannot_replay_or_ack_another_users_delivery` ; `test_each_user_is_judged_on_their_own_identities` ; `test_a_bob_session_is_unaffected_by_another_profiles_identity_state` |
| E17 | Reprises Décrypter et ACK | — | `test_replay_and_ack_are_403_without_identity_and_the_ack_applies_once_restored` ; `test_http_provider_failure_with_a_refused_declaration_is_503_and_b1_governs_the_resumption` |
| E18 | Aucune écriture métier ni appel fournisseur sur refus | ROLLBACK avant exécution, aucun verrou métier | test E2 (état durable et xmin inchangés) ; `test_http_c1b_refusal_writes_no_delivery` |
| E19 | Liaison concurrente à une lecture ou mutation : résultat transactionnel cohérent | sérialisation au rang users | `test_a_linking_committed_while_the_runner_waits_is_seen_at_t_authorization[×4 plans]` ; `test_a_linking_started_after_the_runner_locks_waits_and_the_command_is_refused[×4]` ; `test_a_sign_in_finalization_never_waits_on_the_identity_rank` ; `test_authz_f2_portfolio_concurrency::test_email_linking_finalization_and_portfolio_mutation_in_both_orders` |
| E20 | Aucun contournement via les 20 commandes | contrôle dans `_run` | test E2 (`set(REGISTRY)` comparé) |
| E21 | Dormance : aucun montage `api.py`, aucune route activée, aucun Railway, aucun frontend ; anciennes routes publiques inchangées | `api.py`, `web*`, `alembic/` non modifiés | `test_api_app_still_exposes_no_f2_personal_route` ; `test_authz_f2_dormancy` (inventaire étendu) ; suite historique complète |
| E22 | Ne pas modifier moteurs pédagogiques, données financières, schémas de preuve, progression, analyse | aucun fichier de ces domaines modifié | `git diff 564a19a..HEAD --stat` (§ 2) |

### 15.3 Points vérifiés à la demande de l'audit

- **Identité vérifiée puis expurgée** : admise (E10) — conforme à l'exigence explicite « ne doit pas
  automatiquement invalider ». Une ligne expurgée **jamais** vérifiée reste refusée.
- **Fournisseur Supabase** : liste fermée `("supabase",)`, identique à `ck_auth_identities_provider`.
  **Issuer** : le cahier des charges ne demande pas de filtrage par issuer (« identité fournisseur liée et
  vérifiée », « vérification fournisseur/e-mail établie ») ; R1 PR 2 (§ 6) enregistre l'issuer configuré à la
  liaison. Aucun filtrage n'est ajouté (il exigerait une dépendance nouvelle du runner) : non vérifiable contre
  la V1.2 intégrale (R-1).
- **Priorités et ordre des verrous** : E4 à E6, vérifiés par tests ; les verrous existants sont pris dans le
  même ordre et le rang identité s'insère à sa place dans l'ordre global F2 déjà figé.
- **B2, échec fournisseur puis `RecordDecryptageAttemptFailure` refusé `identity_link_required`** : comportement
  confirmé et désormais fixé par deux contre-épreuves (ajoutées par cette fermeture) :
  `test_b2_provider_failure_then_refused_declaration_keeps_the_lease[identity_lost]` (DECLARATION_REFUSED, un
  seul appel Claude, déclaration refusée avant exécution, réservation et xmin inchangés ; témoin `[control]` :
  DECLARED, réservation écrite) et
  `test_http_provider_failure_with_a_refused_declaration_is_503_and_b1_governs_the_resumption` (HTTP `503
  temporarily_unavailable`, bail conservé, reprise de la même intention → `202 in_progress`, aucun nouvel appel
  Claude). **Aucun comportement modifié** (R-2).

### 15.4 Réserves restantes

- **R-1 — V1.2 intégrale inaccessible.** Conformité démontrée au cahier des charges du fondateur seulement ;
  toute décision de la V1.2 absente de ce cahier (notamment un éventuel filtrage par issuer) n'est pas
  vérifiable. À lever par le fondateur en confirmant que le cahier des charges reproduit intégralement les
  décisions ACT-L1-PR1 de la V1.2, ou en versant la V1.2 au dépôt.
- **R-2 — Déclaration B2 refusée pour identité → 503.** Le cahier des charges impose que le refus ne soit
  « jamais transformé silencieusement en 401, 404, 409 ou 503 » ; les contrats R1 figés imposent que B2
  rapporte une déclaration refusée comme `DECLARATION_REFUSED` (vocabulaire fermé de trois valeurs, sans
  motif) et que C3 la présente en `503 temporarily_unavailable`. Dans ce chemin, la réponse décrit l'échec
  fournisseur dont la déclaration n'a pas été confirmée, pas le refus de la commande principale. Le chemin
  n'est atteignable que si l'identité devient non admissible après la réautorisation B2 (aucune écriture F2
  ne le produit : pas de déliaison, FK composites). Le présenter en 403 exigerait d'étendre le vocabulaire figé
  `DECLARATIONS` et la matrice `_PROVIDER_REPLIES` : décision d'architecture **non prise ici**, à arbitrer par
  le fondateur. Le comportement fail-closed est préservé (bail jamais présumé libéré).

## 16. Clôture des réserves (audit sur `d6de88f`, 9 novembre 2026)

### 16.1 Référence contractuelle

Le fondateur a identifié la référence du chantier : la **corrective architecturale « Activation F2 V1.2 » du
8 novembre 2026**, établie sur `564a19a`, explicitement validée et figée dans la conversation d'architecture.
Sa section **F-01** est la référence contractuelle de PR1 ; les autres sections, notamment **F-05**, fixent les
contraintes transversales à préserver.

Limite de preuve : le texte de cette corrective n'est versé ni au dépôt ni dans la PR, et n'a pas été transmis
à cette session. La conformité ci-dessous est donc établie contre (a) le cahier des charges du fondateur
(§ 15.1), qui en reprend le contrat cible, et (b) les trois clarifications formulées par le fondateur et par
l'audit le 9 novembre 2026. Elle ne prétend à aucune lecture directe de F-01 ou F-05.

Les § 16.2 à 16.4 sont des **clarifications d'interprétation** de comportements existants, vérifiés par tests.
Ils ne présentent aucune règle nouvelle comme faisant partie du freeze historique et ne modifient aucun code de
production.

### 16.2 Expurgation

- Une identité **durablement vérifiée** (`email_verified_at` renseigné) puis **expurgée** (`verified_email_normalized`
  NULL, `pii_redacted_at` renseigné) **reste admissible**.
- Une identité **jamais vérifiée** (`email_verified_at` NULL) **n'est jamais admissible**, qu'elle soit
  expurgée ou non.
- Une ligne vérifiée sans adresse **ni** expurgation (état jamais écrit par F2) n'est pas admissible : refus fermé.
- Code : `_adapters.lock_linked_identities`. Tests :
  `test_identity_rows_are_admitted_only_with_a_durable_verification[True-True-True]` (admise),
  `[False-False-False]` et `[False-True-False]` (jamais admises), `test_redacting_a_linked_identity_keeps_access`,
  `test_a_verified_timestamp_without_address_nor_redaction_proves_nothing`.

### 16.3 Issuer

**Comportement actuel du runner** : il vérifie le fournisseur (liste fermée `LINKED_IDENTITY_PROVIDERS =
("supabase",)`, identique à `ck_auth_identities_provider`), la vérification e-mail durable et l'appartenance au
`user_id` autorisé. **Il ne compare pas l'issuer.** Il ne reçoit aucune configuration Supabase. Aucune
dépendance nouvelle n'est ajoutée : décision non prise ici. Test :
`test_the_runner_checks_the_provider_but_not_the_issuer_current_behaviour`. Ce test force une ligne d'un autre
issuer par SQL direct ; elle est admise par le runner, ce qui fixe le comportement sans le présenter comme une
garantie.

**Les écritures existantes imposent l'issuer configuré.** Constats vérifiés sur `d6de88f` :

1. Il n'existe **qu'une seule** écriture d'identité dans le code de production : l'`INSERT` de
   `F2AuthService._prepare_linked_identity` (`core/auth_f2/service.py`). On n'y trouve aucune autre insertion,
   aucune suppression, aucune construction ORM `AuthIdentity(...)`. Cela vaut pour `api.py`, `scripts/` et les
   migrations, où `0017` crée la table sans donnée. Les deux seules mises à jour portent sur
   `last_authenticated_at` (connexion) et `created_at` / `email_verified_at` (liaison). Aucune ne touche
   `issuer`, `subject`, `provider` ni `user_id`. Test d'inventaire :
   `test_identities_are_written_only_by_the_f2_linking_with_the_configured_issuer`.
2. Cet `INSERT` lie `issuer = self._supabase.issuer`, l'issuer du fournisseur injecté dans le moteur F2.
   - `F2AuthService.__init__` refuse tout fournisseur dont l'issuer ne respecte pas `https://<hôte>/auth/v1`
     (`is_valid_issuer`).
   - `HttpSupabaseOtpProvider` le dérive de l'URL configurée du projet : `base_url + "/auth/v1"`.
3. Le `subject` vient exclusivement d'une `VerifiedIdentity`. Celle-ci n'est construite qu'à partir d'une
   réponse `verifyOtp` conforme : HTTP 200, `user.id` UUID canonique, `aud = authenticated`, même e-mail,
   `email_confirmed_at` non vide, `access_token` présent (`core/auth_f2/providers.py`, R1 PR 2 § 6).
4. L'insertion n'est conservée qu'après revalidation complète sous verrous. Sinon, le point de sauvegarde est
   annulé.
5. La FK composite `auth_operations_proven_identity_fkey` lie toute opération committée au quadruplet
   `(id, provider, issuer, subject)` de l'identité prouvée. La connexion OTP recherche l'identité par
   `(supabase, issuer configuré, subject)` : une identité d'un autre issuer ne peut jamais ouvrir de session.
6. Tests existants : `test_f2_service.py` (issuer enregistré = issuer configuré),
   `test_f2_normalize_providers.py` (`is_valid_issuer`, dérivation de l'issuer).
   Ajouté : `test_otp_linking_records_the_configured_provider_issuer_and_proven_subject`.

**Conclusion** : aucun chemin applicatif actuel ne permet d'introduire une identité arbitraire, ni avec un
issuer non configuré. **Limite résiduelle** : PostgreSQL contraint l'issuer seulement à `^https://`. Une
écriture SQL directe, hors application (exploitation, import manuel), pourrait créer une ligne d'un autre
issuer, que le runner admettrait. Toute future voie d'écriture d'identité devra conserver ces invariants ; le
test d'inventaire la signalera.

### 16.4 Décrypter B2 : deux cas distincts

| Cas | Ce que refuse le runner | Résultat B2 | HTTP |
|---|---|---|---|
| **Refus direct d'identité** : la commande nécessaire au tour (B1, réautorisation `ReadProfile`, lecture de contexte, C1-B, C2) rend `IDENTITY_LINK_REQUIRED` | la commande demandée par l'utilisateur | `REFUSED` / `IDENTITY_REQUIRED` (B2) ou statut runner (B1, C1-B, C2) | **403 `identity_link_required`** |
| **Échec fournisseur, puis déclaration refusée** : EODHD, FMP ou Claude échoue ; la déclaration interne `RecordDecryptageAttemptFailure` rend `IDENTITY_LINK_REQUIRED` | la **déclaration** de l'échec, opération de comptabilité interne du bail, après l'échec | `PROVIDER_FAILED` / `DECLARATION_REFUSED` | **503 `temporarily_unavailable`** |

**Pourquoi le second cas n'est pas une conversion d'un refus d'identité en indisponibilité :**

1. **Cause de l'échec.** L'issue du tour est déjà fixée avant la déclaration : le fournisseur a échoué
   (`PROVIDER_FAILED`). Le tour ne peut pas produire de réponse, quelle que soit la déclaration. La
   déclaration ne demande aucun accès personnel. Elle informe B1 de l'échec pour libérer le bail par
   anticipation.
2. **Vocabulaire figé R1 PR 9-B2.** L'issue d'une déclaration est l'une de trois valeurs fermées (`DECLARED`,
   `DECLARATION_REFUSED`, `DECLARATION_UNCERTAIN`), rapportée telle quelle. Une déclaration refusée signifie
   seulement que le bail n'est pas présumé libéré. Le motif du refus n'est pas transporté, par construction.
3. **Matrice figée R1 PR 9-C3 (`_PROVIDER_REPLIES`).** `provider_unavailable` (503) est réservé à une
   déclaration **confirmée**. Toute déclaration non confirmée donne `503 temporarily_unavailable`, quel qu'en
   soit le motif : session révoquée, réservation, conflit, identité. Le refus d'identité n'y est pas traité à
   part ; il suit la règle déjà figée pour toute déclaration non confirmée.
4. **Fail-closed et reprise.** Le bail n'est pas présumé libéré. Toute reprise, même à bail actif, repasse par
   B1, donc par le runner, qui exige à nouveau l'identité :
   - tant qu'elle n'est pas admissible, la reprise obtient **403 `identity_link_required`**, sans écriture.
     L'indisponibilité ne masque donc jamais le refus d'identité au-delà de la réponse à l'échec fournisseur.
   - une fois l'identité admissible, la même intention retrouve le bail actif (`202 in_progress`), sans nouvel
     appel Claude.
5. **Atteignabilité.** Ce cas suppose que l'identité, admise par B1 et par la réautorisation B2 quelques
   instants plus tôt, cesse d'être admissible avant la déclaration. Aucune écriture F2 ne le produit : il
   n'existe aucune déliaison, et les FK composites figent l'identité. Il n'est atteignable que par un état forcé.

Comportement **conservé**, vérifié par :
- `test_http_turn_refusals_from_b1_b2_and_c1b_are_403` et `test_b2_reauthorization_refusal_calls_no_provider`
  (refus direct → 403, aucun fournisseur) ;
- `test_b2_provider_failure_then_refused_declaration_keeps_the_lease[identity_lost|control]` ;
- `test_http_provider_failure_with_a_refused_declaration_is_503_and_b1_governs_the_resumption`.

La réserve R-2 du § 15.4 est ainsi close **par interprétation documentée**, sous réserve de sa conformité à
F-01 et F-05 (texte non consulté, § 16.1).
