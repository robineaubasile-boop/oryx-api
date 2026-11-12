# ACT-L1 / F04-B V0.3 — Matrice de traçabilité exigences → implémentation → tests

## 1. Statut de ce document (à lire en premier)

| Élément | Valeur |
|---|---|
| Objet | Rendre vérifiable, sans reconstruction, la conformité de la PR #240 au freeze **F04-B V0.3** |
| Nature | **Reconstruction et synthèse**. Ce document **n'est pas** le texte du freeze et n'en est **pas une copie mot pour mot** |
| Texte intégral du freeze F04-B V0.3 | **Non versionné dans le dépôt**. Validé dans la conversation de conception ; s'il faut le reproduire exactement, **seul le fondateur peut le fournir** |
| Décisions | Aucune décision du freeze n'est ajoutée, retirée ni reformulée ici. En cas d'écart entre ce document et le freeze, **le freeze prévaut** |
| Branche / PR | `claude/eloquent-tesla-is1a0u` / robineaubasile-boop/oryx-api#240, base `5ccc9e2bc122a44f1a2bfd810b28257bb6e31b31` |

### 1.1 Sources utilisées, par ordre de force probante

| Code | Source | Disponibilité |
|---|---|---|
| **[A]** | `docs/act_l1_f04a_architecture_v03.md` (F04-A V0.3, figé et fusionné) : § 8 (configuration et ressources), § 9.1, § 13 « F04-B » | versionnée dans le dépôt |
| **[M]** | Demande d'implémentation F04-B transmise par le fondateur (11/11/2026) : décision de freeze, blocs B01 à B06, frontière, interdits, tests obligatoires, livrables | **non versionnée** (conversation) ; reprise ici par synthèse |
| **[G]** | Grille de vérification transmise avec la demande de clôture des réserves (12/11/2026), présentée par le fondateur comme une synthèse qui « ne remplace pas le texte intégral du freeze validé » | **non versionnée** ; reprise ici par synthèse |
| **[D]** | `docs/act_l1_f04b_resources_v03.md` (documentation d'implémentation de la PR) | versionnée dans la PR |

Une exigence marquée seulement **[M]** ou **[G]** n'est vérifiable dans GitHub qu'à travers ce document : c'est
la limite documentaire résiduelle (§ 9).

## 2. Lecture de la matrice

- **Implémentation** : fichier(s) et symbole(s) qui réalisent l'exigence.
- **Tests** : noms exacts des tests (fichier `tests/…`) qui l'exercent ; **PG** = exécuté sur base PostgreSQL
  jetable (jobs `ci / postgres-16` et `ci / postgres-18`), sinon sans base.
- **État** : *réalisé* (implémenté et testé), *documenté* (exigence de procédure ou de réserve, sans code),
  *ouvert* (hors F04-B, reporté par le freeze).

## 3. B01 — Configuration stricte

| # | Exigence (synthèse) | Src | Implémentation | Tests | État |
|---|---|---|---|---|---|
| B01-1 | Chargeur pur, déterministe, sans effet réseau ni lecture implicite de l'environnement à l'import | M, A § 9.1 | `core/f2_runtime/config.py::load_f2_config(variables)` (lit le seul mappage reçu) | `test_f04b_config.py::test_loading_is_deterministic_and_reads_only_the_given_mapping` ; `test_f04b_database.py::test_importing_f04b_has_no_external_effect_and_builds_no_engine`, `::test_f04b_sources_never_reference_the_legacy_engine_nor_the_environment` | réalisé |
| B01-2 | Les quinze variables contractuelles, toutes requises | M, G | `config.py::VARIABLES`, `_PARSERS` | `test_f04b_config.py::test_the_contract_lists_exactly_fifteen_variables`, `::test_empty_environment_reports_every_missing_variable`, `::test_each_missing_variable_is_refused` ; `test_f04b_dormancy.py::test_the_f2_loader_still_refuses_an_incomplete_configuration_when_called` | réalisé |
| B01-3 | Validation fermée limitée aux variables F2, sans rejeter les autres variables Railway | M | `config.py::F2_PREFIX`, `UNKNOWN_VARIABLE` | `test_f04b_config.py::test_foreign_variables_are_ignored_but_unknown_f2_variables_are_refused` | réalisé |
| B01-4 | Quatre documents JSON de trousseaux indépendants, schémas `oryx-f2-{auth,csrf,portfolio,decryptage}-keys-v1` | M, A § 8.2, G | `config.py::_auth_keyring`, `_single_keyring`, `*_SCHEMA` | `test_f04b_config.py::test_complete_configuration_is_loaded_with_existing_classes`, `::test_single_keyring_documents_have_exact_properties`, `::test_auth_document_has_exact_properties_and_six_purposes` | réalisé |
| B01-5 | Six usages Auth F2 (`candidate`, `derive`, `active`, `otp`, `invitation`, `abuse`), `keys` et `current` par usage | M, A § 8.2, G | `config.py::_auth_keyring` (réutilise `core.auth_f2.keys.PURPOSES`) | `test_f04b_config.py::test_auth_document_has_exact_properties_and_six_purposes` | réalisé |
| B01-6 | Clés base64url canonique sans remplissage, exactement 32 octets | M, G | `core.auth_f2.crypto.b64url_decode_secret` (réutilisé) | `test_f04b_config.py::test_non_canonical_or_wrong_length_keys_are_refused`, `::test_non_canonical_auth_key_is_refused_with_its_purpose` | réalisé |
| B01-7 | Refus : document malformé, champ inconnu, propriété JSON dupliquée, version absente | M, G | `config.py::_document`, `_pairs`, `_exact`, `_versions` | `test_f04b_config.py::test_malformed_documents_are_refused`, `::test_duplicate_properties_are_refused_at_every_level`, `::test_oversized_documents_are_refused`, `::test_single_keyring_documents_have_exact_properties` | réalisé |
| B01-8 | Syntaxe de version propre à chaque classe existante ; réutilisation des classes et de leurs validations ; versions historiques | M, G | `Keyring`, `CsrfKeyring`, `PortfolioKeyring`, `DecryptageKeyring` instanciés tels quels | `test_f04b_config.py::test_version_syntax_is_the_one_of_each_existing_class`, `::test_historical_versions_are_kept_next_to_the_current_one` | réalisé |
| B01-9 | Refus des réutilisations de clés entre usages, versions et familles | M, A § 8.2, G | `config.py::_no_reuse` et contrôle inter-familles de `load_f2_config` | `test_f04b_config.py::test_a_key_reused_across_versions_of_one_keyring_is_refused`, `::test_a_key_reused_across_auth_purposes_or_versions_is_refused`, `::test_an_auth_key_reused_by_another_family_is_refused`, `::test_a_key_shared_between_two_dedicated_families_is_refused` | réalisé |
| B01-10 | Même objet Auth F2 et même objet CSRF transmissibles aux deux compositions | M, A § 8.2 | `F2Config.keyring`, `F2Config.csrf_keyring` (un objet chacun) | `test_f04b_resources.py::test_both_isolated_compositions_accept_the_same_keyrings_and_engine` | réalisé |
| B01-11 | Aucun secret généré, aucune valeur par défaut, aucun repli legacy | M, A § 8.1, G | `load_f2_config` (aucun appel au générateur, aucune variable legacy lue) | `test_f04b_config.py::test_no_secret_is_generated_and_nothing_defaults`, `::test_legacy_only_environment_is_refused_without_fallback` | réalisé |
| B01-12 | Aucune donnée sensible dans les erreurs ou les journaux | M | `F2ConfigError(problems)` : codes seulement ; `F2Config.__repr__` | `test_f04b_config.py::test_errors_never_chain_decoder_messages`, `::test_several_problems_are_reported_together_without_values`, `::test_configuration_reveals_nothing_and_is_not_serializable` (et l'assertion `_assert_no_secret` de chaque refus) | réalisé |

## 4. B02 — Dépendances

| # | Exigence (synthèse) | Src | Implémentation | Tests / preuve | État |
|---|---|---|---|---|---|
| B02-1 | Python 3.11.9 ; `runtime.txt` non modifié | M, G | `runtime.txt` inchangé | `test_f04b_dependencies.py::test_runtime_is_unchanged` ; `test_f04b_dormancy.py::test_frozen_files_are_unchanged` | réalisé |
| B02-2 | `requirements.txt` reste le point d'entrée Railway ; dépendances directes explicites ; transitives verrouillées avec empreintes | M, A § 8.5, G | `requirements.in` (13 directes épinglées), `requirements.txt` (verrou complet, 47 distributions, sha256) | `test_f04b_dependencies.py::test_direct_production_dependencies_are_explicit_and_pinned`, `::test_requirements_txt_is_the_complete_hashed_lock_of_requirements_in` ; `test_f2_dormancy.py::test_production_requirements_are_unchanged` | réalisé |
| B02-3 | `httpx` et `httpx2` déclarés et compatibles ; aucun alias global | M, A § 8.5, G | `requirements.in` : `httpx==0.28.1`, `httpx2==2.13.1` | `test_f04b_dependencies.py::test_no_global_httpx_alias_anywhere` | réalisé |
| B02-4 | Versions candidates qualifiées (cohérence avec la CI) | M, A § 8.5 | même version et mêmes empreintes que `ci/requirements-ci.txt` | `test_f04b_dependencies.py::test_production_lock_is_exactly_the_ci_qualified_subset`, `::test_the_test_environment_runs_the_locked_versions` | réalisé |
| B02-5 | Installation propre depuis les **seules** dépendances de production sous Python 3.11.9, reproductible, journalisée | M, A § 13, G | procédure du test ci-contre (venv vierge, `pip install -r requirements.txt` uniquement, `pip check`, versions, imports, construction dormante) | `test_f04b_production_install.py::test_a_clean_environment_installs_and_runs_from_requirements_txt_alone` — journal publié dans l'artefact CI `reports-postgres-16` / `reports-postgres-18`, dossier `f04b-production-install/` (`install.log`, `summary.json`) | réalisé |
| B02-6 | Non-régression API historique et worker | M, G | aucun changement de `api.py` ni du worker | suites complètes `ci / postgres-16` et `ci / postgres-18` (dont `test_r1d1_runtime.py`, `test_observation_service.py`, `test_f2_dormancy.py::test_api_routes_and_hooks_are_exactly_those_of_main`) ; `test_f04b_dormancy.py::test_the_real_legacy_server_answers_identically_with_a_complete_f2_configuration` | réalisé |

## 5. B03 / B04 — PostgreSQL et pool

| # | Exigence (synthèse) | Src | Implémentation | Tests | État |
|---|---|---|---|---|---|
| B03-1 | Fabrique explicite d'un moteur SQLAlchemy synchrone F2 ; aucun moteur à l'import | M, A § 9.1, G | `core/f2_runtime/database.py::create_f2_engine` | `test_f04b_database.py::test_engine_is_built_only_on_request_with_the_frozen_pool`, `::test_importing_f04b_has_no_external_effect_and_builds_no_engine` | réalisé |
| B03-2 | Un seul moteur partagé par les compositions Auth et Personnel | M, A § 8.3, G | `resources.py::build_f2_resources` (un `engine`) | `test_f04b_resources.py::test_both_isolated_compositions_accept_the_same_keyrings_and_engine` | réalisé |
| B03-3 | Aucun import ni usage de `core.db.engine` / `core.db.SessionLocal` ; `core/db.py` inchangé | M, G | aucun import de `core.db` / `core.models` dans `core/f2_runtime` | `test_f04b_database.py::test_f04b_sources_never_reference_the_legacy_engine_nor_the_environment`, `::test_importing_f04b_has_no_external_effect_and_builds_no_engine` ; `test_f04b_preflight.py::test_computing_the_head_loads_no_legacy_module` ; `test_f04b_dormancy.py::test_frozen_files_are_unchanged` | réalisé |
| B03-4 | Normalisation sûre de `DATABASE_URL`, compatible avec les formats PostgreSQL existants | M | `database.py::normalize_f2_database_url` | `test_f04b_database.py::test_normalization_matches_the_legacy_formats_it_accepts`, `::test_database_url_outside_the_contract_is_refused_without_echo` ; `test_f04b_config.py::test_database_url_formats_are_normalized_to_psycopg2` | réalisé |
| B03-5 | Moteur possédé par le gestionnaire F04-B, fermeture explicite | M, A § 8.3, G | `resources.py` : `owner.own("engine", engine, engine.dispose)` | `test_f04b_resources.py::test_close_runs_each_closer_once_in_reverse_order_and_is_idempotent` ; **PG** `test_f04b_database.py::test_pool_is_capped_at_seven_and_waits_at_most_five_seconds` (0 connexion serveur après `dispose`) | réalisé |
| B04-1 | `pool_size=5`, `max_overflow=2`, `pool_timeout=5`, `pool_pre_ping=True`, `pool_recycle=1800`, `isolation_level="READ COMMITTED"` | M, G | `database.py::POOL_PARAMETERS` | `test_f04b_database.py::test_frozen_pool_parameters` | réalisé |
| B04-2 | Plafond, saturation, attente bornée, restitution | M | `QueuePool` | **PG** `test_f04b_database.py::test_pool_is_capped_at_seven_and_waits_at_most_five_seconds`, `::test_saturation_under_concurrency_never_exceeds_the_cap`, `::test_connections_are_returned_after_errors_and_rollbacks` | réalisé |
| B04-3 | Isolation READ COMMITTED effective | M, A § 8.3, G | `isolation_level` explicite | **PG** `test_f04b_database.py::test_isolation_is_read_committed_even_if_the_session_default_differs` | réalisé |
| B04-4 | Aucun retry transactionnel automatique | M | aucun retry | **PG** `test_f04b_database.py::test_pool_is_capped_at_seven_and_waits_at_most_five_seconds` ; `::test_unreachable_server_fails_once_without_retry` | réalisé |
| B04-5 | Valeurs non validées pour Railway ; dimensionnement réel à qualifier avant activation | M, A § 8.3, G | — | `docs/act_l1_f04b_resources_v03.md` § 9 risque 1 | documenté (ouvert pour l'activation) |

## 6. B05 — Ressources fournisseurs

| # | Exigence (synthèse) | Src | Implémentation | Tests | État |
|---|---|---|---|---|---|
| B05-1 | Enregistrer chaque ressource effectivement construite | M, A § 8.6, G | `resources.py::ResourceOwner.own` | `test_f04b_resources.py::test_resources_are_built_in_order_and_owned` | réalisé |
| B05-2 | Nettoyer les constructions partielles en ordre inverse | M, A § 9.2, G | `build_f2_resources` (`owner.close()` sur échec) | `test_f04b_resources.py::test_a_partial_build_closes_what_was_built_in_reverse_order` (6 étapes), `::test_a_partial_build_survives_a_failing_cleanup` | réalisé |
| B05-3 | Fermeture explicite et idempotente, une seule fois, malgré l'échec d'une autre | M, A § 8.6, G | `ResourceOwner.close` | `test_f04b_resources.py::test_close_runs_each_closer_once_in_reverse_order_and_is_idempotent`, `::test_close_continues_after_a_failing_closer_and_reports_it`, `::test_close_is_single_even_under_concurrent_calls`, `::test_owned_clients_are_really_closed_when_nothing_is_injected` | réalisé |
| B05-4 | Ne pas fermer les ressources injectées | M | `ResourceOwner.borrow` ; `_owns_transport`, `_owns_client` des adaptateurs | `test_f04b_resources.py::test_injected_resources_are_never_closed`, `::test_transport_errors_are_still_uncertain_and_closing_is_explicit`, `::test_claude_transport_keeps_one_request_and_no_retry` | réalisé |
| B05-5 | Aucun attribut privé d'adaptateur pour forcer une fermeture | M | `resources.py` n'appelle que `close()` / `dispose()` | `test_f04b_resources.py::test_the_module_never_uses_private_adapter_attributes_to_close` | réalisé |
| B05-6 | Supabase et Resend : httpx, contrats HTTP, TLS, `trust_env=False`, aucune redirection, délais, classification conservés ; extension minimale de fermeture | M, A § 8.4, G | `core/auth_f2/http_providers.py::close` | `test_f04b_resources.py::test_supabase_and_resend_keep_their_http_contract`, `::test_owned_httpx_clients_keep_tls_no_redirect_no_env_and_timeouts`, `::test_transport_errors_are_still_uncertain_and_closing_is_explicit` ; suites existantes `test_f2_normalize_providers.py`, `test_f2_http_flows.py` | réalisé |
| B05-7 | EODHD et FMP : sessions injectées historiques conservées ; mode `session_factory` (une session par appel, fermée) ; jamais de session partagée entre appels C3 concurrents ; combinaison ambiguë refusée ; `trust_env=False` pour les sessions F04-B | M, G | `core/decryptage_providers.py` : `session_factory`, `_session_source`, `_call`, `isolated_session` | `test_f04b_resources.py::test_historical_injected_session_is_unchanged`, `::test_session_and_factory_together_are_refused`, `::test_factory_mode_sends_exactly_the_historical_eodhd_request`, `::test_factory_mode_sends_exactly_the_historical_fmp_request`, `::test_each_concurrent_call_gets_its_own_session`, `::test_f04b_sessions_ignore_the_environment_and_never_retry`, `::test_built_market_transports_use_one_session_per_call`, `::test_default_resources_use_the_isolated_session_factory` | réalisé |
| B05-8 | Méthodes B2, retries, redirections, timeouts, classification des erreurs inchangés ; aucun assouplissement temporel | M, G | `get_json` inchangé | `test_f04b_resources.py::test_factory_mode_keeps_the_error_classification_and_always_closes`, `::test_no_request_is_sent_without_time_budget_and_the_session_is_closed` ; suite existante `test_authz_f2_pr9b2_units.py` (dont `test_the_default_http_session_never_retries`) | réalisé |
| B05-9 | Claude : `ClaudeTransport.create_message` et `max_retries=0` conservés ; fermeture du client SDK possédé ; clients injectés préservés | M, A § 8.4, G | `ClaudeTransport.close`, `_owns_client` | `test_f04b_resources.py::test_owned_claude_client_is_closed_once`, `::test_claude_transport_keeps_one_request_and_no_retry` ; suite existante `test_authz_f2_pr9b2_units.py` | réalisé |
| B05-10 | F04-B ne décide jamais qu'une ressource est sûre à fermer pendant un travail C3 actif (relève de F04-C / F04-E) | M, A § 8.6, § 10.3, G | docstring « LIMITE » de `resources.py` | `docs/act_l1_f04b_resources_v03.md` § 6.3 | documenté (hors F04-B) |

## 7. B06 — Précontrôles en lecture seule

| # | Exigence (synthèse) | Src | Implémentation | Tests (**PG** sauf mention) | État |
|---|---|---|---|---|---|
| B06-1 | Opération explicite, transaction effectivement en lecture seule | M, A § 8.7, G | `preflight.py::run_f2_preflight` (`SET TRANSACTION READ ONLY`, `ROLLBACK`) | `test_f04b_preflight.py::test_nominal_preflight_reports_and_writes_nothing`, `::test_the_preflight_transaction_is_really_read_only` | réalisé |
| B06-2 | Connexion au serveur attendu | M, G | `_checks` : base, utilisateur, majeure qualifiée | `test_f04b_preflight.py::test_a_server_that_is_not_the_configured_database_fails_closed`, `::test_an_unqualified_server_version_fails_closed`, `::test_an_unreachable_server_fails_closed` | réalisé |
| B06-3 | Isolation effective READ COMMITTED | M, G | `_checks` | `test_f04b_preflight.py::test_an_engine_without_read_committed_fails_closed` | réalisé |
| B06-4 | Version Alembic en base = tête attendue du code | M, A § 8.7, G | `expected_alembic_head`, `_checks` | `test_f04b_preflight.py::test_expected_head_is_the_single_head_of_the_code` (sans base), `::test_an_older_alembic_version_fails_closed_without_upgrade`, `::test_an_unexpected_head_or_several_rows_fail_closed`, `::test_missing_or_empty_alembic_version_fails_closed` | réalisé |
| B06-5 | Présence des objets F2 nécessaires | M, G | `F2_REQUIRED_TABLES`, `F2_REQUIRED_COLUMNS` | `test_f04b_preflight.py::test_a_missing_f2_table_fails_closed`, `::test_a_missing_f2_column_fails_closed` | réalisé |
| B06-6 | `auth_abuse_key_state` présent et compatible via `load_key_state` | M, A § 8.7, G | `load_key_state` | `test_f04b_preflight.py::test_missing_abuse_key_state_fails_closed_and_is_not_initialized`, `::test_keyring_incompatible_with_the_recorded_state_fails_closed`, `::test_recorded_previous_version_must_also_be_known` | réalisé |
| B06-7 | Interdits : aucun `alembic upgrade`, aucune `initialize_abuse_key_state`, aucune migration, création ou modification de données, rotation, réparation | M, A § 8.7, G | — (absence vérifiée) | `test_f04b_preflight.py::test_the_preflight_never_migrates_initializes_or_repairs` (sans base), `::test_nominal_preflight_reports_and_writes_nothing` ; `test_f2_dormancy.py::test_f04b_reference_matrix_is_closed_and_exact` (sans base) | réalisé |
| B06-8 | État absent ou incompatible → échec fermé | M, G | `F2PreflightError(code)` | tous les tests `*_fails_closed*` ci-dessus | réalisé |

## 8. Frontière, interdictions et dormance

| # | Exigence (synthèse) | Src | Vérification | État |
|---|---|---|---|---|
| F-1 | Modifications limitées : nouveaux modules F04-B, tests, `requirements.txt` et verrous, extensions techniques des deux modules fournisseurs, documentation | M, G | diff de la PR (liste exhaustive des fichiers dans le compte rendu) ; `test_f2_dormancy.py::test_f04b_reference_matrix_is_closed_and_exact` | réalisé |
| F-2 | `api.py`, `core/db.py`, porte F-02A (`core/route_policy/`) non modifiés | M, G | `test_f04b_dormancy.py::test_frozen_files_are_unchanged` (sha256 à `5ccc9e2`) | réalisé |
| F-3 | Runners et moteurs métier B1 / B2 / C3, routes et contrats HTTP non modifiés | M, G | diff : aucun fichier de `core/authz_f2/`, `core/authz_f2_http/`, `core/auth_f2_http/` ni des moteurs métier modifié ; suites existantes vertes | réalisé |
| F-4 | Aucune migration | M, G | `test_authz_f2_pr9b2_boundary.py::test_no_new_migration` ; jobs `ci / migrations-16`, `ci / migrations-18` | réalisé |
| F-5 | Aucune modification Railway, aucun déploiement, aucune activation F2, point d'entrée servi inchangé | M, A § 11, G | `test_f04b_dormancy.py::test_no_application_module_references_f04b`, `::test_the_served_entrypoint_ignores_a_complete_f2_configuration`, `::test_a_complete_f2_configuration_builds_no_f2_engine_or_composition`, `::test_the_real_legacy_server_answers_identically_with_a_complete_f2_configuration` | réalisé |
| F-6 | Tests avec secrets synthétiques, fournisseurs simulés, bases jetables ; jamais la base de production | M | `tests/f04b_support.py` ; garde-fous `tests/pg_guard.py`, `tests/conftest.py` | réalisé |
| F-7 | P1 (confiance réseau / IP) et P2 (drainage C3, COMMIT incertain) restent ouverts | A § 7, § 10, G | non traités par F04-B | ouvert (hors F04-B) |

## 9. Limites de cette traçabilité

1. **Texte du freeze non versionné** : les sources [M] et [G] sont des transmissions du fondateur hors dépôt.
   Ce document les synthétise ; il ne prouve pas qu'aucune autre exigence du freeze n'existe. Une
   reproduction intégrale exacte du freeze F04-B V0.3 doit être **fournie par le fondateur** si l'audit
   l'exige.
2. **Écarts d'interprétation déjà déclarés** (voir `docs/act_l1_f04b_resources_v03.md` § 9 et le compte
   rendu de la PR) : règle du modèle Claude recopiée (équivalence testée) ; transport httpx injecté jamais
   fermé ; précontrôle limité aux majeures PostgreSQL 16 et 18 ; aucun `connect_timeout` ajouté. Aucun de ces
   points ne modifie une décision du freeze ; ils sont soumis à l'audit.
3. **Preuve d'installation** : elle dépend de l'accès de la CI à l'index PyPI (aucun secret requis). Hors CI,
   sans accès à l'index, le test est sauté avec sa raison ; la CI refuse tout test sauté.
