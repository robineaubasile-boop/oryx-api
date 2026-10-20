# shellcheck shell=bash
# Environnement minimal des processus Python de la CI (sourcé, pas exécuté).
#
# Les commandes sont lancées sous `env -i` avec cette liste blanche : aucune
# variable RAILWAY_*, DATABASE_* ou PG*, aucun secret, aucun jeton GitHub
# n'atteint pytest ni les contrôles Alembic. Seule l'URL de la base jetable
# est transmise, via ORYX_TEST_DATABASE_URL.
CLEAN_ENV=(
  env -i
  "PATH=${PATH}"
  "HOME=${HOME}"
  "LANG=C.UTF-8"
  "LC_ALL=C.UTF-8"
  "TZ=UTC"
  "PYTHONDONTWRITEBYTECODE=1"
  "ORYX_TEST_DATABASE_URL=${ORYX_TEST_DATABASE_URL:?ORYX_TEST_DATABASE_URL absent}"
)
# setup-python exporte LD_LIBRARY_PATH vers libpython (interpréteur partagé).
if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
  CLEAN_ENV+=("LD_LIBRARY_PATH=${LD_LIBRARY_PATH}")
fi
