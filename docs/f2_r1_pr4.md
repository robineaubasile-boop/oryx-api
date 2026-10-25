# R1 PR 4 — invitation, activation, reprise et liaison e-mail F2 (dormantes)

Référence normative : **C.4.5 consolidée contrôlée, addendum C.4.5-A
intégré (section 10)**, et rapport de contrôle de fermeture documentaire
du 24/10/2026. Référence de compatibilité : `main` au SHA
`6cc8ca0320d094f424c5f6d3afddbcfafa6a5bbb` (PR 3 fusionnée), identique au
`main` au moment de l'implémentation.

**Statut du texte.** L'en-tête et le §9 du fichier
`Oryx_R1_PR4_C4_5_Consolidee_Controlee.md` portent encore « candidate au
freeze — NON FIGÉE » ; le propriétaire du projet a confirmé explicitement
le freeze et demandé l'implémentation (validation exigée par ce §9). Les
documents ne sont pas versionnés dans le dépôt.

## 1. Périmètre livré

| Élément | Fichier |
|---|---|
| Migration additive `0019_f2_http_admission` | `alembic/versions/0019_f2_http_admission.py`, `core/models.py` |
| Paramètres PR 4 (budgets, quotas, version de politique) | `core/auth_f2/policy.py` |
| Auxiliaires anti-abus (version explicite, `http_action`, comptage par opération) | `core/auth_f2/abuse.py` |
| Admission HTTP durable et autonome | `core/auth_f2/http_admission.py` (nouveau) |
| Quotas métier, reprise typée, consultation du résultat | `core/auth_f2/service.py` |
| Matrice fermée des sept routes | `core/auth_f2_http/contract_pr4.py` (nouveau) |
| Sept routes, rendu PR 4, `/sign-in/resume` typée | `core/auth_f2_http/routes.py` |
| Composition isolée (admission injectée) | `core/auth_f2_http/isolated.py` |

Hors périmètre, non livré : routes administratives d'invitation, interface,
fusion de profils, connexion par identité non vérifiée, `user_id` fourni
par le client, nouvelle méthode de récupération, activation publique.

## 2. Dormance

`api.py` n'est pas modifié : aucune route F2, aucun import de
`core.auth_f2*` (tests de dormance PR 2 / PR 3 inchangés et verts, y
compris l'import du vrai `api.py` dans un sous-processus sous plusieurs
environnements, dont des drapeaux d'activation). Les sept routes
n'existent que dans l'application isolée construite par les tests
(`build_isolated_f2_app`). Aucune variable d'environnement n'est lue,
aucun appel fournisseur n'a lieu dans les tests (doubles Supabase / Resend
sans réseau), aucune initialisation implicite de `auth_abuse_key_state`.

## 3. Migration 0019 (C.4.5 §2.1)

`auth_abuse_events` uniquement (aucune table, aucun index) :

- colonne nullable `http_action VARCHAR` (ajout sans réécriture) ;
- `ck_auth_abuse_events_event_kind` : liste de 0018 **+**
  `auth_http_probe_attempt` ;
- `ck_auth_abuse_events_otp_link` : clause « exigent `otp_request_id` »
  inchangée ; `auth_http_probe_attempt` ajouté aux types qui l'**interdisent**
  (un probe lié à une demande OTP est refusé, même sans le CHECK suivant —
  prouvé en le retirant dans une transaction de test) ;
- nouveau `ck_auth_abuse_events_http_probe` : `http_action` renseignée si
  et seulement si l'événement est un probe ; liste fermée des sept actions ;
  un probe a `auth_operation_id`, `otp_request_id`, `blocked_until` NULL et
  `policy_version = 'f2-pr4-http-v1'`.

