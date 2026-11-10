# F-02A V0.2 — Registre de routage fermé et retrait des routes legacy (dormant)

## 1. Référentiel

Architecture F-02A V0.2 auditée, corrigée et figée par le fondateur. Aucune décision architecturale nouvelle n'a
été prise ; les choix d'implémentation qui méritent l'attention de l'audit sont listés au § 9.

`main` relu avant toute modification : `d30cbc1e2c01900880464812e0780449b9de00db` (merge de la PR #237),
**identique** au SHA de référence du freeze : aucune différence à intégrer.

| Élément | Avant | Après |
|---|---|---|
| Routes et méthodes de `api:app` | 23 déclarées + 8 automatiques | **inchangées** |
| Middleware de `api:app` | `CORSMiddleware` (`*`, credentials) | **inchangé** |
| Routes F2 montées en production | 0 | 0 |
| Migrations | 21 | 21 (aucune) |
| Dépendances de production | 10 | 10 (aucune) |
| Modules applicatifs important la politique | — | 0 |

## 2. Périmètre exact

**Créé (production, paquet isolé, jamais importé)** — `core/route_policy/` :

- `registry.py` : registre **fermé** et figé (`POLICY_VERSION = "F-02A-V0.2"`), 58 couples (méthode, gabarit),
  une catégorie par gabarit, comptes vérifiés à l'import ;
- `canonical.py` : forme canonique du chemin **brut** (`raw_path`) et appariement exact aux gabarits ;
- `gate.py` : décision pure `decide(method, raw_path)` / `classify(scope)` et porte ASGI `RoutePolicyGate`.

**Créé (tests)** : `tests/test_route_policy.py` (sans base), `tests/test_route_policy_f2.py` (PostgreSQL réel,
compositions F2 réelles), `tests/test_route_policy_dormancy.py` (véritable `api:app`). Cette documentation.

**Inchangé** : `api.py`, `core/auth_f2*`, `core/authz_f2*` (moteurs F2, runner, contrats métier), `core/models.py`,
`alembic/`, `web/`, `web-v2/`, `requirements.txt`, CI, Railway. Aucun test historique modifié ni supprimé.

## 3. Inventaire automatique

L'inventaire est **calculé**, pas recopié : `test_the_real_api_inventory_is_exactly_the_legacy_registry` importe
le véritable `api:app` dans un sous-processus (garde réseau active) et exige l'égalité exacte entre
`{(méthode, chemin)}` de toutes ses routes — y compris les routes ajoutées automatiquement par FastAPI — et la
partie legacy du registre. `test_real_f2_compositions_expose_exactly_the_registered_routes` fait de même avec les
compositions F2 réelles (`build_isolated_f2_app`, `build_isolated_personal_app` avec et sans C3).

Routes automatiques FastAPI découvertes sur `api:app` : `GET` **et** `HEAD` sur `/openapi.json`, `/docs`,
`/docs/oauth2-redirect`, `/redoc` (8 couples). Les `APIRoute` déclarées n'ont **pas** de `HEAD` implicite.

## 4. Matrice de routage finale

Décision indépendante de la méthode pour A, B, C-en-attente et documentation ; méthode exacte exigée pour
`/health` et les routes F2 (sinon 405 avec `Allow`).

| Cat. | Méthode | Chemin | Décision de la porte |
|---|---|---|---|
| A | GET | `/api/user/{user_id}` | 410 `legacy_personal_api_retired` |
| A | PUT | `/api/user/{user_id}/level` | 410 |
| A | GET, POST | `/api/user/{user_id}/portfolio` | 410 |
| A | PUT, DELETE | `/api/user/{user_id}/portfolio/{position_id}` | 410 |
| A | GET | `/api/user/{user_id}/theses` | 410 |
| A | DELETE | `/api/user/{user_id}/theses/{ticker}` | 410 |
| A | POST | `/decryptage` | 410 |
| A | POST | `/api/runtime/assistant-deliveries/{assistant_turn_id}/ack` | 410 |
| A | POST | `/web-chat` | 410 |
| A | POST | `/web-v2/portfolio-analyze` | 410 |
| A | POST | `/education` | 410 |
| A | POST | `/coach` | 410 |
| A | POST | `/checklist` | 410 |
| B | GET | `/web` | 404 |
| B | POST | `/analyze` | 404 |
| B | POST | `/analyze-etf` | 404 |
| B | POST | `/pedagogie/lookup` | 404 |
| C | GET | `/health` | 200 `{"status":"ok"}`, servi par la porte (maintenu) |
| C | GET | `/web-v2` | 404 jusqu'à la validation F-05 du nouveau frontend |
| C | GET | `/web-v2/search` | 404 jusqu'à l'audit (entrées, effets, quotas, délais, coûts) |
| C | GET | `/web-v2/fx-rate` | 404 jusqu'à l'audit |
| D | GET | `/api/auth/f2/csrf/bootstrap` | transmis à l'application F2 d'authentification |
| D | POST | `/api/auth/f2/{sign-in/start, sign-in/resend, sign-in/verify, sign-in/resume, session/resolve, logout, invitation/begin, invitation/complete, invitation/resume, email-link/start, email-link/resend, email-link/verify, email-link/result}` | transmis (13 routes) |
| D | GET | `/api/v2/me` | transmis à l'application personnelle F2 |
| D | PUT | `/api/v2/me/level` | transmis |
| D | GET, POST | `/api/v2/me/portfolio` | transmis |
| D | GET, PUT, DELETE | `/api/v2/me/portfolio/{position_id}` | transmis |
| D | GET | `/api/v2/me/analysis-overview`, `/api/v2/me/theses`, `/api/v2/me/theses/{thesis_id}` | transmis |
| D | POST | `/api/v2/me/decryptage/{turns, replay, ack}` | transmis (Décryptage C3) |
| auto | GET, HEAD | `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` | 404 |
| — | toute | tout autre chemin, ou chemin non canonique | 404 |

Comptes : A = 15 couples (8 sous `/api/user/{user_id}`), B = 4, C = 4, D = 27 (14 + 13), documentation = 8.

## 5. Contrats

**Refus legacy (A)** : `410`, corps exact `{"error":"legacy_personal_api_retired"}` (octets constants, aucun écho),
`Content-Type: application/json`, `Content-Length`, `Cache-Control: no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (mêmes valeurs que les applications F2
isolées), **aucun** `Set-Cookie`, `Location`, `Vary` ni `Access-Control-*`. Toute méthode — y compris `HEAD`
(corps vide), `OPTIONS` (aucun pré-vol CORS) et une méthode inattendue (`PROPFIND`, `FOO`, minuscules) — reçoit ce
refus dès lors que le chemin canonique correspond à un gabarit retiré. Les paramètres (`{user_id}`, `{ticker}`…)
acceptent tout segment canonique non vide.

**Désactivé, candidat en attente, documentation, inconnu, non canonique** : `404` `{"detail":"Not Found"}`,
mêmes en-têtes. Le corps est identique à celui des applications F2 isolées pour un chemin inconnu.

**Méthode non déclarée sur `/health` ou une route F2** : `405` `{"detail":"Method Not Allowed"}`, `Allow` =
méthodes déclarées pour ce gabarit (triées), mêmes en-têtes no-store.

**Routes F2** : transmises **telles quelles** (`scope`, `receive`, `send`), la seule altération étant le retrait
défensif des en-têtes `Access-Control-*` de la réponse (les applications F2 n'en émettent aucun). Les tests
démontrent l'identité octet pour octet (statut, liste ordonnée des en-têtes, corps) avec et sans la porte.

## 6. Garanties et leurs preuves

| Garantie | Mécanisme | Preuve |
|---|---|---|
| Inventaire exhaustif | égalité exacte calculée sur `api:app` et les compositions F2 réelles | `test_the_real_api_inventory_…`, `test_real_f2_compositions_…` |
| Politique figée | matrice écrite indépendamment dans les tests ; comptes vérifiés à l'import ; gabarits sans chevauchement | `test_the_registry_is_exactly_the_frozen_policy`, `_build_index` |
| Aucun repli legacy, aucune transmission implicite vers `api:app` | la porte n'importe pas `api` ; les seules cibles sont deux applications injectées, validées : route hors catégorie, documentation, `Mount` ou `CORSMiddleware` → `RouteGateConfigError` | `test_the_legacy_application_can_never_be_composed`, `test_out_of_policy_applications_are_refused` |
| Chemins inconnus / ambigus refusés | registre fermé, appariement exact par segment, casse comprise | `test_unknown_paths_are_404` |
| Chemins non canoniques refusés, jamais normalisés | décision sur `raw_path` : jeu d'octets `[A-Za-z0-9-._~/]`, aucun segment vide, `.` ou `..`, ≤ 2048 octets ; `root_path` vide ; `path` cohérent avec `raw_path` | `test_non_canonical_variants_are_never_routed` (32 variantes × 49 gabarits), `test_scope_inconsistencies_are_404` |
| Aucun double décodage | aucun `%` accepté : rien n'est jamais décodé, ni par la porte ni (par construction) par l'application cible | `test_traversal_and_double_encoding_…`, vrai serveur uvicorn |
| Aucune redirection | la porte n'émet jamais de 3xx ; une barre oblique finale est non canonique et n'atteint donc jamais `redirect_slashes` de FastAPI | `test_the_gate_never_redirects` |
| HEAD, OPTIONS, méthodes inattendues | décision par gabarit pour A/B/C/doc ; 405 pour `/health` et F2 ; `X-HTTP-Method-Override` ignoré | `test_every_template_and_every_method_…`, `test_method_override_headers_are_ignored` |
| Aucun CORS permissif hérité | aucun en-tête CORS émis ; pré-vols F2 refusés (405) sans atteindre F2 ; `Access-Control-*` retiré des réponses transmises ; `CORSMiddleware` refusé en aval | `test_cors_is_never_emitted_nor_inherited`, `test_cors_preflights_never_reach_f2_…` |
| `/docs`, `/redoc`, `/openapi.json` non exposés | catégorie documentation → 404 ; application aval avec documentation refusée | `test_disabled_pending_and_documentation_routes_are_404_…` |
| Refus sans base, réseau, fournisseur ni handler | la porte répond seule, sans lire le corps ; gardes actives (`socket`, `psycopg2.connect`, `Engine.connect`, `create_engine`, `FastAPI/Starlette/Router.__call__`, `anthropic.Anthropic`, `requests.Session`) ; sous-processus : aucun module `api`, framework, base, fournisseur ou F2 chargé | `test_refusals_touch_no_database_…`, `test_the_policy_package_loads_no_…`, `test_refusals_with_a_valid_f2_session_touch_nothing` (état durable PostgreSQL identique, aucun appel runner / moteur / Supabase / Resend / EODHD / FMP / Claude) |
| Aucun cookie ni donnée personnelle dans les refus | corps constants ; en-têtes fixes | `test_refusals_never_echo_request_data_nor_set_cookies` |
| Contrats F2 inchangés, un seul autoriseur | transmission sans réécriture ; aucune authentification dans la porte | `test_every_personal_route_is_byte_identical_…`, `test_every_auth_route_refusal_is_byte_identical_…`, `test_the_full_browser_journey_crosses_the_gate`, `test_portfolio_and_decryptage_mutations_cross_the_gate` |
| Comportement réel du serveur | uvicorn/h11 réel, requêtes HTTP brutes (forme absolue, `OPTIONS *`, `%2e%2e`, `..`, `;`) | `test_a_real_uvicorn_server_applies_the_same_decisions` |
| Dormance intégrale | `api.py` ne mentionne pas la politique ; aucun module applicatif ne l'importe ; `api:app` garde routes, middleware, CORS, documentation, redirections et statuts historiques avec ou sans variables d'activation | `tests/test_route_policy_dormancy.py` + dormances historiques inchangées |

## 7. Interface pour F-04

```python
from core.route_policy.gate import RoutePolicyGate

gate = RoutePolicyGate(
    f2_auth_app=build_isolated_f2_app(...),          # ou None : routes d'authentification -> 404
    f2_personal_app=build_isolated_personal_app(...), # ou None : routes personnelles -> 404
)
# gate est une application ASGI 3 à servir directement (uvicorn), SANS root_path.
```

- Arguments nommés **obligatoires**, sans valeur par défaut ; validation stricte à la construction.
- `decide(method, raw_path)` et `classify(scope)` sont exposés pour la journalisation ou l'observabilité F-04
  (aucun journal dans la porte : rien de personnel n'y transite).
- Portée `lifespan` acquittée par la porte seule ; portée `websocket` fermée (1008) avant acceptation.

## 8. Limites connues

1. **`/health` ne répond pas à `HEAD`** (405), exactement comme `api:app` aujourd'hui. Si la sonde Railway
   devait utiliser `HEAD`, le registre devrait être révisé (F-04).
2. **En-tête `Allow` des 405 F2** : la porte liste toutes les méthodes déclarées du gabarit (ex. `GET, POST`
   pour `/api/v2/me/portfolio`), alors que FastAPI n'indique que celles de la première route trouvée. Statut,
   corps et en-têtes no-store sont identiques ; seul `Allow` est plus exact. Les tests le figent.
3. **`raw_path` obligatoire** : un serveur ASGI qui ne le fournit pas (ou y laisse la chaîne de requête) voit
   toutes ses requêtes refusées (404). uvicorn le fournit conformément à la spécification.
4. **Paramètres legacy encodés** : un ancien chemin dont un segment exigerait un encodage-pourcent (ex.
   `/api/user/%5EFCHI/theses`) n'est pas canonique et reçoit 404, pas 410. Refusé dans les deux cas.
5. **Lifespan** non transmis aux applications injectées : les compositions F2 actuelles n'en ont pas ; F-04
   devra réviser ce point si une application aval en définit un.
6. Le corps des requêtes refusées n'est jamais lu ; la fermeture ou la vidange de la connexion relève du
   serveur (comportement standard d'uvicorn).

## 9. Points restant à valider

**F-03** : la suppression éventuelle des anciens comptes, portefeuilles, niveaux, thèses et conversations est
hors périmètre (chantier séparé et sécurisé) ; F-02A ne touche ni tables, ni comptes, ni historiques. Aucune
migration n'est demandée (plus aucun testeur).

**F-04** (composition ASGI) :
- brancher `RoutePolicyGate` comme **unique** application servie, à la place de `api:app` ;
- configuration production des deux compositions F2 (secrets, origines, résolveur d'IP derrière le proxy
  Railway, initialisation anti-abus explicite) ;
- décider de la sonde de santé (`GET /health`) et de la politique `HEAD` ;
- vérifier `root_path` vide et `proxy_headers` cohérents avec le résolveur d'IP F2 ;
- retirer `api:app` du point d'entrée **sans** supprimer ses contre-épreuves historiques ;
- vérification Railway après merge (hors de cette PR).

**F-05** : validation du nouveau frontend F2 servi sur `GET /web-v2` ; jusque-là, 404. L'activation exigera une
révision du registre (`PUBLIC_PENDING_F05` → catégorie servie) et de ses tests.

**Audit `GET /web-v2/search` et `GET /web-v2/fx-rate`** : entrées (longueur et forme de `q`), effets, quotas,
délais et coûts des fournisseurs de marché (`core/market_lookup.py`). Tant que l'audit n'est pas conclu, 404.

## 10. Exécution

- `tests/test_route_policy.py` : sans base de données (aucune variable requise).
- `tests/test_route_policy_f2.py` : `ORYX_TEST_DATABASE_URL` vers une base jetable (sinon ignoré, comme les
  autres tests PostgreSQL).
- `tests/test_route_policy_dormancy.py` : sous-processus à environnement contrôlé.
