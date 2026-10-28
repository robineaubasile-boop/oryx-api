# R1 PR 8 — Lectures F2 sécurisées des analyses et des thèses (dormantes)

## 1. Références normatives et SHA

Références normatives : **V2C `R1-PR8-V2C-2026-10-28`** et **addendum de
fermeture `R1-PR8-V2D-2026-10-28`**, architecture figée par le fondateur
le 28 octobre 2026 (la V2D prévaut pour les points qu'elle précise ou
corrige). Ces documents ne sont pas versionnés dans le dépôt : le contrat
appliqué est celui reproduit dans la demande d'implémentation, sans
décision architecturale nouvelle.

Référence de compatibilité : `main` au SHA
`c5e25d243149013c99bb66fbc644a54a196666df` (PR #227, R1 PR 7 fusionnée),
identique au `main` réel au moment de l'implémentation (aucun commit
intermédiaire). **Aucune migration** (tête Alembic inchangée
`0020_position_revision`), aucune table, colonne, contrainte ni index,
aucun secret ni trousseau, aucune dépendance, aucune variable
d'environnement, aucun frontend modifié.

> **PR 8 ajoute trois LECTURES dans l'application personnelle F2 isolée.
> Elle ne monte rien sur `api:app`, ne sécurise pas les routes historiques
> `/api/user/{user_id}/*` et n'autorise pas l'activation publique de F2.**

Principe produit inchangé : **« Décrire, jamais prescrire. »** Une
`AnalysisSession` (tentative), une `InvestmentThesis` (résultat historique)
et une preuve pédagogique restent trois notions distinctes. Les lectures ne
produisent aucun événement cognitif, aucune preuve, aucune progression ;
`verified` n'atteste ni la qualité d'une analyse ni une compétence.

## 2. Périmètre fermé

| Élément | Fichier |
|---|---|
| Trois commandes typées, vues fermées, constantes et validateurs | `core/authz_f2/commands.py` |
| Adaptateurs SQL privés et audités, registre à quatorze commandes | `core/authz_f2/_adapters.py` |
| Codec pur : query stricte, curseurs, dates, corps, bornes | `core/authz_f2_http/consultation_codec.py` (nouveau) |
| Routeur des trois lectures | `core/authz_f2_http/consultation.py` (nouveau) |
| Inclusion dans la composition isolée | `core/authz_f2_http/isolated.py` |
| Docstrings de paquet | `core/authz_f2/__init__.py`, `core/authz_f2_http/__init__.py` |

Inchangés : `api.py`, migrations 0001–0020, `core/models.py`,
`web-v2/index.html` et tout frontend, routes `/api/user/{user_id}/*`,
runner (`core/authz_f2/runner.py`), plans de verrouillage, `core/auth_f2*`,
Coach, Éducation, Décrypter, ACK, Portfolio Analysis, Academy, Rallye,
observations, preuves, inférences, C1–C12. Non créés (exclusions) : POST /
PUT / PATCH / DELETE de thèse, abandon d'`AnalysisSession`, CRUD
d'analyses, route `/api/v2/me/analyses`, nouveaux endpoints Décrypter ou
ACK, cache ou table de pagination, snapshot persistant.

## 3. Les trois routes

| Méthode | Route | Commande | Succès |
|---|---|---|---|
| GET | `/api/v2/me/analysis-overview` | `ListAnalysisOverviewPage(limit, after_ticker)` | 200 `{"entries": [...], "next_cursor": ...}` |
| GET | `/api/v2/me/theses` | `ListThesesPage(limit, ticker, after_thesis_id, after_created_at)` | 200 `{"next_cursor": ..., "theses": [...]}` |
| GET | `/api/v2/me/theses/{thesis_id}` | `ReadThesis(thesis_id)` | 200 vue détaillée, sinon 404 `thesis_not_found` |

Query admise : `limit` et `cursor` ; `ticker` sur `/theses` seulement.
Défauts : `limit = 25`, curseur et filtre absents. Le détail refuse toute
query non vide. Tout autre méthode sur ces chemins : 405 ; tout autre
chemin : 404 (routeur). Les routes n'existent que dans
`build_isolated_personal_app` (§16).

## 4. Vues JSON

Toutes les réponses 200 sont du JSON canonique UTF-8 (clés triées,
séparateurs compacts, `ensure_ascii=False`), `Content-Type:
application/json`.

