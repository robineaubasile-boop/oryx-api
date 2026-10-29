# R1 PR 9-A — Fondations transactionnelles F2 / Décrypter (dormantes)

## 1. Références normatives

Freeze architectural validé par le fondateur le 29 octobre 2026 :

1. **R1 PR9 V2** — fondations et découpage PR9-A / B / C ;
2. **R1-PR9-A-V2.1-2026-10-29** — consolidation BF1 / BF2 ;
3. **R1-PR9-A-V2.2-2026-10-29** — fermeture documentaire définitive.

La V2.2 prévaut lorsqu'elle corrige ou précise la V2.1, la V2.1 lorsqu'elle
précise la V2. Ces documents ne sont pas versionnés dans le dépôt : le
contrat appliqué est celui reproduit dans la demande d'implémentation.
Aucune décision architecturale nouvelle n'est prise ici ; les deux
renforcements ajoutés au pont (§ 10) sont des défenses supplémentaires du
contrat « succès seulement après COMMIT confirmé », motivées par deux
contre-exemples MESURÉS, sans changement de mode ni de configuration
figée — ils sont signalés au fondateur (§ 18).

## 2. Commit de base

`main` = `1828ab955ab3adc61c8cdd8a7182cf3a17735ab3` (merge de la PR #228,
R1 PR 8), identique au commit de référence de la mission ; aucun commit
intermédiaire, aucune divergence constatée avec le freeze.

Versions réellement exécutées (CI, `ci/requirements-ci.txt`) : Python
3.11.9, **SQLAlchemy 2.1.3**, **psycopg2-binary 2.9.13**, PostgreSQL
**16.15** et **18.6** (images épinglées par digest). `join_transaction_mode
= "rollback_only"` existe et se comporte comme décrit au § 10 dans cette
version (mesuré) : aucun blocage de version.

## 3. Périmètre PR9-A

Livré (fondations internes, dormantes) :

| Élément | Fichier |
|---|---|
| Plan `DECRYPTAGE_MUTATION`, ensemble `PLANS` | `core/authz_f2/commands.py` |
| SQL fermé `users FOR NO KEY UPDATE`, alerte d'isolation par plan | `core/authz_f2/_adapters.py` |
| Transmission du plan à `require_read_committed` (une ligne) | `core/authz_f2/runner.py` |
| Pont ORM privé (nouveau) | `core/authz_f2/_decryptage_orm_bridge.py` |
| Primitive propriétaire-centrée + extraction sans changement de comportement | `core/assistant_delivery.py` |
| Banc d'essai isolé (nouveau) | `tests/authz_f2_pr9a_support.py` |
| Tests PostgreSQL atomicité / propriété / contrat du pont (nouveau) | `tests/test_authz_f2_pr9a.py` |
| Tests de verrous et de concurrence (nouveau) | `tests/test_authz_f2_pr9a_concurrency.py` |
| Contrôles statiques et de dormance PR9-A (nouveau) | `tests/test_authz_f2_pr9a_boundary.py` |
| Extension BF1 nominative, matrice d'imports, API publique | `tests/test_authz_f2_boundary.py`, `tests/test_authz_f2_dormancy.py`, `tests/test_assistant_delivery.py` |
| Cette documentation | `docs/f2_r1_pr9a.md` |

