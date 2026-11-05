# R1 PR 9-C2 — Replay canonique et ACK F2 Décrypter (dormants)

## 1. Référentiel

Implémentation du freeze **PR9-C2 V1.2 + addendum V1.2.1 et précisions de l'audit de fermeture**.
C2 permet à un principal F2 autorisé :

- **A. Replay** : récupérer le contenu public canonique d'une livraison déjà finalisée par C1-B, sans
  regénération, sans progression, sans écriture ;
- **B. ACK** : confirmer la présentation de cette livraison ; le moteur cognitif EXISTANT capture la
  réponse de façon atomique et idempotente.

C2 reste intégralement **dormant** : aucune route HTTP, aucune activation, aucun appel depuis `api.py`. Le
déclenchement réel de l'ACK côté interface et la coordination définitive avec les writers legacy relèvent
de PR9-C3 ou de l'activation ultérieure.

## 2. Commit de référence

`main` vérifié avant toute modification : `5eff2a85f41d136d25feda9734b7c44c47df221e` (PR9-C1-B et son
correctif d'audit P1). État de référence vérifié dans le code : 18 commandes, 3 opérations privées du pont
(`TurnFinalization`, `DeliveryAcknowledgement`, `CandidateFinalization`), 2 contrats de capacités
(`TransactionProbe`, `FinalizationReadPort`), aucune commande de production n'atteignant l'ACK privé,
aucune route F2 Décrypter.

## 3. Inventaires après C2

| Élément | Avant | Après |
|---|---|---|
| Commandes F2 de production | 18 | **20** (`ReadCanonicalDecryptageDelivery`, `AcknowledgeDecryptageDelivery`) |
| Opérations privées du pont | 3 | **3** (inchangées) |
| Contrats de capacités scellées | 2 | **3** (`AckAdmissionPort`) |
| Plans de verrouillage | 4 | 4 (aucun nouveau) |
| Migrations | 21 (`0021`) | 21 (aucune) |
| Routes HTTP | — | aucune nouvelle |

| Commande | Plan | Verrou préparatoire | Adaptateur | Trousseau |
|---|---|---|---|---|
| `ReadCanonicalDecryptageDelivery` | `READ` | `_no_business_lock` | `_read_canonical_delivery` | **aucun** |
| `AcknowledgeDecryptageDelivery` | `DECRYPTAGE_MUTATION` | `_no_business_lock` | `_acknowledge_delivery` | **aucun** |

Champs EXACTS des deux commandes : `reservation_id: uuid.UUID`, `conversation_key: str`,
`assistant_turn_id: uuid.UUID`. Admission structurelle complète à la construction : UUID de types exacts
(jamais une sous-classe ni une chaîne), conversation au validateur B1 existant
(`decryptage_fingerprint.admit_conversation_key` : 1 à 200 caractères, sans blanc de bord, UTF-8 valide),
aucune normalisation. Aucune commande ne porte de `user_id`, jeton, clé, candidat, données de génération,
payload, statut imposé, décision cognitive, Session, connexion, SQL ni rappel. Le propriétaire est
exclusivement `AuthorizedContext.principal.user_id`.

**Aucun trousseau** : C2 ne recalcule jamais l'empreinte HMAC B1 (il ne possède ni les entrées de
génération ni le jeton). Les deux commandes ne sont donc pas `decryptage_keyed` ; le runner les exécute
avec ou sans trousseau Décrypter. La politique des commandes B1 / B2 / C1-B est inchangée.

## 4. Preuve de provenance terminale C1-B

Commune au replay et à l'ACK (`_adapters._canonical_proof`), distincte de `_recognize_finalized` (qui
reconnaît un candidat B2 complet). UNE instruction SQL (`_CANONICAL_DELIVERY`), propriétaire-centrée,
point d'entrée `decryptage_turn_reservations`, sans verrou ni écriture.

**Ordre de décision figé** : propriété → état B1 → identité demandée → intégrité persistée → lecture ou
mutation.