**Synthèse** — une entrée par ticker exact, tri `ticker COLLATE "C" ASC` :

| Champ | Valeur |
|---|---|
| `ticker` | chaîne historique exacte |
| `state` | `in_progress` (session active retenue) ou `thesis_available` (aucune session active, au moins une thèse) |
| `analysis_session_id` | UUID canonique de la session active retenue, sinon `null` |
| `current_step` | étape reconnue de la session retenue (`business`, `moat`, `chiffres`, `valorisation`, `risques`, `swot_final`), sinon `null` |
| `updated_at` | `updated_at` de la session retenue, sinon `created_at` de la dernière thèse, sinon `null` |
| `latest_thesis_id` | entier si `thesis_available`, sinon `null` |
| `diagnostics` | sous-liste ordonnée de `["multiple_active_sessions", "invalid_current_step"]` |

Population : ticker inclus s'il porte au moins une session `in_progress`
ou au moins une thèse encore enregistrée ; sessions `abandoned` ou
`completed` sans thèse exclues. Session active prioritaire même si
d'anciennes thèses existent ; jamais d'état `completed` inféré d'une
thèse. Sessions actives multiples (anomalie) : classement fermé
`started_at DESC, updated_at DESC, id DESC`, première retenue, nombre
compté, `multiple_active_sessions` si > 1, aucune réparation. Étape
absente ou inconnue : `current_step = null` et `invalid_current_step`,
jamais d'étape de substitution. Dernière thèse : `created_at DESC NULLS
LAST, id DESC`, sans `thesis_text`. États de **présentation**, pas des
stades cognitifs.

**Liste des thèses** — une entrée par thèse, ordre total `created_at DESC
NULLS LAST, id DESC` : `thesis_id`, `ticker`, `created_at` (ou `null`),
`provenance_status` (`verified` / `unverified`). Jamais de `thesis_text`
ni d'extrait.

**Détail** — exactement `thesis_id`, `ticker`, `thesis_text` (intégral,
jamais tronqué), `created_at`, `provenance` = `{"status",
"analysis_session_id"}`. Aucun `UserStatement`, `AnalysisFact`,
`CognitiveEvent`, `AssistantDelivery` ni métadonnée privée.

## 5. Identité serveur et propriété

Les commandes ne portent aucun `user_id` (vérifié) ; le propriétaire est
exclusivement `AuthorizedPrincipal.user_id`, construit par le runner à
`T_authorization`. Chaque sous-requête filtre `user_id = :u` (le user_id
serveur). Détail : `WHERE id = :id AND user_id = :u` ; une thèse étrangère
et une thèse inexistante rendent le même `ABSENT`, puis le même
`404 {"error":"thesis_not_found"}` (corps et en-têtes identiques, testé).
Un curseur ne porte ni `user_id`, ni secret, ni droit : il ne sert jamais
à déterminer l'appartenance (un champ inconnu tel `user_id` est refusé ;
un curseur pointant au-delà des thèses d'autrui rend une page vide).

## 6. Provenance relationnelle

`verified` si et seulement si une sous-requête corrélée trouve
`analysis_sessions s` avec `s.id = investment_theses.analysis_session_id
AND s.user_id = investment_theses.user_id AND s.user_id = :u AND s.ticker
COLLATE "C" = investment_theses.ticker COLLATE "C"` (la thèse étant déjà
filtrée par `user_id = :u`). La jointure ne repose pas sur la seule FK.
L'identifiant brut `analysis_session_id` de la thèse n'est **jamais
projeté** : seul l'identifiant de la session vérifiée l'est (détail), ou un
booléen (liste). Session absente, étrangère, d'un autre ticker (casse
comprise) ou référence orpheline : `{"status": "unverified",
"analysis_session_id": null}`, sans raison, sans identifiant, sans
réparation ni reconstitution par dates ou textes. Le statut de la session
(`in_progress`, `completed`, `abandoned`) n'intervient pas.

## 7. Requêtes SQL et classements

Une **seule instruction SQL métier** par page ou par thèse, exécutée après
l'autorisation définitive (vérifié par capture des instructions). Sous
READ COMMITTED, chaque instruction voit un instantané cohérent pris à son
début ; aucune lecture indépendante des sessions puis des thèses.

Synthèse (`_ANALYSIS_OVERVIEW_PAGE["first" | "after"]`) :

- `k` = `UNION` des tickers (`COLLATE "C"`) des sessions `in_progress` et
  des thèses du principal ;
- `s` = sessions `in_progress` partitionnées par ticker `"C"`,
  `row_number() OVER (… ORDER BY started_at DESC, updated_at DESC, id
  DESC)` et `count(*) OVER (…)` ; jointure `s.ordinal = 1` ;
- `t` = thèses partitionnées par ticker `"C"`, `row_number() OVER (…
  ORDER BY created_at DESC NULLS LAST, id DESC)`, colonnes `id` et
  `created_at` seulement ; jointure `t.ordinal = 1` ;
- projection : session prioritaire, sinon thèse (décidé par l'adaptateur,
  diagnostics calculés sans écriture) ;
- keyset APRÈS l'agrégation : `WHERE k.ticker COLLATE "C" > CAST(:a AS
  text) COLLATE "C"` (absent en première page), `ORDER BY k.ticker COLLATE
  "C" ASC LIMIT :n` avec `n = limit + 1`.

Liste (`_THESES_PAGE`, six instructions fermées : première page, après une
date non nulle, après une date nulle, chacune avec ou sans filtre) :
`WHERE user_id = :u [AND ticker COLLATE "C" = CAST(:t AS text) COLLATE
"C"] [keyset] ORDER BY investment_theses.created_at DESC NULLS LAST,
investment_theses.id DESC LIMIT :n`. Pas d'OFFSET. L'`ORDER BY` est
qualifié : un nom nu y désignerait d'abord la colonne de **sortie**
`created_at` (projection bornée, `NULL` pour une date non représentable),
ce qui placerait une date `infinity` parmi les dates nulles, à l'encontre
du contrat et des prédicats keyset (qui portent, comme toute clause
`WHERE`, sur la colonne réelle) ; testé.

Détail (`_READ_THESIS`) : `… FROM investment_theses WHERE id = :id AND
user_id = :u`, provenance par sous-requête scalaire.

Aucune instruction ne verrouille (`FOR …`), n'écrit, ne contient de `;`,
de commentaire ni de composition dynamique (tests statiques BF1).

## 8. Pagination, curseurs et `NULLS LAST`

Prédicats keyset fermés, combinés à `user_id = :u` et au filtre :

- dernière date non nulle : `(created_at < :d OR (created_at = :d AND id
  < :i) OR created_at IS NULL)` ;
- dernière date nulle : `(created_at IS NULL AND id < :i)`.

`:d` est reconstruit en `datetime` naïf exact (TIMESTAMP WITHOUT TIME
ZONE, précision microseconde, aucune conversion de fuseau). Chaque page
lit `limit + 1` lignes dans PostgreSQL ; la ligne supplémentaire ne dit
que l'existence d'une page suivante (jamais rendue, jamais validée, jamais
source du curseur). `next_cursor` est construit depuis la DERNIÈRE entrée
rendue ; page finale ou vide : `null`.

Curseurs : JSON canonique UTF-8 → Base64URL sans padding, 1024 caractères
au plus. Décodage strict : alphabet, longueur, bits de remplissage nuls,
UTF-8 strict, JSON sans doublon ni constante, schéma exact, types exacts,
**réencodage canonique identique à la chaîne reçue** (refuse ordre des
clés, blancs, échappements superflus, flottants), `v = 1`, route
attendue ; pour `/theses`, `ticker` du curseur égal au filtre HTTP (un
changement de filtre est refusé). `last_ticker` de la synthèse est une
chaîne exacte, éventuellement vide, non soumise aux règles des nouveaux
filtres (seulement aux bornes de transport ; un NUL, impossible dans un
texte PostgreSQL, est refusé). Curseur suivant de plus de 1024 caractères
(ex. ticker de 512 guillemets) : 503 pour la page, jamais de clé opaque ni
de stockage serveur.

Sur données immobiles, le parcours restitue chaque thèse et chaque ticker
exactement une fois (testé pour `limit` 1, 2, 3, 4, 7, 25, 36, 37, 50,
dates égales et nulles, filtre, tickers atypiques, et sur 6 000 thèses).
Sous mutations concurrentes, les pages successives ne forment pas un
snapshot : une thèse insérée avant la position du curseur n'est pas vue,
après elle l'est ; une suppression non encore rendue est omise sans
erreur ; la suppression de la ligne du curseur n'interrompt pas le
parcours ; un changement d'état d'analyse est reflété par les pages
suivantes (tous testés).

## 9. Collation `"C"`

Égalités, `UNION`, partitions, jointures, keyset et tris des tickers sont
tous sous `COLLATE "C"` (test statique : toute référence à un ticker hors
projection est suivie de `COLLATE "C"`). Les tests PostgreSQL tournent sur
des bases de collation **linguistique** (ICU `en-US` localement, comme
`en_US.utf8` en CI) : l'ordre attendu est celui des points de code
(`"B" < "Z" < "_x" < "a"`), différent de l'ordre linguistique. Aucune
normalisation : `"AIR.PA"`, `"air.pa"`, `"Air.pa"`, `"AIR.PA "`, `"á"`
précomposé et décomposé restent distincts. L'égalité ordre Python / `"C"`
suppose une base encodée en UTF-8 (cas de PostgreSQL 16 / 18 par défaut).

## 10. Dates historiques

Format public fermé `YYYY-MM-DDTHH:MM:SS.ffffffZ`, six décimales toujours
présentes, `null` pour une date SQL nulle, aucune date inventée.
`investment_theses.created_at` (TIMESTAMP sans fuseau) est lu comme UTC
historique, sans conversion depuis le fuseau du serveur ;
`analysis_sessions.updated_at` (TIMESTAMPTZ) est converti en UTC **par
PostgreSQL** (`AT TIME ZONE 'UTC'`), indépendamment du `TimeZone` de la
connexion (testé sous UTC, America/New_York, Pacific/Kiritimati,
Asia/Kathmandu, aux années 0001 et 9999).

Une date n'est projetée que si elle tient dans les années 0001 à 9999 UTC,
avec un indicateur explicite `*_representable` ; sinon 503 pour la requête
qui la restituerait, données intactes. Raison technique : psycopg2
convertit **silencieusement** `infinity` / `-infinity` en `datetime.max` /
`datetime.min` (fausse date), et lève une erreur de conversion sur une
année hors bornes **dès la réception**, y compris pour la ligne
supplémentaire d'une page ou pour une date non restituée (thèse masquée
par une session active). Avec la projection bornée, une date hors
représentation ne fait échouer que la page ou la thèse qui la restitue
(testé : `infinity`, `-infinity`, `10000-01-01`, `0044-03-15 BC`).

## 11. Bornes de transport et de restitution

| Borne | Valeur | Dépassement |
|---|---|---|
| Query brute (`scope["query_string"]`, avant décodage) | 2048 octets | 400 |
| Curseur encodé reçu | 1024 caractères ASCII | 400 |
| Curseur suivant produit | 1024 caractères | 503, page refusée |
| `limit` | `[1-9][0-9]?`, 1 à 50 | 400 |
| Filtre `ticker` | non vide, 32 caractères, sans blanc de bord (ni NUL) | 400 |
| `thesis_id` du chemin | `[1-9][0-9]{0,9}`, ≤ 2147483647, chemin brut sans `%` | 400 |
| Ticker restitué | 512 octets UTF-8 (projection SQL bornée + longueur) | 503, jamais tronqué ni haché ni omis |
| Texte de thèse brut | 131072 octets (projection SQL bornée + longueur : jamais transféré au-delà) | 503 |
| Corps JSON 200 final (tout compris, échappements, curseur) | 131072 octets | 503, jamais de 200 partiel |

La synthèse et la liste ne lisent jamais `thesis_text`. Une page contenant
une seule valeur non restituable échoue entièrement (aucune entrée omise) ;
les autres pages, le filtre et les autres routes restent lisibles.

## 12. Contrôle HTTP strict

Ordre fermé (premier échec déterminant ; instrumenté par
`test_check_priorities` pour les trois routes) :

1. méthode et chemin (404 / 405) ;
2. provenance GET personnelle de PR 5 (`personal_get_origin`) → 403
   `origin_refused` ;
3. en-tête Cookie, parseur strict de PR 3 → 400 ;
4. `X-Oryx-Generation-Id` unique, UUID canonique → 400 ;
5. query brute ≤ 2048 octets → 400 ;
6. listes : décodage strict (octets ASCII visibles, `&`, `nom=valeur`, `+`
   = espace, `%HH` strict, UTF-8 strict, un seul décodage, aucune
   normalisation, noms comparés après décodage, doublon / inconnu / vide
   refusés), `limit`, `ticker`, curseur ; détail : query vide **puis**
   `thesis_id` → 400 ;
7. framing et corps : règles de PR 7 (même fonction `_refuse_body`) —
   Transfer-Encoding visible, Content-Length dupliqué ou mal formé → 400 ;
   Content-Length > 4096 → 413 ; Content-Length > 0 → 400 sans lecture ;
   flux non vide → 400 (413 au-delà de 4096) ; troncature / déconnexion →
   400 ; Content-Type n'autorise jamais un corps ;
8. cookie `__Host-oryx-active-<generation_id>` EXACT (jamais un autre
   cookie actif) → 401 `session_invalid` ;
9. runner disponible → 503 ;
10. autorisation transactionnelle → 401 / 404 (détail) / 503 ;
11. lecture SQL métier ; 12. vue validée ; 13. sérialisation complète et
    taille ; 14. émission.

Aucun refus des étapes 1 à 9 n'ouvre de transaction (trace et
`hook_calls` vides). Aucun jeton CSRF n'est exigé (GET). Toutes les
réponses : `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, aucun
`Set-Cookie`, aucun CORS. Erreurs `{"error": code}` parmi
`invalid_request`, `session_invalid`, `origin_refused`, `thesis_not_found`,
`request_too_large`, `temporarily_unavailable`. Journaux : route, statut,
code ou type d'exception, alerte interne sans détail
(`consultation_value_unrepresentable`) ; ni ticker, texte, curseur,
identifiant, cookie, secret ni SQL (testé).

