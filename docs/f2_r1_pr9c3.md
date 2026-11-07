# R1 PR 9-C3 — Orchestration HTTP F2 Décrypter isolée (dormante)

## 1. Référentiel et commit de référence

Implémentation du freeze **PR9-C3 V1 + V1.1 + V1.2 + précisions de fermeture C3-F01 / C3-F02**, validé et
figé par le propriétaire. Aucune décision architecturale nouvelle n'a été prise ; les deux points
d'implémentation qui ont exigé un choix sont signalés au § 20.

`main` vérifié avant toute modification : `a3b8e8bf64c83cece06ed46b19e3e3995081d367` (merge de PR9-C2).
État de référence vérifié dans le code avant implémentation :

| Élément | Valeur vérifiée |
|---|---|
| Commandes F2 (`_adapters.REGISTRY`) | 20 |
| Opérations privées du pont ORM | 3 (`TurnFinalization`, `DeliveryAcknowledgement`, `CandidateFinalization`) |
| Contrats de capacités privées | 3 (`TransactionProbe`, `FinalizationReadPort`, `AckAdmissionPort`) |
| Bail B1 / attributions / backoffs | 360 s / 3 / 30 s puis 120 s |
| Budget B2 | 240 s (marge 5 s) |
| Replay et ACK C2 | implémentés (`ReadCanonicalDecryptageDelivery`, `AcknowledgeDecryptageDelivery`) |
| Route HTTP Décrypter F2 | aucune |
| Migrations | 21 (tête `0021_decryptage_reservations`) |

## 2. Périmètre exact

**Créé** : `core/authz_f2_http/decryptage.py` (configuration fermée `DecryptageHttpConfig`, orchestrateur
interne C3, capacité locale, trois routes), la suite PR9-C3 (trois fichiers de tests + un module de support),
cette documentation.

**Adapté** :

- `core/authz_f2_http/isolated.py` : paramètre facultatif `decryptage` de `build_isolated_personal_app` ;
- `core/authz_f2_http/routes.py` : `_announced_length` et `_read_body` acceptent une borne `limit=`
  keyword-only, **par défaut `MAX_BODY_BYTES` (4096) évalué à l'appel** — comportement des routes
  existantes strictement inchangé, une seule implémentation du framing ;
- `core/authz_f2_http/__init__.py` : docstring ;
- huit tests de frontière / dormance historiques, adaptés **nominativement** (§ 18).

**Inchangé** : `AuthorizedTransactionRunner`, les vingt commandes, les plans de verrouillage, les trois
opérations privées du pont, les trois contrats, B1, B2, C1-B, C2, les transactions cognitives, les routes
personnelles F2, les routes d'authentification F2, toutes les routes et le frontend legacy, `api.py`,
`web-v2/index.html`. Aucune migration, aucun nouveau mécanisme de persistance, aucun worker.

## 3. Composition HTTP isolée (C3-V11-01, C3-V11-06)

```python
build_isolated_personal_app(*, engine, keyring, allowed_origins, csrf_keyring, clock=None, fault_hook=None,
                            wall_clock=time.time, portfolio_keyring=None, decryptage=None)
```

- **`decryptage=None`** : application et runner **strictement identiques** au comportement historique —
  runner construit sans `decryptage_keyring`, aucun transport exigé, aucune route C3
  (`make_personal_app` ne peut jamais en ajouter). Vérifié par l'inventaire exact des dix routes historiques.
- **`decryptage=DecryptageHttpConfig(...)`** (type exact, sinon `PersonalHttpConfigError`) :
  1. le runner **unique** reçoit `decryptage_keyring` (indépendance des clés F2 et portefeuille : vérifiée
     par le runner, sans repli) ;
  2. indépendance des clés Décrypter / CSRF vérifiée par la composition ;
  3. `build_decryptage_router(deps, config)` construit **lui-même** `DecryptageGenerationOrchestrator` avec
     `deps.runner` — sa signature n'accepte ni orchestrateur ni runner externe ;
  4. exactement trois routes `POST` sont ajoutées ; contrôles HTTP / CSRF historiques réutilisés.