| Étape | Contrôle | Refus |
|---|---|---|
| Propriété | réservation `id` ET `user_id` = principal (la réservation d'autrui n'est jamais lue : seule sa propriété est projetée en booléen) | `NOT_FOUND` (absente et étrangère indiscernables) |
| État B1 | `state = finalized` | `REVISION_CONFLICT` (`reserved`, `blocked`) |
| Identité demandée | `conversation_key` et `assistant_turn_id` = conversation et lien B1 de la réservation | `REVISION_CONFLICT` |
| Réservation terminale | `_reservation()` (structure, bornes epoch / compteur, versions) + `finalized_at`, `resolved_ticker` présents ; `lease_token_hash`, `lease_expires_at`, `retry_not_before_at`, `blocked_reason` NULL ; epoch et compteur ≥ 1 ; `fingerprint_version` connue, `fingerprint_key_version` et `request_fingerprint` au format B1 (jamais recalculés) | `UNAVAILABLE` |
| Livraison liée | jointe seulement si la réservation est propre, finalisée, de la conversation demandée et liée à la livraison demandée, ET si la livraison appartient au principal ; identité (`conversation_key`, surface `decryptage`, `source_user_turn_id`, ordinal 1), version de schéma connue, statut `pending` / `delivered` | `UNAVAILABLE` (manquante, d'autrui, incohérente) |
| Provenance V2 | `private_metadata.cognitive_runtime_version` = chaîne `decryptage-cognitive-runtime-v2` ; un seul lien, `capture_version` V2 ; couple `pending + awaiting_delivery` ou `delivered + captured` ; `response_action` NULL en attente, du vocabulaire fermé une fois capturée | `UNAVAILABLE` |
| Provenance transitive | cinq events du lien, event parent de la SupportTrace, AnalysisSessions de la livraison et des events : présents et du principal (seulement COMPTÉS dans l'instruction, jamais lus) — même contrôle que `bridge._require_owned_provenance` | `UNAVAILABLE` |
| Payload public | voir §6 | `UNAVAILABLE` |

Le SHA historique de livraison (`request_fingerprint` de `assistant_deliveries`) et le HMAC B1 sont deux
identités différentes : ils ne sont jamais comparés. Une livraison legacy n'est jamais adoptée, même de
même identité logique : sans lien B1 elle est `REVISION_CONFLICT`, artificiellement liée elle échoue sur le
runtime absent (`UNAVAILABLE`).

## 5. Capacités scellées

| Contrat | Implémentation (`_adapters`) | Construite par | Opération |
|---|---|---|---|
| `TransactionProbe` | `_TransactionProbe` | `_transaction_probe` (bancs d'essai) | `TurnFinalization`, `DeliveryAcknowledgement` (chemin historique PR9-A) |
| `FinalizationReadPort` | `_FinalizationReadPort` | `_finalize_candidate` | `CandidateFinalization` |
| `AckAdmissionPort` (étend `TransactionProbe`) | `_AckAdmissionPort` | `_acknowledge_delivery` seulement | `DeliveryAcknowledgement` (chemin C2) |

`_bridge_contracts.py` reste PUR (aucun import, aucun SQL, aucune Session, aucun réseau). Une seule
implémentation par contrat, définie dans `_adapters` ; une classe qui relèverait de deux contrats dérivés
est refusée. `AckAdmissionPort` n'ajoute qu'UNE méthode, `require_delivery(...)` (paramètres nommés :
identité complète de la livraison verrouillée), utilisable UNE fois, qui compare la livraison effectivement
verrouillée par le pont à l'attestation : `CapabilityRefused` sinon. L'implémentation est liée à la
connexion exacte, à la transaction externe active constatée à la construction, au principal, à la
réservation B1 verrouillée, à sa conversation, à la livraison admise (et son tour source), et à la durée
d'UNE opération ACK (`_close()` dans un `finally`, en succès comme en échec). Non sérialisable, non
copiable, immuable ; `repr` sans contenu ; aucune méthode d'exécution SQL ni de rappel ; aucun booléen
(`is_f2`, `strict_mode`…) ne peut la remplacer.

**Sélection fermée** (`bridge.CAPABILITIES`, tuple de contrats EXACTS par opération) :

```text
TurnFinalization        -> (TransactionProbe,)
DeliveryAcknowledgement -> (TransactionProbe, AckAdmissionPort)
CandidateFinalization   -> (FinalizationReadPort,)
```

`_execute` exige `type(probe) in {implementation(k) for k in CAPABILITIES[type(operation)]}` : jamais un
`isinstance`, jamais une sous-classe, jamais un objet qui imite l'interface. Aucune opération nouvelle.

## 6. Replay et représentation JSONB

Source unique : `assistant_deliveries.response_payload::text`, c'est-à-dire le **JSONB persisté**, jamais
les octets JSON envoyés à l'écriture, jamais le candidat B2, Claude ni les données financières.

Validation (`_canonical_payload`) sans conversion prématurée : `json.loads` avec `parse_float=Decimal`,
`parse_int=Decimal`, constantes non standard refusées, clés dupliquées refusées.

- exactement les huit champs publics C1-B : `success`, `ticker`, `name`, `method_used`, `analysis`,
  `price`, `currency`, `disclaimer` (aucune clé supplémentaire ni réservée) ;
- `success` exactement `true` ; `ticker` résolu admissible ET égal au `resolved_ticker` B1 ; `name` chaîne
  non vide ; `method_used` chaîne ou `null` ; `currency` code ISO à trois majuscules (domaine de
  `FinancialSnapshot`) ; `disclaimer` égal à la constante C1-B ;
- `analysis` : chaîne non vide, sans `ORYX_STEP` ni `<!--` ni NUL, au plus `RESPONSE_MAX_BYTES` = 64 Kio
  UTF-8 (mêmes règles que `validate_response` ; aucune borne nouvelle) ;
  `visible_content_fingerprint(analysis)` (fonction historique) égale au SHA enregistré ;
- `price` : `null` ou nombre JSON lu en `Decimal`, fini, et **exactement** la représentation décimale la
  plus courte d'un binary64 fini (`Decimal(repr(float(d))) == d`) — c'est le domaine écrit par C1-B
  (`float` fini sérialisé par `json`, conservé par le `numeric` JSONB). Booléens, chaînes, tableaux,
  dépassements et décimaux non représentables refusés ; aucun arrondi, aucune troncature, aucun
  remplacement.

Résultat : `CanonicalDecryptageDelivery(reservation_id, assistant_turn_id, delivery_status, payload_json)`,
dataclass figée validée par types exacts ; `payload_json` = **octets UTF-8 du texte JSONB validé, sans
seconde sérialisation**. JSONB ne conserve pas les octets d'origine (ordre des clés, espaces, notation des
nombres : `1e-07` devient `0.0000001`, `1e22` devient un entier) : C2 garantit la fidélité des **valeurs
persistées**, et deux replays d'une même livraison non modifiée rendent les mêmes octets. Un ACK concurrent
peut faire passer `delivery_status` de `pending` à `delivered` sans changer `payload_json`. Jamais de
`private_metadata`, d'empreinte, ni de donnée cognitive dans la vue ; `repr` / `str` sans contenu.

## 7. ACK : raccordement historique / C2 et frontières SQL / ORM

Ordre (dans la transaction externe du runner, UN seul COMMIT) :

1. autorisation F2 complète ; 2. `users FOR NO KEY UPDATE` ; 3. session et génération `FOR SHARE` ;
4. `T_authorization` ; 5. réservation propre `FOR UPDATE` (`_LOCK_RESERVATION` : absente ou d'autrui →
`NOT_FOUND`, rien n'est verrouillé chez autrui) ; 6. preuve terminale (`_canonical_proof`, sous ce
verrou) ; 7. attestation `_AckAdmissionPort` ; 8. pont, opération `DeliveryAcknowledgement` INCHANGÉE ;
9. `lock_owned_delivery` (id ET user_id ET conversation dans le `FOR UPDATE`) ; 10. `require_delivery`,
runtime V2, lien V2 **verrouillé** (`FOR UPDATE`, ordre livraison → lien, celui de la capture), couple
statut / lien, AVANT `_require_owned_provenance` et `capture_delivered_response` ; 11. capture
historique ; 12. `pending → delivered` ; 13. capture V2 exigée par le pont, puis l'adaptateur vérifie que
`DeliveryAcknowledged` (type exact réutilisé) porte la livraison admise, `delivered` et une
`response_action` V2 non nulle ; COMMIT unique par le runner.

- Les adaptateurs restent propriétaires du SQL textuel ; le pont reste propriétaire des services ORM ; ni
  l'un ni l'autre ne possède, committe ni annule la transaction du runner.
- Chemin historique (`TransactionProbe`) : comportements V1 / V2 / legacy antérieurs conservés (livraison
  absente → `ABSENT` ; legacy → aucune capture, `response_action` `None`).
- Chemin C2 (`AckAdmissionPort`) : une livraison introuvable sous verrou est `AckProvenanceIncoherent`
  (503, jamais un 404) ; une attestation absente, fausse, d'une autre connexion, transaction, principal,
  réservation, conversation ou livraison, close ou déjà consommée est refusée AVANT la provenance et la
  capture ; **jamais de repli vers le chemin historique** (l'adaptateur ne construit que l'attestation ;
  le type exact choisit le chemin).
- Pas d'`AlreadyAcknowledged` : un ACK rejoué rend le même `DeliveryAcknowledged`.

## 8. Statuts et refus

| Situation | Résultat |
|---|---|
| Session F2 invalide (révoquée, expirée) | `SESSION_INVALID` |
| Réservation inexistante ou étrangère | `NOT_FOUND` |
| Réservation propriétaire non finalisée (`reserved`, `blocked`) | `REVISION_CONFLICT` |
| Conversation ou UUID demandé différent du lien B1 (dont livraison d'autrui ou legacy non liée) | `REVISION_CONFLICT` |
| Réservation finalisée avec livraison liée manquante ou d'autrui | `UNAVAILABLE` |
| Livraison persistée incohérente (identité, schéma, statut) | `UNAVAILABLE` |
| Runtime V2 absent, V1 ou non textuel | `UNAVAILABLE` |
| Lien cognitif manquant, version divergente, couple interdit | `UNAVAILABLE` |
| Provenance transitive cassée ou étrangère | `UNAVAILABLE` |
| Payload incohérent | `UNAVAILABLE` |
| Attestation privée invalide | `UNAVAILABLE` |
| Isolation autre que READ COMMITTED (ACK) | `UNAVAILABLE` |
| COMMIT refusé ou incertain | `UNAVAILABLE` |
| Replay ou ACK valide | `AUTHORIZED` |

Les refus ne portent aucune valeur ni principal ; les journaux ne contiennent que le nom de la commande, le
statut et un nom d'alerte fermé (`decryptage_reservation_incoherent`, `decryptage_delivery_incoherent`,
`decryptage_cognitive_link_incoherent`, `decryptage_provenance_incoherent`,
`decryptage_payload_incoherent`, `decryptage_ack_incoherent`, `decryptage_bridge_capability_refused`,
`decryptage_bridge_ack_provenance_incoherent`…) : jamais de contenu, d'empreinte, de secret,
d'identifiant cognitif ni de détail SQL.

## 9. Protocole de verrous

| Acteur | Ordre |
|---|---|
| Replay C2 (`READ`) | `users FOR KEY SHARE` → session / génération `FOR SHARE` → UNE lecture sans verrou métier |
| ACK C2 | `users FOR NO KEY UPDATE` → session / génération → réservation `FOR UPDATE` → (preuve sans verrou) → livraison `FOR UPDATE` → lien `FOR UPDATE` → AnalysisSession → events (capture historique inchangée) |
| ACK historique (`/api/runtime/assistant-deliveries/{id}/ack`) | livraison `FOR UPDATE` → lien → AnalysisSession → events |
| Finalisation C1-B | `users FOR NO KEY UPDATE` → … → réservation `FOR UPDATE` → … |

Deux ACK C2 du même utilisateur, et un ACK C2 contre une finalisation C1-B, sont sérialisés au rang
`users` (avant tout verrou métier). ACK C2 et ACK historique se rencontrent sur la livraison, dans le même
ordre (livraison → lien) : aucun cycle. Le replay n'attend aucun verrou d'ACK (`FOR KEY SHARE` est
compatible avec `FOR NO KEY UPDATE`) et lit l'état committé. Deux utilisateurs ne sont jamais sérialisés.

## 10. Idempotence et récupération après COMMIT incertain

- Premier ACK : `pending + awaiting_delivery → delivered + captured`, capture et statut dans la même
  transaction.
- ACK rejoué : `delivered + captured` → no-op (aucune capture, aucun event, aucune SupportTrace, xmin
  inchangés).
- Échec avant capture, après capture et avant `delivered`, ou après `delivered` et avant COMMIT : ROLLBACK
  intégral.
- COMMIT refusé ou incertain : `UNAVAILABLE`, jamais une réussite présumée, aucun retry interne ; une
  nouvelle invocation explicitement autorisée retrouve l'état persisté (no-op si le COMMIT avait eu lieu,
  première capture sinon), jamais une seconde capture.
- Aucune opération C2 n'appelle la progression pédagogique d'entrée de C1-B ; `stale_delivery` et toutes
  les règles de causalité cognitive V2 sont celles de `capture_delivered_response`, inchangé.

**`delivered` signifie confirmation de l'insertion du contenu dans l'interface first-party, jamais une
preuve de lecture ni de compréhension.**

## 11. Fichiers modifiés

| Fichier | Modification |
|---|---|
| `core/authz_f2/commands.py` | deux commandes, `CanonicalDecryptageDelivery`, `CANONICAL_DELIVERY_STATUSES` |
| `core/authz_f2/_bridge_contracts.py` | contrat `AckAdmissionPort`, `CONTRACTS`, scellement à trois contrats |
| `core/authz_f2/_adapters.py` | `_CANONICAL_DELIVERY`, `_canonical_proof`, validation du payload, `_AckAdmissionPort`, `_read_canonical_delivery`, `_acknowledge_delivery`, registre (20) ; imports de deux modules PURS (`decryptage_candidate`, `decryptage_market_data`) pour les constantes C1-B |
| `core/authz_f2/_decryptage_orm_bridge.py` | `CAPABILITIES` en tuples fermés, `_admitted_capabilities`, chemin attesté de `_acknowledge`, `_require_attested_canonical`, `AckProvenanceIncoherent`, constantes V2 et empreinte visible exposées aux adaptateurs |
| `tests/…pr9c2*` | suites fonctionnelle, concurrence, frontière et support |
| tests historiques | adaptations NOMINATIVES (§13) |

Aucune modification de `api.py`, du frontend, de `core/assistant_delivery.py`,
`core/decryptage_cognitive_runtime.py`, `core/decryptage_progress.py`, des règles B1 / B2 / C1-B, des
migrations ni du système d'observation.

## 12. Tests ajoutés (PostgreSQL 16 et 18)

- `tests/test_authz_f2_pr9c2.py` : replay (pending exact, delivered, octets stables, une instruction,
  aucune écriture / verrou / fournisseur, sans trousseau, vue immuable) ; matrice propriété / état /
  identité / intégrité pour le replay ET l'ACK (absente, étrangère sans verrou ni attente, livraison
  d'autrui, conversation, UUID, legacy non liée, `reserved`, `blocked`, livraison manquante, liée à
  autrui, legacy artificiellement liée, runtime absent / V1 / non textuel, lien absent / V1,
  `pending + captured`, `delivered + awaiting_delivery`, AnalysisSession d'autrui ou absente, ticker
  absent, autre tour, schéma inconnu, propriétaire divergent, provenance transitive et parent de
  SupportTrace d'autrui) ; JSONB (prix null, décimaux, zéro, négatif, plus petit sous-normal, plus grand
  fini, notations réécrites par JSONB, hors domaine, mauvais types, Unicode, accents, échappements,
  empreinte, marqueur, champs manquants / supplémentaires / réservés, borne de 64 Kio conservée, absence
  de seconde sérialisation) ; ACK (première capture, rejeu no-op, sans trousseau, `stale_delivery`,
  chemin historique PR9-A, attestation hors contrat refusée avant toute capture dans chaque cas,
  revérification V2 sur les objets verrouillés, scellement, aucun repli, contrôle du résultat) ;
  atomicité (échecs à chaque phase, COMMIT refusé sans retry, COMMIT incertain puis ACK répété, session
  révoquée / expirée, isolation, transaction perdue, aucune fuite dans les journaux) ; unités de la preuve
  terminale (états interdits par les CHECK).
