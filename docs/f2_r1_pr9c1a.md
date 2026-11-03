# R1 PR 9-C1-A — Préparation / application cognitive et fondations transactionnelles privées (dormantes)

## 1. Références normatives

Référentiel approuvé : **PR9-C V1.2**, **addendum V1.2.1**, exigences
**F-01 à F-04**. Ces documents ne sont pas versionnés dans le dépôt : le
contrat appliqué est celui reproduit dans la demande d'implémentation
(sections 0 à 11). Seule **F-01** (dépendance `_adapters.py` ↔ pont) y est
citée nommément ; le contenu littéral de F-02 à F-04 n'est pas disponible
ici. Les exigences de la demande ont toutes été appliquées ; si F-02 à F-04
contiennent une exigence absente de la demande, elle n'a pas pu être
vérifiée (point signalé à l'audit, § 15).

Workflow : architecture figée → implémentation → PR → audit indépendant →
merge manuel par le fondateur → validation Railway. Cette PR n'est ni
mergée, ni déployée, et n'active rien.

## 2. Commit de base

`main` = `6556dd10918540b924d4dd3754f5545223b76cf9` (merge de la PR #231,
R1 PR 9-B2, correctifs d'audit), **identique** au commit de référence audité.
Aucun commit intermédiaire, aucune divergence avec le freeze.

Versions exécutées (identiques à la CI, `ci/requirements-ci.txt`) : Python
3.11, SQLAlchemy **2.1.3**, psycopg2-binary **2.9.13**, PostgreSQL **16.15**
et **18.6** (images officielles épinglées par digest, mêmes options que
`ci/start_postgres.sh`).

## 3. Périmètre

Livré (dormant) :

| Élément | Fichier |
|---|---|
| Primitives cognitives Prepare / Apply, décision préparée privée | `core/decryptage_cognitive_runtime.py` |
| Moteur de règles de progression unique + variante préparée fermée | `core/decryptage_progress.py` |
| Primitives privées du pont : SAVEPOINT C1, Prepare lié au SAVEPOINT, vue de revalidation, Apply | `core/authz_f2/_decryptage_orm_bridge.py` |
| Banc d'essai isolé (registre et inventaire de TEST) | `tests/authz_f2_pr9c1a_support.py` |
| Tests PostgreSQL fonctionnels et transactionnels | `tests/test_authz_f2_pr9c1a.py` |
| Tests PostgreSQL de verrous et de concurrence | `tests/test_authz_f2_pr9c1a_concurrency.py` |
| Contrôles statiques, unitaires et de dormance | `tests/test_authz_f2_pr9c1a_boundary.py` |
| Mises à jour explicites de contrats statiques existants (§ 10) | `tests/test_authz_f2_boundary.py`, `tests/test_authz_f2_dormancy.py`, `tests/test_authz_f2_pr9a_boundary.py`, `tests/test_decryptage_cognitive_runtime.py`, `tests/test_assistant_delivery.py` |
| Cette documentation | `docs/f2_r1_pr9c1a.md` |

**Non modifiés** : `api.py`, `web-v2/`, `core/authz_f2/_adapters.py`,
`core/authz_f2/runner.py`, `core/authz_f2/commands.py`, tous les modules B1 /
B2, `core/assistant_delivery.py`, `core/cognitive_capture.py`, migrations.

**Non implémenté (interdit en C1-A)** : `FinalizeDecryptageCandidate`,
toute commande de production, toute opération nouvelle dans
`bridge.OPERATIONS`, UPDATE `reserved → finalized`, nouvel état ou motif de
réservation, bail B1 (360 s inchangé), replay C2, ACK F2 C2, route HTTP,
`api:app`, frontend, migration, schéma des livraisons, appel Claude /
EODHD / FMP, règle produit du Chemin Oryx, correction des writers legacy.

## 4. Refactorisation cognitive

`capture_user_turn` (legacy) est désormais la composition de deux
primitives internes, **sans aucun changement de comportement** :

| Primitive | Contenu | Écrit ? |
|---|---|---|
| `_classify_user_turn(db, *, delivery, ticker, user_text, selection)` | préconditions de livraison (runtime V2, `pending`, aucun lien) ; `_lock_active_analysis_sessions` (TOUTES les sessions actives, une instruction, ordre historique) ; ancre (`_conversation_anchor_event_id`) ; events open + ancre verrouillés en une instruction triée ; `AmbiguousOpenDecryptageEvents` ; classement (`no_open_event`, `context_switched`, `StaleConversationContext`, `contribution_appended`, `no_user_contribution`) ; cible figée de la réponse | **non** (SELECT seulement) |
| `_write_user_turn(db, *, delivery, classification, user_text)` | contribution à l'ancre EXACTE (si `contribution_appended`), lien `awaiting_delivery`, flush | oui |

Politique de sélection fermée : `_LEGACY_SELECTION` (plusieurs sessions
actives → la plus récente, historique) ou `_F2_SELECTION` (plusieurs →
`AmbiguousActiveAnalysisSessions`, refus fermé, même vocabulaire que la
lecture B2 `multiple_active_sessions`). Dans les deux cas, le même ensemble
de lignes est verrouillé par la même instruction.

Phases F2 (publiques dans le module, atteintes seulement par le pont) :

- `prepare_locked_cognitive_turn(db, *, delivery, ticker, user_text)` →
  `_classify_user_turn(selection=_F2_SELECTION)` + `answered_step` lu sous le
  verrou de l'ancre (`_event_step`, même dérivation que `answered_step`) →
  `PreparedCognitiveTurn`. **N'écrit rien** : ni contribution, ni
  `DecryptageCognitiveLink`, ni `UserStatement`, ni `InvestmentThesis`, ni
  progression.
- `apply_prepared_cognitive_turn(db, *, prepared)` → `_write_user_turn` sur
  la décision : aucune nouvelle ancre, aucune nouvelle AnalysisSession,
  aucun reclassement.

`PreparedCognitiveTurn` : construite seulement par la fabrique (jeton de
module), immuable (`__setattr__` / `__delattr__` refusés), **à usage unique**
(consommée AVANT toute vérification et toute mutation), liée à la Session
qui l'a préparée, références ORM exigées attachées à cette Session,
persistantes et entièrement chargées (aucune relecture implicite sans
verrou), livraison encore `pending` ; pickle / copie / deepcopy refusés ;
`repr()` sans texte. Elle ne porte que des valeurs issues de la livraison du
principal et du tour admis.

**Preuve d'identité du legacy** : la séquence complète des instructions SQL
émises par le vrai `api.decryptage` / ACK historiques sur un parcours de 21
tours et ACK (same, forward, step_skip, retrograde, stale_contribution_step,
forward sans contribution, swot_final, conversation libre après le bilan)
est **identique octet pour octet** entre `main` et cette branche (477
instructions, noms de SAVEPOINT normalisés ; script ad hoc non versionné).

## 5. Progression pédagogique

`core/decryptage_progress.py` : les règles sont dans **une seule** fonction,
`_apply_progress_rules(db, *, active, ...)`, appliquée à une session DÉJÀ
choisie et verrouillée (ou à son absence). Aucune règle dupliquée (contrôlé
statiquement).

| Point d'entrée | Sélection de la session | Règles |
|---|---|---|
| `apply_construction_these_progress` (legacy, inchangé) | `find_active_analysis_session(for_update=True)` (la plus récente si plusieurs, anomalie journalisée) | `_apply_progress_rules` |
| `apply_prepared_construction_these_progress` (F2) | **aucune** : `analysis_session` = l'instance préparée (ou `None`) | `_apply_progress_rules` |

Variante F2 — jamais une autre session :

- instance préparée : AnalysisSession de CETTE Session, persistante,
  chargée, même propriétaire, même ticker, `in_progress` ; sinon
  `PreparedAnalysisSessionMismatch` (aucune écriture) ;
- absence préparée : lecture **sans verrou** (aucune attente, aucun nouvel
  ordre de verrous) ; une session `in_progress` commitée apparue depuis →
  `PreparedAnalysisSessionMismatch` (jamais adoptée).

Règles préservées (parité testée scénario par scénario, § 11) : `new`,
`same`, `forward`, `step_skip`, `retrograde_marker`,
`forward_without_contribution`, `terminal_without_contribution`,
`stale_contribution_step`, `terminal_before_risques`, `swot_final` (thèse
seulement sur contribution réelle à `risques`), swot_final sans session
(ignoré). `UserStatement` seulement pour une contribution réelle, sur
`answered_step` ; `InvestmentThesis` jamais depuis un simple texte Claude ;
`analysis_session_id` partout ; décision `product_progress_action` persistée
sur le lien. Une étape ou un niveau n'est jamais une preuve de compétence
(aucun appel T3+).

## 6. Pont ORM privé : protocole transactionnel préparé

Configuration inchangée : `Session(bind=connexion_du_runner,
autoflush=False, join_transaction_mode="rollback_only")`, garde
`_BridgeSession`, `_require_live`, inventaire `OPERATIONS` à **deux**
opérations. Primitives privées ajoutées (aucune n'est atteinte par la
production) :

| Phase figée | Primitive | Garanties |
|---|---|---|
| 3. SAVEPOINT | `_business_savepoint(db)` (gestionnaire de contexte) | Session du pont exigée ; jamais imbriqué dans un autre SAVEPOINT ; **aucune écriture ORM en attente** (`PendingWritesBeforeSavepoint` : `begin_nested()` la flusherait hors du SAVEPOINT) ; sonde de la transaction du runner ; `with db.begin_nested()` |
| 5. Préparation | `_prepare_turn(db, scope, user_id, delivery, *, ticker, user_text)` | SAVEPOINT C1 courant exigé ; livraison du principal (`ProvenanceIncoherent` sinon) ; aucune écriture en attente avant ; **aucune écriture produite** (contrôlé) ; rend `_PreparedTurn` lié à CE SAVEPOINT et à CE principal |
| 6. Revalidation (partie cognitive) | `_PreparedTurn.context` (`PreparedCognitiveContext`, vue immuable et bornée) ; `_cognitive_context_matches(context, *, ticker, anchor, active_session)` (pure) | compare ce qui a été verrouillé à l'ancre et à la session active d'un snapshot B2 (`dc.PreparationAnchor`, `dc.PreparationActiveSession`) ; `ANCHOR_UNBOUND` jamais accepté |
| 7. Application | `_apply_prepared_turn(db, scope, user_id, prepared, *, method_id, step_marker, financial_snapshot)` | décision consommée avant toute mutation ; même SAVEPOINT encore courant ; même principal ; `dcr.apply_prepared_cognitive_turn`, `dp.apply_prepared_construction_these_progress(analysis_session=decision.analysis_session)`, `bind_locked_analysis_session`, `record_turn_progress`, flush ; vue `PreparedTurnApplied` |
| 8. RELEASE | sortie normale du gestionnaire | flush, `RELEASE SAVEPOINT`, sonde |

Sorties du SAVEPOINT :

- **`_LocalRollback(reason)`** (seul motif d'annulation locale, vocabulaire
  FERMÉ `LOCAL_ROLLBACK_REASONS = ("context_changed",)`) : sonde AVANT
  l'annulation — une erreur SQL avalée dans le SAVEPOINT laisse la
  sous-transaction avortée, la sonde échoue et l'opération entière est
  refusée (jamais masquée par le retour au SAVEPOINT) —, puis `ROLLBACK TO
  SAVEPOINT`, `expunge_all()`, sonde ; la transaction du runner continue ;
- **toute autre exception** : `ROLLBACK TO SAVEPOINT` puis propagation →
  ROLLBACK intégral par le runner (UNAVAILABLE) ;
- jamais de COMMIT ni de ROLLBACK global dans le pont (aucun appel
  `commit()` / `rollback()`, contrôlé par BF1).

Faits **mesurés** qui fondent ces choix :

1. SQLAlchemy 2.1.3 : un `ROLLBACK TO SAVEPOINT` (`_restore_snapshot(
   dirty_only=True)`) **n'expire pas** les objets seulement chargés dans le
   SAVEPOINT. Une `AnalysisSession` verrouillée puis « déverrouillée » par
   le retour au SAVEPOINT resterait utilisable avec ses valeurs. D'où :
   liaison de la décision au SAVEPOINT (pont), `expunge_all()` après toute
   annulation locale, et refus par le runtime de toute référence détachée.
2. PostgreSQL 16.15 et 18.6 (sondes NOWAIT d'une connexion tierce) : un
   verrou de ligne pris AVANT le SAVEPOINT reste détenu après `ROLLBACK TO
   SAVEPOINT`, même si la ligne a été reverrouillée dans le SAVEPOINT ; un
   verrou pris DANS le SAVEPOINT est libéré.

La liaison au SAVEPOINT vit dans le pont, pas dans les services : le graphe
d'appels audité interdit à tout service atteint de lire ou de piloter la
transaction (`get_transaction` interdit) ; ce contrat n'a pas été affaibli.

L'UPDATE `reserved → finalized` (phase 9) et le COMMIT (phase 10) ne sont
pas touchés : le pont ne lit ni n'écrit aucune réservation (contrôlé
statiquement), aucun succès de finalisation n'est fabriqué.

## 7. Ordre des verrous

Inchangé. Prepare prend exactement les verrous historiques de la capture,
dans le même ordre (livraison → lien → AnalysisSession(s) actives → events
open + ancre, une instruction triée par id) ; Apply n'en prend aucun nouveau
sauf, comme le legacy, l'event d'ancrage déjà détenu (`append_user_
contribution`) et les FK (KEY SHARE). La vérification d'absence de la
variante préparée lit sans verrou. Aucun verrou n'est pris dans un ordre
nouveau.

## 8. Frontières d'import et de sécurité (F-01)

- `_adapters.py` n'importe pas le pont et n'est pas modifié ; aucun import
  dynamique dans `core/authz_f2` (contrôlé). Le pont importe déjà
  `_adapters` (inchangé) ; il importe désormais aussi
  `core.authz_f2.decryptage_context` (module pur, déjà chargé
  transitivement par `commands`) pour les types de revalidation.
- **Point pour C1-B** : le raccordement `_adapters.py → pont` (UPDATE B1
  après `_execute`) créera le cycle visé par F-01 ; il exigera d'extraire
  le strict nécessaire (`AdapterInvariantBroken`, `ABSENT`,
  `require_read_committed`) dans un module privé commun. Non fait en C1-A
  (aucun raccordement de production, F-01).
- Une seule Session SQLAlchemy (celle du pont) ; aucune Session, Engine ni
  Connection dans une commande ; aucun callback client ; aucun réseau ;
  propriétaire toujours `AuthorizedContext.principal.user_id` ; vues
  bornées, immuables, sans texte ni instance ORM ; aucun secret B1 (le pont
  n'en manipule aucun).

## 9. Graphe d'appels fermé (mis à jour explicitement)

`tests/test_authz_f2_pr9a_boundary.py::REACHABLE["_finalize_turn"]`
(chemin legacy du banc PR 9-A) :

- runtime cognitif : `+ _classify_user_turn`, `+ _write_user_turn`,
  `_lock_active_analysis_session` → `_lock_active_analysis_sessions` ;
- progression : `+ _require_progress_request`, `+ _apply_progress_rules`.

Nouveau `tests/test_authz_f2_pr9c1a_boundary.py::REACHABLE_C1A` :

- `_prepare_turn` → `prepare_locked_cognitive_turn`, `_classify_user_turn`,
  `_runtime_version`, `_lock_link`, `_lock_active_analysis_sessions`,
  `_conversation_anchor_event_id`, `_anchor_event_id`, `_lock_turn_events`,
  `_session_ticker`, `_event_step` ;
- `_apply_prepared_turn` → runtime (`apply_prepared_cognitive_turn`,
  `_write_user_turn`, `_append_contribution`, `_conversation_support_refs`,
  `record_turn_progress`, `_utcnow`), capture (`append_user_contribution` et
  ses gardes, `_lock_event`), progression
  (`apply_prepared_construction_these_progress`, `_require_progress_request`,
  `_visible_in_progress_session_ids`, `_apply_progress_rules`,
  `_progress_action`), livraison (`bind_locked_analysis_session`,
  `_bind_locked`, `_require_owned` et ses gardes).

Aucune fonction atteinte ne commite, n'annule, n'ouvre de transaction ou de
connexion, n'écrit de SQL textuel ; `begin_nested()` seulement dans les
trois SAVEPOINT historiques. Les méthodes des décisions (hors graphe par
fonctions) sont contrôlées séparément.

## 10. Changements explicites des contrats statiques existants

Aucune assertion retirée ; chaque changement reflète une dépendance
nouvelle et justifiée, assortie d'assertions supplémentaires :

| Test | Changement | Assertion ajoutée |
|---|---|---|
| `test_authz_f2_boundary.py::test_no_boundary_module_controls_a_transaction` (BF1, exception nominative du pont) | `begin_nested` dans `_business_savepoint`, `get_transaction` dans `_require_session_live` | un seul `with db.begin_nested()` ; aucun `commit` / `rollback` appelé dans le pont |
| `test_authz_f2_dormancy.py::IMPORT_MATRIX` | pont : `+ core.authz_f2.decryptage_context` | — |
| `test_authz_f2_pr9a_boundary.py::REACHABLE` | § 9 | — |
| `test_decryptage_cognitive_runtime.py::test_public_api_is_exact_and_keyword_only` | `+ prepare_locked_cognitive_turn`, `+ apply_prepared_cognitive_turn` | signatures exactes, paramètres mot-clé uniquement, nouvelles erreurs sous `DecryptageCognitiveRuntimeError` |
| `test_decryptage_cognitive_runtime.py::test_runtime_writes_t2_only_through_cognitive_capture_primitives` | constructions : décision, ses deux erreurs, `ValueError` / `TypeError` / `AttributeError` des gardes | seul modèle ORM instancié : le lien |
| `test_assistant_delivery.py::test_progress_module_owns_no_transaction_and_swallows_nothing` | `+ sqlalchemy` | uniquement `from sqlalchemy import inspect as orm_state` |

## 11. Tests

### Fonctionnels (PostgreSQL, `test_authz_f2_pr9c1a.py`)

- Prepare seul : uniquement des `SELECT` (dont `FOR UPDATE`), aucune ligne
  créée ni modifiée (comptes et xmin de la session et de l'event
  inchangés), aucune écriture ORM en attente ; seule la livraison
  revendiquée (phase 4) est écrite.
- Apply : toute re-sélection rendue impossible pendant Apply
  (`_lock_active_analysis_sessions`, `_conversation_anchor_event_id`,
  `_lock_turn_events`, `_classify_user_turn`, `_session_ticker`,
  `prepare_locked_cognitive_turn`, `find_active_analysis_session`,
  `apply_construction_these_progress`, `capture_user_turn` remplacés par des
  fonctions qui échouent) : succès, même session, même ancre, contribution
  à l'event exact, `answered_step` = dérivation historique après capture.
- Revalidation : contexte A accepté et appliqué ; contexte B → annulation
  locale `context_changed`, état inchangé.
- **Parité legacy / F2** (13 scénarios, deux utilisateurs dans la même
  base) : mêmes résultats tour par tour et même état final normalisé
  (sessions, events et contributions, livraisons et liens, UserStatements,
  thèses, faits).
- Plusieurs sessions actives : F2 refuse (rien n'est écrit), legacy retient
  la plus récente.
- Décision non réutilisable : après application ; après annulation locale
  (même SAVEPOINT, nouveau SAVEPOINT, runtime direct, progression) ; dans
  une autre transaction ; autre principal ; pickle / copie / affectation
  refusés ; construction directe refusée ; livraison d'autrui jamais
  préparée.

### Transactionnels (PostgreSQL)

- SAVEPOINT C1 dans la transaction du runner : premier SAVEPOINT après le
  verrou `users`, SAVEPOINT des services imbriqués dedans, `RELEASE` en
  dernier, aucun `ROLLBACK` / `COMMIT` / `BEGIN` intermédiaire, 2 COMMIT
  par appel (phase A, phase B), rien d'autre.
- Annulation locale : effets provisoires (contribution, lien, progression,
  UserStatement, livraison) effacés, écritures antérieures au SAVEPOINT
  commitées, xmin de la session et de l'event inchangés.
- Écriture en attente avant le SAVEPOINT : refus, alerte
  `decryptage_bridge_pending_writes`.
- Erreur SQL inattendue (propagée ; avalée puis annulation locale ; avalée
  puis RELEASE) : UNAVAILABLE, rien d'écrit (écriture antérieure comprise).
- Perte de la transaction externe dans le SAVEPOINT : UNAVAILABLE, rien
  d'écrit.
- Collision UNIQUE réelle dans le SAVEPOINT de
  `register_or_resolve_conversation` imbriqué : retour au SAVEPOINT du
  service seulement, SAVEPOINT C1 libéré.
- Rejeu (claim perdu) : aucune préparation, aucune écriture.

### Concurrence (PostgreSQL, `test_authz_f2_pr9c1a_concurrency.py`)

- Verrous avant le SAVEPOINT conservés après annulation locale, verrous
  pris dedans libérés (sondes NOWAIT, avec et sans verrou préalable) ;
  profil entre Prepare et Apply : session et ancre verrouillées, `users`
  `FOR NO KEY UPDATE` compatible `FOR KEY SHARE`.
- Contre un ACK legacy (deux ordres) et contre un `/decryptage` legacy (deux
  ordres) : attente constatée au rang `analysis_sessions`, une seule
  avancée, session préparée conservée ; la préparation qui a attendu relit
  l'état commité sous verrou et l'applique tel quel.
- Tentative concurrente sur une session préparée verrouillée : attente,
  jamais de seconde tentative.
- Absence préparée contredite par une session commitée : refus fermé.
- Limite (création concurrente non commitée) : figée par un test dédié.
- Chaque test : `pg_stat_database.deadlocks` inchangé et aucun refus 503
  quand un succès est attendu (un délai n'est jamais une preuve).

### Sécurité (sans base, `test_authz_f2_pr9c1a_boundary.py`)

Registre à 17 commandes et inventaire du pont à 2 opérations inchangés,
aucune commande de finalisation, primitives inatteignables depuis la
production (OPERATIONS, `_adapters`, runner, routeurs, `api.py`, frontend),
aucun accès à la réservation, bail 360 s, aucune migration, absence de cycle
`_adapters` ↔ pont et d'import dynamique, graphe fermé, structure exacte du
SAVEPOINT, politiques de sélection, règles non dupliquées, vues validées,
table de vérité de la revalidation. Dormance du vrai `api:app` et absence de
réseau à l'import : suites existantes (inchangées, vertes).

Résultats chiffrés : voir la description de la PR.

## 12. Limites connues (non résolues par C1-A)

- Les writers legacy ne sont pas coordonnés avec F2.
- **Création concurrente d'AnalysisSession quand la session est
  initialement absente** : aucune ligne n'est verrouillable ; une création
  concurrente non commitée au moment d'Apply est invisible (READ
  COMMITTED, aucune contrainte d'unicité) et deux sessions `in_progress`
  peuvent coexister, comme entre deux `/decryptage` historiques (test
  `test_known_limitation_uncommitted_concurrent_session_creation_is_not_detected`).
  Une création commitée avant Apply est, elle, détectée (refus fermé).
- Certaines mutations d'ancres par des writers legacy ; **ABA du niveau
  legacy** (`PUT /api/user/{id}/level` n'incrémente pas `level_revision`) :
  inchangés, relèvent de C1-B et au-delà.
- `_cognitive_context_matches` ne revalide que la partie cognitive (ticker,
  session active et étape, ancre, classement) ; niveau, révision, faits,
  contributions, thèse : C1-B.
- Une annulation locale vide la carte d'identité de la Session du pont
  (`expunge_all()`) : tout objet chargé avant le SAVEPOINT devra être
  rechargé (aucun dans le protocole figé, où le SAVEPOINT suit
  immédiatement l'autorisation).

## 13. Preuve d'absence d'activation

Aucun fichier d'activation touché (`api.py`, `web-v2/`, routeurs F2,
`runner.py`, `_adapters.py`, `commands.py`) ; registre de production à
17 commandes ; `bridge.OPERATIONS` à 2 opérations ; les primitives C1-A et
les nouveaux points d'entrée des services ne sont nommés par aucun module
applicatif hors du pont et des deux services ; le vrai `api:app` ne charge
ni `authz_f2` ni le pont (suites de dormance) ; le banc de test est
substitué puis restauré (objets d'origine vérifiés) à chaque test.

## 14. Réservé à C1-B

Commande `FinalizeDecryptageCandidate` et son entrée dans le registre ;
opération du pont et point d'entrée composant les phases 1 à 10 ; claim
provisoire dans le SAVEPOINT C1 ; revalidation complète du snapshot B2
(niveau, révision, faits, contributions, thèse, ABA) ; vocabulaire fermé des
refus métier (traduction de `StaleConversationContext`,
`AmbiguousActiveAnalysisSessions`, `AmbiguousOpenDecryptageEvents`,
`PreparedAnalysisSessionMismatch`, collisions) et motifs d'annulation
locale supplémentaires ; fencing du bail ; UPDATE `reserved → finalized`
dans `_adapters.py` et résolution du cycle d'import F-01 ; COMMIT unique.

## 15. Points soumis à l'audit

1. Contenu littéral de F-02 à F-04 non disponible (§ 1).
2. `LOCAL_ROLLBACK_REASONS` limité à `context_changed`.
3. Vérification d'absence préparée par lecture sans verrou (détection
   partielle, aucune attente nouvelle).
4. `answered_step` calculé en Prepare pour F2 seulement (le legacy garde son
   calcul après capture : SQL identique).
5. Exception BF1 élargie (un `with db.begin_nested()` dans le pont) et
   import `sqlalchemy.inspect` dans la progression.
