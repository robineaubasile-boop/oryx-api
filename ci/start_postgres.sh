#!/usr/bin/env bash
# Démarre un PostgreSQL jetable pour un job CI, puis crée sa base de test.
#
#   ci/start_postgres.sh <16|18> <nom_de_base> [<autre_base_jetable>...]
#
# - Image officielle épinglée par digest (aucun tag flottant).
# - Conteneur en réseau HÔTE (--network host), écoute sur 127.0.0.1
#   uniquement. En réseau bridge (port publié, service container GitHub),
#   PostgreSQL voit la connexion arriver sur l'adresse du conteneur
#   (172.17.x.x) : inet_server_addr() n'est alors pas une boucle locale et
#   le garde-fou serveur des tests destructeurs refuse, à juste titre, la
#   base. En réseau hôte, inet_server_addr() vaut 127.0.0.1.
# - Mot de passe aléatoire propre au job (aucun secret GitHub).
# - Base ^oryx_[a-z0-9_]*_test$ marquée du commentaire sentinelle
#   'oryx-disposable-test-db'.
#
# ORYX_TEST_DATABASE_URL désigne la première base ; les suivantes (même
# serveur, mêmes garde-fous) servent aux contrôles Alembic offline.
#
# Écrit dans $GITHUB_ENV (s'il existe) ORYX_TEST_DATABASE_URL et
# ORYX_CI_POSTGRES_EXPECTED_VERSION ; n'exporte aucune variable PG* ni
# DATABASE_*.
set -euo pipefail

major="${1:?usage: ci/start_postgres.sh <16|18> <nom_de_base>}"
database="${2:?usage: ci/start_postgres.sh <16|18> <nom_de_base>}"
databases=("${@:2}")

case "$major" in
  16)
    image="docker.io/library/postgres@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d"
    version="16.15"
    ;;
  18)
    image="docker.io/library/postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336"
    version="18.6"
    ;;
  *)
    echo "version PostgreSQL non supportée : $major" >&2
    exit 2
    ;;
esac

for name in "${databases[@]}"; do
  if [[ ! "$name" =~ ^oryx_[a-z0-9_]*_test$ ]]; then
    echo "nom de base refusé (attendu ^oryx_[a-z0-9_]*_test\$)" >&2
    exit 2
  fi
done

port=55432
container="oryx-ci-postgres-${major}"
password="$(openssl rand -hex 24)"
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  echo "::add-mask::${password}"
fi

docker pull --quiet "$image" >/dev/null
docker run --detach --name "$container" --network host \
  --env POSTGRES_PASSWORD="$password" \
  --env POSTGRES_HOST_AUTH_METHOD=scram-sha-256 \
  "$image" \
  -c listen_addresses=127.0.0.1 -c port="$port" \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null

# L'entrypoint officiel lance d'abord un serveur temporaire sans écoute TCP
# (initdb) : un pg_isready réussi sur 127.0.0.1 désigne le serveur définitif.
ready=0
for _ in $(seq 1 60); do
  if docker exec "$container" pg_isready -q -h 127.0.0.1 -p "$port" -U postgres; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  docker logs "$container" >&2 || true
  echo "PostgreSQL $major n'a pas démarré" >&2
  exit 1
fi

for name in "${databases[@]}"; do
  docker exec "$container" psql -v ON_ERROR_STOP=1 -q -p "$port" -U postgres -d postgres \
    -c "CREATE DATABASE ${name}" \
    -c "COMMENT ON DATABASE ${name} IS 'oryx-disposable-test-db'"
done

url="postgresql://postgres:${password}@127.0.0.1:${port}/${database}"
echo "PostgreSQL ${major} prêt : image ${image}, bases ${databases[*]}, 127.0.0.1:${port}"
docker image inspect "$image" --format 'RepoDigests={{json .RepoDigests}} Env={{json .Config.Env}}'

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "ORYX_TEST_DATABASE_URL=${url}"
    echo "ORYX_CI_POSTGRES_EXPECTED_VERSION=${version}"
    echo "ORYX_CI_POSTGRES_IMAGE=${image}"
  } >>"$GITHUB_ENV"
else
  echo "ORYX_TEST_DATABASE_URL=${url}"
fi