- `tests/test_authz_f2_pr9c2_concurrency.py` : deux ACK C2, ACK C2 contre ACK historique (deux ordres),
  ACK C2 contre finalisation C1-B (deux ordres), replay pendant un ACK (C2 et historique), deux
  utilisateurs, ordre réel des verrous (journal des instructions), profil des verrous détenus (sondes
  NOWAIT) ; `pg_stat_database.deadlocks` inchangé.
- `tests/test_authz_f2_pr9c2_boundary.py` : inventaires (20 / 3 / 3), champs et admission des commandes,
  aucun trousseau ni HMAC, contrats purs et scellés, sélection fermée sans `isinstance`, construction
  unique de l'attestation fermée dans un `finally`, ordre de l'adaptateur et de la preuve, absence de
  resérialisation, constantes recopiées égales aux services, vérifications du pont avant provenance et
  capture, aucun fournisseur, dormance.

## 13. Adaptations nominatives des tests historiques

Chaque adaptation est rendue nécessaire par C2 et remplacée par une assertion au moins équivalente :

| Test | Avant | Après |
|---|---|---|
| `test_authz_f2_boundary.py::test_sql_is_executed_only_by_the_audited_adapters` | 4 lectures ORM du pont, toutes sans verrou, dans `_require_owned_provenance` | les 4 mêmes (toujours sans verrou) + UNE lecture verrouillée exacte (lien de la livraison attestée) dans `_require_attested_canonical` |
| `…::test_no_boundary_module_controls_a_transaction` | `get_transaction()` dans 3 méthodes des capacités | + `require_delivery` (lecture seule) |
| `…::test_adapter_statements_are_owner_scoped_and_non_transactional` | inventaire fermé des instructions | + `_CANONICAL_DELIVERY`, contrôlée par `_check_canonical_statement` (filtres propriétaire exacts, aucune écriture, aucun verrou, tables fermées, jointures externes depuis `o`) |
| `…::test_closed_registry_and_lock_plans` | 18 commandes ; toute commande `DECRYPTAGE_MUTATION` reçoit le trousseau | 20 ; le replay est une lecture sans trousseau ; l'ACK est `DECRYPTAGE_MUTATION` SANS trousseau (freeze §4), adaptateurs exacts |
| `test_authz_f2_pr9a_boundary.py::test_closed_inventory_of_exact_operation_types` | un contrat par opération | tuple fermé ; ACK = sonde OU attestation ; classes exactes admises vérifiées |
| `…::test_entry_points_call_the_services_in_the_historical_order` | ordre historique de `_acknowledge` ; `implementation(CAPABILITIES[…])` | ordre historique inchangé + ordre des contrôles C2 ajoutés ; `_admitted_capabilities` |
| `…::test_production_registry_stays_closed_at_eighteen_commands` | 18 | renommé `…_twenty_commands`, 20, ACK dans `DECRYPTAGE_MUTATION` |
| `…::test_bench_commands_are_unknown_outside_the_isolated_fixture` | « 18 » | « 20 », mêmes refus |
| `test_authz_f2_pr9c1b_boundary.py` | 18 commandes, contrat seul, 2 implémentations | 20, tuple d'un contrat, 3 implémentations, attestation construite par le seul adaptateur ACK |
| `test_authz_f2_pr9c1a_boundary.py` | 18 ; attributs du pont utilisés par les adaptateurs | 20 ; + opération ACK, sa vue, constantes V2 et empreinte visible (aucune primitive C1-A) |
| `test_authz_f2_pr9b1_boundary.py` | vocabulaire des commandes Décrypter | + les deux commandes et la vue C2 ; toujours aucune commande force / reset / unlock |
| `test_authz_f2_pr9b2_boundary.py` | 18 commandes, 11 lectures ; seule `_PREPARATION_CONTEXT` lit les tables cognitives | 20, 12 lectures (replay sans trousseau) ; `_CANONICAL_DELIVERY` lit aussi lien et events, sans écriture, jamais contributions ni faits |
| `test_authz_f2_dormancy.py` | imports de `_adapters` | + `decryptage_candidate`, `decryptage_market_data` (modules purs) |
| `test_authz_f2_runner.py`, supports PR9-A / C1-A | 18 commandes ; capacité seule | 20 ; tuple de contrats pour l'opération de banc |

## 14. Dormance

Aucune route, aucun import depuis `api.py` ni `core/authz_f2_http` ; le point d'entrée réel ne charge aucun
module `authz` (vérifié en processus neuf, même avec des variables d'activation) ; aucune migration
(`0021` reste la tête).

## 15. Limites connues avant PR9-C3

- Le déclenchement réel de l'ACK par l'interface first-party et la coordination définitive avec les
  writers legacy (`/decryptage`, ACK historique) relèvent de PR9-C3 ou de l'activation : C2 cohabite avec
  eux par les verrous existants (livraison → lien), sans les modifier.
- La preuve terminale ne peut pas détecter une modification du texte public qui aurait été faite en
  recalculant aussi l'empreinte visible (même limite que C1-B : l'empreinte est une intégrité, pas une
  signature).
- `payload_json` est le texte JSONB : un client qui comparerait les octets à ceux du candidat d'origine
  observerait des différences de forme (ordre des clés, espaces, notation des nombres) ; seules les
  valeurs sont garanties.
- Le replay reflète l'état committé au début de son instruction (READ COMMITTED) : un ACK en cours n'est
  visible qu'après son COMMIT.