`DecryptageHttpConfig` (gelée, non sérialisable, `repr` sans contenu) : `decryptage_keyring`
(`DecryptageKeyring`, type exact), `eodhd` (`search`, `fundamentals`, `realtime`), `fmp` (`profile`,
`ratios`), `claude` (`create_message`), `model` (explicite : ASCII sans blanc, 1 à 128 caractères — borne
B2), `monotonic` (horloge monotone appelable, sondée finie ; budget C3 **et** budget B2), `price_recent_max_age`
(paramètre B2 existant, `None` par défaut : comportement B2 inchangé). Toute pièce manquante, `None`,
incomplète ou de mauvais type : refus. Rien n'est lu dans l'environnement ; aucune connexion, aucun fil, aucun
appel fournisseur à l'import ni à la construction (sonde en sous-processus, avec ou sans variables
évocatrices).

Exigences héritées et revérifiées : moteur PostgreSQL, isolation explicite autre que READ COMMITTED refusée,
origines HTTPS validées, trousseau CSRF indépendant, trousseau portefeuille si configuré.

## 4. Contrats des trois endpoints

| Route | Corps exact | Commande(s) | Succès |
|---|---|---|---|
| `POST /api/v2/me/decryptage/turns` | `conversation_key`, `client_turn_id`, `ticker_input`, `question`, `context`, `last_method_id` | `ReserveDecryptageTurn` puis B2 / C1-B / C2 selon B1 | 200 canonique, 202 |
| `POST /api/v2/me/decryptage/replay` | `reservation_id`, `conversation_key`, `assistant_turn_id` | `ReadCanonicalDecryptageDelivery` | 200 canonique |
| `POST /api/v2/me/decryptage/ack` | idem | `AcknowledgeDecryptageDelivery` | 200 `{"assistant_turn_id", "delivery_status": "delivered"}` |

`/turns` est une soumission initiale **ou une reprise active** : une nouvelle invocation de la même intention
peut obtenir une nouvelle attribution B1 après expiration du bail ou du backoff ; ce n'est jamais une
consultation passive. L'ACK n'est jamais appelé par `/turns` ni par `/replay` (vérifié statiquement et en base :
la livraison reste `pending`).

Restitution 200 (`/turns` et `/replay`) :
`{"assistant_turn_id":"…","delivery_status":"pending|delivered","payload":<octets JSONB>,"reservation_id":"…"}`.

## 5. Admission et sécurité F2

Ordre fermé (le premier échec répond ; aucun refus d'admission n'appelle le runner) :

1. méthode et chemin (`405` / `404`) ;
2. provenance de mutation personnelle : `Origin` obligatoire et exact, Host canonique autorisé,
   `Sec-Fetch-Site: same-origin` s'il est présent, Referer de même origine → `403 origin_refused` ;
3. en-tête `Cookie` (parseur strict PR 3) → `400` ;
4. CSRF `oryx-f2-csrf-v1` → `503` (trousseau / horloge) ou `403 csrf_invalid` ;
5. `X-Oryx-Generation-Id` unique, UUID canonique → `400` ;
6. aucune query → `400` ;
7. `Content-Type: application/json` [`; charset=utf-8`] → `415 invalid_request` ;
8. framing : `Transfer-Encoding`, `Content-Length` dupliqué, signé ou mal formé → `400` ; `Content-Length`
   valide au-delà de la borne → `413` ;
9. **flux reçu** borné (384 Kio pour `/turns`, 4096 octets pour `/replay` et `/ack`), jugé avant l'ajout de
   chaque fragment, sans se fier à `Content-Length` ; discordance, troncature, déconnexion → `400` ;
10. JSON UTF-8 strict (objet, clés exactes, aucun doublon, aucune constante, aucun nombre, UUID canoniques via
    `canonical_uuid`), commande typée construite et admise structurellement → `400` ;
11. cookie `__Host-oryx-active-<generation_id>` **exact** → `401 session_invalid` ;
12. `/turns` seulement : place locale acquise sans attente → `503 capacity_unavailable`.

Aucun `user_id`, état, jeton, HMAC, epoch ni principal n'est accepté dans le corps (clé supplémentaire :
`400`). La borne de 384 Kio couvre l'expansion des échappements JSON (`\uXXXX` : 6 octets par caractère) des
bornes **métier B1 inchangées** (question 16 Kio, contexte 32 Kio, total 48 Kio UTF-8), testée au maximum
exact. Toute réponse : `Cache-Control: no-store`, `Pragma: no-cache`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, aucun `Set-Cookie`, aucun CORS. Journaux : route, statut, code et motifs
fermés seulement (jamais corps, texte, cookie, jeton, HMAC, identifiants, SQL).

## 6. Machine d'état de l'orchestration `/turns`

