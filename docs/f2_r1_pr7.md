# R1 PR 7 — CRUD de portefeuille F2 versionné et idempotent (dormant)

Références normatives : **V2C consolidée `PR7-V2C-2026-10-26`** et
**addendum de fermeture `PR7-V2D-2026-10-26`**, architecture figée par le
fondateur le 26 octobre 2026 (la V2D prévaut pour les points qu'elle
consolide). Ces documents ne sont pas versionnés dans le dépôt : le contrat
appliqué est celui reproduit dans la demande d'implémentation, sans
décision architecturale nouvelle.

Référence de compatibilité : `main` au SHA
`59265c8fd4ec6300f6605f1da3a9e0114e5ef512` (PR #226, R1 PR 6 fusionnée),
identique au `main` au moment de l'implémentation. **Une migration additive**
(`0020_position_revision`, nouvelle tête) ; aucun trigger, aucune table
d'idempotence nouvelle, aucune dépendance de production, aucune variable
d'environnement.

> **PR7 implémente un CRUD versionné et idempotent dans l'application
> personnelle F2 isolée. Elle ne sécurise ni les routes historiques ni les
> anciens identifiants navigateur. Elle n'autorise pas l'activation
> publique de F2.**

Principe produit inchangé : **« Décrire, jamais prescrire. »** Aucune
somme de cibles imposée, aucune suggestion d'allocation, aucune donnée
pédagogique (observation, preuve, inférence, C1–C12) n'est créée.

## 1. Objectif et périmètre

| Élément | Fichier |
|---|---|
| Migration `0020_position_revision` | `alembic/versions/0020_position_revision.py` |
| Modèle `PortfolioPosition.position_revision` + CHECK | `core/models.py` |
| Saisie décimale, `binary64-decimal-v1`, diagnostic historique | `core/authz_f2/portfolio_values.py` (nouveau, pur) |
| Trousseau HMAC portefeuille, trame `oryx-portfolio-request-v1` | `core/authz_f2/portfolio_fingerprint.py` (nouveau, pur) |
| Résultats et reçus `portfolio-write-result-v1` | `core/authz_f2/portfolio_results.py` (nouveau, pur) |
| Trois commandes, `PositionView` étendue, plan `PORTFOLIO_MUTATION` | `core/authz_f2/commands.py` |
| Adaptateurs SQL (reçu, position, CAS, reçus), états fermés, registre à onze commandes | `core/authz_f2/_adapters.py` |
| Trousseau portefeuille, READ COMMITTED, états fermés, `IDEMPOTENCY_CONFLICT` | `core/authz_f2/runner.py` |
| Cinq routes `/api/v2/me/portfolio` | `core/authz_f2_http/portfolio.py` (nouveau) |
| Composition isolée (trousseau portefeuille, indépendance des clés, isolation) | `core/authz_f2_http/isolated.py` |

Inchangés : `api.py`, routes `/api/user/{user_id}/*`, identités navigateur,
import `localStorage`, `web-v2/index.html` et tout frontend, Coach,
Éducation, Décryptage, Portfolio Analysis, thèses, conversations,
observations, preuves, inférences, C1–C12, migrations 0001–0019,
`core/auth_f2*` (PR 2 à PR 4), plans `READ` et `PROFILE_MUTATION`, routes
`GET /api/v2/me` et `PUT /api/v2/me/level`, délais `lock_timeout = 5s` /
`statement_timeout = 10s`.

## 2. Contrat des cinq routes

| Méthode | Route | Commande | Succès |
|---|---|---|---|
| GET | `/api/v2/me/portfolio` | `ListPositions` | 200 `{"positions": [vue...]}` (position_id croissant) |
| GET | `/api/v2/me/portfolio/{position_id}` | `ReadPosition` | 200 vue, sinon 404 `position_not_found` |
| POST | `/api/v2/me/portfolio` | `CreatePortfolioPosition` | 201 + `Location: /api/v2/me/portfolio/{id}` |
| PUT | `/api/v2/me/portfolio/{position_id}` | `ReplacePortfolioPosition` | 200 |
| DELETE | `/api/v2/me/portfolio/{position_id}` | `DeletePortfolioPosition` | 200 |

En-têtes des mutations : `X-Oryx-Operation-Id` (POST, PUT, DELETE) et
`X-Oryx-Expected-Revision` (PUT, DELETE), uniques. Corps POST / PUT :
objet JSON exact de cinq clés `ticker`, `quantity`, `purchase_price`,
`target_percent`, `envelope` (optionnels explicitement `null`) ; PUT est un
**remplacement complet**, jamais un PATCH. DELETE : **aucun corps**, aucun
Content-Type requis.

Exemple de corps 201 (JSON canonique, clés triées) :

```json
{"operation_id":"11111111-1111-4111-8111-111111111111","operation_type":"portfolio_position_create","outcome":"created","position":{"data_status":"valid","envelope":"PEA","numeric_encoding":"binary64-decimal-v1","position_id":42,"position_revision":"0","purchase_price":null,"quantity":"4","target_percent":null,"ticker":"AIR.PA","validation_issues":[]},"position_id":42}
```

## 3. Identité serveur

Le propriétaire est **exclusivement** `AuthorizedPrincipal.user_id`,
construit par le runner à `T_authorization` depuis les lignes de session
verrouillées. Aucune commande ne porte de `user_id` ; un `user_id` client
(clé JSON inconnue, query) est refusé (400) ; aucun ancien UUID navigateur
n'est lu. Tout SQL individuel filtre `id = :id AND user_id = :u`
(`:u` = identité serveur) ; le reçu est cherché par
`(user_id serveur, operation_type, operation_id)`.

## 4. Modèle de position et migration 0020

`portfolio_positions` gagne **une** colonne :

```sql
position_revision BIGINT NOT NULL DEFAULT 0
CONSTRAINT ck_portfolio_positions_position_revision_non_negative CHECK (position_revision >= 0)
```

- Lignes existantes et nouvelles : révision 0 (aucun backfill : ADD COLUMN
  avec constante ne réécrit pas la table — `relfilenode` inchangé, testé).
  **La révision 0 est une origine technique, pas une preuve d'absence de
  modification antérieure.**
- Révision interne : entier exact `0 … 9223372036854775807` ; HTTP : chaîne
  décimale canonique ; ni recyclage, ni débordement, ni remise à zéro.
- Champs financiers historiques : `Float` / binary64 **inchangés** (aucune
  conversion NUMERIC).
- `portfolio_write_idempotency` (0016) est réutilisée telle quelle.
- Garde-fous : `lock_timeout 5s`, `statement_timeout 15s`,
  `idle_in_transaction_session_timeout 15s` (SET LOCAL).

**Downgrade protégé**, une seule transaction de maintenance :

1. `LOCK TABLE portfolio_positions IN ACCESS EXCLUSIVE MODE` **en premier**
   (un writer PR 7 déjà engagé sur les positions termine avant ; un writer
   qui n'a encore verrouillé que des reçus — ROW SHARE — attend ensuite les
   positions sans bloquer l'étape 2 : aucun cycle) ;
2. `LOCK TABLE portfolio_write_idempotency IN SHARE MODE` (aucune
   insertion de reçu concurrente) ;
3. refus (exception, rien n'est modifié) s'il existe **un reçu** ou **une
   révision ≠ 0** ;
4. sinon retrait du CHECK puis de la colonne (retour exact à 0019).

Mode offline : le contrôle est émis en bloc `DO $$ … RAISE EXCEPTION … $$`
évalué à l'exécution du script ; aucune donnée n'est inspectée à la
génération. La descente de 0016 n'est pas modifiée ; tant que des reçus
existent, le refus de 0020 empêche toute descente au-delà.

## 5. CAS, no-op, ABA, révision maximale

PUT effectif : **un** UPDATE conditionnel
`WHERE id = :id AND user_id = :u AND position_revision = :r`, qui écrit les
cinq champs, `updated_at = T_authorization` et
`position_revision = position_revision + 1`, relu par `RETURNING`
(valeurs et révision + 1 vérifiées). DELETE :
`DELETE … WHERE id AND user_id AND position_revision = :r RETURNING`.

| État | Résultat |
|---|---|
| Révision exacte, valeurs différentes | 200, révision + 1 |
| Révision exacte, valeurs identiques | 200, **aucun UPDATE** (xmin, `updated_at` inchangés), reçu enregistré |
| Révision périmée (inférieure ou future), valeurs différentes ou identiques | 409 `position_revision_conflict`, aucune écriture |
| Position absente ou étrangère (sans reçu) | 404 `position_not_found` indiscernable |
| Révision maximale, valeurs identiques | 200 sans UPDATE |
| Révision maximale, modification effective | 503, alerte `ERROR … alert=position_revision_exhausted` |
| DELETE à révision exacte, même maximale | 200, suppression |
| DELETE à révision périmée | 409, aucune suppression |

La révision est comparée **avant** les valeurs. L'identité des valeurs est
jugée en `binary64-decimal-v1` (§6), jamais par une sortie texte de
PostgreSQL. Deux no-op d'`operation_id` distincts réussissent tous deux.
`A → B → A` incrémente deux fois ; l'ancienne révision rend 409. Une
écriture conditionnelle sans ligne après des contrôles réussis sous verrou
est une violation d'invariant (ROLLBACK, 503), jamais un faux conflit. Une
position historique invalide est corrigeable par un PUT complet à révision
exacte ; DELETE n'exige aucune réparation préalable.

## 6. Représentation financière

**Saisie** (chaînes JSON, jamais des nombres JSON) :
`(?:0|[1-9][0-9]*)(?:\.[0-9]{0,5}[1-9])?` ASCII, ≤ 21 caractères (borne
jugée avant toute analyse), ≤ 15 chiffres significatifs, ≤ 6 décimales ;
ni signe, exposant, blanc, zéro superflu, chiffre Unicode, NaN ni
Infinity. Analyse `Decimal`, conversion binary64 finie, exigence
`Decimal(repr(f)) == valeur saisie` (aucun arrondi silencieux ; ce n'est
pas une affirmation d'exactitude décimale du flottant).

| Champ | Domaine |
|---|---|
| `quantity` | ]0, 10^12] |
| `purchase_price` | `null` ou [0, 10^12] |
| `target_percent` | `null` ou [0, 100] |

Ticker d'une nouvelle écriture : `[A-Z0-9][A-Z0-9.^=-]{0,31}` exact (ni
trim, ni majuscules automatiques) ; enveloppe : `null`, `PEA`, `CTO`.
Domaine technique du premier contrat F2, pas une classification des
instruments.

**Sortie `binary64-decimal-v1`** : `repr` CPython (round-trip le plus
court, identique de 3.11 à 3.13) rendu en notation fixe sans exposant ni
zéro fractionnaire inutile ; `+0.0` et `-0.0` → `"0"` ; `NaN`, `Infinity`,
`-Infinity` toujours en **chaînes**, jamais en constantes JSON. Lecture
des flottants par `float8send(col)` (8 octets IEEE 754) : indépendante de
`extra_float_digits` (testé à −15, 0, 1, 3). Écriture par le paramètre
`float` du pilote (texte `repr`, relu exactement par `float8in`). Vecteurs :
subnormaux (`5e-324` → `"0." + 323 zéros + "5"`), plus grand subnormal,
plus petit normal, `±max`, `1e15/1e16`, `1e-4/1e-5`, `1e21/1e22`, 2^53.
Les renderers versionnés restent disponibles (`RENDERERS`).

## 7. Lecture diagnostique de l'historique

Vue publique fermée : `position_id` (entier), `position_revision`
(chaîne), `ticker`, `quantity`, `purchase_price`, `target_percent`
(chaînes `binary64-decimal-v1` ou `null`), `envelope`,
`numeric_encoding = "binary64-decimal-v1"`, `data_status`
(`valid` | `legacy_invalid`), `validation_issues` — **aucune date**.

Un champ est valide si sa représentation serait acceptée telle quelle par
une nouvelle écriture. Codes fermés, dans l'ordre, un au plus par champ :
`ticker_invalid`, `quantity_invalid`, `purchase_price_invalid`,
`target_percent_invalid`, `envelope_invalid`. Calculé à la lecture, jamais
enregistré ; GET ne répare jamais. Structure SQL impossible (type,
révision hors bornes, quantité absente) : 503. Les chaînes historiques ne
sont **jamais** présumées sûres pour une insertion HTML : tout futur
affichage doit les échapper.

## 8. Format HMAC normatif

`oryx-portfolio-request-v1` : `ASCII("ORYX-PORTFOLIO-WRITE") || 0x00`
puis douze champs `u32be(len(payload)) || payload` (`00` = null, `01 ||
UTF-8(valeur)`), dans l'ordre : `fingerprint_version`,
`fingerprint_key_version`, `user_id` serveur, `operation_type`,
`operation_id` (UUID minuscule), `position_id`, `expected_revision`,
`ticker`, `quantity`, `purchase_price`, `target_percent`, `envelope`.
HMAC-SHA256, hexadécimal minuscule, `hmac.compare_digest`. Jamais le JSON
brut.

Vecteurs (clés fictives `k1 = 00..1f`, `k2 = 1f..00`, `test-user-a`),
reproduits à l'octet près (`tests/test_authz_f2_portfolio_crypto.py`, avec
trames littérales indépendantes de l'encodeur) :

| Cas | Trame | HMAC |
|---|---|---|
| CREATE | 190 | `a1ca9277cee69dfd6f17a4d59eeeb03f45c7bed45244cae5ec91acda1551f178` (SHA256 trame `693a31b9…98e7b1b9`) |
| CREATE, cible `"0"` | 191 | `336a1e13ddd8f733badb6ac759075ad0beec6c0fc920bd6f48cd8d0e257f0690` |
| CREATE, `test-user-b` | 190 | `e8b52988265e91bab428c5d1699a0a6a5eb9541dd38bb279dd877de5faf01a48` |
| CREATE, UUID `4444…` | 190 | `93d78397ee7c88fc4fa638da24ea10e7e5ec643a9a6866c6cf142586eea13d41` |
| CREATE, clé `k2` | 190 | `b19aac16d74b057862f2442dbf06556c39b1acc8946bc1d27f0d567f6740f54a` |
| UPDATE, révision 7 | 202 | `02f7df95ed54673383a03ba75a4bc75da2ba8ca049bc671ec3a5c20aeb589924` |
| UPDATE, révision 8 | 202 | `253c923a490c804b4cadbdd0f7696918d96b66470a7c99069bf0a8e0065d804d` |
| DELETE, révision 8 | 183 | `2b134527d162394f7b3dcf36f3234559c4d865d6f43004ca42769e2c276ac825` (SHA256 trame `a177615…59fad9ff`) |

## 9. Trousseau portefeuille et rotation

`PortfolioKeyring({version: clé}, current)` : versions
`[a-z0-9][a-z0-9_-]{0,31}`, clés de 32 octets, distinctes entre versions ;
à la composition (`build_isolated_personal_app`), aucune clé ne peut
égaler une clé F2 (six usages, toutes versions) ni une clé CSRF. Aucune
variable d'environnement, aucun chargeur de production : PR 7 n'active ni
ne configure de secret. Trousseau absent : mutations 503 (lectures
inchangées).

Une version est immuable et jamais réattribuée. Rotation : (1) déployer un
trousseau lisant l'ancienne et la nouvelle clé, (2) vérifier toutes les
instances, (3) basculer `current`, (4) conserver les anciennes clés tant
que des reçus les désignent (pas de TTL en R1). Un rejeu utilise
**toujours** la version de canonicalisation et la version de clé du reçu ;
version ou clé introuvable : 503 sans mutation ni repli
(`alert=portfolio_key_unavailable`).

**Modèle de menace** : le HMAC protège l'**intention** de requête ; il ne
signe pas le résultat JSONB. La frontière de confiance inclut le serveur
et son rôle SQL : aucune garantie contre une modification SQL malveillante
du payload par un acteur privilégié (aucun MAC de résultat sans nouvelle
décision). Une version réattribuée à une autre clé (configuration
interdite) n'est pas détectable : ses rejeux rendraient 409.

## 10. Résultats et reçus durables

`portfolio-write-result-v1`, construit, validé et sérialisé **avant
COMMIT**, inséré dans la même transaction que la mutation ; le runner
n'annonce le succès qu'après le COMMIT confirmé.

- CREATE (201) / UPDATE (200) : `operation_id`, `operation_type`,
  `position_id`, `outcome` (`created` | `replaced`), `position` (vue §7) ;
  révision 0 (CREATE), attendue (no-op) ou attendue + 1.
- DELETE (200) : `operation_id`, `operation_type`, `position_id`,
  `outcome = "deleted"`, `deleted_revision` (= attendue).

JSON compact, clés triées, `ensure_ascii=False`, UTF-8, jamais NaN /
Infinity natifs, ≤ 16 Kio ; aucun `user_id`, secret, HMAC ni date.
Colonnes : `fingerprint_version`, `fingerprint_key_version`,
`request_fingerprint`, `result_version`, `result_status`,
`result_payload` (JSONB), `position_id`, `created_at = T_authorization`.
Aucun reçu d'échec, pas de TTL, pas de purge, pas de table pending.

Rejeu : `result_payload::text` relu, analysé strictement (doublons,
constantes, nombres non entiers refusés), validé (clé primaire, identité,
versions, statut, `position_id`, payload fermé, types, bornes, vue
recalculée et diagnostic cohérent), puis empreinte recalculée ; corps
reconstruit par le même renderer versionné : **mêmes octets** que la
réponse d'origine (testé de bout en bout). Toute incohérence : 503
(`alert=portfolio_receipt_invalid`), aucune réexécution. Après empreinte
identique, le résultat doit aussi correspondre à l'intention (valeurs,
position, révision), sinon 503.

