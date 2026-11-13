# ACT-L1 / F04-B V0.3 — Matrice de traçabilité : freeze → implémentation → tests

## 1. Statut

| Élément | Valeur |
|---|---|
| Texte normatif | `docs/act_l1_f04b_freeze_v03.md` — transcription littérale du contrat F04-B V0.3 transmis par le fondateur (validé le 10/11/2026 selon le fondateur) |
| Nature du présent document | **Matrice d'audit**, non normative. Elle cite les sections du freeze (`§ n`) et ne les reformule que pour les nommer ; **le freeze prévaut** |
| Branche / PR | `claude/eloquent-tesla-is1a0u` / robineaubasile-boop/oryx-api#240, base `5ccc9e2bc122a44f1a2bfd810b28257bb6e31b31` |
| Version précédente | La version antérieure de ce document était une reconstruction faite sans le texte du freeze ; elle est remplacée par cette comparaison avec le texte transmis |

Légende : **PG** = test exécuté sur base PostgreSQL jetable (jobs `ci / postgres-16` et `ci / postgres-18`) ;
**PROD** = exécuté dans l'environnement installé depuis `requirements.txt` seul
(`tests/test_f04b_production_install.py`, journal dans l'artefact CI `reports-postgres-NN`,
dossier `f04b-production-install/`). État : *conforme*, *conforme (corrigé)*, *écart à arbitrer*, *hors F04-B*.

## 2. § 1 et § 8 — Objet, frontière, remise à F04-C

| Exigence du freeze | Implémentation | Preuve | État |
|---|---|---|---|
| § 1 : chargeur strict, gestionnaire de ressources, précontrôles en lecture seule ; aucune application ASGI, route, serveur, point d'entrée | `core/f2_runtime/{config,resources,preflight}.py` ; aucune composition | `test_f04b_dormancy.py::test_no_application_module_references_f04b`, `::test_frozen_files_are_unchanged` | conforme |
| § 8 A : charger la configuration, opération pure et déterministe | `config.py::load_f2_config` | `test_f04b_config.py::test_loading_is_deterministic_and_reads_only_the_given_mapping` | conforme |
| § 8 B : construire les ressources avec registre de propriété et nettoyage partiel | `resources.py::build_f2_resources`, `ResourceOwner` | § 6 ci-dessous | conforme |
| § 8 C : précontrôle en lecture seule, réussite ou échec fermé | `preflight.py::run_f2_preflight` | § 7 ci-dessous | conforme |

## 3. § 2 — B01 Configuration et trousseaux

