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
  reste `DECLARATION_REFUSED` (bail jamais présumé libéré).
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