## 11. Plan de verrouillage BF2 et isolation

`PORTFOLIO_MUTATION = LockPlan("portfolio_mutation", "FOR UPDATE",
("portfolio_write_idempotency", "portfolio_positions"), True)`. Phase B :

0. `SELECT current_setting('transaction_isolation')` = `read committed`,
   sinon refus fermé (503, `alert=portfolio_isolation_refused`) ;
1. `users FOR UPDATE` ; 2. `oryx_sessions FOR SHARE` ;
3. `oryx_session_generations FOR SHARE` ;
4. reçu du triplet `FOR UPDATE` → `ReceiptFound` ;
5. sans reçu : CREATE → `CreateReady` (aucune position verrouillée) ;
   PUT / DELETE → position propre `FOR UPDATE` → `PositionFound` ou
   `PositionMissing` ;
6. `T_authorization = clock_timestamp()` ; 7. revalidation intégrale
   (cryptographie, version exacte de clé, session, génération, livraison
   confirmée, révocation, `T < absolute_expires_at`, identité, profil) ;
8. exécution ; 9. reçu ; 10. COMMIT.

L'état verrouillé est d'un type **exact** du registre (`Spec.states`) ;
l'ancien refus d'`ABSENT` avant l'exécution est remplacé par cet état
fermé, jugé après l'autorisation. L'absence d'un reçu n'est **jamais** un
verrou : deux opérations du même compte sont sérialisées par `users FOR
UPDATE`, et la seconde relit (READ COMMITTED, nouvel instantané par
instruction) le reçu committé par la première. Sous REPEATABLE READ ou
SERIALIZABLE, refus fermé (testé). Le niveau global PostgreSQL et les
chemins PR 5 / PR 6 ne sont pas modifiés ; une isolation explicitement
configurée sur le moteur autre que READ COMMITTED est refusée à la
composition.