Non créés (interdits) : `POST /api/v2/me/decryptage`, `POST
/api/v2/me/assistant-deliveries/{id}/ack`, toute route F2 Décrypter,
réservation durable, `decryptage_turn_reservations`, migration `0021`,
table, colonne, champ métier, empreinte de réservation, clé HMAC, bail,
retry de génération, orchestration Claude, appel EODHD, progression,
preuve, observation ou inférence nouvelle, modification frontend,
activation F2. Inchangés : `api.py`, `web-v2/index.html`, migrations
0001–0020, endpoints historiques, modules pédagogiques (seul
`core/assistant_delivery.py` reçoit l'extraction autorisée du § 8).

## 4. Nouveau plan de verrouillage (A-01)

```python
DECRYPTAGE_MUTATION = LockPlan("decryptage_mutation", "FOR NO KEY UPDATE", (), True)
```

Instruction fermée (constante, jamais composée depuis une donnée client) :

```sql
SELECT id FROM users WHERE id = :u FOR NO KEY UPDATE
```

`READ` (`FOR KEY SHARE`), `PROFILE_MUTATION` (`FOR UPDATE`) et
`PORTFOLIO_MUTATION` (`FOR UPDATE`) sont inchangés. READ COMMITTED est
vérifié au début de la phase B (refus fermé, alerte
`decryptage_isolation_refused` ; `portfolio_isolation_refused` inchangée).
Le niveau d'isolation global du moteur n'est pas modifié. **Aucune
commande du registre de production n'utilise ce plan** (contrôlé
statiquement).

Justification `FOR NO KEY UPDATE` — **mesurée** (sondes NOWAIT d'une
connexion tierce pendant qu'une mutation Décrypter F2 est suspendue,
`test_no_key_update_is_compatible_with_foreign_key_checks`) :

| Mode demandé par un tiers sur la ligne `users` | Résultat |
|---|---|
| `FOR KEY SHARE` (contrôle de FK) | accordé |
| `FOR SHARE` | `55P03` |
| `FOR NO KEY UPDATE` (autre mutation Décrypter, UPDATE historique de `level`) | `55P03` |
| `FOR UPDATE` (mutations profil / portefeuille F2) | `55P03` |

Un INSERT historique avec FK vers `users` aboutit sans attendre. Deux
mutations Décrypter du même compte se sérialisent au rang `users` (attente
constatée sur un verrou de ligne de `users`, avant T_authorization) ; deux
comptes différents ne se bloquent jamais (le second aboutit pendant la
pause du premier). `FOR NO KEY UPDATE` ne résout PAS à lui seul les
courses métier : les writers historiques ne le prennent pas et restent
protégés par leurs propres verrous (§ 7).

## 5. Contrats BF1 / BF2 préservés

- Interface publique unique : `run(credential, command)` ; constructeur
  inchangé (`engine, keyring, portfolio_keyring, clock, fault_hook`) ;
  aucun paramètre ajouté.
- Registre de production : **exactement quatorze commandes**, mêmes types,
  mêmes plans ; ni commande de démonstration, ni commande Décrypter.
- Phase A (pré-validation sans écriture) et phase B (READ COMMITTED,
  `users`, session `FOR SHARE`, génération `FOR SHARE`, verrous
  préparatoires de `spec.lock`, `clock_timestamp()` = T_authorization,
  décision complète, `AuthorizedContext`, adaptateur du type exact, COMMIT
  ou ROLLBACK) inchangées ; succès rendu seulement après COMMIT confirmé.
- Contrats PR5–PR8 inchangés (suites existantes vertes, § 16).

## 6. Verrous préparatoires et verrous d'exécution (A-03)

| Catégorie | Moment | `DECRYPTAGE_MUTATION` en PR9-A |
|---|---|---|
| Préparatoires | avant T_authorization, dans `spec.lock`, sans écriture | **aucun** (`business_locks = ()`, `spec.lock = _no_business_lock`) ; l'attente préparatoire est celle des rangs session et génération |
| D'exécution | après la décision, même transaction, ordre des services | ceux des services Décrypter appelés par le pont (§ 7) |