Validation du résultat du runner avant tout 200 : `AuthorizationOutcome`,
statut `AUTHORIZED`, `AuthorizedPrincipal` de la génération sélectionnée,
vue du type exact, entrées validées à la construction, au plus `limit`
entrées, `has_more` seulement sur page pleine, première entrée strictement
après le curseur, filtre respecté, thèse demandée ; tout écart → 503
(testé par un runner simulé), jamais de correction.

## 13. Plan BF1 / BF2

BF1 : registre fermé à **quatorze** commandes — les onze de PR 7
(`ReadProfile`, `ListPositions`, `ReadPosition`, `ListTheses`,
`ListThesesForTicker`, `ReadDelivery`, `ListConversationDeliveries`,
`UpdateProfileLevel`, `CreatePortfolioPosition`,
`ReplacePortfolioPosition`, `DeletePortfolioPosition`, sémantique
inchangée) et `ListAnalysisOverviewPage`, `ListThesesPage`, `ReadThesis`.
Données gelées, types exacts, bornes validées, aucun `user_id`, aucune
connexion, aucun callback ni SQL ; type exact exigé par le runner
(sous-classes refusées). SQL seulement dans `_adapters.py`, en constantes
de l'inventaire audité.

BF2 : plan `READ` **inchangé** (`users FOR KEY SHARE` → `oryx_sessions
FOR SHARE` → `oryx_session_generations FOR SHARE` → `T_authorization =
clock_timestamp()` → revalidation intégrale → lecture métier → COMMIT),
pré-validation de phase A conservée, égalité avec l'échéance refusée,
révocation committée avant la décision refusée, aucune confirmation
implicite de livraison, aucun réseau dans la transaction, aucune
transaction conservée entre deux pages, aucun verrou de ligne métier
(constaté dans `pg_locks` : `AccessShareLock` de relation seulement ;
`FOR UPDATE NOWAIT` d'un tiers réussit pendant la lecture), aucune
écriture (xmin / ctid de toutes les tables lues, F2 et pédagogiques
inchangés). Isolation globale inchangée ; runner inchangé.

## 14. Compatibilité Décrypter / ACK

Testé sur connexions réelles, dans les deux sens : une lecture pendant le
tour final Décrypter (`apply_construction_these_progress`, session `FOR
UPDATE`, completed, thèse insérée, non committé) ne l'attend pas et voit
l'état committé antérieur ; un tour Décrypter committé pendant qu'une
lecture est autorisée n'attend pas, et l'instruction métier exécutée
ensuite le voit ; idem pour la suppression historique `DELETE
/api/user/{user_id}/theses/{ticker}` et pour un ACK
(`lock_delivery_for_ack` + `acknowledge_delivery`). Bascules
concurrentes atomiques (session ↔ thèse, thèse déplacée entre deux
tickers) : chaque lecture voit exactement un état committé. Charge mixte
(lectures, mutation de portefeuille, lecture de profil, écrivain
historique) sans interblocage ni refus parasite.

## 15. Absence de mutation pédagogique

Aucune écriture dans `cognitive_events`, `user_statements`,
`analysis_facts`, `assistant_deliveries`, `analysis_sessions`,
`investment_theses`, `users`, tables F2 (empreinte xmin / ctid avant et
après, sessions `pending` et confirmées). Aucune réparation des sessions
actives multiples, des étapes invalides, des provenances incohérentes ni
des dates.

## 16. Dormance publique

Le vrai `api:app`, en sous-processus, avec ou sans variables évoquant une
activation (dont `ORYX_THESES_F2`, `ORYX_V2_THESES`,
`ORYX_ANALYSIS_OVERVIEW_F2`, `ORYX_PR8_ENABLED`) : `GET
/api/v2/me/analysis-overview`, `GET /api/v2/me/theses` (avec ou sans
query) et `GET /api/v2/me/theses/1` / `…/2147483647` → 404, aucun cookie,
aucun module `core.auth_f2*` / `core.authz_f2*` chargé, aucun accès réseau,
routes publiques exactement celles de `main`. `api.py` ne mentionne aucun
élément PR 8 ; aucun fichier applicatif ne référence les nouveaux modules ;
importer les modules PR 8 n'ouvre aucune connexion ; matrices d'imports
fermées étendues des seules entrées nouvelles ; `requirements.txt`
inchangé.

## 17. Tests PostgreSQL 16 / 18

| Suite | Contenu |
|---|---|
| `tests/test_authz_f2_consultation_codec.py` (sans base) | query (%HH, UTF-8, `+` / `%2B`, doublons après décodage, inconnus, vides, 2048), `limit`, filtre, `thesis_id`, dates (microsecondes, UTC, hors représentation, reconstruction exacte), curseurs (Base64URL, padding, bits de remplissage, alphabet, JSON non canonique, doublon, champ inconnu, version, route, filtre, date, id, 1024 / 1025, curseur trop long produit, ticker vide, caractères spéciaux), commandes et vues fermées, ordre total, corps, 131072 exact / +1, échappement |
| `tests/test_authz_f2_consultation.py` | §20 A (propriété, identité, pending, révoquée, génération révoquée, expirée, échéance exactement à `T_authorization`, clé F2 historique absente, profil absent, panne, aucune écriture), B (synthèse complète, une instruction, aucun `thesis_text`), C (ordre, filtre), D (provenance), E (dates, ±infinity, hors bornes, ligne supplémentaire, fuseau de connexion, texte jamais transféré au-delà de la borne, 512 / 513 octets, multioctets, page entière refusée) |
| `tests/test_authz_f2_consultation_http.py` | vues JSON exactes, 404 indiscernables, parcours paginés, mutations entre pages, bornes HTTP (131072 exact / +1, échappement, liste > 131072, curseur trop long), transport G complet, ordre instrumenté, runner absent ou incohérent, journaux sans fuite, composition F2 réelle (connexion, résolution, lecture, logout) |
| `tests/test_authz_f2_consultation_concurrency.py` | §20 H : Décrypter, suppression historique, statut, ACK, verrous, instantané par instruction, expiration et révocation pendant l'attente du verrou users, révocation concurrente, charge mixte |
| `tests/test_authz_f2_consultation_performance.py` | §18 : 10 000 + 10 000 lignes, plans `EXPLAIN (ANALYZE, BUFFERS)` sous `statement_timeout = 10s`, parcours complets de l'utilisateur concentré ; rapport `reports/r1_pr8_explain_pg<version>.json` (artefact CI) |
| Suites adaptées (matrices fermées) | `test_authz_f2_boundary.py` (inventaire SQL, quatorze commandes, contrôle statique PR 8), `test_authz_f2_runner.py` (registre), `test_authz_f2_http.py` et `test_authz_f2_portfolio_http.py` (liste exacte des routes), `test_authz_f2_dormancy.py` (sonde, matrice d'imports, marqueurs), `test_f2_dormancy.py` (matrice de référence F2) |

Aucun test existant supprimé, sauté ni affaibli. Résultats d'exécution :
voir la description de la Pull Request (PostgreSQL 16.15 et 18.6 locaux,
puis la CI GitHub).

## 18. Plans `EXPLAIN (ANALYZE, BUFFERS)` mesurés

Mesures réelles, locales, sur bases jetables (aucun index ajouté ; seuls
index existants : clés primaires). Jeu : 10 000 `AnalysisSession` et 10 000
`InvestmentThesis`, 21 utilisateurs, dont 6 000 + 6 000 pour un seul
(1 500 tickers de sessions, ⅓ `in_progress` donc plusieurs sessions
actives par ticker, étapes nulles et inconnues, dates égales ; 3 000
tickers de thèses avec casse et caractères spéciaux, 10 % de dates nulles,
dates égales, provenance pour ¼). `ANALYZE` exécuté, cache chaud. Colonnes
: durée d'exécution, planification (ms), lignes rendues (`limit + 1`),
buffers partagés touchés / lus, blocs temporaires écrits, scans (lignes
retenues / écartées par filtre), tris, durée totale par le VRAI runner
(phases A et B, ms).

PostgreSQL 16.15 :

| Parcours | Exéc. | Plan. | Lignes | Buffers hit / read | Temp | Scans | Tris | Runner |
|---|---|---|---|---|---|---|---|---|
| `overview_first_page` | 10.93 | 0.58 | 51 | 4578 / 0 | 0 | Seq Scan analysis_sessions 2000/8000 ×2 ; Seq Scan investment_theses 6000/4000 ×2 | quicksort 81, 197, 469 kB | 15.62 |
| `overview_last_page` | 10.08 | 0.65 | 51 | 4578 / 0 | 0 | Seq Scan analysis_sessions 0/10000 et 2000/8000 ; Seq Scan investment_theses 236/9764 et 6000/4000 | quicksort 25, 197, 469 kB | 11.17 |
| `theses_first_page` | 5.54 | 0.49 | 51 | 2289 / 0 | 0 | Seq Scan investment_theses 6000/4000 ; Seq Scan analysis_sessions 6000/4000 (provenance hachée) | top-N heapsort 34 kB | 7.24 |
| `theses_deep_page_after_date` | 4.69 | 0.47 | 51 | 2289 / 0 | 0 | Seq Scan investment_theses 1999/8001 ; Seq Scan analysis_sessions 6000/4000 | top-N heapsort 34 kB | 7.32 |
| `theses_deep_page_after_null` | 2.32 | 0.47 | 51 | 784 / 0 | 0 | Index Scan (pkey) investment_theses 99/900 ; Seq Scan analysis_sessions 6000/4000 | quicksort 31 kB | 4.89 |
| `theses_filter_ticker` | 2.55 | 0.68 | 4 | 2185 / 0 | 0 | Seq Scan investment_theses 4/9996 ; Index Scan (pkey) analysis_sessions | quicksort 25 kB | 4.86 |
| `thesis_detail` | 0.09 | 0.42 | 1 | 8 / 0 | 0 | Index Scan (pkey) investment_theses ; Index Scan (pkey) analysis_sessions | — | 3.45 |

PostgreSQL 18.6 :

| Parcours | Exéc. | Plan. | Lignes | Buffers hit / read | Temp | Scans | Tris | Runner |
|---|---|---|---|---|---|---|---|---|
| `overview_first_page` | 9.43 | 0.65 | 51 | 4578 / 0 | 0 | Seq Scan analysis_sessions 2000/8000 ×2 ; Seq Scan investment_theses 6000/4000 ×2 | quicksort 193, 181, 422 kB | 13.72 |
| `overview_last_page` | 9.68 | 0.58 | 51 | 4578 / 0 | 0 | Seq Scan analysis_sessions 0/10000 et 2000/8000 ; Seq Scan investment_theses 236/9764 et 6000/4000 | quicksort 25, 181, 422 kB | 11.94 |
| `theses_first_page` | 5.38 | 0.52 | 51 | 2289 / 0 | 0 | Seq Scan investment_theses 6000/4000 ; Seq Scan analysis_sessions 6000/4000 | top-N heapsort 34 kB | 8.75 |
| `theses_deep_page_after_date` | 4.30 | 0.45 | 51 | 2289 / 0 | 0 | Seq Scan investment_theses 1999/8001 ; Seq Scan analysis_sessions 6000/4000 | top-N heapsort 34 kB | 6.62 |
| `theses_deep_page_after_null` | 2.43 | 0.50 | 51 | 784 / 0 | 0 | Index Scan (pkey) investment_theses 99/900 ; Seq Scan analysis_sessions 6000/4000 | quicksort 31 kB | 4.75 |
| `theses_filter_ticker` | 2.10 | 0.51 | 4 | 2185 / 0 | 0 | Seq Scan investment_theses 4/9996 ; Index Scan (pkey) analysis_sessions | quicksort 25 kB | 4.88 |
| `thesis_detail` | 0.13 | 0.56 | 1 | 8 / 0 | 0 | Index Scan (pkey) investment_theses ; Index Scan (pkey) analysis_sessions | — | 3.47 |

Tous les tris en mémoire, aucun bloc temporaire, aucun dépassement du
`statement_timeout = 10s` ; parcours complet des 6 000 thèses et de tous
les tickers de l'utilisateur concentré par curseurs, chaque page sous le
délai. Mesure complémentaire locale à **10×** (100 000 + 100 000 lignes,
50 000 + 50 000 sur un utilisateur, script hors dépôt) : synthèse 68–80 ms,
liste 25–35 ms, filtre 9–12 ms (PostgreSQL 16 et 18), croissance linéaire.
La CI réexécute les mesures à 10 000 + 10 000 sur PostgreSQL 16 et 18 et
publie le rapport en artefact.

## 19. Limites résiduelles connues

- **Aucun index sur `user_id` / `ticker`** de `investment_theses` et
  `analysis_sessions` (exclu par le contrat : aucune migration) : chaque
  page de liste ou de synthèse parcourt séquentiellement les tables
  entières (tous utilisateurs). Coût linéaire en volume total (≈ 10 ms à
  20 000 lignes, ≈ 75 ms à 200 000) ; le délai de 10 s ne serait approché
  qu'à des volumes de l'ordre de dizaines de millions de lignes (non
  mesuré). Un index relèverait d'un amendement architectural.
- **Valeurs non restituables** (ticker > 512 octets, texte > 131072
  octets, date hors 0001–9999 UTC ou infinie, page JSON > 131072 octets,
  curseur suivant > 1024 caractères) : 503 explicite pour la page ou la
  thèse concernée, sans troncature ; une page bloquée empêche d'atteindre
  les pages suivantes par curseur (le filtre ticker, la synthèse et les
  autres thèses restent lisibles). Aucune réparation automatique : décision
  opérationnelle hors PR 8.
- **Cohérence inter-pages** : aucun snapshot entre pages (§8) ; cohérence
  garantie par instruction seulement.
- **Ordre Python = ordre `"C"`** pour la validation des pages : suppose une
  base UTF-8.
- **Redirection de barre oblique finale héritée** : l'application isolée
  (PR 5–PR 7, `redirect_slashes` par défaut de FastAPI) répond 307 vers le
  chemin sans barre pour `/api/v2/me/theses/` comme pour `/api/v2/me/` et
  `/api/v2/me/portfolio/` ; comportement de composition existant, non
  modifié par PR 8 (le corriger changerait PR 5–PR 7) — signalé pour
  décision.
- **NUL** : un filtre ticker ou un `last_ticker` contenant U+0000
  (impossible dans un texte PostgreSQL) est refusé en 400 plutôt que
  transmis (détail d'intégration).
- Garanties logiques (registre fermé, inventaire SQL audité, tests
  négatifs), pas un bac à sable Python.

## 20. Conditions de non-activation F2

PR 8 ne lève aucun blocage de l'activation publique :

1. Routes `/api/user/{user_id}/*` (dont `GET /api/user/{user_id}/theses`
   et `DELETE /api/user/{user_id}/theses/{ticker}`) toujours actives et
   fondées sur un `user_id` client.
2. Aucune migration des identités navigateur ni du `localStorage`.
3. Frontend non adapté (aucun appel aux routes F2).
4. Décrypter et ACK (PR 9) non migrés vers F2.
5. Trousseaux F2, CSRF et portefeuille sans chargeur de production.
6. Montage sur `api:app`, activation et validation Railway : décisions du
   fondateur, hors de cette PR. F2 reste dormant après la fusion de PR 8.
