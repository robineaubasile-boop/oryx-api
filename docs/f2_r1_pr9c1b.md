# R1 PR 9-C1-B — Finalisation atomique Décrypter F2 (dormante)

## 1. Référentiel

Périmètre : **PR9-C1-B V1 + correctifs V1.1, V1.2 et addendum V1.2.1**,
subordonné au freeze global **PR9-C V1.2 + addendum V1.2.1 + F-01 à F-04**.
Ces textes ne sont pas versionnés dans le dépôt : le contrat appliqué est
celui restitué dans la demande d'implémentation (sections 0 à 18), dont les
14 décisions essentielles. Aucune architecture nouvelle n'est choisie ici ;
les écarts constatés sont listés au § 16.

## 2. Commit de base

`main` = `61e1558ceddef949854e27ca587aab3c59a06e5b` (fusion de PR9-C1-A),
**identique** au HEAD audité ; aucune divergence avec le freeze.

## 3. Parcours transactionnel

```
runner (UNE transaction externe, READ COMMITTED vérifié)
 1. autorisation : users FOR NO KEY UPDATE -> session FOR SHARE -> génération FOR SHARE -> T_authorization
_adapters._finalize_candidate
 2. candidat d'un autre principal -> ABSENT (aucune lecture)
    réservation id ET user_id FOR UPDATE -> T_lease_decision
 3. identité du tour ; HMAC d'intention (version de clé ENREGISTRÉE)
    finalized -> reconnaissance terminale (§ 11) ; blocked -> vue terminale
    reserved -> epoch, jeton, échéance (sinon CONFLICT)
 4. TransactionProbe + FinalizationReadPort (capacités scellées)
 5. bridge._execute(conn, ctx, CandidateFinalization, port)
    pont._finalize_candidate
     6. SAVEPOINT C1 (matérialisé immédiatement ; port lié UNE fois à ce SAVEPOINT)
     7. claim_delivery provisoire (created=False / collision -> annulation locale legacy_collision)
     8. Prepare (sessions actives multiples / ancre stale -> preparation_refused)
     9. port.read_preparation : _PREPARATION_CONTEXT, UNE instruction, DANS le SAVEPOINT
    10. snapshot reconstruit + sélecteur partagé == snapshot du candidat ET décision compatible
        (sinon context_changed)
    11. Apply de la décision préparée
    12. progression historique sur la session préparée
    13. flush + RELEASE SAVEPOINT
14. retour aux adaptateurs SQL
15. T_finalize (SELECT clock_timestamp()) puis UPDATE reserved -> finalized conditionnel
    (aucune ligne -> CONFLICT -> ROLLBACK GLOBAL de toutes les écritures ORM)
16. COMMIT unique par le runner
17. AUTHORIZED / DecryptageFinalized seulement après COMMIT confirmé
```

Aucun appel Claude, EODHD ou FMP, aucun second runner, aucun retry.

## 4. Inventaires

**18 commandes F2** : ReadProfile, ListPositions, ReadPosition, ListTheses,
ListThesesForTicker, ReadDelivery, ListConversationDeliveries,
UpdateProfileLevel, CreatePortfolioPosition, ReplacePortfolioPosition,
DeletePortfolioPosition, ListAnalysisOverviewPage, ListThesesPage,
ReadThesis, ReserveDecryptageTurn, RecordDecryptageAttemptFailure,
ReadDecryptagePreparationContext, **FinalizeDecryptageCandidate**
(`DECRYPTAGE_MUTATION`, `decryptage_keyed=True`, `_no_business_lock`).

**3 opérations du pont** : `TurnFinalization` et `DeliveryAcknowledgement`
(historiques, `TransactionProbe` seule), **`CandidateFinalization`**
(`FinalizationReadPort`). Table `CAPABILITIES` : capacité EXACTE par
opération.

## 5. Capacités privées

