# R1 PR 9-B1 — Réservation durable, idempotence, bail et fencing F2 (dormante)

## 1. Références normatives et base

- Architecture R1 PR9-B V1 + V2 + V2.1 : freeze validé.
- Contrat d'exécution R1 PR9-B1 E1 + E1.1 + E1.2 : freeze validé ; E1.2
  prévaut sur E1.1 et E1. Ces documents ne sont pas versionnés dans le
  dépôt : le contrat appliqué est celui reproduit dans la demande.
- Base : `main` = `3767fe86096fd42d4d579aec38b9fdd42780f6b5` (merge de la
  PR #229, R1 PR9-A), identique au SHA attendu ; aucune divergence.
- Tête Alembic avant : `0020_position_revision` ; après :
  `0021_decryptage_reservations`.

Aucune décision architecturale nouvelle. Les points où le contrat laissait
un détail d'implémentation ouvert sont listés au § 16 (à valider).

## 2. Périmètre livré

| Élément | Fichier |
|---|---|
| Migration 0021 (table, UNIQUE composite, FK, CHECK, downgrade gardé) | `alembic/versions/0021_decryptage_turn_reservations.py` |
| Modèle `DecryptageTurnReservation`, UNIQUE composite de `ConversationIdentity` | `core/models.py` |
| Prédicat privé de collision (seule modification) | `core/interaction_identity.py` |
| Trousseau `DecryptageKeyring`, trame HMAC v1, admission structurelle (pur) | `core/authz_f2/decryptage_fingerprint.py` |
| Politique de bail, `LeaseToken`, capacité `DecryptageLease`, vues fermées (pur) | `core/authz_f2/decryptage_reservation.py` |
| Commandes `ReserveDecryptageTurn`, `RecordDecryptageAttemptFailure` | `core/authz_f2/commands.py` |
| Adaptateurs SQL fermés, `Spec.decryptage_keyed`, registre à 16 | `core/authz_f2/_adapters.py` |
| Dépendance facultative `decryptage_keyring`, dispatch fermé | `core/authz_f2/runner.py` |
| Tests | voir § 13 |

Non livré (interdit) : route HTTP (ni `api:app`, ni application F2 isolée),
appel Claude ou EODHD, ticker canonique (`resolved_ticker` reste NULL),
`AssistantDelivery`, finalisation `reserved → finalized`, écriture
cognitive ou pédagogique, worker, cron, file, purge, activation Railway,
frontend, extension du pont ORM PR9-A.

## 3. Migration 0021

Fichier `0021_decryptage_turn_reservations.py` (nom autorisé), identifiant
de révision **`0021_decryptage_reservations`** : l'identifiant autorisé
fait 33 caractères alors que `alembic_version.version_num` est un
`VARCHAR(32)` ; même règle que `0013_decryptage_conv_affinity` (fichier
`0013_decryptage_conversation_affinity.py`).

- `conversation_identities` : `ADD CONSTRAINT
  uq_conversation_identities_key_owner UNIQUE (conversation_key, user_id)`,
  non différable ; `conversation_identities_pkey` conservée.
- `decryptage_turn_reservations` : 21 colonnes exactes du contrat (types
  TEXT / UUID / INTEGER / BIGINT / TIMESTAMPTZ), aucun `server_default`,
  aucun texte de question, contexte, prompt ou réponse.
- Contraintes nommées : PK `decryptage_turn_reservations_pkey` ; UNIQUE
  `uq_decryptage_turn_reservations_turn` (user_id, conversation_key,
  surface, source_user_turn_id, delivery_ordinal) ; UNIQUE
  `uq_decryptage_turn_reservations_assistant_delivery` (valeurs non nulles :
  NULLS DISTINCT) ; FK `…_user_id_fkey` (users), FK composite
  `…_conversation_fkey` (conversation_key, user_id) →
  `uq_conversation_identities_key_owner`, FK `…_assistant_delivery_id_fkey`.
- 13 CHECK nommés : surface `= 'decryptage'`, ordinal `= 1`, version
  d'empreinte `IN ('oryx-decryptage-reservation-v1')`, version de clé
  `^[a-z0-9][a-z0-9_-]{0,31}$`, empreinte `^[0-9a-f]{64}$`, empreinte de
  jeton (même format ou NULL), état, motif, compteurs ≥ 0, forme de
  `reserved` (livraison, finalisation et motif NULL ; jeton + échéance XOR
  backoff), de `blocked` (motif présent ; jeton, échéance, backoff,
  livraison, finalisation NULL), de `finalized` (livraison et finalisation
  présentes ; jeton, échéance, backoff, motif NULL), horodatages.
- Aucun index supplémentaire (ceux des PK / UNIQUE), aucun trigger,
  fonction, type, backfill ni réparation ; aucune donnée écrite hors
  `alembic_version`. Garde-fous `SET LOCAL` (5 s / 15 s / 15 s) : un verrou
  DDL non obtenu fait échouer la migration.
- Downgrade : `LOCK … ACCESS EXCLUSIVE`, refus s'il existe une réservation
  (bloc `DO` évalué à l'exécution en mode offline), sinon `DROP TABLE` puis
  retrait de la contrainte composite : retour exact à 0020 (catalogue et
  données comparés).

Les CHECK n'établissent pas l'immutabilité : elle est garantie par les
mutations SQL fermées (§ 7) ; aucune ne réécrit identité, empreinte,
versions ni `created_at`, et toutes portent `state = 'reserved'`.

## 4. `conversation_identities` (E1.2)

**Impact exact sur `core/interaction_identity.py`** : le prédicat privé
`_is_primary_key_violation` devient `_is_identity_collision` :

```python
diag.sqlstate == "23505" and diag.constraint_name in (
    "conversation_identities_pkey", "uq_conversation_identities_key_owner")
```

Interface publique, SAVEPOINT, relecture du gagnant, `populate_existing`,
validation du propriétaire, absence de commit / rollback propriétaire et de
réattribution : inchangés (contrôlés statiquement et sur PostgreSQL).

**Nécessité mesurée** : PostgreSQL contrôle les index uniques dans l'ordre
de leurs OID. Sur une base créée par les migrations, la PK (plus ancienne)
est signalée ; après reconstruction de la PK (dump / restauration, REINDEX
ou recréation de contrainte), la contrainte composite est signalée pour une
collision de même propriétaire :
`test_postgresql_may_report_the_composite_constraint_and_both_writers_resolve_it`
le reproduit (SQLSTATE `23505`, contrainte `uq_conversation_identities_key_owner`,
que l'ancien prédicat n'aurait pas reconnue) puis montre que les writers
historiques et l'adaptateur F2 concurrents résolvent la collision.

**Adaptateur F2** `_register_conversation` (`core/authz_f2/_adapters.py`),
seconde écriture applicative inventoriée dans BF1 : propriété lue avant
création (`SELECT user_id = :u AS owned … WHERE conversation_key = :k`,
jamais le propriétaire d'autrui) ; absente : `INSERT` dans un SAVEPOINT
local (`conn.begin_nested()`, connexion du runner) ; seule une violation
23505 de l'une des deux contraintes est traduite, le gagnant étant relu par
une NOUVELLE instruction (READ COMMITTED) : même propriétaire → poursuite,
autre → `NOT_FOUND` sans écriture, introuvable → refus fermé. Toute autre
erreur est propagée (testé avec des erreurs injectées 23505 / 23503 / 23514
portant d'autres noms ou les noms ciblés). Aucune Session, connexion ni
helper générique.

## 5. Commandes F2 et runner

Registre fermé : **16 commandes** (14 + `ReserveDecryptageTurn`,
`RecordDecryptageAttemptFailure`), indexé par type exact, sous-classes
refusées. Plan `DECRYPTAGE_MUTATION` **inchangé** ; `spec.lock =
_no_business_lock` : aucun préverrouillage avant T_authorization ; les
verrous métier sont pris par l'adaptateur d'exécution après l'autorisation
positive. Ordre des verrous F2 inchangé.

| Commande | Entrées | Refusé du client |
|---|---|---|
| `ReserveDecryptageTurn` | conversation_key, client_turn_id, ticker_input, question, context, last_method_id | propriétaire, epoch, jeton, échéance, tentatives, HMAC, versions, état, motif |
| `RecordDecryptageAttemptFailure` | reservation_id, lease_epoch, `LeaseToken` brut, classification fermée | tout le reste |

**Runner (E1.1)** — modifications minimales :

- `Spec.decryptage_keyed: bool = False`, distinct de `Spec.keyed`
  (portefeuille exclusivement) ; `Spec.__post_init__` refuse les deux à la
  fois ; défense dans `run()` : `UNAVAILABLE` + alerte
  `key_families_conflict` avant toute transaction ;
- paramètre facultatif `decryptage_keyring` (type EXACT
  `DecryptageKeyring`, stocké en privé `_decryptage`) ; aucune de ses clés
  ne peut être une clé F2 ou portefeuille (`AuthorizationConfigError`) ;
- absent : les deux commandes Décrypter rendent `UNAVAILABLE` avant toute
  transaction (`decryptage_keyring_missing`), les autres commandes sont
  inchangées ;
- dispatch : `keyed` → trousseau portefeuille + points d'interruption ;
  `decryptage_keyed` → trousseau Décrypter ; sinon aucun. `run(credential,
  command)` inchangé ; aucun conteneur générique ; aucune variable
  d'environnement.

Résultats du runner : `AUTHORIZED` + vue (après COMMIT confirmé) ;
`NOT_FOUND` (conversation ou réservation d'autrui ou inexistante) ;
`IDEMPOTENCY_CONFLICT` (même tour, autre intention) ; `REVISION_CONFLICT`
(capacité périmée : état, epoch, jeton ou échéance — l'epoch est la
révision du bail) ; `SESSION_INVALID` ; `UNAVAILABLE` (panne, clé ou version
introuvable, ligne incohérente, epoch épuisé, COMMIT incertain). Tous les
refus sont des ROLLBACK sans écriture.

**Un `AUTHORIZED` n'autorise pas Claude** : seul `LeaseGranted` porte une
capacité (§ 9).

## 6. HMAC versionné

`oryx-decryptage-reservation-v1`, HMAC-SHA256, clé de 32 octets du
`DecryptageKeyring` dédié (aucun repli vers F2, CSRF ou portefeuille ;
types distincts, valeurs distinctes vérifiées), 64 hex minuscules.

Trame : `ASCII("ORYX-DECRYPTAGE-RESERVATION") || 0x00` puis, pour chacun
des 11 champs dans l'ordre (fingerprint_version, fingerprint_key_version,
user_id serveur, conversation_key, surface, source_user_turn_id,
delivery_ordinal, ticker normalisé, question, contexte, last_method_id) :
`u32be(len) || (0x00 | 0x01 || UTF-8)`. UUID minuscule avec tirets, entiers
décimaux, ticker `strip().upper()`, question / contexte `strip()`, NULL
distinct de `""`, aucune normalisation Unicode implicite.

Retry : version d'empreinte et version de clé ENREGISTRÉES, recalcul,
comparaison en temps constant ; divergence → `IDEMPOTENCY_CONFLICT` ;
version ou clé introuvable → `UNAVAILABLE` (alerte
`decryptage_key_unavailable`), jamais de remplacement silencieux. Le
SHA-256 historique d'`assistant_deliveries` n'est jamais comparé au HMAC.

Bornes (admission dans la commande, avant tout SQL, jamais de troncature) :
conversation 1–200 caractères sans blanc de bord ; ticker normalisé 1–32 ;
question ≤ 16 Kio, contexte ≤ 32 Kio, somme ≤ 48 Kio (UTF-8, valeurs
signées) ; last_method_id ≤ 64 caractères ; chaînes exactes sans NUL,
encodables en UTF-8 ; jeton 256 bits.

Vecteurs (clés fictives `d1 = bytes(range(32))`, `d2 = bytes(range(31, -1,
-1))`, tour `123e4567-e89b-42d3-a456-426614174000`, `user-a`, `conv-1`,
saisies `" mc.pa "`, `"  Pourquoi ?  "`, contexte vide, méthode NULL) :

| Vecteur | HMAC |
|---|---|
| base | `a2f2e0991778c42837c0682ebe0253bd9a9e98eca20a440600805ac8edda45eb` |
| last_method_id `""` | `0e978688e40998b194c5a9ccf48ed1345e4bffab33413ada5a9fc4c35d62cdd3` |
| last_method_id `construction_these` | `846e6fe2e502a3b535c31d0da8ffdb654885d7e57e0602fe08ffee906c4ad14d` |
| clé `d2` | `ebeed5fb7e5c75c33784a0b86a4a9c484463b1941d326d797c4412919c2138b0` |
| Unicode NFC | `803c158d98d424cb92c6a9c5e22c849d0c538ebdb985fa25bba85bf389eda795` |
| Unicode NFD | `2755d7925001446227dcdf225d7ce872c472867193afd2032d35a56ee0a95f80` |
| ticker `straße` → `STRASSE` | `b2b756e9c1b89dc0cde765648eba0708e5aeb21e77c0df223fdcede8ebb52f48` |
| autre user_id | `6b6541a85d439d9e526e5b6bde85fcfa72fae824b08407d293002b1fedaa7614` |
| autre tour | `795784ec6359a44b6b04b207ac5185efd45518fd74050fc244cc4446d3ca01c0` |

Chaque vecteur est recalculé par une implémentation indépendante
(`tests/test_authz_f2_pr9b1_crypto.py`), avec les octets exacts de la trame.

## 7. Machine d'états, bail, fencing

Paramètres serveur figés : bail 360 s, 3 attributions, backoffs 30 s puis
120 s, aucun renouvellement, aucune purge.

| État lu sous verrou, à T_lease_decision | Décision |
|---|---|
| absente, livraison historique de l'identité | INSERT `blocked / legacy_collision`, epoch 0, 0 tentative, aucun jeton, HMAC conservé |
| absente | INSERT `reserved`, epoch 1, 1 tentative, jeton haché, échéance = T + 360 s → `LeaseGranted` |
| empreinte divergente (tout état) | `IDEMPOTENCY_CONFLICT`, rien n'est écrit |
| `finalized` | lien relu (id ET user_id) et identité comparée : `ReservationFinalized`, ou refus si incohérent |
| `blocked` | `ReservationBlocked(motif)`, rien n'est écrit |
| `reserved` + livraison historique | `blocked / legacy_collision` (prévaut sur bail, backoff, reprise) |
| `reserved`, bail actif (T < échéance), même au plafond | `ReservationInProgress`, rien n'est écrit |
| `reserved`, backoff non échu | `ReservationRetryDeferred`, rien n'est écrit |
| `reserved`, bail expiré (T ≥ échéance) ou backoff échu, tentatives < 3 | epoch + 1, tentatives + 1, nouveau jeton, échéance T + 360 s → `LeaseGranted` |
| idem, tentatives = 3 | `blocked / attempts_exhausted` |

Déclaration d'échec : réservation propre (`id ET user_id … FOR UPDATE`),
`state = reserved`, epoch exact, jeton exact (temps constant), échéance
présente et `T_lease_decision < lease_expires_at` ; sinon
`REVISION_CONFLICT` sans écriture. Puis : livraison historique →
`blocked / legacy_collision` ; tentatives = 3 → `blocked /
attempts_exhausted` ; sinon libération (jeton et échéance NULL,
`retry_not_before_at = T + 30 s | 120 s`). Résultat `AttemptFailureRecorded`.

Chaque mutation est UNE instruction conditionnelle (`id`, `user_id`,
`state = 'reserved'`, epoch et compteur lus sous verrou ; libération : jeton
haché et `lease_expires_at > T`) relue par `RETURNING` ; aucune ligne =
invariant rompu (503), jamais un faux conflit. Epoch et compteur ne
débordent jamais (`next_epoch`, `next_attempt` ; epoch maximal :
`decryptage_epoch_exhausted`).

**Démonstration du fencing** (tests PostgreSQL) : ancien epoch, ancien
jeton, jeton d'un successeur avec l'epoch de l'ancien, bail expiré, instant
exact d'échéance, bail libéré, réservation bloquée : refus sans écriture
(xmin inchangé) ; l'ancien détenteur ne libère ni ne remet à zéro son
successeur ; une réservation terminale n'est jamais réactivée.

## 8. Horloges

`T_authorization` (runner BF2) inchangé. `T_lease_decision` =
`SELECT clock_timestamp()` exécuté dans une instruction DISTINCTE après le
`FOR UPDATE` de la réservation (adaptateur `_lease_clock`) ; toutes les
échéances sont calculées par PostgreSQL (`CAST(:at AS timestamptz) +
make_interval(secs => …)`). Ni `now()`, ni horloge Python, ni navigateur.
Mesuré : une reprise ou une déclaration d'échec qui attend le verrou de la
réservation (tenu par un tiers) au-delà de l'échéance est décidée APRÈS
l'attente (bail repris / déclaration refusée).

## 9. Capacité et confidentialité

`LeaseToken` (32 octets `secrets.token_bytes`) : `repr`/`str` masqués,
non hachable, `pickle`, `copy`, `deepcopy`, `dataclasses.asdict`,
`json.dumps` et `fastapi.encoders.jsonable_encoder` refusés ou sans le
secret ; stocké uniquement sous `SHA-256("ORYX-DECRYPTAGE-LEASE-TOKEN\0" ||
jeton)`. `DecryptageLease` (reservation_id, epoch, jeton, échéance
indicative, conversation, tour, ordinal) et `LeaseGranted` : immuables,
masqués, non sérialisables. Testé sur l'`AuthorizationOutcome` COMPLET
(repr, str, f-strings) et dans les journaux. Le jeton n'est jamais un
justificatif F2 : une déclaration d'échec exige une nouvelle autorisation
complète (session révoquée : `SESSION_INVALID` même avec le jeton).

## 10. Concurrence et COMMIT incertain

- Deux réservations F2 du même tour : sérialisées au rang `users` (attente
  observée), un seul bail.
- Gagnant non committé puis committé / annulé / d'intention divergente
  (connexion de test interne) : attente observée sur l'index unique, retour
  au SAVEPOINT, relecture par une nouvelle instruction (séquence d'instructions
  vérifiée), `ReservationInProgress` / `LeaseGranted` / `IDEMPOTENCY_CONFLICT`.
- COMMIT incertain (perte simulée après COMMIT) : `UNAVAILABLE`, aucune
  capacité ; le retry trouve la réservation durable (`ReservationInProgress`,
  jeton perdu), puis, à l'échéance, epoch 2 — jamais deux capacités sur le
  même epoch.

| Scénario (deux ordres quand applicable) | Résultat observé |
|---|---|
| B1 contre B1 (même tour ; échec déclaré contre retry) | attente au rang users ; un seul bail ; libération vue par le retry |
| writer historique contre writer historique (même / autre propriétaire) | attente sur l'index ; gagnant relu ; conflit sans réattribution |
| writer historique de conversation contre B1 | attente sur l'index ; même propriétaire : bail ; autre : `NOT_FOUND`, rien d'écrit |
| B1 contre writer historique de conversation | le writer attend ; même propriétaire : idempotent ; autre : conflit |
| `/decryptage` historique pendant une réservation suspendue | la route aboutit sans attendre ; bail rendu ; le retry bloque `legacy_collision` ; livraison jamais adoptée |
| `claim_delivery` historique non committé puis B1 | B1 attend la conversation ; blocage direct `legacy_collision` (epoch 0) |
| B1 contre mutation F2 de profil / portefeuille (2 × 2 ordres) | attente au rang users ; les deux `AUTHORIZED` |
| révocation après autorisation | la révocation attend le COMMIT ; puis `SESSION_INVALID`, rien d'écrit |
| révocation pendant l'attente au rang users | `SESSION_INVALID` à T_authorization, rien d'écrit |
| tiers tenant la réservation au-delà de l'échéance | décision après l'attente (bail repris / échec refusé) |

Chaque scénario exige les résultats métier exacts, l'attente observée dans
`pg_stat_activity` et `pg_stat_database.deadlocks` inchangé ; aucun délai de
verrou n'est pris pour une preuve d'absence d'interblocage, et aucune
garantie « deadlock-free » générale n'est affirmée. Profil de verrous mesuré
(sondes NOWAIT) : `users` FOR KEY SHARE accordé, FOR UPDATE refusé ;
réservation propre tenue FOR UPDATE jusqu'au COMMIT ; conversation
compatible FOR NO KEY UPDATE (FK KEY SHARE seulement) ; aucune livraison
verrouillée.

## 11. Collisions avec `/decryptage` historique

Aucune adoption : `assistant_delivery_id` n'est jamais écrit par B1 ; une
collision n'est constatée que par une ligne `assistant_deliveries` de
l'identité COMPLÈTE (propriétaire, conversation, surface, tour, ordinal)
lue après le verrou ; jamais déduite d'une erreur SQL. **Limite de B1** :
le writer historique ne connaît pas le verrou de réservation ; une livraison
peut apparaître après le dernier contrôle B1 (scénario mesuré ci-dessus) ;
la revalidation finale et `claim_delivery` relèvent de PR9-C.

## 12. Dormance

Aucun module applicatif n'importe les commandes ou modules B1 (contrôle
statique) ; le vrai `api:app` répond 404 sur `/api/v2/me*` et ne charge
aucun module F2, y compris avec des variables évoquant une activation B1 ;
`api.py` ne mentionne aucun nom B1 ; les deux modules purs n'ouvrent aucune
connexion et n'importent ni `core.db` ni réseau ; aucune route ajoutée.

## 13. Tests

| Fichier | Contenu |
|---|---|
| `tests/test_migration_0021_decryptage_turn_reservations.py` | chaîne, sha256 0001–0020, SQL offline, modèle, montée / descente exactes, 22 violations de CHECK, FK composite, unicités, downgrade refusé (en ligne / offline) |
| `tests/test_authz_f2_pr9b1_crypto.py` | vecteurs, trame, rotation, bornes, admission, jeton, capacité, sérialisations |
| `tests/test_authz_f2_pr9b1.py` | réservation, retry, intention, bail, expiration stricte, plafond, backoffs, fencing, débordement, collisions legacy, finalisé, F2, propriété, trousseau, panne, COMMIT incertain, ordre, verrous, secret |
| `tests/test_authz_f2_pr9b1_concurrency.py` | § 10 |
| `tests/test_authz_f2_pr9b1_boundary.py` | familles de clés, trousseau, sous-classes, adaptateurs, prédicats, interface historique, absence d'appelant |
| Extensions nominatives | BF1 (`test_authz_f2_boundary.py` : inventaire des 12 instructions, 2 SAVEPOINT localisés, verrous d'exécution), dormance (matrice, tête, variables), PR9-A (registre 16, graphe d'appels avec `_is_identity_collision`, signature du runner, présence de 0021), comparateur de métadonnées (révision 7), épinglages de tête 0020 → 0021 |

Aucun test supprimé ni affaibli ; les tests de 0020 ciblent désormais
explicitement `0020_position_revision` (comme ceux de 0019 lors de l'ajout
de 0020).

## 14. Exécution des tests

Voir la description de la PR (résultats locaux PostgreSQL 16.15 et 18,
contrôles GitHub `ci / postgres-16`, `ci / postgres-18`,
`ci / migrations-16`, `ci / migrations-18`, `ci / web`, `ci / required`).

## 15. Réservé à PR9-B2 / PR9-C

Orchestration Claude / EODHD, contexte pédagogique, ticker canonique
(`resolved_ticker`), routes HTTP et codecs, finalisation `reserved →
finalized` et lien de livraison, gestion des collisions découvertes pendant
`claim_delivery`, activation.

## 16. Réserves et points à valider

1. **Identifiant de révision** `0021_decryptage_reservations` (le nom
   autorisé dépasse `VARCHAR(32)`), fichier au nom autorisé.
2. **Classification fermée de l'échec** : le vocabulaire n'étant pas
   reproduit dans la demande, B1 retient `market_data_unavailable`,
   `generation_unavailable`, `generation_unusable` ; validée et non
   persistée (aucune colonne), sans effet sur la politique.
3. **Statut des refus de capacité** : `REVISION_CONFLICT` (epoch = révision
   du bail) pour état, epoch, jeton ou échéance périmés, de façon
   indiscernable ; `NOT_FOUND` pour une réservation absente ou d'autrui.
   Aucun statut nouveau dans le runner.
4. **Indépendance des clés** vérifiée par le constructeur du runner
   (Décrypter ≠ F2, ≠ portefeuille), en plus du type exact.
5. **Échéance du premier bail** : T_lease_decision est lu après la lecture
   verrouillée (ligne absente) et avant l'INSERT ; si l'INSERT attendait
   l'emplacement unique puis aboutissait (gagnant annulé), le bail serait
   plus court de la durée d'attente (≤ `lock_timeout` 5 s), jamais plus
   long, sans second détenteur. Deux transactions B1 d'un même compte ne
   peuvent pas produire cette attente (sérialisées au rang `users`) ; seule
   une écriture de test directe la produit.
6. **Clé de conversation** : comme en PR9-A, l'espace des clés est global ;
   une clé d'autrui rend `NOT_FOUND` alors qu'une clé libre est créée
   (existence observable, comportement historique).
7. `fingerprint_version` est contraint par `IN (…)` : une nouvelle version
   de canonicalisation exigera une migration.
8. Admission plus stricte que la route historique pour `conversation_key`
   (aucun blanc de bord, ≤ 200), alignée sur les commandes F2 et
   `TurnFinalization` de PR9-A ; les autres bornes sont nouvelles (la route
   historique n'en avait pas).