```
admission ─▶ place acquise ─▶ started = monotonic() ─▶ soumission au fil de travail
  A  budget ≤ 0 ......................................▶ 503 (aucune réservation)
  B  runner.run(ReserveDecryptageTurn)
       statut ≠ AUTHORIZED ...........................▶ matrice B1
       principal ≠ type exact ou génération ≠ sélecteur ▶ 503 (alerte)
       identité retenue (user_id, session_id, generation_id)
       ReservationInProgress / RetryDeferred .........▶ 202
       ReservationBlocked ............................▶ 409 reservation_blocked
       ReservationFinalized ..........................▶ E (replay C2)
       LeaseGranted (bail du même tour, sinon 503 alerte)
  C  reste < 255 s ...................................▶ 503 (bail committé conservé, aucune déclaration)
     generator.generate(credential, commande originale, résultat B1)
       REFUSED / PROVIDER_FAILED .....................▶ matrice B2
       GENERATED (candidat du même propriétaire et du même bail, sinon 503 alerte)
  D  reste < 15 s ....................................▶ 503
     runner.run(FinalizeDecryptageCandidate)  — NOUVELLE autorisation
       statut ≠ AUTHORIZED ...........................▶ matrice C1-B (UNAVAILABLE : COMMIT incertain)
       principal ≠ identité B1 .......................▶ 503 (alerte)
       vue ...........................................▶ matrice C1-B
     DecryptageFinalized / AlreadyFinalized
  E  reste < 5 s (après C1-B) ........................▶ 503 prudent (finalisation durable conservée)
     runner.run(ReadCanonicalDecryptageDelivery)
       principal ≠ identité, vue ≠ type exact, identifiants ≠ attendus ▶ 503
       ─▶ 200 (enveloppe canonique)
```

Toute exception imprévue du fil : `503 temporarily_unavailable`, type d'exception seul journalisé. Aucune
transaction n'est ouverte par C3 ; chaque étape est une transaction du runner ; B2 n'en garde aucune pendant
un appel fournisseur (vérifié : 0 transaction F2 ouverte pendant EODHD et Claude).

## 7. Matrice HTTP fermée (C3-V11-03)

**B1** : `LeaseGranted` → B2 ; `ReservationInProgress` → `202 in_progress` ; `ReservationRetryDeferred` →
`202 retry_deferred` ; `ReservationBlocked` → `409 reservation_blocked` ; `ReservationFinalized` → replay ;
`SESSION_INVALID` → `401 session_invalid` ; `NOT_FOUND` → `404 not_found` ; `IDEMPOTENCY_CONFLICT` →
`409 idempotency_conflict` ; `REVISION_CONFLICT` → `409 revision_conflict` ; `UNAVAILABLE` → `503`.

**B2** (vocabulaire `dg.REFUSAL_REASONS` entièrement inventorié, égalité testée) :

| Résultat B2 | HTTP |
|---|---|
| `GENERATED` | finalisation |
| `instrument_not_found`, `instrument_ambiguous` | `422 instrument_unavailable` |
| `context_changed` + les cinq refus fermés de préparation | `409 context_conflict` |
| `session_invalid` | `401 session_invalid` |
| `reservation_not_found` | `404 not_found` |
| `intent_mismatch`, `lease_stale` | `409 revision_conflict` |
| `budget_exhausted`, `lease_expired`, `authorization_unavailable` | `503 temporarily_unavailable` |
| `owner_changed`, `lease_not_granted`, `lease_mismatch`, `lease_already_used` | `503` + alerte interne |
| `PROVIDER_FAILED` / `declared` | `503 provider_unavailable` |
| `PROVIDER_FAILED` / `declaration_refused`, `declaration_uncertain` | `503 temporarily_unavailable` |