| | TransactionProbe | FinalizationReadPort |
|---|---|---|
| Implémentation | `_adapters._TransactionProbe` (scellée) | `_adapters._FinalizationReadPort` (scellée) |
| Construite par | `_transaction_probe` (opérations historiques) | `_finalize_candidate`, après autorisation ET verrou + vérification B1 |
| Liée à | connexion exacte, objet de transaction externe constaté, principal, durée d'une opération (`_close`) | idem + réservation, identité du tour, epoch, jeton, ticker résolu, paramètres de projection FIGÉS |
| Capacité | `require_live()` : identité + activité de la transaction + sonde READ COMMITTED (échoue sur transaction avortée, 25P02) | + `read_preparation(db, scope)` UNE fois |
| Refus | autre connexion, autre transaction, autre principal, opération close | + hors du SAVEPOINT C1 lié, avant son ouverture, après RELEASE, après annulation locale, SAVEPOINT de service ouvert au-dessus, autre Session, seconde lecture : **sans requête métier** |

Aucune méthode `execute`, `query`, `run` ni rappel. Scellement : contrat
dans `_bridge_contracts.py` ; une seule implémentation par contrat, définie
dans `core.authz_f2._adapters` (toute autre sous-classe : `TypeError`) ; le
pont exige la classe EXACTE enregistrée.

**Constat mesuré** : un SAVEPOINT de Session SQLAlchemy est émis
paresseusement. Le SAVEPOINT C1 est désormais matérialisé dès son ouverture
(`_BridgeSession._materialize_savepoint`) et le scope vérifie aussi le
SAVEPOINT de la connexion, pour qu'aucune relecture ne s'exécute hors de lui.

## 6. Absence d'import circulaire (F-01)

`core/authz_f2/_bridge_contracts.py` (aucun import) porte `ABSENT`,
`CONFLICT`, `IDEMPOTENCY_CONFLICT`, `AdapterInvariantBroken`,
`TransactionLost`, `CapabilityRefused` et les contrats ; `_adapters`
réexporte les MÊMES objets. Le pont n'importe plus `_adapters` ; `_adapters`
est l'UNIQUE importateur du pont. Testé : imports dans les deux ordres (et
via `runner`, `commands`) en processus neufs ; le pont seul ne charge pas
`_adapters` et n'a aucune capacité SQL.

## 7. Résultats et effets transactionnels

| Résultat | Statut runner | Effet durable |
|---|---|---|
| `DecryptageFinalized` | AUTHORIZED | livraison + lien + contribution + progression + statements / thèse / faits + réservation finalized, une transaction |
| `AlreadyFinalized` | AUTHORIZED | aucun |
| `FinalizationContextChanged` | AUTHORIZED | aucun (SAVEPOINT annulé) |
| `FinalizationPreparationRefused(reason)` | AUTHORIZED | aucun |
| `LegacyCollisionBlocked` | AUTHORIZED | réservation blocked / legacy_collision |
| `ReservationBlocked` (déjà terminale) | AUTHORIZED | aucun |
| `lease_stale`, UPDATE B1 refusé, bail expiré, epoch / jeton faux, autre tour | REVISION_CONFLICT | aucun (ROLLBACK) |
| intention divergente | IDEMPOTENCY_CONFLICT | aucun |
| candidat d'autrui, réservation absente | NOT_FOUND | aucun |
| erreur technique (SQL, invariant, clé introuvable, exception cognitive non admise, collision non prouvée, COMMIT refusé ou incertain) | UNAVAILABLE | aucun (sauf COMMIT incertain : état à constater, rejeu reconnu) |

## 8. Collisions legacy

`claim_delivery` historique (identité serveur, surface, ordinal 1, SHA
historique `decryptage_request_fingerprint`, payload candidat, métadonnées
V2). `created=False` (même SHA compris) ou `AssistantTurnIdentityCollision`
EXACTE -> annulation locale `legacy_collision`. Toute autre erreur (dont
`IntegrityError` inattendue) -> échec global. Après l'annulation : livraison
gagnante relue par l'identité complète, filtrée par le propriétaire
(collision non prouvée : 503) ; `T_finalize` ; `_BLOCK_PROVEN_COLLISION`
(epoch, compteur, jeton haché, échéance, identité, aucune livraison liée,
motif EXISTANT `legacy_collision`) ; aucune ligne -> CONFLICT. Jamais
d'adoption, aucun objet ORM du SAVEPOINT annulé réutilisé.

## 9. Revalidation B2 et sélecteur partagé

Seules `AmbiguousActiveAnalysisSessions` -> `multiple_active_sessions` et
`StaleConversationContext` -> `stale_conversation_context` (types EXACTS)
deviennent des refus de préparation ; `AmbiguousOpenDecryptageEvents`,
`CognitiveLinkInvariantError`, `PreparedCognitiveTurnUnusable`,
`PreparedAnalysisSessionMismatch` et toute sous-classe : refus technique.

