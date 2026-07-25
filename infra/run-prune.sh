#!/usr/bin/env bash
# Delete Postgres rows now covered by published history (created_at < frontier - overlap). Runs the
# osmsg CLI inside the compose network so it can reach the db service. A no-op until the frontier
# advances after the monthly maintain publish.
set -euo pipefail

cd /opt/osmsg/infra

dsn="${OSMSG_PSQL_DSN:-postgresql://osmsg:osmsg@db:5432/osmsg}"
repo="${OSMSG_HISTORY_REPO:-kshitijrajsharma/osmsg-history}"

echo "[prune] pruning Postgres covered by ${repo}"
exec docker compose run --rm --entrypoint osmsg worker \
  maintain prune-pg --psql-dsn "${dsn}" --history-url "hf://datasets/${repo}"
