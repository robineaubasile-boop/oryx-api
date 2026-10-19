#!/usr/bin/env bash
# Contrôles Alembic indépendants (jobs ci / migrations-NN).
#
#   ci/run_migration_checks.sh <leg> <python> <reports_dir> <base_offline>
#
# Même environnement minimal que pytest (ci/clean_env.sh) ; DATABASE_URL
# n'est injectée que dans les sous-processus Alembic de ci/check_migrations.py.
set -uo pipefail

leg="${1:?leg}"
python="${2:?python}"
reports="${3:?reports_dir}"
offline_database="${4:?base_offline}"
mkdir -p "$reports"
# shellcheck source=ci/clean_env.sh
source ci/clean_env.sh

"${CLEAN_ENV[@]}" "$python" ci/verify_test_database.py \
  --expected-version "${ORYX_CI_POSTGRES_EXPECTED_VERSION:?version attendue absente}" \
  --report "$reports/postgres.json" || exit 1

"${CLEAN_ENV[@]}" "$python" ci/check_migrations.py \
  --leg "$leg" --commit "${GITHUB_SHA:-local}" \
  --offline-database "$offline_database" --report "$reports/result.json" \
  2>&1 | tee "$reports/migrations.log"
exit "${PIPESTATUS[0]}"