Relecture : `_PREPARATION_CONTEXT` (même instruction et même code de
décision `_preparation_outcome` que B2), dans le SAVEPOINT, après Prepare.
Snapshot reconstruit par `build_snapshot` avec la méthode recalculée par
`decryptage_method_selection.select_method` (extraite de
`DecryptageGenerationOrchestrator._prepare_prompt`, appelée par B2 ET C1-B),
comparé au candidat (contenu canonique ET empreinte), PUIS
`_cognitive_context_matches` : les deux sont exigés.

## 10. Atomicité après RELEASE SAVEPOINT

L'UPDATE B1 est la seule mutation après RELEASE. Testé : horloge de décision
finale au-delà de l'échéance, et UPDATE rendu impossible — les instructions
de la finalisation (INSERT de user_statements, RELEASE SAVEPOINT) sont
journalisées, puis l'état observé par une connexion tierce est
**identique** à l'état initial (toutes tables Décrypter, réservations, xmin
compris) : REVISION_CONFLICT, jamais un objet métier committé.

## 11. AlreadyFinalized

Aucun SAVEPOINT. Vérifiés depuis PostgreSQL : propriétaire, identité,
version d'empreinte, version de clé ENREGISTRÉE (clé indisponible : 503,
jamais substituée), HMAC recalculé, epoch du candidat, état finalized,
livraison liée, ticker résolu enregistré et égal, finalized_at ; livraison
relue (id ET user_id) : identité, statut pending / delivered, version de
schéma, SHA historique recalculé avec le **niveau du candidat**. Vue sans
payload ; aucun contenu candidat ne remplace le canonique (récupération :
PR9-C2). Incohérence persistée : 503 sans réparation.

## 12. Tests ajoutés

| Fichier | Couverture |
|---|---|
| `tests/test_authz_f2_pr9c1b.py` (PostgreSQL) | nominal (ordre SQL, 2 COMMIT, effets), analyse complète jusqu'à la thèse, règles de progression, UPDATE B1 refusé après RELEASE (2 variantes), bail expiré après Prepare, erreur à chaque phase (7), exceptions cognitives non admises (6), IntegrityError inattendue, COMMIT refusé, COMMIT incertain, revalidation (niveau, révision, fait, dernier NULL, contribution, étape, thèse, méthode / session, ancre), sessions multiples, ancre stale, hors bornes, non représentable, AlreadyFinalized (11 cas), capacités périmées, ancien epoch, autre principal, intention divergente, collisions (SHA différent / identique, bail expiré, non prouvée), parité de méthode (6 bases), secrets, candidat altéré |
| `tests/test_authz_f2_pr9c1b_capabilities.py` (PostgreSQL) | scellement, surface, liaison connexion / transaction / principal, transaction avortée, refus de capacité, réservation unique, lecture dans le SAVEPOINT, 6 refus hors scope sans requête, bail expiré |
| `tests/test_authz_f2_pr9c1b_concurrency.py` (PostgreSQL) | deux finalisations concurrentes, deux candidats du même bail, gagnant legacy pendant le claim (2 SHA), contre ACK legacy (2 ordres), contre /decryptage legacy (2 ordres) |
| `tests/test_authz_f2_pr9c1b_boundary.py` (sans base) | inventaires, commande et opération, vues, SQL hors adaptateurs, contrats purs, cycle (4 ordres), second runner, constructeurs de capacités, ordre des phases, mapping des motifs, reconnaissance terminale, graphe fermé, sélecteur, projection financière, dormance |
| `tests/authz_f2_pr9c1b_support.py` | génération réelle (orchestrateur B2, transports simulés) et finalisation |

## 13. Modifications des tests historiques (exceptions nominatives)

Aucune assertion de sécurité retirée ; chaque changement reflète une
dépendance exigée par le freeze et ajoute des assertions :

