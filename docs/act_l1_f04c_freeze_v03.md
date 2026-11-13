<!--
TRANSCRIPTION — en-tête de provenance, hors texte normatif.

Document : contrat ACT-L1 F04-C V0.3, transcrit tel que transmis par le fondateur le 13/11/2026 dans la session
d'implémentation (fichier « Oryx_ACT-L1_F04-C_Freeze_Candidat_V0.3.md »).

Validation : le fondateur a explicitement validé le freeze limité F04-C V0.3 le 13 novembre 2026 (message de
lancement de l'implémentation du 13/11/2026) ; selon ce même message, les mentions « candidat » et « validation en
attente » du texte décrivent son état AVANT cette validation, et le texte technique est figé sans autorisation
d'élargir son périmètre.

Règles de transcription : texte reproduit sans reformulation, correction ni ajout, y compris la ligne de statut
interne et les coquilles éventuelles. Seul cet en-tête commenté est ajouté. Empreinte SHA-256 du fichier transmis :
f992436b174898263a1cb11e6053d53b18e925ec7fc5140bdea308d30d4f86d5. En cas de divergence avec toute autre documentation du dépôt (traçabilité, implémentation), le
présent texte prévaut.

Document associé : docs/act_l1_f04c_traceability_v03.md (exigences -> implémentation -> tests -> preuves, écarts).
-->

# ORYX INVEST — ACT-L1 / F04-C V0.3
## Contrat architectural candidat : composition ASGI dormante, lifespan et suivi des travaux

**Statut : CANDIDAT AU FREEZE — validation du texte intégral par le fondateur en attente.**  
**Date de préparation :** 13 novembre 2026  
**Dépôt source :** `robineaubasile-boop/oryx-api`  
**Référence GitHub auditée :** `82378d731cbd1a9ce688217bac0f426575d8062c` (`main`, revérifié le 13/11/2026)  
**Références normatives existantes :** `docs/act_l1_f04a_architecture_v03.md`, `docs/act_l1_f04b_freeze_v03.md`, `docs/act_l1_f04b_resources_v03.md`, politique F-02A V0.2.  
**Qualification préalable :** rapport transmis `RAPPORT_F04C_Q1_Q2_V0.1.md` ; Q1 = **NO-GO-A**, Q2 = **ÉCART À ARBITRER**, preuves expérimentales brutes non encore conservées dans le dépôt.

> Les choix **B** (suivi explicite des jobs), **double registre** et **Q2/O1** (moteur legacy inerte encadré) ont été approuvés par le fondateur. **Ce texte organise leur implémentation ; il n'est pas encore un ordre d'implémenter.**

## 1. Finalité, statut et frontière

F04-C assemble un **nouveau point d'entrée ASGI indépendant et non servi** pour Auth F2 et Personnel F2 ; possède le cycle de vie des ressources F04-B ; maintient une admission F2 fermable ; supervise **les requêtes admises ET les travaux physiques** susceptibles de survivre à leur requête. F04-C **ne constitue ni une activation de F2, ni un basculement de production**.

**Interdictions :** aucune modification des décisions métier, de l'autorisation, de l'idempotence, des transactions, de C3/B1/C2, des corps et statuts métier HTTP, des chemins ou méthodes de routes ; aucune migration, modification de secrets, modification ou redéploiement manuel Railway, activation de variable, changement du `api:app` actuellement servi, suppression du legacy, ou fusion de PR sans décision du fondateur. Aucun repli vers `api.py`.

La sécurité HTTP de provenance, la confiance des IP de proxy et P1 restent en **F04-D** ; les délais de plateforme, signaux, COMMIT incertain, reprises et P2 restent en **F04-E**. F-05 est le frontend séparé et F-03 le basculement opérationnel. Éducation/Coach futurs ne sont **pas** ajoutés par ce lot : les anciennes routes restent régies par F-02A.

## 2. Périmètre autorisable d'une future PR F04-C

**Nouveaux composants :** un point d'entrée dormant (chemin proposé : `core/f2_runtime/entrypoint.py`) ; une enveloppe d'état/lifespan (peut être dans le même module) ; un registre de travaux et un soumetteur explicite (chemin proposé : `core/f2_runtime/work_registry.py`) ; tests dédiés `tests/test_f04c_*.py` et rapport de traçabilité documentaire F04-C. Le nom exact des nouveaux fichiers peut varier si justifié avant PR ; leurs contrats ne peuvent pas varier silencieusement.

**Modifications minimales autorisées pour l'injection du soumetteur commun :**

- `core/auth_f2_http/isolated.py`, `core/auth_f2_http/routes.py` ;
- `core/authz_f2_http/isolated.py`, `core/authz_f2_http/routes.py` ;
- `core/authz_f2_http/portfolio.py`, `core/authz_f2_http/consultation.py`, `core/authz_f2_http/decryptage.py`.

Ce sont les **seuls sept fichiers existants** dont la modification est autorisée, et uniquement pour l'injection technique du soumetteur et la substitution des **sept points** `run_in_threadpool` constatés. Toute modification supplémentaire d'un fichier existant nécessite un nouvel arbitrage écrit. Les contrats et signatures des compositions isolées utilisés par les tests historiques doivent rester compatibles ; la composition F04-C, elle, doit **exiger** un soumetteur explicite et partagé, jamais accepter le chemin de compatibilité non supervisé.

**Fichiers notamment interdits :** `api.py`, `core/db.py`, `core/models.py`, `core/route_policy/*`, `core/f2_runtime/{config,database,resources,preflight}.py`, services/runner/adaptateurs métier F2, worker, Alembic, `requirements*`, frontend et fichiers Railway. Aucun changement aux invariants `_TurnCapacity` / `_TurnSlot` C3 (deux places, propriété et libération une fois).

## 3. Point d'entrée et politique de routage

1. Le nouveau module ASGI exporte une application importable par Uvicorn **sans lire les secrets, ouvrir de connexion, lancer de thread, construire de moteur, ni importer `api` ou la composition personnelle** à l'import du point d'entrée. Une instance d'enveloppe inerte peut être construite sans effet externe.
2. Au démarrage uniquement, configuration F04-B validée **avant tout import susceptible de charger indirectement `core.db`** ; composition F2, ressources et précontrôle ont lieu pendant le lifespan. Le sous-processus doit prouver `"api" not in sys.modules`.
3. Chaîne lorsque prête : `Uvicorn → enveloppe F04-C → RoutePolicyGate (F-02A inchangée) → Auth F2 / Personnel F2`. La porte reste l'unique autorité de classification des routes. L'enveloppe peut utiliser la fonction **existante** `classify` pour reconnaître une décision `FORWARD_F2_*`, jamais implémenter une seconde politique ni modifier les scopes.
4. Vérification **d'égalité exacte**, pas seulement d'inclusion : `registry.pairs_of(F2_AUTH)` = **14** couples effectivement exposés par Auth ; `registry.pairs_of(F2_PERSONAL)` = **13** par Personnel **avec C3 obligatoire**. Composition manquante, route supplémentaire, docs, CORS ou application legacy : échec fermé du démarrage.
5. Les classes/compositions existantes sont réutilisées : `build_isolated_f2_app`, `build_isolated_personal_app` et `DecryptageHttpConfig` complet, construit avec `config.decryptage_keyring`, `resources.eodhd/fmp/claude`, `config.decryptage_model` et l'horloge admise ; le moteur F04-B est partagé, ainsi que **les mêmes objets** Keyring Auth et CSRF. `AuthorizedTransactionRunner` demeure le seul autoriseur personnel.
6. IP : injecter explicitement un `ip_resolver` existant ; aucun en-tête de proxy n'est déclaré fiable par F04-C. Le résolveur provisoire de pair direct, si retenu pour la composition dormante, **ne valide pas P1** et devra être revu par F04-D.
7. Sur `404`, `405`, legacy `410`, ressources documentaires, méthodes refusées, websocket `1008` et `GET /health`, déléguer à la porte sans changement **octet pour octet** des réponses F-02A pertinentes (en-têtes inclus). `GET /health` demeure `200 {"status":"ok"}` même en maintenance et n'est **pas** une readiness probe.

## 4. État de l'enveloppe et admission

États logiques : `INERT → STARTING → OPEN → DRAINING → CLOSED` ; échec de démarrage = état terminal fermé, `startup.failed`, jamais `OPEN`. Transitions synchronisées, non réentrantes et observables. Des appels concurrents ou répétés de lifespan ne produisent ni démarrage, ni fermeture, ni réponse terminale en double.

- **INERT / STARTING / DRAINING / CLOSED :** aucune **nouvelle** admission F2 ; pour une décision F-02A `FORWARD_F2_AUTH` ou `FORWARD_F2_PERSONAL`, répondre exactement **HTTP 503** avec corps ASCII JSON `{"error":"temporarily_unavailable"}`, en-têtes no-store de même politique, sans cookie, sans CORS, sans redirection ; `HEAD` avec corps vide si applicable. Le refus précède toute lecture du corps HTTP, tout appel fournisseur et toute opération en base.
- **OPEN :** les requêtes F2 sont admises atomiquement ; l'admission existe **avant** tout `await` de lecture/contrôle/traitement et reste valide jusqu'à la sortie de l'appel ASGI aval (réponse ou annulation). La transmission à la porte garde `scope`, `receive`, `send` identiques, hors protections déjà appliquées par F-02A.
- **Passage à DRAINING :** interdit atomiquement de nouvelles admissions, laisse les anciennes requêtes terminables et leurs jobs déjà autorisés soumettre/terminer dans le cadre de leur jeton ; aucun HTTP déjà admis n'est converti rétroactivement en refus de maintenance. Une admission fermée n'est pas un signal d'annulation des transactions en cours.
- **Suivi des admissions :** exactement un jeton par appel ASGI F2 admis, libéré une seule fois dans un `finally` même sous `asyncio.CancelledError` (qui n'est pas un `Exception` ordinaire). Une admission ne peut soumettre aucun travail après sa libération.

## 5. Registre des jobs physiques et soumetteur B

Un **seul registre physique thread-safe** appartient à l'instance de l'enveloppe. Le **même soumetteur** est injecté dans Auth F2 et Personnel F2 (et C3), aux sept points actuels :

1. `core/auth_f2_http/routes.py:268` ;
2. `core/authz_f2_http/routes.py:301` ;
3. `core/authz_f2_http/routes.py:346` ;
4. `core/authz_f2_http/portfolio.py:232` ;
5. `core/authz_f2_http/consultation.py:143` ;
6. `core/authz_f2_http/decryptage.py:344` (`/turns`) ;
7. `core/authz_f2_http/decryptage.py:825` (`/replay`, `/ack` via un même site).

**Protocole obligatoire pour chaque soumission :**

- `REGISTERED` : le jeton d'admission vivant et le travail sont inscrits **avant** toute soumission à AnyIO, atomiquement vis-à-vis du drainage. Un soumetteur sans jeton vivant refuse sans lancer de fil. Une soumission pendant DRAINING reste permise **uniquement** si son admission avait déjà été acceptée et demeure vivante.
- `REGISTERED → STARTED` : la **première instruction exécutée par le wrapper du fil** réclame atomiquement le démarrage. Si le travail avait été `ABANDONED`, **aucun effet métier/fournisseur/SQL n'est permis** ; le callable n'est pas exécuté.
- `STARTED → FINISHED` : seule une clause `finally` dans le **fil physique** peut marquer la fin, après le retour/échec réel du callable. Annuler l'attente HTTP ne termine jamais le job.
- `REGISTERED → ABANDONED` : l'attente HTTP annulée, échouée ou sortie prématurément peut abandonner un job **seulement avant** `STARTED` ; cette transition vaut preuve atomique de non-démarrage effectif. Si le fil a déjà pris `STARTED`, l'abandon est sans effet : sa terminaison relève du fil.
- `ABANDONED` et `FINISHED` : états terminaux exclusifs ; inscription, terminaison et retrait sont effectués une fois, avec comptage non négatif ; double finalisation, double libération et résultat perdu sans suivi sont interdits.
- Tous les chemins, y compris erreur de création/soumission, épuisement de pool, cancellation native, `BaseException` et exceptions fournisseur, doivent produire une transition terminale correcte ; ne pas transformer une annulation de l'appelant en réponse métier prétendument réussie.

**Atomicité inter-registres :** une barrière logique commune protège les invariants suivants : (i) une soumission n'est valide qu'avec une admission non libérée ; (ii) acquisition/libération d'admission, inscription/abandon/fin du job et transition DRAINING sont correctement ordonnés ; (iii) le prédicat **`admissions_actives == 0 ET jobs_non_terminaux == 0`** est évalué de façon atomique, sans fenêtre permettant une soumission ultérieure. L'implémentation doit prouver cette propriété, qu'elle utilise une serrure commune ou une synchronisation équivalente. La notification du fil au drainage doit être thread-safe, sans bloquer la boucle événementielle.

**C3 :** `_TurnCapacity` / `_TurnSlot` restent **indépendants** et conservent leur limite de deux orchestrations. Le wrapper du registre peut marquer `STARTED` avant l'appel du travail C3, mais `slot.start()` reste le garde **avant B1** ; si le slot a été abandonné, aucune orchestration B1 ni appel fournisseur n'est effectué. `slot.finish()` et `slot.abandon()` gardent exactement leur sémantique existante. Le nouveau suivi ne remplace ni la capacité C3, ni la réservation durable B1.

**Compatibilité :** les compositions isolées hors F04-C et leurs tests peuvent conserver l'ancien soumetteur par défaut. Cette voie n'est **jamais autorisée en production F04-C** : la construction F04-C échoue si l'une des deux compositions n'utilise pas exactement le soumetteur enregistré.

## 6. Lifespan, construction, précontrôle, fermeture

**Ordre exigé au démarrage :**

1. Recevoir `lifespan.startup` ; état `STARTING`, admissions fermées.
2. Charger et valider strictement `load_f2_config(copie_de_os.environ)` : aucun secret de secours, aucune mutation de l'environnement. Une `DATABASE_URL` invalide doit être rejetée **avant** que l'import indirect de `core.db` puisse construire un engine legacy. Les imports du point d'entrée ne doivent pas contourner cet ordre.
3. Construire **une** instance `F2Resources` via `build_f2_resources(config)` ; ressources possédées une fois, seul moteur F2 configuré et partagé. Les échecs partiels sont nettoyés par le propriétaire F04-B.
4. Exécuter `run_f2_preflight(resources.engine, config.keyring)` : lecture seule, tête Alembic, tables/colonnes, version PostgreSQL qualifiée, isolation et état anti-abus. **Aucune migration** ni initialisation d'état.
5. Construire les deux compositions isolées avec C3 et le soumetteur commun, vérifier **14/13**, aucune documentation ni CORS ; construire `RoutePolicyGate` inchangée.
6. Refuser tout lifespan aval **actif** non pris en charge : les compositions FastAPI actuelles avec `_DefaultLifespan` implicite et listes `on_startup/on_shutdown` vides sont acceptées ; un `lifespan=` explicite ou un gestionnaire startup/shutdown (y compris via routeur inclus) entraîne un échec de construction. Le critère de détection doit être testé avec les versions FastAPI/Starlette verrouillées ; ne pas refuser tous les `lifespan_context` indistinctement.
7. Après succès total : publier atomiquement le gate prêt, état `OPEN`, puis envoyer **un seul** `lifespan.startup.complete`.

Tout échec : `lifespan.startup.failed` **une seule fois**, ressources construites fermées via `F2Resources.close()` (ou rapport de nettoyage de l'échec F04-B), aucun F2 accepté. Aucune erreur de démarrage ne divulgue URL, secret ou token. Ne pas exécuter silencieusement un lifespan aval : le refuser.

**Arrêt gracieux :** recevoir `lifespan.shutdown` → DRAINING atomique → refuser les admissions nouvelles → attendre **conjointement** la fin de toutes les admissions et de tous les jobs physiques → seulement alors `F2Resources.close()` → vérifier le rapport de fermeture → état `CLOSED` → `lifespan.shutdown.complete`. Si la fermeture rapporte un échec, ne pas déclarer une fermeture gracieuse réussie ; émettre un échec observable, sans divulguer de détails secrets.

**Délai de drainage dépassé :** état distinct `DRAIN_TIMEOUT` (résultat, pas forcément un nouvel état) ; **ne jamais fermer les ressources** tant que des jobs ou admissions peuvent les utiliser ; ne jamais annoncer `shutdown.complete` **comme fermeture gracieuse**. Le mécanisme technique de notification ASGI d'un shutdown dépassé et les valeurs opérationnelles de délai sont soumis à qualification F04-E ; aucun délai Railway/Uvicorn n'est inventé par F04-C. Un signal forcé ou `SIGKILL` peut empêcher tout lifespan : aucune garantie de cleanup après perte de processus n'est revendiquée.

**COMMIT incertain :** une erreur HTTP, une déconnexion, `500`, timeout, `SIGTERM` ou `SIGKILL` ne prouvent ni réussite ni annulation de la transaction. F04-C ne lance aucune compensation et aucun rejeu automatique ; seule la relecture de l'état durable par B1/C2 et les mécanismes métiers existants autorisent la reprise.

## 7. Arbitrage Q2/O1 — moteur SQLAlchemy legacy

Le chemin d'import `isolated → runner → _adapters → _decryptage_orm_bridge → assistant_delivery → interaction_identity → models → db` charge `core.db`. Avec `DATABASE_URL`, ce module crée un objet engine historique à l'import. Q2 a observé **zéro connexion legacy et zéro usage du legacy** lors des appels F2 qualifiés ; le moteur F04-B est celui employé par Auth et Personnel.

**Décision O1 pour F04-C :** tolérer **un objet engine legacy inerte**, sans modifier `core.db`, `core.models` ou l'environnement du processus, **uniquement** avec preuve reproductible qu'aucune connexion, session ou transaction F2 n'utilise `core.db.engine`, `SessionLocal` ou `get_db`. Il ne devient jamais une ressource F04-C, ni un moteur de secours. Test statique d'absence d'accès direct et test dynamique d'attribution des connexions aux moteurs, y compris sur erreurs et sur startup avorté. Toute utilisation legacy, ouverture implicite de connexion ou accès hors provenance démontrée = NO-GO.

`load_f2_config` **avant import de composition** empêche qu'une URL mal formée atteigne ce chemin avant validation F04-B. L'éventuelle extraction de `Base` est reportée à un chantier séparé, jamais intégrée discrètement à F04-C.

## 8. Tests et preuves obligatoires avant un merge F04-C

**A. Composition et dormance**
- Import du point d'entrée en sous-processus vierge : pas de connexion, de secret lu, d'engine, de `api` dans `sys.modules` ; aucune incidence sur `api:app`, worker, Railway.
- `startup.failed` sur config invalide (dont URL), clés incompatibles, construction fournisseur avortée, PG hors contrat, migration/état anti-abus invalide, composition partielle, 13/14 incomplets, C3 absent, documentation/CORS, lifespan aval explicite ; fermeture des ressources possédées une fois.
- Deux compositions réelles, **même** engine, mêmes instances de clés requises et même soumetteur ; exactement 14/13 couples ; F2 complet inerte jusqu'au signal startup réussi ; absence de route ou de repli legacy.
- Contrôles de compatibilité FastAPI `_DefaultLifespan` implicite accepté / gestionnaire ou lifespan explicite refusé.

**B. Routage et maintenance**
- Matrice F-02A exhaustive (410/404/405/health, méthodes inhabituelles, raw_path non canonique, GET/HEAD, en-têtes et corps comparés **octet pour octet**) en OPEN et DRAINING ; CORS jamais ajouté.
- F2 hors OPEN : **503 exact**, no-store, sans lecture de corps/SQL/fournisseur ; autres routes inchangées ; `/health` 200 sans promettre la readiness.
- Admission + maintenance simultanée : une requête admise avant DRAINING poursuit son contrôle/traitement ; nouvelles admissions refusées ; aucune admission oubliée ou libérée deux fois.

**C. Registres et annulations**
- Rejouer **Q1-01 à Q1-10** avec approche B (normal, avant soumission, file AnyIO, fenêtre après test de cancellation, fil actif, RST TCP, `/turns` `/replay` `/ack`, Auth/Personnel en concurrence, exceptions fournisseur, admission/shutdown, SIGTERM réel et double SIGINT).
- Couvrir explicitement : requête admise **sans thread**, bloquée sur corps HTTP ; passage à DRAINING ; libération de la requête ; fermeture impossible avant libération de l'admission, même quand `jobs=0`.
- Couvrir : thread démarré, appel HTTP annulé, admission retirée ; `jobs>0` jusqu'à `WORK_FINISHED`, `pool.checkedout()>0` observable ; aucune fermeture prématurée.
- Couvrir : annulation entre `REGISTERED` et exécution ; `ABANDONED` prouve non-démarrage ; callable non invoqué ; aucun commit tardif d'un travail abandonné.
- Couvrir : concurrence entre `start`, `abandon`, `finish`, DRAINING et deux notifications terminales : jamais de double terminal, compteur négatif, travail invisible, ou barrière de fermeture franchie avant la fin réelle.
- Vérifier les 7 points nominativement ; tests négatifs avec soumetteur non instrumenté et fil brut : le banc doit détecter les violations, pas les ignorer.
- `/turns` garde sa capacité 2 et toutes les preuves C3/B1/C2 : acquisitions, abandons, libérations, B1 avant fournisseur, replay et ack.
- Sur vrai SIGTERM avec timeout Uvicorn fini, un dépassement reste un **délai dépassé**, jamais une fausse fermeture gracieuse ; si double SIGINT saute le lifespan, constater honnêtement l'absence de garantie ; aucune prétention de résoudre P2 avant F04-E.

**D. SQL, versions et régressions**
- Répéter Q2-01 à Q2-06 avec instrumentation construite **avant imports** et sous-processus distincts : compte exact engines/connexions F2 vs legacy, sessions ORM, nettoyage au démarrage avorté, URL invalide refusée avant import dangereux ; aucune modification globale d'environnement.
- Couvrir PostgreSQL **16 et 18** par CI ou banc reproductible avant merge ; le rapport pré-freeze Q1/Q2 n'a testé que 16.15. En l'absence de test PG18, le critère expérimental requis reste **NON QUALIFIÉ**.
- CI des tests existants, import et démarrage legacy, bornes C3, frontière PR 9-B et dormance F04-B/F-02A. Aucune dépendance nouvelle ou changement de lock sans arbitrage.
- Fournir chronologies synthétiques et **banc d'essai reproductible** pour Q1 et Q2 (tests versionnés et instructions exécutables, ou preuves archivées), SHA exact de PR et versions verrouillées. Un rapport narratif seul ne vaut pas exécution indépendante.

## 9. Acceptation / refus de la PR

**GO d'audit** seulement si : diff limité au périmètre §2, composition complète dormante, égalité 14/13, aucun import/usage de `api`, aucun moteur legacy utilisé, deux registres atomiquement cohérents, sept soumissions couvertes, arrêts et annulations qualifiés, zéro ressource fermée tant qu'un travail peut encore l'utiliser, réponses F-02A inchangées, tests PG16/18 et suites historiques satisfaisants, preuves reproductibles, aucune activation Railway.

**NO-GO immédiat** : une seule écriture F2 commite après que le suivi a été déclaré vide et les ressources fermées ; un thread peut démarrer non suivi ; le seul compteur de requêtes ou seul compteur de jobs décide la fermeture ; C3 perd son garde ou ses deux places ; un F2 utilise le moteur legacy ; une route modifiée, une migration, un secret exposé ou un lifespan aval actif silencieusement ignoré ; P1/P2 présentées comme résolues ; la PR active le nouveau point d'entrée ; tests indispensables non exécutés présentés comme PASS.

Si un test PG18 ne peut pas tourner dans l'environnement Claude, il doit être **annoncé non qualifié**, puis exécuté dans un environnement adapté avant décision de merge, sans simuler son succès.

## 10. Réserves non résolues, sans extension tacite de F04-C

- **P1 / F04-D :** origine et confiance du client IP derrière Railway, Host/Origin/Referer/Sec-Fetch-Site, décisions fondateur ultérieures.
- **P2 / F04-E :** délai de grâce Uvicorn, comportement réel Railway SIGTERM/SIGKILL, arrêt forcé, COMMIT incertain, reprises, limitation du pool en conditions de production.
- **Traçabilité des preuves :** scripts et JSON expérimentaux Q1/Q2 localisés dans le scratchpad Claude ; les conserver ou recréer en tests versionnés avant de prétendre à la reproductibilité.
- **Confidentialité des logs :** `core/decryptage_progress.py` comporte des `print` d'identifiants utilisateur/ticker/session ; constat confirmé dans GitHub, **correctif séparé requis avant activation F2**, sans modifier ce fichier dans la PR F04-C.
- **Éducation / Coach :** architecture pédagogique disponible, mais intégration F2 distincte, hors F04-C ; aucune route ajoutée par anticipation.

## 11. Workflow après validation du texte par le fondateur

1. Validation explicite du présent **freeze limité F04-C V0.3** (ou modifications demandées).
2. Prompt Claude Code Opus 5.5 strictement limité à ce texte, à ce périmètre, avec **PR sans merge ni déploiement**.
3. Audit indépendant ChatGPT du diff, des tests, de la traçabilité et des cas de course ; GO/NO-GO.
4. **Merge exclusivement par le fondateur** si GO et qualification expérimentale acquise.
5. Vérification Railway du legacy non impacté, sans activation F2 ; puis F04-D et F04-E dans leurs freezes séparés.

**Fin du candidat. Aucune autorisation d'implémentation n'est déduite de sa seule rédaction.**
