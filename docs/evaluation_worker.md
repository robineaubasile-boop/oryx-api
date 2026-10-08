# oryx-evaluation-worker (R1-D1)

Worker interne qui branche T3 réel (interprétation locale) sur les
CognitiveEvents Décrypter **finalized**. Il n'expose aucune route, n'a pas de
domaine public, n'utilise pas FastAPI et n'impacte jamais le tour utilisateur
Décrypter. Après la complétion d'un run il s'arrête : aucun T5 / T6, aucune
inférence longitudinale, aucun état utilisateur global.

## Démarrage

Même repo, même Postgres que l'API, service Railway séparé
`oryx-evaluation-worker` (à créer APRÈS merge et validation ; cette PR ne
modifie pas Railway) :

```
python -m core.evaluation_worker
# équivalent : python scripts/run_evaluation_worker.py
```

Le worker refuse de démarrer (exit code 2, raison journalisée sans secret) si
une variable manque ou est invalide, ou si la migration
`0014_evaluation_run_leases` n'est pas appliquée.

## Variables d'environnement (toutes obligatoires)

| Variable | Format | Rôle |
| --- | --- | --- |
| `DATABASE_URL` | URL PostgreSQL | Même base que l'API. |
| `ANTHROPIC_API_KEY` | secret | Clé du fournisseur (jamais journalisée, jamais dans le repo). |
| `ORYX_EVALUATION_MODEL` | model id | UNIQUE model_id de D1B **et** D1D ; persisté dans le run et inclus dans l'`input_fingerprint`. Recommandé : `claude-opus-5-5`. Changer de modèle = nouvel `input_fingerprint`. |
| `ORYX_T3_INITIAL_CUTOFF` | UTC ISO-8601 explicite (`2026-10-07T20:00:00Z` ou `+00:00`) | Seuls les events fermés à partir de cet instant (`closed_at >= cutoff`) sont évalués. Aucune valeur par défaut, jamais `now()` ni epoch : aucun backfill silencieux des events historiques. |
| `ORYX_EVALUATION_POLL_SECONDS` | entier 1..3600 | Pause entre deux cycles sans travail. |
| `ORYX_EVALUATION_LEASE_SECONDS` | entier 60..86400 | Durée de la lease d'un run (horloge PostgreSQL). |
| `ORYX_EVALUATION_LLM_TIMEOUT_SECONDS` | entier 10..3600 | Timeout d'UN appel LLM. Contrainte : `LEASE >= TIMEOUT + 60`. |

Exemple de valeurs : `POLL=30`, `LEASE=600`, `TIMEOUT=300`.

## Prérequis base

- Migration `0014_evaluation_run_leases` appliquée (`alembic upgrade head`).
- Taxonomie `oryx-v1` bootstrapée (`python -m scripts.bootstrap_pedagogical_taxonomy_v1`)
  **et activée** (`activate_taxonomy_v1`, action explicite distincte). Sans
  release active conforme à la SPEC canonique (fingerprint
  `50b690b9…f7df5e2f7`), aucun run n'est démarré (fail closed, journal
  `[R1-D1] taxonomy_unavailable`).

## Comportement

1. **Recovery d'abord** : runs `running / candidate` sans lease valide
   (absente ou expirée) repris — même run, jamais un nouveau — uniquement si
   leurs versions correspondent EXACTEMENT à un bundle supporté (V2 courant
   ou V1 legacy, voir ci-dessous) et leur model_id et release à ceux du
   worker (sinon `unsupported_version`, aucune mutation). La release
   utilisée est celle du run (même `retired`), jamais la release active
   courante.
2. **Nouveaux events** : Décrypter, `finalized`, `closed_at >= cutoff`,
   versions T2 supportées, contributions capturées par le runtime
   `decryptage-cognitive-runtime-v2`, et AUCUN run initial existant (même
   `failed` : pas de retry automatique).
3. Pour chaque run : D1A (provenance) -> taxonomie vérifiée -> manifest /
   `input_fingerprint` / `evaluation_dedup_key` -> TX start + claim lease ->
   D1B (sans transaction) -> si observations : renew + D1D batch -> TX
   résultat atomique (observations + mappings + complete).
4. Arrêt : SIGTERM / SIGINT terminent l'élément en cours. Un crash ne fait
   jamais échouer un run : sa lease expire et il est repris.

## Versions et bundles d'exécution (R1-D1E)

L'exécution d'un run (prompts, contrats et validation D1B / D1D) est
sélectionnée par un bundle versionné ; les versions PERSISTÉES du run
déterminent le bundle de sa recovery.

| Version | `LEGACY_V1_BUNDLE` (R1-D1) | `CURRENT_V2_BUNDLE` (R1-D1E) |
| --- | --- | --- |
| evaluation_input_schema | `decryptage-evaluation-input-v1` | `decryptage-evaluation-input-v1` |
| normalization | `decryptage-normalization-v1` | `decryptage-normalization-v1` |
| local_stage | `decryptage-local-stage-v1` | `decryptage-local-stage-v2` |
| capability_mapping | `decryptage-capability-mapping-v1` | `decryptage-capability-mapping-v2` |
| evaluation_schema | `decryptage-evaluation-schema-v1` | `decryptage-evaluation-schema-v2` |
| evaluator | `decryptage-local-evaluation-pipeline-v1` | `decryptage-local-evaluation-pipeline-v2` |
| prompt_spec | `decryptage-t3-prompt-bundle-v1` | `decryptage-t3-prompt-bundle-v2` |

- **Nouveaux runs initiaux : V2 uniquement.** D1B émet `stage_basis`
  (quatre booléens descriptifs) pour une observation supportive ; le serveur
  dérive `local_stage` (application -> comprehension -> discovery -> none,
  jamais mastery). D1D évalue chaque capacité candidate (`supported` +
  `reason`) ; le serveur dérive `localized` / `competency_only`. Seuls le
  stade et la localisation dérivés sont persistés : aucune colonne, aucune
  migration.
- **Run V1 `running / candidate`** (laissé par le worker R1-D1) : repris
  avec le bundle V1 exact (mêmes prompts, contrats, `input_fingerprint`).
- **Run V1 `completed` / `failed`** : intact ; jamais de retry, jamais de
  second run initial V2 pour son event (un seul run initial par event,
  toutes versions confondues).
- **Combinaison de versions inconnue ou mixte** : `unsupported_version`,
  aucune mutation.
- Toujours au plus deux appels LLM par run (D1B, puis D1D batch si au
  moins une observation). Journaux `run_started` / `recovery` : nom du
  bundle (`current-v2` / `legacy-v1`).

## Journaux

`[R1-D1] discovered / run_started / lease_claimed / d1b_completed /
lease_renewed / d1d_completed / run_completed / run_failed / lease_lost /
recovery / unsupported_version / not_evaluable / taxonomy_unavailable` :
identifiants techniques et compteurs uniquement, jamais de texte
utilisateur, de stimulus, d'aide, de prompt ni de réponse LLM.

## failure_code (vocabulaire fermé)

`invalid_evaluation_input`, `provider_error`, `provider_timeout`,
`invalid_d1b_output`, `invalid_d1d_output`, `input_fingerprint_mismatch`,
`internal_validation_error`. Une lease perdue n'est jamais écrite : l'ancien
worker abandonne son résultat sans aucune mutation.
