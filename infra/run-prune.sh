#!/usr/bin/env bash
# Delete Postgres rows now covered by published history. Runs the osmsg CLI in the compose network
# (to reach the db service); a no-op until the frontier advances after a maintain publish.
set -euo pipefail

cd /opt/osmsg/infra

dsn="${OSMSG_PSQL_DSN:-postgresql://osmsg:osmsg@db:5432/osmsg}"
repo="${OSMSG_HISTORY_REPO:-kshitijrajsharma/osmsg-history}"

echo "[prune] pruning Postgres covered by ${repo}"
exec docker compose run --rm --entrypoint osmsg worker \
  maintain prune-pg --psql-dsn "${dsn}" --history-url "hf://datasets/${repo}"