| Exigence du freeze | Implémentation | Preuve | État |
|---|---|---|---|
| § 2.1 : dictionnaire de configuration reçu explicitement | `load_f2_config(variables)` (tout `Mapping`, `dict` compris) | `test_f04b_config.py::test_non_mapping_environment_is_refused`, `::test_loading_is_deterministic_and_reads_only_the_given_mapping` | conforme |
| § 2.1 : quinze variables, aucun fournisseur facultatif | `config.py::VARIABLES` | `test_f04b_config.py::test_the_contract_lists_exactly_fifteen_variables`, `::test_each_missing_variable_is_refused`, `::test_empty_environment_reports_every_missing_variable` ; `test_f04b_dormancy.py::test_the_f2_loader_still_refuses_an_incomplete_configuration_when_called` | conforme |
| § 2.1 : aucun secret généré, par défaut ou historique | aucun générateur ni variable legacy lus | `test_f04b_config.py::test_no_secret_is_generated_and_nothing_defaults`, `::test_legacy_only_environment_is_refused_without_fallback` | conforme |
| § 2.1 : liste `ORYX_F2_*` fermée ; autres variables non interprétées | `F2_PREFIX`, `UNKNOWN_VARIABLE` | `test_f04b_config.py::test_foreign_variables_are_ignored_but_unknown_f2_variables_are_refused` | conforme |
| § 2.2 : format Auth F2 `{schema, keys{usage: {version: valeur}}, current{usage: version}}` | `config.py::_auth_keyring` | `test_f04b_config.py::test_contract_auth_document_has_exactly_the_frozen_structure` (exemple du freeze recopié), `::test_auth_document_has_exact_properties_and_six_purposes`, `::test_the_former_purposes_format_is_refused` | **conforme (corrigé)** — écart E-1 |
| § 2.2 : CSRF, Portefeuille, Décryptage `{schema, keys{version: valeur}, current}` et identifiants exacts | `config.py::_single_keyring`, `*_SCHEMA` | `test_f04b_config.py::test_single_keyring_documents_have_exact_properties`, `::test_complete_configuration_is_loaded_with_existing_classes` | conforme |
| § 2.2 : 32 octets, base64url canonique sans remplissage | `core.auth_f2.crypto.b64url_decode_secret` | `test_f04b_config.py::test_non_canonical_or_wrong_length_keys_are_refused`, `::test_non_canonical_auth_key_is_refused_with_its_purpose` | conforme |
| § 2.2 : syntaxe de version de chaque classe, différences conservées | classes existantes instanciées telles quelles | `test_f04b_config.py::test_version_syntax_is_the_one_of_each_existing_class` | conforme |
| § 2.2 : refus obligatoires (document absent, clé absente, JSON invalide, doublons, propriété inconnue, mauvais type, mauvaise longueur, non canonique, version courante manquante) | `_document`, `_pairs`, `_exact`, `_versions` | `test_f04b_config.py::test_each_missing_variable_is_refused`, `::test_malformed_documents_are_refused`, `::test_duplicate_properties_are_refused_at_every_level`, `::test_oversized_documents_are_refused`, `::test_single_keyring_documents_have_exact_properties`, `::test_auth_document_has_exact_properties_and_six_purposes`, `::test_non_canonical_or_wrong_length_keys_are_refused` | conforme |
| § 2.2 : réutilisation d'une clé entre usages ou familles ; comparaison transversale aux quatre trousseaux | `_no_reuse` + contrôle inter-familles | `test_f04b_config.py::test_a_key_reused_across_versions_of_one_keyring_is_refused`, `::test_a_key_reused_across_auth_purposes_or_versions_is_refused`, `::test_an_auth_key_reused_by_another_family_is_refused`, `::test_a_key_shared_between_two_dedicated_families_is_refused` | conforme |
| § 2.2 : même `Keyring` et même `CsrfKeyring` pour les deux compositions | un objet de chaque dans `F2Config` | `test_f04b_resources.py::test_both_isolated_compositions_accept_the_same_keyrings_and_engine` | conforme |
| § 2.2 : historique des rotations hors trousseau ; procédure distincte avant activation | versions historiques admises, aucune rotation | `test_f04b_config.py::test_historical_versions_are_kept_next_to_the_current_one` | conforme (procédure : hors F04-B) |
| § 2.2 : version d'enregistrement anti-abus déterminée par PostgreSQL, non par `current["abuse"]` | `preflight.py` lit l'état par `load_key_state` ; F04-B n'écrit pas l'état | **PG** `test_f04b_preflight.py::test_keyring_incompatible_with_the_recorded_state_fails_closed`, `::test_recorded_previous_version_must_also_be_known` | conforme |
| § 2.3 : déterministe, sans connexion ni réseau ; aucune lecture de `os.environ` à l'import | aucun `os`, aucun effet à l'import | `test_f04b_database.py::test_importing_f04b_has_no_external_effect_and_builds_no_engine`, `::test_f04b_sources_never_reference_the_legacy_engine_nor_the_environment` | conforme |
| § 2.3 : objets typés et immuables | `F2Config` gelée, trousseaux en `MappingProxyType` | `test_f04b_config.py::test_loaded_configuration_is_immutable`, `::test_complete_configuration_is_loaded_with_existing_classes` | conforme (preuve ajoutée) |
| § 2.3 : pas d'import du moteur global legacy | aucun import `core.db` / `core.models` | `test_f04b_database.py::test_importing_f04b_has_no_external_effect_and_builds_no_engine` | conforme |
| § 2.3 : jamais de clé, jeton ou URL PostgreSQL avec identifiants journalisés | aucune journalisation ; erreurs par codes ; `repr` masqués | `test_f04b_config.py::test_errors_never_chain_decoder_messages`, `::test_several_problems_are_reported_together_without_values`, `::test_configuration_reveals_nothing_and_is_not_serializable` | conforme |
| § 2.3 : refus avant toute construction de ressources | `build_f2_resources` n'accepte qu'une `F2Config` validée | `test_f04b_resources.py::test_build_refuses_wrong_inputs` | conforme |

## 4. § 3 — B02 Dépendances de production