| Test | Changement | Justification |
|---|---|---|
| BF1 `test_no_boundary_module_controls_a_transaction` | pont : plus de `get_transaction` ; `with` : `_business_savepoint(db, probe)` ; unique `super().connection()` dans `_materialize_savepoint` ; adaptateurs : `get_transaction` localisé dans `__init__`, `require_live`, `read_preparation` | sonde déplacée dans la capacité (SQL des adaptateurs) |
| BF1 `test_adapter_statements_are_owner_scoped_and_non_transactional` | + `_FINALIZATION_DECISION_CLOCK`, `_FINALIZE_RESERVATION`, `_BLOCK_PROVEN_COLLISION` avec filtres exacts ; colonnes écrites autorisées étendues pour la seule finalisation | UPDATE B1 et blocage prouvé du freeze |
| BF1 registre, réception de `conn` | 18 commandes ; `_require_probe` remplace `_require_live(conn, outer)` ; `bound_to(conn)` du contrat | 18ᵉ commande, capacités |
| Dormance `IMPORT_MATRIX` | arêtes nouvelles exactes (§ 6) | contrats, finalisation |
| Dormance `test_importing_the_boundary_opens_nothing` | `core.db` / `core.models` désormais chargés par l'import de la frontière ; toujours aucune connexion, aucun moteur sans `DATABASE_URL`, ni `api` ni `httpx` ; modules purs vérifiés sans `core.db` | conséquence inévitable de l'import statique `_adapters -> pont` (§ 16) |
| PR9-A boundary | seul importateur `_adapters` (import statique unique) ; 3 opérations + capacités ; ordre de `_execute` ; imports transitifs du pont ; 18 commandes | idem |
| C1-A boundary | primitives atteintes seulement par `_finalize_candidate` ; attributs du pont utilisés par les adaptateurs : liste FERMÉE ; six motifs ; structure du SAVEPOINT (sonde par la capacité, matérialisation, liaison du port) | raccordement C1-B |
| PR9-B1 / B2 boundary, runner | 18 commandes ; dispatch Décrypter + finalisation | 18ᵉ commande |
| PR9-B2 audit P1-01 | l'appel silencieux `lookup_method(..., log=False)` est suivi dans le module extrait ; la génération appelle `dms.select_method` | extraction exigée (§ 10 du freeze) |
| Bancs PR9-A / C1-A | sonde fournie par `_adapters._transaction_probe` ; signature `(db, user_id, operation, probe)` ; `_business_savepoint(db, probe)` ; capacité de l'opération de test déclarée | interface privée adaptée, assertions conservées |

## 14. Dormance

Aucune route, `api.py`, frontend, `runner.run` inchangés ; le vrai `api:app`
ne charge aucun module `authz` (testé avec variables d'activation) ;
aucune migration (21 fichiers, tête 0021) ; bail 360 s, 3 tentatives,
backoffs, motifs et états inchangés.

## 15. Limites non résolues

- **Coexistence avec les writers legacy non coordonnés** : `/decryptage` et
  l'ACK historiques ne prennent pas le verrou B1 ; leurs courses restent
  protégées par les verrous métier et la revalidation (contexte B refusé),
  pas par B1. À traiter avant activation.
- Création concurrente d'AnalysisSession initialement absente, mutations
  d'ancre legacy, ABA du niveau legacy : inchangés (C1-A).
- Le texte généré du payload candidat n'a pas d'empreinte dans le contrat
  B2 : une altération en mémoire du texte n'est pas détectable par la
  commande (les autres invariants le sont). Le candidat reste une donnée
  serveur en mémoire, jamais une preuve.
- `not_found` et `intent_mismatch` (motifs du freeze) n'ont aucun
  producteur en C1-B : la réservation et l'intention sont vérifiées sous le
  verrou AVANT le SAVEPOINT ; leur constat dans la relecture serait une
  impossibilité, traitée en 503 (jamais une absence métier). Le mapping
  figé reste implémenté.
- COMMIT incertain : UNAVAILABLE, état à constater ; récupération : C2.

## 16. Écarts signalés

1. Importer la frontière charge désormais `core.db` et `core.models`
   (conséquence directe de l'import statique `_adapters -> pont` exigé et de
   l'interdiction d'import dynamique) ; exception de test nominative et
   bornée (§ 13).
2. `_BridgeSession._materialize_savepoint` (unique `super().connection()`
   du pont) : nécessaire pour garantir que la relecture B2 s'exécute dans le
   SAVEPOINT (constat mesuré, § 5).
3. `FinalizeDecryptageCandidate` revérifie les invariants du candidat en
   rappelant ses validations (`__post_init__`) sur l'objet reçu.