C3 n'invente jamais une libération de bail et ne déclare jamais d'échec à la place de B2
(`RecordDecryptageAttemptFailure` n'apparaît pas dans le module, vérifié statiquement).

**C1-B** : `DecryptageFinalized`, `AlreadyFinalized` → replay ; `FinalizationContextChanged`,
`FinalizationPreparationRefused` → `409 context_conflict` ; `LegacyCollisionBlocked`, `ReservationBlocked` →
`409 reservation_blocked` ; `SESSION_INVALID` → `401` ; `NOT_FOUND` → `404` ; `REVISION_CONFLICT` →
`409 revision_conflict` ; `IDEMPOTENCY_CONFLICT` → `409 idempotency_conflict` ; `UNAVAILABLE` → `503`
(COMMIT potentiellement effectué, jamais un rollback prétendu).

**C2 (replay et ACK)** : `AUTHORIZED` + vue exacte → `200` ; `SESSION_INVALID` → `401` ; `NOT_FOUND` → `404` ;
`REVISION_CONFLICT` → `409 revision_conflict` ; `UNAVAILABLE` → `503`. Tout statut non inventorié
(`IDEMPOTENCY_CONFLICT` compris) ou toute vue incorrecte : `503`.

## 8. Contrat `202` (C3-V12-04)

```json
{"lease_expires_at":"2026-11-07T11:30:00.000000Z","reservation_id":"…","status":"in_progress"}
{"reservation_id":"…","retry_not_before_at":"2026-11-07T11:32:00.000000Z","status":"retry_deferred"}
```

UTC, RFC 3339, exactement six chiffres de microsecondes, suffixe `Z` (`rfc3339_utc`, formatage explicite,
année sur quatre chiffres) ; aucune clé supplémentaire ; aucun secret, jeton ni HMAC ; aucun `Retry-After` ;
aucun polling automatique. Un `202` ne promet pas qu'un fil est encore actif.

## 9. Budget global de 270 s (C3-V12-01)

Horloge monotone injectée (`DecryptageHttpConfig.monotonic`) ; début : après admission et attribution de la
place, **avant** la soumission au fil. Gardes coopératives : budget épuisé avant B1 → aucune réservation ;
< 255 s avant B2 → `503` (bail committé conservé, ni déclaration ni libération : B1 gouverne la reprise) ;
< 15 s avant C1-B → `503` ; < 5 s après C1-B → `503` prudent, la finalisation durable reste acquise et la même
intention la restitue ensuite. Horloge défaillante (exception, NaN, infini) : budget réputé épuisé.

**270 s est un objectif coopératif, pas un SLA** : les seuils ne prouvent pas qu'une transaction terminera
dans le temps imparti ; C3 n'interrompt jamais C1-B ni son COMMIT (testé : un COMMIT « dépassant » le budget est
mené à terme). Budget B2 (240 s) et bail B1 (360 s) inchangés. Une déconnexion ne prouve aucun échec : aucun
retry fournisseur, aucun COMMIT présumé annulé ; la reprise utilise la même intention.

## 10. Propriété des deux places d'exécution (C3-V12-05, C3-F01)

Deux orchestrations `/turns` simultanées par **instance** (`MAX_CONCURRENT_TURNS = 2`). Ce quota est
**local** : ce n'est pas un quota distribué ; la prévention durable des attributions concurrentes reste
assurée par B1 (testé avec deux compositions indépendantes sur la même base).

Machine d'état d'une place (`_TurnSlot`, toutes les transitions sous le verrou de `_TurnCapacity`) :

| État | Transition | Qui rend la place |
|---|---|---|
| `acquired` | `start()` par le fil → `started` | — |
| `acquired` | `abandon()` par la requête (soumission annulée avant démarrage) → `abandoned` | la requête |
| `started` | `finish()` dans le `finally` du fil → `finished` | le fil |

`abandon()` après `start()` et `start()` après `abandon()` sont sans effet : exactement un propriétaire, une
seule libération. L'annulation de la coroutine ou la déconnexion ne libère jamais une place détenue par un fil
démarré. L'acquisition est non bloquante (`503 capacity_unavailable` avant tout appel B1). `/replay` et `/ack`
ne consomment aucune place. La propriété ne repose pas sur `run_in_threadpool` : elle est garantie par la
machine d'état, testée sur les deux issues de la course annulation / démarrage (et par 2 × 2000 courses réelles
entre fils).

## 11. Garanties et limites des reprises

| Incident | Comportement |
|---|---|
| perte avant B1 | la même intention crée la réservation |
| perte après COMMIT B1 | la même intention retrouve la réservation ; `202 in_progress` tant que le bail est actif ; aucun second bail |
| perte pendant B2 | le fil poursuit ou s'interrompt ; la reprise repose exclusivement sur B1 (expiration → nouvelle attribution, epoch 2) |
| perte après COMMIT C1-B | `ReservationFinalized` puis replay C2, aucun appel fournisseur |
| perte pendant replay | nouvelle lecture C2, aucune écriture |
| perte après COMMIT ACK | nouvel ACK explicite, idempotent, aucune seconde capture |
| COMMIT B1 / C1-B / ACK incertain | `503` ; `UNAVAILABLE` n'est jamais une preuve de rollback ; aucun retry interne ; jamais un nouveau `client_turn_id` |