| Exigence du freeze | Implémentation | Preuve | État |
|---|---|---|---|
| § 3.1 : Python 3.11.9 ; `runtime.txt` inchangé | inchangé | `test_f04b_dependencies.py::test_runtime_is_unchanged` ; PROD (vérifie la version) | conforme (journaux Railpack non consultables depuis le dépôt) |
| § 3.1 : versions candidates FastAPI à Alembic | `requirements.in` | `test_f04b_dependencies.py::test_direct_production_dependencies_are_explicit_and_pinned`, `::test_the_test_environment_runs_the_locked_versions` | conforme |
| § 3.1 : autres dépendances directes existantes qualifiées ; transitives verrouillées de manière reproductible | `python-dotenv`, `yfinance`, `pydantic` épinglés aux versions CI ; verrou complet avec empreintes | `test_f04b_dependencies.py::test_requirements_txt_is_the_complete_hashed_lock_of_requirements_in`, `::test_production_lock_is_exactly_the_ci_qualified_subset` | conforme |
| § 3.2 : `requirements.txt` reste le point d'entrée Railway | `requirements.txt` = verrou complet | idem | conforme |
| § 3.2 : « une solution autorisée » : verrou de production distinct **référencé** depuis `requirements.txt` | variante retenue : `requirements.txt` **est** le verrou ; directes dans `requirements.in` | idem | **écart de forme à arbitrer** — E-2 |
| § 3.2 : le verrou CI peut conserver ses dépendances de test | `ci/requirements-ci.txt` inchangé | `test_f04b_dependencies.py::test_production_lock_is_exactly_the_ci_qualified_subset` | conforme |
| § 3.2 : démonstration par installation propre Python 3.11.9 | venv vierge, `pip install -r requirements.txt` seul, `pip check`, versions exactes | PROD `test_f04b_production_install.py::test_a_clean_environment_installs_and_runs_from_requirements_txt_alone` | conforme |
| § 3.2 : imports, **appels simulés des adaptateurs** et tests legacy/worker | sonde PROD : imports, construction dormante, appels simulés Supabase / Resend / EODHD / FMP / Claude ; tests legacy et worker dans les suites CI (mêmes versions) | PROD (`simulated_calls`) ; suites `ci / postgres-16` et `ci / postgres-18` (dont `test_r1d1_runtime.py`, `test_observation_service.py`) | conforme (appels simulés ajoutés à la preuve PROD) |
| § 3.2 : aucun alias `httpx` / `httpx2`, aucune suppression de dépendance legacy, `runtime.txt` inchangé | — | `test_f04b_dependencies.py::test_no_global_httpx_alias_anywhere`, `::test_runtime_is_unchanged` ; dépendances legacy toutes conservées (`PRODUCTION_LOCK`) | conforme |
| § 3.2 : versions candidates, non déclarées vérifiées en production | `docs/act_l1_f04b_resources_v03.md` § 9 | — | conforme |

## 5. § 4 et § 5 — B03 Moteur PostgreSQL, B04 Pool