**Graphe de dépendances** (tous testés sur connexions réelles, dans les
deux ordres quand ils diffèrent, `pg_stat_database.deadlocks` inchangé) :

| Parcours | Comportement |
|---|---|
| PR 7 / PR 7 même compte (CREATE, UPDATE, DELETE, rejeu, no-op) | sérialisés sur `users FOR UPDATE` |
| PR 7 / PR 7 comptes distincts | aucune attente |
| PR 7 / mutation de profil PR 6 | même rang `users FOR UPDATE`, sérialisés |
| PR 7 / résolution de session (génération FOR UPDATE) | attend le FOR SHARE du runner, aucun rang remonté |
| PR 7 / révocation, logout (session puis génération FOR UPDATE) | attend le COMMIT ; une révocation antérieure est vue |
| PR 7 / connexion OTP (users FOR KEY SHARE, FK) | attend `users FOR UPDATE` ; PR 7 attend une finalisation en cours |
| PR 7 / liaison e-mail (finalisation : users FOR UPDATE) | même rang, sérialisés, deux ordres |
| PR 7 / lecteurs F2 (READ : users FOR KEY SHARE) | la lecture attend le COMMIT (borné par lock_timeout) |
| FK `positions.user_id`, `receipts.user_id` → users | FOR KEY SHARE pris par la transaction qui tient déjà FOR UPDATE : aucune dépendance inverse |
| Séquence `portfolio_positions_id_seq` | `nextval` non transactionnel : aucune attente, trous possibles après rollback |
| Migration / downgrade 0020 | verrous de table ACCESS EXCLUSIVE puis SHARE, `lock_timeout` 5 s |
| Écritures historiques | voir §16 (limite explicite) |

