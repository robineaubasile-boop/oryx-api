#!/usr/bin/env bash
# Suite Python complète contre la base jetable du job (jobs ci / postgres-NN).
#
#   ci/run_pytest.sh <leg> <python> <reports_dir>
#
# Environnement minimal (ci/clean_env.sh) : seule ORYX_TEST_DATABASE_URL est
# transmise. Les tests de migration injectent eux-mêmes DATABASE_URL (la même
# URL jetable) dans leurs seuls sous-processus Alembic.
#
# Aucun filtrage : ni -k, ni -m, ni --deselect, ni parallélisation (les tests
# destructeurs partagent la base du job et s'exécutent l'un après l'autre).
set -uo pipefail

leg="${1:?leg}"
python="${2:?python}"
reports="${3:?reports_dir}"
mkdir -p "$reports"
# shellcheck source=ci/clean_env.sh
source ci/clean_env.sh

"${CLEAN_ENV[@]}" "$python" ci/verify_test_database.py \
  --expected-version "${ORYX_CI_POSTGRES_EXPECTED_VERSION:?version attendue absente}" \
  --report "$reports/postgres.json" || exit 1

"${CLEAN_ENV[@]}" "$python" -m pytest \
  -p no:cacheprovider \
  -o junit_family=xunit2 \
  -rfEsxX \
  --junitxml="$reports/junit.xml" \
  2>&1 | tee "$reports/pytest.log"
pytest_status=${PIPESTATUS[0]}
echo "pytest exit code: ${pytest_status}"

"$python" ci/check_test_report.py --kind pytest --leg "$leg" \
  --junit "$reports/junit.xml" --log "$reports/pytest.log" \
  --exit-code "$pytest_status" --commit "${GITHUB_SHA:-local}" \
  --extra "$reports/postgres.json" --extra "$reports/environment.json" \
  --out "$reports/result.json"
