#!/usr/bin/env bash
# Advance the local history artifact to the newest published month, then drop the Postgres rows that
# history now covers. The API is bounced between the two steps so it serves the advanced frontier before
# the prune runs; otherwise a stale cached frontier would still expect the pruned rows from Postgres.
set -euo pipefail

cd /opt/osmsg/infra

artifact_host="${OSMSG_ARTIFACT_DIR:-/mnt/mnt/osmsg/artifact}"
dsn="${OSMSG_PSQL_DSN:-postgresql://osmsg:osmsg@db:5432/osmsg}"
repo="${OSMSG_HISTORY_REPO:-kshitijrajsharma/osmsg-history}"

echo "[artifact-refresh] advancing ${artifact_host} from ${repo}"
docker compose run --rm -v "${artifact_host}:/artifact" --entrypoint osmsg worker \
  maintain refresh --artifact-dir /artifact --repo "${repo}"

echo "[artifact-refresh] reloading the API onto the advanced frontier"
docker compose restart api

echo "[artifact-refresh] pruning Postgres to the local frontier"
exec docker compose run --rm -v "${artifact_host}:/artifact" --entrypoint osmsg worker \
  maintain prune-pg --psql-dsn "${dsn}" --history-url /artifact