Aucune promesse d'absence de deadlock pour tout programme futur : une
erreur d'interblocage ou un délai est un 503, sans retry automatique.

## 12. HTTP, CSRF, framing et priorité des erreurs

Ordre (premier échec déterminant ; aucun refus de transport n'ouvre de
transaction ; vérifié par instrumentation) :

| # | Contrôle | Refus |
|---|---|---|
| 1 | routage / méthode | 404 / 405 (inchangés) |
| 2 | provenance (GET : politique PR 5 ; mutations : Origin obligatoire = origine du Host, Sec-Fetch-Site same-origin s'il est présent, Referer même origine, doublons refusés) | 403 `origin_refused` |
| 3 | en-tête Cookie (parseur strict PR 3) | 400 |
| 4 | CSRF `oryx-f2-csrf-v1` (mutations ; `X-Oryx-CSRF`, `__Host-oryx-csrf-binding`) | 503 indisponible / 403 `csrf_invalid` |
| 5 | `X-Oryx-Generation-Id` | 400 |
| 6 | aucune query | 400 |
| 7 | `position_id` : `[1-9][0-9]{0,9}`, ≤ 2147483647, chemin brut sans encodage `%` | 400 |
| 8 | `X-Oryx-Operation-Id` unique, UUID canonique minuscule | 400 |
| 9 | `X-Oryx-Expected-Revision` unique, `0` ou `[1-9][0-9]*`, ≤ 19 chiffres ASCII, ≤ 2^63 − 1 | 400 |
| 10 | Content-Type `application/json` [`; charset=utf-8`] (POST, PUT) | 415 `invalid_request` |
| 11 | framing : `Transfer-Encoding` visible, `Content-Length` dupliqué ou mal formé | 400 |
| 12 | taille / corps : CL > 4096 → 413 sans lecture ; flux > 4096 → 413 ; discordance, troncature, déconnexion → 400 ; GET / DELETE : CL > 0 → 400 sans lecture, flux non vide → 400 | 413 / 400 |
| 13 | JSON (objet, cinq clés, sans doublon, sans constante ni nombre, rien après) puis champs dans l'ordre fermé | 400 |
| 14 | cookie `__Host-oryx-active-<generation_id>` EXACT | 401 `session_invalid` |
| 15 | runner disponible | 503 |
| 16 | autorisation transactionnelle | 401 / 404 / 409 / 503 |
| 17 | résultat vérifié (type, génération, opération, octets re-rendus) | 503 |

Codes : 200, 201, 400 `invalid_request`, 401 `session_invalid`, 403
`origin_refused` / `csrf_invalid`, 404 `position_not_found`, 409
`idempotency_conflict` / `position_revision_conflict`, 413
`request_too_large`, 415 `invalid_request`, 503 `temporarily_unavailable`.
Corps d'erreur `{"error":"code"}`. Toute réponse : `Cache-Control:
no-store`, `Pragma: no-cache`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, **aucun `Set-Cookie`**, aucun CORS. Un 409
ne divulgue jamais la révision courante. Journaux : route, statut, code
(ou alerte / type d'exception) — jamais secret, cookie, HMAC, empreinte,
UUID d'opération, donnée financière, corps ni SQL paramétré (testé).

## 13. Priorités métier sous autorisation

Reçu présent : structure → empreinte selon ses versions → identique :
résultat d'origine ; différente : 409 `idempotency_conflict` ;
clé / version / reçu invalide : 503. Aucun reçu : propriété (404) →
révision (409) → no-op, modification ou suppression → reçu → COMMIT.
Le reçu est examiné **avant** de conclure à l'absence de la position :
rejouer un DELETE committé rend 200 ; un POST rejoué après suppression
rend son ancien 201 sans recréation ; un PUT ancien rend son snapshot ; une
**nouvelle** opération sur une position disparue rend 404. Un reçu d'un
autre compte n'est jamais interrogé (même UUID ailleurs : autre clé) ; la
réutilisation d'un `operation_id` sur une autre cible rend 409
`idempotency_conflict` sans révéler l'existence de la cible.

## 14. Commit incertain

COMMIT dont l'issue est inconnue : 503, jamais « annulé », aucun retry,
aucun nouvel `operation_id`. Le client répète la **même** intention (même
UUID, session admissible) : si le COMMIT a réussi, le reçu est retrouvé et
restitué ; s'il a été annulé, une seule exécution a lieu. Un 503 ne
signifie donc jamais qu'aucune écriture n'a été committée. Fautes
injectées et testées sans demi-état : avant opération (`authorized`),
après la modification (`position_written`), avant le reçu
(`receipt_ready`), après le reçu (`receipt_written`), avant COMMIT
(`executed`), COMMIT refusé, COMMIT incertain, crash du processus.

## 15. Concurrence (résultats sous sessions admissibles, sans panne)

Deux POST d'UUID distincts : deux positions. Même triplet (deux sessions,
ou quatre **processus** indépendants) : une seule position, mêmes octets
pour tous. Deux PUT effectifs depuis la même révision : un succès, un 409.
Deux no-op : deux succès à révision inchangée. Suppression gagnante :
le PUT concurrent rend 404 ; PUT gagnant : la suppression à l'ancienne
révision rend 409. Contrainte inattendue, interblocage, délai : 503,
jamais un conflit métier.

## 16. Limites de confiance

- **Écritures historiques** (`/api/user/{user_id}/portfolio*`, `user_id`
  client) : elles écrivent sans faire avancer `position_revision`. Le CAS
  F2 ne les détecte pas (`tests/test_authz_f2_portfolio.py::
  test_legacy_writer_bypasses_the_revision_known_limit`) ; un UPDATE
  historique d'une ligne non verrouillée n'attend pas une mutation PR 7
  (seule une insertion historique attend, via la FK vers users). Les
  garanties ne valent qu'à la frontière F2.
- Frontière de confiance : serveur + rôle SQL (§9).
- Garanties logiques (registre fermé, inventaire SQL audité, tests
  négatifs), pas un bac à sable Python.
- Le diagnostic `legacy_invalid` décrit la conformité au contrat de saisie,
  pas la justesse financière d'une position.
- La révision 0 n'atteste aucune absence d'historique de modification.

## 17. Tests et résultats

| Suite | Contenu |
|---|---|
| `tests/test_migration_0020_position_revision.py` | chaîne et identifiant, 0001–0019 inchangées (sha256), SQL offline exact (upgrade, downgrade verrouillé + garde DO), modèle ; montée sur données historiques (révision 0, aucune réécriture, autres tables intactes), défaut / CHECK / bornes, insertion historique, downgrade vierge et exact, refus (reçu, révision ≠ 0) en ligne et offline, attente d'un writer puis échec fermé, verrous excluant reçus et écritures concurrentes |
| `tests/test_authz_f2_portfolio_crypto.py` | huit vecteurs à l'octet, trames littérales, changement isolé de chaque champ, null / zéro / chaîne vide, Unicode, UUID, versions de clé, trousseau invalide, rotation, rétention, redémarrage, clé absente, comparaison constante, indépendance F2 / CSRF |
| `tests/test_authz_f2_portfolio_values.py` | vecteurs `binary64-decimal-v1` (subnormaux, extrêmes, exposants, spéciaux), saisie valide / invalide, domaines, tickers, enveloppes, vues diagnostiques, payloads corrompus, JSON strict, 16 Kio, commandes typées |
| `tests/test_authz_f2_portfolio.py` | CAS (matrice, no-op, ABA, 2^53, maximum), réparation, invariants, idempotence (rejeux, après suppression, conflits, autre compte / type, reçus corrompus), rotation, fautes à chaque point, commit incertain, historique mixte, `extra_float_digits`, autorisation prioritaire, propriété, READ COMMITTED, ordre des verrous, délai, aucune donnée pédagogique |
| `tests/test_authz_f2_portfolio_concurrency.py` | §11 et §15 sur connexions réelles et processus spawn ; expiration pendant l'attente du profil, de la session, de la génération, du reçu et de la position ; révocations ; charge mixte |
| `tests/test_authz_f2_portfolio_http.py` | cinq routes, vues exactes, 201 + Location, rejeu octet pour octet, identifiants non canoniques, provenance, CSRF, cookies, query, Content-Type, framing ASGI brut, 4096 / 4097, JSON, champs, ordre instrumenté, runner absent / en panne / résultats incohérents, composition, inter-comptes, absence de fuite, parcours bout en bout |
| Suites adaptées | `test_authz_f2_runner.py` et `test_authz_f2_concurrency.py` : la mutation de démonstration est remplacée par `ReplacePortfolioPosition` (mêmes scénarios, CAS en plus) et son refus est testé ; `test_authz_f2_boundary.py` : inventaire SQL, onze commandes, plans ; `test_authz_f2_http.py` : liste des routes ; dormance ; tests épinglant la tête Alembic (0020, 20 fichiers) et comparateur de métadonnées (écart `position_revision` retiré seulement avant 0020) |

Résultats d'exécution locale : voir la description de la Pull Request
(PostgreSQL 16 local ; PostgreSQL 16 et 18 par la CI).

## 18. Compatibilité PR 1 à PR 6

Aucun test existant supprimé ni affaibli. Adaptations strictement
contractuelles : tête Alembic et nombre de fichiers (20) ; `PositionView`
étendue (quantité en chaîne `binary64-decimal-v1`) ; registre à onze
commandes ; plan `USER_MUTATION` retiré avec la mutation de démonstration
qu'il servait seul ; `NOT_FOUND` d'une mutation jugé après `authorized` ;
statuts fermés (six) ; matrices d'imports étendues des seules entrées
nouvelles ; données historiques de `test_pg_full_chain_0014…` avec la
nouvelle colonne à 0 ; refus de downgrade de 0019 testé depuis 0019.

## 19. Dormance

`api.py` inchangé ; sur le vrai `api:app`, en sous-processus, avec ou sans
variables évoquant une activation (dont `ORYX_PORTFOLIO_F2`,
`ORYX_PORTFOLIO_KEYS`) : `GET/POST /api/v2/me/portfolio`,
`GET/PUT/DELETE /api/v2/me/portfolio/1` → 404, aucun cookie, aucun module
`core.auth_f2*` / `core.authz_f2*` chargé, aucun accès réseau, routes
publiques exactement celles de `main` ; `api.py` ne mentionne aucun
élément PR 7 ; importer les modules PR 7 n'ouvre aucune connexion ;
`requirements.txt` inchangé ; tête Alembic `0020_position_revision`.

## 20. Blocages de l'activation publique

1. Routes `/api/user/{user_id}/*` (dont l'écriture du portefeuille sans
   révision) toujours actives et fondées sur un `user_id` client.
2. Aucun chargeur de production ni procédure opérée pour le trousseau
   portefeuille (création, distribution, rotation, conservation des
   anciennes versions tant que des reçus existent).
3. Aucune migration des identités navigateur ni du `localStorage`.
4. Frontend non adapté (aucun appel aux routes F2).
5. Thèses, Décrypter, ACK (PR 8, PR 9) non migrés.
6. Activation, montage sur `api:app` et validation Railway : décisions du
   fondateur, hors de cette PR.