Conservés tels quels : `ck_auth_abuse_events_resume_link`, `…_block`,
`…_refusal_bucket`, les FK, `uq_auth_abuse_events_refusal_bucket` et les
index de 0017, `auth_abuse_key_state`. Fichiers 0015–0018 inchangés
(sha256 vérifiés par test). Toute ligne valide sous 0018 reste valide
(une ligne de chaque type historique traverse l'upgrade, CHECK revalidés).

**Downgrade** : `LOCK TABLE auth_abuse_events IN ACCESS EXCLUSIVE MODE`
**avant** le contrôle ; refus (exception, aucune suppression) s'il existe
un probe ou une `http_action` ; sinon retrait du CHECK, de la colonne, et
restauration **textuelle exacte** des deux CHECK de 0018 (catalogue
PostgreSQL de 0018 comparé à l'identique après downgrade). Même garde en
SQL offline (bloc `DO`).

## 4. Admission HTTP (C.4.5 §2.2, §2.3)

`HttpAdmission.admit(action, client_ip)` :

1. sujets IP manquants créés dans une transaction courte committée
   (`abuse.ensure_subjects`) ;
2. transaction d'admission : `pg_advisory_xact_lock_shared` de l'état de
   clé → sujets IP `FOR UPDATE` (instruction unique triée, toutes versions
   de K_abuse comptées) → `clock_timestamp()` → budget commun (tous les
   probes de l'IP) → budget propre (filtre `http_action`) → un probe ;
3. COMMIT ; `True` n'est rendu qu'après un COMMIT confirmé.

| Portée | Budget |
|---|---|
| sept actions, même IP | 60 / 5 min |
| `invitation_begin` | 20 / 1 h |
| `invitation_complete`, `invitation_resume`, `email_link_result` | 30 / 5 min |
| `email_link_start`, `email_link_resend`, `email_link_verify` | budget commun seul, + quotas OTP de PR 2 |

Refus de budget : `False`, aucune ligne écrite, route 429 `rate_limited`
sans moteur. Toute erreur (état de clé absent ou incompatible, délai de
verrou, échec d'insertion, COMMIT en échec ou incertain) : exception, route
503 `temporarily_unavailable`, moteur **jamais** appelé ; un probe
éventuellement committé reste compté, sans annulation rétroactive. Aucun
verrou n'est conservé : la transaction métier est distincte et ouverte
après ce COMMIT. L'admission s'applique aussi aux invitations et
opérations inconnues et précède les sorties anticipées du moteur OTP.

## 5. Quotas métier (C.4.5 §2.4) et ordre des verrous (§3)

**Ouvertures d'invitation** (`begin_invitation_activation`) : sous
`invitations FOR UPDATE`, heure lue après le verrou, comptage des
opérations `invitation_activation` **conservées** (table `auth_operations`,
jamais purgée ; tout statut) : ≥ 3 dans `]t − 1 h, t]` ou ≥ 12 au total →
`invitation_unavailable` (même réponse publique qu'une invitation
inexistante, expirée, révoquée ou consommée).

**Reprise d'activation** (`resume_result(expected_kind='invitation_activation',
client_ip=…)`) et **consultation du résultat de liaison**
(`read_email_linking_result`) : 10 admissions par opération sur 5 min,
événements `resume_attempt` (FK vers l'opération, sujet IP courant,
`policy_version = 'f2-pr4-http-v1'`), comptés **par `auth_operation_id`**,
toutes IP et toutes versions de clé confondues.

Ordre des verrous (aucune remontée de rang, donc aucun cycle avec l'ordre
global de PR 2 : état de clé → sujets → invitation → users → opération →
candidat → demande OTP → tentative → session → génération → identité) :

| Parcours | Ordre |
|---|---|
| admission HTTP | état de clé (partagé) → sujets IP |
| begin | invitation `FOR UPDATE` → (insertions opération, candidat) |
| complete (inchangé) | invitation → opération → candidat |
| reprise d'activation | état de clé → sujets IP → opération `FOR UPDATE` (pris d'emblée, aucune conversion depuis `FOR SHARE`) → candidat → session → génération → heure |
| résultat de liaison, phase 2 | état de clé → sujets IP → opération `FOR UPDATE` → session → génération → heure |

Le justificatif (secret candidat, ou session réauthentifiée) et toutes les
conditions d'éligibilité sont validés **avant** le débit : un UUID isolé,
un faux secret, une opération inconnue, étrangère ou d'un autre type
n'écrivent aucun `resume_attempt`. Deux admissions concurrentes de la même
opération se sérialisent sur l'opération (T1, §8).

## 6. Moteur

- `resume_result(…, expected_kind=None, client_ip=None)` : `None` =
  entrée interne historique (inchangée, sans écriture) ;
  `supabase_email_sign_in` = route `/sign-in/resume` (seul changement PR 3
  intentionnel) ; `invitation_activation` = route `/invitation/resume`,
  avec quota ; toute autre valeur lève `ValueError`.
- `read_email_linking_result(operation_id, session, client_ip)` : n'appelle
  ni `authenticate_session` ni le helper HTTP `authenticated` (aucune
  confirmation de livraison, vérifiée par test). Phase 1 : transaction en
  lecture seule, sans verrou, jugeant la seule session désignée
  (`session_invalid` quelle que soit l'opération). Phase 2 : transaction
  distincte, verrous ci-dessus, session **réauthentifiée** (révocation
  entre les phases, expiration pendant l'attente du verrou → 401), puis
  éligibilité stricte : opération `email_linking` committée ;
  `session.id == initiating_session_id` ; `session.user_id ==
  target_user_id` ; méthode `invitation_activation` ; résultat
  `f2-c2-result-v1` / `{"outcome": "email_linked"}` sans session ni
  candidat ; identité liée (profil, `provider/issuer/subject` prouvés)
  et demande OTP `verified` conformes au résultat ; `at < resume_until`.
  Sinon `result_unavailable`. Puis quota, puis `email_linked`. Aucune
  session, identité, opération ni OTP créés.
- start / resend / verify de liaison, complete : logique PR 2 réutilisée
  sans modification.

## 7. Contrat HTTP (C.4.5 §4, §5, §10)

| POST `/api/auth/f2…` | Clés exactes | Justificatif | Action d'admission | Moteur |
|---|---|---|---|---|
| `/invitation/begin` | `token` | — (capacité 409) | `invitation_begin` | `begin_invitation_activation` |
| `/invitation/complete` | `operation_id`, `candidate_id` | cookie candidat exact | `invitation_complete` | `complete_invitation_activation` |
| `/invitation/resume` | `operation_id`, `candidate_id` | cookie candidat exact | `invitation_resume` | `resume_result(expected_kind=invitation_activation)` |
| `/email-link/start` | `generation_id`, `email` (≤ 320) | cookie actif exact | `email_link_start` | `start_email_linking` |
| `/email-link/resend` | `generation_id`, `operation_id` | cookie actif exact | `email_link_resend` | `resend_email_linking_otp` |
| `/email-link/verify` | `generation_id`, `operation_id`, `code` | cookie actif exact | `email_link_verify` | `verify_email_linking` |
| `/email-link/result` | `generation_id`, `operation_id` | cookie actif exact | `email_link_result` | `read_email_linking_result` |

Ordre : provenance → Cookie strict → CSRF → Content-Type et corps →
champs dans l'ordre du tableau → cookie exact → capacité (begin) → IP
fiable (les sept routes) → admission → moteur. Chaque refus de transport
(403 `origin_refused`, 400 `invalid_request`, 403 `csrf_invalid`, 415
`invalid_request`, 413 `request_too_large`, 401 `credential_missing` /
`session_invalid`, 409 `credential_limit`, 503 `temporarily_unavailable`)
produit `{"error":"<code>"}` sans `Set-Cookie`, sans probe, sans
`resume_attempt`, sans appel moteur (prouvé pour chaque route).

Matrice `core/auth_f2_http/contract_pr4.py`, testée égale à une copie
indépendante des tables §5.2–§5.8 ; distincte de la matrice PR 3, qui est
inchangée (test d'égalité conservé). Rendus :

- A (begin) 202 `{"status":"activation_started","operation_id","candidate_id"}` + candidat ;
- B (complete) 200 `{"status":"invitation_activated","operation_id","generation_id","expires_at"}` + actif ;
- C (resume) 200 `{"status":"resumed", …}` + même actif ;
- D 202 `{"status":"code_sent","operation_id"}` ; E 200 `{"status":"email_linked","operation_id"}` ;
- F 503 `{"error":"delivery_uncertain"|"delivery_failed","operation_id"}` (sinon 503 générique) ;
- G `{"error":"<code>"}`.

Triplet absent pour l'endpoint (y compris un triplet d'un autre endpoint
ou de PR 3) et toute incohérence (candidat ou session inattendus, autre
opération, `IssuedSession` incomplète ou mal typée, drapeau `resumed`
contraire, secret non canonique, échéance naïve ou passée) : 503 sans
cookie. **Aucune réponse PR 4 ne porte `expires_in`** ; `expires_at` =
`session.absolute_expires_at` en UTC, sérialisé
`AAAA-MM-JJTHH:MM:SS.ffffffZ` (microsecondes toujours présentes : choix de
sérialisation déterministe, le texte normatif n'imposant que « UTC
déterministe »). Les réponses PR 3 sont inchangées (`expires_in` = Max-Age).

Cookies (§10.3) : noms `__Host-oryx-candidate-<candidate_id>` /
`__Host-oryx-active-<generation_id>`, `Max-Age; Path=/; Secure; HttpOnly;
SameSite=Lax`, sans `Domain` ni `Expires`. Candidat `floor(900 − Δmono)` ;
actif créé `floor(min(1296000 − Δmono, absolute_expires_at − heure
murale))` ; actif repris `floor(min(1295700 − Δmono, …))` ; < 1 s → 503
sans cookie. Le candidat n'est ni renouvelé ni effacé après complete /
resume ; la liaison e-mail ne pose aucun cookie ; seul begin réserve
`count + 1 + candidats + 1 ≤ 16`. Les 503 historiques de PR 3 avec
candidat sont inchangés.

## 8. Tests et preuves

| Fichier | Contenu |
|---|---|
| `tests/test_migration_0019_f2_http_admission.py` | chaîne, sha256 0015–0018, sur-ensembles stricts, SQL offline exact, modèles, upgrade avec toutes les lignes historiques, downgrade exact (catalogue), refus de downgrade (en ligne et offline) pour chaque action, forme fermée du probe, `http_action` interdite aux types historiques, règles de 0018 conservées, parité DDL |
| `tests/test_f2_pr4_contract.py` | sans PostgreSQL : matrice = table normative, chaque entrée par la route (statut, JSON, cookies, en-têtes), triplets inconnus et d'autres endpoints, formes incohérentes, admission refusée / en panne / absente, refus de transport par route, ordre des défauts, durées et conservation des cookies, journaux sans secret |
| `tests/test_f2_pr4_admission.py` | PostgreSQL : N / N+1 commun et par action, fenêtres à la microseconde, IP indépendantes, IPv6 par /64, IPv4 mappée, rotation sans remise à zéro, inconnues comptées, sorties anticipées OTP, refus métier après admission, T3 (panne après COMMIT), T4 (échec avant COMMIT, COMMIT incertain, délai de verrou, clé indisponible) |
| `tests/test_f2_pr4_flows.py` | PostgreSQL : parcours complets, consommation unique, complétions concurrentes, ouvertures 3/4 et 12/13 (séquentielles et concurrentes), frontière horaire, réponse unique d'invitation indisponible, reprise 10/11 multi-IP, aucun débit sans justificatif, reprise typée, liaison sans session ni cookie ni fusion, T2 / T5 (réponses identiques), révocation entre phases, expiration pendant l'attente du verrou, quota de résultat 10/11 |
| `tests/test_f2_pr4_concurrency.py` | **T1** (§10.4) pour `/invitation/resume` et `/email-link/result`, 2 et 4 processus |

**T1** : neuf `resume_attempt` durables produits par de vraies requêtes
depuis trois IP ; processus `spawn` avec chacun son application et son
moteur NullPool ; IP `198.51.100.1` et `198.51.100.2` (normalisées
`ipv4:198.51.100.1` ≠ `ipv4:198.51.100.2`, vérifié) ; PID PostgreSQL de
chaque processus relevés et disjoints entre eux et de la connexion qui
tient l'opération `FOR UPDATE` ; toutes les requêtes observées
simultanément bloquées (une par IP sur le verrou d'opération, les autres
sur leur sujet IP) ; puis exactement un 200, les autres 429
`rate_limited` sans cookie ; dix lignes lues sur une connexion
d'observation indépendante ; une nouvelle demande valide laisse dix. Le
rapport (IP, PID, attentes, 9 → 10) est attaché au JUnit
(`record_testsuite_property`). Contre-épreuves exécutées localement (non
committées) : un comptage par IP ou un verrou `FOR SHARE` font échouer T1.

## 9. Écarts, choix et risques résiduels

- `resume_attempt` porte `policy_version = 'f2-pr4-http-v1'` (aucun
  `resume_attempt` n'était écrit avant PR 4) ; pas de CHECK sur cette
  version, la migration se limitant au §2.1.
- Le dépassement des plafonds d'invitation est journalisé
  (`f2 invitation openings exhausted`, sans jeton) mais rendu comme
  `invitation_unavailable`.
- Une même IP sérialise ses admissions sur son sujet (verrou court) ; la
  consultation d'un résultat verrouille brièvement l'opération de liaison
  `FOR UPDATE`.
- Prérequis d'activation hérités de PR 3, toujours ouverts : chaîne de
  proxys Railway et résolveur d'IP de confiance, chargement des clés,
  initialisation de `auth_abuse_key_state`, planification de la purge.
