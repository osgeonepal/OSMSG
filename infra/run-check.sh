#!/usr/bin/env bash
# Backfill 'stub' changesets (edits captured but metadata missing) from the OSM API. Runs inside the live
# worker container so it uses the deployed osmsg build; idempotent and only touches closed changesets.
set -euo pipefail

dsn="${OSMSG_PSQL_DSN:-postgresql://osmsg:osmsg@db:5432/osmsg}"

echo "[check] backfilling stub changesets"
exec docker exec infra-worker-1 osmsg maintain check --psql-dsn "${dsn}" --fix