**C3 ne fournit pas de mécanisme durable d'exécution asynchrone** : un fil interrompu par une panne de
processus n'est pas repris automatiquement ; seule une nouvelle invocation explicite de `/turns` (après
l'échéance du bail) relance la génération.

## 12. Continuité du principal (C3-V12-02, C3-F02)

Identité de référence : `(user_id, session_id, generation_id)` du premier `AuthorizedPrincipal` B1, dont la
génération doit égaler le sélecteur HTTP. Chaque résultat `AUTHORIZED` consommé directement par C3 (C1-B, C2)
doit porter un `AuthorizedPrincipal` de type exact et la même identité ; sinon `503`, aucun contenu. B2 reçoit
le même justificatif et le même runner et conserve ses propres contrôles de propriétaire (non modifié). Le
runner réautorise chaque commande (six autorisations F2 complètes pour un tour nominal, vérifié). Entre deux
requêtes, une reprise sous une **nouvelle session du même propriétaire** est admise (B1 lie l'intention au
propriétaire) ; un autre propriétaire obtient `404`. Une révocation pendant B2 fait refuser C1-B (`401`) : le
candidat ne quitte jamais le serveur. Aucune identité n'est corrigée par une valeur du navigateur.

## 13. Rotation des clés HMAC B1 (C3-V12-03)

