#!/usr/bin/env bash
# Delete Postgres rows now covered by published history. Runs the osmsg CLI in the compose network
# (to reach the db service); a no-op until the frontier advances after a maintain publish.
set -euo pipefail

cd /opt/osmsg/infra

dsn="${OSMSG_PSQL_DSN:-postgresql://osmsg:osmsg@db:5432/osmsg}"
repo="${OSMSG_HISTORY_REPO:-kshitijrajsharma/osmsg-history}"
# Keep ~a month beyond the frontier so global recent-window queries (<=30d) retain their pre-frontier days.
overlap_days="${OSMSG_PRUNE_OVERLAP_DAYS:-40}"

echo "[prune] pruning Postgres covered by ${repo}, keeping ${overlap_days}d beyond the frontier"
exec docker compose run --rm --entrypoint osmsg worker \
  maintain prune-pg --psql-dsn "${dsn}" --history-url "hf://datasets/${repo}" --overlap-days "${overlap_days}"
