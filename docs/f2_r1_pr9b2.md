# R1 PR 9-B2 — Préparation pédagogique, fournisseurs et candidat interne Décrypter F2 (dormante)

## 1. Références normatives et commit de base

- Architecture normative : **PR9-B2 V1.1 — freeze architectural validé par le
  fondateur le 02/11/2026**, reproduit dans la demande d'implémentation. Les
  documents PR9-B V1 / V2 / V2.1 et E1 / E1.1 / E1.2 ne sont pas versionnés :
  seuls les contrats documentés dans le dépôt (`docs/f2_r1_pr9a.md`,
  `docs/f2_r1_pr9b1.md`, `docs/f2_r1_pr8.md`) et le freeze V1.1 ont été
  appliqués ; une conformité littérale à ces documents non versionnés ne peut
  pas être vérifiée.
- Base : `main` = `3c8f62e68c8f7f44d6facf052468dcae8a405b39` (merge de la PR
  #230, R1 PR9-B1), identique au SHA de contrôle ; aucune divergence.
- Tête Alembic inchangée : `0021_decryptage_reservations` ; **aucune
  migration**.

## 2. Périmètre exact

| Élément | Fichier |
|---|---|
| Vue de préparation fermée (pure) | `core/authz_f2/decryptage_context.py` (nouveau) |
| Commande READ `ReadDecryptagePreparationContext` | `core/authz_f2/commands.py` |
| Instruction unique `_PREPARATION_CONTEXT`, adaptateur `_read_preparation_context`, registre à 17 | `core/authz_f2/_adapters.py` |
| Identification de l'instrument (pure) | `core/authz_f2/decryptage_instrument.py` (nouveau) |
| Politique financière EODHD / FMP (pure) | `core/authz_f2/decryptage_market_data.py` (nouveau) |
| Snapshot, validation de la réponse, candidat | `core/authz_f2/decryptage_candidate.py` (nouveau) |
| Orchestrateur et budget | `core/authz_f2/decryptage_generation.py` (nouveau) |
| Transports réels EODHD / FMP / Anthropic (dormants) | `core/decryptage_providers.py` (nouveau) |
| Docstring du paquet | `core/authz_f2/__init__.py` |
| Drapeau mot-clé `log` de `lookup_method` (défaut historique, correctif d'audit P1-01) | `core/pedagogie_library.py` |
| Tests | § 18, § 22 |

Non livré (interdit par le freeze) : route publique ou F2 Décrypter, ACK F2,
modification de `/decryptage` historique ou du frontend, `AssistantDelivery`,
`claim_delivery`, finalisation, écriture de `assistant_delivery_id` ou de
`resolved_ticker`, écriture d'`AnalysisSession`, `UserStatement`,
`AnalysisFact`, `InvestmentThesis`, `CognitiveEvent`, `SupportTrace`,
progression, observation T3, inférence, migration, worker, cron, file,
modification des règles de bail B1 ou des contrats PR9-A, paramètre Railway.
`api.py`, `core/data_fetcher.py`, `core/ticker_resolver.py`,
`core/decryptage_engine.py`, `core/decryptage_progress.py`,
`core/decryptage_cognitive_runtime.py`, `core/assistant_delivery.py`,
`core/models.py`, `web*`, `alembic/` : **inchangés**.
`core/pedagogie_library.py` : seul ajout d'un paramètre mot-clé `log=True`
(§ 22, P1-01) ; comportement historique et appelants inchangés.

## 3. Interfaces nouvelles

```python
DecryptageGenerationOrchestrator(runner, *, eodhd, fmp, claude, model, clock=time.monotonic,
                                 price_recent_max_age=None)
    .generate(credential, reservation: ReserveDecryptageTurn, reserved: AuthorizationOutcome) -> GenerationOutcome

GenerationOutcome(status, reason, candidate, declaration)
    status ∈ {generated, refused, provider_failed}
```

Transports (protocoles injectés, simulés dans les tests) : `eodhd.search /
fundamentals / realtime(arg, *, timeout_s)`, `fmp.profile / ratios(symbol, *,
timeout_s)`, `claude.create_message(*, model, system, user, max_tokens,
timeout_s) -> {"stop_reason", "content"}`. Ils lèvent une exception en cas
d'échec (TimeoutError compris) et rendent `None` pour une ressource
introuvable. Implémentation réelle : `EodhdTransport`, `FmpTransport`,
`ClaudeTransport` (`core/decryptage_providers.py`), jamais importée par un
module applicatif.

## 4. Nouvelle commande READ

`ReadDecryptagePreparationContext(reservation_id, lease_epoch, lease_token,
conversation_key, client_turn_id, ticker_input, question, context,
last_method_id, resolved_ticker)` — type exact, gelée, sans `user_id`, sans
callable, Session ni connexion ; admission complète à la construction (mêmes
règles que `ReserveDecryptageTurn` pour les entrées, ticker résolu
`^[A-Z0-9][A-Z0-9.-]{0,31}$`) ; `from_lease(lease, reservation,
resolved_ticker)` exige le même tour ; `repr()` masque le jeton ;
non sérialisable.

Spécification : plan **READ inchangé** (users `FOR KEY SHARE`, session et
génération `FOR SHARE`), `_no_business_lock`, `decryptage_keyed=True` : le
trousseau Décrypter est transmis par le **dispatch existant** du runner pour
revérifier l'intention B1 (aucune empreinte équivalente inventée, aucun
paramètre nouveau du runner).

Décision (une instruction, puis Python) :

| Constat | Résultat |
|---|---|
| réservation absente ou d'autrui (`id` ET `user_id`) | `NOT_FOUND` |
| autre tour (conversation, `client_turn_id`), état ≠ `reserved`, epoch ≠, jeton ≠ (temps constant), `read_at ≥ lease_expires_at` | `REVISION_CONFLICT` |
| HMAC recalculé (versions enregistrées) ≠ enregistré | `IDEMPOTENCY_CONFLICT` |
| version / clé introuvable, profil, conversation, ancre, provenance incohérents, lignes d'autrui rattachées à la session active, panne SQL | `UNAVAILABLE` (alerte `decryptage_preparation_incoherent` ou erreur générique) |
| deux sessions `in_progress` pour ce ticker | `DecryptagePreparationRefused(multiple_active_sessions)` |
| étape ouverte absente / inconnue / `swot_final` | `…(active_session_step_invalid)` |
| ancre au même ticker, event non `open` ou tentative non active | `…(stale_conversation_context)` |
| contenu hors bornes | `…(context_out_of_bounds)` |
| fait NaN / ±infini | `…(context_value_invalid)` |
| sinon | `DecryptagePreparationView` |

Aucune écriture (vérifié : état complet et `xmin` inchangés).

## 5. Registre à 17 commandes

Seize commandes B1 + `ReadDecryptagePreparationContext`. Aucun plan
nouveau ; trois commandes reçoivent le trousseau Décrypter (deux mutations
B1, une lecture B2). Tests épinglés mis à jour nominativement (16 → 17) :
`test_authz_f2_boundary.py`, `test_authz_f2_runner.py`,
`test_authz_f2_pr9a_boundary.py`, `authz_f2_pr9a_support.py`,
`test_authz_f2_pr9b1_boundary.py` ; aucun test supprimé ni affaibli.

## 6. Stratégie de lecture cohérente

`_PREPARATION_CONTEXT` est **une seule instruction** (CTE `o`, `r`, `p`, `cv`,
`an`, `ae`, `asx`, `act`, `sel`, `fa`, `st`, `th`) : sous READ COMMITTED,
une instruction voit un seul instantané ; tout le contexte (réservation,
`clock_timestamp()`, profil, conversation, ancre, session active, faits,
contributions, thèse) est donc lu sous le **même** instantané. Deux lectures
(avant et après les données financières) sont deux instructions, deux
transactions courtes distinctes : **aucune garantie n'est prétendue entre
elles** ; leur égalité (`context_key()`, tout sauf `read_at`) est exigée,
sinon `context_changed`.

Propriété : unique paramètre `:u` propagé par `o.u` ; chaque table filtrée
par ce propriétaire serveur (inventaire nominatif dans
`tests/test_authz_f2_boundary.py`) ; les lignes d'autrui rattachées à la
session active sont seulement **comptées** (refus fermé), jamais lues.

Ancre R1-C4 : même requête que
`decryptage_cognitive_runtime._conversation_anchor_event_id` (dernière
livraison V2 `delivered` + `captured`, même ordre, jamais plus loin) ; même
sémantique que `precheck_conversation_context` (autre ticker = navigation,
jamais stale). Différences volontaires : event et session d'ancrage exigés
du même propriétaire ; plusieurs sessions actives = refus (le legacy prend la
plus récente, inchangé).

Bornes (dans la projection SQL, aucune troncature) : 2 sessions actives lues
au plus ; 65 faits / contributions lus pour 64 admis ; étape ≤ 32 octets ;
type de fait ≤ 64 octets ; contribution ≤ 16 Kio ; total des contributions
≤ 64 Kio ; thèse ≤ 32 Kio ; date de thèse représentable (années 1 à 9999).
Ordres : faits `fact_date ASC, id ASC`, contributions `statement_date ASC,
id ASC`, thèse `created_at DESC NULLS LAST, id DESC` (ordre PR8 ; le legacy
`_get_latest_thesis` trie `created_at DESC` sans `NULLS LAST`, inchangé).
Flottants lus par `float8send`.

## 7. Snapshot et provenance

`PreparationSnapshot` (`oryx-decryptage-preparation-snapshot-v1`) = contexte
**réellement injecté** : tour (réservation, epoch, conversation, tour),
propriétaire, ticker saisi et résolu, `(level, level_revision)`, méthode et
base (`active_analysis`, `initial_turn`, `lookup`), ancre, session active et
étape ouverte, faits utilisés (DERNIÈRE ligne par type injecté, même NULL
— règle historique du dict ; une dernière valeur NULL est rendue `N/D` dans
le prompt, jamais une valeur antérieure ; § 22, P1-02), contributions
utilisées (étape et texte non vides), thèse rappelée (tour initial
seulement), identifiants inclus.

Empreinte : `SHA-256("ORYX-DECRYPTAGE-PREPARATION-SNAPSHOT\0" ||
JSON canonique)` — clés triées, séparateurs compacts, UTF-8 sans échappement,
NaN interdit, UUID texte, flottants `{"binary64": "<binary64-decimal-v1>"}`.
Empreinte financière séparée (`ORYX-DECRYPTAGE-MARKET-DATA\0`).

**Limite ABA** : `PUT /api/user/{user_id}/level` n'incrémente pas
`level_revision` ; une modification suivie d'un retour à la valeur initiale
n'est pas détectable (test `test_an_aba_level_change_is_invisible_to_b2_and_documented`).
Le snapshot permet la revalidation ; il ne garantit pas l'absence de
modification concurrente. La stratégie de verrouillage et de revalidation
appartient à PR9-C.

## 8. Politique ticker

`ticker_input` (B1, `strip().upper()`, signé) ≠ `resolved_ticker` ≠
`InstrumentResolution` (statut + provenance). Jamais de repli brut :

1. table `_KNOWN_TICKER_OVERRIDES` (vérifiée manuellement, recopiée) :
   `known_override`, aucun appel ;
2. `CODE.PLACE` : ligne (Code, Exchange) exacte d'un type admis ;
3. forme US : ligne (Code, `US`) exacte, sauf même code sur une autre place
   pour un autre ISIN (homonymie : `ambiguous`) ;
4. code exact et/ou nom normalisé EXACTEMENT égal : identité = un seul ISIN ;
   code et nom divergents, zéro / plusieurs ISIN, nom apparenté d'une autre
   entreprise : `ambiguous` ; rien : `not_found` ; cotation choisie parmi
   celles de CETTE entreprise par le classement historique (copie de `rank`,
   équivalence testée contre `_pick_best_match`).

Une recherche au plus. Réponse mal formée, panne, délai : `unavailable`
(→ `market_data_unavailable`). `not_found` / `ambiguous` : refus, sans
déclaration. Format historique conservé (code nu pour US, `CODE.PLACE`
sinon, valeurs d'override telles quelles) pour rester comparable aux
`AnalysisSession` existantes. Aucun ticker persisté.

Arbitrage plus strict que le legacy : un nom court qui n'est ni un symbole
exact ni un nom exact (ex. `ALO` pour Alstom), un homonyme (`MERCK`,
`AIR`, `FERRARI` avec Ferrari Group) ou une identité sans ISIN sont refusés
au lieu d'être départagés heuristiquement.

## 9. Politique EODHD / FMP

EODHD principal (fondamentaux puis temps réel), FMP en repli (EODHD
inexploitable : panne, introuvable, identité, devise, données insuffisantes)
ou complément contrôlé. Chaque champ (`current_price`, `revenue_growth`,
`operating_margin`, `gross_margin_latest`, `roe`, `roic`, `eps`,
`fcf_per_share`, `net_cash`, `debt_to_equity`) porte statut (`present`,
`absent`, `currency_mismatch`), valeur finie, source, période
(`FY:<date>`, `TTM`, `CAGR:FY..FY`, `REALTIME`, `PROFILE`) et unité.

- identité : `General.Code` = code résolu, ISIN identique s'il est connu des
  deux côtés ; FMP `symbol` = ticker résolu ;
- devise obligatoire ; seule conversion GBX → GBP (prix ÷ 100, tracée) ;
  monétaires des états retenus seulement si la devise des états est la
  devise de cotation ;
- absent ≠ zéro : aucune valeur par défaut, ni `EPSEstimateCurrentYear`, ni
  taux d'impôt de 25 %, ni trésorerie à 0 ; NaN, infini, booléen, `"NA"` :
  absent ; CAGR sur exercices consécutifs uniquement ;
- complément FMP : seulement un champ ABSENT de même définition, même date
  de clôture et (monétaire) même devise ; jamais d'écrasement ; la croissance
  FMP (YoY) n'est jamais présentée comme le CAGR ;
- suffisance : ≥ 3 des 5 champs clés (seuil historique) ;
- textes fournisseurs bornés, sans caractère de contrôle ; le prompt
  rappelle qu'ils sont des contenus, jamais des instructions ;
- conversion numérique sûre (§ 22, P2-02) : un entier non représentable en
  binary64 ou tout dérivé non fini est absent, jamais une exception ;
- fraîcheur du cours fermée (§ 22, P2-01) : `recent`, `dated`, `undated`,
  `unavailable` ; seul `recent` (seuil EXPLICITE, aucun par défaut) est
  présenté comme le cours actuel.

Les signaux qualitatifs historiques (`fcf_vs_net_income`,
`gross_margin_trend`, `receivables_vs_revenue`, prévisibilité, P/E
historique) ne sont pas recalculés par B2 : affichés `N/D`.

## 10. Exclusion de yfinance

`fetch_financial_data()` (EODHD + FMP + yfinance) n'est jamais utilisé ;
aucun module B2 n'importe `core.data_fetcher`, `core.ticker_resolver`,
`core.market_lookup`, `yfinance` ni `api`, directement ou transitivement
(fermeture des imports calculée sur le code réel :
`test_neither_yfinance_nor_the_historical_fetchers_are_reachable_from_b2`) ;
test PostgreSQL du repli FMP avec les chemins historiques remplacés par des
sentinelles qui échouent.

## 11. Budget et limites temporelles

Horloge monotone locale injectable ; 240 s dont 5 s de marge ; plafonds
cumulés : F2 20 s, résolution 15 s, EODHD + FMP 80 s, Claude 105 s,
construction / validation 15 s. Allocation de chaque appel = min(reste du
plafond, reste global, reste du bail indicatif). Seuils de démarrage : 1 s
(données), 20 s (Claude), 0,5 s (F2, validation). Résultat rendu au-delà de
son allocation : écarté (`StepOverrun`), classé par son étape. Bail
indicatif = `lease_expires_at − read_at` (PostgreSQL) rapporté à l'instant
local de début de lecture (conservateur) ; Claude rendu après cette
échéance, ou candidat construit après : `lease_expired`, aucun candidat. Le
bail commence avant B2 : aucune garantie de temps résiduel pour C.

Transports réels : connexion ≤ min(5 s, allocation), chaque lecture ≤
min(5 s, allocation), échéance contrôlée après chaque morceau, taille
maximale ; dépassement maximal : une lecture (couvert par la marge). Aucun
thread, aucun retry, aucune redirection.

## 12. Configuration du client Claude

`anthropic.Anthropic(api_key=…, max_retries=0, timeout=httpx2.Timeout(105,
connect=5))`, puis `with_options(max_retries=0).messages.create(…,
timeout=httpx2.Timeout(allocation, connect=min(5, allocation)))`,
`max_tokens = 2500` (historique), un message utilisateur, prompt système de
`core.decryptage_engine.build_system_prompt` + bloc de provenance. Absence
de retry **démontrée** : réponse 529 simulée par `httpx2.MockTransport` →
exactement une requête. Le modèle est un paramètre serveur explicite (aucune
variable d'environnement lue). La boucle à deux tentatives de `/decryptage`
n'est pas reprise ; garde locale : un second appel dans une exécution lève.

Pédagogie conservée : méthodes canoniques (`METHODES`, `lookup_method`),
`construction_these` forcée avec une analyse active (prioritaire sur
historique, `last_method_id` et thèse) ou sur un tour initial (thèse
rappelée), étape ouverte transmise, une question à la fois, socratique, aucun
verdict ni prix cible (règles du moteur historique inchangé). Niveau
`debutant` / `avance` lu par le serveur, jamais un stade pédagogique.

## 13. Contrat du candidat

`GeneratedDecryptageCandidate` (`oryx-decryptage-generated-candidate-v1`) :

| Bloc | Champs |
|---|---|
| A. Tour | `turn: TurnInputs` (conversation, tour, ticker saisi, question, contexte, `last_method_id`, normalisés B1) |
| B. Capacité | `lease: DecryptageLease` B1 original (réservation, epoch, jeton masqué), `owner_user_id` (principal) |
| C. Instrument | `instrument: InstrumentResolution` (ticker, code, place, provenance, nom, ISIN) |
| D. Données | `market: FinancialSnapshot`, `market_fingerprint` |
| E. Pédagogie | `preparation: PreparationSnapshot` (niveau, révision, méthode, version, empreinte), `prepared_read_at`, `prepared_lease_expires_at` |
| F. Réponse | `payload: CandidatePayload` (forme `/decryptage`), `step_marker`, `response_diagnostics`, `model`, version |

Validé à la construction (cohérence bail / tour / snapshot / propriétaire /
ticker / payload / empreinte) ; immuable (dataclasses gelées et tuples
uniquement — vérifié par parcours) ; `pickle`, copie, JSON refusés ;
`repr()` sans jeton ni texte ; `redacted()` pour le diagnostic ;
`payload.as_dict()` rend une copie. `delivery_fingerprint_inputs()` rend les
valeurs serveur nécessaires à la future empreinte `AssistantDelivery`
(niveau compris) sans la calculer : la conversion relève de C. Jamais
persisté, jamais retourné à un navigateur.

## 14. Gestion des échecs

| Origine | Résultat |
|---|---|
| recherche / EODHD / FMP en panne, délai, inexploitable, données insuffisantes | `provider_failed / market_data_unavailable` + déclaration |
| Claude en panne, délai, résultat tardif | `provider_failed / generation_unavailable` + déclaration |
| réponse vide, marqueurs seuls, plusieurs blocs, `max_tokens`, résidu de marqueur, hors bornes | `provider_failed / generation_unusable` + déclaration |
| bail non accordé, autre tour, bail déjà confié, session, propriété, capacité, intention, F2 indisponible, contexte (refus fermés), contexte modifié, instrument introuvable / ambigu, budget insuffisant AVANT une étape, bail indicatif dépassé | `refused / <motif>`, **aucune** déclaration |

Déclaration : `RecordDecryptageAttemptFailure.from_lease(lease, classe)` par
`runner.run` (nouvelle autorisation complète) ; PostgreSQL décide (bail
valide, epoch, jeton, état, backoffs B1). Issue rapportée : `declared`,
`declaration_refused` (bail jamais présumé libéré), `declaration_uncertain`
(UNAVAILABLE, dont COMMIT incertain : jamais présumé). Aucune commande
`force_block` / `reset` / `unlock`. Une exception imprévue (« crash ») se
propage : aucune livraison, aucune réparation ; toute nouvelle attribution
relève de B1.

Arbitrage : `max_tokens` est classé `generation_unusable` (texte tronqué),
alors que le legacy l'accepte.

## 15. Protection du bail et des secrets

Le jeton n'est jamais un justificatif : chaque accès passe par
`runner.run(credential, command)`. Jeton absent de tout `repr`, de la vue
de préparation (ni jeton, ni empreinte HMAC, ni empreinte du jeton), des
journaux (testé avec `caplog` complet) et de toute sérialisation. Clés
fournisseurs jamais journalisées (aucun appel de journalisation dans les
transports) ; corps fournisseurs jamais journalisés ; l'orchestrateur ne
journalise que statut, motif, étape et type d'exception. La sélection de
méthode est appelée avec `log=False` : la question n'est jamais imprimée ni
journalisée par le chemin F2 (§ 22, P1-01).

## 16. Limites d'idempotence réseau

Un appel Claude explicite au plus par exécution (garde locale) ; un même
`(reservation_id, epoch)` n'est confié qu'à une exécution à la fois et une
seule fois par orchestrateur (registre en mémoire, borné à 4096 baux). Aucune
unicité globale entre processus, orchestrateurs ou après crash : B1 ne
compte pas les appels fournisseurs ; deux orchestrateurs recevant le même
bail peuvent produire deux candidats du même epoch — seule la finalisation
PR9-C (verrou + fencing) pourra en retenir au plus un.

## 17. Cas de concurrence

| Scénario | Observé |
|---|---|
| même bail, même orchestrateur (appel imbriqué pendant Claude) | `lease_already_used`, un seul appel Claude |
| même bail, deux orchestrateurs (barrière pendant Claude) | deux candidats du même epoch, état PostgreSQL inchangé |
| révocation entre les deux lectures | `session_invalid`, Claude non appelé |
| niveau modifié pendant les fournisseurs | `context_changed` ; ABA invisible (documenté) |
| contribution / fait inséré entre les lectures | `context_changed` |
| ancre fermée entre les lectures | `stale_conversation_context` |
| analyse active modifiée pendant Claude | candidat produit avec l'ancien snapshot ; une relecture diffère (empreinte) : revalidation C requise |
| bail repris par un successeur entre les lectures | `lease_stale`, successeur intact |
| candidat d'un ancien epoch (reprise pendant Claude) | candidat epoch 1 ; déclaration et lecture avec ce bail : `REVISION_CONFLICT` |
| crash après génération | exception propagée, état inchangé |
| déclaration refusée (bail expiré) / COMMIT incertain | `declaration_refused` (jeton toujours en base) / `declaration_uncertain` |

Ces tests montrent l'absence d'effet canonique de B2 et le transport des
éléments utiles à C ; ils ne démontrent pas les garanties finales de C.

## 18. Tests exécutés et résultats

| Fichier | Contenu |
|---|---|
| `tests/test_authz_f2_pr9b2.py` (PostgreSQL) | lecture (§ 4, 6), orchestration, fournisseurs, échecs, budget, transactions |
| `tests/test_authz_f2_pr9b2_concurrency.py` (PostgreSQL) | § 17 |
| `tests/test_authz_f2_pr9b2_units.py` (sans base) | instrument, données, budget, réponse, snapshot, candidat, commande, transports réels simulés |
| `tests/test_authz_f2_pr9b2_boundary.py` (sans base) | registre, runner seul accès, graphe d'imports, dormance du vrai `api:app`, import sans réseau, aucune migration |
| `tests/authz_f2_pr9b2_support.py` | environnement, horloge et transports simulés |
| `tests/test_authz_f2_pr9b2_audit.py` (sans base) | régressions du correctif d'audit P1-01, P1-02, P2-01, P2-02 (§ 22) |
| Extensions nominatives | BF1 (`_PREPARATION_CONTEXT`, aucune CTE modifiante, registre 17, lecture à trousseau), matrice d'imports, inventaire transitif du pont, dispatch des trousseaux, vocabulaire des commandes, liste blanche T2-A de `test_migration_0005` (`_adapters.py` LIT l'event d'ancrage, comme le pont PR9-A) |

Résultats : voir la description de la PR (exécutions locales PostgreSQL
16.15 et 18.6, images CI épinglées ; contrôles GitHub `ci / *`).

## 19. Preuves de dormance

Aucun module applicatif n'importe B2 ni les transports ; `api.py` ne
mentionne aucun nom B2 ; le vrai `api:app` (processus isolé, avec et sans
variables évoquant une activation) ne charge aucun module B2, garde ses
routes exactes et répond 404 sur `POST /api/v2/me/decryptage` et l'ACK F2 ;
importer B2 n'ouvre aucune connexion ni socket, même avec `DATABASE_URL`, et
ne charge aucun client réseau ; transports : aucune configuration lue à
l'import ni à la construction ; aucune migration.

## 20. Responsabilités réservées à PR9-C

Finalisation `reserved → finalized` ; lien `assistant_delivery_id` ;
revalidation finale du bail sous verrous ; revalidation du snapshot
(`level`, `level_revision`, session active, étape, ancre, contributions) et
gestion de l'ABA ; collisions tardives avec `/decryptage` historique ;
`claim_delivery` et empreinte SHA-256 historique (à partir de
`delivery_fingerprint_inputs()`) ; `TurnFinalization` ; capture cognitive ;
progression `AnalysisSession` ; persistance éventuelle de `resolved_ticker` ;
ACK F2 ; réponse HTTP ; composition des transports réels et des clés ;
activation.

## 21. Risques résiduels et points à valider

1. Une course après la seconde lecture (analyse, niveau, ancre, bail) n'est
   pas détectable par B2 : PR9-C doit tout revalider.
2. ABA sur le niveau (écriture historique sans révision).
3. Résolution plus stricte que le legacy (§ 8) : plus de refus
   `instrument_*` sur des noms courts ou homonymes.
4. Signaux qualitatifs historiques non reproduits (§ 9) ; noms de champs
   FMP `stable` (`debtEquityRatio` / `debtToEquityRatio`, `period`) non
   vérifiables sans réseau.
5. Timeouts des bibliothèques HTTP par opération, pas en temps total : la
   borne réelle est l'allocation plus une lecture (≤ 5 s, couverte par la
   marge) ; un appel Claude non streamé peut attendre jusqu'à son allocation.
6. Idempotence réseau limitée à un orchestrateur (§ 16).
7. `max_tokens` refusé comme inexploitable (§ 14).
8. Seuil de fraîcheur du cours (§ 22, P2-01) : **à arbitrer par le
   fondateur** ; sans seuil (défaut), aucun cours n'est présenté comme
   actuel et le payload candidat porte `price = null`.
9. Une lecture `READ` reçoit désormais le trousseau Décrypter (dispatch
   existant, test nominatif) : à confirmer à l'audit.

## 22. Correctif d'audit indépendant (NO-GO → corrections)

Base du correctif : `9c2c596` (HEAD audité). Architecture V1.1 inchangée
(registre à 17, plan READ, runner, B1, bail 360 s, budget 240 s, EODHD puis
FMP, aucun yfinance, double lecture, ancre R1-C4, candidat immuable, aucune
route, migration, livraison ni écriture cognitive). Tests de régression
écrits AVANT les correctifs (`tests/test_authz_f2_pr9b2_audit.py`) :
**39 échecs sur `9c2c596`**, dont `OverflowError: int too large to convert
to float` (5 cas) et `used_facts` rendant `(1, 20.0)` au lieu de `(2, None)`.

### P1-01 — La question utilisateur n'est plus journalisée par F2

`lookup_method(..., *, log=True)` : paramètre **mot-clé uniquement**, défaut
historique ; chaque `print` de la fonction est gardé par `if log:` (rien
d'autre ne change : mêmes branches, priorités, verrou de thèse, reprise de
session, absence de correspondance). Le chemin F2 l'appelle avec
`log=False`. Aucune redirection de stdout, aucun remplacement de `print`,
aucun état global : la sélection silencieuse est locale à l'appel.

Tests : égalité stricte du résultat silencieux et historique sur toutes les
branches (12 cas), signature et défaut, garde de CHAQUE `print` (AST),
absence de `redirect_stdout` / `sys.stdout` / `builtins`, exécutions
concurrentes (200 sélections historiques et 200 silencieuses en parallèle :
les 200 lignes historiques sont imprimées, aucune silencieuse), et, sur
PostgreSQL, une génération F2 complète sans la question dans stdout, stderr
ni journaux, avec la même méthode que le legacy.

### P1-02 — La dernière valeur d'un fait l'emporte, même NULL

`used_facts` ne filtre plus les valeurs NULL avant de retenir la dernière
ligne (`fact_date ASC, id ASC`) de chaque type injecté : comportement exact
du dict historique. Le snapshot porte la ligne réellement retenue (id,
valeur `None`) et son empreinte change avec elle ; le prompt reçoit `N/D`
(convention d'absence du moteur, qui interdit toute estimation), jamais une
valeur antérieure ni un zéro. Écart textuel assumé : le legacy afficherait
`None`.

Tests A à G de l'audit (unitaires) et propagation PostgreSQL (prompt
`- ROE : N/D`, aucune trace de `20.0`, provenance de la ligne NULL).

### P2-01 — Fraîcheur du cours

`FinancialSnapshot.price_freshness` ∈ {`recent`, `dated`, `undated`,
`unavailable`} et `price_reference_at` :

| Statut | Condition | Prompt | Payload `price` |
|---|---|---|---|
| `recent` | date de cotation fiable ET 0 ≤ (read_at PostgreSQL de la seconde lecture − date) ≤ seuil EXPLICITE | « Prix actuel » + « Cours récent vérifié » | valeur |
| `dated` | date fiable, sans seuil, trop ancienne ou future | « Prix actuel : N/D » + « Dernier cours connu … au <date> ; ne le présente jamais comme le cours actuel » | `null` |
| `undated` | cours sans date fiable (timestamp absent ou invalide, profil FMP) | « Prix actuel : N/D » + « sans date fiable » | `null` |
| `unavailable` | aucun cours | « Cours : non disponible » | `null` |

Aucun seuil n'est introduit silencieusement : `price_recent_max_age=None`
par défaut ; **la valeur du seuil est un point à arbitrer par le
fondateur** (§ 21). La valeur, la date et la source du cours restent dans le
snapshot financier (et dans son empreinte) pour PR9-C. Aucune date ni
fraîcheur n'est inventée ; le moteur historique et `/decryptage` sont
inchangés.

Tests : cours daté avec et sans seuil, ancien, futur, sans timestamp
(absent, nul, chaîne), absent, prix FMP, bloc de provenance, empreinte,
seuil invalide refusé ; propagation PostgreSQL vers le prompt Claude et le
candidat pour chaque statut.

### P2-02 — Conversions numériques sûres

`number()` traduit en absence le seul `OverflowError` d'un entier non
représentable (aucune autre exception masquée) ; une chaîne hors domaine
(`float` rend ±inf) est absente ; `_present` refuse tout dérivé non fini
(produit, quotient, arrondi) et `_ratio` exige des opérandes finis (jamais
de zéro fabriqué par une division par l'infini). Zéro réellement fourni :
`0.0`, présent ; absence : `None`.

Tests : `10**400`, `-10**400`, `"1e400"`, `"9"*400`, NaN, ±inf, booléens,
`None`, conteneurs, chaînes vides ; valeurs valides et zéro réel ; valeurs
extrêmes EODHD et FMP sans interruption du parsing ni infini.

### Tests existants ajustés (attentes corrigées, aucun affaiblissement)

- `test_the_snapshot_keeps_only_what_the_prompt_uses_with_historical_rules` :
  attendait l'exclusion d'un `eps` NULL dernier (le défaut P1-02) ;
- `candidate()` (unitaires) et `test_a_full_generation_…` : attendaient le
  cours daté publié comme actuel (le défaut P2-01).