| Exigence du freeze | Implémentation | Preuve | État |
|---|---|---|---|
| § 4 : un seul moteur synchrone F2, explicite, partagé Auth / Personnel | `database.py::create_f2_engine` ; un `engine` dans `F2Resources` | `test_f04b_resources.py::test_both_isolated_compositions_accept_the_same_keyrings_and_engine` | conforme |
| § 4 : la fabrique n'importe ni `core.db.engine` ni `core.db.SessionLocal` | — | `test_f04b_database.py::test_f04b_sources_never_reference_the_legacy_engine_nor_the_environment` ; `test_f04b_preflight.py::test_computing_the_head_loads_no_legacy_module` | conforme |
| § 4 : `postgres://` / `postgresql://` → `postgresql+psycopg2://` ; tout autre pilote refusé | `normalize_f2_database_url` | `test_f04b_database.py::test_normalization_matches_the_legacy_formats_it_accepts`, `::test_database_url_outside_the_contract_is_refused_without_echo` ; `test_f04b_config.py::test_database_url_formats_are_normalized_to_psycopg2`, `::test_unsupported_or_incomplete_database_urls_are_refused` | conforme |
| § 4 : la normalisation n'altère pas les mots de passe encodés | décodage SQLAlchemy, valeur transmise au pilote | `test_f04b_database.py::test_normalization_keeps_encoded_passwords_exactly` ; **PG** `::test_an_encoded_password_really_authenticates` | conforme (preuve ajoutée) |
| § 4 : `READ COMMITTED` explicite | `ISOLATION_LEVEL` | **PG** `test_f04b_database.py::test_isolation_is_read_committed_even_if_the_session_default_differs` | conforme |
| § 4 : construction à l'appel uniquement ; connexion réservée au précontrôle ou à l'usage | aucune connexion à la construction | `test_f04b_database.py::test_engine_is_built_only_on_request_with_the_frozen_pool` ; `test_f04b_resources.py::test_resources_are_built_in_order_and_owned` | conforme |
| § 4 : moteur possédé par le gestionnaire F04-B | `owner.own("engine", …, engine.dispose)` | `test_f04b_resources.py::test_close_runs_each_closer_once_in_reverse_order_and_is_idempotent` | conforme |
| § 5 : 5 / 2 / 5 s / pre-ping / 1 800 s / READ COMMITTED | `POOL_PARAMETERS` | `test_f04b_database.py::test_frozen_pool_parameters` | conforme |
| § 5 : plafond de sept connexions ; pool borné ; attente limitée | `QueuePool` | **PG** `test_f04b_database.py::test_pool_is_capped_at_seven_and_waits_at_most_five_seconds`, `::test_saturation_under_concurrency_never_exceeds_the_cap` | conforme |
| § 5 : aucun retry transactionnel | — | **PG** idem ; `test_f04b_database.py::test_unreachable_server_fails_once_without_retry` | conforme |
| § 5 : fermeture possible après restitution | `dispose` | **PG** `test_f04b_database.py::test_pool_is_capped_at_seven_and_waits_at_most_five_seconds`, `::test_connections_are_returned_after_errors_and_rollbacks` | conforme |
| § 5 : absence de second moteur F2 implicite | — | `test_f04b_database.py::test_importing_f04b_has_no_external_effect_and_builds_no_engine` ; `test_f04b_dormancy.py::test_a_complete_f2_configuration_builds_no_f2_engine_or_composition` | conforme |
| § 5 : capacité Railway et chevauchements à qualifier avant activation | — | `docs/act_l1_f04b_resources_v03.md` § 9 | hors F04-B |

## 6. § 6 — B05 Ressources et transports fournisseurs