Politique B1 inchangée : une réservation conserve sa version de clé ; une nouvelle réservation utilise la clé
courante ; aucune substitution, aucun recalcul avec une autre clé. La composition accepte un
`DecryptageKeyring` contenant la clé courante et les anciennes versions. Testé : K1 → K2 avec K1 conservée
(reprise d'un tour K1 finalisé : replay, aucun appel Claude ; nouveau tour signé K2) ; K1 supprimée (`503`,
aucun nouveau bail, aucune substitution K2, état inchangé à l'octet près) ; replay C2 toujours disponible avec
les identifiants connus, sans recalcul du HMAC B1.

**Dépendance opérationnelle** : les anciennes clés doivent rester dans le trousseau tant que la récupération
des réservations signées avec elles est promise. Aucune purge automatique de réservation ni de clé.

## 14. Sérialisation JSONB (C3-V11-05)

`canonical_body` insère `CanonicalDecryptageDelivery.payload_json` (octets exacts de
`response_payload::text`, validés par C2) **tel quel** à la place de la valeur `payload` d'une enveloppe dont
les autres valeurs sont des UUID validés et un statut fermé. Le résultat est relu strictement (nombres en
`Decimal`, doublons et constantes refusés) avant émission, sans jamais être resérialisé ; toute anomalie :
`503`, jamais une réparation. Réponse `Response(content=bytes, media_type="application/json")`. Tests :
décimaux, notation JSONB (`1e-07` → `0.0000001`), grands entiers, prix nul et zéro, Unicode, échappements,
` `, emoji, octets du sous-objet conservés, identifiants cohérents, aucune double sérialisation ;
jamais de reconstruction depuis le candidat B2.

## 15. ACK post-affichage et idempotence

L'ACK confirme une **présentation déclarée** par l'interface first-party, jamais une preuve de lecture ni de
compréhension ; il n'est déclenché que par `POST /ack`. Premier ACK : nouvelle autorisation F2, preuve C2,
capture cognitive existante, `pending → delivered`, COMMIT unique du runner. ACK répété : aucune seconde
capture ni progression, réponse `delivered`. Deux ACK concurrents (deux sessions du même propriétaire) : une
seule capture. COMMIT incertain : `503`, un nouvel ACK explicite retrouve l'état durable. La vue ACK est la
vue historique du pont, dont le type exact est vérifié par l'adaptateur SQL ; la couche HTTP, qui ne peut pas
nommer le pont (frontière d'import figée), la contrôle structurellement (§ 20).

## 16. Coexistence legacy

Les writers legacy (`/decryptage`, ACK historique) sont inchangés. Une livraison legacy du même tour avant B1
bloque la réservation (`409 reservation_blocked`) ; apparue pendant B2, elle est prouvée par C1-B
(`LegacyCollisionBlocked`, `409`). Aucune livraison legacy n'est adoptée. La coordination définitive avec les
writers legacy reste hors périmètre.

## 17. Dormance de production

`api:app` ne contient aucune route C3 et ne charge aucun module `authz` / `auth_f2`, quelles que soient les
variables d'environnement (sous-processus, avec et sans variables d'activation plausibles ; six chemins C3 →
`404`). `api.py` et `web-v2/index.html` sont inchangés et ne mentionnent ni C3 ni `/api/v2/`. Aucun fichier de
production hors du paquet isolé ne nomme C3. Aucune migration (21), aucune commande nouvelle (20), aucun plan
nouveau, aucune opération ni capacité nouvelle, aucune dépendance de production nouvelle.

## 18. Adaptations nominatives des tests historiques

Aucune assertion historique n'est supprimée : chaque interdiction reste vérifiée pour tous les autres
fichiers ; seuls les inventaires devenus légitimement obsolètes sont étendus nominativement, avec une
assertion fermée sur le nouveau contrat.

| Test | Adaptation |
|---|---|
| `test_authz_f2_dormancy::IMPORT_MATRIX` | + `decryptage.py` (imports exacts) ; `isolated.py` + `core.authz_f2_http.decryptage` ; sonde d'import + `decryptage` |
| `test_f2_dormancy::F2_REFERENCE_MATRIX` | + `decryptage.py` (mêmes primitives F2 que le portefeuille) |
| `test_authz_f2_pr9a_boundary` (registre) | « decryptage » admis dans `decryptage.py` / `isolated.py` seulement, qui ne nomment jamais le pont |
| `test_authz_f2_pr9b1_boundary` | `decryptage.py` : exactement `ReserveDecryptageTurn`, `DecryptageKeyring`, `LeaseGranted` (jamais `RecordDecryptageAttemptFailure`) |
| `test_authz_f2_pr9b2_boundary` | marqueurs B2 exacts de `decryptage.py` et `isolated.py` ; jamais `decryptage_providers` |
| `test_authz_f2_pr9c1a_boundary` | `FinalizeDecryptageCandidate` nommé aussi par `decryptage.py` |
| `test_authz_f2_pr9c1b_boundary` | `decryptage.py` ne nomme que la commande (aucune opération, capacité ni vue interne) ; `isolated.py` aucune |
| `test_authz_f2_pr9c2_boundary` | `decryptage.py` nomme les deux commandes et la vue, jamais `AckAdmissionPort` ; `isolated.py` aucune |

## 19. Résultats des tests

_RESULTATS_

## 20. Points d'implémentation signalés à l'audit

1. **Vue ACK** : le freeze demande une « vue métier du type exact attendu ». Le type
   (`DeliveryAcknowledged`) n'existe que dans le pont ORM privé, que la couche HTTP ne peut pas importer
   (frontière figée par PR9-A / C1-A). L'exactitude du type est donc garantie par l'adaptateur SQL
   (`type(outcome) is not bridge.DeliveryAcknowledged` → `DecryptageUnavailable`), et C3 vérifie en plus une
   forme structurelle fermée (dataclass gelée nommée `DeliveryAcknowledged`, champs exacts, livraison
   demandée, statut `delivered`). Aucune frontière n'a été modifiée pour cela.
2. **Bornes du framing** : plutôt que dupliquer le code de framing, `routes._announced_length` /
   `routes._read_body` reçoivent une borne keyword-only facultative (défaut inchangé, évalué à l'appel).

## 21. Risques restants avant activation publique

- pas d'exécution asynchrone durable : un fil perdu (crash) n'est relancé que par une nouvelle invocation
  explicite après l'échéance du bail ;
- quota de deux places **local** à l'instance (non distribué) ;
- l'ACK HTTP dépend de la future intégration first-party pour garantir que la déclaration d'affichage
  correspond à une présentation réelle ;
- coordination définitive avec les writers legacy, connexion au frontend et ouverture aux utilisateurs :
  chantier ultérieur ;
- conservation opérationnelle des anciennes clés Décrypter (§ 13) ;
- 270 s coopératif, non garanti ; dépendance à `statement_timeout` / `lock_timeout` du runner pour borner C1-B ;
- seuil de fraîcheur du cours B2 (`price_recent_max_age`) toujours à arbitrer (par défaut aucun cours n'est
  présenté comme actuel) ;
- transports réels (`core/decryptage_providers.py`) non exercés contre les fournisseurs dans cette PR.