Mesuré : une révocation committée pendant l'attente du rang `users`, du
rang `oryx_sessions` ou du rang `oryx_session_generations` est vue à
T_authorization (refus, rien n'est écrit) ; une échéance atteinte pendant
l'attente aussi ; une révocation lancée APRÈS l'autorisation attend le
COMMIT de la transaction autorisée, qui aboutit, puis plus rien n'est
autorisé.

## 7. Graphe des verrous F2 / legacy (A-02)

### 7.1 FK réelles (relevées sur le schéma `head`, PostgreSQL 16)

Chaque INSERT (ou UPDATE de la colonne référençante) prend `FOR KEY SHARE`
sur la ligne parente : `analysis_facts`, `analysis_sessions`,
`assistant_deliveries`, `cognitive_events`, `conversation_identities`,
`investment_theses`, `oryx_sessions`, `portfolio_positions`,
`portfolio_write_idempotency`, `user_statements` → `users` ;
`assistant_deliveries.conversation_key` → `conversation_identities` ;
`assistant_deliveries`, `analysis_facts`, `cognitive_events`,
`investment_theses`, `user_statements` (`analysis_session_id`) →
`analysis_sessions` ; `decryptage_cognitive_links` → `assistant_deliveries`
et (`input_event_id`, `input_context_event_id`, `response_context_event_id`,
`response_event_id`, `context_exit_event_id`) → `cognitive_events`,
`support_trace_id` → `support_traces` ; `support_traces` →
`cognitive_events`. Aucun trigger utilisateur. Contraintes d'unicité
concernées : `conversation_identities_pkey`,
`uq_assistant_deliveries_turn_ordinal`,
`uq_cognitive_events_event_dedup_key`, `uq_support_traces_event_sequence`.
Tous les verrous ci-dessous sont des verrous de **ligne** (aucun verrou de
table explicite ; seuls les verrous de relation implicites
`RowShareLock` / `RowExclusiveLock`, compatibles entre eux).

### 7.2 Tableau par transaction

Légende de durée : « tx » = jusqu'au COMMIT / ROLLBACK de la transaction.
« Mesuré » = constaté par un test cité ; les autres colonnes décrivent le
code (hypothèse de lecture, recoupée par les tests de concurrence).

| Transaction (point d'entrée) | Ordre d'acquisition (ressource : mode) | FK implicites (KEY SHARE) | Arêtes d'attente possibles | Test | Résultat observé |
|---|---|---|---|---|---|
| Runner phase B, plan READ | users : KEY SHARE → oryx_sessions : SHARE → génération : SHARE | — | attend un `FOR UPDATE` de users (profil / portefeuille F2, liaison F2) | PR5 / PR8 | inchangé |
| Runner, PROFILE_MUTATION / PORTFOLIO_MUTATION | users : UPDATE → session : SHARE → génération : SHARE → (portefeuille) reçu : UPDATE → position : UPDATE | users (déjà détenue) | attend / bloque Décrypter F2 au rang users | `test_profile_and_portfolio_mutations_wait_coherently` (4 ordres) | attente au rang `users`, les deux aboutissent |
| **Runner, DECRYPTAGE_MUTATION — finalisation d'un tour** (pont `TurnFinalization`) | users : NO KEY UPDATE → session : SHARE → génération : SHARE → T_auth → [conversation_identities : INSERT en SAVEPOINT si neuve] → assistant_deliveries : INSERT en SAVEPOINT → lien (aucun existant) → analysis_sessions active : UPDATE → events open de la session + ancre : UPDATE (une instruction triée par id) → lien : INSERT → progression (analysis_sessions déjà détenue) : UPDATE, INSERT user_statements / investment_theses / analysis_facts | users, conversation_identities, analysis_sessions, events référencés (déjà UPDATE) | attend une analysis_sessions détenue par un ACK ou un /decryptage historiques ; attend l'index unique (tour ou conversation concurrents) | `test_measured_lock_profile_of_a_contribution_turn_and_of_its_ack` | **mesuré** : users NO KEY UPDATE, oryx_sessions SHARE, génération SHARE, conversation_identities KEY SHARE (FK), analysis_sessions UPDATE, event d'ancrage UPDATE |
| **Runner, DECRYPTAGE_MUTATION — ACK** (pont `DeliveryAcknowledgement`) | users : NO KEY UPDATE → session : SHARE → génération : SHARE → T_auth → assistant_deliveries **propre** : UPDATE (id ET user_id ET conversation_key) → lectures sans verrou de TOUTE la provenance (lien, trace, events, AnalysisSessions) → lien : UPDATE → analysis_sessions : UPDATE → events open : UPDATE (trié) → support_traces : INSERT / event fermé : UPDATE / event ouvert : INSERT en SAVEPOINT → lien, livraison : UPDATE | users, conversation_identities, analysis_sessions, cognitive_events | attend une livraison détenue par un ACK historique ; attend analysis_sessions | idem | **mesuré** : users NO KEY UPDATE, livraison UPDATE, lien UPDATE, analysis_sessions UPDATE, event fermé UPDATE |
| `/decryptage` historique (phase B) | assistant_deliveries : INSERT (SAVEPOINT) → lien → analysis_sessions : UPDATE → events : UPDATE (trié) → lien : INSERT → progression → livraison propre : UPDATE (bind) | users, conversation_identities, analysis_sessions, events | attend analysis_sessions détenue par la finalisation F2 ; **jamais** users (KEY SHARE compatible avec NO KEY UPDATE) | `test_f2_finalization_against_legacy_decryptage_preserves_integrity` (2 ordres) | attente sur `analysis_sessions`, une seule avancée, aucun interblocage |
| ACK historique | livraison : UPDATE (UUID seul) → lien : UPDATE → analysis_sessions : UPDATE → events : UPDATE → support_traces / events | idem | attend la livraison détenue par l'ACK F2 ; attend analysis_sessions | `test_f2_ack_against_legacy_ack_captures_once`, `test_f2_finalization_against_legacy_ack` (2 ordres chacun) | attente sur `assistant_deliveries` (ACK contre ACK) ou `analysis_sessions` (finalisation contre ACK) ; capture unique |
| Révocation F2 | oryx_sessions : UPDATE → générations : UPDATE (triées) | — | attend le SHARE de session d'une transaction autorisée | `test_revocation_after_authorization_waits_for_the_commit` | attend le COMMIT, puis `True` |
| `authenticate_session` F2 | session : SHARE → génération : UPDATE | — | bloque / attend le SHARE de génération | PR5 (inchangé) | inchangé |
| Liaison OTP F2 | users : UPDATE → opérations → … | — | rang users avant tout le reste | PR4 / PR5 | inchangé |
| `PUT /api/user/{id}/level` historique | users : NO KEY UPDATE (UPDATE de `level`) | — | se sérialise avec Décrypter F2 au rang users ; ne détient rien d'autre | conflit mesuré par la sonde `FOR NO KEY UPDATE` (§ 4) | hypothèse : aucun cycle (une seule ligne verrouillée) |
| Portefeuille / thèses historiques | INSERT / DELETE de lignes métier | users (FK) | aucune attente sur users en NO KEY UPDATE | `test_no_key_update_is_compatible_with_foreign_key_checks` | INSERT historique non bloqué |

### 7.3 Scénarios de cycle examinés

- Décrypter F2 contre writers historiques Décrypter : F2 prend `users`
  d'abord, que les writers historiques ne demandent jamais dans un mode
  conflictuel ; tous deux prennent ensuite les lignes métier dans l'ordre
  unique historique (livraison → lien → AnalysisSession → events par id →
  traces) via les mêmes fonctions : aucune arête inverse. Mesuré : six
  scénarios croisés (deux ordres chacun), compteur
  `pg_stat_database.deadlocks` inchangé, aucun 503.
- Le pont ne préverrouille rien : aucun event, aucune AnalysisSession
  n'est verrouillé hors des services, aucun ordre historique n'est
  modifié. La relecture de provenance de l'ACK (§ 8) est faite **sans
  verrou**.
- Aucun deadlock n'est masqué par un délai : chaque test de concurrence
  exige AUTHORIZED (aucun `status=unavailable` journalisé) ET un compteur
  d'interblocages inchangé. Un délai de verrou est un refus 503 sans
  écriture (`test_lock_timeout_in_phase_b_is_unavailable_never_a_success`),
  jamais une preuve d'absence d'interblocage. **Aucune garantie
  « deadlock-free » générale n'est affirmée** : seuls les scénarios
  ci-dessus sont démontrés.

## 8. Primitive de livraison propriétaire-centrée (V2.2-01)

```python
select(AssistantDelivery).where(
    AssistantDelivery.id == assistant_turn_id,
    AssistantDelivery.user_id == authorized_user_id,
    AssistantDelivery.conversation_key == conversation_key,
).with_for_update().execution_options(populate_existing=True)
```

`ad.lock_owned_delivery` : les trois conditions font partie de la requête
de verrouillage (SQL émis vérifié) ; absente, étrangère ou autre
conversation : même `AssistantDeliveryNotFound` sans identifiant, rendu
`NOT_FOUND` par le pont ; version de schéma inconnue : refus fermé ;
`populate_existing` recharge l'état après attente ; aucune réparation.

Mesuré (`test_foreign_delivery_locked_by_a_third_party_is_refused_without_waiting`) :
une livraison d'autrui verrouillée `FOR UPDATE` par un tiers ; l'ACK F2
rend `NOT_FOUND` sans aucune attente de verrou (constaté en continu dans
`pg_stat_activity`, bien avant `lock_timeout`). Contre-épreuve : la
primitive historique `lock_delivery_for_ack` (UUID d'abord) attend ce
verrou et échoue en `55P03` sous `lock_timeout = 300ms`.

Propriété transitive (complétée après l'audit indépendant du HEAD
`28af014`) : le lien est l'enfant de la livraison verrouillée ; avant toute
capture, premier ACK comme ACK rejoué, V1 comme V2, le pont relit **sans
verrou** TOUTES les références cognitives non nulles du lien —
`input_event_id`, `input_context_event_id`, `response_context_event_id`,
`context_exit_event_id` (V1), `response_event_id`, `support_trace_id` et
l'event parent de cette SupportTrace —, puis les AnalysisSessions de la
livraison et de chacun de ces events, et exige le même propriétaire.
Référence d'autrui ou introuvable : `ProvenanceIncoherent` (503, alerte
`decryptage_bridge_provenance_incoherent`), ROLLBACK intégral. La liste
des colonnes est comparée aux FK réelles du modèle
(`test_the_provenance_check_covers_every_cognitive_reference_of_a_link`) :
une future colonne de référence non couverte ferait échouer les tests.
Testé sur PostgreSQL (la ligne d'autrui tenue `FOR UPDATE` par un tiers
pendant l'appel : refus par la relecture, jamais une attente de verrou ;
état inchangé, xmin compris) : `response_event_id` d'un ACK
`transitioned_event` rejoué, `support_trace_id` et parent de trace d'un
ACK `continued_event` rejoué, event rattaché à l'AnalysisSession d'autrui,
`context_exit_event_id` d'une livraison V1 ; cas cohérents (rejeux V2,
V1 avec sortie de contexte propre) toujours acceptés. Ces cinq refus
échouaient sur l'ancienne fonction (contre-épreuve exécutée). Aucun
verrou ajouté, aucun ordre historique modifié. La finalisation n'atteint
que des lignes filtrées par `user_id` (services historiques) ou créées
dans la transaction.

Compatibilité historique : `_lock_delivery`, `lock_delivery_for_ack`,
`acknowledge_delivery`, `bind_analysis_session` gardent leur code d'accès
et leurs erreurs (403 / 404 / idempotence vérifiés à nouveau) ; seules les
mutations sur livraison déjà verrouillée sont extraites
(`_mark_delivered`, `_bind_locked`) et exposées au chemin F2 par
`acknowledge_locked_delivery` / `bind_locked_analysis_session`, qui
revérifient le propriétaire. Le pont n'atteint jamais `_lock_delivery`
(graphe d'appels statique).

## 9. Configuration ORM

```python
Session(bind=authorized_connection, autoflush=False, join_transaction_mode="rollback_only")
```

Une Session par opération (`_BridgeSession(conn)`), sur la connexion de la
transaction déjà ouverte par le runner ; aucun moteur, aucune connexion,
jamais `SessionLocal` (vérifié à l'exécution : `core.db.SessionLocal` et
`core.db.engine` remplacés par des sentinelles qui échouent, aucun emprunt
ni connexion d'aucun pool entre T_authorization et la fin de l'exécution).
`autoflush=False` est la configuration de `core.db.SessionLocal` : les
services flushent explicitement ; le pont fait un flush final explicite
avant la vue.

## 10. Comportement `rollback_only` (mesuré, SQLAlchemy 2.1.3)

| Action dans la Session | Effet mesuré sur la transaction du runner |
|---|---|
| `Session.commit()` | non propagé ; la transaction externe reste active et son ROLLBACK efface tout |
| `Session.rollback()` | **termine** la transaction externe ; une instruction suivante sur la connexion échoue, **mais le bloc `engine.begin()` du runner se termine ensuite sans erreur et sans COMMIT** |
| `Session.close()` | aucun effet sur la transaction externe |
| erreur de flush hors SAVEPOINT, avalée | la transaction externe est annulée ; même sortie silencieuse sans COMMIT |
| erreur d'**instruction** avalée | transaction PostgreSQL avortée alors que SQLAlchemy la croit active ; le COMMIT final devient un ROLLBACK **silencieux** (aucune erreur psycopg2) |

Ces trois sorties silencieuses produiraient un faux succès. Défenses du
pont (aucun changement de mode ni de configuration) :

1. `_BridgeSession` refuse `commit()`, `rollback()`, `begin()` non
   imbriqué, `close()`, `reset()`, `invalidate()`, `connection()`
   (`TransactionOwnershipViolation`, 503 + alerte) ; `begin_nested()`
   reste permis ; seul le nettoyage du pont ferme la Session ;
2. avant tout résultat : la transaction du runner doit être exactement la
   même transaction active (`TransactionLost` sinon), puis une sonde
   réutilise l'instruction fermée de l'isolation, qui échoue sur une
   transaction avortée (`InFailedSqlTransaction`).

Testé : chacune des sept méthodes appelée par un service → 503, rien
d'écrit ; flush en échec avalé → 503 (arrêté par le flush final,
`PendingRollbackError`) ; instruction en échec avalée → 503 (sonde) ;
`_require_live` détecte une transaction externe terminée.

## 11. `autoflush=False`

Identique au chemin historique (`SessionLocal`). Les écritures en attente
ne partent qu'aux `flush()` explicites des services, aux `begin_nested()`
(§ 13) et au flush final du pont.

## 12. Cycle de vie et nettoyage

Connexion autorisée → Session → opération fermée → flush final → vue
immuable (types `uuid.UUID`, `bool`, `str`, `None` ; jamais une instance
ORM) → contrôle de la transaction → fermeture → retour au runner → COMMIT
externe → succès. En échec : Session fermée, exception d'origine propagée
(une erreur de nettoyage est journalisée `cleanup_failed` et ne la masque
jamais — testé), ROLLBACK par le runner. Un échec de fermeture APRÈS
succès est propagé : aucun succès n'est alors rendu.

## 13. SAVEPOINT et flush préalable

Mesuré : `begin_nested()` flushe les écritures en attente AVANT le
SAVEPOINT (`INSERT`, `SAVEPOINT`, `INSERT`, `ROLLBACK TO SAVEPOINT`) ; le
retour au SAVEPOINT ne les touche pas ; un ROLLBACK externe les efface.
SAVEPOINT atteints (et seulement eux) : `claim_delivery`,
`register_or_resolve_conversation`, `open_event_idempotent` ; chacun ne
traduit que sa contrainte nommée, toute autre erreur est propagée
(contrôlé statiquement). Collisions réelles testées :

- conversation insérée par une transaction concurrente : attente sur
  l'index, `ROLLBACK TO SAVEPOINT`, gagnant relu (READ COMMITTED), une
  seule ligne ; écritures de la transaction extérieure commitées en cas
  de succès, toutes annulées par un échec ultérieur ;
- même tour inséré par une transaction historique : gagnant relu,
  `created = False`, aucune duplication, aucune capture par le perdant.

## 14. Liste fermée des opérations, services et dépendances

| Opération (type exact) | Point d'entrée | Services, dans l'ordre |
|---|---|---|
| `TurnFinalization` | `_finalize_turn` | `claim_delivery` ; si créée : `capture_user_turn`, `answered_step` (contribution), `apply_construction_these_progress` (`construction_these` + marqueur), `bind_locked_analysis_session`, `record_turn_progress` |
| `DeliveryAcknowledgement` | `_acknowledge` | `lock_owned_delivery`, relecture de provenance, `capture_delivered_response`, `acknowledge_locked_delivery` |

Les opérations ne portent ni `user_id`, ni Session, connexion, fonction,
SQL, nom de service ; les textes JSON sont immuables. Le graphe d'appels
complet atteint depuis chaque point d'entrée est calculé sur le code réel
et comparé à une liste exacte (`REACHABLE`,
`tests/test_authz_f2_pr9a_boundary.py`) : aucune fonction atteinte ne
commite, n'annule, n'ouvre de transaction ou de connexion, ni n'écrit de
SQL textuel. Imports transitifs du pont inventoriés (16 modules `core.*`,
dont `core.db` par `core.models`) ; aucun module réseau ; aucune connexion
à l'import, même avec `DATABASE_URL`.

## 15. Banc d'essai isolé (V2.2-04)

Registre fermé de TEST = quatorze commandes de production + `BenchTurn`
et `BenchAck` (types exacts définis dans les tests), dont l'adaptateur
d'exécution appelle le vrai pont sous le vrai runner. La fixture `bench`
le substitue à `_adapters.REGISTRY` le temps d'un test puis vérifie que
l'objet de production d'origine (quatorze commandes) est restauré. Aucun
paramètre, drapeau ou variable d'environnement. Dans un processus neuf,
le runner refuse `BenchTurn`, `BenchAck` et les opérations brutes
(`UnknownCommand`). Échecs injectés par remplacement temporaire d'un
service ou par les points d'interruption existants du runner : aucun
rappel n'est passé au pont.

## 16. Preuves PostgreSQL 16 et 18

Exécutions locales (images CI épinglées, 16.15 et 18.6) des trois
fichiers PR9-A : **101 tests verts sur chaque version, trois fois de
suite** (HEAD `28af014`) ; après le correctif de propriété transitive,
**110 tests PR9-A** (131 avec les contrôles BF1 et de dormance) verts
sur chaque version. Suite Python complète : voir la description de la PR (résultats
locaux) et les contrôles GitHub `ci / postgres-16` et `ci / postgres-18`.

## 17. Résultats des tests de sécurité

| Test | Résultat |
|---|---|
| A — livraison créée puis échec avant la capture | aucune livraison, lien, progression ni conversation durable |
| B — contribution capturée puis échec avant la progression | aucune contribution, livraison ni mutation cognitive durable |
| C — progression qui crée réellement la thèse (contribution sur `risques`, `swot_final`), puis échec avant le résultat | aucune thèse, aucun UserStatement, session non terminée, aucune livraison |
| D — ACK : capture faite (lien `captured`, trace créée) puis échec avant `delivered` | livraison `pending`, lien `awaiting_delivery`, aucune trace ni event |
| E — succès complet | lignes commitées = lignes vues dans la transaction du runner (xmin = son xid ou ceux de ses SAVEPOINT) ; 2 COMMIT par appel (phase A en lecture, phase B) et 2 emprunts de connexion, rien d'autre ; analyse complète jusqu'à la thèse |
| F — SAVEPOINT | § 13 |
| G — COMMIT rejeté par PostgreSQL (trigger différé) | 503, rien d'écrit, opération exécutée une seule fois |
| G — COMMIT abouti mais signalé en erreur (perte simulée) | 503 (`OperationalError` journalisé), **état incertain** pour l'appelant : ni succès ni annulation prétendus, aucun rejeu ; l'état réel ne se constate que par lecture, un rejeu du même tour est idempotent (`created = False`) |
| Autorisation | justificatif invalide, expiré, révoqué, génération révoquée, livraison non confirmée, erreur à T_authorization : aucun point d'entrée du pont appelé, rien d'écrit |
| Propriété | livraison étrangère, absente, d'une autre conversation : même `NOT_FOUND` sans valeur ni principal ; conversation d'autrui : `NOT_FOUND`, rien d'écrit ; provenance incohérente (toute référence du lien, trace et son parent, AnalysisSessions ; V1, V2, rejeu) : 503 sans verrou ni écriture ; version inconnue : 503 |
| Registre | sous-classe, opération brute, commande inconnue : `UnknownCommand` avant toute transaction |

## 18. Limitations et risques résiduels

- **Renforcements à valider par le fondateur** (non figés littéralement,
  sans changement de mode) : garde de la Session et sonde de la
  transaction (§ 10). Ils ne retirent aucune capacité figée.
- La sortie silencieuse d'un `engine.begin()` sur transaction avortée
  (§ 10) concerne potentiellement tout adaptateur du runner qui avalerait
  une erreur SQL ; les adaptateurs PR5–PR8 n'en avalent aucune
  (inchangés). Le pont est seul protégé par la sonde.
- Erreurs métier Décrypter (`StaleConversationContext`, collisions) :
  503 générique du runner en PR9-A ; leur traduction en résultats fermés
  relève de PR9-B / C.
- Espace de noms des `conversation_key` global (R1-B) : une clé déjà
  attribuée à autrui est refusée (`NOT_FOUND`) alors qu'une clé libre est
  enregistrée ; l'existence d'une clé reste donc observable (comportement
  historique, non modifié).
- Les writers historiques ne prennent pas le verrou `users` : leurs
  courses restent protégées par les verrous métier et contraintes
  existants, pas par `FOR NO KEY UPDATE`.
- COMMIT incertain : aucune réparation automatique ; la résolution
  repose sur l'identité idempotente du tour.
- Performances : chaque exécution du pont ajoute une Session ORM, quatre
  lectures de provenance au plus (ACK) et une sonde ; non mesuré sous
  charge.

## 19. Maintien des 14 commandes

`test_closed_registry_and_lock_plans`,
`test_production_registry_stays_closed_at_fourteen_commands`,
`test_bench_commands_are_unknown_outside_the_isolated_fixture` et la
fixture `bench` (avant / après chaque test PR9-A) : 14 commandes, aucune
sous le plan `DECRYPTAGE_MUTATION`, aucun adaptateur hors `_adapters`.

## 20. Dormance F2

Le vrai `api:app` ne charge aucun module `authz` ni le pont (processus
isolé, avec et sans variables évoquant une activation) ; `api.py` ne
mentionne ni le pont, ni le plan, ni les opérations, ni les routes
interdites ; aucun module applicatif n'importe le pont ; ses imports
n'ouvrent aucune connexion ni réseau. Les sondes de dormance PR5–PR8
restent vertes.

## 21. Absence de migration

Tête Alembic `0020_position_revision`, 20 fichiers de migration, aucune
`0021`, aucune table ni colonne.

## 22. Réservé à PR9-B / PR9-C

Routes `POST /api/v2/me/decryptage` et `POST
/api/v2/me/assistant-deliveries/{id}/ack`, réservation durable et sa
table, empreinte de réservation, clé HMAC, bail, retry de génération,
orchestration Claude et EODHD, traduction fermée des erreurs métier,
commandes de production utilisant `DECRYPTAGE_MUTATION`, codecs HTTP,
activation.