| Exigence du freeze | Implémentation | Preuve | État |
|---|---|---|---|
| § 6.1 : F04-B possède les ressources créées ; mode de fermeture identifiable | `ResourceOwner` ; EODHD / FMP : session fermée à chaque appel | `test_f04b_resources.py::test_resources_are_built_in_order_and_owned`, `::test_owned_clients_are_really_closed_when_nothing_is_injected` | conforme |
| § 6.1 : nettoyage des constructions partielles en ordre inverse ; un échec de fermeture n'empêche pas les autres | `build_f2_resources`, `ResourceOwner.close` | `test_f04b_resources.py::test_a_partial_build_closes_what_was_built_in_reverse_order`, `::test_a_partial_build_survives_a_failing_cleanup`, `::test_close_continues_after_a_failing_closer_and_reports_it` | conforme |
| § 6.1 : deuxième fermeture sans effet destructeur | idempotence | `test_f04b_resources.py::test_close_runs_each_closer_once_in_reverse_order_and_is_idempotent`, `::test_close_is_single_even_under_concurrent_calls` | conforme |
| § 6.1 : ressource injectée jamais fermée ; propriété des transports injectés testée | `borrow`, `_owns_transport`, `_owns_client` | `test_f04b_resources.py::test_injected_resources_are_never_closed`, `::test_transport_errors_are_still_uncertain_and_closing_is_explicit` | conforme |
| § 6.1 : aucune ressource réputée libre sur expiration d'un délai | aucun délai dans `close` | revue de `resources.py` (LIMITE) | conforme |
| § 6.2 : méthodes conservées ; extension minimale de fermeture ; TLS, `trust_env=False`, pas de redirection, délais, classification, pas de journalisation | `http_providers.py::close` | `test_f04b_resources.py::test_supabase_and_resend_keep_their_http_contract`, `::test_owned_httpx_clients_keep_tls_no_redirect_no_env_and_timeouts` ; suites `test_f2_normalize_providers.py`, `test_f2_http_flows.py` | conforme |
| § 6.2 : partage des clients HTTPX sous réserve des tests de concurrence et de fermeture | un client par adaptateur, partagé | `test_f04b_resources.py::test_shared_httpx_clients_serve_concurrent_calls_then_close` | conforme (preuve ajoutée) |
| § 6.3 : interfaces B2 et session injectée historique conservées | `session=` inchangé | `test_f04b_resources.py::test_historical_injected_session_is_unchanged` ; suite `test_authz_f2_pr9b2_units.py` | conforme |
| § 6.3 : fabrique injectable sélectionnée par F04-B ; session indépendante par appel, fermée y compris sur erreur ou annulation | `session_factory`, `_call` (`finally`) | `test_f04b_resources.py::test_each_concurrent_call_gets_its_own_session`, `::test_factory_mode_keeps_the_error_classification_and_always_closes`, `::test_factory_sessions_are_closed_on_cancellation`, `::test_built_market_transports_use_one_session_per_call`, `::test_default_resources_use_the_isolated_session_factory` | conforme (preuve d'annulation ajoutée) |
| § 6.3 : `session=` et `session_factory=` non combinables | `_session_source` | `test_f04b_resources.py::test_session_and_factory_together_are_refused` | conforme |
| § 6.3 : sessions F04-B `trust_env=False` | `isolated_session` | `test_f04b_resources.py::test_f04b_sessions_ignore_the_environment_and_never_retry` | conforme |
| § 6.3 : interdits (méthodes B2, retries, redirections, seuils, classification, lecture de configuration, session partagée en concurrence) | `get_json` inchangé | `test_f04b_resources.py::test_factory_mode_sends_exactly_the_historical_eodhd_request`, `::test_factory_mode_sends_exactly_the_historical_fmp_request`, `::test_no_request_is_sent_without_time_budget_and_the_session_is_closed`, `::test_each_concurrent_call_gets_its_own_session` ; suite `test_authz_f2_pr9b2_units.py` | conforme |
| § 6.4 : `ClaudeTransport`, `create_message`, `max_retries=0` ; fermeture du client SDK possédé ; règle de propriété du client injecté testée | `ClaudeTransport.close`, `_owns_client` | `test_f04b_resources.py::test_owned_claude_client_is_closed_once`, `::test_claude_transport_keeps_one_request_and_no_retry` ; suite `test_authz_f2_pr9b2_units.py` | conforme |
| § 6.5 : F04-B ne démontre pas qu'une fermeture est sûre pendant un fil C3 actif | LIMITE documentée | `docs/act_l1_f04b_resources_v03.md` § 6.3 | hors F04-B (F04-C / F04-E) |

## 7. § 7 — B06 Précontrôles

| Exigence du freeze | Implémentation | Preuve (**PG** sauf mention) | État |
|---|---|---|---|
| Opération explicite ; transaction effectivement en lecture seule | `run_f2_preflight` (`SET TRANSACTION READ ONLY`, `ROLLBACK`) | `test_f04b_preflight.py::test_nominal_preflight_reports_and_writes_nothing`, `::test_the_preflight_transaction_is_really_read_only` | conforme |
| 1. connexion au serveur prévu | base, utilisateur, majeure qualifiée | `test_f04b_preflight.py::test_a_server_that_is_not_the_configured_database_fails_closed`, `::test_an_unqualified_server_version_fails_closed`, `::test_an_unreachable_server_fails_closed` | conforme (voir E-3) |
| 2. isolation effective READ COMMITTED | `_checks` | `test_f04b_preflight.py::test_an_engine_without_read_committed_fails_closed` | conforme |
| 3. version Alembic compatible avec la tête du code ; référence versionnée, sans `alembic/env.py` ni migration | `expected_alembic_head` (`ScriptDirectory`) | `test_f04b_preflight.py::test_expected_head_is_the_single_head_of_the_code` (sans base), `::test_computing_the_head_loads_no_legacy_module` (sans base), `::test_an_older_alembic_version_fails_closed_without_upgrade`, `::test_an_unexpected_head_or_several_rows_fail_closed`, `::test_missing_or_empty_alembic_version_fails_closed` | conforme |
| 4. objets nécessaires à la composition F2 | `F2_REQUIRED_TABLES`, `F2_REQUIRED_COLUMNS` | `test_f04b_preflight.py::test_a_missing_f2_table_fails_closed`, `::test_a_missing_f2_column_fails_closed` | conforme |
| 5. `auth_abuse_key_state` présent et compatible via `load_key_state` | `load_key_state` | `test_f04b_preflight.py::test_missing_abuse_key_state_fails_closed_and_is_not_initialized`, `::test_keyring_incompatible_with_the_recorded_state_fails_closed`, `::test_recorded_previous_version_must_also_be_known` | conforme |
| 6. absence d'incohérence détectable | plusieurs lignes `alembic_version`, version précédente inconnue, base ou utilisateur inattendus refusés | tests ci-dessus | conforme |
| Interdits : upgrade, `initialize_abuse_key_state`, création de tables ou lignes, rotation ou purge, réparation, marqueurs anti-abus | aucun de ces appels ; instructions SELECT / SET seules | `test_f04b_preflight.py::test_the_preflight_never_migrates_initializes_or_repairs` (sans base), `::test_nominal_preflight_reports_and_writes_nothing` ; `test_f2_dormancy.py::test_f04b_reference_matrix_is_closed_and_exact` (sans base) | conforme |

## 8. § 9 à § 11 — Périmètre, GO / NO-GO, réserves

| Exigence du freeze | Preuve | État |
|---|---|---|
| § 9 autorisés : modules F04-B, tests, `requirements.txt` et verrous, extensions limitées des deux modules fournisseurs, ajustements nécessaires de tests existants, documentation | diff de la PR ; `test_f2_dormancy.py::test_f04b_reference_matrix_is_closed_and_exact` | conforme |
| § 9 interdits : `api.py`, `core/db.py`, runners, commandes, B1 / B2 / C3, refus et routes HTTP, gate F-02A, migration, Railway, activation | `test_f04b_dormancy.py::test_frozen_files_are_unchanged` (sha256) ; aucun fichier de `core/authz_f2/`, `core/authz_f2_http/`, `core/auth_f2_http/`, `core/route_policy/`, `alembic/` modifié ; `test_authz_f2_pr9b2_boundary.py::test_no_new_migration` | conforme |
| § 10 Dormance : aucun changement des points d'entrée publics | `test_f04b_dormancy.py::test_the_served_entrypoint_ignores_a_complete_f2_configuration`, `::test_the_real_legacy_server_answers_identically_with_a_complete_f2_configuration`, `::test_a_complete_f2_configuration_builds_no_f2_engine_or_composition` | conforme |
| § 10 Régression : suites PostgreSQL 16 / 18, migrations, web | jobs `ci / postgres-16`, `ci / postgres-18`, `ci / migrations-16`, `ci / migrations-18`, `ci / web`, `ci / required` | conforme (voir le compte rendu de la PR pour le run du commit courant) |
| § 10 Railway : API et worker opérationnels après merge | non vérifiable avant merge | à vérifier par le fondateur après merge |
| § 10 : secrets synthétiques, fournisseurs simulés, bases jetables | `tests/f04b_support.py`, `tests/pg_guard.py` | conforme |
| § 11 : P1, P2, qualification du pool, préparation des secrets et de l'état anti-abus, F04-C à F-03, décision d'activation | non traités | hors F04-B |

## 9. Écarts relevés par rapport au texte du freeze

| Id | Écart | Traitement |
|---|---|---|
| **E-1** | § 2.2 : `ORYX_F2_AUTH_KEYS_JSON` implémenté initialement avec une racine `purposes`, alors que le freeze fixe `schema` / `keys` / `current` à la racine | **corrigé** : seul le format du freeze est accepté ; test contractuel sur l'exemple du freeze et refus de l'ancien format |
| **E-2** | § 3.2 : le freeze cite comme « solution autorisée » un verrou distinct référencé depuis `requirements.txt` ; la PR fait de `requirements.txt` le verrou lui-même | **non modifié, à arbitrer** : exigences de fond tenues ; si le fondateur exige la forme citée, la conversion est mécanique (`requirements.txt` → `-r` vers un verrou distinct, sans changer une seule version) |
| **E-3** | § 7.1 « serveur prévu » : la PR exige en plus une majeure PostgreSQL qualifiée par la CI (16 ou 18) | **signalé** : interprétation plus stricte, en échec fermé ; à confirmer au regard de la version Railway |
| **E-4** | § 4 / § 5 : aucun `connect_timeout` ajouté (paramètre absent du freeze) ; l'établissement d'une connexion neuve n'est pas borné par F04-B | **signalé** : aucune valeur hors freeze introduite ; à traiter en F04-C / F04-E |
| **E-5** | § 2.2 : la règle du modèle Claude est recopiée dans `config.py` (le module C3 charge `core.db`, interdit par § 2.3) | **signalé** : équivalence testée (`test_model_rule_is_exactly_the_c3_rule`) |
